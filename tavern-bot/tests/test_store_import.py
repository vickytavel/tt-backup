#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavernlib.import_st import copy_local, dest_dir_for, import_item, import_set, ImportError_
from tavernlib.pngutil import read_chara_json, wrap_card_as_png
from tavernlib.store import LibraryStore, safe_filename, unique_path


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "lib"
        self.store = LibraryStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_dedup_by_sha(self):
        data = b"hello-resource"
        a, created = self.store.add_file(
            data, type="other", subtype=None, title="Hello", original_filename="hello.txt", note="n"
        )
        self.assertTrue(created)
        b, created2 = self.store.add_file(
            data, type="other", subtype=None, title="Hello again", original_filename="hello2.txt"
        )
        self.assertFalse(created2)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.store.list_items()), 1)

    def test_unique_path_never_overwrites(self):
        d = self.root / "scratch"
        p1 = unique_path(d, "note.txt")
        p1.write_text("1")
        p2 = unique_path(d, "note.txt")
        self.assertNotEqual(p1, p2)
        self.assertFalse(p2.exists())

    def test_safe_filename_strips_separators(self):
        self.assertNotIn("/", safe_filename("a/b\\c:d"))
        self.assertEqual(safe_filename("..."), "unnamed")

    def test_abspath_rejects_escape(self):
        with self.assertRaises(ValueError):
            self.store.abspath("../etc/passwd")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.lib = LibraryStore(base / "lib")
        self.st = base / "st"
        for name in (
            "characters",
            "worlds",
            "OpenAI Settings",
            "instruct",
            "context",
            "sysprompt",
            "reasoning",
            "KoboldAI Settings",
            "NovelAI Settings",
            "TextGen Settings",
            "QuickReplies",
            "themes",
            "User Avatars",
        ):
            (self.st / name).mkdir(parents=True)
        os.environ["TAVERN_IMPORT_LOCAL"] = "1"

    def tearDown(self):
        os.environ.pop("TAVERN_IMPORT_LOCAL", None)
        self.tmp.cleanup()

    def _import(self, item_id, confirm=False):
        return import_item(
            self.lib,
            item_id,
            st_root=self.st,
            copy_fn=copy_local,
            confirm_again=confirm,
        )

    def test_character_png_import_and_suffix(self):
        card = json.dumps(
            {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "测试角色", "description": "d", "first_mes": "hi"}},
            ensure_ascii=False,
        )
        png = wrap_card_as_png(card)
        item, _ = self.lib.add_file(png, type="character", subtype=None, title="测试角色", original_filename="t.png")
        result = self._import(item["id"])
        self.assertTrue(result["ok"])
        dest = self.st / "characters" / result["dest_name"]
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.stat().st_mode & 0o777, 0o644)
        self.assertIsNotNone(read_chara_json(dest.read_bytes()))

        # Second import without confirm is refused; with confirm uses _2.
        from tavernlib.import_st import ImportError_ as IE

        with self.assertRaises(IE) as ctx:
            self._import(item["id"], confirm=False)
        self.assertEqual(str(ctx.exception), "already_imported")
        result2 = self._import(item["id"], confirm=True)
        self.assertEqual(result2["dest_name"], "测试角色_2.png")
        self.assertTrue((self.st / "characters" / "测试角色_2.png").is_file())
        self.assertTrue((self.st / "characters" / "测试角色.png").is_file())

    def test_json_character_is_wrapped_as_png(self):
        payload = json.dumps(
            {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "JSON卡", "description": "d", "first_mes": "hi"}},
            ensure_ascii=False,
        ).encode("utf-8")
        item, _ = self.lib.add_file(payload, type="character", subtype=None, title="JSON卡", original_filename="j.json")
        result = self._import(item["id"])
        self.assertTrue(result["dest_name"].endswith(".png"))
        dest = self.st / "characters" / result["dest_name"]
        extracted = read_chara_json(dest.read_bytes())
        self.assertIsNotNone(extracted)
        self.assertIn("JSON卡", extracted)

    def test_world_and_preset_and_persona(self):
        world = json.dumps({"name": "见闻", "entries": {"1": {"uid": 1, "keys": ["a"], "content": "b", "enabled": True}}}).encode()
        w, _ = self.lib.add_file(world, type="world", subtype=None, title="见闻", original_filename="w.json")
        r = self._import(w["id"])
        self.assertEqual(r["dest_name"], "见闻.json")
        self.assertTrue((self.st / "worlds" / "见闻.json").is_file())

        preset = json.dumps({"chat_completion_source": "openai", "openai_model": "x"}).encode()
        p, _ = self.lib.add_file(preset, type="preset", subtype="openai", title="Default", original_filename="Default.json")
        r = self._import(p["id"])
        self.assertTrue((self.st / "OpenAI Settings" / "Default.json").is_file())

        from tavernlib.pngutil import make_solid_png

        img, _ = self.lib.add_file(make_solid_png(), type="persona", subtype=None, title="我的面具", original_filename="me.png")
        r = self._import(img["id"])
        self.assertTrue((self.st / "User Avatars" / r["dest_name"]).is_file())

    def test_other_not_importable(self):
        from tavernlib.import_st import ImportError_ as IE

        item, _ = self.lib.add_file(b"{}\n", type="other", subtype=None, title="chat", original_filename="c.jsonl")
        with self.assertRaises(IE):
            self._import(item["id"])

    def test_dest_dir_map(self):
        self.assertEqual(dest_dir_for({"type": "preset", "subtype": "instruct"}, self.st).name, "instruct")
        self.assertIsNone(dest_dir_for({"type": "other", "subtype": None}, self.st))


