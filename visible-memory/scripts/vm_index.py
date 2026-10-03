"""Rebuildable SQLite candidate index; Markdown remains authoritative.

Public readers never create/update a database.  Only explicit wrapper operations
call build/upsert.  The size/mtime overlay is process-local and intentionally is
not a guarantee against edits that restore both attributes (check detects those).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from memory_v1 import (VaultError, digest, fingerprint, identifier, normalized,
                       parse_entry, validate_entry, now)

APP_ID = 0x564D4932
VERSION = 2
DB_NAME = "search.sqlite"
REBUILD_MARKER = "rebuild.pending"
SIDECARS = ("-journal", "-wal", "-shm")
RECOVERABLE = {"INDEX_MISSING", "INDEX_CORRUPT", "INDEX_BUSY", "INDEX_SIDECAR"}


def _db_path(vault):
    return vault._path("_index", DB_NAME)


def _vault_id(vault):
    try:
        header = json.loads(vault._raw(vault._path("_state", "vault.json")))
        return identifier(header["vault_id"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VaultError("INDEX_CORRUPT", f"无法验证索引所属记忆库：{exc}") from None


def _sidecars(vault, path):
    for suffix in SIDECARS:
        other = vault._guard(Path(str(path) + suffix))
        if other.exists():
            raise VaultError("INDEX_SIDECAR", "索引存在待处理的 SQLite 侧车；只读查询不会执行恢复。", {"path": str(other)})


def _configure(connection, readonly):
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA foreign_keys=ON")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "delete":
        raise VaultError("INDEX_CORRUPT", "索引必须使用 DELETE 日志模式。")


def connect(vault, readonly=True):
    """Return a connection. Caller must close it; reads use a stable transaction."""
    path = _db_path(vault)
    if vault._path("_index", REBUILD_MARKER).exists():
        raise VaultError("INDEX_BUSY", "索引重建尚未收尾；只读调用不会处理恢复标记。")
    _sidecars(vault, path)
    if not path.is_file():
        raise VaultError("INDEX_MISSING", "检索索引不存在；只读调用不会创建。")
    # A read-only SQLite WAL connection can still create shared-memory sidecars.
    # Reject WAL mode from the file header before SQLite is allowed to open it.
    with path.open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise VaultError("INDEX_CORRUPT", "索引文件头损坏或不是回滚日志格式；未打开 SQLite。")
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                                     uri=True, timeout=1.0, isolation_level=None)
        _configure(connection, readonly)
        if readonly:
            connection.execute("BEGIN")
        if connection.execute("PRAGMA application_id").fetchone()[0] != APP_ID or connection.execute("PRAGMA user_version").fetchone()[0] != VERSION:
            raise VaultError("INDEX_CORRUPT", "索引格式或应用标记不匹配。")
        connection.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()[0]
        if connection.execute("SELECT value FROM meta WHERE key='vault_id'").fetchone()[0] != _vault_id(vault):
            raise VaultError("INDEX_CORRUPT", "索引属于其他记忆库，未使用其缓存内容。")
        connection.execute("SELECT doc_id,id,entry_json,sha256,fingerprint,size,mtime_ns,type,status,scope,updated_at,title_norm,tags_norm,body_norm,fts_unsafe FROM documents LIMIT 0")
        connection.execute("SELECT gram,doc_id FROM short_grams LIMIT 0")
        connection.execute("SELECT tag,doc_id FROM tags LIMIT 0")
        connection.execute("SELECT rowid,title_norm,tags_norm,body_norm FROM terms_fts LIMIT 0")
        return connection
    except (sqlite3.Error, TypeError) as exc:
        if connection is not None:
            connection.close()
        raise VaultError("INDEX_CORRUPT", f"无法读取索引：{exc}") from None
    except BaseException:
        if connection is not None:
            connection.close()
        raise


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE documents(
 doc_id INTEGER PRIMARY KEY,
 id TEXT NOT NULL UNIQUE,
 entry_json TEXT NOT NULL,
 sha256 TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 size INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL,
 type TEXT NOT NULL,
 status TEXT NOT NULL,
 scope TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 title_norm TEXT NOT NULL,
 tags_norm TEXT NOT NULL,
 body_norm TEXT NOT NULL,
 fts_unsafe INTEGER NOT NULL
);
CREATE INDEX document_filters ON documents(status,type,scope);
CREATE INDEX document_fingerprint ON documents(fingerprint,status);
CREATE INDEX document_fts_unsafe ON documents(fts_unsafe);
CREATE TABLE tags(tag TEXT NOT NULL, doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
 PRIMARY KEY(tag,doc_id)) WITHOUT ROWID;
CREATE INDEX tags_document ON tags(doc_id);
CREATE TABLE short_grams(gram TEXT NOT NULL, doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
 PRIMARY KEY(gram,doc_id)) WITHOUT ROWID;
CREATE INDEX grams_document ON short_grams(doc_id);
CREATE VIRTUAL TABLE terms_fts USING fts5(title_norm,tags_norm,body_norm,
 content='documents', content_rowid='doc_id', tokenize='trigram case_sensitive 1');
"""


