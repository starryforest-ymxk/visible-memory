#!/usr/bin/env python3
"""Explicit-request, local Markdown memory store. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
import unicodedata
import uuid

READS = {"status", "list", "search", "show", "history", "check"}
WRITES = {"init", "add", "update", "archive", "restore", "rebuild"}
TYPES = {"preference": "偏好", "fact": "事实", "decision": "决策", "lesson": "经验", "note": "笔记"}
VIEWS = ("INDEX.md", "USER.md", "MEMORY.md", "ARCHIVE.md")
FIELDS = {"title", "type", "tags", "source", "scope", "body", "review_after"}
META = ("id", "title", "type", "status", "tags", "created_at", "updated_at", "revision", "source", "scope", "review_after")


class VaultError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise VaultError("INVALID_ID", "条目编号必须是规范的小写 UUID。") from None
    return value


def normalized(text):
    return unicodedata.normalize("NFKC", text).casefold()


def md_text(text):
    text = " ".join(str(text).split())
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def validate_entry(entry):
    identifier(entry.get("id"))
    if set(entry) - set(META) - {"body"}:
        raise VaultError("INVALID_ENTRY", "条目包含未知字段；请保留原文并检查格式。")
    for key in ("title", "source", "scope", "body"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise VaultError("INVALID_ENTRY", f"{key} 必须是非空字符串。")
    if not isinstance(entry.get("type"), str) or entry["type"] not in TYPES or not isinstance(entry.get("status"), str) or entry["status"] not in {"active", "archived"}:
        raise VaultError("INVALID_ENTRY", "type 或 status 不合法。")
    if type(entry.get("revision")) is not int or entry["revision"] < 1:
        raise VaultError("INVALID_ENTRY", "revision 必须是正整数。")
    tags = entry.get("tags")
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise VaultError("INVALID_ENTRY", "tags 必须是字符串数组。")
    for key in ("created_at", "updated_at"):
        try:
            value = dt.datetime.fromisoformat(entry[key])
            if value.tzinfo is None:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise VaultError("INVALID_ENTRY", f"{key} 必须是带时区的 ISO 8601 字符串。") from None
    if "review_after" in entry:
        try:
            if not isinstance(entry["review_after"], str):
                raise ValueError()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["review_after"]):
                raise ValueError()
            dt.date.fromisoformat(entry["review_after"])
        except (ValueError, TypeError):
            raise VaultError("INVALID_ENTRY", "review_after 必须是 YYYY-MM-DD 字符串。") from None


def render_entry(entry):
    validate_entry(entry)
    lines = ["+++"]
    for key in META:
        if key in entry:
            # JSON string/list/int literals are valid for these TOML field types.
            lines.append(f"{key} = {json.dumps(entry[key], ensure_ascii=False)}")
    return ("\n".join(lines) + "\n+++\n" + entry["body"]).encode("utf-8")


def parse_entry(raw):
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("+++\n"):
            raise ValueError("缺少 +++ TOML 文件头")
        header, body = text[4:].split("\n+++\n", 1)
        entry = tomllib.loads(header)
        entry["body"] = body
        validate_entry(entry)
        return entry
    except (UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise VaultError("INVALID_ENTRY", f"Markdown/TOML 格式错误：{exc}") from None


def fingerprint(entry):
    return digest(json_bytes({k: entry[k] for k in sorted(FIELDS) if k in entry}))


class Vault:
    def __init__(self, root):
        # Constructing a Vault never opens the directory; authorization is checked first.
        self.root = Path(os.path.abspath(os.fspath(root)))

    def _guard(self, path):
        path = Path(os.path.abspath(path))
        try:
            path.relative_to(self.root)
        except ValueError:
            raise VaultError("UNSAFE_PATH", "路径超出指定记忆库。") from None
        if self.root == Path(self.root.anchor) or str(self.root).startswith("\\\\"):
            raise VaultError("UNSAFE_PATH", "不能使用磁盘根目录或网络路径作为记忆库。")
        for part in reversed((path, *path.parents)):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise VaultError("UNSAFE_PATH", f"拒绝符号链接或重解析点：{part}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise VaultError("UNSAFE_PATH", f"拒绝具有多个硬链接的文件：{part}")
        return path

    def _path(self, *parts):
        return self._guard(self.root.joinpath(*parts))

    def _raw(self, path):
        self._guard(path)
        return path.read_bytes()

    def _atomic(self, path, data, expected=None, new=False):
        self._guard(path)
        if not path.parent.is_dir():
            raise VaultError("MISSING_DIRECTORY", f"目录不存在：{path.parent}")
        temp = self._path(*path.relative_to(self.root).parts[:-1], ".vm-" + str(uuid.uuid4()) + ".tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._guard(path)
            if new and path.exists():
                raise VaultError("CONFLICT", f"目标文件已存在：{path}")
            if expected is not None and (not path.is_file() or digest(self._raw(path)) != expected):
                raise VaultError("CONFLICT", f"文件已被其他操作修改：{path}")
            os.replace(temp, path)
            try:
                verified = self._raw(path) == data
            except OSError as exc:
                raise VaultError("COMMITTED_UNVERIFIED", f"文件已替换，但读回失败：{exc}", {"path": str(path), "committed": True}) from None
            if not verified:
                raise VaultError("COMMITTED_UNVERIFIED", f"文件已替换，但读回不匹配：{path}", {"path": str(path), "committed": True})
        finally:
            if temp.exists():
                self._guard(temp)
                temp.unlink()

    def _load_state(self):
        path = self._path("_state", "vault.json")
        if not path.is_file():
            raise VaultError("NOT_INITIALIZED", "该目录尚未初始化；读取不会自动建立记忆库。")
        raw = self._raw(path)
        try:
            state = json.loads(raw)
            if state.get("app") != "visible-memory" or state.get("format_version") != 1:
                raise ValueError("未知格式版本或应用标记")
            for key in ("views", "fingerprints", "requests"):
                if not isinstance(state[key], dict):
                    raise ValueError(key)
            identifier(state["vault_id"])
            for name, value in state["views"].items():
                if name not in VIEWS or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError("非法总览校验值")
            for entry_id, cache in state["fingerprints"].items():
                identifier(entry_id)
                if not isinstance(cache, dict) or not isinstance(cache.get("status"), str) or cache["status"] not in {"active", "archived"}:
                    raise ValueError("非法指纹记录")
                if any(not isinstance(cache.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", cache[k]) for k in ("sha256", "fingerprint")):
                    raise ValueError("非法指纹校验值")
            for receipt in state["requests"].values():
                if not isinstance(receipt, dict):
                    raise ValueError("非法请求记录")
                identifier(receipt.get("id"))
                if any(not isinstance(receipt.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[k]) for k in ("sha256", "signature")):
                    raise ValueError("非法请求校验值")
            if state.get("pending") is not None:
                pending = state["pending"]
                if not isinstance(pending, dict):
                    raise ValueError("非法待恢复操作")
                identifier(pending.get("id"))
                if any(not isinstance(pending.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", pending[k]) for k in ("sha256", "signature")):
                    raise ValueError("非法待恢复校验值")
                if pending.get("request_id") is not None and not isinstance(pending["request_id"], str):
                    raise ValueError("非法待恢复请求编号")
                previous = pending.get("previous_sha256")
                if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{64}", previous)):
                    raise ValueError("非法前一版本校验值")
                event_keys = ("operation", "request", "event_id", "event_at")
                if any(k in pending for k in event_keys):
                    if any(not isinstance(pending.get(k), str) or not pending[k] for k in event_keys):
                        raise ValueError("非法待恢复操作来源")
                    if pending["operation"] not in {"add", "update", "archive", "restore"}:
                        raise ValueError("非法待恢复操作类型")
                    identifier(pending["event_id"])
                    if dt.datetime.fromisoformat(pending["event_at"]).tzinfo is None:
                        raise ValueError("非法待恢复操作时间")
            if type(state.get("initializing", False)) is not bool:
                raise ValueError("非法初始化状态")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise VaultError("INVALID_STATE", f"状态文件损坏；未执行自动修复：{exc}") from None
        for name in ("entries", "history", "_state"):
            if not self._path(name).is_dir():
                raise VaultError("INVALID_STATE", f"缺少目录：{name}")
        return state, digest(raw)

    @contextlib.contextmanager
    def _lock(self):
        path = self._path("_state", "write.lock")
        try:
            stream = path.open("xb")
        except FileExistsError:
            raise VaultError("LOCKED", "记忆库存在写入锁。请检查持有进程；不会自动删除遗留锁。") from None
        try:
            with stream:
                stream.write(json_bytes({"pid": os.getpid(), "created_at": now()}))
                stream.flush()
            yield
        finally:
            self._guard(path)
            path.unlink(missing_ok=True)

    def _get(self, entry_id):
        path = self._path("entries", identifier(entry_id) + ".md")
        if not path.is_file():
            raise VaultError("NOT_FOUND", f"未找到指定条目：{entry_id}")
        raw = self._raw(path)
        try:
            entry = parse_entry(raw)
            if entry["id"] != entry_id:
                raise VaultError("INVALID_ENTRY", "文件名与条目 id 不一致。")
        except VaultError as exc:
            raise VaultError(exc.code, str(exc), {"path": str(path)}) from None
        return entry, raw, path

    def _scan(self):
        entries, errors = [], []
        for path in sorted(self._path("entries").iterdir()):
            try:
                self._guard(path)
                if path.suffix != ".md" or not path.is_file():
                    raise VaultError("UNKNOWN_FILE", "entries 中包含非条目文件；保留且不读取。")
                entry, raw, _ = self._get(path.stem)
                entries.append((entry, raw, path))
            except VaultError as exc:
                errors.append({"path": str(path), "code": exc.code, "message": str(exc)})
        return entries, errors

    def _result_entry(self, entry, raw, path):
        return {"entry": entry, "sha256": digest(raw), "path": str(path)}

    def _summary(self, entry, raw, path):
        return {"id": entry["id"], "title": entry["title"], "type": entry["type"],
                "status": entry["status"], "tags": entry["tags"], "scope": entry["scope"],
                "source": entry["source"], "updated_at": entry["updated_at"],
                "excerpt": entry["body"][:240], "sha256": digest(raw), "path": str(path),
                "needs_review": bool(entry.get("review_after") and entry["review_after"] <= dt.date.today().isoformat())}

    def _snapshot(self, entry, raw):
        folder = self._path("history", entry["id"])
        folder.mkdir(exist_ok=True)
        path = self._path("history", entry["id"], f"v{entry['revision']:06d}-{digest(raw)}.md")
        if path.exists():
            if self._raw(path) != raw:
                raise VaultError("HISTORY_CONFLICT", "历史版本内容不一致；拒绝覆盖。")
        else:
            self._atomic(path, raw, new=True)
        return str(path)

    def _event(self, command, request, entry=None, event_id=None, timestamp=None):
        folder = self._path("history", entry["id"] if entry else "maintenance")
        folder.mkdir(exist_ok=True)
        path = self._path(*folder.relative_to(self.root).parts, "event-" + (identifier(event_id) if event_id else str(uuid.uuid4())) + ".md")
        text = f"# 记忆操作记录\n\n- 时间：{timestamp or now()}\n- 操作：{command}\n"
        if entry:
            text += f"- 条目：{entry['id']}\n- 版本：{entry['revision']}\n"
        text += "\n## 用户请求（来源数据）\n\n" + "\n".join("> " + line for line in request.splitlines()) + "\n"
        data = text.encode("utf-8")
        if path.exists():
            if self._raw(path) != data:
                raise VaultError("HISTORY_CONFLICT", "操作记录已被修改，拒绝覆盖。")
        else:
            self._atomic(path, data, new=True)
        return str(path)

    def _view_documents(self, entries, timestamp=None):
        active = sorted((e for e, _, _ in entries if e["status"] == "active"), key=lambda e: (e["type"], e["title"], e["id"]))
        archived = sorted((e for e, _, _ in entries if e["status"] == "archived"), key=lambda e: e["updated_at"], reverse=True)
        stamp = f"> 生成时间：{timestamp or now()}。本页为生成的总览；请修改链接中的条目原文，再要求刷新总览。\n\n"
        def listing(items):
            if not items:
                return "目前没有条目。\n"
            return "\n".join(f"- [{md_text(e['title'])}](entries/{e['id']}.md) · {TYPES[e['type']]} · {md_text(e['updated_at'])}\n  - {md_text(e['body'][:160])}\n  - 范围：{md_text(e['scope'])}；标签：{md_text('、'.join(e['tags'])) or '无'}" for e in items) + "\n"
        intro = """# 可见记忆库

