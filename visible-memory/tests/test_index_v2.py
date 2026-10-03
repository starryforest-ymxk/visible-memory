"""Index/overlay contract tests using only synthetic temporary Markdown vaults."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import uuid

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import memory_v1 as base
import vm_index as index


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="虚构索引 ", dir=SKILL_ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("entries", "history", "_state"):
            (self.root / name).mkdir()
        (self.root / "_state" / "vault.json").write_text(json.dumps({"vault_id": str(uuid.uuid4())}), encoding="utf-8")
        self.vault = base.Vault(self.root)
        self.counter = 0

    def record(self, **changes):
        self.counter += 1
        entry = {"id": str(uuid.UUID(int=self.counter)), "title": "虚构中文偏好", "type": "preference",
                 "status": "active", "tags": ["沟通", "精确标签"], "source": "合成测试出处，不是真实记忆",
                 "scope": "测试范围", "created_at": "2026-09-06T12:00:00+08:00",
                 "updated_at": "2026-09-06T12:00:00+08:00", "revision": 1,
                 "body": "连续中文简洁回答，代码审查需要事实证据。ABC UI C++ café。"}
        entry.update(changes)
        return self.write(entry)

    def write(self, entry):
        path = self.root / "entries" / (entry["id"] + ".md")
        raw = base.render_entry(entry)
        path.write_bytes(raw)
        return entry, raw, path

    def build(self, records=None):
        if records is None:
            records, errors = self.vault._scan()
            self.assertEqual(errors, [])
        index.build(self.vault, records)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def ids(self, result):
        return [entry["id"] for entry in result["entries"]]

    def oracle(self, query="", include_archived=False, entry_type=None, tags=None, scope=None):
        terms = base.normalized(query).split()
        rows, _ = self.vault._scan()
        out = []
        for entry, _, _ in rows:
            if not include_archived and entry["status"] != "active":
                continue
            if entry_type is not None and entry["type"] != entry_type:
                continue
            if scope is not None and entry["scope"] != scope:
                continue
            if any(tag not in entry["tags"] for tag in (tags or [])):
                continue
            title = base.normalized(entry["title"])
            tag_text = base.normalized(" ".join(entry["tags"]))
            body = base.normalized(entry["body"] + " " + entry["scope"])
            score = sum(5 * (term in title) + 3 * (term in tag_text) + (term in body) for term in terms)
            if not terms or score:
                out.append((score, entry["updated_at"], entry["id"]))
        return [row[2] for row in sorted(out, reverse=True)]

    def test_oracle_unicode_short_long_punctuation_or_ranking_and_exact_total(self):
        self.record(title="中文 UI", body='简洁回答 英文 ＡＢＣ straße café 漢字 "abc" c++ a*b a?b 记忆')
        self.record(title="非标题", tags=["简洁回答"], body="代码审查 中文 UI")
        self.record(title="body only", body="简洁回答 abc abcde 代码审查 中文")
        self.record(status="archived", title="中文 简洁回答")
        self.record(body="末尾\x00零字符后文 中文\x00记忆")
        self.build()
        for query in ["中", "中文", "简洁回答", "UI", "ABC", "abcde", "Ｃ＋＋", '"abc"',
                      "a*b", "a?b", "代码审查 中文", "简洁回答 简洁回答 abc", "STRASSE", "CAFÉ", "不存在", "零字符后文", "中文\x00记忆"]:
            with self.subTest(query=query):
                result = index.search(self.vault, query, limit=100)
                self.assertEqual(self.ids(result), self.oracle(query))
                self.assertEqual(result["total"], len(self.oracle(query)))
                self.assertEqual(result["mode"], "indexed")

    def test_randomized_substrings_match_oracle(self):
        rng = random.Random(2409)
        alphabet = "中文事实偏好修改AaＢ1+*?\"éß，"
        texts = ["".join(rng.choice(alphabet) for _ in range(90)) for _ in range(40)]
        for position, text in enumerate(texts):
            self.record(title=text[:12], body=text, status="archived" if position % 7 == 0 else "active")
        self.build()
        for _ in range(60):
            text = rng.choice(texts)
            begin = rng.randrange(len(text) - 8)
            query = text[begin:begin + rng.randrange(1, 8)]
            self.assertEqual(self.ids(index.search(self.vault, query, limit=100)), self.oracle(query), query)

    def test_exact_filters_and_repeated_tags_are_and(self):
        self.record(tags=["ABC", "中文"], scope="项目A", type="fact")
        self.record(tags=["abc", "中文"], scope="项目A", type="fact")
        self.record(tags=["ABC"], scope="项目A 子项目", type="fact")
        self.record(tags=["ABC", "中文"], scope="项目A", type="decision")
        self.build()
        filters = {"entry_type": "fact", "tags": ["ABC", "中文", "ABC"], "scope": "项目A"}
        result = index.search(self.vault, **filters)
        self.assertEqual(self.ids(result), self.oracle(**filters))
        self.assertEqual(result["total"], 1)

    def test_overlay_new_changed_deleted_archive_and_filters(self):
        first, _, _ = self.record(body="无目标")
        second, _, _ = self.record(body="目标关键词")
        removed, _, removed_path = self.record(body="目标关键词")
        self.build()
        self.write({**first, "body": "后来加入目标关键词", "tags": ["新标签"], "scope": "新范围", "type": "fact"})
        self.write({**second, "status": "archived"})
        removed_path.unlink()
        self.record(body="新增目标关键词")
        before = self.snapshot()
        result = index.search(self.vault, "目标关键词", limit=100)
        self.assertEqual(self.ids(result), self.oracle("目标关键词"))
        self.assertEqual(result["changes"], {"added": 1, "changed": 2, "deleted": 1})
        filters = {"tags": ["新标签"], "entry_type": "fact", "scope": "新范围"}
        self.assertEqual(self.ids(index.search(self.vault, "目标关键词", **filters)), self.oracle("目标关键词", **filters))
        self.assertEqual(self.snapshot(), before)

    def test_pagination_applies_after_all_base_and_overlay_matches(self):
        for position in range(120):
            self.record(title=f"第{position}项", body="共享查询关键词")
        self.build()
        self.record(title="共享查询关键词", body="变化覆盖层最高分")
        expected = self.oracle("共享查询关键词")
        result = index.search(self.vault, "共享查询关键词", offset=79, limit=8)
        self.assertEqual(result["total"], 121)
        self.assertEqual(self.ids(result), expected[79:87])

    def test_unchanged_nonhits_do_not_open_markdown(self):
        records = [self.record(title=f"其他{i}", body="普通正文") for i in range(40)]
        selected = self.record(title="唯一命中词", body="特殊正文")
        self.build([*records, selected])
        with mock.patch.object(self.vault, "_raw", wraps=self.vault._raw) as reads:
            self.assertEqual(index.search(self.vault, "唯一命中词")["total"], 1)
        entry_reads = [Path(call.args[0]).name for call in reads.call_args_list if Path(call.args[0]).parent.name == "entries"]
        self.assertEqual(entry_reads, [selected[2].name])

    def test_matched_fields_terms_and_excerpt_are_bounded(self):
        self.record(title="标题词", tags=["标签词"], scope="范围词", body="前缀" * 250 + "正文词" + "末尾" * 200)
        self.build()
        result = index.search(self.vault, "正文词 标题词 标签词 范围词 正文词")
        row = result["entries"][0]
        self.assertEqual(row["matched_terms"], ["正文词", "标题词", "标签词", "范围词"])
        self.assertEqual(row["matched_fields"], ["title", "tags", "body", "scope"])
        self.assertIn("正文词", row["excerpt"])
        self.assertLessEqual(len(row["excerpt"]), 240)
        self.assertEqual(result["verification"], "metadata_plus_selected_sha256")

    def test_invalid_overlay_suppresses_cached_result(self):
        entry, _, path = self.record(body="目标词")
        self.build()
        path.write_text("格式损坏且修改大小", encoding="utf-8")
        result = index.search(self.vault, "目标词")
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["complete"])
        self.assertTrue(result["errors"])

    def test_reads_create_no_files_or_sidecars(self):
        self.record()
        self.build()
        records, _ = self.vault._scan()
        before = self.snapshot()
        index.search(self.vault, "中文")
        index.search(self.vault)
        index.status(self.vault)
        index.check(self.vault, records)
        list(index.iter_entries(self.vault))
        index.duplicates(self.vault, records[0][0])
        with contextlib.closing(index.connect(self.vault)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM documents")
        self.assertEqual(before, self.snapshot())

    def test_missing_corrupt_and_sidecar_index_fall_back_without_repair(self):
        self.record()
        result = index.search(self.vault, "中文")
        self.assertEqual(result["mode"], "full_scan_fallback")
        self.assertFalse((self.root / "_index").exists())
        self.build()
        db = self.root / "_index" / "search.sqlite"
        good = db.read_bytes()
        db.write_bytes(b"intentionally damaged synthetic db")
        before = self.snapshot()
        self.assertEqual(index.search(self.vault, "中文")["total"], 1)
        self.assertFalse(index.status(self.vault)["index_available"])
        self.assertEqual(before, self.snapshot())
        db.write_bytes(good)
        sidecar = Path(str(db) + "-journal")
        sidecar.write_bytes(b"do not recover")
        before = self.snapshot()
        self.assertEqual(index.search(self.vault, "中文")["mode"], "full_scan_fallback")
        self.assertEqual(before, self.snapshot())

    def test_upsert_replaces_old_fts_shortgrams_tags_and_preserves_other_row(self):
        first, _, _ = self.record(title="旧词", tags=["旧标签"], body="原始旧长词")
        second = self.record(title="保留")
        self.build()
        new = self.write({**first, "title": "新词", "tags": ["新标签"], "body": "更新新长词", "revision": 2})
        index.upsert(self.vault, *new)
        self.assertEqual(index.search(self.vault, "旧词 原始旧长词")["total"], 0)
        self.assertEqual(index.search(self.vault, "新词 更新新长词", tags=["新标签"])["total"], 1)
        self.assertEqual(index.search(self.vault, tags=["旧标签"])["total"], 0)
        self.assertEqual(list(index.iter_entries(self.vault, [second[0]["id"]]))[0][1], base.digest(second[1]))
        self.assertEqual(index.status(self.vault)["changes"], {"added": [], "changed": [], "deleted": []})

    def test_wal_header_is_rejected_before_sqlite_can_create_sidecars(self):
        self.record()
        self.build()
        db = self.root / "_index" / "search.sqlite"
        with contextlib.closing(sqlite3.connect(db)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.assertFalse(Path(str(db) + "-shm").exists())
        before = self.snapshot()
        self.assertEqual(index.search(self.vault, "中文")["mode"], "full_scan_fallback")
        self.assertEqual(before, self.snapshot())

    def test_missing_index_table_marks_status_and_check_unavailable(self):
        self.record()
        self.build()
        with contextlib.closing(index.connect(self.vault, readonly=False)) as connection:
            connection.execute("DROP TABLE short_grams")
        before = self.snapshot()
        self.assertFalse(index.status(self.vault)["index_available"])
        self.assertFalse(index.check(self.vault, self.vault._scan()[0])["index_available"])
        self.assertEqual(index.search(self.vault, "中文")["mode"], "full_scan_fallback")
        self.assertEqual(before, self.snapshot())

    def test_full_check_detects_damaged_cached_filters_and_search_text(self):
        record = self.record()
        self.build()
        with contextlib.closing(index.connect(self.vault, readonly=False)) as connection:
            connection.execute("UPDATE documents SET scope='错误缓存范围',body_norm='错误缓存正文'")
        result = index.check(self.vault, self.vault._scan()[0])
        self.assertFalse(result["index_consistent"])
        self.assertEqual(result["index_changes"]["changed"], [record[0]["id"]])

    def test_full_check_finds_preserved_size_and_timestamp_change(self):
        entry, _, path = self.record(body="旧文")
        self.build()
        info = path.stat()
        self.write({**entry, "body": "新文"})
        self.assertEqual(path.stat().st_size, info.st_size)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(index.status(self.vault)["changes"]["changed"], [])
        records, _ = self.vault._scan()
        result = index.check(self.vault, records)
        self.assertFalse(result["index_consistent"])
        self.assertEqual(result["index_changes"]["changed"], [entry["id"]])

    def test_top_readback_retries_once_for_hidden_edit(self):
        entry, _, path = self.record(body="旧文")
        self.build()
        info = path.stat()
        self.write({**entry, "body": "新文"})
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        result = index.search(self.vault, "旧文")
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["index_stale"])

    def test_top_keeps_changing_bounded_retry_raises(self):
        entry, _, _ = self.record(body="目标词")
        self.build()
        original = index._read_source
        calls = 0
        def changing(vault, entry_id):
            nonlocal calls
            calls += 1
            current, raw, path, info = original(vault, entry_id)
            return {**current, "body": f"目标词 变化{calls}"}, raw + str(calls).encode(), path, info
        with mock.patch.object(index, "_read_source", side_effect=changing):
            with self.assertRaises(base.VaultError) as caught:
                index.search(self.vault, "目标词")
        self.assertEqual(caught.exception.code, "CONCURRENT_EDIT")
        self.assertLessEqual(calls, 3)

    def test_duplicates_find_overlay_and_never_use_archived(self):
        original, _, _ = self.record(body="原文")
        self.build()
        changed = {**original, "body": "变化之后"}
        self.write(changed)
        before = self.snapshot()
        self.assertEqual(index.duplicates(self.vault, changed)[0]["id"], original["id"])
        self.assertIsNone(index.duplicates(self.vault, original))
        self.assertEqual(before, self.snapshot())
        self.write({**changed, "status": "archived"})
        self.assertIsNone(index.duplicates(self.vault, changed))

    def test_build_rejects_changed_input_and_keeps_old_index(self):
        record = self.record(body="原文")
        self.build([record])
        previous = (self.root / "_index" / "search.sqlite").read_bytes()
        self.write({**record[0], "body": "已经变化"})
        with self.assertRaises(base.VaultError):
            self.build([record])
        self.assertEqual((self.root / "_index" / "search.sqlite").read_bytes(), previous)
        self.assertEqual([p.name for p in (self.root / "_index").iterdir()], ["search.sqlite"])

    def test_readonly_unsafe_hardlinked_database_rejected(self):
        self.record()
        self.build()
        db = self.root / "_index" / "search.sqlite"
        duplicate_link = self.root / "synthetic-hardlink.sqlite"
        try:
            os.link(db, duplicate_link)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(base.VaultError) as caught:
            index.search(self.vault, "中文")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")

    def test_initial_build_refuses_unknown_main_and_sidecar(self):
        record = self.record()
        folder = self.root / "_index"
        folder.mkdir()
        for suffix in ("", "-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                unknown = folder / ("search.sqlite" + suffix)
                unknown.write_bytes(b"unknown pre-existing user file")
                before = self.snapshot()
                with mock.patch.object(index, "_file_hash", side_effect=AssertionError("unowned bytes must not be read")):
                    with self.assertRaises(base.VaultError) as caught:
                        index.build(self.vault, [record], allow_replace=False)
                self.assertEqual(caught.exception.code, "UNOWNED_INDEX")
                self.assertEqual(before, self.snapshot())
                unknown.unlink()
        index.build(self.vault, [record], allow_replace=False)
        self.assertEqual(index.search(self.vault, "中文")["mode"], "indexed")

    def test_explicit_rebuild_retains_main_copy_and_moves_recovery_sidecars(self):
        record = self.record()
        self.build()
        db = self.root / "_index" / "search.sqlite"
        original_main = db.read_bytes()
        sides = {suffix: ("synthetic interrupted " + suffix).encode() for suffix in index.SIDECARS}
        for suffix, raw in sides.items():
            Path(str(db) + suffix).write_bytes(raw)
        before = self.snapshot()
        self.assertEqual(index.search(self.vault, "中文")["mode"], "full_scan_fallback")
        self.assertEqual(before, self.snapshot())
        result = index.build(self.vault, [record], allow_replace=True)
        backups = {Path(path).name: Path(path).read_bytes() for path in result["recovery_backups"]}
        self.assertEqual(backups["search.sqlite"], original_main)
        for suffix, raw in sides.items():
            self.assertEqual(backups["search.sqlite" + suffix], raw)
            self.assertFalse(Path(str(db) + suffix).exists())
        self.assertFalse((db.parent / index.REBUILD_MARKER).exists())
        self.assertEqual(index.search(self.vault, "中文")["mode"], "indexed")
        before = self.snapshot()
        index.search(self.vault, "中文")
        self.assertEqual(before, self.snapshot())

    def test_real_hot_journal_is_preserved_without_sqlite_recovery(self):
        import subprocess
        record = self.record()
        self.build()
        db = self.root / "_index" / "search.sqlite"
        code = "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE documents SET scope='uncommitted'\"); os._exit(0)"
        subprocess.run([sys.executable, "-B", "-c", code, str(db)], check=True)
        journal = Path(str(db) + "-journal")
        self.assertTrue(journal.exists())
        main_bytes, journal_bytes = db.read_bytes(), journal.read_bytes()
        result = index.build(self.vault, [record], allow_replace=True)
        backups = {Path(path).name: Path(path).read_bytes() for path in result["recovery_backups"]}
        self.assertEqual(backups[db.name], main_bytes)
        self.assertEqual(backups[journal.name], journal_bytes)
        self.assertEqual(index.search(self.vault, scope="测试范围")["total"], 1)

    def test_failed_publication_keeps_marker_and_backups_then_explicit_retry_recovers(self):
        record = self.record()
        self.build()
        db = self.root / "_index" / "search.sqlite"
        journal = Path(str(db) + "-journal")
        journal.write_bytes(b"retain interrupted cache bytes")
        original_main = db.read_bytes()
        original_replace = os.replace
        def fail_publication(source, target):
            if Path(target) == db and Path(source).name.startswith(".build-"):
                raise OSError("simulated main DB publication failure")
            return original_replace(source, target)
        with mock.patch.object(index.os, "replace", side_effect=fail_publication):
            with self.assertRaises(OSError):
                index.build(self.vault, [record])
        self.assertEqual(db.read_bytes(), original_main)
        self.assertTrue((db.parent / index.REBUILD_MARKER).exists())
        before = self.snapshot()
        self.assertEqual(index.search(self.vault, "中文")["mode"], "full_scan_fallback")
        self.assertEqual(before, self.snapshot())
        result = index.build(self.vault, [record])
        backups = {Path(path).name: Path(path).read_bytes() for path in result["recovery_backups"]}
        self.assertEqual(backups["search.sqlite"], original_main)
        self.assertEqual(backups["search.sqlite-journal"], b"retain interrupted cache bytes")
        self.assertFalse((db.parent / index.REBUILD_MARKER).exists())
        self.assertEqual(index.search(self.vault, "中文")["mode"], "indexed")

    def test_index_from_another_vault_is_not_used(self):
        self.record()
        self.build()
        header = self.root / "_state" / "vault.json"
        header.write_text(json.dumps({"vault_id": str(uuid.uuid4())}), encoding="utf-8")
        before = self.snapshot()
        result = index.search(self.vault, "中文")
        self.assertEqual(result["mode"], "full_scan_fallback")
        self.assertIn("其他记忆库", result["warnings"][0]["message"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