def _fields(entry):
    return (normalized(entry["title"]), normalized(" ".join(entry["tags"])),
            normalized(entry["body"] + " " + entry["scope"]))


def _grams(fields):
    result = set()
    for value in fields:
        result.update(value)
        result.update(value[i:i + 2] for i in range(len(value) - 1))
    return result


def _leaf_info(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink > 1:
        raise VaultError("UNSAFE_PATH", "拒绝非普通文件、重解析点或硬链接。", {"path": str(path)})
    return info


def _identity(info):
    return info.st_size, info.st_mtime_ns


def _read_source(vault, entry_id):
    path = vault._path("entries", identifier(entry_id) + ".md")
    before = _leaf_info(path)
    raw = vault._raw(path)
    after = _leaf_info(path)
    if _identity(before) != _identity(after):
        raise VaultError("CONCURRENT_EDIT", "条目读取期间发生变化。", {"id": entry_id})
    entry = parse_entry(raw)
    if entry["id"] != entry_id:
        raise VaultError("INVALID_ENTRY", "条目编号与文件名不一致。", {"path": str(path)})
    return entry, raw, path, after


def _put(connection, entry, raw, info):
    """Keep the three derived tables in the caller's transaction."""
    validate_entry(entry)
    fields = _fields(entry)
    old = connection.execute("SELECT doc_id,title_norm,tags_norm,body_norm FROM documents WHERE id=?", (entry["id"],)).fetchone()
    if old is not None:
        doc_id = old["doc_id"]
        connection.execute("INSERT INTO terms_fts(terms_fts,rowid,title_norm,tags_norm,body_norm) VALUES('delete',?,?,?,?)", tuple(old))
        connection.execute("DELETE FROM short_grams WHERE doc_id=?", (doc_id,))
        connection.execute("DELETE FROM tags WHERE doc_id=?", (doc_id,))
    else:
        doc_id = None
    values = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")), digest(raw), fingerprint(entry),
              info.st_size, info.st_mtime_ns, entry["type"], entry["status"], entry["scope"], entry["updated_at"], *fields,
              int(any("\x00" in value for value in fields)))
    if old is None:
        cursor = connection.execute("INSERT INTO documents(id,entry_json,sha256,fingerprint,size,mtime_ns,type,status,scope,updated_at,title_norm,tags_norm,body_norm,fts_unsafe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (entry["id"], *values))
        doc_id = cursor.lastrowid
    else:
        connection.execute("UPDATE documents SET entry_json=?,sha256=?,fingerprint=?,size=?,mtime_ns=?,type=?,status=?,scope=?,updated_at=?,title_norm=?,tags_norm=?,body_norm=?,fts_unsafe=? WHERE doc_id=?", (*values, doc_id))
    connection.execute("INSERT INTO terms_fts(rowid,title_norm,tags_norm,body_norm) VALUES(?,?,?,?)", (doc_id, *fields))
    connection.executemany("INSERT INTO short_grams(gram,doc_id) VALUES(?,?)", ((term, doc_id) for term in _grams(fields)))
    connection.executemany("INSERT INTO tags(tag,doc_id) VALUES(?,?)", ((tag, doc_id) for tag in set(entry["tags"])))


def _file_hash(path):
    import hashlib
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _index_files(vault, target):
    snapshot = {}
    for suffix in ("", *SIDECARS):
        path = vault._guard(Path(str(target) + suffix))
        if path.exists():
            _leaf_info(path)
            snapshot[suffix] = _file_hash(path)
        else:
            snapshot[suffix] = None
    return snapshot


def _copy_backup(vault, source, target, checksum):
    """Copy a verified main DB, retaining the source until atomic publication."""
    import shutil
    temp = vault._guard(target.with_name(".copy-" + str(uuid.uuid4())))
    try:
        vault._guard(source)
        vault._guard(target)
        if target.exists():
            raise VaultError("UNOWNED_INDEX", "恢复备份路径已存在，未覆盖。")
        with source.open("rb") as incoming, temp.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if _file_hash(temp) != checksum or _file_hash(source) != checksum:
            raise VaultError("CONFLICT", "建立备份时旧索引发生变化。")
        vault._guard(target)
        if target.exists():
            raise VaultError("CONFLICT", "备份目标在复制时被其他操作创建。")
        os.replace(temp, target)
    finally:
        if temp.exists():
            vault._guard(temp).unlink()


def _prior_backups(vault, marker, owner):
    if not marker.exists():
        return [], None
    raw = vault._raw(marker)
    try:
        data = json.loads(raw)
        if data.get("app") != "visible-memory-index-rebuild" or data.get("version") != 1 or data.get("vault_id") != owner:
            raise ValueError("恢复标记归属不匹配")
        paths = data["recovery_backups"]
        if not isinstance(paths, list):
            raise ValueError("恢复备份列表损坏")
        verified_paths = []
        for relative in paths:
            path = Path(relative)
            if path.is_absolute() or len(path.parts) != 4 or path.parts[:2] != ("history", "index-recovery") or path.name not in {DB_NAME, *(DB_NAME + suffix for suffix in SIDECARS)}:
                raise ValueError("恢复备份路径不匹配")
            identifier(path.parts[2])
            absolute = vault._guard(vault.root / path)
            if absolute.exists():
                _leaf_info(absolute)
                verified_paths.append(str(path))
        return verified_paths, digest(raw)
    except (ValueError, TypeError, KeyError) as exc:
        raise VaultError("INDEX_CORRUPT", f"重建恢复标记损坏，未覆盖：{exc}") from None


def build(vault, records, allow_replace=True, vault_id=None):
    """Explicit full rebuild; atomically replace only our owned derived database."""
    folder = vault._path("_index")
    target = _db_path(vault)
    marker = vault._path("_index", REBUILD_MARKER)
    if not allow_replace:
        existing = [vault._guard(Path(str(target) + suffix)) for suffix in ("", *SIDECARS)] + [marker]
        if any(path.exists() for path in existing):
            raise VaultError("UNOWNED_INDEX", "检索目录已有未登记的主库、侧车或恢复标记，未读取或覆盖。")
    original = _index_files(vault, target)
    owner = identifier(vault_id) if vault_id is not None else _vault_id(vault)
    prior_backups, marker_hash = _prior_backups(vault, marker, owner)
    folder.mkdir(exist_ok=True)
    temp = vault._path("_index", ".build-" + str(uuid.uuid4()) + ".sqlite")
    connection = None
    try:
        # Claim our exact temporary name before handing it to SQLite.
        with temp.open("xb"):
            pass
        connection = sqlite3.connect(temp.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        _configure(connection, False)
        connection.execute(f"PRAGMA application_id={APP_ID}")
        connection.execute(f"PRAGMA user_version={VERSION}")
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        count = 0
        seen = set()
        entries_folder = vault._path("entries")
        for entry, raw, path in records:
            identifier(entry["id"])
            if entry["id"] in seen:
                raise VaultError("INVALID_ENTRY", "重建输入含重复编号。")
            seen.add(entry["id"])
            path = Path(path)
            if path != entries_folder / (entry["id"] + ".md"):
                raise VaultError("UNSAFE_PATH", "重建输入原文路径不匹配。")
            # records were scanned by the caller; reject changes since that scan.
            before = _leaf_info(path)
            current = path.read_bytes()
            info = _leaf_info(path)
            if current != raw or _identity(before) != _identity(info):
                raise VaultError("CONCURRENT_EDIT", "重建过程中原文发生变化；原索引保留。", {"id": entry["id"]})
            if parse_entry(raw) != entry:
                raise VaultError("INVALID_ENTRY", "重建输入与原文不一致。")
            _put(connection, entry, raw, info)
            count += 1
        stamp = now()
        connection.executemany("INSERT INTO meta(key,value) VALUES(?,?)", (("built_at", stamp), ("updated_at", stamp), ("vault_id", owner)))
        connection.execute("COMMIT")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("SELECT count(*) FROM documents").fetchone()[0] != count:
            raise VaultError("INDEX_CORRUPT", "临时索引验证失败。")
        connection.close()
        connection = None
        with temp.open("r+b") as stream:
            os.fsync(stream.fileno())
        fresh_hash = _file_hash(temp)
        vault._guard(target)
        if _index_files(vault, target) != original:
            raise VaultError("CONFLICT", "索引在重建期间被其他操作修改。")
        backups = list(prior_backups)
        backup_folder = None
        if any(original[suffix] is not None for suffix in SIDECARS):
            backup_folder = vault._path("history", "index-recovery", str(uuid.uuid4()))
            backup_folder.mkdir(parents=True, exist_ok=False)
            backups.extend(str((backup_folder / (DB_NAME + suffix)).relative_to(vault.root))
                           for suffix in ("", *SIDECARS) if original[suffix] is not None)
        marker_data = (json.dumps({"app": "visible-memory-index-rebuild", "version": 1,
                                  "vault_id": owner, "recovery_backups": backups},
                                 ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        vault._atomic(marker, marker_data, expected=marker_hash, new=marker_hash is None)
        # Readers now fall back to Markdown even if the process stops after a
        # sidecar has moved and before the replacement main DB is published.
        if backup_folder is not None:
            if original[""] is not None:
                _copy_backup(vault, target, vault._guard(backup_folder / DB_NAME), original[""])
            for suffix in SIDECARS:
                if original[suffix] is None:
                    continue
                source = vault._guard(Path(str(target) + suffix))
                backup = vault._guard(backup_folder / (DB_NAME + suffix))
                if not source.exists() or _file_hash(source) != original[suffix] or backup.exists():
                    raise VaultError("CONFLICT", "移交恢复侧车前校验失败；恢复标记和已有备份保留。")
                os.replace(source, backup)
                if _file_hash(backup) != original[suffix]:
                    raise VaultError("CONFLICT", "侧车移交后校验失败；备份保留。")
        expected_after_moves = {"": original[""], **{suffix: None for suffix in SIDECARS}}
        if _index_files(vault, target) != expected_after_moves:
            raise VaultError("CONFLICT", "发布新索引前原主库或侧车发生变化；恢复备份保留。")
        os.replace(temp, target)
        if _file_hash(target) != fresh_hash:
            raise VaultError("COMMITTED_UNVERIFIED", "新索引已替换但读回校验失败。")
        if digest(vault._raw(marker)) != digest(marker_data):
            raise VaultError("COMMITTED_UNVERIFIED", "新索引已发布但恢复标记发生变化。")
        vault._guard(marker).unlink()
        return {"recovery_backups": [str(vault._guard(vault.root / path)) for path in backups]}
    except sqlite3.Error as exc:
        raise VaultError("INDEX_CORRUPT", f"索引重建失败：{exc}") from None
    finally:
        if connection is not None:
            connection.close()
        for suffix in ("", *SIDECARS):
            owned = vault._guard(Path(str(temp) + suffix))
            if owned.exists():
                owned.unlink()


def upsert(vault, entry, raw, path):
    current, current_raw, current_path, info = _read_source(vault, entry["id"])
    if current != entry or current_raw != raw or Path(path) != current_path:
        raise VaultError("CONCURRENT_EDIT", "索引写入前原文已变化。")
    try:
        with contextlib.closing(connect(vault, readonly=False)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _put(connection, entry, raw, info)
                connection.execute("UPDATE meta SET value=? WHERE key='updated_at'", (now(),))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            if connection.execute("SELECT sha256 FROM documents WHERE id=?", (entry["id"],)).fetchone()[0] != digest(raw):
                raise VaultError("COMMITTED_UNVERIFIED", "索引更新后的校验值不匹配。")
    except sqlite3.Error as exc:
        raise VaultError("INDEX_CORRUPT", f"索引更新失败：{exc}") from None


def _catalog(vault, connection):
    """One scandir plus cached metadata; never open unchanged Markdown files."""
    indexed = {row["id"]: (row["size"], row["mtime_ns"]) for row in connection.execute("SELECT id,size,mtime_ns FROM documents")}
    present, errors = {}, []
    folder = vault._path("entries")
    with os.scandir(folder) as stream:
        for item in stream:
            try:
                if not item.name.endswith(".md"):
                    raise VaultError("UNKNOWN_FILE", "entries 中包含非条目文件；未读取。")
                entry_id = identifier(item.name[:-3])
                info = item.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink > 1:
                    raise VaultError("UNSAFE_PATH", "拒绝非普通文件、重解析点或硬链接。")
                present[entry_id] = _identity(info)
            except (OSError, VaultError) as exc:
                errors.append({"path": item.path, "code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)})
    added = set(present) - set(indexed)
    deleted = set(indexed) - set(present)
    changed = {entry_id for entry_id in present.keys() & indexed.keys() if present[entry_id] != indexed[entry_id]}
    return {"added": added, "changed": changed, "deleted": deleted, "errors": errors}


def _entry_from_row(row):
    try:
        entry = json.loads(row["entry_json"])
        validate_entry(entry)
        if entry["id"] != row["id"]:
            raise ValueError("编号不一致")
        return entry
    except (ValueError, TypeError, KeyError, VaultError) as exc:
        raise VaultError("INDEX_CORRUPT", f"索引条目损坏：{exc}") from None


def _filter(entry, include_archived, entry_type, tags, scope):
    return ((include_archived or entry["status"] == "active") and
            (entry_type is None or entry["type"] == entry_type) and
            (scope is None or entry["scope"] == scope) and
            all(tag in entry["tags"] for tag in (tags or [])))


def _score(entry, terms):
    title, tags, body = _fields(entry)
    return sum(5 * (term in title) + 3 * (term in tags) + (term in body) for term in terms)


def _candidate_rows(connection, terms, include_archived, entry_type, tags, scope):
    candidate_ids = None
    if terms:
        candidate_ids = set()
        for term in set(terms):
            if len(term) < 3:
                candidate_ids.update(row[0] for row in connection.execute("SELECT doc_id FROM short_grams WHERE gram=?", (term,)))
            elif "\x00" in term:
                # FTS treats embedded NUL specially; preserve literal semantics.
                candidate_ids = None
                break
            else:
                literal = '"' + term.replace('"', '""') + '"'
                candidate_ids.update(row[0] for row in connection.execute("SELECT rowid FROM terms_fts WHERE terms_fts MATCH ?", (literal,)))
                candidate_ids.update(row[0] for row in connection.execute("SELECT doc_id FROM documents WHERE fts_unsafe=1"))
    where, params = [], []
    if not include_archived:
        where.append("d.status='active'")
    if entry_type is not None:
        where.append("d.type=?")
        params.append(entry_type)
    if scope is not None:
        where.append("d.scope=?")
        params.append(scope)
    for tag in set(tags or []):
        where.append("EXISTS(SELECT 1 FROM tags t WHERE t.doc_id=d.doc_id AND t.tag=?)")
        params.append(tag)
    predicate = " AND ".join(where) or "1"
    if candidate_ids is None:
        yield from connection.execute("SELECT d.* FROM documents d WHERE " + predicate, params)
    else:
        values = sorted(candidate_ids)
        for position in range(0, len(values), 800):
            chunk = values[position:position + 800]
            yield from connection.execute("SELECT d.* FROM documents d WHERE " + predicate + " AND d.doc_id IN (" + ",".join("?" for _ in chunk) + ")", (*params, *chunk))


def _indexed_matches(vault, connection, terms, include_archived, entry_type, tags, scope, forced):
    delta = _catalog(vault, connection)
    excluded = delta["added"] | delta["changed"] | delta["deleted"] | forced
    matches, errors = [], list(delta["errors"])
    for row in _candidate_rows(connection, terms, include_archived, entry_type, tags, scope):
        if row["id"] in excluded:
            continue
        entry = _entry_from_row(row)
        if not _filter(entry, include_archived, entry_type, tags, scope):
            continue
        score = _score(entry, terms)
        if terms and not score:
            continue
        matches.append((score, entry, row["sha256"], vault.root / "entries" / (entry["id"] + ".md")))
    for entry_id in sorted((delta["added"] | delta["changed"] | forced) - delta["deleted"]):
        try:
            entry, raw, path, _ = _read_source(vault, entry_id)
            if not _filter(entry, include_archived, entry_type, tags, scope):
                continue
            score = _score(entry, terms)
            if not terms or score:
                matches.append((score, entry, digest(raw), path))
        except (OSError, VaultError) as exc:
            errors.append({"id": entry_id, "code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)})
    return matches, errors, delta


def _fallback(vault, terms, include_archived, entry_type, tags, scope):
    records, errors = vault._scan()
    found = []
    for entry, raw, path in records:
        if not _filter(entry, include_archived, entry_type, tags, scope):
            continue
        score = _score(entry, terms)
        if not terms or score:
            found.append((score, entry, digest(raw), path))
    return found, errors


def _summary(entry, checksum, path, terms):
    body = entry["body"]
    searchable = {"title": normalized(entry["title"]), "tags": normalized(" ".join(entry["tags"])),
                  "body": normalized(body), "scope": normalized(entry["scope"])}
    matched_terms = [term for term in dict.fromkeys(terms) if any(term in value for value in searchable.values())]
    matched_fields = [field for field, value in searchable.items() if any(term in value for term in terms)]
    # Keep offsets correct even when NFKC/casefold expands a character: use a
    # nearby original-body window selected by a normalized prefix boundary.
    first = min((normalized(body).find(term) for term in terms if term in normalized(body)), default=-1)
    start = 0
    if first > 100:
        lo, hi = 0, len(body)
        while lo < hi:
            mid = (lo + hi) // 2
            if len(normalized(body[:mid])) < first:
                lo = mid + 1
            else:
                hi = mid
        start = max(0, lo - 80)
    return {"id": entry["id"], "title": entry["title"], "type": entry["type"],
            "status": entry["status"], "tags": entry["tags"], "scope": entry["scope"],
            "source": entry["source"], "updated_at": entry["updated_at"],
            "excerpt": ("…" if start else "") + body[start:start + (239 if start else 240)],
            "matched_terms": matched_terms, "matched_fields": matched_fields,
            "sha256": checksum, "path": str(path),
            "needs_review": bool(entry.get("review_after") and entry["review_after"] <= dt.date.today().isoformat())}


def search(vault, query="", include_archived=False, limit=8, offset=0, entry_type=None, tags=None, scope=None):
    """Search/list with exact global candidate ranking and a non-persistent overlay."""
    if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise VaultError("INVALID_QUERY", "查询或分页参数不合法。")
    if tags is not None and (not isinstance(tags, (list, tuple)) or any(not isinstance(t, str) for t in tags)):
        raise VaultError("INVALID_FILTER", "tags 必须是字符串列表。")
    terms = normalized(query).split()
    forced = set()
    for attempt in range(2):
        warnings = []
        mode = "indexed"
        delta = {"added": set(), "changed": set(), "deleted": set()}
        try:
            with contextlib.closing(connect(vault)) as connection:
                found, errors, delta = _indexed_matches(vault, connection, terms, include_archived, entry_type, tags, scope, forced)
        except (sqlite3.Error, VaultError) as exc:
            if isinstance(exc, VaultError) and exc.code not in RECOVERABLE:
                raise
            mode = "full_scan_fallback"
            warnings.append({"code": "INDEX_FALLBACK", "message": str(exc)})
            found, errors = _fallback(vault, terms, include_archived, entry_type, tags, scope)
        found.sort(key=lambda item: (item[0], item[1]["updated_at"], item[1]["id"]), reverse=True)
        page = found[offset:offset + limit]
        drift = set()
        for _, entry, checksum, _ in page:
            try:
                actual, raw, _, _ = _read_source(vault, entry["id"])
                if digest(raw) != checksum or actual != entry:
                    drift.add(entry["id"])
            except (OSError, VaultError):
                drift.add(entry["id"])
        if drift:
            forced.update(drift)
            if attempt == 0:
                continue
            raise VaultError("CONCURRENT_EDIT", "结果原文连续变化；已重试一次，未返回旧缓存。", {"ids": sorted(drift)})
        stale = mode != "indexed" or bool(delta["added"] or delta["changed"] or delta["deleted"] or forced)
        return {"entries": [_summary(entry, checksum, path, terms) for _, entry, checksum, path in page],
                "total": len(found), "offset": offset, "errors": errors, "complete": not errors,
                "mode": mode, "warnings": warnings,
                "index_stale": stale, "views_stale": stale,
                "verification": "metadata_plus_selected_sha256" if mode == "indexed" else "full_scan_plus_selected_sha256",
                "changes": {key: len(delta[key]) for key in ("added", "changed", "deleted")}}


def status(vault):
    try:
        with contextlib.closing(connect(vault)) as connection:
            delta = _catalog(vault, connection)
            counts = {row[0]: row[1] for row in connection.execute("SELECT status,count(*) FROM documents GROUP BY status")}
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            return {"index_available": True, "active_count": counts.get("active", 0), "archived_count": counts.get("archived", 0),
                    "counts_source": "index_snapshot", "built_at": meta["built_at"], "updated_at": meta["updated_at"],
                    "changes": {key: sorted(delta[key]) for key in ("added", "changed", "deleted")},
                    "errors": delta["errors"], "index_stale": any(delta.values())}
    except (sqlite3.Error, VaultError) as exc:
        if isinstance(exc, VaultError) and exc.code not in RECOVERABLE:
            raise
        records, errors = vault._scan()
        return {"index_available": False, "index_stale": True, "counts_source": "full_scan_fallback",
                "active_count": sum(e["status"] == "active" for e, _, _ in records),
                "archived_count": sum(e["status"] == "archived" for e, _, _ in records),
                "errors": errors, "warnings": [{"code": "INDEX_FALLBACK", "message": str(exc)}]}


def check(vault, records):
    """Explicit full-hash check; does not repair or write an index."""
    actual = {entry["id"]: (entry, digest(raw)) for entry, raw, _ in records}
    try:
        with contextlib.closing(connect(vault)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise VaultError("INDEX_CORRUPT", "SQLite 完整性检查未通过。")
            cached, inconsistent_columns = {}, set()
            for row in connection.execute("SELECT * FROM documents"):
                entry = _entry_from_row(row)
                cached[row["id"]] = (entry, row["sha256"])
                if (_fields(entry) != (row["title_norm"], row["tags_norm"], row["body_norm"]) or
                    any(entry[key] != row[key] for key in ("type", "status", "scope", "updated_at")) or
                    fingerprint(entry) != row["fingerprint"]):
                    inconsistent_columns.add(row["id"])
            delta = {"added": sorted(actual.keys() - cached.keys()), "deleted": sorted(cached.keys() - actual.keys()),
                     "changed": sorted({key for key in actual.keys() & cached.keys() if actual[key] != cached[key]} | inconsistent_columns)}
            return {"index_available": True, "index_consistent": not any(delta.values()), "index_stale": any(delta.values()), "index_changes": delta}
    except (sqlite3.Error, VaultError) as exc:
        if isinstance(exc, VaultError) and exc.code not in RECOVERABLE:
            raise
        return {"index_available": False, "index_consistent": False, "index_stale": True,
                "warnings": [{"code": "INDEX_FALLBACK", "message": str(exc)}]}


def iter_entries(vault, ids=None):
    """Yield (entry, sha256, canonical_path) from the cached snapshot, not overlay."""
    try:
        with contextlib.closing(connect(vault)) as connection:
            if ids is None:
                rows = connection.execute("SELECT * FROM documents ORDER BY id")
                for row in rows:
                    entry = _entry_from_row(row)
                    yield entry, row["sha256"], vault.root / "entries" / (entry["id"] + ".md")
            else:
                for entry_id in ids:
                    identifier(entry_id)
                    row = connection.execute("SELECT * FROM documents WHERE id=?", (entry_id,)).fetchone()
                    if row is not None:
                        entry = _entry_from_row(row)
                        yield entry, row["sha256"], vault.root / "entries" / (entry_id + ".md")
    except sqlite3.Error as exc:
        raise VaultError("INDEX_CORRUPT", f"读取索引快照失败：{exc}") from None


def duplicates(vault, entry):
    """Fingerprint-only admission check. No semantic recall and no index writes."""
    desired = fingerprint(entry)
    candidates = []
    try:
        with contextlib.closing(connect(vault)) as connection:
            delta = _catalog(vault, connection)
            if delta["errors"]:
                raise VaultError("INVALID_ENTRIES", "存在无法校验的条目，未执行保存。", delta["errors"])
            excluded = delta["added"] | delta["changed"] | delta["deleted"]
            candidates.extend(row[0] for row in connection.execute("SELECT id FROM documents WHERE fingerprint=? AND status='active' ORDER BY id", (desired,)) if row[0] not in excluded)
            for entry_id in sorted(delta["added"] | delta["changed"]):
                current, raw, path, _ = _read_source(vault, entry_id)
                if current["status"] == "active" and fingerprint(current) == desired:
                    candidates.append(entry_id)
    except (sqlite3.Error, VaultError) as exc:
        if isinstance(exc, VaultError) and exc.code not in RECOVERABLE:
            raise
        records, errors = vault._scan()
        if errors:
            raise VaultError("INVALID_ENTRIES", "条目无法解析，不能可靠检查重复内容。", errors)
        candidates = [current["id"] for current, _, _ in records if current["status"] == "active" and fingerprint(current) == desired]
    for entry_id in sorted(set(candidates)):
        current, raw, path, _ = _read_source(vault, entry_id)
        if current["status"] == "active" and fingerprint(current) == desired:
            return current, raw, path
    return None
