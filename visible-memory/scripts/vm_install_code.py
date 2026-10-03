"""Code-only install/upgrade. Never resolves or reads a memory vault."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import uuid

import install as legacy_install
import vm_config as config
from memory_v1 import VaultError, digest, identifier, json_bytes, now

ROOT_FILES = {'SKILL.md', 'README.md', 'release.json'}
FOLDERS = {'scripts', 'references', 'agents', 'tests'}


def files(root):
    root = config.guard(root)
    result = {}
    if not root.exists():
        return result
    if not root.is_dir():
        raise VaultError('INVALID_TARGET', 'Skill 路径必须是普通目录。')

    def visit(folder):
        for path in sorted(folder.iterdir()):
            config.guard(path)
            if path.name in {'__pycache__', '.pytest_cache'} or path.suffix in {'.pyc', '.pyo'}:
                continue
            rel = path.relative_to(root).as_posix()
            if path.is_dir():
                if Path(rel).parts[0] not in FOLDERS:
                    raise VaultError('UNOWNED_TARGET', f'程序目录有未知文件夹，未读取其内容：{rel}')
                visit(path)
            else:
                if not whitelist(rel):
                    raise VaultError('UNOWNED_TARGET', f'程序目录有未知文件，未读取或覆盖：{rel}')
                result[rel] = config.raw_file(path)
    visit(root)
    return result


def whitelist(rel):
    parts = Path(rel).parts
    if rel in ROOT_FILES:
        return True
    if len(parts) < 2:
        return False
    if parts[0] in {'scripts', 'tests'}:
        return Path(rel).suffix == '.py'
    if parts[0] == 'references':
        return Path(rel).suffix == '.md' or Path(rel).name in {'v1-release.json', 'v2-release.json'}
    return rel == 'agents/openai.yaml'


def hashes(values):
    return {rel: digest(raw) for rel, raw in sorted(values.items())}


def release(values):
    try:
        value = json.loads(values['release.json'])
        if value['app'] != 'visible-memory' or value['version'] != config.VERSION or value['vault_format'] != 2 or value['config_format'] != 1:
            raise ValueError('发布标记不匹配')
        expected = value['files']
        if hashes({p: b for p, b in values.items() if p != 'release.json'}) != expected:
            raise ValueError('文件与发布校验清单不一致')
        if any(not whitelist(rel) for rel in values):
            raise ValueError('发布包含非白名单文件')
    except (KeyError, ValueError, TypeError) as exc:
        raise VaultError('INVALID_RELEASE', f'程序发布清单无效：{exc}') from None
    return value


def routing(original, target):
    # Reuse marker validation and byte-preserving prefix/suffix handling.
    generated = legacy_install.routing(original, target, '由用户配置确定')
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    lines = [legacy_install.START.decode(), '## 可见记忆管理（仅显式请求）',
             '- 当前记忆目录由用户配置确定；默认配置为 `%LOCALAPPDATA%\\VisibleMemory\\config.json`。',
             f'- 仅用户本次明确要求记忆检索、查看、检查、保存、更新、归档、恢复、导入、刷新或目录设置时，读取 `{target / "SKILL.md"}` 并按其流程执行。',
             '- 普通任务不读取用户配置或记忆库，包括原文、历史、索引、导航和文件清单；不得因启动会话、任务相关性、上下文压缩或此前授权而读取。',
             '- 一次明确检索仅授权该次指定范围；管理操作只读取完成该操作所需的条目、版本及校验信息，不召回无关记忆。',
             '- 目录设置窗口仅按明确请求打开；目录切换只修改用户配置，不扩大正文读取授权。',
             '- 对象或范围不清时询问缺失信息；明确无歧义的管理要求可直接完成。',
             '- 记忆正文不嵌入全局指令；Codex 原生记忆不自动导入、同步或修改。', legacy_install.END.decode()]
    start, end = generated.index(legacy_install.START), generated.index(legacy_install.END) + len(legacy_install.END)
    return generated[:start] + newline.join(line.encode('utf-8') for line in lines) + generated[end:]


def _recognize(current, source):
    if not current:
        return 'empty'
    actual = hashes(current)
    if 'release.json' in current:
        try:
            marker = json.loads(current['release.json'])
            expected = marker['files']
            if marker['app'] == 'visible-memory' and hashes({p: b for p, b in current.items() if p != 'release.json'}) == expected:
                return marker['version']
        except (ValueError, KeyError, TypeError):
            pass
    for name in ('references/v2-release.json', 'references/v1-release.json'):
        if name not in source:
            continue
        try:
            manifest = json.loads(source[name])
            if manifest['app'] == 'visible-memory' and actual == manifest['files']:
                return manifest.get('version', 'known-legacy')
        except (ValueError, KeyError, TypeError):
            continue
    # Report paths and hashes, not memory data or file bodies.
    candidates = []
    for name in ('references/v2-release.json', 'references/v1-release.json'):
        try:
            candidates.append(json.loads(source[name])['files'])
        except (KeyError, ValueError, TypeError):
            pass
    expected = max(candidates, key=lambda m: sum(actual.get(k) == v for k, v in m.items()), default={})
    differences = [{'path': p, 'actual_sha256': actual.get(p), 'expected_sha256': expected.get(p)}
                   for p in sorted(set(expected) | set(actual)) if expected.get(p) != actual.get(p)]
    raise VaultError('UNRECOGNIZED_INSTALLATION', '目标不是完整的已知发布版本，或文件已被用户修改；未覆盖。', differences)


def _journal_path(backup_dir, operation_id):
    return config.guard(backup_dir / ('code-' + identifier(operation_id)) / 'operation.json')


def _save(path, record):
    before = config.raw_file(path)
    config.atomic(path, json_bytes(record), digest(before) if before is not None else None)


def _restore(record, path):
    target, agents = Path(record['target']), Path(record['agents_file'])
    if not config.overlap(target, Path(record['source'])) and agents.name == 'AGENTS.md':
        pass
    else:
        raise VaultError('INVALID_OPERATION_RECORD', '代码恢复路径不合法。')
    backup = path.parent
    conflicts, restored = [], []
    for rel, written_sha in reversed(list(record.get('written', {}).items())):
        candidate = Path(rel)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise VaultError('INVALID_OPERATION_RECORD', '备份清单含越界路径。')
        destination = config.guard(target / candidate)
        actual = config.raw_file(destination)
        previous_sha = record['before_files'].get(rel)
        if (digest(actual) if actual is not None else None) == previous_sha:
            continue
        if actual is None or digest(actual) != written_sha:
            conflicts.append(str(destination))
            continue
        if previous_sha is None:
            destination.unlink()  # Only the exact new file written by this operation.
        else:
            previous = config.raw_file(backup / 'skill' / rel)
            if previous is None or digest(previous) != previous_sha:
                conflicts.append(str(destination))
                continue
            config.atomic(destination, previous, written_sha)
        restored.append(str(destination))
    if record.get('agents_written'):
        current = config.raw_file(agents)
        actual_sha = digest(current) if current is not None else None
        if actual_sha != record['before_agents']:
            if actual_sha != record['after_agents']:
                conflicts.append(str(agents))
            elif record['before_agents'] is None:
                config.guard(agents).unlink()
                restored.append(str(agents))
            else:
                original = config.raw_file(backup / 'AGENTS.md')
                if original is None or digest(original) != record['before_agents']:
                    conflicts.append(str(agents))
                else:
                    config.atomic(agents, original, actual_sha)
                    restored.append(str(agents))
    record['phase'] = 'rollback_conflict' if conflicts else 'rolled_back'
    record['rollback_conflicts'] = conflicts
    record['restored_files'] = restored
    _save(path, record)
    return {'rolled_back': not conflicts, 'rollback_conflicts': conflicts, 'restored_files': restored}


def defaults(source=None, target=None, agents_file=None, backup_dir=None):
    codex_dir = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    return tuple(config.guard(p) for p in (source or Path(__file__).parent.parent,
        target or Path.home() / '.agents' / 'skills' / 'visible-memory', agents_file or codex_dir / 'AGENTS.md',
        backup_dir or config.default_config().parent / 'backups'))


def install_code(source=None, target=None, agents_file=None, backup_dir=None, *, request, apply=False,
                 upgrade_code=False, preview_token=None, fault=None):
    config.require_request(request)
    result = {'ok': False, 'applied': False, 'vault_inspected': False, 'config_changed': False, 'program_version': config.VERSION}
    record, path = None, None
    fault = fault or (lambda stage, record: None)
    try:
        source, target, agents, backups = defaults(source, target, agents_file, backup_dir)
        for a, b in ((source, target), (source, backups), (target, backups)):
            if config.overlap(a, b):
                raise VaultError('OVERLAPPING_PATHS', '源码、安装位置和备份位置必须彼此独立。')
        if agents.name != 'AGENTS.md' or any(config.overlap(agents, folder) for folder in (source, target, backups)):
            raise VaultError('INVALID_AGENTS_PATH', '全局引导必须是独立的 AGENTS.md。')
        if config.raw_file(agents.with_name('AGENTS.override.md')):
            raise VaultError('AGENTS_SHADOWED', '非空 AGENTS.override.md 会遮蔽全局引导，未安装。')
        published, current = files(source), files(target)
        version = release(published)
        old_version = _recognize(current, published)
        if current and current != published and not upgrade_code:
            raise VaultError('UPGRADE_CODE_REQUIRED', '已有不同版本，请明确使用 --upgrade-code；不会迁移记忆库。')
        before = config.raw_file(agents)
        after = routing(before or b'', target)
        changed = [p for p, raw in published.items() if current.get(p) != raw]
        obsolete = sorted(set(current) - set(published))
        if obsolete:
            raise VaultError('OBSOLETE_MANAGED_FILES', '升级涉及移除旧代码文件，当前版本未声明此变更，未覆盖。', obsolete)
        state = {'source_files': hashes(published), 'target_files': hashes(current),
                 'source': str(source), 'target': str(target), 'agents_file': str(agents),
                 'agents_sha256': digest(before) if before is not None else None,
                 'agents_after_sha256': digest(after), 'backup_dir': str(backups)}
        token = digest(json_bytes(state))
        result.update(ok=True, preview=not apply, preview_token=token, source=str(source), target=str(target),
                      previous_version=old_version, changed_files=changed, agents_changed=before != after,
                      unchanged=not changed and before == after, agents_file=str(agents), backups_directory=str(backups))
        if not apply or result['unchanged']:
            return result
        if preview_token is not None and preview_token != token:
            raise VaultError('STALE_PREVIEW', '代码升级预览已变化，请重新预览。')
        operation_id = str(uuid.uuid4())
        path = _journal_path(backups, operation_id)
        result.update(operation_id=operation_id, backup_path=str(path.parent))
        lock_path = target.parent / ('.' + target.name + '.install.lock')
        with config.lock(lock_path, operation_id=operation_id) as owner:
            if files(source) != published or files(target) != current or config.raw_file(agents) != before:
                raise VaultError('CONFLICT', '锁内预检发现程序或全局引导变化，未覆盖。')
            if config.raw_file(agents.with_name('AGENTS.override.md')):
                raise VaultError('AGENTS_SHADOWED', '全局覆盖文件在预检后变化。')
            record = {'app': 'visible-memory-code-upgrade', 'schema_version': 1, 'operation_id': operation_id,
                      **state, 'request': request, 'at': now(), 'phase': 'backing_up',
                      'before_files': hashes(current), 'before_agents': state['agents_sha256'],
                      'after_agents': digest(after), 'written': {}, 'agents_written': False,
                      'lock': {'path': str(lock_path), 'owner': owner}}
            _save(path, record)
            for rel, raw in current.items():
                config.atomic(path.parent / 'skill' / rel, raw)
            if before is not None:
                config.atomic(path.parent / 'AGENTS.md', before)
            record['phase'] = 'installing'
            _save(path, record)
            fault('backed_up', record)
            # Scripts and references first, then the discovery entry and guide.
            ordered = sorted(changed, key=lambda p: (p in {'SKILL.md', 'release.json'}, p))
            for rel in ordered:
                raw = published[rel]
                # Persist intended after-hash before replace for crash recovery.
                record['written'][rel] = digest(raw)
                _save(path, record)
                config.atomic(target / rel, raw, digest(current[rel]) if rel in current else None)
                result['applied'] = True
                fault('file_written', record)
            if before != after:
                record['agents_written'] = True
                _save(path, record)
                config.atomic(agents, after, state['agents_sha256'])
                result['applied'] = True
                fault('agents_written', record)
            if files(target) != published or config.raw_file(agents) != after:
                raise VaultError('VERIFY_FAILED', '代码或全局引导读回不匹配。')
            record['phase'] = 'complete'
            _save(path, record)
            result.update(verified=True, preview=False)
            return result
    except (VaultError, legacy_install.InstallError, OSError, ValueError) as exc:
        result.update(ok=False, error={'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc), 'details': getattr(exc, 'details', None)})
        if record is not None and path is not None:
            try:
                result.update(_restore(record, path))
                result['applied'] = not result['rolled_back']
            except (VaultError, OSError, ValueError) as rollback_exc:
                result.update(rolled_back=False, pending_recovery=True, rollback_error=str(rollback_exc))
        return result


def recover_code(backup_dir, operation_id, *, request, apply=False):
    config.require_request(request)
    path = _journal_path(config.guard(backup_dir), operation_id)
    raw = config.raw_file(path)
    if raw is None:
        raise VaultError('OPERATION_NOT_FOUND', '未找到指定代码备份。')
    try:
        record = json.loads(raw)
        if (record['app'], record['schema_version'], record['operation_id'], record['backup_dir']) != ('visible-memory-code-upgrade', 1, operation_id, str(config.guard(backup_dir))):
            raise ValueError('备份标记不匹配')
    except (ValueError, KeyError, TypeError) as exc:
        raise VaultError('INVALID_OPERATION_RECORD', f'代码备份记录损坏：{exc}') from None
    result = {'ok': True, 'applied': False, 'preview': not apply, 'phase': record['phase'],
              'operation_id': operation_id, 'vault_inspected': False, 'config_changed': False}
    if record['phase'] in {'rolled_back', 'complete'}:
        return {**result, 'message': '操作已收尾。已完成发布的版本回退需重新进行代码升级；不恢复记忆库快照。'}
    if not apply:
        return {**result, 'planned_action': '校验后恢复本次旧代码与引导；不更改配置或数据。'}
    lock_path = Path(record['target']).parent / ('.' + Path(record['target']).name + '.install.lock')
    saved_lock = record.get('lock')
    if saved_lock is None or saved_lock['path'] != str(lock_path) or saved_lock['owner'].get('operation_id') != operation_id:
        raise VaultError('INVALID_OPERATION_RECORD', '代码操作锁记录不匹配。')
    if config.verify_recoverable_lock(lock_path, saved_lock['owner']):
        config.guard(lock_path).unlink()
    with config.lock(lock_path, operation_id=operation_id):
        if config.raw_file(path) != raw:
            raise VaultError('CONFLICT', '代码恢复记录已变化。')
        result.update(_restore(record, path))
    result['ok'] = result['rolled_back']
    result['applied'] = bool(result.get('restored_files'))
    return result
