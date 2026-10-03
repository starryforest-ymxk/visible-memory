"""Bounded Markdown navigation with durable, stable page membership.

Only called inside an explicitly authorized write operation and its vault lock.
Display metadata is derived data; it deliberately contains no full entry body.
"""
from __future__ import annotations

import re
import os
import uuid

from memory_v1 import TYPES, VIEWS, VaultError, digest, md_text, validate_entry


PAGE_SIZE = 50
# One backlink plus at most 49 children keeps every navigation page at 50 links.
FANOUT = 49
PARTITIONS = tuple(f"{status}/{kind}" for status in ("active", "archived") for kind in TYPES)


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS views_pages (
            partition TEXT NOT NULL, page INTEGER NOT NULL CHECK(page > 0),
            dirty INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(partition, page)
        );
        CREATE TABLE IF NOT EXISTS views_members (
            id TEXT PRIMARY KEY, partition TEXT NOT NULL, page INTEGER NOT NULL,
            title TEXT NOT NULL, excerpt TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS views_member_page
            ON views_members(partition, page, created_at, id);
        CREATE TABLE IF NOT EXISTS views_files (
            path TEXT PRIMARY KEY, sha256 TEXT, target_sha256 TEXT,
            dirty INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS views_state (
            id INTEGER PRIMARY KEY CHECK(id=1), dirty INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO views_state(id,dirty) VALUES(1,1);
    """)


def adopt_legacy(conn, viewsdict):
    """Import only previously owned v1 root views; never claim arbitrary paths."""
    with conn:
        for name, checksum in viewsdict.items():
            if name not in VIEWS or not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                raise VaultError("INVALID_STATE", "旧版总览登记信息无效。")
            # A resumed migration may already have published the v2 page. Keep
            # its newer ownership record instead of restoring the v1 checksum.
            conn.execute("INSERT OR IGNORE INTO views_files(path,sha256,dirty) VALUES(?,?,1)", (name, checksum))


def file_hashes(conn):
    return dict(conn.execute("SELECT path,sha256 FROM views_files WHERE sha256 IS NOT NULL"))


def pending(conn):
    return bool(conn.execute("SELECT 1 FROM views_state WHERE dirty=1 LIMIT 1").fetchone()
                or conn.execute("SELECT 1 FROM views_pages WHERE dirty=1 LIMIT 1").fetchone()
                or conn.execute("SELECT 1 FROM views_files WHERE dirty=1 LIMIT 1").fetchone())


def status(conn):
    pages = conn.execute("SELECT COUNT(*),COALESCE(SUM(dirty),0) FROM views_pages").fetchone()
    files = conn.execute("SELECT COUNT(*),COALESCE(SUM(dirty),0) FROM views_files").fetchone()
    return {"views_stale": pending(conn), "content_pages": pages[0],
            "dirty_content_pages": pages[1], "generated_files": files[0], "dirty_generated_files": files[1]}


def check(vault, conn):
    """Read-only hash and ownership inspection, without traversing unsafe links."""
    errors = []
    registered = file_hashes(conn)
    for name, checksum in registered.items():
        try:
            path = vault._path(*name.split("/"))
            if not path.is_file():
                raise VaultError("MISSING_VIEW", "已登记总览文件缺失。")
            if digest(vault._raw(path)) != checksum:
                raise VaultError("VIEW_CHANGED", "总览内容与登记校验值不同；刷新前将保留备份。")
        except (VaultError, OSError) as exc:
            errors.append({"path": str(vault.root / name), "code": getattr(exc, "code", "VIEW_IO_ERROR"), "message": str(exc)})
    for name in VIEWS:
        if name not in registered:
            try:
                if vault._path(name).exists():
                    errors.append({"path": str(vault.root / name), "code": "UNOWNED_FILE", "message": "根目录存在未登记的同名总览文件。"})
            except (VaultError, OSError) as exc:
                errors.append({"path": str(vault.root / name), "code": getattr(exc, "code", "VIEW_IO_ERROR"), "message": str(exc)})
    try:
        folder = vault._path("catalog")
        stack = [folder] if folder.exists() else []
        while stack:
            current = stack.pop()
            with os.scandir(current) as items:
                for item in items:
                    try:
                        path = vault._guard(item.path)
                        if item.is_dir(follow_symlinks=False):
                            stack.append(path)
                        elif path.relative_to(vault.root).as_posix() not in registered:
                            errors.append({"path": str(path), "code": "UNOWNED_FILE", "message": "分类目录存在未登记文件；保留且不读取。"})
                    except (VaultError, OSError) as exc:
                        errors.append({"path": item.path, "code": getattr(exc, "code", "VIEW_IO_ERROR"), "message": str(exc)})
    except (VaultError, OSError) as exc:
        errors.append({"path": str(vault.root / "catalog"), "code": getattr(exc, "code", "VIEW_IO_ERROR"), "message": str(exc)})
    return {**status(conn), "errors": errors, "views_stale": bool(errors) or pending(conn)}


def _short(text, limit):
    # Bound what is copied into display tables even for unusually large entries.
    compact = " ".join(text[: max(4096, limit * 4)].split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _partition(entry):
    partition = f"{entry['status']}/{entry['type']}"
    if partition not in PARTITIONS:
        raise VaultError("INVALID_ENTRY", "分页类型或状态无效。")
    return partition


def _assign(conn, entry):
    validate_entry(entry)
    partition = _partition(entry)
    previous = conn.execute("SELECT partition,page FROM views_members WHERE id=?", (entry["id"],)).fetchone()
    affected = set()
    if previous is not None and previous[0] == partition:
        page = previous[1]
    else:
        if previous is not None:
            affected.add((previous[0], previous[1]))
            conn.execute("DELETE FROM views_members WHERE id=?", (entry["id"],))
        available = conn.execute("""
            SELECT p.page FROM views_pages p
            LEFT JOIN views_members m ON m.partition=p.partition AND m.page=p.page
            WHERE p.partition=? GROUP BY p.page HAVING COUNT(m.id) < ?
            ORDER BY p.page LIMIT 1
        """, (partition, PAGE_SIZE)).fetchone()
        if available:
            page = available[0]
        else:
            page = conn.execute("SELECT COALESCE(MAX(page),0)+1 FROM views_pages WHERE partition=?", (partition,)).fetchone()[0]
            conn.execute("INSERT INTO views_pages(partition,page,dirty) VALUES(?,?,1)", (partition, page))
    conn.execute("""
        INSERT INTO views_members(id,partition,page,title,excerpt,created_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET partition=excluded.partition,page=excluded.page,
            title=excluded.title,excerpt=excluded.excerpt,created_at=excluded.created_at
    """, (entry["id"], partition, page, _short(entry["title"], 64), _short(entry["body"], 96), entry["created_at"]))
    affected.add((partition, page))
    conn.executemany("UPDATE views_pages SET dirty=1 WHERE partition=? AND page=?", affected)
    return affected


def _ensure_folder(vault, parts):
    folder = vault._path(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    vault._guard(folder)


def _write(vault, conn, name, text, warnings):
    parts = name.split("/")
    path = vault._path(*parts)
    data = text.encode("utf-8")
    if len(data) > (32768 if name in VIEWS else 65536) or (name in VIEWS and len(text.splitlines()) > 200):
        raise VaultError("VIEW_SIZE_LIMIT", f"生成页面超出大小上限：{name}")
    known = conn.execute("SELECT sha256,target_sha256,dirty FROM views_files WHERE path=?", (name,)).fetchone()
    previous = vault._raw(path) if path.exists() else None
    previous_sha = digest(previous) if previous is not None else None
    target = digest(data)
    if previous == data and known is not None and known[0] == target and known[1] is None and not known[2]:
        return
    if previous is not None:
        if known is None or (known[0] is None and previous_sha != known[1]):
            raise VaultError("UNOWNED_FILE", f"未登记的同名文件，拒绝覆盖：{path}")
        if previous_sha not in (known[0], known[1]):
            _ensure_folder(vault, ("history", "views"))
            backup = vault._path("history", "views", f"{uuid.uuid4()}-{path.name}")
            vault._atomic(backup, previous, new=True)
            warnings.append({"code": "MANUAL_VIEW_BACKUP", "path": str(backup),
                             "message": f"{name} 的手工修改已备份。"})
    # Persist ownership intent before creating the file so interruption can resume.
    with conn:
        conn.execute("""INSERT INTO views_files(path,target_sha256,dirty) VALUES(?,?,1)
            ON CONFLICT(path) DO UPDATE SET target_sha256=excluded.target_sha256,dirty=1""", (name, target))
    if previous != data:
        _ensure_folder(vault, tuple(parts[:-1]))
        vault._atomic(path, data, expected=previous_sha, new=previous is None)
    with conn:
        conn.execute("UPDATE views_files SET sha256=?,target_sha256=NULL,dirty=0 WHERE path=?", (target, name))


def _page_document(conn, partition, page):
    status, kind = partition.split("/")
    records = conn.execute("SELECT id,title,excerpt,created_at FROM views_members WHERE partition=? AND page=? ORDER BY created_at,id", (partition, page)).fetchall()
    if len(records) > PAGE_SIZE:
        raise VaultError("INVALID_STATE", "页面成员超过 50 条；请检查分页状态。")
    lines = [f"# {TYPES[kind]} · {'有效' if status == 'active' else '归档'} · 第 {page} 页", "",
             "[分类目录](INDEX.md) · [记忆库首页](../../../INDEX.md)", "",
             "> 本页由脚本生成。请修改链接中的条目原文，再明确要求刷新总览。", ""]
    if not records:
        lines.extend(["本页目前没有条目。页面保留，原有链接仍然有效。", ""])
    for entry_id, title, excerpt, created_at in records:
        lines.append(f"- [{md_text(title)}](../../../entries/{entry_id}.md) · {md_text(created_at[:10])}")
        lines.append(f"  - {md_text(excerpt)}")
    return "\n".join(lines) + "\n"


def _navigation_documents(conn, partition):
    status, kind = partition.split("/")
    title = f"{TYPES[kind]} · {'有效' if status == 'active' else '归档'}"
    pages = [row[0] for row in conn.execute("SELECT page FROM views_pages WHERE partition=? ORDER BY page", (partition,))]
    count = conn.execute("SELECT COUNT(*) FROM views_members WHERE partition=?", (partition,)).fetchone()[0]
    children = [(f"第 {page} 页", f"page-{page:06d}.md") for page in pages]
    documents = {}
    level = 0
    while len(children) > FANOUT:
        level += 1
        parents = []
        for start in range(0, len(children), FANOUT):
            number = start // FANOUT + 1
            filename = f"nav-l{level}-{number:06d}.md"
            lines = [f"# {title} · 导航 {level}-{number}", "", "[分类目录](INDEX.md)", ""]
            lines.extend(f"- [{label}]({link})" for label, link in children[start:start + FANOUT])
            documents[f"catalog/{partition}/{filename}"] = "\n".join(lines) + "\n"
            parents.append((f"导航 {level}-{number}", filename))
        children = parents
    lines = [f"# {title}", "", "[记忆库首页](../../../INDEX.md)", "",
             f"总览快照：{count} 条，{len(pages)} 个分册页面。", "",
             "页面归属保持稳定；全局排序和筛选请明确要求检索记忆。", ""]
    lines.extend(f"- [{label}]({link})" for label, link in children)
    if not children:
        lines.append("目前没有条目。")
    documents[f"catalog/{partition}/INDEX.md"] = "\n".join(lines) + "\n"
    return documents


def _root_documents(conn):
    counts = dict(conn.execute("SELECT partition,COUNT(*) FROM views_members GROUP BY partition"))
    def links(status, kinds):
        return "\n".join(f"- [{TYPES[kind]}](catalog/{status}/{kind}/INDEX.md)：{counts.get(f'{status}/{kind}', 0)} 条" for kind in kinds)
    active = sum(counts.get(f"active/{kind}", 0) for kind in TYPES)
    archived = sum(counts.get(f"archived/{kind}", 0) for kind in TYPES)
    intro = f"""# 可见记忆库

这里保存你明确要求保留的记忆。普通任务不会自动检索或加载本库。

总览快照：有效 {active} 条；归档 {archived} 条。

## 有效记忆分类

{links('active', TYPES)}

## 其他入口

- [用户偏好](USER.md)
- [事实、决策与经验](MEMORY.md)
- [已归档记忆](ARCHIVE.md)
- [条目原文](entries/)
- [历史版本与操作记录](history/)

## 使用方法

- 查看：“检索关于……的记忆”或“看看保存了哪些记忆”。每次授权仅限本次请求。
- 保存：“记住：……”。普通聊天和临时要求不会自动保存。
- 修改：提供条目编号，说“把这条记忆改成……”。
- 归档：“忘记这条记忆”。归档可以恢复，原文和历史仍保留。
- 历史：“查看这条记忆的历史版本”。
- 手工修改：编辑 entries 中对应正文并保留 TOML 文件头；下次明确检索会轻量检查变化。
- 刷新：“刷新记忆索引”或“刷新记忆总览”，重新核验原文并更新生成页面。
- 检查：“检查重复或过时的记忆”；检查本身不修改内容。

## 文件与检索

entries 中的 Markdown 是唯一正式原文；本页和分类分册只用于浏览。
每个内容页最多 50 条。页面保留固定归属，跨页不表示全局时间顺序。
手工修改不立即更新磁盘索引和总览；明确检索会将检测到的变化在内存中合并。
轻量检查比较文件名、大小和修改时间；同时保持大小和时间不变的修改需明确执行完整检查。
生成页面被手工修改时，覆盖前会备份。历史不能还原从未观察或备份过的修改。
_index 是可重建检索缓存；_state 中的操作凭据属于持久记录，不能当缓存删除。
本库为本地明文。明确检索出的内容会进入当前 Codex 任务上下文。
本库与 Codex 原生记忆独立，不自动导入、同步或修改原生记忆设置。
"""
    stamp = "> 本页为生成的导航快照；需要最新内容时请明确检索或刷新。\n\n"
    return {"INDEX.md": intro,
            "USER.md": "# 用户偏好\n\n[记忆库首页](INDEX.md)\n\n" + stamp + links("active", ("preference",)) + "\n",
            "MEMORY.md": "# 事实、决策与经验\n\n[记忆库首页](INDEX.md)\n\n" + stamp + links("active", ("fact", "decision", "lesson")) + "\n",
            "ARCHIVE.md": "# 已归档记忆\n\n[记忆库首页](INDEX.md)\n\n归档不会永久删除原文或历史，可按编号要求恢复。\n\n" + stamp + links("archived", TYPES) + "\n"}


def _render(vault, conn, pages, partitions, warnings):
    for partition, page in sorted(pages):
        name = f"catalog/{partition}/page-{page:06d}.md"
        _write(vault, conn, name, _page_document(conn, partition, page), warnings)
        with conn:
            conn.execute("UPDATE views_pages SET dirty=0 WHERE partition=? AND page=?", (partition, page))
    for partition in sorted(partitions):
        for name, text in _navigation_documents(conn, partition).items():
            _write(vault, conn, name, text, warnings)
    for name, text in _root_documents(conn).items():
        _write(vault, conn, name, text, warnings)


def update(vault, conn, entry, warnings):
    """Maintain one entry using only its supplied content and bounded display rows."""
    had_pending = pending(conn)
    with conn:
        pages = _assign(conn, entry)
        conn.execute("UPDATE views_state SET dirty=1 WHERE id=1")
    _render(vault, conn, pages, {partition for partition, _ in pages}, warnings)
    with conn:
        # A local update cannot prove earlier, unrelated navigation completed.
        # Only a full rebuild may clear a pre-existing unresolved view job.
        conn.execute("UPDATE views_state SET dirty=? WHERE id=1", (int(had_pending),))
    return {"content_pages": len(pages), "pages": [f"catalog/{partition}/page-{page:06d}.md" for partition, page in sorted(pages)]}


def rebuild(vault, conn, records, warnings):
    """Reconcile authoritative records while preserving healthy existing pages."""
    # Validate everything before altering display metadata, and reject duplicate IDs.
    entries = [record[0] for record in records]
    seen = set()
    for entry in entries:
        validate_entry(entry)
        if entry["id"] in seen:
            raise VaultError("INVALID_ENTRIES", "重建输入中存在重复条目编号。")
        seen.add(entry["id"])
    with conn:
        old_ids = {row[0] for row in conn.execute("SELECT id FROM views_members")}
        conn.executemany("DELETE FROM views_members WHERE id=?", ((entry_id,) for entry_id in old_ids - seen))
        # Free changed partitions before assigning newcomers, so every new page
        # choice observes all available holes in the authoritative snapshot.
        for entry in entries:
            conn.execute("DELETE FROM views_members WHERE id=? AND partition<>?", (entry["id"], _partition(entry)))
        # Existing members remain in place. Sorting only determines first allocation.
        for entry in sorted(entries, key=lambda e: (e["created_at"], e["id"])):
            _assign(conn, entry)
        conn.execute("UPDATE views_pages SET dirty=1")
        conn.execute("UPDATE views_state SET dirty=1 WHERE id=1")
    pages = {(row[0], row[1]) for row in conn.execute("SELECT partition,page FROM views_pages")}
    _render(vault, conn, pages, PARTITIONS, warnings)
    with conn:
        conn.execute("UPDATE views_state SET dirty=0 WHERE id=1")
    return {"entries": len(entries), "content_pages": len(pages), "files": len(file_hashes(conn))}
