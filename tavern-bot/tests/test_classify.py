#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavernlib.classify import classify, classify_json_obj
from tavernlib.pngutil import embed_chara, is_png, make_solid_png, read_chara_json, wrap_card_as_png


def card_v2(name: str = "东玄测试") -> dict:
    return {
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
    }


class PngRoundtripTests(unittest.TestCase):
    def test_make_png_is_valid(self):
        data = make_solid_png()
        self.assertTrue(is_png(data))
        self.assertIsNone(read_chara_json(data))

    def test_embed_and_read_chara_and_ccv3(self):
        card = json.dumps(card_v2(), ensure_ascii=False)
        png = wrap_card_as_png(card)
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        obj = json.loads(extracted)
        # ccv3 takes precedence and bumps the spec
        self.assertTrue(str(obj.get("spec", "")).startswith("chara_card_"))
        self.assertEqual(obj["data"]["name"], "丌玄测试")

    def test_zxt_chunk_is_read(self):
        import base64
        import struct
        import zlib
        from tavernlib.pngutil import PNG_SIG, _chunk

        card = json.dumps(card_v2("压缩角色"), ensure_ascii=False)
        encoded = base64.b64encode(card.encode("utf-8"))
        compressed = zlib.compress(encoded, 9)
        payload = b"chara\x00" + bytes([0]) + compressed
        png = (
            PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk("IDAT", zlib.compress(b"\x00\x30\x38\x2e"))
            + _chunk("zTXt", payload)
            + _chunk("IEND", b"")
        )
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        self.assertEqual(json.loads(extracted)["data"]["name"], "压缩角色")


class ClassifyTests(unittest.TestCase):
    def test_png_character(self):
        png = wrap_card_as_png(json.dumps(card_v2("小橘"), ensure_ascii=False))
        result = classify(png, filename="xiaoj.png")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "小橘")
        self.assertFalse(result.uncertain)
        self.assertTrue(result.importable)

    def test_plain_png_without_hint_is_other_uncertain(self):
        result = classify(make_solid_png(), filename="photo.png")
        self.assertEqual(result.type, "other")
        self.assertTrue(result.uncertain)

    def test_plain_png_with_persona_hint(self):
        result = classify(make_solid_png(), filename="me.png", hint="这是我的面具")
        self.assertEqual(result.type, "persona")
        self.assertTrue(result.importable)

    def test_plain_png_in_persona_channel(self):
        result = classify(make_solid_png(), filename="face.webp", channel_name="面具")
        # this file is actually a png despite the name
        self.assertEqual(result.type, "persona")

    def test_character_json_spec(self):
        data = json.dumps(card_v2("池簌"), ensure_ascii=False).encode("utf-8")
        result = classify(data, filename="chi.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "池簌")

    def test_character_v1_json(self):
        payload = {
            "name": "旧卡",
            "description": "d",
            "personality": "p",
            "first_mes": "hi",
            "scenario": "s",
        }
        result = classify(json.dumps(payload).encode(), filename="old.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "旧卡")

    def test_world_json(self):
        world = {
            "name": "东海见闻",
            "entries": {
                "1": {
                    "uid": 1,
                    "keys": ["东海"],
                    "content": "一片海",
                    "enabled": True,
                    "constant": False,
                }
            },
        }
        result = classify(json.dumps(world, ensure_ascii=False).encode("utf-8"), filename="east.json")
        self.assertEqual(result.type, "world")
        self.assertEqual(result.title, "东海见闻")
        self.assertTrue(result.importable)

    def test_world_entries_array(self):
        world = {
            "entries": [
                {"uid": 2, "key": ["k"], "content": "c", "enabled": True},
            ]
        }
        result = classify(json.dumps(world).encode(), filename="w.json")
        self.assertEqual(result.type, "world")

    def test_openai_preset(self):
        obj = {
            "chat_completion_source": "openai",
            "openai_model": "gpt-4o",
            "temperature": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "openai"))

    def test_instruct_preset(self):
        obj = {
            "name": "ChatML",
            "input_sequence": "<|im_start|>user",
            "output_sequence": "<|im_start|>assistant",
            "system_sequence": "<|im_start|>system",
            "input_suffix": "<|im_end>",
            "output_suffix": "<|im_end>",
            "names_behavior": "force",
        }
        result = classify(json.dumps(obj).encode(), filename="ChatML.json")
        self.assertEqual((result.type, result.subtype), ("preset", "instruct"))

    def test_context_preset(self):
        obj = {
            "name": "Default",
            "story_string": "{{#if system}}{{system}}{{/if}}",
            "example_separator": "",
            "chat_start": "",
            "story_string_position": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "context"))

    def test_sysprompt(self):
        import base64
        import struct
        import zlib
        from tavernlib.pngutil import PNG_SIG, _chunk

        card = json.dumps(card_v2("压缩角色"), ensure_ascii=False)
        encoded = base64.b64encode(card.encode("utf-8"))
        compressed = zlib.compress(encoded, 9)
        payload = b"chara\x00" + bytes([0]) + compressed
        png = (
            PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk("IDAT", zlib.compress(b"\x00\x30\x38\x2e"))
            + _chunk("zTXt", payload)
            + _chunk("IEND", b"")
        )
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        self.assertEqual(json.loads(extracted)["data"]["name"], "压缩角色")