这里保存你明确要求保留的记忆。普通任务不会自动检索或加载本库。

## 浏览

- [用户偏好](USER.md)
- [事实、决策与经验](MEMORY.md)
- [已归档记忆](ARCHIVE.md)
- [条目原文](entries/)
- [历史版本与操作记录](history/)

## 使用方法

- 查看：在 Codex 中说“检索关于……的记忆”或“看看保存了哪些记忆”。
- 保存：说“记住：……”。临时聊天要求不会自动保存。
- 修改：提供条目编号或原文链接，说“把这条记忆改成……”。
- 删除：说“归档这条记忆”。归档停止普通召回，但文件和历史仍保留，可要求恢复。
- 历史：说“查看这条记忆的历史版本”。首版不提供永久擦除。
- 手工编辑：修改 entries 中对应 Markdown 的正文，保留 +++ 文件头；然后说“刷新记忆总览”。
- 整理：说“检查重复或过时的记忆”；检查本身不会修改任何内容。

每次检索只适用于该次明确请求，不开启后续自动检索。管理指定条目仅进行完成该操作必要的读取和校验。

entries 中的文件是正式原文。总览手改内容在刷新前会备份；历史不能还原从未观察或备份过的手工修改。
_state 只用于版本、内容指纹及写入协调。存储是本地明文，明确检索出的内容会进入当前 Codex 任务上下文。
本库与 Codex 原生记忆独立：初次不导入、不自动同步，本库归档不删除来源文件。

