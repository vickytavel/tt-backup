#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavernlib.fragments import extract_fragments
from tavernlib.format_card import format_form_text, format_ingest_reply, format_thread_body
from tavernlib.store import LibraryStore, LEGACY_SCHEMA
from tavernlib.taxonomy import SCHEMA_VERSION, THEME_SEEDS


class MigrationTests(unittest.TestCase):
    def test_migrates_legacy_db_without_dropping_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "lib"
        root.mkdir()
        db = root / "library.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            """
            INSERT INTO items (
                type, subtype, title, original_filename, stored_path, sha256, note,
                author, created_at, uncertain
            ) VALUES ('world', NULL, '旧世界书', 'old.json', 'world/old.json', 'abc123legacy', 'n', 'ann', '2026-01-01T00:00:00+00:00', 0)
            """
        )
        conn.commit()
        conn.close()
        (root / "world").mkdir()
        (root / "world" / "old.json").write_text("{}", encoding="utf-8")

        store = LibraryStore(root)
        item = store.get(1)
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "旧世界书")
        self.assertEqual(item["sha256"], "abc123legacy")
        self.assertIn("review_status", item)
        self.assertEqual(item.get("review_status") or "pending", "pending")
        self.assertTrue(any(t["path"] == "现代" for t in store.list_theme_categories("character")))
        tags = store.list_tags("relation")
        self.assertTrue(any(t["name"] == "青梅竹马" for t in tags))
        with store._connect() as c:
            ver = c.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()["value"]
        self.assertEqual(int(ver), SCHEMA_VERSION)
        # Second open is idempotent.
        store2 = LibraryStore(root)
        self.assertEqual(store2.get(1)["title"], "旧世界书")
        self.assertEqual(len(store2.list_items()), 1)


class StoreV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LibraryStore(Path(self.tmp.name) / "lib")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reingest_updates_same_record(self):
        data = b"same-bytes-xyz"
        a, created = self.store.add_file(data, type="other", subtype=None, title="A", original_filename="a.txt", note="first")
        self.assertTrue(created)
        b, created2 = self.store.add_file(
            data, type="other", subtype=None, title="B", original_filename="b.txt", note="second", source_url="https://example.com/x"
        )
        self.assertFalse(created2)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.store.list_items()), 1)
        self.assertEqual(b["note"], "second")
        self.assertEqual(b["source_url"], "https://example.com/x")

    def test_catalog_only_imagegen_has_no_file(self):
        item, created = self.store.add_file(
            b"not-stored",
            type="imagegen",
            subtype=None,
            title="立绘",
            original_filename="look.png",
            catalog_only=True,
            catalog_key="catalog:discord:1:2",
            thumbnail_url="https://cdn.discordapp.com/example.png",
        )
        self.assertTrue(created)
        self.assertTrue(item["catalog_only"])
        self.assertEqual(item["stored_path"], "")
        self.assertEqual(item["thumbnail_url"], "https://cdn.discordapp.com/example.png")
        files = [p for p in (Path(self.tmp.name) / "lib").rglob("*") if p.is_file() and p.suffix != ".db" and "library.db" not in p.name]
        self.assertEqual(files, [])
        self.assertFalse(self.store.is_importable(item))

        again, created2 = self.store.add_file(
            b"ignored",
            type="imagegen",
            subtype=None,
            title="立绘2",
            original_filename="look.png",
            catalog_only=True,
            catalog_key="catalog:discord:1:2",
        )
        self.assertFalse(created2)
        self.assertEqual(again["id"], item["id"])

    def test_tags_themes_links_and_search(self):
        world = json.dumps(
            {
                "name": "见闻",
                "entries": {
                    "1": {"uid": 1, "comment": "文风", "keys": ["文风"], "content": "请用细腻的心理描写，避免说明文。", "enabled": True},
                    "2": {"uid": 2, "comment": "规则", "keys": ["规则"], "content": "禁止 OOC。", "enabled": True},
                },
            },
            ensure_ascii=False,
        ).encode()
        item, _ = self.store.add_file(world, type="world", subtype=None, title="见闻", original_filename="w.json")
        frags = self.store.get_enriched(item["id"])["fragments"]
        self.assertGreaterEqual(len(frags), 2)
        kinds = {f["kind"] for f in frags}
        self.assertIn("style", kinds)
        self.assertIn("rule", kinds)

        self.store.set_item_themes(item["id"], ["世界观设定", "科幻/赛博朋克"], typ="world")
        self.store.set_item_tags(
            item["id"],
            [{"bucket": "relation", "name": "强强", "source": "user"}, {"bucket": "world", "name": "现代"}],
            replace_buckets=["*"],
        )
        card, _ = self.store.add_file(b'{"spec":"chara_card_v2"}', type="character", subtype=None, title="角色A", original_filename="a.json")
        self.store.add_link(card["id"], item["id"], "character_worldbook")
        found = self.store.list_items(query="细腻的心理", enrich=True)
        self.assertTrue(any(x["id"] == item["id"] for x in found))
        by_theme = self.store.list_items(theme="科幻", enrich=True)
        self.assertTrue(any(x["id"] == item["id"] for x in by_theme))
        by_tag = self.store.list_items(tag="强强", bucket="relation", enrich=True)
        self.assertTrue(any(x["id"] == item["id"] for x in by_tag))
        enriched = self.store.get_enriched(item["id"])
        self.assertTrue(any(l["kind"] == "character_worldbook" for l in enriched["links"]))
        wb = self.store.get_enriched(item["id"])
        self.assertEqual(wb.get("worldbook_origin"), "bound")

    def test_change_type_moves_file_and_imagegen_deletes_it(self):
        item, _ = self.store.add_file(b"hello-file", type="other", subtype=None, title="图", original_filename="x.png")
        path = self.store.abspath(item["stored_path"])
        self.assertTrue(path.is_file())
        moved = self.store.change_type(item["id"], "beautify", subtype="background")
        self.assertEqual(moved["type"], "beautify")
        new_path = self.store.abspath(moved["stored_path"])
        self.assertTrue(new_path.is_file())
        self.assertFalse(path.exists())
        self.assertIn("beautify", str(new_path))

        gone = self.store.change_type(item["id"], "imagegen")
        self.assertTrue(gone["catalog_only"])
        self.assertEqual(gone["stored_path"], "")
        self.assertFalse(new_path.exists())

    def test_user_tag_clears_pending_and_blacklist_blocks(self):
        item, _ = self.store.add_file(b"z", type="other", subtype=None, title="z", original_filename="z.txt")
        self.store.set_item_tags(item["id"], [{"bucket": "plot", "name": "新造标签", "source": "ai"}], replace_buckets=["plot"])
        tags = self.store.list_tags("plot", include_blacklisted=True)
        created = next(t for t in tags if t["name"] == "新造标签")
        self.assertTrue(created["pending"])
        self.store.set_item_tags(item["id"], [{"bucket": "plot", "name": "新造标签", "source": "user"}], replace_buckets=["plot"])
        created = next(t for t in self.store.list_tags("plot") if t["name"] == "新造标签")
        self.assertFalse(created["pending"])
        self.store.set_tag_flags(created["id"], blacklisted=True)
        self.store.set_item_tags(item["id"], [{"bucket": "plot", "name": "新造标签", "source": "user"}], replace_buckets=["plot"])
        enriched = self.store.get_enriched(item["id"])
        self.assertFalse(any(t["name"] == "新造标签" for t in enriched["tags"]))

    def test_merge_tags(self):
        a = self.store.ensure_tag("plot", "自定义甲")
        b = self.store.ensure_tag("plot", "自定义乙")
        item, _ = self.store.add_file(b"m", type="other", subtype=None, title="m", original_filename="m.txt")
        self.store.set_item_tags(item["id"], [{"bucket": "plot", "name": "自定义甲", "source": "user"}], replace_buckets=["plot"])
        self.store.merge_tags(b["id"], a["id"])
        enriched = self.store.get_enriched(item["id"])
        names = [t["name"] for t in enriched["tags"]]
        self.assertIn("自定义乙", names)
        self.assertNotIn("自定义甲", names)

    def test_bundle_and_metadata_patch(self):
        a, _ = self.store.add_file(b"ba", type="character", subtype=None, title="卡", original_filename="a.json")
        b, _ = self.store.add_file(b"bb", type="world", subtype=None, title="书", original_filename="b.json")
        bid = self.store.set_bundle([a["id"], b["id"]])
        self.assertTrue(bid)
        self.assertEqual(self.store.get(a["id"])["bundle_id"], bid)
        updated = self.store.apply_metadata(
            a["id"],
            {
                "title": "新标题",
                "summary": "简介",
                "license_repost": "allowed",
                "recommended_models": "Claude, GPT",
                "themes": ["现代"],
                "tags": [{"bucket": "relation", "name": "青梅竹马"}],
                "review_status": "confirmed",
            },
        )
        self.assertEqual(updated["title"], "新标题")
        self.assertEqual(updated["license_repost"], "allowed")
        self.assertEqual(updated["recommended_models"], ["Claude", "GPT"])
        self.assertEqual(updated["themes"], ["现代"])
        self.assertEqual(updated["review_status"], "confirmed")
        self.assertTrue(any(t["name"] == "青梅竹马" for t in updated["tags"]))

    def test_extra_worldbook_link(self):
        main, _ = self.store.add_file(b"main-wb", type="world", subtype=None, title="主世界", original_filename="m.json")
        extra, _ = self.store.add_file(b"extra-wb", type="world", subtype=None, title="番外", original_filename="e.json")
        self.store.add_link(main["id"], extra["id"], "worldbook_extra")
        self.assertEqual(self.store.get(extra["id"])["worldbook_origin"], "extra")


class FragmentTests(unittest.TestCase):
    def test_world_and_preset_and_character_book(self):
        world = {
            "name": "W",
            "entries": {
                "1": {"comment": "文风指导", "content": "用短句。", "keys": ["文风"]},
                "2": {"comment": "玩法规则", "content": "先扔骰子。", "keys": ["规则"]},
            },
        }
        frags = extract_fragments(world, "world")
        kinds = {f["kind"] for f in frags}
        self.assertEqual(kinds, {"style", "rule"})

        preset = {"name": "文风预设", "content": "# 文风\n请细写动作。\n# 规则\n不要 OOC。"}
        pfrags = extract_fragments(preset, "preset")
        self.assertTrue(any(f["kind"] == "style" for f in pfrags))
        self.assertTrue(any(f["kind"] == "rule" for f in pfrags))

        card = {"spec": "chara_card_v2", "data": {"name": "A", "character_book": {"entries": [{"comment": "设定", "content": "这是世界观补充。"}]}}}
        cfrags = extract_fragments(card, "character")
        self.assertTrue(cfrags)
        self.assertTrue(any(f["kind"] == "setting" for f in cfrags))


class FormatTests(unittest.TestCase):
    def test_thread_and_form_and_reply_text(self):
        item = {
            "id": 1,
            "title": "测试卡",
            "type": "character",
            "author": "ann",
            "review_status": "pending",
            "license_repost": "allowed",
            "themes": ["现代"],
            "tags": [{"bucket": "relation", "name": "青梅竹马"}],
            "recommended_models": ["X"],
        }
        body = format_thread_body(item)
        self.assertIn("测试卡", body)
        self.assertIn("青梅竹马", body)
        form = format_form_text(item)
        self.assertIn("整理资源", form)
        self.assertIn("尚未启用自动打标", form)
        reply = format_ingest_reply("测试卡", created=True, jump="https://discord.com/x", typ="character", uncertain=True)
        self.assertIn("已入库", reply)
        self.assertIn("请在子区里", reply)
        exists = format_ingest_reply("测试卡", created=False, jump="https://discord.com/x")
        self.assertIn("已存在", exists)

    def test_theme_seeds_cover_agreed_types(self):
        for typ in ("character", "persona", "preset", "world", "beautify", "imagegen", "style"):
            self.assertTrue(THEME_SEEDS[typ])


class ImportBeautifyTests(unittest.TestCase):
    def test_beautify_theme_and_imagegen_import_rules(self):
        import os
        from tavernlib.import_st import ImportError_, copy_local, dest_dir_for, import_item

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        lib = LibraryStore(base / "lib")
        st = base / "st"
        (st / "themes").mkdir(parents=True)
        (st / "backgrounds").mkdir(parents=True)
        os.environ["TAVERN_IMPORT_LOCAL"] = "1"
        self.addCleanup(lambda: os.environ.pop("TAVERN_IMPORT_LOCAL", None))

        css, _ = lib.add_file(b"body{color:#fff;background:#000}", type="beautify", subtype="css", title="皮肤", original_filename="skin.css")
        self.assertEqual(dest_dir_for(css, st).name, "themes")
        result = import_item(lib, css["id"], st_root=st, copy_fn=copy_local)
        self.assertTrue((st / "themes" / result["dest_name"]).is_file())

        img, _ = lib.add_file(b"png-bytes", type="imagegen", subtype=None, title="图", original_filename="a.png", catalog_only=True, catalog_key="catalog:t")
        with self.assertRaises(ImportError_):
            import_item(lib, img["id"], st_root=st, copy_fn=copy_local)


if __name__ == "__main__":
    unittest.main()
