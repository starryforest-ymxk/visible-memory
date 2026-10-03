"""Public v2 behavior and failure tests; all content is fictional and temporary."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("visible_memory_v2_tests", SCRIPTS / "memory.py")
memory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = memory
SPEC.loader.exec_module(memory)
import memory_v1


def hashes(root):
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class CoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="虚构 v2 记忆 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Agents 中文测试"
        self.vault = memory.Vault(self.root)

    def call(self, command, **kwargs):
        kwargs.setdefault("request", "用户明确要求测试记忆操作：" + command)
        return self.vault.execute(command, **kwargs)

    def payload(self, **overrides):
        data = {"title": "写作风格", "type": "preference", "tags": ["写作", "测试"],
                "source": "虚构用户明确要求保存 https://example.invalid/source", "scope": "仅测试项目",
                "body": "写作需要清楚的中文解释，保留必要的证据。"}
        data.update(overrides)
        return data

    def initialize(self):
        result = self.call("init", apply=True)
        self.assertTrue(result["ok"], result)
        return result

    def add(self, **overrides):
        result = self.call("add", apply=True, payload=self.payload(**overrides))
        self.assertTrue(result["ok"], result)
        return result

    def ids(self, result):
        return [x["id"] for x in result["entries"]]

    def assert_error(self, action, expected=None):
        try:
            result = action()
        except memory.VaultError as exc:
            code = exc.code
        else:
            self.assertFalse(result.get("ok"), result)
            code = result["error"]["code"]
        if expected:
            self.assertEqual(code, expected)
        return code

    def test_initialization_preview_and_small_state(self):
        self.call("init")
        self.assertFalse(self.root.exists())
        self.initialize()
        header = json.loads((self.root / "_state" / "vault.json").read_bytes())
        self.assertEqual(header["format_version"], 2)
        self.assertNotIn("requests", header)
        self.assertNotIn("fingerprints", header)
        self.assertEqual(list((self.root / "entries").iterdir()), [])
        self.assertEqual(self.call("list")["total"], 0)

    def test_add_update_archive_restore_and_history(self):
        self.initialize()
        saved = self.add()
        entry_id = saved["entry"]["id"]
        self.assertIn(entry_id, self.ids(self.call("search", query="写作")))
        updated = self.call("update", apply=True, entry_id=entry_id, expected_sha256=saved["sha256"],
                            payload={"body": "修改后的写作偏好：简明解释。"})
        self.assertEqual(updated["entry"]["id"], entry_id)
        self.assertEqual(updated["entry"]["revision"], 2)
        archived = self.call("archive", apply=True, entry_id=entry_id, expected_sha256=updated["sha256"])
        self.assertNotIn(entry_id, self.ids(self.call("search", query="写作")))
        self.assertIn(entry_id, self.ids(self.call("search", query="写作", include_archived=True)))
        restored = self.call("restore", apply=True, entry_id=entry_id, expected_sha256=archived["sha256"])
        self.assertEqual(restored["entry"]["status"], "active")
        history = self.call("history", entry_id=entry_id)
        self.assertGreaterEqual(len(history["versions"]), 4)
        self.assertGreaterEqual(len(history["events"]), 4)

    def test_every_read_and_write_preview_preserves_all_vault_files(self):
        self.initialize()
        saved = self.add()
        entry_id = saved["entry"]["id"]
        before = hashes(self.root)
        for command, arguments in (("status", {}), ("list", {}), ("search", {"query": "写作"}),
                                   ("show", {"entry_id": entry_id}), ("history", {"entry_id": entry_id}),
                                   ("check", {}), ("rebuild", {}),
                                   ("add", {"payload": self.payload(title="仅预览")}),
                                   ("update", {"entry_id": entry_id, "expected_sha256": saved["sha256"],
                                               "payload": {"body": "仅预览，不保存"}})):
            with self.subTest(command=command):
                self.call(command, **arguments)
                self.assertEqual(hashes(self.root), before)

    def test_current_request_gate_precedes_filesystem_access(self):
        with patch("pathlib.Path.open") as opened, patch("os.scandir") as scanned:
            self.assert_error(lambda: self.vault.execute("search", query="写作", request=""), "EXPLICIT_REQUEST_REQUIRED")
        opened.assert_not_called()
        scanned.assert_not_called()

    def test_optimistic_update_refuses_hand_edit(self):
        self.initialize()
        saved = self.add()
        path = Path(saved["path"])
        path.write_bytes(path.read_bytes() + "\n手工追加".encode())
        before = hashes(self.root)
        self.assert_error(lambda: self.call("update", apply=True, entry_id=saved["entry"]["id"],
                                           expected_sha256=saved["sha256"], payload={"body": "不能覆盖"}), "CONFLICT")
        self.assertEqual(before, hashes(self.root))

    def test_noop_update_or_restore_does_not_create_new_revision(self):
        self.initialize()
        saved = self.add()
        before = hashes(self.root)
        for command, arguments in (("update", {"payload": {"body": saved["entry"]["body"]}}), ("restore", {})):
            with self.subTest(command=command):
                result = self.call(command, apply=True, entry_id=saved["entry"]["id"],
                                   expected_sha256=saved["sha256"], **arguments)
                self.assertTrue(result["unchanged"], result)
                self.assertEqual(result["sha256"], saved["sha256"])
                self.assertEqual(before, hashes(self.root))

    def test_unknown_payload_fields_are_rejected_without_changes(self):
        self.initialize()
        before = hashes(self.root)
        self.assert_error(lambda: self.call("add", apply=True, payload=self.payload(titel="拼错的字段")), "INVALID_INPUT")
        self.assertEqual(before, hashes(self.root))

    def test_hand_edit_is_searchable_without_index_writes(self):
        self.initialize()
        saved = self.add(title="普通测试条目", tags=["测试"], body="原始正文不含目标词。")
        path = Path(saved["path"])
        path.write_bytes(path.read_bytes() + "\n新出现的火星花园主题。".encode())
        before = hashes(self.root)
        result = self.call("search", query="火星花园")
        self.assertIn(saved["entry"]["id"], self.ids(result))
        self.assertEqual(before, hashes(self.root))
        self.assertIn("火星花园", self.call("show", entry_id=saved["entry"]["id"])["entry"]["body"])

    def test_request_replay_and_reused_request_rejection(self):
        self.initialize()
        saved = self.add(request_id="save-001")
        replay = self.add(request_id="save-001")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entry"]["id"], saved["entry"]["id"])
        self.assertEqual(self.call("list")["total"], 1)
        self.assert_error(lambda: self.call("add", apply=True, payload=self.payload(request_id="save-001", body="另一条内容")), "REQUEST_ID_REUSED")

    def test_search_index_rebuild_preserves_request_receipt(self):
        self.initialize()
        saved = self.add(request_id="durable-retry")
        index = self.root / "_index" / "search.sqlite"
        index.write_bytes(b"deliberately corrupt disposable index")
        before = hashes(self.root)
        result = self.call("search", query="写作")
        self.assertIn(saved["entry"]["id"], self.ids(result))
        self.assertEqual(hashes(self.root), before)
        rebuilt = self.call("rebuild", apply=True)
        self.assertTrue(rebuilt["ok"], rebuilt)
        retry = self.add(request_id="durable-retry")
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["entry"]["id"], saved["entry"]["id"])

    def test_durable_state_corruption_stops_write_but_show_survives(self):
        self.initialize()
        saved = self.add()
        operations = self.root / "_state" / "operations.sqlite"
        operations.write_bytes(b"deliberately corrupt durable state")
        before = hashes(self.root)
        self.assert_error(lambda: self.call("add", apply=True, payload=self.payload(title="必须拒绝")), "OPERATIONS_UNAVAILABLE")
        self.assertEqual(before, hashes(self.root))
        self.assertEqual(self.call("show", entry_id=saved["entry"]["id"])["entry"]["body"], saved["entry"]["body"])

    def test_missing_index_does_not_auto_create_files(self):
        self.initialize()
        saved = self.add()
        (self.root / "_index" / "search.sqlite").unlink()
        before = hashes(self.root)
        self.assertIn(saved["entry"]["id"], self.ids(self.call("search", query="写作")))
        self.assertEqual(before, hashes(self.root))

    def test_history_failure_recovers_once_and_replay_does_not_duplicate(self):
        self.initialize()
        with patch.object(self.vault, "_event", side_effect=OSError("injected history failure")):
            saved = self.call("add", apply=True, payload=self.payload(request_id="history-retry"))
        self.assertTrue(saved["applied"], saved)
        self.assertTrue(saved.get("recovery_required") or any(x["code"] == "HISTORY_INCOMPLETE" for x in saved.get("warnings", [])), saved)
        entry_id = saved.get("entry", {}).get("id", saved.get("id"))
        self.assertIsNotNone(entry_id)
        self.assertTrue(self.call("rebuild", apply=True)["ok"])
        first = self.call("history", entry_id=entry_id)
        self.assertEqual(len(first["events"]), 1)
        self.assertTrue(self.call("rebuild", apply=True)["ok"])
        second = self.call("history", entry_id=entry_id)
        self.assertEqual(first, second)
        replay = self.add(request_id="history-retry")
        self.assertTrue(replay["replayed"])

    def test_committed_but_unverified_has_recovery_evidence(self):
        self.initialize()
        original = self.vault._atomic
        def fail_after_replace(path, data, *args, **kwargs):
            result = original(path, data, *args, **kwargs)
            if Path(path).parent == self.root / "entries":
                raise memory.VaultError("COMMITTED_UNVERIFIED", "injected post-replace readback failure", {"committed": True})
            return result
        with patch.object(self.vault, "_atomic", side_effect=fail_after_replace):
            result = self.call("add", apply=True, payload=self.payload(request_id="verify-retry"))
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["applied"], result)
        self.assertTrue(result["recovery_required"], result)
        self.assertEqual(len(list((self.root / "entries").glob("*.md"))), 1)
        self.assertTrue(self.call("rebuild", apply=True)["ok"])
        self.assertTrue(self.add(request_id="verify-retry")["replayed"])

    def test_corrupt_entry_is_preserved_and_reported(self):
        self.initialize()
        saved = self.add()
        Path(saved["path"]).write_bytes(b"not a TOML Markdown entry")
        before = hashes(self.root)
        result = self.call("search", query="写作")
        self.assertTrue(result.get("errors"), result)
        self.assertNotIn(saved["entry"]["id"], self.ids(result))
        self.assertEqual(before, hashes(self.root))

    def test_precommit_update_failure_preserves_old_text_and_can_recover(self):
        self.initialize()
        saved = self.add()
        original = self.vault._atomic
        def fail_before_replace(path, data, *args, **kwargs):
            if Path(path).parent == self.root / "entries":
                raise OSError("injected failure before canonical replacement")
            return original(path, data, *args, **kwargs)
        with patch.object(self.vault, "_atomic", side_effect=fail_before_replace):
            result = self.call("update", apply=True, entry_id=saved["entry"]["id"],
                               expected_sha256=saved["sha256"], payload={"body": "待保存的新正文", "request_id": "precommit-update"})
        self.assertFalse(result["ok"], result)
        current = self.call("show", entry_id=saved["entry"]["id"])
        self.assertEqual(current["sha256"], saved["sha256"])
        self.assertTrue(self.call("rebuild", apply=True)["ok"])
        retried = self.call("update", apply=True, entry_id=saved["entry"]["id"],
                            expected_sha256=saved["sha256"], payload={"body": "待保存的新正文", "request_id": "precommit-update"})
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(retried["entry"]["revision"], 2)

    def test_precommit_add_failure_is_false_and_same_request_can_retry_directly(self):
        self.initialize()
        original = self.vault._atomic
        def fail_before_replace(path, data, *args, **kwargs):
            if Path(path).parent == self.root / "entries":
                raise OSError("injected precommit add failure")
            return original(path, data, *args, **kwargs)
        with patch.object(self.vault, "_atomic", side_effect=fail_before_replace):
            failed = self.call("add", apply=True, payload=self.payload(request_id="precommit-add"))
        self.assertFalse(failed["ok"], failed)
        self.assertIs(failed["applied"], False)
        self.assertEqual(list((self.root / "entries").glob("*.md")), [])
        saved = self.add(request_id="precommit-add")
        self.assertEqual(self.call("list")["total"], 1)
        self.assertTrue(self.add(request_id="precommit-add")["replayed"])
        self.assertEqual(saved["entry"]["revision"], 1)

    def test_init_final_header_interruption_keeps_original_event_request(self):
        original_request = "明确初始化虚构库：原始用户要求"
        retry_request = "请继续刚才中断的初始化"
        original = self.vault._atomic
        def fail_final_header(path, data, *args, **kwargs):
            if Path(path) == self.root / "_state" / "vault.json" and not json.loads(data).get("initializing"):
                raise OSError("injected final init header interruption")
            return original(path, data, *args, **kwargs)
        with patch.object(self.vault, "_atomic", side_effect=fail_final_header):
            with self.assertRaises(OSError):
                self.call("init", apply=True, request=original_request)
        header = json.loads((self.root / "_state" / "vault.json").read_bytes())
        self.assertTrue(header["initializing"])
        events = {p.name: p.read_bytes() for p in (self.root / "history" / "maintenance").glob("event-*.md")}
        self.assertEqual(len(events), 1)
        self.assertTrue(self.call("init", apply=True, request=retry_request)["ok"])
        after = {p.name: p.read_bytes() for p in (self.root / "history" / "maintenance").glob("event-*.md")}
        self.assertEqual(events, after)
        self.assertIn(original_request.encode(), next(iter(after.values())))
        self.assertNotIn(retry_request.encode(), next(iter(after.values())))

    def test_wal_durable_database_with_no_sidecars_never_creates_them_during_reads(self):
        self.initialize()
        saved = self.add()
        path = self.root / "_state" / "operations.sqlite"
        connection = sqlite3.connect(path)
        self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        connection.close()
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())
        before = hashes(self.root)
        for command, arguments in (("status", {}), ("search", {"query": "写作"})):
            with self.subTest(command=command):
                result = self.call(command, **arguments)
                self.assertTrue(result.get("operations_unavailable"), result)
                self.assertEqual(hashes(self.root), before)
        self.assertEqual(self.call("show", entry_id=saved["entry"]["id"])["sha256"], saved["sha256"])
        self.assert_error(lambda: self.call("add", apply=True, payload=self.payload(title="必须拒绝 WAL 写入")), "OPERATIONS_UNAVAILABLE")
        self.assertEqual(hashes(self.root), before)

    def test_operation_database_publish_failure_leaves_no_formal_empty_database(self):
        original = memory.operations.os.replace
        def fail_operations_publish(source, destination, *args, **kwargs):
            if Path(destination) == self.root / "_state" / "operations.sqlite":
                raise OSError("injected before operations temporary database publication")
            return original(source, destination, *args, **kwargs)
        with patch.object(memory.operations.os, "replace", side_effect=fail_operations_publish):
            self.assert_error(lambda: self.call("init", apply=True), "OPERATIONS_UNAVAILABLE")
        self.assertFalse((self.root / "_state" / "operations.sqlite").exists())
        self.assertTrue(json.loads((self.root / "_state" / "vault.json").read_bytes())["initializing"])
        self.assertTrue(self.call("init", apply=True, request="明确重试初始化")["ok"])
        self.assertEqual(self.call("list")["total"], 0)

    def prepare_v1(self):
        old = memory_v1.Vault(self.root)
        old.execute("init", request="明确初始化虚构旧库", apply=True)
        payload = self.payload(request_id="legacy-request")
        saved = old.execute("add", request="明确保存虚构旧记忆", apply=True, payload=payload)
        return payload, saved

    def test_migration_index_stage_interruption_resumes_without_duplicate_history(self):
        payload, saved = self.prepare_v1()
        before = hashes(self.root)
        with patch.object(memory.index, "build", side_effect=OSError("injected migration index interruption")):
            with self.assertRaises(OSError):
                self.call("migrate", apply=True)
        self.assertEqual(json.loads((self.root / "_state" / "vault.json").read_bytes())["format_version"], 1)
        self.assertEqual(hashes(self.root)["entries/" + saved["entry"]["id"] + ".md"], before["entries/" + saved["entry"]["id"] + ".md"])
        resumed = self.call("migrate", apply=True)
        self.assertTrue(resumed["ok"], resumed)
        self.assertTrue(self.call("add", apply=True, payload=payload)["replayed"])
        after = hashes(self.root)
        for relative, checksum in before.items():
            if relative.startswith("history/"):
                self.assertEqual(after[relative], checksum)

    def test_migration_operation_database_creation_window_is_recoverable(self):
        payload, _ = self.prepare_v1()
        original = self.vault._ops
        def crash_after_database_creation(header, **kwargs):
            connection = original(header, **kwargs)
            if kwargs.get("create"):
                connection.close()
                raise OSError("injected stop after operations database creation")
            return connection
        with patch.object(self.vault, "_ops", side_effect=crash_after_database_creation):
            with self.assertRaises(OSError):
                self.call("migrate", apply=True)
        self.assertEqual(json.loads((self.root / "_state" / "vault.json").read_bytes())["format_version"], 1)
        self.assertTrue(self.call("migrate", apply=True)["ok"])
        self.assertTrue(self.call("add", apply=True, payload=payload)["replayed"])

    def test_migration_final_header_interruption_keeps_original_event_request(self):
        self.prepare_v1()
        original_request = "明确迁移虚构旧库：最初的要求"
        retry_request = "请继续完成中断的迁移"
        original = self.vault._atomic
        def fail_final_header(path, data, *args, **kwargs):
            if Path(path) == self.root / "_state" / "vault.json" and json.loads(data).get("format_version") == 2:
                raise OSError("injected final migration header interruption")
            return original(path, data, *args, **kwargs)
        with patch.object(self.vault, "_atomic", side_effect=fail_final_header):
            with self.assertRaises(OSError):
                self.call("migrate", apply=True, request=original_request)
        self.assertEqual(json.loads((self.root / "_state" / "vault.json").read_bytes())["format_version"], 1)
        def migration_events():
            return {p.name: p.read_bytes() for p in (self.root / "history" / "maintenance").glob("event-*.md")
                    if "- 操作：migrate" in p.read_text(encoding="utf-8")}
        events = migration_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(self.call("migrate", apply=True, request=retry_request)["ok"])
        self.assertEqual(events, migration_events())
        self.assertIn(original_request.encode(), next(iter(events.values())))
        self.assertNotIn(retry_request.encode(), next(iter(events.values())))

    def test_migration_refuses_unknown_existing_search_index_without_overwrite(self):
        self.prepare_v1()
        index = self.root / "_index" / "search.sqlite"
        index.parent.mkdir()
        unknown = b"Unknown user's file, not an owned disposable cache"
        index.write_bytes(unknown)
        entry_before = hashes(self.root / "entries")
        self.assert_error(lambda: self.call("migrate", apply=True), "UNOWNED_INDEX")
        self.assertEqual(index.read_bytes(), unknown)
        self.assertEqual(hashes(self.root / "entries"), entry_before)
        self.assertEqual(json.loads((self.root / "_state" / "vault.json").read_bytes())["format_version"], 1)

    def test_migrate_v1_preserves_raw_history_and_durable_retry(self):
        payload, saved = self.prepare_v1()
        before = hashes(self.root)
        preview = self.call("migrate")
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(before, hashes(self.root))
        migrated = self.call("migrate", apply=True)
        self.assertTrue(migrated["ok"], migrated)
        self.assertTrue(migrated["migrated"])
        self.assertTrue(Path(migrated["backup_manifest"]).is_file())
        after = hashes(self.root)
        for relative, checksum in before.items():
            if relative.startswith(("entries/", "history/")):
                self.assertEqual(after[relative], checksum)
        retry = self.call("add", apply=True, payload=payload)
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["entry"]["id"], saved["entry"]["id"])
        self.assertTrue(self.call("rebuild", apply=True)["ok"])
        self.assertTrue(self.call("add", apply=True, payload=payload)["replayed"])


if __name__ == "__main__":
    unittest.main()
