#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavernlib.pieces import extract_from_bytes, extract_pieces
from tavernlib.pngutil import wrap_card_as_png
from tavernlib.store import LibraryStore

CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "雨巷医生",
        "description": "沉默寡言的老医生，右手有一道旧伤。",
        "first_mes": "他把伞往你这边偏了偏。",
        "alternate_greetings": ["雨还没停。", "你站在门口，没有进来。"],
        "character_book": {
            "name": "雨巷设定",
            "entries": [
                {"id": 3, "keys": ["诊所"], "comment": "诊所位置", "content": "诊所开在雨巷尽头。"},
                {"id": 12, "keys": ["旧伤"], "comment": "旧伤来历", "content": "他从不解释那道疤。"},
            ],
        },
    },
}

WORLD = {
    "name": "独立世界书",
    "entries": {"0": {"uid": 0, "key": ["雨"], "comment": "雨季", "content": "雨要下三个月。"}},
}


class PieceExtractTests(unittest.TestCase):
    def test_card_yields_body_greetings_and_entries(self):
        pieces = extract_pieces(CARD, "character")
        kinds = [p["kind"] for p in pieces]
        self.assertEqual(kinds.count("body"), 1)
        self.assertEqual(kinds.count("greeting"), 3)
        self.assertEqual(kinds.count("world_entry"), 2)
        greetings = [p for p in pieces if p["kind"] == "greeting"]
        self.assertTrue(greetings[0]["meta"]["primary"])
        self.assertEqual(greetings[2]["ord"], 2)

    def test_world_and_preset(self):
        self.assertEqual(len(extract_pieces(WORLD, "world")), 1)
        preset = {"prompts": [{"identifier": "main", "name": "主提示", "content": "你是引擎。"}]}
        pieces = extract_pieces(preset, "preset")
        self.assertEqual(pieces[0]["source_ref"], "block:main")

    def test_png_bytes_are_decoded(self):
        png = wrap_card_as_png(json.dumps(CARD, ensure_ascii=False))
        pieces = extract_from_bytes(png, "character")
        self.assertEqual(len([p for p in pieces if p["kind"] == "body"]), 1)


class PieceStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LibraryStore(Path(tmp.name))
        png = wrap_card_as_png(json.dumps(CARD, ensure_ascii=False))
        self.card, _ = self.store.add_file(
            png, type="character", subtype=None, title="雨巷医生", original_filename="雨巷医生.png"
        )
        world = json.dumps(WORLD, ensure_ascii=False).encode()
        self.world, _ = self.store.add_file(
            world, type="world", subtype=None, title="独立世界书", original_filename="世界书.json"
        )

    def test_pieces_are_persisted_on_ingest(self):
        pieces = self.store.pieces(self.card["id"])
        self.assertEqual(len(pieces), 6)
        self.assertTrue(all(p["chars"] > 0 for p in pieces))
        enriched = self.store.get_enriched(self.card["id"])
        self.assertEqual(len(enriched["pieces"]), 6)

    def test_search_hits_body_and_entry(self):
        hits = self.store.search_pieces("旧伤")
        self.assertTrue(any(h["kind"] == "body" for h in hits))
        self.assertTrue(any(h["kind"] == "world_entry" for h in hits))
        items = self.store.list_items(query="三个月")
        self.assertEqual([it["title"] for it in items], ["独立世界书"])

    def test_search_by_kind(self):
        hits = self.store.search_pieces("雨", kind="greeting")
        self.assertTrue(hits)
        self.assertTrue(all(h["kind"] == "greeting" for h in hits))

    def test_tags_bind_to_one_piece_only(self):
        pieces = self.store.pieces(self.card["id"], with_text=True)
        entry = next(p for p in pieces if p["kind"] == "world_entry")
        self.store.set_piece_tags(entry["id"], [{"bucket": "setting", "name": "旧伤"}])
        tagged = self.store.piece(entry["id"])
        self.assertEqual([t["name"] for t in tagged["tags"]], ["旧伤"])
        other = next(p for p in pieces if p["kind"] == "greeting")
        self.assertEqual(self.store.piece(other["id"])["tags"], [])
        self.assertEqual([p["id"] for p in self.store.pieces_by_tag(tagged["tags"][0]["id"])], [entry["id"]])

    def test_reingest_refreshes_pieces(self):
        before = len(self.store.pieces(self.card["id"]))
        changed = json.loads(json.dumps(CARD))
        changed["data"]["description"] = "换了一段正文。"
        path = self.store.abspath(self.card["stored_path"])
        path.write_bytes(wrap_card_as_png(json.dumps(changed, ensure_ascii=False)))
        count = self.store.extract_pieces(self.card["id"])
        after = self.store.pieces(self.card["id"], with_text=True)
        body = next(p for p in after if p["kind"] == "body")
        self.assertEqual(body["text"], "换了一段正文。")
        self.assertEqual(count, before)


if __name__ == "__main__":
    unittest.main()
