"""Behavioral tests for the visible-memory vault; all data stays in scratch vaults.

Run with Python 3.11+: python -B -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


SKILL_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("visible_memory_v1_oracle", SKILL_ROOT / "scripts" / "memory_v1.py")
assert SPEC is not None and SPEC.loader is not None
memory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = memory
SPEC.loader.exec_module(memory)


def snapshot(root: Path) -> dict[str, bytes]:
    """Capture files, including private state, to detect incidental writes."""
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


class VaultTests(unittest.TestCase):
    def setUp(self):
        # Keep test assets inside staging, never the real Desktop or native store.
        self.temp = tempfile.TemporaryDirectory(prefix="虚构记忆 tests ", dir=SKILL_ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Agents 测试"
        self.vault = memory.Vault(self.root)

    def call(self, command, **kwargs):
        kwargs.setdefault("request", "测试用户明确要求：" + command)
        return self.vault.execute(command, **kwargs)

    def initialize(self):
        return self.call("init", apply=True)

    def payload(self, **overrides):
        data = {
            "title": "写作偏好：中文说明",
            "type": "preference",
            "tags": ["沟通", "测试"],
            "source": "虚构用户明确要求保存；测试来源 https://example.invalid/source",
            "scope": "仅限测试项目，不能推广为全局约定",
            "body": "希望使用中文。\n\n保留 `代码`、$()、引号 \" 和路径 C:\\测试\\文件。\n",
        }
        data.update(overrides)
        return data

    def add(self, **overrides):
        return self.call("add", apply=True, payload=self.payload(**overrides))

    def create(self, **overrides):
        self.initialize()
        return self.add(**overrides)

    def show(self, entry_id):
        return self.call("show", entry_id=entry_id)

    def entry_path(self, entry_id):
        return self.root / "entries" / (entry_id + ".md")

    def ids(self, result):
        return [entry["id"] for entry in result["entries"]]

    def assert_vault_error(self, operation):
        with self.assertRaises(memory.VaultError) as caught:
            operation()
        self.assertTrue(caught.exception.code)
        return caught.exception

    def make_junction(self, link, target):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        env = os.environ.copy()
        env.update(VM_TEST_LINK=str(link), VM_TEST_TARGET=str(target))
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path $env:VM_TEST_LINK -Target $env:VM_TEST_TARGET | Out-Null"],
            env=env, capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            self.fail("Cannot create Windows test junction: " + result.stderr.decode("utf-8", errors="replace"))
        # Remove only the junction itself before TemporaryDirectory cleanup.
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)

    def test_empty_request_rejected_before_filesystem_access(self):
        for command in ("status", "search", "show", "init", "add", "update", "archive", "restore", "rebuild"):
            with self.subTest(command=command):
                with mock.patch("pathlib.Path.open") as opened, \
                     mock.patch("pathlib.Path.stat") as stated, \
                     mock.patch("pathlib.Path.iterdir") as listed, \
                     mock.patch("os.open") as os_opened:
                    self.assert_vault_error(lambda: self.vault.execute(command, request="  \n\t", apply=True))
                    opened.assert_not_called()
                    stated.assert_not_called()
                    listed.assert_not_called()
                    os_opened.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_uninitialized_reads_do_not_create_store(self):
        for command in ("status", "list", "search", "check"):
            with self.subTest(command=command):
                try:
                    self.call(command, query="测试")
                except memory.VaultError:
                    pass
                self.assertFalse(self.root.exists())

    def test_init_preview_does_not_create_store(self):
        self.call("init")
        self.assertFalse(self.root.exists())

    def test_initialization_has_no_personal_entries(self):
        self.initialize()
        self.assertEqual(list((self.root / "entries").glob("*.md")), [])
        for name in ("INDEX.md", "USER.md", "MEMORY.md", "ARCHIVE.md"):
            self.assertTrue((self.root / name).is_file(), name)
        status = self.call("status")
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["archived_count"], 0)

    def test_repeat_initialization_preserves_store(self):
        self.create()
        before = snapshot(self.root)
        self.initialize()
        self.assertEqual(snapshot(self.root), before)

    def test_initialization_does_not_overwrite_unowned_file(self):
        self.root.mkdir()
        index = self.root / "INDEX.md"
        index.write_text("现有手工文件，不应覆盖。", encoding="utf-8")
        before = snapshot(self.root)
        self.assert_vault_error(self.initialize)
        self.assertEqual(snapshot(self.root), before)

    def test_add_roundtrips_unicode_special_characters_and_source(self):
        result = self.create(review_after="2035-01-02")
        entry_id = result["entry"]["id"]
        self.assertEqual(str(uuid.UUID(entry_id)), entry_id)
        shown = self.show(entry_id)
        for key, value in self.payload(review_after="2035-01-02").items():
            self.assertEqual(shown["entry"][key], value, key)
        self.assertEqual(shown["sha256"], hashlib.sha256(self.entry_path(entry_id).read_bytes()).hexdigest())
        raw = self.entry_path(entry_id).read_text(encoding="utf-8")
        self.assertTrue(raw.startswith("+++\n"))
        self.assertEqual(shown["entry"]["status"], "active")

    def test_read_commands_leave_all_file_bytes_unchanged(self):
        result = self.create()
        entry_id = result["entry"]["id"]
        before = snapshot(self.root)
        for command, kwargs in (
            ("status", {}), ("list", {}), ("search", {"query": "沟通"}),
            ("show", {"entry_id": entry_id}),
            ("history", {"entry_id": entry_id}), ("check", {}),
        ):
            with self.subTest(command=command):
                self.call(command, **kwargs)
                self.assertEqual(snapshot(self.root), before)

    def test_write_previews_leave_store_unchanged(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        sha = self.show(entry_id)["sha256"]
        before = snapshot(self.root)
        for command, kwargs in (
            ("add", {"payload": self.payload(title="新条目", body="另外的内容")}),
            ("update", {"entry_id": entry_id, "expected_sha256": sha, "payload": {"body": "修改预览"}}),
            ("archive", {"entry_id": entry_id, "expected_sha256": sha}),
            ("rebuild", {}),
        ):
            with self.subTest(command=command):
                self.call(command, **kwargs)
                self.assertEqual(snapshot(self.root), before)

    def test_exact_duplicate_add_does_not_make_new_entry(self):
        first = self.create()
        second = self.add()
        self.assertEqual(first["entry"]["id"], second["entry"]["id"])
        self.assertEqual(len(list((self.root / "entries").glob("*.md"))), 1)

    def test_request_id_retry_is_idempotent(self):
        request_id = "a625cb99-bb4f-49db-85fd-41ddc088e3f6"
        first = self.create(request_id=request_id)
        second = self.add(request_id=request_id)
        self.assertEqual(first["entry"]["id"], second["entry"]["id"])
        self.assertEqual(self.call("status")["active_count"], 1)

    def test_stale_fingerprint_cannot_silently_claim_different_content_saved(self):
        original = self.create()
        entry_id = original["entry"]["id"]
        path = self.entry_path(entry_id)
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("希望使用中文。", "手工改为另一种内容。"), encoding="utf-8")
        try:
            retried = self.add()
        except memory.VaultError:
            # A clear stale-index conflict is also a valid safe outcome.
            self.assertIn("手工改为另一种内容。", path.read_text(encoding="utf-8"))
        else:
            shown = self.show(retried["entry"]["id"])
            self.assertEqual(shown["entry"]["body"], self.payload()["body"])

    def test_update_preserves_id_source_scope_and_previous_version(self):
        first = self.create()
        entry_id = first["entry"]["id"]
        original = self.entry_path(entry_id).read_bytes()
        updated = self.call("update", apply=True, entry_id=entry_id,
                            expected_sha256=self.show(entry_id)["sha256"],
                            payload={"body": "更新后的中文偏好。", "title": "更新标题"})
        shown = self.show(entry_id)
        self.assertEqual(updated["entry"]["id"], entry_id)
        self.assertEqual(shown["entry"]["body"], "更新后的中文偏好。")
        self.assertEqual(shown["entry"]["source"], self.payload()["source"])
        self.assertEqual(shown["entry"]["scope"], self.payload()["scope"])
        self.assertTrue(self.call("history", entry_id=entry_id)["versions"])
        self.assertIn(original, [p.read_bytes() for p in (self.root / "history").rglob("*.md")])

    def test_update_without_expected_checksum_fails_without_change(self):
        created = self.create()
        before = snapshot(self.root)
        self.assert_vault_error(lambda: self.call("update", apply=True,
            entry_id=created["entry"]["id"], payload={"body": "拒绝丢失更新"}))
        self.assertEqual(snapshot(self.root), before)

    def test_stale_checksum_conflict_preserves_newer_content(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        old_sha = self.show(entry_id)["sha256"]
        self.call("update", apply=True, entry_id=entry_id, expected_sha256=old_sha,
                  payload={"body": "另一任务已经更新"})
        before = snapshot(self.root)
        self.assert_vault_error(lambda: self.call("update", apply=True, entry_id=entry_id,
            expected_sha256=old_sha, payload={"body": "不应覆盖"}))
        self.assertEqual(snapshot(self.root), before)

    def test_manual_edit_is_immediately_searchable_without_rebuild(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        path = self.entry_path(entry_id)
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write("\n手工添加独有词：蓝鲸观测站\n")
        before = snapshot(self.root)
        results = self.call("search", query="蓝鲸观测站")
        self.assertIn(entry_id, self.ids(results))
        self.assertIn("蓝鲸观测站", self.show(entry_id)["entry"]["body"])
        self.assertEqual(snapshot(self.root), before)

    def test_archive_excluded_by_default_and_restore_retains_identity(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        self.call("archive", apply=True, entry_id=entry_id,
                  expected_sha256=self.show(entry_id)["sha256"])
        self.assertNotIn(entry_id, self.ids(self.call("list")))
        self.assertNotIn(entry_id, self.ids(self.call("search", query="中文")))
        self.assertIn(entry_id, self.ids(self.call("list", include_archived=True)))
        self.assertIn(entry_id, self.ids(self.call("search", query="中文", include_archived=True)))
        archived = self.show(entry_id)
        self.assertEqual(archived["entry"]["status"], "archived")
        self.assertEqual(self.call("status")["archived_count"], 1)
        self.assertTrue(self.entry_path(entry_id).exists())
        self.call("restore", apply=True, entry_id=entry_id, expected_sha256=archived["sha256"])
        self.assertIn(entry_id, self.ids(self.call("list")))
        self.assertEqual(self.show(entry_id)["entry"]["source"], self.payload()["source"])
        self.assertEqual(self.call("status")["archived_count"], 0)

    def test_restore_preview_does_not_write(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        self.call("archive", apply=True, entry_id=entry_id,
                  expected_sha256=self.show(entry_id)["sha256"])
        before = snapshot(self.root)
        self.call("restore", entry_id=entry_id, expected_sha256=self.show(entry_id)["sha256"])
        self.assertEqual(snapshot(self.root), before)

    def test_corrupt_entry_is_reported_and_never_repaired_on_read(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        self.entry_path(entry_id).write_text("+++\nthis is not TOML\n+++\n保留损坏原文", encoding="utf-8")
        before = snapshot(self.root)
        self.assert_vault_error(lambda: self.show(entry_id))
        for command in ("status", "list", "search", "check"):
            result = self.call(command, query="保留")
            self.assertIn(entry_id, json.dumps(result, ensure_ascii=False))
        self.assertEqual(snapshot(self.root), before)

    def test_rebuild_preserves_manually_edited_view_in_history(self):
        self.create()
        view = self.root / "USER.md"
        edited = view.read_bytes() + "\n人工误改总览：需要保留备份。\n".encode("utf-8")
        view.write_bytes(edited)
        self.call("rebuild", apply=True)
        snapshots = [p.read_bytes() for p in (self.root / "history").rglob("*.md")]
        self.assertIn(edited, snapshots)
        self.assertNotEqual(view.read_bytes(), edited)

    def test_add_preserves_import_source_file(self):
        self.initialize()
        source_path = Path(self.temp.name) / "导入来源.md"
        source_path.write_text("测试来源内容：明确选中的笔记。", encoding="utf-8")
        original = source_path.read_bytes()
        added = self.add(type="note", source=str(source_path), body=source_path.read_text(encoding="utf-8"))
        self.assertEqual(source_path.read_bytes(), original)
        self.assertEqual(added["entry"]["source"], str(source_path))

    def test_invalid_payloads_fail_without_mutating(self):
        self.initialize()
        base = self.payload()
        cases = [{**base, "type": "invented"}, {**base, "title": ""},
                 {**base, "tags": "not a list"}, {**base, "source": ""},
                 {**base, "body": ""}, {**base, "review_after": "2030-02-30"}]
        for payload in cases:
            with self.subTest(payload=payload):
                before = snapshot(self.root)
                self.assert_vault_error(lambda: self.call("add", apply=True, payload=payload))
                self.assertEqual(snapshot(self.root), before)

    def test_unsafe_entry_identifiers_fail_without_mutating(self):
        self.create()
        for unsafe in ("../outside", "..\\outside", "C:\\Windows\\file", "/etc/passwd", "not-a-uuid"):
            with self.subTest(identifier=unsafe):
                before = snapshot(self.root)
                self.assert_vault_error(lambda: self.show(unsafe))
                self.assertEqual(snapshot(self.root), before)

    def test_failed_atomic_replace_preserves_original_entry(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        target = self.entry_path(entry_id)
        original = target.read_bytes()
        sha = self.show(entry_id)["sha256"]
        real_replace = os.replace

        def fail_only_target(source, destination, *args, **kwargs):
            if Path(destination) == target:
                raise OSError("simulated entry replacement failure")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(memory.os, "replace", side_effect=fail_only_target):
            with self.assertRaises((memory.VaultError, OSError)):
                self.call("update", apply=True, entry_id=entry_id,
                          expected_sha256=sha, payload={"body": "不完整写入"})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.show(entry_id)["entry"]["body"], self.payload()["body"])
        self.assertFalse((self.root / "_state" / "write.lock").exists())

    def test_existing_write_lock_blocks_mutation_without_removing_lock(self):
        self.initialize()
        lock = self.root / "_state" / "write.lock"
        lock.write_text("由另一个任务持有", encoding="utf-8")
        before = snapshot(self.root)
        self.assert_vault_error(lambda: self.add())
        self.assertEqual(snapshot(self.root), before)

    def test_symlink_entry_cannot_escape_vault(self):
        self.initialize()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("不应读取或修改", encoding="utf-8")
        entry_id = str(uuid.uuid4())
        link = self.entry_path(entry_id)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest("OS disallows symlink creation: " + str(error))
        before = outside.read_bytes()
        self.assert_vault_error(lambda: self.show(entry_id))
        self.assertEqual(outside.read_bytes(), before)

    def test_root_symlink_is_rejected(self):
        actual = Path(self.temp.name) / "external vault"
        actual.mkdir()
        try:
            self.root.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("OS disallows symlink creation: " + str(error))
        try:
            linked_vault = memory.Vault(self.root)
        except memory.VaultError:
            pass
        else:
            self.assert_vault_error(lambda: linked_vault.execute("init", request="明确初始化", apply=True))
        self.assertEqual(list(actual.iterdir()), [])

    def test_windows_root_junction_is_rejected(self):
        actual = Path(self.temp.name) / "junction target"
        actual.mkdir()
        self.make_junction(self.root, actual)
        try:
            linked_vault = memory.Vault(self.root)
        except memory.VaultError:
            pass
        else:
            self.assert_vault_error(lambda: linked_vault.execute("init", request="明确初始化", apply=True))
        self.assertEqual(list(actual.iterdir()), [])

    def test_windows_entries_junction_cannot_escape_vault(self):
        self.initialize()
        entries = self.root / "entries"
        entries.rmdir()  # Empty and owned by this isolated test vault.
        outside = Path(self.temp.name) / "outside entries"
        outside.mkdir()
        entry_id = str(uuid.uuid4())
        victim = outside / (entry_id + ".md")
        victim.write_text("不应读取或修改此来源", encoding="utf-8")
        self.make_junction(entries, outside)
        before = snapshot(outside)
        self.assert_vault_error(lambda: self.show(entry_id))
        self.assertEqual(snapshot(outside), before)

    def test_pagination_and_search_limits(self):
        self.initialize()
        for index in range(10):
            self.add(title=f"检索上限 {index}", body=f"共同检索词，独立内容 {index}")
        page = self.call("search", query="共同检索词")
        self.assertEqual(len(page["entries"]), 8)
        first = self.ids(self.call("list", limit=3, offset=0))
        second = self.ids(self.call("list", limit=3, offset=3))
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertFalse(set(first) & set(second))

    def test_interrupted_initialization_resumes_and_preserves_unrelated_file(self):
        real_replace = os.replace

        def fail_second_view(source, destination, *args, **kwargs):
            if Path(destination) == self.root / "USER.md":
                raise OSError("simulated initialization interruption")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(memory.os, "replace", side_effect=fail_second_view):
            with self.assertRaises((memory.VaultError, OSError)):
                self.initialize()
        self.assertTrue((self.root / "INDEX.md").is_file())
        index_before = (self.root / "INDEX.md").read_bytes()
        unrelated = self.root / "用户自己的文件.md"
        unrelated.write_text("初始化重试不得覆盖我的内容。", encoding="utf-8")
        unrelated_before = unrelated.read_bytes()
        result = self.initialize()
        self.assertTrue(result["applied"])
        self.assertEqual((self.root / "INDEX.md").read_bytes(), index_before)
        self.assertEqual(unrelated.read_bytes(), unrelated_before)
        self.assertFalse(self.call("status")["initializing"])
        self.assertEqual(self.call("status")["active_count"], 0)

    def test_interrupted_initialization_does_not_overwrite_modified_view(self):
        real_replace = os.replace

        def fail_second_view(source, destination, *args, **kwargs):
            if Path(destination) == self.root / "USER.md":
                raise OSError("simulated initialization interruption")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(memory.os, "replace", side_effect=fail_second_view):
            with self.assertRaises((memory.VaultError, OSError)):
                self.initialize()
        index = self.root / "INDEX.md"
        index.write_text("在失败后手工修改，不能覆盖。", encoding="utf-8")
        before = snapshot(self.root)
        self.assert_vault_error(self.initialize)
        self.assertEqual(snapshot(self.root), before)

    def test_final_state_failure_requires_rebuild_then_request_replay_is_safe(self):
        self.initialize()
        real_replace = os.replace
        state_writes = 0
        request_id = "9443a3f6-88a4-4f07-9605-fad7de980b26"

        def fail_final_state(source, destination, *args, **kwargs):
            nonlocal state_writes
            if Path(destination) == self.root / "_state" / "vault.json":
                state_writes += 1
                if state_writes == 2:
                    raise OSError("simulated final state commit failure")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(memory.os, "replace", side_effect=fail_final_state):
            result = self.add(request_id=request_id)
        self.assertEqual(state_writes, 2)
        self.assertTrue(result["applied"])
        self.assertIn("STATE_STALE", [warning["code"] for warning in result["warnings"]])
        entry_id = result["entry"]["id"]
        self.assertEqual(self.show(entry_id)["entry"]["body"], self.payload()["body"])
        self.assertTrue(self.call("status")["pending_recovery"])
        before_retry = snapshot(self.root)
        error = self.assert_vault_error(lambda: self.add(request_id=request_id))
        self.assertEqual(error.code, "RECOVERY_REQUIRED")
        self.assertEqual(snapshot(self.root), before_retry)
        self.call("rebuild", apply=True)
        self.assertFalse(self.call("status")["pending_recovery"])
        replay = self.add(request_id=request_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entry"]["id"], entry_id)
        self.assertEqual(self.call("status")["active_count"], 1)

    def test_list_valued_type_and_status_report_errors_without_typeerror(self):
        self.initialize()
        for field in ("type", "status"):
            with self.subTest(field=field):
                created = self.add(title=f"字段类型测试 {field}", body=f"虚构正文 {field}")
                entry_id = created["entry"]["id"]
                path = self.entry_path(entry_id)
                original = path.read_text(encoding="utf-8")
                old = 'type = "preference"' if field == "type" else 'status = "active"'
                self.assertIn(old, original)
                path.write_text(original.replace(old, field + ' = ["invalid"]', 1), encoding="utf-8")
                before = snapshot(self.root)
                self.assert_vault_error(lambda: self.show(entry_id))
                for command in ("status", "list", "search", "check"):
                    result = self.call(command, query="虚构")
                    self.assertIn(entry_id, json.dumps(result, ensure_ascii=False))
                self.assertEqual(snapshot(self.root), before)
                # Restore this fixture so the next subcase is independently valid.
                path.write_text(original, encoding="utf-8")
        self.assert_vault_error(lambda: self.add(type=["preference"]))

    def test_readback_failure_reports_committed_unverified_without_success(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        target = self.entry_path(entry_id)
        expected_sha = self.show(entry_id)["sha256"]
        real_replace = os.replace
        real_raw = self.vault._raw
        entry_replaced = False

        def replace_then_mark(source, destination, *args, **kwargs):
            nonlocal entry_replaced
            result = real_replace(source, destination, *args, **kwargs)
            if Path(destination) == target:
                entry_replaced = True
            return result

        def fail_readback_only_after_commit(path):
            if Path(path) == target and entry_replaced:
                raise OSError("simulated postcommit readback failure")
            return real_raw(path)

        with mock.patch.object(memory.os, "replace", side_effect=replace_then_mark), \
             mock.patch.object(self.vault, "_raw", side_effect=fail_readback_only_after_commit):
            result = self.call("update", apply=True, entry_id=entry_id,
                expected_sha256=expected_sha, payload={"body": "已经写入但当时无法验证。"})
        self.assertTrue(entry_replaced)
        self.assertTrue(result["applied"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["verification_failed"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(result["error"]["code"], "COMMITTED_UNVERIFIED")
        self.assertNotIn("entry", result)
        self.assertEqual(self.show(entry_id)["entry"]["body"], "已经写入但当时无法验证。")
        self.assertTrue(self.call("status")["pending_recovery"])

    def test_precommit_update_failure_can_rebuild_then_retry_same_request(self):
        created = self.create()
        entry_id = created["entry"]["id"]
        target = self.entry_path(entry_id)
        expected_sha = self.show(entry_id)["sha256"]
        old_bytes = target.read_bytes()
        real_replace = os.replace
        payload = {"body": "明确要求更新后的内容。", "request_id": "af365c73-6d6c-4cae-b4ba-f67a299eac82"}

        def fail_before_entry_commit(source, destination, *args, **kwargs):
            if Path(destination) == target:
                raise OSError("simulated precommit update failure")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(memory.os, "replace", side_effect=fail_before_entry_commit):
            with self.assertRaises((memory.VaultError, OSError)):
                self.call("update", apply=True, entry_id=entry_id,
                          expected_sha256=expected_sha, payload=payload)
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertTrue(self.call("status")["pending_recovery"])
        self.call("rebuild", apply=True)
        self.assertEqual(target.read_bytes(), old_bytes)
        retried = self.call("update", apply=True, entry_id=entry_id,
                            expected_sha256=expected_sha, payload=payload)
        self.assertTrue(retried["applied"])
        self.assertEqual(retried["entry"]["body"], payload["body"])

    def test_adding_manually_edited_body_reuses_original_id(self):
        created = self.create(body="原有正文 A。\n")
        entry_id = created["entry"]["id"]
        path = self.entry_path(entry_id)
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("原有正文 A。", "手工改后 B。"), encoding="utf-8")
        before = snapshot(self.root)
        duplicate = self.add(body="手工改后 B。\n")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["entry"]["id"], entry_id)
        self.assertEqual(duplicate["entry"]["body"], "手工改后 B。\n")
        self.assertEqual(len(list((self.root / "entries").glob("*.md"))), 1)
        self.assertEqual(snapshot(self.root), before)

    def test_review_after_rejects_compact_and_iso_week_dates(self):
        self.initialize()
        for malformed in ("20300102", "2030-W01-2", "2030W012"):
            with self.subTest(review_after=malformed):
                before = snapshot(self.root)
                self.assert_vault_error(lambda: self.add(review_after=malformed))
                self.assertEqual(snapshot(self.root), before)

    def test_recovery_restores_original_audit_request_and_deduplicates_event(self):
        self.initialize()
        original_request = "记住：这是一条明确请求。\n恢复时必须保留原始来源。"
        with mock.patch.object(self.vault, "_event", side_effect=KeyboardInterrupt("simulated process interruption before audit")):
            with self.assertRaises(KeyboardInterrupt):
                self.call("add", apply=True, request=original_request, payload=self.payload())
        state_path = self.root / "_state" / "vault.json"
        pending = json.loads(state_path.read_text(encoding="utf-8"))["pending"]
        entry_id = pending["id"]
        self.assertTrue(self.entry_path(entry_id).is_file())
        self.assertEqual(pending["operation"], "add")
        self.assertEqual(pending["request"], original_request)
        event_path = self.root / "history" / entry_id / ("event-" + pending["event_id"] + ".md")
        self.assertFalse(event_path.exists())
        real_replace = os.replace

        def fail_after_audit_recovery(source, destination, *args, **kwargs):
            if Path(destination) == state_path:
                raise OSError("simulated interruption finishing recovery")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(memory.os, "replace", side_effect=fail_after_audit_recovery):
            with self.assertRaises((memory.VaultError, OSError)):
                self.call("rebuild", apply=True, request="明确刷新总览，第一次恢复")
        event_before = event_path.read_bytes()
        text = event_before.decode("utf-8")
        self.assertIn("- 操作：add", text)
        self.assertIn(pending["event_at"], text)
        for line in original_request.splitlines():
            self.assertIn("> " + line, text)
        self.assertNotIn("第一次恢复", text)
        self.assertTrue(self.call("status")["pending_recovery"])
        self.call("rebuild", apply=True, request="明确刷新总览，完成恢复")
        self.assertEqual(event_path.read_bytes(), event_before)
        self.assertEqual(len(list((self.root / "history" / entry_id).glob("event-*.md"))), 1)
        self.assertFalse(self.call("status")["pending_recovery"])

    def test_handled_audit_io_error_keeps_pending_until_explicit_rebuild(self):
        self.initialize()
        original_request = "记住：操作记录写入失败时也要保留这次明确请求。"
        with mock.patch.object(self.vault, "_event", side_effect=OSError("simulated handled audit I/O failure")):
            result = self.call("add", apply=True, request=original_request, payload=self.payload())
        self.assertTrue(result["applied"])
        self.assertTrue(result["recovery_required"])
        self.assertIn("HISTORY_INCOMPLETE", [warning["code"] for warning in result["warnings"]])
        entry_id = result["entry"]["id"]
        self.assertEqual(self.show(entry_id)["entry"]["body"], self.payload()["body"])
        state_path = self.root / "_state" / "vault.json"
        pending = json.loads(state_path.read_text(encoding="utf-8"))["pending"]
        self.assertEqual(pending["request"], original_request)
        event_path = self.root / "history" / entry_id / ("event-" + pending["event_id"] + ".md")
        self.assertFalse(event_path.exists())
        self.assertEqual(self.assert_vault_error(lambda: self.add()).code, "RECOVERY_REQUIRED")
        self.call("rebuild", apply=True, request="明确刷新总览并恢复未完成的记录")
        recovered = event_path.read_text(encoding="utf-8")
        self.assertIn("- 操作：add", recovered)
        self.assertIn("> " + original_request, recovered)
        self.assertIn(pending["event_at"], recovered)
        self.assertFalse(self.call("status")["pending_recovery"])
        self.assertEqual(len(list((self.root / "history" / entry_id).glob("event-*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
