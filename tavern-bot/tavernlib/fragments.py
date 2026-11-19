"""Extract searchable content fragments from worldbooks, presets, and character cards.

The original file is left intact. Fragments are additional rows for search.
番外 is a separate record (not only a fragment) and is created by the user or
by explicit linking, not automatically here.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .pngutil import is_png, read_chara_json
from .taxonomy import FRAGMENT_KIND_LABELS, MAX_TEXT

logger = logging.getLogger(__name__)

STYLE_MARKERS = ("文风", "文笔", "写作风格", "行文", "语风", "描写风格", "writing style", "prose style", "narration style")
RULE_MARKERS = ("规则", "玩法", "合规", "禁限", "禁忌", "rule", "ruleset", "gameplay")
SETTING_MARKERS = ("设定", "世界观", "lore", "background setting", "world setting")
CHARACTER_MARKERS = ("角色补充", "角色设定", "人物", "character info", "character note")


def _text_from_bytes(data: bytes) -> str | None:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _card_json_from_bytes(data: bytes) -> str | None:
    """Return the card JSON embedded in a PNG card or a CharX archive."""
    if is_png(data):
        return read_chara_json(data)
    if data[:2] == b"PK":
        # Imported lazily: classify pulls in pngutil but never fragments.
        from .classify import read_charx_card

        parsed = read_charx_card(data)
        if parsed:
            return parsed[0]
    return None


def _loads(data: bytes | str | dict) -> Any | None:
    """Parse a stored file into a JSON object.

    The library keeps uploaded files byte-for-byte, so a character card reaches
    this function as PNG bytes (chara/ccv3 chunk) or as a CharX zip. Neither is
    UTF-8 text, which is why a decode-only parser silently returned nothing for
    every PNG card in the library.
    """
    if isinstance(data, dict):
        return data
    text: str | None
    if isinstance(data, bytes):
        text = _card_json_from_bytes(data) or _text_from_bytes(data)
    else:
        text = data
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _excerpt(text: str, limit: int = 800) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _kind_from_text(*parts: str) -> str:
    blob = " ".join(p for p in parts if p).lower()
    for marker in STYLE_MARKERS:
        if marker.lower() in blob:
            return "style"
    for marker in RULE_MARKERS:
        if marker.lower() in blob:
            return "rule"
    for marker in CHARACTER_MARKERS:
        if marker.lower() in blob:
            return "character"
    for marker in SETTING_MARKERS:
        if marker.lower() in blob:
            return "setting"
    return "other"


def _iter_world_entries(obj: dict) -> list[tuple[str, dict]]:
    entries = obj.get("entries")
    out: list[tuple[str, dict]] = []
    if isinstance(entries, dict):
        for key, val in entries.items():
            if isinstance(val, dict):
                out.append((str(key), val))
    elif isinstance(entries, list):
        for i, val in enumerate(entries):
            if isinstance(val, dict):
                uid = val.get("uid", i)
                out.append((str(uid), val))
    return out


def _entry_title(entry: dict, fallback: str) -> str:
    for key in ("comment", "name", "title", "uid"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:80]
        if isinstance(raw, (int, float)):
            return f"{fallback} {raw}"
    keys = entry.get("keys") or entry.get("key")
    if isinstance(keys, list) and keys:
        return str(keys[0])[:80]
    if isinstance(keys, str) and keys.strip():
        return keys.strip()[:80]
    return fallback


def _entry_body(entry: dict) -> str:
    for key in ("content", "text", "value"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return ""


def fragments_from_world(obj: dict) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for ref, entry in _iter_world_entries(obj):
        body = _entry_body(entry)
        if not body.strip():
            continue
        title = _entry_title(entry, f"词条 {ref}")
        keys = entry.get("keys") or entry.get("key") or ""
        key_text = " ".join(keys) if isinstance(keys, list) else str(keys)
        kind = _kind_from_text(title, key_text, body[:400])
        results.append(
            {
                "kind": kind,
                "title": title,
                "excerpt": _excerpt(body),
                "source_ref": f"entry:{ref}",
            }
        )
    return results


def fragments_from_character_book(obj: dict) -> list[dict[str, str]]:
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if not isinstance(data, dict):
        return []
    book = data.get("character_book") or obj.get("character_book")
    if not isinstance(book, dict):
        return []
    # character_book uses the same entries shape as a worldbook, sometimes a list.
    wrapped = book if "entries" in book else {"entries": book.get("entries") or book}
    frags = fragments_from_world(wrapped if "entries" in wrapped else {"entries": book.get("entries", [])})
    for item in frags:
        item["source_ref"] = "character_book:" + item.get("source_ref", "")
    return frags


CARD_TEXT_FIELDS = (
    ("description", "角色描述"),
    ("personality", "性格"),
    ("scenario", "场景"),
    ("first_mes", "开场白"),
    ("mes_example", "对话示例"),
    ("system_prompt", "系统提示"),
    ("post_history_instructions", "后置指令"),
)


def fragments_from_character_card(obj: dict) -> list[dict[str, str]]:
    """Index the card body itself, not only its embedded worldbook."""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if not isinstance(data, dict):
        return []
    name = str(data.get("name") or obj.get("name") or "").strip()
    results: list[dict[str, str]] = []
    for field_name, label in CARD_TEXT_FIELDS:
        body = data.get(field_name)
        if not isinstance(body, str) or not body.strip():
            continue
        title = f"{name}·{label}" if name else label
        results.append(
            {
                "kind": _kind_from_text(title, body[:400]),
                "title": title[:80],
                "excerpt": _excerpt(body),
                "source_ref": f"card:{field_name}",
            }
        )
    return results


def fragments_from_character_regex(obj: dict) -> list[dict[str, str]]:
    """One summary fragment listing the card's embedded regex scripts.

    A card can ship a dozen SCOPED scripts (status bars, chat screens, message
    types). Indexing them one by one buries the worldbook entries, so the names
    go into a single searchable fragment: searching "状态栏" still hits the card.
    """
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if not isinstance(data, dict):
        return []
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        return []
    scripts = extensions.get("regex_scripts")
    if not isinstance(scripts, list) or not scripts:
        return []
    names: list[str] = []
    for script in scripts:
        if not isinstance(script, dict):
            continue
        label = str(script.get("scriptName") or script.get("findRegex") or "").strip()
        if label:
            names.append(label[:40])
    if not names:
        return []
    name = str(data.get("name") or obj.get("name") or "").strip()
    title = f"{name}·内嵌正则" if name else "内嵌正则"
    body = f"内嵌 {len(scripts)} 条正则脚本：" + "、".join(names)
    return [
        {
            "kind": _kind_from_text(title, body[:400]),
            "title": title[:80],
            "excerpt": _excerpt(body),
            "source_ref": "card:regex_scripts",
        }
    ]


_HEADER_RE = re.compile(r"^(?:#{1,4}\s+|【|\[)(.+?)(?:】|\])?\s*$", re.MULTILINE)


def fragments_from_preset(obj: dict) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    blobs: list[tuple[str, str]] = []
    for key in ("content", "story_string", "sysprompt", "system_prompt", "prompt"):
        raw = obj.get(key)
        if isinstance(raw, str) and raw.strip():
            blobs.append((key, raw))
    # Instruct / context sometimes nest the useful text.
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("content", "story_string"):
            raw = data.get(key)
            if isinstance(raw, str) and raw.strip():
                blobs.append((key, raw))

    for source, text in blobs:
        headers = list(_HEADER_RE.finditer(text))
        if headers:
            for i, match in enumerate(headers):
                title = match.group(1).strip().strip("[]【】# ")
                start = match.end()
                end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
                body = text[start:end].strip()
                if not body:
                    continue
                results.append(
                    {
                        "kind": _kind_from_text(title, body[:400]),
                        "title": title[:80] or source,
                        "excerpt": _excerpt(body),
                        "source_ref": f"preset:{source}:{i}",
                    }
                )
        else:
            kind = _kind_from_text(str(obj.get("name") or ""), text[:400])
            if kind != "other" or len(text) > 80:
                results.append(
                    {
                        "kind": kind if kind != "other" else ("style" if "文风" in (obj.get("name") or "") else "other"),
                        "title": str(obj.get("name") or source)[:80],
                        "excerpt": _excerpt(text),
                        "source_ref": f"preset:{source}",
                    }
                )
    return results


def fragments_from_qr_set(obj: dict) -> list[dict[str, str]]:
    """SillyTavern Quick Reply sets are stored as {'qrList': [...]}."""
    entries = obj.get("qrList")
    if entries is None:
        entries = obj.get("qrs")
    if not isinstance(entries, list):
        return []
    set_name = str(obj.get("name") or "").strip()
    results: list[dict[str, str]] = []
    for i, qr in enumerate(entries):
        if not isinstance(qr, dict):
            continue
        message = qr.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        label = str(qr.get("label") or qr.get("title") or f"QR {i}").strip()
        title = f"{set_name}·{label}" if set_name else label
        results.append(
            {
                "kind": _kind_from_text(title, message[:400]),
                "title": title[:80],
                "excerpt": _excerpt(message),
                "source_ref": f"qr:{label[:60] or i}",
            }
        )
    return results


def _extract_fragments(data: bytes | str | dict, typ: str) -> list[dict[str, str]]:
    """Return fragment dicts for a stored file. Safe on unrecognized input."""
    obj: Any
    if isinstance(data, dict):
        obj = data
    else:
        obj = _loads(data)
    if not isinstance(obj, dict):
        return []

    results: list[dict[str, str]] = []
    if typ == "world":
        results.extend(fragments_from_world(obj))
    elif typ == "character":
        results.extend(fragments_from_character_card(obj))
        results.extend(fragments_from_character_regex(obj))
        results.extend(fragments_from_character_book(obj))
    elif typ in {"preset", "style"}:
        results.extend(fragments_from_preset(obj))
        results.extend(fragments_from_qr_set(obj))
        # A world-shaped object mis-filed as style/preset should still be searchable.
        if "entries" in obj:
            results.extend(fragments_from_world(obj))
    elif typ == "world" or "entries" in obj:
        results.extend(fragments_from_world(obj))

    # Cap to keep the index usable.
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for frag in results:
        title = (frag.get("title") or "")[:80]
        excerpt = (frag.get("excerpt") or "")[:MAX_TEXT]
        kind = frag.get("kind") if frag.get("kind") in FRAGMENT_KIND_LABELS else "other"
        key = (kind, title, excerpt[:80])
        if not excerpt or key in seen:
            continue
        seen.add(key)
        cleaned.append(
            {
                "kind": kind,
                "title": title,
                "excerpt": excerpt,
                "source_ref": (frag.get("source_ref") or "")[:200],
            }
        )
        if len(cleaned) >= 80:
            break
    return cleaned


def extract_fragments(data: bytes | str | dict, typ: str) -> list[dict[str, str]]:
    """Public entry point.

    Indexing is a convenience: a malformed card must never block an upload, and
    the failure must be logged rather than silently swallowed.
    """
    try:
        return _extract_fragments(data, typ)
    except Exception:
        logger.warning("fragment extraction failed (type=%s)", typ, exc_info=True)
        return []
