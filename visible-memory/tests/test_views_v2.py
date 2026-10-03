"""Stable pagination, file ownership, and recovery using entirely fictitious data."""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import uuid

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import memory_v1 as legacy
import vm_views as views


def entry(number, **changes):
    result = {"id": str(uuid.UUID(int=number)), "title": f"虚构条目 {number}", "type": "fact",
              "status": "active", "tags": ["虚构"], "source": "测试来源", "scope": "隔离测试",
              "created_at": "2026-09-06T08:00:00+08:00", "updated_at": "2026-09-06T08:00:00+08:00",
              "revision": 1, "body": f"用于验证分册的虚构内容 {number}。"}
    result.update(changes)
    return result


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="分页虚构数据 ", dir=SKILL_ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("entries", "history", "_state"):
            (self.root / name).mkdir()
        self.vault = legacy.Vault(self.root)
        self.conn = sqlite3.connect(self.root / "_state" / "ops-test.sqlite")
        self.addCleanup(self.conn.close)
        views.init_schema(self.conn)
        self.warnings = []

    def rebuild(self, entries):
        records = [(e, legacy.render_entry(e), self.root / "entries" / f"{e['id']}.md") for e in entries]
        return views.rebuild(self.vault, self.conn, records, self.warnings)

    def page(self, number, partition="active/fact"):
        return self.root / "catalog" / partition / f"page-{number:06d}.md"

    def membership(self, number):
        return self.conn.execute("SELECT partition,page FROM views_members WHERE id=?", (str(uuid.UUID(int=number)),)).fetchone()

    def test_boundary_50_51_100_101_stable_pages(self):
        self.rebuild([entry(n) for n in range(1, 51)])
        first = self.page(1).read_bytes()
        self.assertEqual(self.membership(50), ("active/fact", 1))
        views.update(self.vault, self.conn, entry(51), self.warnings)
        self.assertEqual(first, self.page(1).read_bytes())
        self.assertEqual(self.membership(51), ("active/fact", 2))
        self.rebuild([entry(n) for n in range(1, 101)])
        first = self.page(1).read_bytes()
        second = self.page(2).read_bytes()
        self.assertEqual(self.membership(100), ("active/fact", 2))
        result = views.update(self.vault, self.conn, entry(101), self.warnings)
        self.assertEqual(result["content_pages"], 1)
        self.assertEqual(self.membership(101), ("active/fact", 3))
        self.assertEqual(first, self.page(1).read_bytes())
        self.assertEqual(second, self.page(2).read_bytes())

    def test_title_update_reads_no_other_original_and_keeps_other_pages(self):
        self.rebuild([entry(n) for n in range(1, 102)])
        second = self.page(2).read_bytes()
        writes = []
        reads = []
        atomic, raw = self.vault._atomic, self.vault._raw
        def recording_atomic(path, data, **kwargs):
            writes.append(path.relative_to(self.root).as_posix())
            return atomic(path, data, **kwargs)
        def recording_raw(path):
            reads.append(path.relative_to(self.root).as_posix())
            return raw(path)
        with mock.patch.object(self.vault, "_atomic", side_effect=recording_atomic), mock.patch.object(self.vault, "_raw", side_effect=recording_raw):
            views.update(self.vault, self.conn, entry(1, title="新的显示标题"), self.warnings)
        self.assertEqual(writes, ["catalog/active/fact/page-000001.md"])
        self.assertFalse(any(name.startswith("entries/") for name in reads))
        self.assertEqual(second, self.page(2).read_bytes())
        self.assertEqual(self.membership(1), ("active/fact", 1))

    def test_archive_moves_two_pages_preserves_empty_and_reuses_first_hole(self):
        self.rebuild([entry(1)])
        result = views.update(self.vault, self.conn, entry(1, status="archived"), self.warnings)
        self.assertEqual(result["content_pages"], 2)
        self.assertTrue(self.page(1).exists())
        self.assertIn("没有条目", self.page(1).read_text(encoding="utf-8"))
        self.assertEqual(self.membership(1), ("archived/fact", 1))
        views.update(self.vault, self.conn, entry(2), self.warnings)
        self.assertEqual(self.membership(2), ("active/fact", 1))
        views.update(self.vault, self.conn, entry(1, type="note"), self.warnings)
        self.assertEqual(self.membership(1), ("active/note", 1))
        self.assertTrue(self.page(1, "archived/fact").exists())

    def test_new_item_uses_lowest_page_with_space_without_moving_neighbors(self):
        self.rebuild([entry(n) for n in range(1, 102)])
        views.update(self.vault, self.conn, entry(20, status="archived"), self.warnings)
        views.update(self.vault, self.conn, entry(102), self.warnings)
        self.assertEqual(self.membership(102), ("active/fact", 1))
        self.assertEqual(self.membership(51), ("active/fact", 2))
        self.assertEqual(self.membership(101), ("active/fact", 3))

    def test_rebuild_keeps_assignment_and_removed_file_leaves_empty_page(self):
        self.rebuild([entry(n) for n in range(1, 52)])
        self.rebuild([entry(n) for n in range(1, 51)])
        self.assertTrue(self.page(2).exists())
        self.assertIn("没有条目", self.page(2).read_text(encoding="utf-8"))
        self.rebuild([entry(n) for n in range(52, 0, -1)])
        self.assertEqual(self.membership(51), ("active/fact", 2))
        self.assertEqual(self.membership(1), ("active/fact", 1))

    def test_page_order_uses_creation_then_uuid(self):
        self.rebuild([entry(3), entry(2), entry(1)])
        views.update(self.vault, self.conn, entry(3, created_at="2026-09-05T08:00:00+08:00"), self.warnings)
        text = self.page(1).read_text(encoding="utf-8")
        positions = [text.index(f"../../../entries/{entry(n)['id']}.md") for n in (3, 1, 2)]
        self.assertEqual(positions, sorted(positions))

    def test_large_fields_bounded_and_display_table_has_no_full_body(self):
        text = "测试😀[\\]" * 10000
        self.rebuild([entry(n, title=text, body=text) for n in range(1, 51)])
        self.assertLessEqual(self.page(1).stat().st_size, 65536)
        row = self.conn.execute("SELECT title,excerpt FROM views_members LIMIT 1").fetchone()
        self.assertLessEqual(len(row[0]), 65)
        self.assertLessEqual(len(row[1]), 97)
        for name in legacy.VIEWS:
            data = (self.root / name).read_bytes()
            self.assertLessEqual(len(data), 32768)
            self.assertLessEqual(len(data.splitlines()), 200)

    def test_navigation_has_at_most_50_links_and_recursive_reachable_pages(self):
        with self.conn:
            self.conn.executemany("INSERT INTO views_pages(partition,page,dirty) VALUES('active/fact',?,1)", ((n,) for n in range(1, 2501)))
        docs = views._navigation_documents(self.conn, "active/fact")
        self.assertTrue(any("nav-l2-" in name for name in docs))
        for text in docs.values():
            self.assertLessEqual(len(re.findall(r"\]\(", text)), 50)
        all_links = "\n".join(docs.values())
        for n in range(1, 2501):
            self.assertEqual(all_links.count(f"(page-{n:06d}.md)"), 1)

    def test_manual_page_backup_and_unknown_filename_rejected(self):
        self.rebuild([entry(1)])
        self.page(1).write_text("用户亲自修改过的页面", encoding="utf-8")
        views.update(self.vault, self.conn, entry(1, title="最新标题"), self.warnings)
        backups = [w for w in self.warnings if w["code"] == "MANUAL_VIEW_BACKUP"]
        self.assertEqual(len(backups), 1)
        self.assertEqual(Path(backups[0]["path"]).read_text(encoding="utf-8"), "用户亲自修改过的页面")
        unexpected = self.page(2)
        unexpected.write_text("不能覆盖的自有文件", encoding="utf-8")
        with self.assertRaises(legacy.VaultError) as caught:
            self.rebuild([entry(n) for n in range(1, 52)])
        self.assertEqual(caught.exception.code, "UNOWNED_FILE")
        self.assertEqual(unexpected.read_text(encoding="utf-8"), "不能覆盖的自有文件")
        self.assertTrue(views.status(self.conn)["views_stale"])

    def test_interruption_after_atomic_file_commit_resumes_without_duplicate_backup(self):
        self.rebuild([entry(1)])
        original = self.vault._atomic
        def interrupted(path, data, **kwargs):
            original(path, data, **kwargs)
            if path == self.page(1):
                raise OSError("模拟文件已写入但状态还没完成")
        changed = entry(1, title="已提交的新标题")
        with mock.patch.object(self.vault, "_atomic", side_effect=interrupted):
            with self.assertRaises(OSError):
                views.update(self.vault, self.conn, changed, self.warnings)
        self.assertTrue(views.pending(self.conn))
        views.update(self.vault, self.conn, changed, self.warnings)
        self.assertTrue(views.pending(self.conn))
        self.assertEqual(self.warnings, [])
        self.assertIn("已提交的新标题", self.page(1).read_text(encoding="utf-8"))
        self.rebuild([changed])
        self.assertFalse(views.pending(self.conn))

    def test_navigation_failure_after_content_page_still_reports_stale(self):
        self.rebuild([entry(1)])
        with self.conn:
            self.conn.execute("DELETE FROM views_files WHERE path='USER.md'")
        with self.assertRaises(legacy.VaultError):
            views.update(self.vault, self.conn, entry(1, title="内容已生成"), self.warnings)
        self.assertIn("内容已生成", self.page(1).read_text(encoding="utf-8"))
        self.assertTrue(views.status(self.conn)["views_stale"])
        self.assertEqual(views.status(self.conn)["dirty_content_pages"], 0)

    def test_unrelated_update_cannot_clear_previous_unregistered_navigation_failure(self):
        self.rebuild([entry(1)])
        original = views._write
        def interrupted(vault, conn, name, text, warnings):
            if name == "catalog/active/fact/INDEX.md":
                raise OSError("模拟内容页完成后、导航意图登记前中断")
            return original(vault, conn, name, text, warnings)
        with mock.patch.object(views, "_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                views.update(self.vault, self.conn, entry(2), self.warnings)
        self.assertEqual(views.status(self.conn)["dirty_content_pages"], 0)
        self.assertEqual(views.status(self.conn)["dirty_generated_files"], 0)
        self.assertTrue(views.status(self.conn)["views_stale"])
        views.update(self.vault, self.conn, entry(3, type="note"), self.warnings)
        self.assertTrue(views.status(self.conn)["views_stale"])
        self.rebuild([entry(1), entry(2), entry(3, type="note")])
        self.assertFalse(views.status(self.conn)["views_stale"])

    def test_legacy_adoption_and_resumed_migration_keep_new_hashes(self):
        old = {}
        for name in legacy.VIEWS:
            data = f"旧版虚构总览 {name}".encode("utf-8")
            (self.root / name).write_bytes(data)
            old[name] = legacy.digest(data)
        views.adopt_legacy(self.conn, old)
        self.rebuild([entry(1)])
        hashes = views.file_hashes(self.conn)
        views.adopt_legacy(self.conn, old)
        self.assertEqual(hashes, views.file_hashes(self.conn))
        self.assertEqual(self.warnings, [])

    def test_readonly_check_detects_change_missing_and_unknown_without_writes(self):
        self.rebuild([entry(1)])
        self.page(1).write_text("手工修改", encoding="utf-8")
        (self.root / "USER.md").unlink()
        (self.root / "catalog" / "我的说明.md").write_text("用户文件", encoding="utf-8")
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = views.check(self.vault, self.conn)
        self.assertTrue(result["views_stale"])
        self.assertEqual({e["code"] for e in result["errors"]}, {"VIEW_CHANGED", "MISSING_VIEW", "UNOWNED_FILE"})
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
