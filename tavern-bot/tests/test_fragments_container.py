"""Fragment extraction must read card containers (PNG / CharX), not only text JSON.

Regression guard: PNG character cards used to decode-fail inside fragments._loads,
so every uploaded card ended up with zero fragments.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tavernlib.fragments import extract_fragments
from tavernlib.pngutil import wrap_card_as_png
from tavernlib.store import LibraryStore


def card_obj(entries: int = 18) -> dict:
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "桑洛凡",
            "description": "金主，说话很直接。",
            "first_mes": "「钱打过来了。」",
            "scenario": "深夜的办公室。",
            "extensions": {"world": "桑洛凡·金主快打钱"},
            "character_book": {
                "name": "桑洛凡·金主快打钱",
                "entries": [
                    {
                        "id": i,
                        "comment": f"词条 {i}",
                        "keys": [f"k{i}"],
                        "content": "状态栏规则：输出前先写状态。" if i == 0 else f"第 {i} 条设定内容。",
                        "constant": i < 3,
                    }
                    for i in range(entries)
                ],
            },
        },
    }


class ContainerFragmentTests(unittest.TestCase):
    def test_png_card_yields_card_and_worldbook_fragments(self):
        png = wrap_card_as_png(json.dumps(card_obj(), ensure_ascii=False))
        frags = extract_fragments(png, "character")
        self.assertTrue(frags)
        refs = {f["source_ref"] for f in frags}
        self.assertIn("card:description", refs)
        self.assertIn("character_book:entry:0", refs)
        book = [f for f in frags if f["source_ref"].startswith("character_book:")]
        self.assertEqual(len(book), 18)

    def test_card_body_is_indexed(self):
        png = wrap_card_as_png(json.dumps(card_obj(2), ensure_ascii=False))
        frags = extract_fragments(png, "character")
        titles = {f["title"] for f in frags}
        self.assertIn("桑洛凡·角色描述", titles)
        self.assertIn("桑洛凡·开场白", titles)

    def test_charx_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("card.json", json.dumps(card_obj(3), ensure_ascii=False))
        frags = extract_fragments(buf.getvalue(), "character")
        self.assertTrue(any(f["source_ref"].startswith("character_book:") for f in frags))

    def test_unknown_binary_is_safe(self):
        self.assertEqual(extract_fragments(b"\x89PNG\r\n\x1a\n\x00\x00", "character"), [])
        self.assertEqual(extract_fragments(b"PK\x03\x04garbage", "character"), [])
        self.assertEqual(extract_fragments(b"", "character"), [])

    def test_plain_json_card_still_works(self):
        frags = extract_fragments(json.dumps(card_obj(2), ensure_ascii=False).encode(), "character")
        self.assertTrue(any(f["source_ref"] == "character_book:entry:0" for f in frags))

    def test_card_regex_scripts_summarised(self):
        card = card_obj(2)
        card["data"]["extensions"]["regex_scripts"] = [
            {"scriptName": "Top", "findRegex": "<status_top>", "replaceString": ""},
            {"scriptName": "Bottom", "findRegex": "<status_bottom>", "replaceString": ""},
        ]
        png = wrap_card_as_png(json.dumps(card, ensure_ascii=False))
        frags = extract_fragments(png, "character")
        regex = [f for f in frags if f["source_ref"] == "card:regex_scripts"]
        self.assertEqual(len(regex), 1)
        self.assertIn("Top", regex[0]["excerpt"])
        self.assertIn("2 条", regex[0]["excerpt"])

    def test_quick_reply_set_indexed(self):
        qr = {"version": 2, "name": "桑洛凡QR", "qrList": [{"label": "线上消息", "message": "/sendas name={{char}}"}]}
        frags = extract_fragments(json.dumps(qr, ensure_ascii=False).encode(), "preset")
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["title"], "桑洛凡QR·线上消息")
        self.assertTrue(frags[0]["source_ref"].startswith("qr:"))

    def test_store_indexes_png_card_on_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LibraryStore(Path(tmp))
            png = wrap_card_as_png(json.dumps(card_obj(4), ensure_ascii=False))
            item, created = store.add_file(
                png,
                type="character",
                subtype=None,
                title="桑洛凡",
                original_filename="04d22757a4549910.png",
            )
            self.assertTrue(created)
            frags = store.get_enriched(item["id"])["fragments"]
            self.assertTrue(frags)
            self.assertTrue(any(f["source_ref"].startswith("character_book:") for f in frags))
            self.assertTrue(any(f["source_ref"] == "card:description" for f in frags))


if __name__ == "__main__":
    unittest.main()