class ClassifyTests(unittest.TestCase):
    def test_png_character(self):
        png = wrap_card_as_png(json.dumps(card_v2("小橘"), ensure_ascii=False))
        result = classify(png, filename="xiaoj.png")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "小橘")
        self.assertFalse(result.uncertain)
        self.assertTrue(result.importable)

    def test_plain_png_without_hint_is_other_uncertain(self):
        result = classify(make_solid_png(), filename="photo.png")
        self.assertEqual(result.type, "other")
        self.assertTrue(result.uncertain)

    def test_plain_png_with_persona_hint(self):
        result = classify(make_solid_png(), filename="me.png", hint="这是我的面具")
        self.assertEqual(result.type, "persona")
        self.assertTrue(result.importable)

    def test_plain_png_in_persona_channel(self):
        result = classify(make_solid_png(), filename="face.webp", channel_name="面具")
        # this file is actually a png despite the name
        self.assertEqual(result.type, "persona")

    def test_character_json_spec(self):
        data = json.dumps(card_v2("池簌"), ensure_ascii=False).encode("utf-8")
        result = classify(data, filename="chi.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "池簌")

    def test_character_v1_json(self):
        payload = {
            "name": "旧卡",
            "description": "d",
            "personality": "p",
            "first_mes": "hi",
            "scenario": "s",
        }
        result = classify(json.dumps(payload).encode(), filename="old.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "旧卡")

    def test_world_json(self):
        world = {
            "name": "东海见闻",
            "entries": {
                "1": {
                    "uid": 1,
                    "keys": ["东海"],
                    "content": "一片海",
                    "enabled": True,
                    "constant": False,
                }
            },
        }
        result = classify(json.dumps(world, ensure_ascii=False).encode("utf-8"), filename="east.json")
        self.assertEqual(result.type, "world")
        self.assertEqual(result.title, "东海见闻")
        self.assertTrue(result.importable)

    def test_world_entries_array(self):
        world = {
            "entries": [
                {"uid": 2, "key": ["k"], "content": "c", "enabled": True},
            ]
        }
        result = classify(json.dumps(world).encode(), filename="w.json")
        self.assertEqual(result.type, "world")

    def test_openai_preset(self):
        obj = {
            "chat_completion_source": "openai",
            "openai_model": "gpt-4o",
            "temperature": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "openai"))

    def test_instruct_preset(self):
        obj = {
            "name": "ChatML",
            "input_sequence": "<|im_start|>user",
            "output_sequence": "<|im_start|>assistant",
            "system_sequence": "<|im_start|>system",
            "input_suffix": "<|im_end>",
            "output_suffix": "<|im_end>",
            "names_behavior": "force",
        }
        result = classify(json.dumps(obj).encode(), filename="ChatML.json")
        self.assertEqual((result.type, result.subtype), ("preset", "instruct"))

    def test_context_preset(self):
        obj = {
            "name": "Default",
            "story_string": "{{#if system}}{{system}}{{/if}}",
            "example_separator": "",
            "chat_start": "",
            "story_string_position": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "context"))

    def test_sysprompt(self):
        import base64
        import struct
        import zlib
        from tavernlib.pngutil import PNG_SIG, _chunk

        card = json.dumps(card_v2("压缩角色"), ensure_ascii=False)
        encoded = base64.b64encode(card.encode("utf-8"))
        compressed = zlib.compress(encoded, 9)
        payload = b"chara\x00" + bytes([0]) + compressed
        png = (
            PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk("IDAT", zlib.compress(b"\x00\x30\x38\x2e"))
            + _chunk("zTXt", payload)
            + _chunk("IEND", b"")
        )
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        self.assertEqual(json.loads(extracted)["data"]["name"], "压缩角色")


