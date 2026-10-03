"""Upgrade/recovery tests use fictional vaults and an isolated migration boundary."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("upgrade_installer", Path(__file__).parents[1] / "scripts" / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="visible-memory-upgrade-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source, self.target, self.vault = (self.base / name for name in ("新版本", "installed", "Agents"))
        self.agents, self.backups = self.base / "config" / "AGENTS.md", self.base / "backups"
        old = {"SKILL.md": b"old skill", "scripts/memory.py": b"old core",
               "scripts/install.py": b"old installer", "agents/openai.yaml": b"old interface"}
        self.old_skill = old
        for relative, raw in old.items():
            self.put(self.target / relative, raw)
            self.put(self.source / relative, b"new " + raw)
        self.put(self.source / "scripts" / "search.py", b"new dependency")
        self.put(self.source / "references" / "v1-release.json", installer.json_data({
            "release": "visible-memory-v1", "files": {p: installer.sha256(raw) for p, raw in old.items()}}))
        self.original_agents = "用户原有指令\r\n".encode("utf-8")
        self.put(self.agents, self.original_agents)
        self.put(self.vault / "_state" / "vault.json", installer.json_data({"format_version": 1, "requests": {"r1": "proof"}}))
        self.put(self.vault / "entries" / "entry.md", b"fictional entry unchanged by migration")
        self.put(self.vault / "history" / "old.md", b"fictional old history")
        for view in installer.VAULT_VIEWS:
            self.put(self.vault / view, ("old " + view).encode())
        self.before_vault = installer.file_manifest(self.vault, vault=True)
        self.calls = []
        self.migration_patch = patch.object(installer, "run_migration", side_effect=self.migrate)
        self.migration_patch.start()
        self.addCleanup(self.migration_patch.stop)

    def put(self, path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def migrate(self, source, vault, request, apply):
        self.calls.append((request, apply))
        if apply and json.loads((vault / "_state" / "vault.json").read_bytes())["format_version"] == 1:
            self.assertEqual((self.target / "scripts" / "memory.py").read_bytes(), self.old_skill["scripts/memory.py"])
            self.put(vault / "_state" / "vault.json", installer.json_data({"format_version": 2}))
            self.put(vault / "_state" / "operations.sqlite", b"durable migration baseline")
            self.put(vault / "_index" / "search.sqlite", b"rebuildable index")
            self.put(vault / "catalog" / "page.md", b"generated page")
            self.put(vault / "history" / "migrations" / "backup.json", b"retained migration evidence")
            for view in installer.VAULT_VIEWS:
                self.put(vault / view, ("new " + view).encode())
        return {"ok": True, "applied": apply, "migrated": apply,
                "backup_manifest": str(vault / "history" / "migrations" / "backup.json")}

    def run_upgrade(self, apply=True, rollback=False, request="请升级我现有的可见记忆 Skill"):
        return installer.install(self.source, self.target, self.vault, self.agents, self.backups,
                                 apply=apply, upgrade=not rollback, rollback=rollback, request=request)

    def manifest(self):
        return {p.relative_to(self.base).as_posix(): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}

    def test_preview_and_missing_request_write_nothing(self):
        before = self.manifest()
        result = self.run_upgrade(apply=False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["applied"])
        self.assertEqual(before, self.manifest())
        result = self.run_upgrade(request="")
        self.assertEqual(result["error"]["code"], "EXPLICIT_REQUEST_REQUIRED")
        self.assertEqual(before, self.manifest())

    def test_upgrade_backups_authorization_and_idempotence(self):
        result = self.run_upgrade()
        self.assertTrue(result["ok"], result)
        journal = json.loads(Path(result["backup_manifest"]).read_bytes())
        self.assertEqual(journal["phase"], "complete")
        folder = Path(result["backup_manifest"]).parent
        self.assertEqual((folder / "AGENTS.md").read_bytes(), self.original_agents)
        for relative, raw in self.old_skill.items():
            self.assertEqual((folder / "skill" / relative).read_bytes(), raw)
        self.assertTrue(all(request == "请升级我现有的可见记忆 Skill" for request, _ in self.calls))
        self.assertIn("_index 和 catalog", self.agents.read_text(encoding="utf-8"))
        before = self.manifest()
        again = self.run_upgrade()
        self.assertTrue(again["unchanged"], again)
        self.assertFalse(again["applied"])
        self.assertEqual(before, self.manifest())

    def test_unknown_or_modified_v1_refuses_before_backup(self):
        for relative in ("SKILL.md", "personal.txt"):
            with self.subTest(relative=relative):
                path = self.target / relative
                old = path.read_bytes() if path.exists() else None
                self.put(path, b"user modified")
                before = self.manifest()
                result = self.run_upgrade()
                self.assertEqual(result["error"]["code"], "TARGET_CONFLICT")
                self.assertEqual(before, self.manifest())
                if old is None:
                    path.unlink()
                else:
                    path.write_bytes(old)

    def test_memory_entrypoint_published_last(self):
        publications = []
        original = installer.atomic_write
        def record(path, raw, expected):
            if installer.within(path, self.target):
                publications.append(path.relative_to(self.target).as_posix())
            return original(path, raw, expected)
        with patch.object(installer, "atomic_write", side_effect=record):
            result = self.run_upgrade()
        self.assertTrue(result["ok"], result)
        self.assertEqual(publications[-1], "scripts/memory.py")

    def test_failure_after_migration_reports_change_and_resumes(self):
        original = installer.atomic_write
        def interrupt(path, raw, expected):
            if path == self.target / "scripts" / "memory.py":
                raise OSError("simulated publication interruption")
            return original(path, raw, expected)
        with patch.object(installer, "atomic_write", side_effect=interrupt):
            result = self.run_upgrade()
        self.assertFalse(result["ok"])
        self.assertTrue(result["applied"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(json.loads((self.vault / "_state" / "vault.json").read_bytes())["format_version"], 2)
        resumed = self.run_upgrade()
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(sum(apply for _, apply in self.calls), 1)

    def test_migration_partial_failure_does_not_publish_core(self):
        with patch.object(installer, "run_migration", side_effect=lambda s, v, r, a: {
            "ok": not a, "applied": a, "error": {"code": "INJECTED", "message": "partial"}}):
            result = self.run_upgrade()
        self.assertFalse(result["ok"])
        self.assertTrue(result["applied"])
        self.assertEqual((self.target / "scripts" / "memory.py").read_bytes(), self.old_skill["scripts/memory.py"])
        self.assertTrue(self.run_upgrade()["ok"])

    def test_before_write_rollback_restores_v1_and_keeps_history_evidence(self):
        self.assertTrue(self.run_upgrade()["ok"])
        before = self.manifest()
        preview = self.run_upgrade(apply=False, rollback=True, request="回退本次升级")
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(before, self.manifest())
        result = self.run_upgrade(rollback=True, request="回退本次升级")
        self.assertTrue(result["ok"], result)
        self.assertEqual(installer.file_manifest(self.target), {p: installer.sha256(raw) for p, raw in self.old_skill.items()})
        for relative, checksum in self.before_vault.items():
            self.assertEqual(installer.sha256((self.vault / relative).read_bytes()), checksum)
        self.assertEqual(self.agents.read_bytes(), self.original_agents)
        self.assertFalse((self.vault / "_state" / "operations.sqlite").exists())
        self.assertTrue((self.vault / "history" / "migrations" / "backup.json").exists())

    def test_rollback_refuses_after_entry_or_durable_state_change(self):
        self.assertTrue(self.run_upgrade()["ok"])
        for relative in ("entries/entry.md", "_state/operations.sqlite"):
            with self.subTest(relative=relative):
                path = self.vault / relative
                old = path.read_bytes()
                path.write_bytes(old + b"after migration write")
                before = self.manifest()
                result = self.run_upgrade(rollback=True, request="回退")
                self.assertEqual(result["error"]["code"], "ROLLBACK_AFTER_WRITE")
                self.assertEqual(before, self.manifest())
                path.write_bytes(old)

    def test_rollback_interruption_can_resume(self):
        self.assertTrue(self.run_upgrade()["ok"])
        original = installer.atomic_write
        def interrupt(path, raw, expected):
            if path == self.vault / "USER.md":
                raise OSError("simulated rollback interruption")
            return original(path, raw, expected)
        with patch.object(installer, "atomic_write", side_effect=interrupt):
            result = self.run_upgrade(rollback=True, request="回退")
        self.assertFalse(result["ok"])
        self.assertTrue(result["applied"])
        result = self.run_upgrade(rollback=True, request="继续回退")
        self.assertTrue(result["ok"], result)

    def test_backup_corruption_refuses_rollback_without_changes(self):
        result = self.run_upgrade()
        backup = Path(result["backup_manifest"]).parent / "skill" / "SKILL.md"
        backup.write_bytes(b"corrupted backup")
        before = self.manifest()
        result = self.run_upgrade(rollback=True, request="回退")
        self.assertEqual(result["error"]["code"], "BACKUP_CONFLICT")
        self.assertEqual(before, self.manifest())

    def test_vault_lock_refuses_upgrade(self):
        self.put(self.vault / "_state" / "write.lock", b"active writer")
        before = self.manifest()
        result = self.run_upgrade()
        self.assertEqual(result["error"]["code"], "VAULT_LOCKED")
        self.assertEqual(before, self.manifest())

    def test_real_migration_backend_upgrade_and_before_write_rollback(self):
        self.migration_patch.stop()
        scripts = Path(__file__).parents[1] / "scripts"
        old_spec = importlib.util.spec_from_file_location("upgrade_v1_fixture", scripts / "memory_v1.py")
        old_module = importlib.util.module_from_spec(old_spec)
        old_spec.loader.exec_module(old_module)
        self.vault = self.base / "真实接口虚构库"
        old_vault = old_module.Vault(self.vault)
        old_vault.execute("init", request="初始化虚构升级测试库", apply=True)
        saved = old_vault.execute("add", request="保存虚构升级测试条目", apply=True, payload={
            "title": "接口测试", "type": "note", "body": "这是虚构记忆。", "tags": [],
            "scope": "仅测试", "source": "虚构明确保存要求", "request_id": "real-backend-test"})
        original = installer.file_manifest(self.vault, vault=True)
        # Ship the real runtime in a fictional package with its own known-v1 fixture manifest.
        for script in scripts.glob("*.py"):
            self.put(self.source / "scripts" / script.name, script.read_bytes())
        result = self.run_upgrade()
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads((self.vault / "_state" / "vault.json").read_bytes())["format_version"], 2)
        self.assertEqual(installer.sha256(Path(saved["path"]).read_bytes()), saved["sha256"])
        result = self.run_upgrade(rollback=True, request="回退虚构测试升级")
        self.assertTrue(result["ok"], result)
        after = installer.file_manifest(self.vault, vault=True)
        for relative, checksum in original.items():
            self.assertEqual(after[relative], checksum)
        replay = old_vault.execute("add", request="重试虚构条目", apply=True, payload={
            "title": "接口测试", "type": "note", "body": "这是虚构记忆。", "tags": [],
            "scope": "仅测试", "source": "虚构明确保存要求", "request_id": "real-backend-test"})
        self.assertTrue(replay["replayed"])


if __name__ == "__main__":
    unittest.main()
