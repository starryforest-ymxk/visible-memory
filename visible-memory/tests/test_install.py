"""Installer tests use only isolated temporary directories and fictional content."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("memory_install", Path(__file__).parents[1] / "scripts" / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="visible-memory-install-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "来源 Skill"
        self.target = self.base / "installed" / "visible-memory"
        self.vault = self.base / "Agents"
        self.agents = self.base / "config" / "AGENTS.md"
        self.backups = self.base / "backups"
        for relative, data in {"SKILL.md": b"test skill", "scripts/memory.py": b"# test core",
                               "agents/openai.yaml": b"interface: {}", "references/format.md": b"test format"}.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.agents.parent.mkdir()
        self.agents.write_bytes(b"User instructions\r\n")

    def run_install(self, apply=True):
        return installer.install(self.source, self.target, self.vault, self.agents, self.backups, apply=apply)

    def manifest(self):
        return {str(path.relative_to(self.base)): path.read_bytes()
                for path in self.base.rglob("*") if path.is_file()}

    def test_preview_is_read_only(self):
        before = self.manifest()
        result = self.run_install(apply=False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["applied"])
        self.assertEqual(self.manifest(), before)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.vault.exists())

    def test_preserve_prefix_suffix_and_exact_backup(self):
        prefix = "前面的规则\r\n".encode("utf-8")
        suffix = "\r\n后面的规则\n".encode("utf-8")
        original = prefix + installer.START + b"\r\nold route\r\n" + installer.END + suffix
        self.agents.write_bytes(original)
        result = self.run_install()
        self.assertTrue(result["ok"], result)
        after = self.agents.read_bytes()
        self.assertTrue(after.startswith(prefix + installer.START))
        self.assertTrue(after.endswith(installer.END + suffix))
        self.assertEqual(Path(result["backup_path"]).read_bytes(), original)
        self.assertFalse(self.vault.exists())

    def test_idempotence(self):
        first = self.run_install()
        self.assertTrue(first["ok"], first)
        before = self.manifest()
        second = self.run_install()
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["unchanged"])
        self.assertFalse(second["applied"])
        self.assertEqual(self.manifest(), before)

    def test_append_preserves_existing_bytes_without_newline(self):
        original = "用户自己的规则，不要更改".encode("utf-8")
        self.agents.write_bytes(original)
        result = self.run_install()
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.agents.read_bytes().startswith(original + b"\n" + installer.START))
        self.assertEqual(Path(result["backup_path"]).read_bytes(), original)

    def test_empty_agents_backed_up_exactly(self):
        self.agents.write_bytes(b"")
        result = self.run_install()
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["backup_path"]).read_bytes(), b"")

    def test_backup_conflict_refuses_before_target_creation(self):
        original = self.agents.read_bytes()
        self.backups.mkdir()
        backup = self.backups / ("AGENTS-" + installer.sha256(original) + ".md")
        backup.write_bytes(b"unexpected backup contents")
        before = self.manifest()
        result = self.run_install()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BACKUP_CONFLICT")
        self.assertEqual(self.manifest(), before)

    def test_reject_different_or_unowned_target_before_writing(self):
        for relative in ("SKILL.md", "personal.txt"):
            with self.subTest(relative=relative):
                path = self.target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"do not overwrite")
                before = self.manifest()
                result = self.run_install()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "TARGET_CONFLICT")
                self.assertEqual(self.manifest(), before)
                path.unlink()

    def test_reject_duplicate_or_unpaired_markers(self):
        cases = [installer.START + b"\nmissing end", installer.END,
                 (installer.START + b"\n" + installer.END + b"\n") * 2,
                 installer.END + b"\n" + installer.START]
        for original in cases:
            with self.subTest(original=original):
                self.agents.write_bytes(original)
                before = self.manifest()
                result = self.run_install()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "INVALID_MANAGED_BLOCK")
                self.assertEqual(self.manifest(), before)

    def test_nonempty_override_refuses_and_empty_override_allows(self):
        override = self.agents.with_name("AGENTS.override.md")
        override.write_bytes(b"another configuration")
        before = self.manifest()
        result = self.run_install()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "AGENTS_SHADOWED")
        self.assertEqual(self.manifest(), before)
        override.write_bytes(b"")
        self.assertTrue(self.run_install()["ok"])

    def test_exclude_cache_and_scratch(self):
        for relative in ("scripts/__pycache__/cache.pyc", "scripts/scratch/private.txt", "notes.txt"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not shipped")
        result = self.run_install()
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.target / "scripts/__pycache__").exists())
        self.assertFalse((self.target / "scripts/scratch").exists())
        self.assertFalse((self.target / "notes.txt").exists())

    def test_hardlink_rejected(self):
        original = self.base / "hardlink-original.txt"
        original.write_bytes(b"test skill")
        linked = self.source / "SKILL.md"
        linked.unlink()
        try:
            os.link(original, linked)
        except OSError as exc:
            self.skipTest(str(exc))
        result = self.run_install()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSAFE_LINK")
        self.assertFalse(self.target.exists())

    def test_reparse_or_symlink_path_rejected(self):
        destination = self.base / "outside"
        destination.mkdir()
        self.target.parent.mkdir()
        linked = self.target
        if os.name == "nt":
            command = f"New-Item -ItemType Junction -Path '{str(linked).replace(chr(39), chr(39) * 2)}' -Target '{str(destination).replace(chr(39), chr(39) * 2)}' | Out-Null"
            proc = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True)
            if proc.returncode:
                self.skipTest(proc.stderr.decode(errors="replace"))
            self.addCleanup(lambda: linked.rmdir() if linked.exists() else None)
        else:
            try:
                linked.symlink_to(destination, target_is_directory=True)
            except OSError as exc:
                self.skipTest(str(exc))
        result = self.run_install()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSAFE_LINK")
        self.assertEqual(list(destination.iterdir()), [])

    def test_root_and_overlap_rejected(self):
        root = Path(self.base.anchor)
        result = installer.install(self.source, root, self.vault, self.agents, self.backups, apply=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNSAFE_PATH")
        result = installer.install(self.source, self.source / "nested", self.vault, self.agents, self.backups, apply=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "OVERLAPPING_PATHS")


if __name__ == "__main__":
    unittest.main()