class ClassifyTests(unittest.TestCase):
    def test_png_character(self):
        png = wrap_card_as_png(json.dumps(card_v2("小橘"), ensure_ascii=False))
        result = classify(png, filename="xiaoj.png")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "小橘")
        self.assertFalse(result.uncertain)
        self.assertTrue(result.importable)

    def test_plain_png_without_hint_is_other_uncertain(self):
        result = classify(make_solid_png(), filename="photo.png")
        self.assertEqual(result.type, "other")
        self.assertTrue(result.uncertain)

    def test_plain_png_with_persona_hint(self):
        result = classify(make_solid_png(), filename="me.png", hint="这是我的面具")
        self.assertEqual(result.type, "persona")
        self.assertTrue(result.importable)

    def test_plain_png_in_persona_channel(self):
        result = classify(make_solid_png(), filename="face.webp", channel_name="面具")
        # this file is actually a png despite the name
        self.assertEqual(result.type, "persona")

    def test_character_json_spec(self):
        data = json.dumps(card_v2("池簌"), ensure_ascii=False).encode("utf-8")
        result = classify(data, filename="chi.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "池簌")

    def test_character_v1_json(self):
        payload = {
            "name": "旧卡",
            "description": "d",
            "personality": "p",
            "first_mes": "hi",
            "scenario": "s",
        }
        result = classify(json.dumps(payload).encode(), filename="old.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "旧卡")

    def test_world_json(self):
        world = {
            "name": "东海见闻",
            "entries": {
                "1": {
                    "uid": 1,
                    "keys": ["东海"],
                    "content": "一片海",
                    "enabled": True,
                    "constant": False,
                }
            },
        }
        result = classify(json.dumps(world, ensure_ascii=False).encode("utf-8"), filename="east.json")
        self.assertEqual(result.type, "world")
        self.assertEqual(result.title, "东海见闻")
        self.assertTrue(result.importable)

    def test_world_entries_array(self):
        world = {
            "entries": [
                {"uid": 2, "key": ["k"], "content": "c", "enabled": True},
            ]
        }
        result = classify(json.dumps(world).encode(), filename="w.json")
        self.assertEqual(result.type, "world")

    def test_openai_preset(self):
        obj = {
            "chat_completion_source": "openai",
            "openai_model": "gpt-4o",
            "temperature": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "openai"))

    def test_instruct_preset(self):
        obj = {
            "name": "ChatML",
            "input_sequence": "<|im_start|>user",
            "output_sequence": "<|im_start|>assistant",
            "system_sequence": "<|im_start|>system",
            "input_suffix": "<|im_end>",
            "output_suffix": "<|im_end>",
            "names_behavior": "force",
        }
        result = classify(json.dumps(obj).encode(), filename="ChatML.json")
        self.assertEqual((result.type, result.subtype), ("preset", "instruct"))

    def test_context_preset(self):
        obj = {
            "name": "Default",
            "story_string": "{{#if system}}{{system}}{{/if}}",
            "example_separator": "",
            "chat_start": "",
            "story_string_position": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "context"))

    def test_sysprompt(self):
        import base64
        import struct
        import zlib
        from tavernlib.pngutil import PNG_SIG, _chunk

        card = json.dumps(card_v2("压缩角色"), ensure_ascii=False)
        encoded = base64.b64encode(card.encode("utf-8"))
        compressed = zlib.compress(encoded, 9)
        payload = b"chara\x00" + bytes([0]) + compressed
        png = (
            PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk("IDAT", zlib.compress(b"\x00\x30\x38\x2e"))
            + _chunk("zTXt", payload)
            + _chunk("IEND", b"")
        )
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        self.assertEqual(json.loads(extracted)["data"]["name"], "压缩角色")


