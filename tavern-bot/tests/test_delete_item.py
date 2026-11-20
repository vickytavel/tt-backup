#!/usr/bin/env python3
"""delete_item: rows and related tables cleared, stored file moved to .trash."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavernlib.store import LibraryStore


class DeleteItemTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "lib"
        self.store = LibraryStore(self.root)

    def _add(self, title: str) -> dict:
        item, created = self.store.add_file(
            b"delete-me-" + title.encode("utf-8"),
            type="other",
            subtype=None,
            title=title,
            original_filename=f"{title}.txt",
        )
        assert created
        return item

    def _count(self, sql: str, args: tuple = ()) -> int:
        with self.store._connect() as conn:
            return int(conn.execute(sql, args).fetchone()[0])

    def test_delete_moves_file_to_trash_and_clears_rows(self):
        item = self._add("Alpha")
        iid = item["id"]
        self.store.set_item_tags(iid, [{"bucket": "world", "name": "豪门世家"}], source="user")
        self.store.replace_fragments(
            iid, [{"kind": "card", "title": "简介", "excerpt": "一段文字", "source_ref": ""}]
        )
        stored = self.store.abspath(item["stored_path"])
        self.assertTrue(stored.is_file())
        result = self.store.delete_item(iid)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], iid)
        self.assertEqual(result["title"], "Alpha")
        self.assertTrue(result["moved_to"].startswith(".trash/"))
        trash_file = self.root / result["moved_to"]
        self.assertTrue(trash_file.is_file())
        self.assertEqual(trash_file.read_bytes(), b"delete-me-Alpha")
        self.assertFalse(stored.exists())
        self.assertIsNone(self.store.get(iid))
        self.assertEqual(self._count("SELECT COUNT(*) FROM item_tags WHERE item_id = ?", (iid,)), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM fragments WHERE item_id = ?", (iid,)), 0)

    def test_delete_unknown_id_returns_none(self):
        self.assertIsNone(self.store.delete_item(99999))

    def test_delete_with_missing_file_still_clears_db(self):
        item = self._add("Ghost")
        stored = self.store.abspath(item["stored_path"])
        stored.unlink()
        result = self.store.delete_item(item["id"])
        self.assertIsNotNone(result)
        self.assertEqual(result["moved_to"], "")
        self.assertIsNone(self.store.get(item["id"]))

    def test_delete_clears_pieces_and_piece_tags(self):
        item = self._add("Pieces")
        iid = item["id"]
        self.store.replace_pieces(iid, [{"kind": "qr", "ord": 0, "title": "Q1", "text": "快回复一"}])
        pieces = self.store.pieces(iid)
        self.assertEqual(len(pieces), 1)
        self.store.set_piece_tags(pieces[0]["id"], [{"bucket": "setting", "name": "旧伤"}])
        self.assertEqual(self._count("SELECT COUNT(*) FROM piece_tags"), 1)
        result = self.store.delete_item(iid)
        self.assertIsNotNone(result)
        self.assertEqual(self._count("SELECT COUNT(*) FROM pieces WHERE item_id = ?", (iid,)), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM piece_tags"), 0)

    def test_delete_clears_links_and_themes(self):
        a = self._add("A")
        b = self._add("B")
        self.store.add_link(a["id"], b["id"], "bundle", "")
        self.store.add_item_theme(a["id"], "现代/都市")
        result = self.store.delete_item(a["id"])
        self.assertIsNotNone(result)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM item_links WHERE from_id = ? OR to_id = ?", (a["id"], a["id"])), 0
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM item_themes WHERE item_id = ?", (a["id"],)), 0)
        self.assertIsNotNone(self.store.get(b["id"]))

    def test_delete_does_not_touch_other_items(self):
        a = self._add("Keep1")
        b = self._add("Keep2")
        self.store.delete_item(a["id"])
        kept = self.store.get(b["id"])
        self.assertEqual(kept["title"], "Keep2")
        self.assertTrue(self.store.abspath(kept["stored_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
