"""Split cards, worldbooks and presets into searchable pieces.

One piece = one retrievable unit: a card body field, a greeting, a worldbook
entry, or a preset block. The original file is never modified; pieces are the
index the rest of the app (search, tags, masks) reads from.

Tag vocabulary is axis-based, so `kind` tells you where a piece came from and
`meta` keeps the original identifiers (entry uid, field name, greeting order).
"""

from __future__ import annotations

import json
from typing import Any

from .classify import read_charx_card
from .pngutil import read_chara_json

PIECE_KINDS = ("body", "greeting", "world_entry", "preset_block")

# Card fields worth indexing separately. description is the main body; the rest
# are optional but each has a distinct role, so they stay separate.
BODY_FIELDS: tuple[tuple[str, str], ...] = (
    ("description", "描述"),
    ("personality", "性格"),
    ("scenario", "场景"),
    ("mes_example", "对话示例"),
    ("system_prompt", "系统提示"),
    ("post_history_instructions", "历史后指令"),
    ("creator_notes", "作者注"),
)


def _payload(obj: dict[str, Any]) -> dict[str, Any]:
    data = obj.get("data")
    return data if isinstance(data, dict) else obj


def _entry_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        out = []
        for key, value in raw.items():
            if isinstance(value, dict):
                out.append(dict(value, id=value.get("id", key)))
        return out
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _entry_keys(entry: dict[str, Any]) -> list[str]:
    keys = entry.get("keys") or entry.get("key") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")]
    return [k for k in keys if isinstance(k, str) and k.strip()]


def _entry_title(entry: dict[str, Any], index: int) -> str:
    for key in ("comment", "name", "title"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    keys = _entry_keys(entry)
    if keys:
        return keys[0][:80]
    return f"词条 {index}"


def _world_pieces(entries: list[dict[str, Any]], *, origin: str, prefix: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        text = str(entry.get("content") or "").strip()
        if not text:
            continue
        uid = entry.get("id", entry.get("uid", index))
        out.append(
            {
                "kind": "world_entry",
                "ord": index,
                "title": _entry_title(entry, index),
                "text": text,
                "source_ref": f"{prefix}{uid}",
                "meta": {
                    "origin": origin,
                    "entry_id": uid,
                    "keys": _entry_keys(entry),
                    "enabled": bool(entry.get("enabled", True)),
                    "constant": bool(entry.get("constant", False)),
                    "order": entry.get("insertion_order", entry.get("order", 0)),
                },
            }
        )
    return out


def _preset_pieces(obj: dict[str, Any]) -> list[dict[str, Any]]:
    prompts = obj.get("prompts")
    if isinstance(prompts, dict):
        prompts = list(prompts.values())
    out: list[dict[str, Any]] = []
    for index, block in enumerate(p for p in (prompts or []) if isinstance(p, dict)):
        text = str(block.get("content") or "").strip()
        if not text:
            continue
        ident = str(block.get("identifier") or block.get("name") or f"block-{index}")
        out.append(
            {
                "kind": "preset_block",
                "ord": index,
                "title": str(block.get("name") or ident)[:80],
                "text": text,
                "source_ref": f"block:{ident}",
                "meta": {
                    "identifier": ident,
                    "role": block.get("role") or "system",
                    "enabled": bool(block.get("enabled", True)),
                },
            }
        )
    return out


def extract_pieces(obj: Any, typ: str) -> list[dict[str, Any]]:
    """Return piece dicts for a parsed card / worldbook / preset."""
    if not isinstance(obj, dict):
        return []
    payload = _payload(obj)

    if typ == "character":
        pieces: list[dict[str, Any]] = []
        for field, label in BODY_FIELDS:
            text = str(payload.get(field) or "").strip()
            if text:
                pieces.append(
                    {
                        "kind": "body",
                        "ord": 0,
                        "title": label,
                        "text": text,
                        "source_ref": f"field:{field}",
                        "meta": {"field": field},
                    }
                )
        first = payload.get("first_mes")
        greetings: list[str] = []
        if isinstance(first, str) and first.strip():
            greetings.append(first)
        alternates = payload.get("alternate_greetings")
        if isinstance(alternates, list):
            greetings.extend(g for g in alternates if isinstance(g, str) and g.strip())
        for index, text in enumerate(greetings):
            pieces.append(
                {
                    "kind": "greeting",
                    "ord": index,
                    "title": "开场白" if index == 0 else f"开场白 {index + 1}",
                    "text": text.strip(),
                    "source_ref": f"greeting:{index}",
                    "meta": {"index": index, "primary": index == 0},
                }
            )
        book = payload.get("character_book") or obj.get("character_book")
        if isinstance(book, dict):
            pieces.extend(
                _world_pieces(_entry_list(book.get("entries")), origin="embedded", prefix="character_book:")
            )
        return pieces

    if typ == "world":
        return _world_pieces(_entry_list(obj.get("entries")), origin="standalone", prefix="entry:")

    if typ in {"preset", "style"}:
        return _preset_pieces(obj)

    return []


def _decode(data: bytes) -> dict[str, Any] | None:
    """Parse PNG cards, CharX archives and plain JSON into a dict."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        raw = read_chara_json(data)
        return json.loads(raw) if raw else None
    if data[:2] == b"PK":
        found = read_charx_card(data)
        return json.loads(found[0]) if found else None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            obj = json.loads(data.decode(encoding))
        except (UnicodeDecodeError, ValueError):
            continue
        return obj if isinstance(obj, dict) else None
    return None


def extract_from_bytes(data: bytes, typ: str) -> list[dict[str, Any]]:
    """Convenience wrapper used at ingest time (file bytes, not a parsed dict)."""
    return extract_pieces(_decode(data), typ)
