"""Extract searchable content fragments from worldbooks, presets, and character cards.

The original file is left intact. Fragments are additional rows for search.
番外 is a separate record (not only a fragment) and is created by the user or
by explicit linking, not automatically here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .taxonomy import FRAGMENT_KIND_LABELS, MAX_TEXT

STYLE_MARKERS = ("文风", "文笔", "写作风格", "行文", "语风", "描写风格", "writing style", "prose style", "narration style")
RULE_MARKERS = ("规则", "玩法", "合规", "禁限", "禁忌", "rule", "ruleset", "gameplay")
SETTING_MARKERS = ("设定", "世界观", "lore", "background setting", "world setting")
CHARACTER_MARKERS = ("角色补充", "角色设定", "人物", "character info", "character note")


def _loads(data: bytes | str) -> Any | None:
    if isinstance(data, bytes):
        for enc in ("utf-8-sig", "utf-8"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                text = None
        else:
            return None
    else:
        text = data
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


def extract_fragments(data: bytes | str | dict, typ: str) -> list[dict[str, str]]:
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
        results.extend(fragments_from_character_book(obj))
    elif typ in {"preset", "style"}:
        results.extend(fragments_from_preset(obj))
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