"""
        counts = f"## 当前内容\n\n有效：{len(active)} 条；归档：{len(archived)} 条。\n\n"
        return {"INDEX.md": intro + stamp + counts + listing(active),
                "USER.md": "# 用户偏好\n\n" + stamp + listing([e for e in active if e["type"] == "preference"]),
                "MEMORY.md": "# 事实、决策与经验\n\n" + stamp + listing([e for e in active if e["type"] in {"fact", "decision", "lesson"}]),
                "ARCHIVE.md": "# 已归档记忆\n\n归档条目不会参与普通检索；可按编号要求恢复。\n\n" + stamp + listing(archived)}

    def _rebuild(self, state, warnings):
        entries, errors = self._scan()
        if errors:
            raise VaultError("INVALID_ENTRIES", "部分条目无法解析，总览未重建。", errors)
        documents = self._view_documents(entries)
        for name, text in documents.items():
            path = self._path(name)
            previous = self._raw(path) if path.exists() else None
            known = state["views"].get(name)
            if previous is not None and known is None:
                raise VaultError("UNOWNED_FILE", f"未登记的同名文件，拒绝覆盖：{path}")
            if previous is not None and digest(previous) != known:
                folder = self._path("history", "views")
                folder.mkdir(exist_ok=True)
                backup = self._path("history", "views", str(uuid.uuid4()) + "-" + name)
                self._atomic(backup, previous, new=True)
                warnings.append({"code": "MANUAL_VIEW_BACKUP", "path": str(backup), "message": f"{name} 的手工修改已备份。"})
            data = text.encode("utf-8")
            self._atomic(path, data, expected=digest(previous) if previous is not None else None, new=previous is None)
            state["views"][name] = digest(data)
        state["fingerprints"] = {e["id"]: {"fingerprint": fingerprint(e), "sha256": digest(raw), "status": e["status"]} for e, raw, _ in entries}

    def _read(self, command, state, entry_id, query, include_archived, limit, offset):
        if command == "show":
            return self._result_entry(*self._get(entry_id))
        if command == "history":
            identifier(entry_id)
            folder = self._path("history", entry_id)
            versions, events = [], []
            if folder.is_dir():
                for path in sorted(folder.iterdir()):
                    self._guard(path)
                    if path.name.startswith("v") and path.suffix == ".md":
                        raw = self._raw(path)
                        entry = parse_entry(raw)
                        if entry["id"] != entry_id:
                            raise VaultError("INVALID_HISTORY", "历史条目编号不匹配。")
                        versions.append(self._result_entry(entry, raw, path))
                    elif path.name.startswith("event-") and path.suffix == ".md":
                        events.append({"path": str(path), "text": self._raw(path).decode("utf-8")})
            return {"versions": versions, "events": events}
        entries, errors = self._scan()
        if command in {"status", "check"}:
            stale = []
            for name in VIEWS:
                path = self._path(name)
                if not path.is_file() or digest(self._raw(path)) != state["views"].get(name):
                    stale.append(name)
            actual = {e["id"]: {"fingerprint": fingerprint(e), "sha256": digest(raw), "status": e["status"]} for e, raw, _ in entries}
            changed = actual != state["fingerprints"]
            out = {"active_count": sum(e["status"] == "active" for e, _, _ in entries),
                   "archived_count": sum(e["status"] == "archived" for e, _, _ in entries),
                   "errors": errors, "views_stale": bool(stale or changed or errors),
                   "changed_views": stale, "entries_changed": changed,
                   "pending_recovery": bool(state.get("pending")), "initializing": state.get("initializing", False)}
            if command == "check":
                groups = {}
                for e, _, _ in entries:
                    if e["status"] == "active":
                        groups.setdefault(fingerprint(e), []).append(e["id"])
                out["duplicates"] = [ids for ids in groups.values() if len(ids) > 1]
                out["review_due"] = [e["id"] for e, _, _ in entries if e["status"] == "active" and e.get("review_after", "9999-12-31") <= dt.date.today().isoformat()]
            return out
        if command == "search" and not query.strip():
            raise VaultError("INVALID_QUERY", "search 需要非空 query。")
        terms = normalized(query).split()
        found = []
        for entry, raw, path in entries:
            if entry["status"] != "active" and not include_archived:
                continue
            score = 0
            if command == "search":
                title = normalized(entry["title"])
                tags = normalized(" ".join(entry["tags"]))
                body = normalized(entry["body"] + " " + entry["scope"])
                for term in terms:
                    score += 5 * (term in title) + 3 * (term in tags) + (term in body)
                if not score:
                    continue
            found.append((score, entry["updated_at"], self._summary(entry, raw, path)))
        found.sort(key=lambda x: (x[0], x[1], x[2]["id"]), reverse=True)
        return {"entries": [item[2] for item in found[offset:offset + limit]], "total": len(found), "offset": offset, "errors": errors}

    def _prepare(self, command, payload, entry_id, expected_sha256):
        old = None
        if command == "add":
            entry = {"id": str(uuid.uuid4()), "status": "active", "created_at": now(), "updated_at": now(), "revision": 1, "tags": []}
            entry.update({k: v for k, v in payload.items() if k in FIELDS})
        else:
            if not expected_sha256:
                raise VaultError("EXPECTED_HASH_REQUIRED", "管理已有条目需要 show 返回的 expected_sha256。")
            previous, raw, path = self._get(entry_id)
            if digest(raw) != expected_sha256:
                raise VaultError("CONFLICT", "条目已发生变化，请读取指定条目的最新版本后再处理。")
            old = (previous, raw, path)
            entry = dict(previous)
            if command == "update":
                if not payload:
                    raise VaultError("INVALID_INPUT", "update 需要至少一个待更新字段。")
                entry.update({k: v for k, v in payload.items() if k in FIELDS})
            else:
                entry["status"] = "archived" if command == "archive" else "active"
            entry["revision"] += 1
            entry["updated_at"] = now()
        validate_entry(entry)
        return entry, old

    def _init(self, apply, request):
        self._guard(self.root)
        if self._path("_state", "vault.json").exists():
            state, _ = self._load_state()
            if not state.get("initializing"):
                return {"applied": False, "already_initialized": True, "root": str(self.root)}
            if not apply:
                return {"applied": False, "preview": True, "resume_initialization": True}
            return self._resume_init(request)
        if self.root.exists():
            if not self.root.is_dir():
                raise VaultError("NOT_EMPTY", "初始化需要目录。")
            # A failure before the marker is published can leave only these empty directories.
            for child in self.root.iterdir():
                self._guard(child)
                if child.name not in {"entries", "history", "_state"} or not child.is_dir() or any(child.iterdir()):
                    raise VaultError("NOT_EMPTY", "初始化只接受空目录或空的受管目录骨架；不会接管已有文件。")
        if not apply:
            return {"applied": False, "preview": True, "root": str(self.root), "files": list(VIEWS)}
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("_state", "entries", "history"):
            self._path(name).mkdir(exist_ok=True)
        state = {"app": "visible-memory", "format_version": 1, "vault_id": str(uuid.uuid4()), "created_at": now(), "views": {}, "fingerprints": {}, "requests": {}, "initializing": True}
        state["views"] = {name: digest(text.encode("utf-8")) for name, text in self._view_documents([], state["created_at"]).items()}
        with self._lock():
            self._atomic(self._path("_state", "vault.json"), json_bytes(state), new=True)
        return self._resume_init(request)

    def _resume_init(self, request):
        with self._lock():
            state, state_hash = self._load_state()
            if any(self._path("entries").iterdir()):
                raise VaultError("INIT_CONFLICT", "初始化尚未完成但 entries 已有内容；保留文件并停止。")
            for name, text in self._view_documents([], state["created_at"]).items():
                path = self._path(name)
                data = text.encode("utf-8")
                if digest(data) != state["views"].get(name):
                    raise VaultError("INIT_CONFLICT", "初始化模板与登记版本不符；保留现有文件。")
                if path.exists():
                    if digest(self._raw(path)) != state["views"][name]:
                        raise VaultError("INIT_CONFLICT", f"初始化文件已被修改：{path}")
                else:
                    self._atomic(path, data, new=True)
            self._event("init", request)
            state["initializing"] = False
            self._atomic(self._path("_state", "vault.json"), json_bytes(state), expected=state_hash)
        return {"applied": True, "root": str(self.root), "active_count": 0, "archived_count": 0, "warnings": []}

    def _recover_pending(self, state, warnings):
        pending = state.get("pending")
        if not pending:
            return
        path = self._path("entries", pending["id"] + ".md")
        if path.exists():
            entry, raw, _ = self._get(pending["id"])
            if digest(raw) == pending["sha256"]:
                self._snapshot(entry, raw)
                if all(k in pending for k in ("operation", "request", "event_id", "event_at")):
                    self._event(pending["operation"], pending["request"], entry, pending["event_id"], pending["event_at"])
                else:
                    warnings.append({"code": "HISTORY_INCOMPLETE", "message": "旧待恢复记录缺少原请求，无法补全原操作事件。"})
            elif digest(raw) != pending.get("previous_sha256"):
                warnings.append({"code": "HISTORY_UNCERTAIN", "id": entry["id"], "message": "正式原文在中断后又有变化，无法重建未观察到的中间版本或操作事件。"})
            if pending.get("request_id") and digest(raw) != pending.get("previous_sha256"):
                # Preserve the intended hash. A changed file makes a replay stop, not duplicate.
                state["requests"][pending["request_id"]] = {k: pending[k] for k in ("id", "sha256", "signature")}
        warnings.append({"code": "RECOVERED_PENDING", "id": pending["id"], "message": "已按现有正式原文恢复状态，未覆盖条目。"})
        state.pop("pending", None)

    def execute(self, command, request="", apply=False, payload=None, entry_id=None,
                expected_sha256=None, query="", include_archived=False, limit=8, offset=0):
        if not isinstance(request, str) or not request.strip():
            raise VaultError("EXPLICIT_REQUEST_REQUIRED", "必须提供本次明确的用户记忆操作请求；不会读取记忆库。")
        if not isinstance(command, str) or command not in READS | WRITES:
            raise VaultError("INVALID_COMMAND", "未知命令。")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise VaultError("INVALID_PAGINATION", "limit 必须为 1–100，offset 不得小于 0。")
        if not isinstance(query, str):
            raise VaultError("INVALID_QUERY", "query 必须是字符串。")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - FIELDS - {"request_id"}:
            raise VaultError("INVALID_INPUT", "输入必须是仅含支持字段的 JSON 对象。")
        if command not in {"add", "update"} and payload:
            raise VaultError("INVALID_INPUT", "该操作不接受 payload。")
        if command in READS and apply:
            raise VaultError("READ_ONLY", "只读操作不能使用 --apply。")
        base = {"ok": True, "operation": command, "applied": False}
        if command == "init":
            return {**base, **self._init(apply, request)}
        state, state_hash = self._load_state()
        if state.get("initializing") and command not in {"status", "check"}:
            raise VaultError("INITIALIZATION_INCOMPLETE", "初始化未完成；请明确重试 init，不会自动恢复。")
        if command in READS:
            return {**base, **self._read(command, state, entry_id, query, include_archived, limit, offset)}
        if not apply:
            if command == "rebuild":
                entries, errors = self._scan()
                return {**base, "preview": True, "entry_count": len(entries), "errors": errors, "files": list(VIEWS)}
            entry, _ = self._prepare(command, payload, entry_id, expected_sha256)
            return {**base, "preview": True, "entry": entry, "note": "预览中的新增编号尚未保留。"}
        with self._lock():
            state, state_hash = self._load_state()
            warnings = []
            if command == "rebuild":
                self._recover_pending(state, warnings)
                self._rebuild(state, warnings)
                self._event(command, request)
                self._atomic(self._path("_state", "vault.json"), json_bytes(state), expected=state_hash)
                return {**base, "applied": True, "views_stale": False, "warnings": warnings}
            if state.get("pending"):
                raise VaultError("RECOVERY_REQUIRED", "上次写入未完成收尾；请明确刷新总览恢复状态后再写入。", {"id": state["pending"]["id"]})
            request_id = payload.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
                raise VaultError("INVALID_INPUT", "request_id 必须是非空字符串。")
            signature = digest(json_bytes({"command": command, "id": entry_id, "payload": {k: v for k, v in payload.items() if k != "request_id"}}))
            if request_id in state["requests"]:
                receipt = state["requests"][request_id]
                if receipt["signature"] != signature:
                    raise VaultError("REQUEST_ID_REUSED", "同一 request_id 不能用于不同操作。")
                entry, raw, path = self._get(receipt["id"])
                if digest(raw) != receipt["sha256"]:
                    raise VaultError("REPLAY_CHANGED", "该请求已完成，但条目随后已改变；请使用新的请求。")
                return {**base, "replayed": True, **self._result_entry(entry, raw, path)}
            entry, old = self._prepare(command, payload, entry_id, expected_sha256)
            if command == "add":
                # Content integrity maintenance for this explicit save, never semantic recall.
                # Refresh fingerprints in memory, including hand-edited files, without returning others.
                current, errors = self._scan()
                if errors:
                    raise VaultError("INVALID_ENTRIES", "条目格式损坏，无法可靠校验重复内容；未新增。", errors)
                state["fingerprints"] = {e["id"]: {"fingerprint": fingerprint(e), "sha256": digest(r), "status": e["status"]} for e, r, _ in current}
                for candidate_id, cache in state["fingerprints"].items():
                    if cache["status"] == "active" and cache["fingerprint"] == fingerprint(entry):
                        candidate = self._get(candidate_id)
                        if digest(candidate[1]) != cache["sha256"]:
                            raise VaultError("STALE_FINGERPRINT", "指纹对应的条目已手改，请明确刷新总览后再保存。")
                        return {**base, "duplicate": True, **self._result_entry(*candidate)}
            if old and all(entry.get(k) == old[0].get(k) for k in FIELDS | {"status"}):
                return {**base, "unchanged": True, **self._result_entry(*old)}
            if old:
                self._snapshot(old[0], old[1])
            raw = render_entry(entry)
            path = self._path("entries", entry["id"] + ".md")
            pending = {"id": entry["id"], "sha256": digest(raw), "previous_sha256": digest(old[1]) if old else None,
                       "signature": signature, "request_id": request_id, "operation": command, "request": request,
                       "event_id": str(uuid.uuid4()), "event_at": now()}
            state["pending"] = pending
            self._atomic(self._path("_state", "vault.json"), json_bytes(state), expected=state_hash)
            state_hash = digest(json_bytes(state))
            try:
                self._atomic(path, raw, expected=expected_sha256 if old else None, new=old is None)
            except VaultError as exc:
                if exc.code == "COMMITTED_UNVERIFIED":
                    return {**base, "ok": False, "applied": True, "verification_failed": True,
                            "id": entry["id"], "path": str(path), "error": {"code": exc.code, "message": str(exc)}, "recovery_required": True}
                raise
            # Canonical commit has succeeded. Failures below must never be reported as no write.
            result = {**base, "applied": True, **self._result_entry(entry, raw, path), "warnings": warnings, "views_stale": False}
            history_complete = True
            try:
                result["history_path"] = self._snapshot(entry, raw)
                self._event(command, request, entry, pending["event_id"], pending["event_at"])
            except (OSError, VaultError) as exc:
                history_complete = False
                result["recovery_required"] = True
                warnings.append({"code": "HISTORY_INCOMPLETE", "message": str(exc)})
            state["fingerprints"][entry["id"]] = {"fingerprint": fingerprint(entry), "sha256": digest(raw), "status": entry["status"]}
            if request_id:
                state["requests"][request_id] = {"signature": signature, "id": entry["id"], "sha256": digest(raw)}
            if history_complete:
                state.pop("pending", None)
            try:
                self._rebuild(state, warnings)
            except (OSError, VaultError) as exc:
                result["views_stale"] = True
                warnings.append({"code": "VIEWS_STALE", "message": str(exc), "details": getattr(exc, "details", None)})
            try:
                self._atomic(self._path("_state", "vault.json"), json_bytes(state), expected=state_hash)
            except (OSError, VaultError) as exc:
                result["views_stale"] = True
                warnings.append({"code": "STATE_STALE", "message": str(exc)})
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Exact, explicitly configured local vault directory")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in sorted(READS | WRITES):
        item = sub.add_parser(command)
        item.add_argument("--request", required=True, help="Current explicit user request, not authorization inferred from related work")
        if command in WRITES:
            item.add_argument("--apply", action="store_true", help="Commit; otherwise preview without filesystem writes")
        if command in {"add", "update"}:
            item.add_argument("--input", required=True, help="UTF-8 JSON file, or - for stdin")
        if command in {"show", "history", "update", "archive", "restore"}:
            item.add_argument("--id", dest="entry_id", required=True)
        if command in {"update", "archive", "restore"}:
            item.add_argument("--expected-sha256", required=True)
        if command in {"list", "search"}:
            item.add_argument("--include-archived", action="store_true")
            item.add_argument("--limit", type=int, default=8)
            item.add_argument("--offset", type=int, default=0)
        if command == "search":
            item.add_argument("--query", required=True)
    args = vars(parser.parse_args(argv))
    root = args.pop("root")
    input_path = args.pop("input", None)
    try:
        if not args["request"].strip():
            raise VaultError("EXPLICIT_REQUEST_REQUIRED", "必须提供本次明确请求；未读取输入或记忆库。")
        if input_path:
            raw = sys.stdin.buffer.read() if input_path == "-" else Path(input_path).read_bytes()
            args["payload"] = json.loads(raw.decode("utf-8-sig"))
        result = Vault(root).execute(**args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except (VaultError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": {"code": getattr(exc, "code", "IO_OR_INPUT_ERROR"), "message": str(exc), "details": getattr(exc, "details", None)}}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
