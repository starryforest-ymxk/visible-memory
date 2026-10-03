"""Durable operation receipts. This database is not a disposable search cache."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from memory_v1 import VaultError, now

SCHEMA = 1


def connect(vault, header, *, write=False, create=False, recover=False, initial_meta=None):
    path = vault._path('_state', 'operations.sqlite')
    if create and path.exists():
        raise VaultError('OPERATIONS_UNAVAILABLE', '拒绝将已有持久库当作新库初始化。')
    for suffix in ('-journal', '-wal', '-shm'):
        side = vault._guard(path.with_name(path.name + suffix))
        if side.exists() and not (write and recover and suffix == '-journal'):
            raise VaultError('OPERATIONS_UNAVAILABLE', '持久操作库存在待恢复日志；只读命令不会修复它。')
    if not path.exists() and not create:
        raise VaultError('OPERATIONS_UNAVAILABLE', '持久操作库缺失，不能作为缓存重新初始化。')
    # SQLite may create WAL sidecars even for mode=ro. Inspect its header before
    # opening, so a read cannot trigger shared-memory creation or log recovery.
    if path.exists():
        with path.open('rb') as stream:
            prefix = stream.read(100)
        if len(prefix) < 100 or prefix[:16] != b'SQLite format 3\x00' or prefix[18:20] != b'\x01\x01':
            raise VaultError('OPERATIONS_UNAVAILABLE', '持久库文件头损坏或使用 WAL；不会自动修复或创建侧车。')
    target, temporary = path, None
    if create:
        temporary = vault._path('_state', '.operations-' + str(uuid.uuid4()) + '.sqlite')
        with temporary.open('xb'):
            pass
        path = temporary
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + ('?mode=rwc' if create else '?mode=rw' if write else '?mode=ro'), uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA foreign_keys=ON')
        if create:
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('PRAGMA synchronous=FULL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS operations_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(request_id TEXT PRIMARY KEY,signature TEXT NOT NULL,id TEXT NOT NULL,sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(
                    operation_id TEXT PRIMARY KEY, id TEXT NOT NULL, command TEXT NOT NULL,
                    request TEXT NOT NULL, request_id TEXT, signature TEXT NOT NULL,
                    previous_sha256 TEXT, sha256 TEXT NOT NULL, event_at TEXT NOT NULL,
                    stage TEXT NOT NULL, error TEXT, index_dirty INTEGER NOT NULL DEFAULT 1,
                    views_dirty INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS operations_stage ON operations(stage);
                CREATE INDEX IF NOT EXISTS operations_dirty ON operations(index_dirty,views_dirty);
                CREATE INDEX IF NOT EXISTS operations_unfinished ON operations(stage)
                    WHERE stage NOT IN ('complete','aborted');
                CREATE INDEX IF NOT EXISTS operations_index_dirty ON operations(operation_id)
                    WHERE index_dirty=1 AND stage!='aborted';
                CREATE INDEX IF NOT EXISTS operations_views_dirty ON operations(operation_id)
                    WHERE views_dirty=1 AND stage!='aborted';
            ''')
            with conn:
                values = {'app': 'visible-memory', 'schema': str(SCHEMA), 'vault_id': header['vault_id']}
                values.update(initial_meta or {})
                for key, value in values.items():
                    conn.execute('INSERT OR IGNORE INTO operations_meta VALUES (?,?)', (key, value))
        elif write:
            if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                raise VaultError('OPERATIONS_UNAVAILABLE', '持久库日志模式异常，需要明确维护。')
            conn.execute('PRAGMA synchronous=FULL')
        else:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
        meta = dict(conn.execute('SELECT key,value FROM operations_meta'))
        if (meta.get('app'), meta.get('schema'), meta.get('vault_id')) != ('visible-memory', str(SCHEMA), header['vault_id']):
            raise VaultError('OPERATIONS_UNAVAILABLE', '持久操作库标记或版本不匹配。')
        # Validate required table shapes without scanning the durable ledger.
        conn.execute('SELECT request_id,signature,id,sha256 FROM receipts LIMIT 0')
        conn.execute('SELECT operation_id,stage,index_dirty,views_dirty FROM operations LIMIT 0')
        if create:
            conn.close()
            conn = None
            with temporary.open('r+b') as stream:
                os.fsync(stream.fileno())
            vault._guard(target)
            if target.exists():
                raise VaultError('CONFLICT', '创建期间出现同名持久库，未覆盖。')
            os.replace(temporary, target)
            return connect(vault, header, write=True)
        return conn
    except (sqlite3.Error, OSError, VaultError) as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, VaultError):
            raise
        raise VaultError('OPERATIONS_UNAVAILABLE', f'持久操作库不可用，未重新初始化：{exc}') from None
    finally:
        if temporary is not None:
            for suffix in ('', '-journal', '-wal', '-shm'):
                owned = vault._guard(temporary.with_name(temporary.name + suffix))
                if owned.exists():
                    owned.unlink()


def pending(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM operations WHERE stage NOT IN ('complete','aborted') ORDER BY event_at,operation_id")]


def flags(conn):
    row = conn.execute("""SELECT EXISTS(SELECT 1 FROM operations WHERE stage NOT IN ('complete','aborted')),
                              EXISTS(SELECT 1 FROM operations WHERE index_dirty=1 AND stage!='aborted'),
                              EXISTS(SELECT 1 FROM operations WHERE views_dirty=1 AND stage!='aborted')""").fetchone()
    return {'pending_recovery': bool(row[0]), 'index_stale': bool(row[1]), 'views_stale': bool(row[2])}


def prepare(conn, operation):
    keys = ('operation_id', 'id', 'command', 'request', 'request_id', 'signature', 'previous_sha256', 'sha256', 'event_at')
    with conn:
        conn.execute('INSERT INTO operations (' + ','.join(keys) + ',stage) VALUES (' + ','.join('?' for _ in keys) + ", 'prepared')", tuple(operation[k] for k in keys))


def receipt(conn, request_id):
    if request_id is None:
        return None
    row = conn.execute('SELECT * FROM receipts WHERE request_id=?', (request_id,)).fetchone()
    return dict(row) if row else None


def save_receipt(conn, operation):
    if operation.get('request_id') is None:
        return
    previous = receipt(conn, operation['request_id'])
    values = {k: operation[k] for k in ('request_id', 'signature', 'id', 'sha256')}
    if previous and previous != values:
        raise VaultError('REQUEST_ID_REUSED', '请求编号已有不同的持久记录。')
    conn.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)', tuple(values.values()))


def mark(conn, operation_id, stage, error=None):
    with conn:
        conn.execute('UPDATE operations SET stage=?,error=? WHERE operation_id=?', (stage, error, operation_id))


def clean(conn, operation_id=None, *, index=False, views=False):
    assignments = []
    if index:
        assignments.append('index_dirty=0')
    if views:
        assignments.append('views_dirty=0')
    if not assignments:
        return
    where = ' WHERE operation_id=?' if operation_id else " WHERE stage IN ('complete','aborted')"
    with conn:
        conn.execute('UPDATE operations SET ' + ','.join(assignments) + where, (operation_id,) if operation_id else ())


def set_meta(conn, key, value):
    with conn:
        conn.execute('INSERT INTO operations_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def get_meta(conn, key):
    row = conn.execute('SELECT value FROM operations_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None
