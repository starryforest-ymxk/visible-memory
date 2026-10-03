"""Lazy per-user configuration and guarded local filesystem primitives. Stdlib only."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from memory_v1 import VaultError, digest, identifier, json_bytes, now

VERSION = '2.1.0'
CONFIG_SCHEMA = 1
CONFIG_FIELDS = {'schema_version', 'revision', 'vault_root', 'vault_id'}


def default_config():
    # Merely computing the path does not read or create anything.
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'VisibleMemory' / 'config.json' if base else Path.home() / 'AppData' / 'Local' / 'VisibleMemory' / 'config.json'


def guard(path):
    raw = Path(path)
    if '..' in raw.parts:
        raise VaultError('UNSAFE_PATH', f'拒绝父目录跳转：{raw}')
    path = Path(os.path.abspath(raw))
    if str(path).startswith(('\\\\', '//')) or path == Path(path.anchor):
        raise VaultError('UNSAFE_PATH', '拒绝网络路径或磁盘根目录。')
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        if kernel.GetDriveTypeW(path.anchor) == 4:
            raise VaultError('UNSAFE_PATH', '拒绝映射到网络的盘符。')
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        for part in path.parts[1:]:
            if part.endswith((' ', '.')) or ':' in part or part.split('.')[0].upper() in reserved:
                raise VaultError('UNSAFE_PATH', f'拒绝 Windows 路径别名：{part}')
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise VaultError('UNSAFE_PATH', f'拒绝符号链接或重解析点：{item}')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise VaultError('UNSAFE_PATH', f'拒绝硬链接：{item}')
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise VaultError('UNSAFE_PATH', f'拒绝特殊文件：{item}')
    return path


def directory(path):
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise VaultError('INVALID_PATH', '记忆目录必须是绝对路径。')
    value = guard(raw)
    if value.exists() and not value.is_dir():
        raise VaultError('NOT_A_DIRECTORY', f'需要普通目录：{value}')
    return value


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def raw_file(path):
    path = guard(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise VaultError('NOT_A_FILE', f'需要普通文件：{path}')
    return path.read_bytes()


def require_request(request):
    if not isinstance(request, str) or not request.strip():
        raise VaultError('EXPLICIT_REQUEST_REQUIRED', '必须提供本次明确请求；未读取配置、输入文件或记忆库。')


def read_config(path=None, *, missing_ok=False):
    path = guard(path or default_config())
    raw = raw_file(path)
    if raw is None:
        if missing_ok:
            return None, None
        raise VaultError('NOT_CONFIGURED', '尚未配置记忆目录。请明确打开目录设置，或使用 settings.py；不会自动创建库或弹出窗口。', {'config_path': str(path)})
    try:
        if len(raw) > 16384:
            raise ValueError('配置过大')
        data = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data, dict) or set(data) != CONFIG_FIELDS:
            raise ValueError('配置必须且只能包含四个规定字段')
        if type(data['schema_version']) is not int or data['schema_version'] != CONFIG_SCHEMA:
            raise ValueError('未知配置格式版本')
        if type(data['revision']) is not int or data['revision'] < 1:
            raise ValueError('revision 必须是正整数')
        identifier(data['vault_id'])
        if not isinstance(data['vault_root'], str):
            raise ValueError('vault_root 必须是绝对路径字符串')
        # Syntax validation only: get/gui must not stat the configured vault.
        value = Path(data['vault_root'])
        if not value.is_absolute() or '..' in value.parts or str(value).startswith(('\\\\', '//')) or value == Path(value.anchor):
            raise ValueError('vault_root 不是安全的绝对目录')
        if str(Path(os.path.abspath(value))) != data['vault_root']:
            raise ValueError('vault_root 未规范化')
    except (ValueError, TypeError, UnicodeError, KeyError, VaultError) as exc:
        raise VaultError('INVALID_CONFIG', f'配置损坏或格式未知，未回退到其他目录：{exc}', {'config_path': str(path)}) from None
    return data, digest(raw)


def vault_header(root, *, expected_id=None):
    # Only the small header is opened; no entries, index, or operation ledger.
    from memory import Vault
    root = directory(root)
    if not root.is_dir():
        raise VaultError('VAULT_UNAVAILABLE', f'已配置的目录不可用：{root}')
    header, sha = Vault(root)._header()
    if header['format_version'] != 2:
        raise VaultError('MIGRATION_REQUIRED', '请选择已初始化的 v2 库；v1 需明确运行 migrate，不会因选目录自动升级。')
    if header.get('initializing'):
        raise VaultError('INITIALIZATION_INCOMPLETE', '库尚未完成初始化；请明确恢复初始化。')
    if expected_id is not None and header['vault_id'] != expected_id:
        raise VaultError('VAULT_ID_MISMATCH', '目录中的记忆库 UUID 与配置不匹配，未使用另一套库。')
    return header, sha


def resolve_root(root=None, config=None):
    if root is not None and config is not None:
        raise VaultError('INVALID_INPUT', '--root 与 --config 不能同时使用。')
    if root is not None:
        return Path(os.path.abspath(root)), None, {'root_source': 'explicit_root'}
    path = guard(config or default_config())
    value, _ = read_config(path)
    header, _ = vault_header(value['vault_root'], expected_id=value['vault_id'])
    return Path(value['vault_root']), header['vault_id'], {'root_source': 'explicit_config' if config else 'user_config', 'config_path': str(path), 'config_revision': value['revision']}


def atomic(path, data, expected=None):
    path = guard(path)
    guard(path.parent).mkdir(parents=True, exist_ok=True)
    temp = guard(path.parent / ('.vm-config-' + str(uuid.uuid4()) + '.tmp'))
    replaced = False
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        current = raw_file(path)
        if (digest(current) if current is not None else None) != expected:
            raise VaultError('CONFLICT', f'文件在预检后变化，未覆盖：{path}')
        guard(path)
        os.replace(temp, path)
        replaced = True
        if raw_file(path) != data:
            raise VaultError('COMMITTED_UNVERIFIED', f'已替换但读回不匹配：{path}', {'path': str(path), 'committed': True})
    except OSError as exc:
        if replaced:
            raise VaultError('COMMITTED_UNVERIFIED', f'已替换但读回失败：{path}', {'path': str(path), 'committed': True}) from exc
        raise
    finally:
        if temp.exists():
            guard(temp).unlink()


def process_identity(pid):
    """Return (alive/dead/unknown, creation identity); refuse uncertain recovery."""
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return 'alive', None
        except ProcessLookupError:
            return 'dead', None
        except (PermissionError, OSError):
            return 'unknown', None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ('dead', None) if ctypes.get_last_error() == 87 else ('unknown', None)
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return 'unknown', None
        if code.value != 259:
            return 'dead', None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
            return 'unknown', None
        return 'alive', str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def lock(path, *, operation_id, on_acquired=None):
    path = guard(path)
    guard(path.parent).mkdir(parents=True, exist_ok=True)
    _, creation = process_identity(os.getpid())
    owner = {'app': 'visible-memory-settings', 'operation_id': operation_id, 'token': str(uuid.uuid4()),
             'pid': os.getpid(), 'process_creation': creation, 'created_at': now()}
    data = json_bytes(owner)
    try:
        with path.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise VaultError('LOCKED', f'已有写入锁，未移除：{path}') from None
    try:
        if on_acquired:
            on_acquired(path, owner)
        yield owner
    finally:
        # Never unlink a replaced or foreign lock, even after an error.
        if raw_file(path) == data:
            guard(path).unlink()


def verify_recoverable_lock(path, recorded):
    data = raw_file(path)
    if data is None:
        return False
    try:
        owner = json.loads(data)
    except (ValueError, UnicodeError):
        raise VaultError('LOCKED', f'未知锁，保持原样：{path}') from None
    if owner != recorded or owner.get('app') != 'visible-memory-settings':
        raise VaultError('LOCKED', f'锁与指定恢复记录不匹配：{path}')
    alive, creation = process_identity(owner['pid'])
    same_process = creation is None or owner.get('process_creation') is None or creation == owner['process_creation']
    if alive == 'unknown' or (alive == 'alive' and same_process):
        raise VaultError('LOCKED', f'原进程仍在运行或无法确认已退出：{path}')
    return True


def write_config(path, before_sha, before, root, vault_id):
    path = guard(path)
    value = {'schema_version': CONFIG_SCHEMA, 'revision': (before['revision'] if before else 0) + 1,
             'vault_root': str(directory(root)), 'vault_id': identifier(vault_id)}
    if before is not None:
        old = raw_file(path)
        if old is None or digest(old) != before_sha:
            raise VaultError('CONFLICT', '配置在预览后变化，未提交。')
        backup = guard(path.parent / 'backups' / ('config-r' + str(before['revision']) + '-' + before_sha + '.json'))
        saved = raw_file(backup)
        if saved is None:
            atomic(backup, old)
        elif saved != old:
            raise VaultError('BACKUP_CONFLICT', '配置备份发生冲突，未提交。')
    atomic(path, json_bytes(value), expected=before_sha)
    actual, checksum = read_config(path)
    if actual != value:
        raise VaultError('COMMITTED_UNVERIFIED', '配置已替换但读回验证失败。', {'committed': True})
    return value, checksum
