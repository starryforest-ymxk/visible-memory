"""Explicit directory management, copy verification and bounded crash recovery."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid
from urllib.parse import unquote, urlsplit

import vm_config as config
from memory_v1 import VaultError, digest, identifier, json_bytes, now


def inventory(root, *, ignored=None, allowed_files=None):
    """Hash a stable, guarded tree. Only exact owned locks may be omitted."""
    root = config.directory(root)
    ignored = ignored or {}
    files, directories = {}, []
    if not root.exists():
        return {'exists': False, 'directories': [], 'files': {}}

    def visit(folder):
        for item in sorted(folder.iterdir()):
            config.guard(item)
            rel = item.relative_to(root).as_posix()
            if item.is_dir():
                directories.append(rel)
                visit(item)
            elif item.is_file():
                if rel in ignored and config.raw_file(item) == json_bytes(ignored[rel]):
                    continue
                if allowed_files is not None and rel not in allowed_files:
                    raise VaultError('TARGET_CHANGED', f'目标有未知文件，未读取或覆盖：{rel}')
                before = item.stat()
                sha = hashlib.sha256()
                with item.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        sha.update(block)
                after = config.guard(item).stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise VaultError('SOURCE_CHANGED', f'读取期间文件变化：{item}')
                files[rel] = {'size': after.st_size, 'mtime_ns': after.st_mtime_ns, 'sha256': sha.hexdigest()}
            else:
                raise VaultError('UNSAFE_PATH', f'拒绝特殊文件：{item}')
    visit(root)
    return {'exists': True, 'directories': sorted(directories), 'files': files}


def relative(root, rel):
    if not isinstance(rel, str):
        raise VaultError('INVALID_OPERATION_RECORD', '恢复记录的相对路径必须是字符串。')
    path = Path(rel)
    if path.is_absolute() or '..' in path.parts or not rel or '\\' in rel:
        raise VaultError('INVALID_OPERATION_RECORD', '恢复记录含越界的相对路径。')
    return config.guard(root / path)


def empty_target(root):
    permitted = {'entries', 'history', '_state', '_index', 'catalog'}
    value = {'exists': root.exists(), 'directories': [], 'files': {}}
    if root.exists():
        for item in sorted(root.iterdir()):
            config.guard(item)
            if not item.is_dir() or item.name not in permitted or any(item.iterdir()):
                raise VaultError('NONEMPTY_TARGET', '目标必须不存在或只包含允许的空库目录；不合并或覆盖。')
            value['directories'].append(item.name)
    return value


def nav_links(vault, conn):
    import vm_views
    count = 0
    stale = False
    warnings = []
    for rel, expected in vm_views.file_hashes(conn).items():
        path = relative(vault.root, rel)
        raw = config.raw_file(path)
        if raw is None:
            stale = True
            continue
        stale |= digest(raw) != expected
        # Only managed generated Markdown is inspected, never entry bodies.
        for match in re.finditer(r'\[[^\]\r\n]*\]\(([^)\r\n]+)\)', raw.decode('utf-8-sig')):
            ref = unquote(match.group(1))
            parts = urlsplit(ref)
            if ref.startswith('#'):
                continue
            if parts.scheme or parts.netloc or Path(parts.path).is_absolute():
                stale = True
                warnings.append({'path': rel, 'code': 'NON_RELATIVE_LINK'})
                continue
            target = Path(os.path.abspath(path.parent / parts.path))
            if not (target == vault.root or vault.root in target.parents):
                stale = True
                warnings.append({'path': rel, 'code': 'LINK_OUTSIDE_VAULT'})
                continue
            config.guard(target)
            if not target.exists():
                # Stale navigation can be copied unchanged, but is reported.
                stale = True
            count += 1
    return {'relative_links': count, 'views_stale': stale, 'navigation_warnings': warnings}


def source_health(root, expected_id):
    import memory
    import vm_operations
    import vm_views
    import vm_index
    header, header_sha = config.vault_header(root, expected_id=expected_id)
    vault = memory.Vault(root)
    index = vault._path('_index', 'search.sqlite')
    for db in (vault._path('_state', 'operations.sqlite'), index):
        for suffix in ('-journal', '-wal', '-shm'):
            if config.guard(db.with_name(db.name + suffix)).exists():
                raise VaultError('ACTIVE_DATABASE_LOG', f'数据库有日志侧车文件，未复制或修复：{db.name + suffix}')
    with contextlib.closing(vm_operations.connect(vault, header)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise VaultError('OPERATIONS_UNAVAILABLE', '持久操作库完整性检查失败，未重新初始化。')
        if vm_operations.pending(conn):
            raise VaultError('PENDING_VAULT_OPERATION', '库有未解决操作；请明确维护后再迁移。')
        flags = vm_operations.flags(conn)
        flags['views_stale'] |= vm_views.status(conn)['views_stale']
        links = nav_links(vault, conn)
    index_stale = bool(flags['index_stale']) or not index.is_file() or vault._path('_index', 'rebuild.pending').exists()
    if index.is_file():
        with index.open('rb') as stream:
            prefix = stream.read(100)
        if len(prefix) < 100 or prefix[:16] != b'SQLite format 3\x00':
            index_stale = True
        elif prefix[18:20] != b'\x01\x01':
            raise VaultError('ACTIVE_DATABASE_LOG', '检索库使用 WAL 模式；不会通过只读打开创建侧车文件。')
        else:
            try:
                with contextlib.closing(vm_index.connect(vault)) as conn:
                    delta = vm_index._catalog(vault, conn)
                    index_stale |= bool(delta['added'] or delta['changed'] or delta['deleted'] or delta['errors'])
            except (VaultError, sqlite3.Error):
                index_stale = True
    return {'vault_id': header['vault_id'], 'header_sha256': header_sha,
            'index_stale': index_stale, 'views_stale': bool(flags['views_stale']) or links['views_stale'],
            'relative_links': links['relative_links'], 'navigation_warnings': links['navigation_warnings']}


class DirectoryService:
    def __init__(self, config_path=None, *, program_root=None, fault=None, progress=None, cancelled=None):
        # Construction/import must not read any user state.
        self.config_path = Path(os.path.abspath(config_path or config.default_config()))
        self.program_root = Path(os.path.abspath(program_root or Path(__file__).parent.parent))
        self.fault = fault or (lambda stage, record: None)
        self.progress = progress or (lambda value: None)
        self.cancelled = cancelled or (lambda: False)

    def _path(self, operation_id):
        return config.guard(self.config_path.parent / 'operations' / (identifier(operation_id) + '.json'))

    def _save(self, record):
        path = self._path(record['operation_id'])
        before = config.raw_file(path)
        config.atomic(path, json_bytes(record), digest(before) if before is not None else None)

    def _stage(self, record, stage):
        record['phase'] = stage
        record.setdefault('stages', []).append({'phase': stage, 'at': now()})
        self._save(record)
        self.fault(stage, record)

    def _cancel(self):
        if self.cancelled():
            raise VaultError('CANCELLED', '已在安全点取消；已创建的库、副本和恢复记录保留。')

    def _protected(self, root):
        if config.overlap(root, self.program_root) or config.overlap(root, self.config_path.parent):
            raise VaultError('OVERLAPPING_PATHS', '记忆目录不能与程序或用户配置目录相互包含。')

    def _pending(self, ignore=None):
        folder = config.guard(self.config_path.parent / 'operations')
        if not folder.exists():
            return
        for path in sorted(folder.iterdir()):
            config.guard(path)
            if not path.is_file() or path.suffix != '.json':
                raise VaultError('UNKNOWN_OPERATION_FILE', f'目录管理记录中有未知项目：{path}')
            record = self._load(path.stem)
            if record['operation_id'] != ignore and record['phase'] != 'complete':
                raise VaultError('PENDING_LOCATION_OPERATION', '已有未完成目录操作；请明确恢复指定操作。', {'operation_id': record['operation_id'], 'path': str(path)})

    def _load(self, operation_id):
        path = self._path(operation_id)
        raw = config.raw_file(path)
        if raw is None:
            raise VaultError('OPERATION_NOT_FOUND', '未找到指定目录管理操作。')
        try:
            record = json.loads(raw)
            if (record['app'], record['schema_version'], record['operation_id'], record['config_path']) != ('visible-memory-location', 1, operation_id, str(self.config_path)):
                raise ValueError('操作标记或配置位置不匹配')
            if record['command'] not in {'use', 'create', 'relocate'} or not isinstance(record['request'], str):
                raise ValueError('未知操作')
            if record['phase'] not in {'intent', 'initializing', 'copying', 'copied', 'verified', 'config_committed', 'complete'}:
                raise ValueError('未知阶段')
            before = record['config_before']
            if before is not None and (not isinstance(before, dict) or set(before) != config.CONFIG_FIELDS or
                    before['schema_version'] != 1 or type(before['revision']) is not int or before['revision'] < 1):
                raise ValueError('非法操作前配置')
            if record['command'] == 'relocate' and (before is None or before['vault_root'] != record['source'] or before['vault_id'] != record['vault_id']):
                raise ValueError('源库身份与操作前配置不匹配')
            root = config.directory(record['target'])
            self._protected(root)
            if record['source']:
                source = config.directory(record['source'])
                self._protected(source)
                if config.overlap(source, root):
                    raise ValueError('源目标重叠')
            for rel, item in record['manifest']['files'].items():
                relative(root, rel)
                if type(item['size']) is not int or item['size'] < 0 or type(item['mtime_ns']) is not int or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
                    raise ValueError('非法文件清单')
            for rel in record['manifest']['directories']:
                relative(root, rel)
            for rel, temp in record.get('temporary', {}).items():
                relative(root, rel)
                p = relative(root, temp)
                if p.parent != relative(root, rel).parent or not p.name.startswith('.vm-relocate-' + operation_id + '-'):
                    raise ValueError('非法临时文件')
            allowed_locks = {str(self.config_path.parent / 'config.lock'), str(root / '_state' / 'write.lock')}
            if record['source']:
                allowed_locks.add(str(Path(record['source']) / '_state' / 'write.lock'))
            for item in record.get('locks', []):
                owner = item['owner']
                if item['path'] not in allowed_locks or owner.get('operation_id') != operation_id or owner.get('app') != 'visible-memory-settings':
                    raise ValueError('非法锁记录')
                if type(owner.get('pid')) is not int or owner['pid'] < 1:
                    raise ValueError('非法锁进程编号')
                identifier(owner.get('token'))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise VaultError('INVALID_OPERATION_RECORD', f'操作记录损坏；未自动重置：{exc}') from None
        return record

    def _preflight(self, command, path):
        config.guard(self.config_path)
        before, before_sha = config.read_config(self.config_path, missing_ok=True)
        target = config.directory(path)
        self._protected(target)
        self._pending()
        snapshot = {'command': command, 'config_path': str(self.config_path), 'config_before': before,
                    'config_before_sha256': before_sha, 'target': str(target), 'source': None,
                    'manifest': {'exists': False, 'directories': [], 'files': {}}}
        if command == 'use':
            header, sha = config.vault_header(target)
            snapshot.update(vault_id=header['vault_id'], target_header_sha256=sha)
        elif command == 'create':
            snapshot['target_before'] = empty_target(target)
        elif command == 'relocate':
            if before is None:
                raise VaultError('NOT_CONFIGURED', '尚未配置当前库，不能迁移。')
            source = config.directory(before['vault_root'])
            self._protected(source)
            if config.overlap(source, target):
                raise VaultError('OVERLAPPING_PATHS', '源和目标不能相同或相互包含。')
            if config.raw_file(source / '_state' / 'write.lock') is not None:
                raise VaultError('LOCKED', '源库有写入锁；未移除。')
            snapshot['target_before'] = empty_target(target)
            snapshot['health'] = source_health(source, before['vault_id'])
            snapshot.update(source=str(source), vault_id=before['vault_id'], manifest=inventory(source))
        else:
            raise VaultError('UNKNOWN_COMMAND', '未知目录管理命令。')
        return snapshot

    def _summary(self, snapshot):
        files = snapshot['manifest']['files']
        return {'command': snapshot['command'], 'current_directory': (snapshot['config_before'] or {}).get('vault_root'),
                'target_directory': snapshot['target'], 'files': len(files), 'bytes': sum(v['size'] for v in files.values()),
                'source_retained': snapshot['command'] == 'relocate', 'health': snapshot.get('health'),
                'vault_id': snapshot.get('vault_id')}

    def _locks_ignored(self, record, root):
        return {Path(x['path']).relative_to(root).as_posix(): x['owner'] for x in record.get('locks', [])
                if Path(x['path']) == root / '_state' / 'write.lock'}

    def _record_lock(self, record, path, owner):
        record['locks'] = [x for x in record.get('locks', []) if x['path'] != str(path)] + [{'path': str(path), 'owner': owner}]
        self._save(record)

    def _copy(self, record):
        source, target = Path(record['source']), Path(record['target'])
        total = len(record['manifest']['files'])
        for rel in record['manifest']['directories']:
            relative(target, rel).mkdir(parents=True, exist_ok=True)
        for number, (rel, expected) in enumerate(sorted(record['manifest']['files'].items()), 1):
            self._cancel()
            src, dst = relative(source, rel), relative(target, rel)
            if dst.exists():
                info = dst.stat()
                if (info.st_size, info.st_mtime_ns, digest(dst.read_bytes())) != (expected['size'], expected['mtime_ns'], expected['sha256']):
                    raise VaultError('TARGET_CHANGED', f'副本被修改，未覆盖：{dst}')
            else:
                temp_rel = record.setdefault('temporary', {}).get(rel)
                if not temp_rel:
                    temp_rel = (Path(rel).parent / ('.vm-relocate-' + record['operation_id'] + '-' + str(number) + '.tmp')).as_posix()
                    record['temporary'][rel] = temp_rel
                    self._save(record)
                temp = relative(target, temp_rel)
                # A partial temp may only be continued if it equals a source prefix.
                done = 0
                if temp.exists():
                    done = temp.stat().st_size
                    if done > expected['size']:
                        raise VaultError('TARGET_CHANGED', f'临时副本被修改：{temp}')
                    with temp.open('rb') as old, src.open('rb') as original:
                        while block := old.read(1024 * 1024):
                            if original.read(len(block)) != block:
                                raise VaultError('TARGET_CHANGED', f'临时副本被修改，未覆盖：{temp}')
                with src.open('rb') as original, temp.open('ab' if temp.exists() else 'xb') as out:
                    original.seek(done)
                    while block := original.read(1024 * 1024):
                        self._cancel()
                        out.write(block)
                        self.progress({'phase': 'copying', 'completed': number - 1, 'total': total, 'path': rel})
                        self.fault('copy_chunk', record)
                    out.flush()
                    os.fsync(out.fileno())
                config.guard(temp)
                if digest(temp.read_bytes()) != expected['sha256']:
                    raise VaultError('SOURCE_CHANGED', f'复制内容与源清单不匹配：{src}')
                os.utime(temp, ns=(src.stat().st_atime_ns, expected['mtime_ns']))
                config.guard(dst)
                if dst.exists():
                    raise VaultError('TARGET_CHANGED', f'目标出现同名文件：{dst}')
                # Windows rename refuses an existing destination, unlike replace.
                os.rename(temp, dst)
            record.setdefault('copied', {})[rel] = expected['sha256']
            self._save(record)
            self.progress({'phase': 'copying', 'completed': number, 'total': total})
            self.fault('file_copied', record)

    def _verify_target(self, record, *, partial=False):
        target = Path(record['target'])
        expected = record['manifest']
        temporaries = set(record.get('temporary', {}).values())
        actual = inventory(target, ignored=self._locks_ignored(record, target),
                           allowed_files=set(expected['files']) | (temporaries if partial else set()))
        allowed_dirs = set(expected['directories']) | {'_state'}
        if not set(actual['directories']).issubset(allowed_dirs):
            raise VaultError('TARGET_CHANGED', '目标出现未知目录，未覆盖。')
        for rel, item in actual['files'].items():
            if rel in expected['files']:
                if item != expected['files'][rel]:
                    raise VaultError('TARGET_CHANGED', f'副本已修改：{rel}')
            elif partial and rel in temporaries:
                # The copy routine checks exact source-prefix bytes before continuing.
                continue
            else:
                raise VaultError('TARGET_CHANGED', f'目标出现未知文件，未覆盖：{rel}')
        if not partial and (actual['files'] != expected['files'] or set(actual['directories']) != set(expected['directories'])):
            raise VaultError('COPY_INCOMPLETE', '副本文件或目录尚不完整。')
        return actual

    def _expected_config(self, record):
        before = record['config_before']
        vault_id = record.get('vault_id')
        if vault_id is None:
            return None
        return {'schema_version': 1, 'revision': (before['revision'] if before else 0) + 1,
                'vault_root': record['target'], 'vault_id': vault_id}

    def _facts(self, record):
        error = None
        try:
            actual, _ = config.read_config(self.config_path, missing_ok=True)
            switched = bool(record.get('config_committed')) or (actual is not None and actual == self._expected_config(record))
        except (VaultError, OSError, ValueError) as exc:
            actual, switched, error = None, None, str(exc)
        count = len(record.get('copied', {}))
        return {'operation_id': record['operation_id'], 'record_path': str(self._path(record['operation_id'])),
                'phase': record['phase'], 'data_copied': count == len(record['manifest']['files']) if record['command'] == 'relocate' else None,
                'copied_files': count, 'total_files': len(record['manifest']['files']),
                'data_verified': bool(record.get('data_verified')), 'config_applied': switched,
                'current_directory': (actual or {}).get('vault_root'), 'target_directory': record['target'],
                'source_retained': record['command'] == 'relocate', 'pending_recovery': record['phase'] != 'complete',
                'applied': True, 'verified': bool(record.get('data_verified')) and switched and record['phase'] == 'complete',
                'config_status_error': error, 'config_replaced': record.get('config_replaced', switched),
                'health': record.get('health')}

    def _perform(self, record, *, recovering=False):
        from memory import Vault
        target = Path(record['target'])
        command = record['command']
        with contextlib.ExitStack() as stack:
            # The config lock was acquired by the caller.
            if command == 'relocate':
                source = Path(record['source'])
                for root in (source, target):
                    stack.enter_context(config.lock(root / '_state' / 'write.lock', operation_id=record['operation_id'],
                                                    on_acquired=lambda path, owner: self._record_lock(record, path, owner)))
                if inventory(source, ignored=self._locks_ignored(record, source)) != record['manifest']:
                    raise VaultError('SOURCE_CHANGED', '源库在预览或中断后变化，未覆盖或切换。')
                source_health(source, record['vault_id'])
                self._verify_target(record, partial=True)
                self._stage(record, 'copying')
                self._copy(record)
                self._stage(record, 'copied')
                self._cancel()
                if inventory(source, ignored=self._locks_ignored(record, source)) != record['manifest']:
                    raise VaultError('SOURCE_CHANGED', '复制期间源库变化，配置保持原值。')
                self._verify_target(record)
                health = source_health(target, record['vault_id'])
                if health != record['health']:
                    raise VaultError('VERIFY_FAILED', '副本身份、操作库或导航校验不一致。')
            elif command == 'create':
                self._stage(record, 'initializing')
                service = self

                class ManagedInit(Vault):
                    @contextlib.contextmanager
                    def _lock(self):
                        with config.lock(self.root / '_state' / 'write.lock', operation_id=record['operation_id'],
                                         on_acquired=lambda path, owner: service._record_lock(record, path, owner)) as owner:
                            if not recovering:
                                value = inventory(self.root, ignored={'_state/write.lock': owner}, allowed_files=set())
                                if set(value['directories']) - {'entries', 'history', '_state', '_index', 'catalog'}:
                                    raise VaultError('TARGET_CHANGED', '初始化加锁期间目标出现未知目录。')
                            yield

                    def _atomic(self, path, data, expected=None, new=False):
                        if path == self.root / '_state' / 'vault.json':
                            record['vault_id'] = json.loads(data)['vault_id']
                            service._save(record)
                        return super()._atomic(path, data, expected, new)

                if recovering and (target / '_state' / 'vault.json').exists():
                    header, _ = Vault(target)._header()
                    if header['vault_id'] != record.get('vault_id'):
                        raise VaultError('TARGET_CHANGED', '创建出的库身份发生变化。')
                    if any((target / 'entries').iterdir()):
                        raise VaultError('TARGET_CHANGED', '创建期间出现记忆条目，不会重新初始化。')
                if not recovering and empty_target(target) != record['target_before']:
                    raise VaultError('TARGET_CHANGED', '初始化前目标目录发生变化。')
                result = ManagedInit(target).execute('init', request=record['request'], apply=True)
                if not result.get('ok'):
                    raise VaultError('INITIALIZATION_INCOMPLETE', '空库初始化未完成。', result)
                if not recovering and result.get('already_initialized') and not record.get('vault_id'):
                    raise VaultError('TARGET_CHANGED', '并发操作在目标创建了另一套库，未接入。')
                header, _ = config.vault_header(target, expected_id=record.get('vault_id'))
                record['vault_id'] = header['vault_id']
                record['created_manifest'] = inventory(target)
                self._save(record)
            else:
                header, sha = config.vault_header(target, expected_id=record['vault_id'])
                if sha != record['target_header_sha256']:
                    raise VaultError('TARGET_CHANGED', '目标库配置在预览后变化。')
            record['data_verified'] = True
            self._stage(record, 'verified')
            self._cancel()
            actual, actual_sha = config.read_config(self.config_path, missing_ok=True)
            expected = self._expected_config(record)
            if actual != expected:
                if actual_sha != record['config_before_sha256']:
                    raise VaultError('CONFIG_CHANGED', '配置在操作期间变化，未切换。')
                self.fault('before_config', record)
                config.write_config(self.config_path, record['config_before_sha256'], record['config_before'], target, record['vault_id'])
                self.fault('after_config', record)
            record['config_committed'] = True
            self._stage(record, 'config_committed')
            config.vault_header(target, expected_id=record['vault_id'])
            self._stage(record, 'complete')
        return {'ok': True, **self._facts(record)}

    def _apply(self, snapshot, token):
        if token != digest(json_bytes({k: v for k, v in snapshot.items() if k != 'request'})):
            raise VaultError('STALE_PREVIEW', '预览状态已变化或 token 不匹配，请重新预览。')
        if snapshot['command'] == 'use' and snapshot['config_before'] and snapshot['config_before']['vault_root'] == snapshot['target'] and snapshot['config_before']['vault_id'] == snapshot['vault_id']:
            return {'ok': True, 'applied': False, 'verified': True, 'unchanged': True, 'current_directory': snapshot['target']}
        operation_id = str(uuid.uuid4())
        record = {**snapshot, 'app': 'visible-memory-location', 'schema_version': 1, 'operation_id': operation_id,
                  'phase': 'intent', 'created_at': now(), 'data_verified': False, 'copied': {}, 'locks': []}
        try:
            with config.lock(self.config_path.parent / 'config.lock', operation_id=operation_id) as owner:
                # Recheck before creating a recovery record or changing a vault.
                if digest(json_bytes(self._preflight(snapshot['command'], snapshot['target']))) != token:
                    raise VaultError('STALE_PREVIEW', '锁内检查发现状态变化，请重新预览。')
                record['locks'] = [{'path': str(self.config_path.parent / 'config.lock'), 'owner': owner}]
                self._save(record)
                self.fault('intent', record)
                return self._perform(record)
        except (VaultError, OSError, ValueError, sqlite3.Error) as exc:
            if self._path(operation_id).exists():
                if getattr(exc, 'details', None) and isinstance(exc.details, dict) and exc.details.get('path') == str(self.config_path) and exc.details.get('committed'):
                    record['config_replaced'] = True
                record['last_error'] = {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}
                try:
                    self._save(record)
                    facts = self._facts(record)
                except (VaultError, OSError, ValueError):
                    facts = {'operation_id': operation_id, 'record_path': str(self._path(operation_id)), 'pending_recovery': True, 'applied': True}
                return {'ok': False, **facts, 'error': record['last_error']}
            raise

    def _recover_snapshot(self, record):
        self._pending(ignore=record['operation_id'])
        current, current_sha = config.read_config(self.config_path, missing_ok=True)
        if current_sha != record['config_before_sha256'] and current != self._expected_config(record):
            raise VaultError('CONFIG_CHANGED', '配置与操作前后状态均不匹配，未自动选择目录。')
        for item in record.get('locks', []):
            config.verify_recoverable_lock(item['path'], item['owner'])
        target = Path(record['target'])
        if record['command'] == 'relocate':
            source = Path(record['source'])
            if inventory(source, ignored=self._locks_ignored(record, source)) != record['manifest']:
                raise VaultError('SOURCE_CHANGED', '源库在中断后变化；不覆盖后来内容。')
            actual_target = self._verify_target(record, partial=True)
        elif record['command'] == 'use':
            header, sha = config.vault_header(target, expected_id=record['vault_id'])
            if sha != record['target_header_sha256']:
                raise VaultError('TARGET_CHANGED', '目标库配置在中断后变化。')
            actual_target = {'header_sha256': sha}
        else:
            actual_target = inventory(target, ignored=self._locks_ignored(record, target))
            if record.get('created_manifest') and actual_target != record['created_manifest']:
                raise VaultError('TARGET_CHANGED', '创建出的库在中断后变化；未覆盖。')
        return {'operation_sha256': digest(config.raw_file(self._path(record['operation_id']))),
                'config_sha256': current_sha, 'target_state': actual_target}

    def recover(self, operation_id, request, *, apply=False, preview_token=None):
        config.require_request(request)
        record = self._load(identifier(operation_id))
        if record['phase'] == 'complete':
            return {'ok': True, 'applied': False, 'already_complete': True, **{k: v for k, v in self._facts(record).items() if k != 'applied'}}
        try:
            snap = self._recover_snapshot(record)
        except (VaultError, OSError, ValueError) as exc:
            facts = {k: v for k, v in self._facts(record).items() if k != 'applied'}
            # Read-only recovery checks still report whether the earlier operation switched.
            if not apply:
                raise VaultError(getattr(exc, 'code', 'IO_ERROR'), str(exc), facts) from None
            return {'ok': False, 'applied': False, **facts, 'error': {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}}
        token = digest(json_bytes(snap))
        if not apply:
            return {'ok': True, 'applied': False, 'preview': True, 'preview_token': token,
                    'summary': self._summary(record), **{k: v for k, v in self._facts(record).items() if k != 'applied'}}
        if preview_token != token:
            raise VaultError('STALE_PREVIEW', '恢复预览已变化，请重新预览。')
        try:
            # All owners were proven exited above. Verify again immediately before removal.
            for item in record.get('locks', []):
                if config.verify_recoverable_lock(item['path'], item['owner']):
                    config.guard(item['path']).unlink()
            with config.lock(self.config_path.parent / 'config.lock', operation_id=operation_id,
                             on_acquired=lambda path, owner: self._record_lock(record, path, owner)):
                current, sha = config.read_config(self.config_path, missing_ok=True)
                if sha != snap['config_sha256']:
                    raise VaultError('CONFIG_CHANGED', '恢复加锁期间配置变化。')
                record.setdefault('recoveries', []).append({'request': request, 'at': now()})
                self._save(record)
                return self._perform(record, recovering=True)
        except (VaultError, OSError, ValueError, sqlite3.Error) as exc:
            record['last_error'] = {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}
            self._save(record)
            return {'ok': False, **self._facts(record), 'error': record['last_error']}

    def execute(self, command, *, request, path=None, apply=False, preview_token=None, operation_id=None):
        config.require_request(request)
        result = self._execute(command, request=request, path=path, apply=apply, preview_token=preview_token, operation_id=operation_id)
        return {**result, 'operation': command, 'request': request}

    def _execute(self, command, *, request, path=None, apply=False, preview_token=None, operation_id=None):
        config.require_request(request)
        if command in ('get', 'doctor') and apply:
            raise VaultError('INVALID_INPUT', '只读设置命令不接受 apply。')
        if command == 'get':
            value, sha = config.read_config(self.config_path, missing_ok=True)
            return {'ok': True, 'applied': False, 'configured': value is not None, 'config': value,
                    'config_path': str(self.config_path), 'config_sha256': sha, 'vault_inspected': False}
        if command == 'doctor':
            result = {'ok': True, 'applied': False, 'python': sys.version, 'python_supported': sys.version_info >= (3, 11),
                      'sqlite': sqlite3.sqlite_version, 'windows': os.name == 'nt', 'vault_inspected': False}
            try:
                with contextlib.closing(sqlite3.connect(':memory:')) as conn:
                    conn.execute("CREATE VIRTUAL TABLE probe USING fts5(value, tokenize='trigram')")
                result['fts5_trigram'] = True
            except sqlite3.Error as exc:
                result.update(fts5_trigram=False, sqlite_error=str(exc))
            try:
                import tkinter
                result.update(tkinter=True, tk_version=tkinter.TkVersion, gui_window_tested=False)
            except ImportError as exc:
                result.update(tkinter=False, tkinter_error=str(exc), gui_window_tested=False)
            result['ok'] = result['python_supported'] and result['fts5_trigram']
            return result
        if command == 'recover':
            return self.recover(operation_id, request, apply=apply, preview_token=preview_token)
        snapshot = self._preflight(command, path)
        if not apply:
            return {'ok': True, 'applied': False, 'preview': True, 'preview_token': digest(json_bytes(snapshot)),
                    'summary': self._summary(snapshot), 'config_path': str(self.config_path)}
        if not preview_token:
            raise VaultError('PREVIEW_REQUIRED', '应用必须携带本次有效的 preview_token，请先预览。')
        snapshot['request'] = request
        # The request is audit text; the token covers state and action, not identity.
        token_snapshot = {k: v for k, v in snapshot.items() if k != 'request'}
        if preview_token != digest(json_bytes(token_snapshot)):
            raise VaultError('STALE_PREVIEW', '预览状态已变化，请重新预览。')
        return self._apply(snapshot, preview_token)