if __name__ == "__main__":
    unittest.main()

class ImportSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.lib = LibraryStore(base / "lib")
        self.st = base / "st"
        for name in ("characters", "worlds", "OpenAI Settings", "QuickReplies", "themes", "User Avatars"):
            (self.st / name).mkdir(parents=True)
        os.environ["TAVERN_IMPORT_LOCAL"] = "1"

    def tearDown(self):
        os.environ.pop("TAVERN_IMPORT_LOCAL", None)
        self.tmp.cleanup()

    def _set(self, item_id, confirm=False):
        return import_set(self.lib, item_id, st_root=self.st, copy_fn=copy_local, confirm_again=confirm)

    def _card(self, title):
        card = json.dumps(
            {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": title, "description": "d", "first_mes": "hi"}},
            ensure_ascii=False,
        )
        item, _ = self.lib.add_file(wrap_card_as_png(card), type="character", subtype=None, title=title, original_filename=f"{title}.png")
        return item

    def _world(self, title):
        data = json.dumps({"name": title, "entries": {"1": {"uid": 1, "keys": ["a"], "content": "b", "enabled": True}}}).encode()
        item, _ = self.lib.add_file(data, type="world", subtype=None, title=title, original_filename=f"{title}.json")
        return item

    def _qr(self, title):
        data = json.dumps({"version": 2, "name": title, "qrList": [{"label": "hi", "message": "/echo hi"}]}, ensure_ascii=False).encode()
        item, _ = self.lib.add_file(data, type="preset", subtype="quick-replies", title=title, original_filename=f"{title}.json")
        return item

    def test_import_set_bundles_card_world_qr(self):
        card = self._card("甲")
        world = self._world("甲的世界")
        qr = self._qr("甲的QR")
        self.lib.set_bundle([card["id"], world["id"], qr["id"]])
        result = self._set(card["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["imported"], 3)
        self.assertEqual(result["summary"]["skipped"], 0)
        self.assertEqual(result["anchor"]["status"], "imported")
        self.assertTrue((self.st / "characters" / "甲.png").is_file())
        self.assertTrue((self.st / "worlds" / "甲的世界.json").is_file())
        self.assertTrue((self.st / "QuickReplies" / "甲的QR.json").is_file())

    def test_import_set_excludes_preset_and_other_card(self):
        card = self._card("乙")
        preset, _ = self.lib.add_file(
            json.dumps({"chat_completion_source": "openai"}).encode(),
            type="preset",
            subtype="openai",
            title="P",
            original_filename="p.json",
        )
        other = self._card("丙")
        self.lib.set_bundle([card["id"], preset["id"], other["id"]])
        result = self._set(card["id"])
        self.assertEqual(result["summary"]["imported"], 1)
        self.assertEqual(result["summary"]["skipped"], 2)
        statuses = {m["id"]: m["status"] for m in result["members"]}
        self.assertEqual(statuses[preset["id"]], "skipped")
        self.assertEqual(statuses[other["id"]], "skipped")
        self.assertFalse((self.st / "OpenAI Settings" / "P.json").exists())

    def test_import_set_via_character_worldbook_link(self):
        card = self._card("丁")
        world = self._world("丁世界")
        self.lib.add_link(card["id"], world["id"], "character_worldbook")
        result = self._set(card["id"])
        self.assertEqual(result["summary"]["imported"], 2)
        self.assertTrue((self.st / "worlds" / "丁世界.json").is_file())

    def test_import_set_already_and_confirm_again(self):
        card = self._card("戊")
        qr = self._qr("戊QR")
        self.lib.set_bundle([card["id"], qr["id"]])
        r1 = self._set(card["id"])
        self.assertEqual(r1["summary"]["imported"], 2)
        r2 = self._set(card["id"])
        self.assertEqual(r2["summary"]["already"], 2)
        self.assertEqual(r2["summary"]["imported"], 0)
        r3 = self._set(card["id"], confirm=True)
        self.assertEqual(r3["summary"]["imported"], 2)
        self.assertTrue((self.st / "characters" / "戊_2.png").is_file())
        self.assertTrue((self.st / "QuickReplies" / "戊QR_2.json").is_file())

    def test_import_set_requires_character_anchor(self):
        world = self._world("锚不是卡")
        with self.assertRaises(ImportError_):
            self._set(world["id"])

    def test_import_set_without_members(self):
        card = self._card("己")
        result = self._set(card["id"])
        self.assertEqual(result["members"], [])
        self.assertEqual(result["anchor"]["status"], "imported")
        self.assertEqual(result["summary"]["imported"], 1)