class ClassifyTests(unittest.TestCase):
    def test_png_character(self):
        png = wrap_card_as_png(json.dumps(card_v2("小橘"), ensure_ascii=False))
        result = classify(png, filename="xiaoj.png")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "小橘")
        self.assertFalse(result.uncertain)
        self.assertTrue(result.importable)

    def test_plain_png_without_hint_is_other_uncertain(self):
        result = classify(make_solid_png(), filename="photo.png")
        self.assertEqual(result.type, "other")
        self.assertTrue(result.uncertain)

    def test_plain_png_with_persona_hint(self):
        result = classify(make_solid_png(), filename="me.png", hint="这是我的面具")
        self.assertEqual(result.type, "persona")
        self.assertTrue(result.importable)

    def test_plain_png_in_persona_channel(self):
        result = classify(make_solid_png(), filename="face.webp", channel_name="面具")
        # this file is actually a png despite the name
        self.assertEqual(result.type, "persona")

    def test_character_json_spec(self):
        data = json.dumps(card_v2("池簌"), ensure_ascii=False).encode("utf-8")
        result = classify(data, filename="chi.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "池簌")

    def test_character_v1_json(self):
        payload = {
            "name": "旧卡",
            "description": "d",
            "personality": "p",
            "first_mes": "hi",
            "scenario": "s",
        }
        result = classify(json.dumps(payload).encode(), filename="old.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "旧卡")

    def test_world_json(self):
        world = {
            "name": "东海见闻",
            "entries": {
                "1": {
                    "uid": 1,
                    "keys": ["东海"],
                    "content": "一片海",
                    "enabled": True,
                    "constant": False,
                }
            },
        }
        result = classify(json.dumps(world, ensure_ascii=False).encode("utf-8"), filename="east.json")
        self.assertEqual(result.type, "world")
        self.assertEqual(result.title, "东海见闻")
        self.assertTrue(result.importable)

    def test_world_entries_array(self):
        world = {
            "entries": [
                {"uid": 2, "key": ["k"], "content": "c", "enabled": True},
            ]
        }
        result = classify(json.dumps(world).encode(), filename="w.json")
        self.assertEqual(result.type, "world")

    def test_openai_preset(self):
        obj = {
            "chat_completion_source": "openai",
            "openai_model": "gpt-4o",
            "temperature": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "openai"))

    def test_instruct_preset(self):
        obj = {
            "name": "ChatML",
            "input_sequence": "<|im_start|>user",
            "output_sequence": "<|im_start|>assistant",
            "system_sequence": "<|im_start|>system",
            "input_suffix": "<|im_end>",
            "output_suffix": "<|im_end>",
            "names_behavior": "force",
        }
        result = classify(json.dumps(obj).encode(), filename="ChatML.json")
        self.assertEqual((result.type, result.subtype), ("preset", "instruct"))

    def test_context_preset(self):
        obj = {
            "name": "Default",
            "story_string": "{{#if system}}{{system}}{{/if}}",
            "example_separator": "",
            "chat_start": "",
            "story_string_position": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "context"))

    def test_sysprompt(self):
        import base64
        import struct
        import zlib
        from tavernlib.pngutil import PNG_SIG, _chunk

        card = json.dumps(card_v2("压缩角色"), ensure_ascii=False)
        encoded = base64.b64encode(card.encode("utf-8"))
        compressed = zlib.compress(encoded, 9)
        payload = b"chara\x00" + bytes([0]) + compressed
        png = (
            PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk("IDAT", zlib.compress(b"\x00\x30\x38\x2e"))
            + _chunk("zTXt", payload)
            + _chunk("IEND", b"")
        )
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        self.assertEqual(json.loads(extracted)["data"]["name"], "压缩角色")


