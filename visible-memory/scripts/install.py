#!/usr/bin/env python3
"""Install visible-memory and its explicit-request routing block. Python stdlib only."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

START = b"<!-- visible-memory:start -->"
END = b"<!-- visible-memory:end -->"
FOLDERS = {"scripts", "references", "agents", "tests"}
EXCLUDED = {"__pycache__", ".pytest_cache", "scratch", ".scratch"}


class InstallError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def guarded(path):
    """Reject aliases before resolving; check every existing ancestor and leaf."""
    raw = Path(path)
    if ".." in raw.parts:
        raise InstallError("UNSAFE_PATH", f"拒绝包含父目录跳转的路径：{raw}")
    absolute = Path(os.path.abspath(raw))
    if absolute == Path(absolute.anchor):
        raise InstallError("UNSAFE_PATH", "不能将磁盘根目录作为安装目标或来源。")
    for candidate in reversed((absolute, *absolute.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InstallError("UNSAFE_LINK", f"拒绝符号链接或 reparse 路径：{candidate}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise InstallError("UNSAFE_LINK", f"拒绝硬链接文件：{candidate}")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise InstallError("UNSAFE_PATH", f"拒绝特殊文件：{candidate}")
    return absolute


def within(path, root):
    return path == root or root in path.parents


def content(path):
    path = guarded(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallError("NOT_A_FILE", f"需要普通文件：{path}")
    return path.read_bytes()


def source_files(source):
    required = {Path("SKILL.md"), Path("scripts/memory.py"), Path("agents/openai.yaml")}
    files = {}

    def visit(folder):
        for item in sorted(folder.iterdir()):
            # Omitted build/cache/scratch artifacts are never traversed or copied.
            if item.name in EXCLUDED or item.suffix in {".pyc", ".pyo", ".tmp"}:
                continue
            guarded(item)
            if item.is_dir():
                visit(item)
            else:
                files[item.relative_to(source)] = content(item)

    roots = {'SKILL.md', 'README.md', 'release.json'}
    for name in sorted(FOLDERS | roots):
        path = guarded(source / name)
        if not path.exists():
            continue
        if name in roots:
            files[Path(name)] = content(path)
        elif path.is_dir():
            visit(path)
        else:
            raise InstallError("INVALID_SOURCE", f"来源目录结构错误：{path}")
    if not required.issubset(files):
        raise InstallError("INVALID_SOURCE", "来源缺少 SKILL.md、scripts/memory.py 或 agents/openai.yaml。")
    return files


def target_preflight(target, files):
    expected_dirs = {parent for rel in files for parent in rel.parents if parent != Path(".")}
    if target.exists() and not target.is_dir():
        raise InstallError("INVALID_TARGET", "Skill 安装位置已经是文件。")

    def visit(folder):
        for item in sorted(folder.iterdir()):
            guarded(item)
            relative = item.relative_to(target)
            if item.is_dir():
                if relative not in expected_dirs:
                    raise InstallError("UNOWNED_TARGET", f"目标含未管理的目录，拒绝覆盖：{item}")
                visit(item)
            elif relative not in files or content(item) != files[relative]:
                raise InstallError("TARGET_CONFLICT", f"目标含不同或未管理的文件，拒绝覆盖：{item}")

    if target.exists():
        visit(target)
    return [relative for relative in files if not (target / relative).exists()]


def routing(original, target, vault_root):
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise InstallError("INVALID_MANAGED_BLOCK", "AGENTS.md 的 visible-memory 标记缺失、重复或不配对。")
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    lines = [
        START.decode(),
        "## 可见记忆管理（仅显式请求）",
        f"- 独立记忆库：`{vault_root}`。",
        f"- 仅用户本次明确要求检索、查看、检查、保存、更新、归档、恢复、导入或刷新本库时，读取 `{target / 'SKILL.md'}` 并按其流程执行。",
        "- 普通任务禁止读取本库，包括 INDEX.md、USER.md、MEMORY.md、ARCHIVE.md、entries、history、_state、_index 和 catalog。不得因启动会话、任务相关性、上下文压缩或此前授权而读取。",
        "- 一次明确检索只授权该次指定范围；不启用后续自动检索。管理操作仅允许读取完成该操作所需的指定条目、版本及校验信息，不顺带召回其他记忆。",
        "- 明确无歧义的管理要求可直接完成；对象或范围不清时询问缺失信息，不擅自全库检索。",
        "- 本库内容不嵌入全局指令；Codex 原生记忆保持现有设置，不自动导入、同步或修改。",
        END.decode(),
    ]
    block = newline.join(line.encode("utf-8") for line in lines)
    if START not in original:
        separator = b"" if not original or original.endswith(b"\n") else newline
        return original + separator + block + newline
    start = original.index(START)
    end = original.index(END) + len(END)
    if start >= end or (start and original[start - 1:start] != b"\n"):
        raise InstallError("INVALID_MANAGED_BLOCK", "AGENTS.md 的起始标记必须独占一行并位于结束标记之前。")
    if end < len(original) and original[end:end + 1] not in {b"\n", b"\r"}:
        raise InstallError("INVALID_MANAGED_BLOCK", "AGENTS.md 的结束标记必须独占一行。")
    return original[:start] + block + original[end:]


def atomic_write(path, data, expected):
    path = guarded(path)
    guarded(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = guarded(path.parent / (".visible-memory-" + str(uuid.uuid4()) + ".tmp"))
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        guarded(path)
        actual = content(path)
        if (sha256(actual) if actual is not None else None) != expected:
            raise InstallError("CONCURRENT_CHANGE", f"文件在预检后发生变化，拒绝覆盖：{path}")
        os.replace(temporary, path)
        if content(path) != data:
            raise InstallError("VERIFY_FAILED", f"写入后读回校验失败：{path}")
    finally:
        if temporary.exists():
            guarded(temporary)
            temporary.unlink()


def install(source: Path, target: Path, vault_root: Path, agents_file: Path,
            backup_dir: Path, apply=False, upgrade=False, request="", rollback=False):
    if upgrade or rollback:
        return upgrade_install(source, target, vault_root, agents_file, backup_dir,
                               apply=apply, request=request, rollback=rollback)
    result = {"ok": False, "applied": False, "created_files": [], "created_backups": [], "agents_changed": False}
    try:
        source, target, vault_root, agents_file, backup_dir = map(
            guarded, (source, target, vault_root, agents_file, backup_dir))
        result.update(source=str(source), target=str(target), vault_root=str(vault_root),
                      agents_file=str(agents_file), backup_path=None)
        if not source.is_dir():
            raise InstallError("INVALID_SOURCE", "Skill 来源必须是已存在的目录。")
        if agents_file.name != "AGENTS.md":
            raise InstallError("INVALID_AGENTS_PATH", "全局指引文件必须命名为 AGENTS.md。")
        for directory in (vault_root, backup_dir):
            if directory.exists() and not directory.is_dir():
                raise InstallError("NOT_A_DIRECTORY", f"需要目录：{directory}")
        if any(within(a, b) or within(b, a) for a, b in
               ((source, target), (source, vault_root), (target, vault_root),
                (source, backup_dir), (target, backup_dir), (vault_root, backup_dir))):
            raise InstallError("OVERLAPPING_PATHS", "来源、安装目标、记忆库和备份目录必须彼此独立。")
        if within(agents_file, source) or within(agents_file, target) or within(agents_file, vault_root):
            raise InstallError("OVERLAPPING_PATHS", "AGENTS.md 不能放在 Skill 来源、安装目标或记忆库中。")
        override = content(agents_file.with_name("AGENTS.override.md"))
        if override:
            raise InstallError("AGENTS_SHADOWED", "AGENTS.override.md 非空，会遮蔽全局路由；未安装。")
        files = source_files(source)
        missing = target_preflight(target, files)
        before = content(agents_file)
        after = routing(before or b"", target, vault_root)
        changed = before != after
        backup = None
        if changed and before is not None:
            backup = guarded(backup_dir / ("AGENTS-" + sha256(before) + ".md"))
            existing = content(backup)
            if existing is not None and existing != before:
                raise InstallError("BACKUP_CONFLICT", f"备份文件冲突：{backup}")
        result.update(ok=True, preview=not apply, files=[str(rel) for rel in files],
                      planned_new_files=[str(target / rel) for rel in missing],
                      planned_agents_change=changed, unchanged=not missing and not changed)
        if not apply:
            return result
        # Recheck all supplied paths and source hashes before the first mutation.
        for path in (source, target, vault_root, agents_file, backup_dir):
            guarded(path)
        if content(agents_file.with_name("AGENTS.override.md")):
            raise InstallError("AGENTS_SHADOWED", "AGENTS.override.md 在预检后变为非空，停止安装。")
        for relative, data in files.items():
            if content(source / relative) != data:
                raise InstallError("SOURCE_CHANGED", f"来源在预检后发生变化：{relative}")
        target_preflight(target, files)
        for relative in missing:
            destination = target / relative
            atomic_write(destination, files[relative], expected=None)
            result["created_files"].append(str(destination))
            result["applied"] = True
        if changed:
            if backup:
                if content(backup) is None:
                    atomic_write(backup, before, expected=None)
                    result["created_backups"].append(str(backup))
                    result["applied"] = True
                if content(backup) != before:
                    raise InstallError("BACKUP_CONFLICT", "备份在预检后发生变化，拒绝修改 AGENTS.md。")
                result["backup_path"] = str(backup)
            atomic_write(agents_file, after, expected=sha256(before) if before is not None else None)
            result["agents_changed"] = True
            result["applied"] = True
        return result
    except (InstallError, OSError, ValueError) as exc:
        result.update(ok=False, error={"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)})
        return result


VAULT_DIRS = {"entries", "history", "_state", "_index", "catalog"}
VAULT_VIEWS = {"INDEX.md", "USER.md", "MEMORY.md", "ARCHIVE.md"}


def json_data(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or path.drive or not path.parts or ".." in path.parts or any(":" in part for part in path.parts):
        raise InstallError("INVALID_MANIFEST", f"清单包含非法相对路径：{value}")
    return path


def file_manifest(root, vault=False, ignore_owned_lock=False):
    """Read only owned paths, rejecting every link before traversal."""
    output = {}
    if not root.exists():
        return output

    def visit(folder):
        guarded(folder)
        for item in sorted(folder.iterdir()):
            guarded(item)
            relative = item.relative_to(root)
            if vault and len(relative.parts) == 1 and item.name not in VAULT_DIRS | VAULT_VIEWS:
                continue
            if vault and relative.as_posix() == "_state/write.lock":
                if ignore_owned_lock:
                    continue
                raise InstallError("VAULT_LOCKED", "记忆库存在写入锁，不能进行升级或回退。")
            if item.is_dir():
                visit(item)
            else:
                output[relative.as_posix()] = sha256(content(item))
    visit(root)
    return output


@contextlib.contextmanager
def rollback_lock(vault_root):
    path = guarded(vault_root / "_state" / "write.lock")
    token = json_data({"operation": "rollback-upgrade", "id": str(uuid.uuid4()), "pid": os.getpid()})
    try:
        with path.open("xb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise InstallError("VAULT_LOCKED", "记忆库存在写入锁，不能回退。") from None
    try:
        yield
    finally:
        if content(path) == token:
            guarded(path).unlink()


def v1_manifest(source):
    path = guarded(source / "references" / "v1-release.json")
    raw = content(path)
    if raw is None:
        raise InstallError("MISSING_RELEASE_MANIFEST", "升级缺少已知 v1 文件清单。")
    value = json.loads(raw)
    if value.get("release") != "visible-memory-v1" or not isinstance(value.get("files"), dict):
        raise InstallError("INVALID_MANIFEST", "已知 v1 文件清单格式错误。")
    for relative, checksum in value["files"].items():
        safe_relative(relative)
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise InstallError("INVALID_MANIFEST", "已知 v1 文件校验值错误。")
    return value["files"]


def run_migration(source, vault_root, request, apply):
    command = [sys.executable, "-B", str(guarded(source / "scripts" / "memory.py")),
               "--root", str(vault_root), "migrate", "--request", request]
    if apply:
        command.append("--apply")
    completed = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        raise InstallError("MIGRATION_PROTOCOL_ERROR", "迁移程序未返回有效 JSON：" + completed.stderr[-1000:]) from None
    if completed.returncode and result.get("ok"):
        result["ok"] = False
    return result


def upgrade_install(source, target, vault_root, agents_file, backup_dir, apply=False,
                    request="", rollback=False):
    # Previews never create a lock, directory or journal.
    if not apply:
        return _upgrade_install(source, target, vault_root, agents_file, backup_dir, False, request, rollback)
    lock_path = None
    try:
        if not isinstance(request, str) or not request.strip():
            raise InstallError("EXPLICIT_REQUEST_REQUIRED", "升级或回退必须提供本次用户明确要求 --request。")
        base = guarded(backup_dir)
        destination = guarded(target)
        # Full preflight precedes creating the installer lock.
        preflight = _upgrade_install(source, target, vault_root, agents_file, backup_dir, False, request, rollback)
        if not preflight.get("ok") or preflight.get("unchanged"):
            return preflight
        base.mkdir(parents=True, exist_ok=True)
        candidate = guarded(base / (".upgrade-" + sha256(str(destination).encode("utf-8"))[:16] + ".lock"))
        try:
            with candidate.open("xb") as stream:
                stream.write(json_data({"pid": os.getpid(), "target": str(destination)}))
                stream.flush()
                os.fsync(stream.fileno())
            lock_path = candidate
        except FileExistsError:
            raise InstallError("UPGRADE_LOCKED", "升级锁已存在；不会自动删除遗留锁。") from None
        return _upgrade_install(source, target, vault_root, agents_file, backup_dir, True, request, rollback)
    except (InstallError, OSError, ValueError) as exc:
        return {"ok": False, "applied": False, "error": {"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)}}
    finally:
        if lock_path is not None:
            guarded(lock_path)
            lock_path.unlink()


def _upgrade_install(source, target, vault_root, agents_file, backup_dir, apply=False,
                     request="", rollback=False):
    result = {"ok": False, "applied": False, "preview": not apply, "upgrade": not rollback,
              "rollback": rollback, "changed_files": [], "created_backups": [], "agents_changed": False}
    try:
        if not isinstance(request, str) or not request.strip():
            raise InstallError("EXPLICIT_REQUEST_REQUIRED", "升级或回退必须提供本次用户明确要求 --request。")
        source, target, vault_root, agents_file, backup_dir = map(
            guarded, (source, target, vault_root, agents_file, backup_dir))
        for a, b in ((source, target), (source, vault_root), (target, vault_root),
                     (source, backup_dir), (target, backup_dir), (vault_root, backup_dir)):
            if within(a, b) or within(b, a):
                raise InstallError("OVERLAPPING_PATHS", "来源、安装目标、记忆库和备份目录必须彼此独立。")
        if agents_file.name != "AGENTS.md" or any(within(agents_file, x) for x in (source, target, vault_root)):
            raise InstallError("INVALID_AGENTS_PATH", "全局 AGENTS.md 路径无效。")
        if content(agents_file.with_name("AGENTS.override.md")):
            raise InstallError("AGENTS_SHADOWED", "AGENTS.override.md 非空，停止升级。")
        desired = source_files(source)
        desired_hashes = {p.as_posix(): sha256(raw) for p, raw in desired.items()}
        job = guarded(backup_dir / ("upgrade-" + sha256(str(target).encode("utf-8"))[:16]))
        journal_path = guarded(job / "upgrade.json")
        journal_raw = content(journal_path)
        journal = json.loads(journal_raw) if journal_raw else None
        result.update(backup_manifest=str(journal_path), target=str(target), vault_root=str(vault_root))
        if journal:
            if journal.get("target") != str(target) or journal.get("vault_root") != str(vault_root) or journal.get("agents_file") != str(agents_file):
                raise InstallError("JOURNAL_CONFLICT", "升级记录与本次路径不符。")
            for field in ("before_skill", "desired_skill", "before_vault"):
                if not isinstance(journal.get(field), dict):
                    raise InstallError("INVALID_MANIFEST", "升级记录缺少文件清单。")
                for relative in journal[field]:
                    safe_relative(relative)
            if rollback:
                if not apply:
                    return rollback_upgrade(journal, journal_path, job, target, vault_root, agents_file, False, result)
                with rollback_lock(vault_root):
                    return rollback_upgrade(journal, journal_path, job, target, vault_root, agents_file, True, result, locked=True)
            if journal.get("phase") == "rolled_back":
                raise InstallError("UPGRADE_ALREADY_ROLLED_BACK", "本次升级已回退；再次升级请使用新的备份目录。")
            if journal["desired_skill"] != desired_hashes:
                raise InstallError("SOURCE_CHANGED", "升级中途来源内容改变；请使用原升级包恢复。")
        elif rollback:
            raise InstallError("NO_UPGRADE_RECORD", "没有可回退的升级记录。")

        current_skill = file_manifest(target)
        agents_before = content(agents_file)
        agents_after = routing(agents_before or b"", target, vault_root)
        current_vault = file_manifest(vault_root, vault=True)
        if not journal:
            known = v1_manifest(source)
            if current_skill == desired_hashes:
                header = json.loads(content(vault_root / "_state" / "vault.json") or b"{}")
                if header.get("format_version") == 2 and agents_before == agents_after:
                    return {**result, "ok": True, "unchanged": True}
            if current_skill != known:
                bad = sorted(set(current_skill) ^ set(known) | {p for p in current_skill if p in known and current_skill[p] != known[p]})
                raise InstallError("TARGET_CONFLICT", "目标不是完整的已知 v1 版本；保留用户修改：" + ", ".join(bad))
            header = json.loads(content(vault_root / "_state" / "vault.json") or b"{}")
            if header.get("format_version") != 1:
                raise InstallError("UPGRADE_STATE_MISMATCH", "首次 v1 升级要求格式版本为 1 的记忆库。")
            journal = {"version": 1, "phase": "prepared", "target": str(target), "vault_root": str(vault_root),
                       "agents_file": str(agents_file), "request": request, "before_skill": current_skill,
                       "desired_skill": desired_hashes, "before_vault": current_vault,
                       "before_agents": sha256(agents_before) if agents_before is not None else None,
                       "after_agents": sha256(agents_after), "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        else:
            for relative, checksum in current_skill.items():
                if checksum not in {journal["before_skill"].get(relative), desired_hashes.get(relative)}:
                    raise InstallError("TARGET_CONFLICT", f"升级期间目标文件已被修改：{relative}")
            if not set(current_skill).issubset(set(journal["before_skill"]) | set(desired_hashes)):
                raise InstallError("TARGET_CONFLICT", "升级目标包含未知文件。")
            actual_agents = sha256(agents_before) if agents_before is not None else None
            if actual_agents not in {journal["before_agents"], journal["after_agents"]}:
                raise InstallError("CONCURRENT_CHANGE", "全局引导在升级期间已被修改。")
            if journal.get("phase") == "complete":
                if current_skill == desired_hashes and actual_agents == journal["after_agents"]:
                    return {**result, "ok": True, "unchanged": True}
                raise InstallError("TARGET_CONFLICT", "已完成升级的目标文件不完整。")
        result.update(planned_files=list(desired_hashes), phase=journal["phase"])
        if not apply:
            result["migration"] = run_migration(source, vault_root, request, False)
            result["ok"] = bool(result["migration"].get("ok"))
            return result

        def save_journal():
            journal.setdefault("stages", []).append({"phase": journal["phase"],
                "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
            previous = content(journal_path)
            atomic_write(journal_path, json_data(journal), sha256(previous) if previous is not None else None)
            result["applied"] = True
            result["phase"] = journal["phase"]

        def backup(relative, original, checksum):
            destination = guarded(job / safe_relative(relative))
            existing = content(destination)
            if existing is not None:
                if sha256(existing) != checksum:
                    raise InstallError("BACKUP_CONFLICT", f"升级备份已被修改：{destination}")
            else:
                if original is None or sha256(original) != checksum:
                    raise InstallError("BACKUP_SOURCE_CHANGED", f"备份来源不再是升级前的内容：{relative}")
                atomic_write(destination, original, None)
                result["created_backups"].append(str(destination))
                result["applied"] = True

        if journal_raw is None:
            save_journal()
        # Every backup is verified before the first migration or package replacement.
        for relative, checksum in journal["before_skill"].items():
            backup("skill/" + relative, content(target / safe_relative(relative)), checksum)
        for relative, checksum in journal["before_vault"].items():
            if relative.split("/")[0] == "_state" or relative in VAULT_VIEWS:
                backup("vault/" + relative, content(vault_root / safe_relative(relative)), checksum)
        if journal["before_agents"] is not None:
            backup("AGENTS.md", content(agents_file), journal["before_agents"])
        if journal["phase"] == "prepared":
            if file_manifest(vault_root, vault=True) != journal["before_vault"]:
                raise InstallError("CONCURRENT_CHANGE", "记忆库在备份前发生变化，停止升级。")
            journal["phase"] = "backed_up"
            save_journal()
        if journal["phase"] in {"backed_up", "migrating"}:
            journal["phase"] = "migrating"
            save_journal()
            migration = run_migration(source, vault_root, request, True)
            result["migration"] = migration
            result["applied"] = result["applied"] or migration.get("applied", False)
            if not migration.get("ok"):
                result.update(ok=False, recovery_required=True, error=migration.get("error", {"code": "MIGRATION_FAILED", "message": "迁移未完成，请使用同一升级包重试。"}))
                return result
            header = json.loads(content(vault_root / "_state" / "vault.json") or b"{}")
            if header.get("format_version") != 2:
                raise InstallError("MIGRATION_UNVERIFIED", "迁移未发布格式版本 2，未安装新入口。")
            baseline = file_manifest(vault_root, vault=True)
            if {p: h for p, h in baseline.items() if p.startswith("entries/")} != {
                    p: h for p, h in journal["before_vault"].items() if p.startswith("entries/")}:
                raise InstallError("MIGRATION_CHANGED_SOURCE", "迁移期间条目集合或原文发生变化，未发布新入口。")
            for relative, checksum in journal["before_vault"].items():
                if relative.split("/")[0] in {"entries", "history"} and baseline.get(relative) != checksum:
                    raise InstallError("MIGRATION_CHANGED_SOURCE", f"迁移改变既有原文或历史：{relative}")
            journal.update(phase="migrated", after_vault=baseline, migration=migration)
            save_journal()
        # Publish dependencies and documentation first; memory.py is the final package file.
        for relative in sorted(desired, key=lambda p: (p.as_posix() == "scripts/memory.py", p.as_posix())):
            destination = guarded(target / relative)
            actual = content(destination)
            if actual == desired[relative]:
                continue
            if content(source / relative) != desired[relative]:
                raise InstallError("SOURCE_CHANGED", f"升级来源发生变化：{relative}")
            expected = journal["before_skill"].get(relative.as_posix())
            if (sha256(actual) if actual is not None else None) != expected:
                raise InstallError("CONCURRENT_CHANGE", f"目标文件在发布前变化：{relative}")
            atomic_write(destination, desired[relative], expected)
            result["changed_files"].append(str(destination))
            result["applied"] = True
        actual = content(agents_file)
        if (sha256(actual) if actual is not None else None) != journal["after_agents"]:
            original = content(job / "AGENTS.md") if journal["before_agents"] is not None else b""
            expected_after = routing(original or b"", target, vault_root)
            atomic_write(agents_file, expected_after, journal["before_agents"])
            result["agents_changed"] = True
        journal["phase"] = "complete"
        save_journal()
        return {**result, "ok": True, "rollback_available": True}
    except (InstallError, OSError, ValueError) as exc:
        result.update(ok=False, error={"code": getattr(exc, "code", "IO_ERROR"), "message": str(exc)})
        if result["applied"]:
            result["recovery_required"] = True
        return result


def rollback_upgrade(journal, journal_path, job, target, vault_root, agents_file, apply, result, locked=False):
    if journal.get("phase") == "rolled_back":
        return {**result, "ok": True, "unchanged": True}
    if journal.get("phase") not in {"complete", "migrated", "rolling_back"} or not isinstance(journal.get("after_vault"), dict):
        raise InstallError("ROLLBACK_UNAVAILABLE", "升级尚未形成已校验的迁移基线，请先恢复升级。")
    current = file_manifest(vault_root, vault=True, ignore_owned_lock=locked)
    resuming = journal["phase"] == "rolling_back"
    def valid_rollback_manifest(value):
        if not resuming:
            return value == journal["after_vault"]
        if not set(value).issubset(journal["after_vault"]):
            return False
        for relative, checksum in journal["after_vault"].items():
            allowed = {checksum}
            if relative in journal["before_vault"]:
                allowed.add(journal["before_vault"][relative])
            elif relative.split("/")[0] in {"_state", "_index", "catalog"}:
                allowed.add(None)
            if value.get(relative) not in allowed:
                return False
        return True
    if not valid_rollback_manifest(current):
        raise InstallError("ROLLBACK_AFTER_WRITE", "迁移后记忆库已有写入、手改或其他变化；禁止恢复旧快照，请前向修复。")
    skill = file_manifest(target)
    if not set(journal["before_skill"]).issubset(skill):
        raise InstallError("TARGET_CONFLICT", "待回退的旧 Skill 文件缺失。")
    for relative, checksum in skill.items():
        if checksum not in {journal["before_skill"].get(relative), journal["desired_skill"].get(relative)}:
            raise InstallError("TARGET_CONFLICT", "升级后 Skill 已被修改，不能自动回退。")
    actual_agents = content(agents_file)
    if (sha256(actual_agents) if actual_agents is not None else None) not in {journal["before_agents"], journal["after_agents"]}:
        raise InstallError("CONCURRENT_CHANGE", "升级后全局引导已被修改，不能自动回退。")
    restores = []
    for relative, checksum in journal["before_skill"].items():
        restores.append((job / "skill" / safe_relative(relative), target / safe_relative(relative), checksum))
    for relative, checksum in journal["before_vault"].items():
        if relative.split("/")[0] == "_state" or relative in VAULT_VIEWS:
            restores.append((job / "vault" / safe_relative(relative), vault_root / safe_relative(relative), checksum))
    if journal["before_agents"] is not None:
        restores.append((job / "AGENTS.md", agents_file, journal["before_agents"]))
    for backup, _, checksum in restores:
        if sha256(content(backup) or b"") != checksum or content(backup) is None:
            raise InstallError("BACKUP_CONFLICT", f"回退备份缺失或改变：{backup}")
    result.update(ok=True, rollback_available=True, retained_history=True)
    if not apply:
        return result
    # Recheck immediately before mutation. No recursive deletion is used.
    if not valid_rollback_manifest(file_manifest(vault_root, vault=True, ignore_owned_lock=locked)):
        raise InstallError("CONCURRENT_CHANGE", "回退预检后记忆库发生变化。")
    journal["phase"] = "rolling_back"
    journal.setdefault("stages", []).append({"phase": "rolling_back", "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    previous = content(journal_path)
    atomic_write(journal_path, json_data(journal), sha256(previous))
    result["applied"] = True
    removals = [(target / safe_relative(p), h) for p, h in skill.items() if p not in journal["before_skill"]]
    removals += [(vault_root / safe_relative(p), h) for p, h in current.items()
                 if p not in journal["before_vault"] and p.split("/")[0] in {"_state", "_index", "catalog"}]
    for backup, destination, checksum in restores:
        actual = content(destination)
        replacement = content(backup)
        if actual != replacement:
            if within(destination, target):
                expected = skill.get(destination.relative_to(target).as_posix())
            elif within(destination, vault_root):
                expected = current.get(destination.relative_to(vault_root).as_posix())
            else:
                expected = sha256(actual_agents) if actual_agents is not None else None
            atomic_write(destination, replacement, expected)
            result["applied"] = True
            result["changed_files"].append(str(destination))
    if journal["before_agents"] is None and content(agents_file) is not None:
        removals.append((agents_file, journal["after_agents"]))
    for destination, checksum in removals:
        guarded(destination)
        if sha256(content(destination) or b"") != checksum:
            raise InstallError("CONCURRENT_CHANGE", f"回退待移除文件变化：{destination}")
        destination.unlink()
        result["applied"] = True
        result["changed_files"].append(str(destination))
    journal["phase"] = "rolled_back"
    journal.setdefault("stages", []).append({"phase": "rolled_back", "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    previous = content(journal_path)
    atomic_write(journal_path, json_data(journal), sha256(previous))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "target", "vault-root", "agents-file", "backup-dir"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--apply", action="store_true", help="Commit; default is read-only preview")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--upgrade", action="store_true", help="Upgrade a verified v1 installation")
    modes.add_argument("--rollback", action="store_true", help="Restore v1 only if the migrated vault is unchanged")
    modes.add_argument('--upgrade-code', action='store_true', help='Upgrade code and guidance only; never inspect a vault')
    modes.add_argument('--recover-code', metavar='OPERATION_ID', help='Explicitly restore an interrupted code upgrade')
    parser.add_argument('--preview-token')
    parser.add_argument("--request", required=True, help="Current explicit user install, upgrade or recovery request")
    args = vars(parser.parse_args(argv))
    try:
        if args['upgrade'] or args['rollback']:
            if any(args[name] is None for name in ('source', 'target', 'vault_root', 'agents_file', 'backup_dir')):
                raise InstallError('MISSING_ARGUMENT', '旧格式升级和回退仍需 source、target、vault-root、agents-file、backup-dir。')
            for name in ('upgrade_code', 'recover_code', 'preview_token'):
                args.pop(name)
            result = install(**args)
        else:
            from vm_install_code import install_code, recover_code
            from vm_config import default_config
            if args['vault_root'] is not None:
                raise InstallError('INVALID_ARGUMENT', '--vault-root 仅用于显式 v1 格式升级或回退。请用 settings.py 配置当前目录。')
            operation_id = args.pop('recover_code')
            if operation_id:
                result = recover_code(args['backup_dir'] or default_config().parent / 'backups', operation_id,
                                      request=args['request'], apply=args['apply'])
            else:
                for name in ('upgrade', 'rollback', 'vault_root'):
                    args.pop(name)
                result = install_code(**args)
    except (InstallError, OSError, ValueError) as exc:
        result = {'ok': False, 'applied': False, 'error': {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}}
    except Exception as exc:
        # Configuration errors use the shared VaultError type, imported lazily.
        from memory_v1 import VaultError
        if not isinstance(exc, VaultError):
            raise
        result = {'ok': False, 'applied': False, 'error': {'code': exc.code, 'message': str(exc), 'details': exc.details}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
