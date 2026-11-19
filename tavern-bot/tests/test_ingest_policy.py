#!/usr/bin/env python3
"""Ingest policy tests: ignored plain images and the author-note cache."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
from tavernlib.classify import classify  # noqa: E402
from tavernlib.pngutil import embed_chara, make_solid_png  # noqa: E402


def card_v2(name: str = "测试卡") -> str:
    return json.dumps(
        {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": name,
                "description": "desc",
                "personality": "kind",
                "first_mes": "hello",
                "mes_example": "",
                "scenario": "",
            },
        },
        ensure_ascii=False,
    )


class IgnoredImageTests(unittest.TestCase):
    def test_plain_png_is_ignored(self):
        result = classify(make_solid_png(), filename="shot.png")
        self.assertEqual(result.type, "other")
        self.assertEqual(result.reason, "png-no-chara")
        self.assertTrue(bot._is_ignored_image_result(result))

    def test_png_with_chara_is_kept(self):
        png = embed_chara(make_solid_png(), card_v2())
        result = classify(png, filename="card.png")
        self.assertEqual(result.type, "character")
        self.assertFalse(bot._is_ignored_image_result(result))

    def test_background_hint_is_kept(self):
        result = classify(make_solid_png(), filename="bg.png", hint="背景图")
        self.assertEqual(result.type, "beautify")
        self.assertFalse(bot._is_ignored_image_result(result))

    def test_imagegen_channel_is_kept(self):
        result = classify(make_solid_png(), filename="ref.png", channel_name="生图")
        self.assertEqual(result.type, "imagegen")
        self.assertFalse(bot._is_ignored_image_result(result))

    def test_persona_hint_is_kept(self):
        result = classify(make_solid_png(), filename="mask.png", hint="面具")
        self.assertEqual(result.type, "persona")
        self.assertFalse(bot._is_ignored_image_result(result))

    def test_unknown_json_is_not_image_skipped(self):
        data = json.dumps({"random": "object"}).encode("utf-8")
        result = classify(data, filename="weird.json")
        self.assertEqual(result.type, "other")
        self.assertFalse(bot._is_ignored_image_result(result))


class NoteHelpersTests(unittest.TestCase):
    def test_message_texts_merges_content_and_snapshots(self):
        msg = SimpleNamespace(
            content=" own text ",
            message_snapshots=[SimpleNamespace(content="原帖简介"), SimpleNamespace(content="原帖简介")],
        )
        self.assertEqual(bot._message_texts(msg), ["own text", "原帖简介"])

    def test_merge_note_orders_cached_first(self):
        self.assertEqual(bot._merge_note("简介", "自己打的字"), "简介\n\n自己打的字")
        self.assertEqual(bot._merge_note("", ""), "")
        self.assertEqual(bot._merge_note("only", ""), "only")


class NoteCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TAVERN_LIBRARY"] = self._tmp.name
        self.the_bot = bot.TavernBot()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("TAVERN_LIBRARY", None)

    @staticmethod
    def _msg(channel_id: int = 10, user_id: int = 20) -> SimpleNamespace:
        return SimpleNamespace(
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(id=user_id),
        )

    def test_store_and_read(self):
        msg = self._msg()
        self.the_bot._store_note_cache(msg, "作者简介文本")
        self.assertEqual(self.the_bot._cached_note(msg), "作者简介文本")

    def test_channels_do_not_cross(self):
        a = self._msg(channel_id=1, user_id=20)
        b = self._msg(channel_id=2, user_id=20)
        self.the_bot._store_note_cache(a, "频道一简介")
        self.assertEqual(self.the_bot._cached_note(b), "")

    def test_expiry(self):
        msg = self._msg()
        self.the_bot._store_note_cache(msg, "过期简介")
        key = self.the_bot._cache_key(msg)
        stored_at, text = self.the_bot._note_cache[key]
        self.the_bot._note_cache[key] = (stored_at - bot.NOTE_CACHE_TTL - 1, text)
        self.assertEqual(self.the_bot._cached_note(msg), "")


if __name__ == "__main__":
    unittest.main()