class ClassifyTests(unittest.TestCase):
    def test_png_character(self):
        png = wrap_card_as_png(json.dumps(card_v2("小橘"), ensure_ascii=False))
        result = classify(png, filename="xiaoj.png")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "小橘")
        self.assertFalse(result.uncertain)
        self.assertTrue(result.importable)

    def test_plain_png_without_hint_is_other_uncertain(self):
        result = classify(make_solid_png(), filename="photo.png")
        self.assertEqual(result.type, "other")
        self.assertTrue(result.uncertain)

    def test_plain_png_with_persona_hint(self):
        result = classify(make_solid_png(), filename="me.png", hint="这是我的面具")
        self.assertEqual(result.type, "persona")
        self.assertTrue(result.importable)

    def test_plain_png_in_persona_channel(self):
        result = classify(make_solid_png(), filename="face.webp", channel_name="面具")
        # this file is actually a png despite the name
        self.assertEqual(result.type, "persona")

    def test_character_json_spec(self):
        data = json.dumps(card_v2("池簌"), ensure_ascii=False).encode("utf-8")
        result = classify(data, filename="chi.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "池簌")

    def test_character_v1_json(self):
        payload = {
            "name": "旧卡",
            "description": "d",
            "personality": "p",
            "first_mes": "hi",
            "scenario": "s",
        }
        result = classify(json.dumps(payload).encode(), filename="old.json")
        self.assertEqual(result.type, "character")
        self.assertEqual(result.title, "旧卡")

    def test_world_json(self):
        world = {
            "name": "东海见闻",
            "entries": {
                "1": {
                    "uid": 1,
                    "keys": ["东海"],
                    "content": "一片海",
                    "enabled": True,
                    "constant": False,
                }
            },
        }
        result = classify(json.dumps(world, ensure_ascii=False).encode("utf-8"), filename="east.json")
        self.assertEqual(result.type, "world")
        self.assertEqual(result.title, "东海见闻")
        self.assertTrue(result.importable)

    def test_world_entries_array(self):
        world = {
            "entries": [
                {"uid": 2, "key": ["k"], "content": "c", "enabled": True},
            ]
        }
        result = classify(json.dumps(world).encode(), filename="w.json")
        self.assertEqual(result.type, "world")

    def test_openai_preset(self):
        obj = {
            "chat_completion_source": "openai",
            "openai_model": "gpt-4o",
            "temperature": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "openai"))

    def test_instruct_preset(self):
        obj = {
            "name": "ChatML",
            "input_sequence": "<|im_start|>user",
            "output_sequence": "<|im_start|>assistant",
            "system_sequence": "<|im_start|>system",
            "input_suffix": "<|im_end>",
            "output_suffix": "<|im_end>",
            "names_behavior": "force",
        }
        result = classify(json.dumps(obj).encode(), filename="ChatML.json")
        self.assertEqual((result.type, result.subtype), ("preset", "instruct"))

    def test_context_preset(self):
        obj = {
            "name": "Default",
            "story_string": "{{#if system}}{{system}}{{/if}}",
            "example_separator": "",
            "chat_start": "",
            "story_string_position": 0,
        }
        result = classify(json.dumps(obj).encode(), filename="Default.json")
        self.assertEqual((result.type, result.subtype), ("preset", "context"))

    def test_sysprompt(self):
        import base64
        import struct
        import zlib
        from tavernlib.pngutil import PNG_SIG, _chunk

        card = json.dumps(card_v2("压缩角色"), ensure_ascii=False)
        encoded = base64.b64encode(card.encode("utf-8"))
        compressed = zlib.compress(encoded, 9)
        payload = b"chara\x00" + bytes([0]) + compressed
        png = (
            PNG_SIG
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk("IDAT", zlib.compress(b"\x00\x30\x38\x2e"))
            + _chunk("zTXt", payload)
            + _chunk("IEND", b"")
        )
        extracted = read_chara_json(png)
        self.assertIsNotNone(extracted)
        self.assertEqual(json.loads(extracted)["data"]["name"], "压缩角色")
