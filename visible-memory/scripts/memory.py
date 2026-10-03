#!/usr/bin/env python3
"""Explicit-only Markdown memory v2. Read commands never persist changes."""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
import memory_v1 as legacy
from memory_v1 import (VaultError, digest, now, json_bytes, identifier, normalized,
                       md_text, validate_entry, render_entry, parse_entry, fingerprint,
                       TYPES, VIEWS, FIELDS, META)
import vm_index as index
import vm_operations as operations
import vm_views as views
READS = legacy.READS
WRITES = legacy.WRITES | {'migrate'}

class Vault(legacy.Vault):
    def __init__(self, root, *, expected_vault_id=None):
        super().__init__(root)
        self.expected_vault_id = expected_vault_id

    @contextlib.contextmanager
    def _lock(self):
        with super()._lock():
            # Pin the configured identity again inside the write lock.
            if self.expected_vault_id is not None:
                self._header()
            yield

    def _atomic(self, path, data, expected=None, new=False):
        """Track the content commit point even if post-replace verification fails."""
        self._guard(path)
        if not path.parent.is_dir():
            raise VaultError('MISSING_DIRECTORY', f'目录不存在：{path.parent}')
        temp = self._path(*path.relative_to(self.root).parts[:-1], '.vm-' + str(uuid.uuid4()) + '.tmp')
        replaced = False
        try:
            with temp.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._guard(path)
            if new and path.exists():
                raise VaultError('CONFLICT', f'目标文件已存在：{path}')
            if expected is not None and (not path.is_file() or digest(self._raw(path)) != expected):
                raise VaultError('CONFLICT', f'文件已被其他操作修改：{path}')
            os.replace(temp, path)
            replaced = True
            if path == getattr(self, '_content_target', None):
                self._content_replaced = True
            if self._raw(path) != data:
                raise VaultError('COMMITTED_UNVERIFIED', f'已替换但读回不匹配：{path}')
        except (OSError, VaultError) as exc:
            if replaced:
                raise VaultError('COMMITTED_UNVERIFIED', f'文件已替换但收尾校验失败：{exc}',
                                 {'path': str(path), 'committed': True}) from None
            raise
        finally:
            if temp.exists():
                self._guard(temp)
                temp.unlink()

    def _header(self):
        path = self._path('_state', 'vault.json')
        if not path.is_file():
            raise VaultError('NOT_INITIALIZED', '记忆库尚未初始化；读取不会创建文件。')
        raw = self._raw(path)
        try:
            value = json.loads(raw)
            if value.get('app') != 'visible-memory' or value.get('format_version') not in (1, 2):
                raise ValueError('未知应用或格式版本')
            identifier(value.get('vault_id'))
            if value['format_version'] == 2 and value.get('operations_schema') != operations.SCHEMA:
                raise ValueError('未知持久库版本')
            if type(value.get('initializing', False)) is not bool:
                raise ValueError('初始化状态不合法')
        except (ValueError, TypeError, AttributeError) as exc:
            raise VaultError('INVALID_STATE', f'库配置损坏：{exc}') from None
        if self.expected_vault_id is not None and value['vault_id'] != self.expected_vault_id:
            raise VaultError('VAULT_ID_MISMATCH', '库 UUID 在调用期间发生变化，未使用另一套库。')
        return value, digest(raw)

    def _scan_complete(self):
        records, errors = self._scan()
        if errors:
            raise VaultError('INVALID_ENTRIES', '部分原文损坏；未覆盖或修复原文。', errors)
        return records

    def _ops(self, header, **kwargs):
        return operations.connect(self, header, **kwargs)

    def _build_index(self, conn, records, warnings):
        owned = operations.get_meta(conn, 'search_index_owned') == 'v2'
        if not owned:
            target = self._path('_index', 'search.sqlite')
            if self._path('_index', 'rebuild.pending').exists():
                raise VaultError('UNOWNED_INDEX', '检索目录已有未登记的维护标记；未覆盖。')
            for suffix in ('', '-journal', '-wal', '-shm'):
                if self._guard(target.with_name(target.name + suffix)).exists():
                    raise VaultError('UNOWNED_INDEX', '检索目录已有未登记的同名文件；未覆盖。')
            operations.set_meta(conn, 'search_index_owned', 'v2')
        result = index.build(self, records, allow_replace=owned)
        if result and result.get('recovery_backups'):
            warnings.append({'code': 'INDEX_RECOVERY_BACKUP', 'paths': result['recovery_backups']})

    def _flags(self, header):
        try:
            with contextlib.closing(self._ops(header)) as conn:
                result = operations.flags(conn)
                result['views_stale'] |= views.status(conn).get('views_stale', False)
                return result
        except (VaultError, sqlite3.Error) as exc:
            return {'pending_recovery': None, 'operations_unavailable': True,
                    'index_stale': None, 'views_stale': None, 'operations_error': str(exc)}

    def _read_v2(self, command, header, entry_id, query, include_archived, limit, offset,
                 entry_type, tags, scope):
        if command == 'show':
            return self._result_entry(*self._get(entry_id))
        if command == 'history':
            return legacy.Vault._read(self, command, {}, entry_id, query, include_archived, limit, offset)
        if command in ('search', 'list'):
            result = index.search(self, query=query if command == 'search' else '',
                                  include_archived=include_archived, limit=limit, offset=offset,
                                  entry_type=entry_type, tags=tags, scope=scope)
            flags = self._flags(header)
            for key in ('index_stale', 'views_stale'):
                result[key] = bool(result.get(key)) or flags.get(key)
            result.update({k: v for k, v in flags.items() if k not in ('index_stale', 'views_stale')})
            return result
        if command == 'status':
            result = index.status(self)
            flags = self._flags(header)
            for key in ('index_stale', 'views_stale'):
                flags[key] = bool(result.get(key)) or flags.get(key)
            result.update(flags)
            result['initializing'] = header.get('initializing', False)
            result['counts_basis'] = result.get('counts_source', 'index_snapshot')
            return result
        records, errors = self._scan()
        result = index.check(self, records)
        groups = {}
        for entry, raw, path in records:
            if entry['status'] == 'active':
                groups.setdefault(fingerprint(entry), []).append(entry['id'])
        result.update(active_count=sum(e['status'] == 'active' for e, _, _ in records),
                      archived_count=sum(e['status'] == 'archived' for e, _, _ in records),
                      duplicates=[v for v in groups.values() if len(v) > 1],
                      review_due=[e['id'] for e, _, _ in records if e['status'] == 'active'
                                  and e.get('review_after', '9999-12-31') <= legacy.dt.date.today().isoformat()],
                      verification='full_sha256', complete=not errors)
        result['errors'] = errors + result.get('errors', [])
        known_flags = self._flags(header)
        for key in ('index_stale', 'views_stale'):
            known_flags[key] = bool(result.get(key)) or known_flags.get(key)
        result.update(known_flags)
        try:
            with contextlib.closing(self._ops(header)) as conn:
                check = views.check(self, conn)
                result['view_check'] = check
                result['views_stale'] = bool(result.get('views_stale')) or check.get('views_stale', False)
        except (VaultError, sqlite3.Error) as exc:
            result['errors'].append({'code': 'OPERATIONS_UNAVAILABLE', 'message': str(exc)})
        result['complete'] = not result['errors'] and not result.get('view_check', {}).get('errors')
        return result

    def _initialize(self, request, apply):
        state_path = self._path('_state', 'vault.json')
        if state_path.exists():
            header, header_hash = self._header()
            if header['format_version'] == 1:
                raise VaultError('MIGRATION_REQUIRED', '这是 v1 记忆库，请明确执行 migrate 升级。')
            if not header.get('initializing'):
                return {'applied': False, 'already_initialized': True, 'format_version': 2}
        else:
            if self.root.exists():
                permitted = {'entries', 'history', '_state', '_index', 'catalog'}
                for item in self.root.iterdir():
                    self._guard(item)
                    if item.name not in permitted or not item.is_dir() or any(item.iterdir()):
                        raise VaultError('NONEMPTY_ROOT', '初始化只接受空目录或空的已知目录结构。')
            header = {'app': 'visible-memory', 'format_version': 2, 'operations_schema': operations.SCHEMA,
                      'vault_id': str(uuid.uuid4()), 'created_at': now(), 'initializing': True,
                      'initial_request': request}
            header_hash = None
        if not apply:
            return {'applied': False, 'preview': True, 'format_version': 2, 'root': str(self.root)}
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('entries', 'history', '_state', '_index', 'catalog'):
            self._path(name).mkdir(exist_ok=True)
        with self._lock():
            if header_hash is None:
                self._atomic(state_path, json_bytes(header), new=True)
                header_hash = digest(json_bytes(header))
            if any(self._path('entries').iterdir()):
                raise VaultError('INITIALIZATION_INCOMPLETE', '初始化期间出现条目，停止初始化，保留原文。')
            with contextlib.closing(self._ops(header, write=True,
                                   create=not self._path('_state', 'operations.sqlite').exists(), recover=True)) as conn:
                views.init_schema(conn)
                warnings = []
                self._build_index(conn, [], warnings)
                views.rebuild(self, conn, [], warnings)
                event_id = str(uuid.uuid5(uuid.UUID(header['vault_id']), 'visible-memory-v2-init'))
                self._event('init', header.get('initial_request', request), event_id=event_id, timestamp=header['created_at'])
                header['initializing'] = False
                header.pop('initial_request', None)
                self._atomic(state_path, json_bytes(header), expected=header_hash)
        return {'applied': True, 'format_version': 2, 'active_count': 0, 'archived_count': 0,
                'root': str(self.root), 'warnings': warnings}

    def _recover(self, conn, warnings):
        unresolved = []
        for op in operations.pending(conn):
            path = self._path('entries', op['id'] + '.md')
            if not path.exists():
                operations.mark(conn, op['operation_id'], 'uncertain', '目标缺失，不能推断从未提交。')
                unresolved.append({'id': op['id'], 'code': 'MISSING_PENDING_ENTRY'})
                continue
            entry, raw, path = self._get(op['id'])
            actual = digest(raw)
            if actual == op['sha256']:
                self._snapshot(entry, raw)
                self._event(op['command'], op['request'], entry, op['operation_id'], op['event_at'])
                with conn:
                    operations.save_receipt(conn, op)
                    conn.execute("UPDATE operations SET stage='complete',error=NULL WHERE operation_id=?", (op['operation_id'],))
                warnings.append({'code': 'RECOVERED_PENDING', 'id': entry['id']})
            elif actual == op['previous_sha256']:
                operations.mark(conn, op['operation_id'], 'aborted')
                operations.clean(conn, op['operation_id'], index=True, views=True)
                warnings.append({'code': 'UNCOMMITTED_OPERATION', 'id': entry['id']})
            else:
                with conn:
                    operations.save_receipt(conn, op)
                operations.mark(conn, op['operation_id'], 'uncertain', '中断后原文又有变化；保留现状与请求凭据。')
                unresolved.append({'id': entry['id'], 'code': 'RECOVERY_CONFLICT', 'path': str(path)})
        if unresolved:
            raise VaultError('RECOVERY_REQUIRED', '未决操作需要明确处理；未覆盖原文，也未重试新增。', unresolved)

    def _rebuild_v2(self, header, request, apply):
        if not apply:
            records = self._scan_complete()
            return {'applied': False, 'preview': True, 'entry_count': len(records),
                    'actions': ['恢复未完成操作', '完整校验原文', '重建检索索引', '刷新分页总览'], **self._flags(header)}
        with self._lock():
            with contextlib.closing(self._ops(header, write=True, recover=True)) as conn:
                warnings = []
                self._recover(conn, warnings)
                records = self._scan_complete()
                self._build_index(conn, records, warnings)
                operations.clean(conn, index=True)
                views.rebuild(self, conn, records, warnings)
                operations.clean(conn, views=True)
                self._event('rebuild', request)
                return {'applied': True, 'entry_count': len(records), 'index_stale': False,
                        'views_stale': False, 'warnings': warnings}

    def _migrate(self, request, apply):
        header, _ = self._header()
        if header['format_version'] == 2:
            with contextlib.closing(self._ops(header)) as conn:
                manifest = operations.get_meta(conn, 'migration_manifest')
            return {'applied': False, 'migrated': False, 'already_migrated': True,
                    'format_version': 2, 'backup_manifest': str(self._path(*Path(manifest).parts)) if manifest else None}
        old = legacy.Vault(self.root)
        state, old_hash = old._load_state()
        if state.get('initializing'):
            raise VaultError('INITIALIZATION_INCOMPLETE', '请先明确恢复 v1 初始化，再升级。')
        if state.get('pending'):
            raise VaultError('RECOVERY_REQUIRED', 'v1 有未决操作；请先使用 v1 明确恢复，再升级。', state['pending'])
        records = self._scan_complete()
        if not apply:
            return {'applied': False, 'preview': True, 'migrated': False, 'from_version': 1,
                    'to_version': 2, 'entry_count': len(records), 'receipt_count': len(state['requests']),
                    'preserves': ['entries bytes', 'UUID', 'timestamps', 'revision', 'existing history', 'request receipts']}
        with self._lock():
            _, current_hash = old._load_state()
            if current_hash != old_hash:
                raise VaultError('CONFLICT', '预检后 v1 状态发生变化。')
            folder = self._path('history', 'migrations', 'v1-' + old_hash)
            folder.mkdir(parents=True, exist_ok=True)
            manifest_path = self._guard(folder / 'manifest.json')
            if manifest_path.exists():
                manifest = json.loads(self._raw(manifest_path))
                if manifest.get('state_sha256') != old_hash or manifest.get('vault_id') != state['vault_id']:
                    raise VaultError('MIGRATION_CONFLICT', '已有迁移备份不匹配。')
            else:
                files = []
                for name in ('_state/vault.json', *VIEWS):
                    source = self._path(*name.split('/'))
                    if not source.exists():
                        continue
                    raw = self._raw(source)
                    backup = self._guard(folder / name.replace('/', '__'))
                    if backup.exists():
                        if self._raw(backup) != raw:
                            raise VaultError('MIGRATION_CONFLICT', '迁移备份文件冲突。')
                    else:
                        self._atomic(backup, raw, new=True)
                    files.append({'path': name, 'backup': str(backup.relative_to(self.root)), 'sha256': digest(raw)})
                manifest = {'version': 1, 'vault_id': state['vault_id'], 'state_sha256': old_hash,
                            'created_at': now(), 'request': request, 'files': files,
                            'entries': {e['id']: digest(raw) for e, raw, _ in records},
                            'receipt_count': len(state['requests'])}
                self._atomic(manifest_path, json_bytes(manifest), new=True)
            for item in manifest['files']:
                if digest(self._raw(self._path(*Path(item['backup']).parts))) != item['sha256']:
                    raise VaultError('MIGRATION_CONFLICT', '迁移备份校验失败。')
            actual_entries = {e['id']: digest(raw) for e, raw, _ in records}
            if actual_entries != manifest['entries']:
                raise VaultError('MIGRATION_CONFLICT', '迁移开始后原文有变化；保留原文，停止自动迁移。')
            new_header = {'app': 'visible-memory', 'format_version': 2, 'operations_schema': operations.SCHEMA,
                          'vault_id': state['vault_id'], 'created_at': state['created_at'], 'initializing': False}
            self._path('_index').mkdir(exist_ok=True)
            self._path('catalog').mkdir(exist_ok=True)
            ops_exists = self._path('_state', 'operations.sqlite').exists()
            relative_manifest = str(manifest_path.relative_to(self.root))
            with contextlib.closing(self._ops(new_header, write=True, create=not ops_exists, recover=True,
                                   initial_meta={'migration_manifest': relative_manifest,
                                                 'migration_state_sha256': old_hash})) as conn:
                existing_manifest = operations.get_meta(conn, 'migration_manifest')
                if ops_exists and existing_manifest != relative_manifest:
                    raise VaultError('MIGRATION_CONFLICT', '已有持久库不属于本次迁移。')
                operations.set_meta(conn, 'migration_manifest', relative_manifest)
                operations.set_meta(conn, 'migration_state_sha256', old_hash)
                views.init_schema(conn)
                views.adopt_legacy(conn, state['views'])
                with conn:
                    for request_id, receipt in state['requests'].items():
                        operations.save_receipt(conn, {'request_id': request_id, **receipt})
                migrated_receipts = {r['request_id']: {k: r[k] for k in ('id', 'sha256', 'signature')}
                                     for r in conn.execute('SELECT * FROM receipts')}
                if migrated_receipts != state['requests']:
                    raise VaultError('MIGRATION_CONFLICT', '迁移后的请求凭据不完全一致。')
                warnings = []
                self._build_index(conn, records, warnings)
                views.rebuild(self, conn, records, warnings)
                rechecked = self._scan_complete()
                if {e['id']: digest(raw) for e, raw, _ in rechecked} != actual_entries:
                    raise VaultError('MIGRATION_CONFLICT', '生成期间原文有变化，格式标记尚未发布。')
                event_id = str(uuid.uuid5(uuid.UUID(state['vault_id']), 'migration-' + old_hash))
                self._event('migrate', manifest.get('request', request), event_id=event_id, timestamp=manifest['created_at'])
                self._atomic(self._path('_state', 'vault.json'), json_bytes(new_header), expected=old_hash)
                return {'applied': True, 'migrated': True, 'format_version': 2,
                        'entry_count': len(records), 'receipt_count': len(migrated_receipts),
                        'backup_manifest': str(manifest_path), 'warnings': warnings}

    def _mutate(self, command, header, request, apply, payload, entry_id, expected_sha256):
        request_id = payload.get('request_id')
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise VaultError('INVALID_INPUT', 'request_id 必须是非空字符串。')
        signature = digest(json_bytes({'command': command, 'id': entry_id,
                                      'payload': {k: v for k, v in payload.items() if k != 'request_id'}}))
        with self._lock() if apply else contextlib.nullcontext():
            with contextlib.closing(self._ops(header, write=apply)) as conn:
                if operations.pending(conn):
                    raise VaultError('RECOVERY_REQUIRED', '上次原文或历史操作未收尾；请明确执行 rebuild 恢复。')
                receipt = operations.receipt(conn, request_id)
                if receipt:
                    if receipt['signature'] != signature:
                        raise VaultError('REQUEST_ID_REUSED', '同一 request_id 不能用于不同操作。')
                    current = self._get(receipt['id'])
                    if digest(current[1]) != receipt['sha256']:
                        raise VaultError('REQUEST_REPLAY_CHANGED', '原请求已处理，但条目随后有变化；未重复保存。')
                    return {'applied': False, 'replayed': True, **self._result_entry(*current)}
                entry, old = self._prepare(command, payload, entry_id, expected_sha256)
                if old and all(entry.get(k) == old[0].get(k) for k in FIELDS | {'status'}):
                    if apply and request_id:
                        with conn:
                            operations.save_receipt(conn, {'request_id': request_id, 'signature': signature,
                                                          'id': old[0]['id'], 'sha256': digest(old[1])})
                    return {'applied': False, 'unchanged': True, **self._result_entry(*old)}
                duplicate = index.duplicates(self, entry) if command == 'add' else None
                if duplicate:
                    if apply and request_id:
                        with conn:
                            operations.save_receipt(conn, {'request_id': request_id, 'signature': signature,
                                                          'id': duplicate[0]['id'], 'sha256': digest(duplicate[1])})
                    return {'applied': False, 'duplicate': True, **self._result_entry(*duplicate)}
                if not apply:
                    return {'applied': False, 'preview': True, 'entry': entry,
                            'path': str(self._path('entries', entry['id'] + '.md')),
                            'previous_sha256': digest(old[1]) if old else None}
                if old:
                    self._snapshot(old[0], old[1])
                raw = render_entry(entry)
                path = self._path('entries', entry['id'] + '.md')
                op = {'operation_id': str(uuid.uuid4()), 'id': entry['id'], 'command': command,
                      'request': request, 'request_id': request_id, 'signature': signature,
                      'previous_sha256': digest(old[1]) if old else None, 'sha256': digest(raw), 'event_at': now()}
                operations.prepare(conn, op)
                self._content_target, self._content_replaced = path, False
                try:
                    self._atomic(path, raw, expected=op['previous_sha256'], new=old is None)
                except (OSError, VaultError) as exc:
                    committed = self._content_replaced or getattr(exc, 'code', None) == 'COMMITTED_UNVERIFIED'
                    try:
                        committed = committed or (path.exists() and digest(self._raw(path)) == op['sha256'])
                    except (OSError, VaultError):
                        pass
                    if not committed:
                        operations.mark(conn, op['operation_id'], 'aborted', str(exc))
                        operations.clean(conn, op['operation_id'], index=True, views=True)
                    return {'ok': False, 'applied': bool(committed), 'verified': False,
                            'recovery_required': bool(committed), 'path': str(path), 'id': entry['id'],
                            'operation_id': op['operation_id'],
                            'error': {'code': 'COMMITTED_UNVERIFIED' if committed else getattr(exc, 'code', 'COMMIT_FAILED'), 'message': str(exc)}}
                result = {'applied': True, 'verified': True, 'operation_id': op['operation_id'],
                          **self._result_entry(entry, raw, path), 'warnings': []}
                try:
                    result['history_path'] = self._snapshot(entry, raw)
                    self._event(command, request, entry, op['operation_id'], op['event_at'])
                    with conn:
                        operations.save_receipt(conn, op)
                        conn.execute("UPDATE operations SET stage='complete' WHERE operation_id=?", (op['operation_id'],))
                except (OSError, VaultError, sqlite3.Error) as exc:
                    result.update(ok=False, recovery_required=True, history_incomplete=True, index_stale=True, views_stale=True)
                    result['warnings'].append({'code': 'HISTORY_INCOMPLETE', 'message': str(exc)})
                    return result
                try:
                    index.upsert(self, entry, raw, path)
                    operations.clean(conn, op['operation_id'], index=True)
                except (OSError, VaultError, sqlite3.Error) as exc:
                    result['index_stale'] = True
                    result['warnings'].append({'code': 'INDEX_STALE', 'message': str(exc)})
                try:
                    views.update(self, conn, entry, result['warnings'])
                    operations.clean(conn, op['operation_id'], views=True)
                except (OSError, VaultError, sqlite3.Error) as exc:
                    result['views_stale'] = True
                    result['warnings'].append({'code': 'VIEWS_STALE', 'message': str(exc)})
                result.update({k: v for k, v in operations.flags(conn).items() if v})
                return result

    def execute(self, command, request='', apply=False, payload=None, entry_id=None,
                expected_sha256=None, query='', include_archived=False, limit=8, offset=0,
                entry_type=None, tags=None, scope=None):
        if not isinstance(request, str) or not request.strip():
            raise VaultError('EXPLICIT_REQUEST_REQUIRED', '必须提供本次明确请求；未读取记忆库。')
        if command not in READS | WRITES:
            raise VaultError('INVALID_COMMAND', '未知命令。')
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise VaultError('INVALID_PAGINATION', 'limit 必须为 1～100，offset 必须为非负整数。')
        if entry_type is not None and entry_type not in TYPES:
            raise VaultError('INVALID_FILTER', '未知类型。')
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(t, str) or not t for t in tags)):
            raise VaultError('INVALID_FILTER', '标签必须是非空字符串列表。')
        if scope is not None and (not isinstance(scope, str) or not scope):
            raise VaultError('INVALID_FILTER', '范围必须是非空字符串。')
        if command == 'search' and (not isinstance(query, str) or not query.strip()):
            raise VaultError('INVALID_QUERY', 'search 需要非空 query。')
        if command in READS and apply:
            raise VaultError('INVALID_INPUT', '只读命令不接受 apply。')
        base = {'ok': True, 'operation': command, 'applied': False}
        if command == 'init':
            return {**base, **self._initialize(request, apply)}
        if command == 'migrate':
            return {**base, **self._migrate(request, apply)}
        header, _ = self._header()
        if header['format_version'] == 1:
            if command in READS and not any((entry_type, tags, scope)):
                result = legacy.Vault(self.root).execute(command, request=request, entry_id=entry_id, query=query,
                                                        include_archived=include_archived, limit=limit, offset=offset)
                return {**result, 'format_version': 1, 'migration_available': True}
            raise VaultError('MIGRATION_REQUIRED', 'v1 格式只提供兼容读取；请明确运行 migrate 升级。')
        if header.get('initializing'):
            raise VaultError('INITIALIZATION_INCOMPLETE', '初始化尚未完成；请明确重试 init。')
        if command in READS:
            return {**base, **self._read_v2(command, header, entry_id, query, include_archived, limit, offset,
                                          entry_type, tags, scope)}
        if command == 'rebuild':
            return {**base, **self._rebuild_v2(header, request, apply)}
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise VaultError('INVALID_INPUT', '输入必须是 JSON 对象。')
        allowed = FIELDS | {'request_id'} if command in ('add', 'update') else {'request_id'}
        if set(payload) - allowed:
            raise VaultError('INVALID_INPUT', '输入包含未知字段。', sorted(set(payload) - allowed))
        if command == 'update' and not (set(payload) & FIELDS):
            raise VaultError('EMPTY_UPDATE', 'update 至少需要一个内容字段。')
        return {**base, **self._mutate(command, header, request, apply, payload, entry_id, expected_sha256)}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument('--root')
    location.add_argument('--config')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in sorted(READS | WRITES):
        item = sub.add_parser(command)
        item.add_argument('--request', required=True)
        if command in WRITES:
            item.add_argument('--apply', action='store_true')
        if command in ('add', 'update'):
            item.add_argument('--input', required=True)
        if command in ('show', 'history', 'update', 'archive', 'restore'):
            item.add_argument('--id', dest='entry_id', required=True)
        if command in ('update', 'archive', 'restore'):
            item.add_argument('--expected-sha256', required=True)
        if command in ('list', 'search'):
            item.add_argument('--include-archived', action='store_true')
            item.add_argument('--limit', type=int, default=8)
            item.add_argument('--offset', type=int, default=0)
            item.add_argument('--type', dest='entry_type', choices=TYPES)
            item.add_argument('--tag', dest='tags', action='append')
            item.add_argument('--scope')
        if command == 'search':
            item.add_argument('--query', required=True)
    args = vars(parser.parse_args(argv))
    root, config, input_path = args.pop('root'), args.pop('config'), args.pop('input', None)
    root_info = {}
    try:
        import vm_config
        vm_config.require_request(args['request'])
        root, expected_id, root_info = vm_config.resolve_root(root, config)
        root_info['vault_root'] = str(root)
        if input_path:
            raw = sys.stdin.buffer.read() if input_path == '-' else Path(input_path).read_bytes()
            args['payload'] = json.loads(raw.decode('utf-8-sig'))
        result = {**Vault(root, expected_vault_id=expected_id).execute(**args), **root_info,
                  'program_version': vm_config.VERSION}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok') else 1
    except (VaultError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, **root_info, 'error': {'code': getattr(exc, 'code', 'IO_OR_INPUT_ERROR'),
                                               'message': str(exc), 'details': getattr(exc, 'details', None)}}, ensure_ascii=False, indent=2))
        return 1

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
