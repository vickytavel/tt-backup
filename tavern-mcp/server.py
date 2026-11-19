#!/usr/bin/env python3
"""tavern-mcp — stdio MCP server for reading tavern cards and writing 面具 files.

Tools are defined in TOOLS; each maps to a function in HANDLERS.
Run: TAVERN_LIBRARY=/opt/tavern-library python3 server.py
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tavern-bot"))

from tavernlib.classify import read_charx_card  # noqa: E402
from tavernlib.paths import LIBRARY_ROOT  # noqa: E402
from tavernlib.pngutil import read_chara_json  # noqa: E402
from tavernlib.store import LibraryStore  # noqa: E402

MASK_ROOT = Path(os.environ.get("TAVERN_MASKS", str(LIBRARY_ROOT / "masks"))).resolve()
MAX_CHARS = 8000
DEFAULT_CHARS = 4000
REQUIRED_SECTIONS = ("身份", "声音与语气", "行为边界")
ALLOWED_MACROS = {"char", "user", "persona"}
PRESET_WHITELIST = {"main", "charDef", "worldInfoBefore", "worldInfoAfter", "nsfw"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
MACRO_RE = re.compile(r"\{\{([^{}]+)\}\}")

_store: LibraryStore | None = None


def store() -> LibraryStore:
    global _store
    if _store is None:
        _store = LibraryStore()
    return _store


# --- errors ---------------------------------------------------------------


class ToolError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


# --- card loading ---------------------------------------------------------


def _item_for(ref: str, type_filter: str | None = None) -> dict[str, Any]:
    """Resolve a card reference: item title, stored path, or absolute-ish path."""
    candidates = store().list_items(type_filter, query=None, limit=2000)
    for item in candidates:
        if ref in (item.get("title"), item.get("stored_path"), item.get("original_filename")):
            return item
    lowered = ref.lower()
    for item in candidates:
        for field in ("title", "stored_path", "original_filename"):
            if (item.get(field) or "").lower() == lowered:
                return item
    raise ToolError("CARD_NOT_FOUND", f"找不到资源：{ref}")


def _card_obj(ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    item = _item_for(ref)
    path = store().abspath(item["stored_path"])
    if not path.is_file():
        raise ToolError("CARD_NOT_FOUND", f"文件不存在：{item['stored_path']}")
    data = path.read_bytes()
    raw: str | None = None
    if path.suffix.lower() in {".png"}:
        raw = read_chara_json(data)
    elif path.suffix.lower() == ".charx" or data.startswith(b"PK"):
        found = read_charx_card(data)
        raw = found[0] if found else None
    elif path.suffix.lower() == ".json":
        raw = data.decode("utf-8", "replace")
    if not raw:
        raise ToolError("CARD_NOT_FOUND", f"{path.name} 不是可解析的角色卡")
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise ToolError("CARD_NOT_FOUND", f"{path.name} 的卡片 JSON 无法解析：{exc}") from exc
    return item, obj


def _payload(obj: dict[str, Any]) -> dict[str, Any]:
    """Return the flat field bag regardless of card spec version."""
    data = obj.get("data")
    if isinstance(data, dict):
        return data
    return obj


def _spec_name(obj: dict[str, Any]) -> str:
    spec = obj.get("spec")
    if isinstance(spec, str) and spec:
        return spec
    return "chara_v1"


def _entries(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """World book entries from a card (character_book) or a standalone world file."""
    payload = _payload(obj)
    book = payload.get("character_book") or obj.get("character_book")
    raw: Any = None
    if isinstance(book, dict):
        raw = book.get("entries")
    if raw is None:
        raw = obj.get("entries") or payload.get("entries")
    return _normalize_entries(raw)


def _normalize_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = []
        for key, value in raw.items():
            if isinstance(value, dict):
                items.append(dict(value, id=value.get("id", key)))
        return items
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _keys_of(entry: dict[str, Any]) -> list[str]:
    keys = entry.get("keys") or entry.get("key") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")]
    return [k for k in keys if isinstance(k, str) and k.strip()]


# --- tools: cards ---------------------------------------------------------


def _pieces(ref: str, *, with_text: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Stored pieces for a card: the same text the library indexes and tags."""
    item = _item_for(ref, "character")
    return item, store().pieces(int(item["id"]), with_text=with_text)


def _piece_field(row: dict[str, Any]) -> str:
    return str((row.get("meta") or {}).get("field") or "")


def card_index(args: dict[str, Any]) -> dict[str, Any]:
    """Full inventory of a card: what exists, how big, without any body text."""
    item, rows = _pieces(str(args.get("card") or ""))
    if rows:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        return {
            "path": item.get("stored_path") or "",
            "name": item.get("title") or "",
            "source": "pieces",
            "counts": counts,
            "total_chars": sum(int(row["chars"] or 0) for row in rows),
            "pieces": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "chars": row["chars"],
                    "field": _piece_field(row),
                    "origin": (row.get("meta") or {}).get("origin") or "",
                }
                for row in rows
            ],
        }

    # Not backfilled yet: fall back to reading the file once.
    item2, obj = _card_obj(str(args.get("card") or ""))
    payload = _payload(obj)
    fields = [
        {"name": name, "chars": len(str(payload.get(name) or ""))}
        for name in (
            "description",
            "personality",
            "scenario",
            "first_mes",
            "mes_example",
            "creator_notes",
            "system_prompt",
            "post_history_instructions",
        )
        if payload.get(name)
    ]
    greetings = payload.get("alternate_greetings") or []
    book = _entries(obj)
    total = sum(f["chars"] for f in fields) + sum(len(str(e.get("content") or "")) for e in book)
    return {
        "path": item2["stored_path"],
        "spec": _spec_name(obj),
        "name": payload.get("name") or item2.get("title") or "",
        "source": "file",
        "fields": fields,
        "alternate_greetings": len(greetings) if isinstance(greetings, list) else 0,
        "worldbook": {
            "name": (payload.get("character_book") or {}).get("name") or "",
            "entries": len(book),
        },
        "total_chars": total,
    }


def _piece_text(ref: str, name: str) -> tuple[bool, str | None]:
    """Look a card field up in stored pieces. Returns (has_pieces, text)."""
    item = _item_for(ref, "character")
    rows = store().pieces(int(item["id"]), with_text=True)
    if not rows:
        return False, None
    match = re.match(r"^alternate_greetings\[(\d+)\]$", name)
    if match:
        want = int(match.group(1)) + 1
        for row in rows:
            if row["kind"] == "greeting" and int(row.get("ord") or 0) == want:
                return True, row.get("text") or ""
        return True, None
    if name == "first_mes":
        for row in rows:
            if row["kind"] == "greeting" and int(row.get("ord") or 0) == 0:
                return True, row.get("text") or ""
        return True, None
    for row in rows:
        if row["kind"] == "body" and _piece_field(row) == name:
            return True, row.get("text") or ""
    return True, None


def card_field(args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("card") or "")
    name = str(args.get("field") or "")
    has, value = _piece_text(ref, name)
    if has:
        if value is None:
            raise ToolError("ENTRY_NOT_FOUND", f"字段不存在：{name}")
        text = value
    else:
        _, obj = _card_obj(ref)
        payload = _payload(obj)
        match = re.match(r"^alternate_greetings\[(\d+)\]$", name)
        if match:
            greetings = payload.get("alternate_greetings") or []
            index = int(match.group(1))
            if not isinstance(greetings, list) or index >= len(greetings):
                raise ToolError("ENTRY_NOT_FOUND", f"没有第 {index} 条 alternate_greetings")
            value = greetings[index]
        else:
            value = payload.get(name)
            if value is None and name in obj:
                value = obj[name]
            if value is None:
                raise ToolError("ENTRY_NOT_FOUND", f"字段不存在：{name}")
        text = str(value)
    offset = max(0, int(args.get("offset") or 0))
    limit = min(MAX_CHARS, int(args.get("max_chars") or DEFAULT_CHARS))
    chunk = text[offset : offset + limit]
    return {
        "field": name,
        "chars": len(text),
        "offset": offset,
        "content": chunk,
        "truncated": offset + len(chunk) < len(text),
    }


def card_list(args: dict[str, Any]) -> dict[str, Any]:
    limit = min(200, int(args.get("limit") or 20))
    query = str(args.get("query") or "").strip() or None
    items = store().list_items("character", query=query, limit=limit)
    return {
        "items": [
            {"name": it.get("title") or "", "path": it.get("stored_path") or ""} for it in items
        ]
    }


# --- tools: worldbook -----------------------------------------------------


def worldbook_index(args: dict[str, Any]) -> dict[str, Any]:
    """Entry inventory: keys, sizes and flags. Never returns entry text."""
    ref = str(args.get("card") or "")
    _, rows = _pieces(ref)
    stored = [row for row in rows if row["kind"] == "world_entry"]
    if stored:
        out = []
        for row in stored:
            meta = row.get("meta") or {}
            out.append(
                {
                    "id": row["id"],
                    "entry_id": meta.get("entry_id"),
                    "keys": meta.get("keys") or [],
                    "comment": row["title"],
                    "order": meta.get("order", 0),
                    "enabled": bool(meta.get("enabled", True)),
                    "constant": bool(meta.get("constant", False)),
                    "origin": meta.get("origin") or "",
                    "chars": row["chars"],
                }
            )
        out.sort(key=lambda row: (not row["enabled"], row["order"]))
        return {
            "source": "pieces",
            "entries": out,
            "enabled_count": sum(1 for row in out if row["enabled"]),
            "total_chars": sum(int(row["chars"] or 0) for row in out),
        }

    _, obj = _card_obj(ref)
    entries = _entries(obj)
    rows = []
    for entry in entries:
        content = str(entry.get("content") or "")
        rows.append(
            {
                "id": entry.get("id"),
                "entry_id": entry.get("id"),
                "keys": _keys_of(entry),
                "comment": entry.get("comment") or "",
                "order": entry.get("insertion_order", entry.get("order", 0)),
                "enabled": bool(entry.get("enabled", True)),
                "constant": bool(entry.get("constant", False)),
                "chars": len(content),
            }
        )
    rows.sort(key=lambda row: (not row["enabled"], row["order"]))
    return {
        "source": "file",
        "book": (obj.get("character_book") or {}).get("name") or "",
        "entries": rows,
        "enabled_count": sum(1 for row in rows if row["enabled"]),
        "total_chars": sum(row["chars"] for row in rows),
    }


def worldbook_get(args: dict[str, Any]) -> dict[str, Any]:
    """Read stored entries by piece id, paged with offset and a total budget."""
    ref = str(args.get("card") or "")
    wanted = {str(x) for x in (args.get("ids") or [])}
    if not wanted:
        raise ToolError("ENTRY_NOT_FOUND", "ids 为空")
    budget = min(MAX_CHARS, int(args.get("max_chars") or DEFAULT_CHARS))
    offset = max(0, int(args.get("offset") or 0))
    _, rows = _pieces(ref, with_text=True)
    stored = [row for row in rows if row["kind"] == "world_entry"]

    if stored:
        out, used = [], 0
        for row in stored:
            if str(row["id"]) not in wanted:
                continue
            content = row.get("text") or ""
            start = min(offset, len(content))
            chunk = content[start : start + max(0, budget - used)]
            used += len(chunk)
            end = start + len(chunk)
            out.append(
                {
                    "id": row["id"],
                    "comment": row["title"],
                    "offset": start,
                    "content": chunk,
                    "chars": len(content),
                    "truncated": end < len(content),
                    "next_offset": end if end < len(content) else None,
                }
            )
        if not out:
            raise ToolError("ENTRY_NOT_FOUND", f"世界书里没有这些条目：{sorted(wanted)}")
        return {"entries": out, "returned_chars": used}

    _, obj = _card_obj(ref)
    out, used = [], 0
    for entry in _entries(obj):
        if str(entry.get("id")) not in wanted:
            continue
        content = str(entry.get("content") or "")
        start = min(offset, len(content))
        chunk = content[start : start + max(0, budget - used)]
        used += len(chunk)
        end = start + len(chunk)
        out.append(
            {
                "id": entry.get("id"),
                "comment": entry.get("comment") or "",
                "offset": start,
                "content": chunk,
                "chars": len(content),
                "truncated": end < len(content),
                "next_offset": end if end < len(content) else None,
            }
        )
    if not out:
        raise ToolError("ENTRY_NOT_FOUND", f"世界书里没有这些条目：{sorted(wanted)}")
    return {"entries": out, "returned_chars": used}


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text)
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def worldbook_match(args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("card") or "")
    text = str(args.get("text") or "")
    if not text.strip():
        raise ToolError("VALIDATION_FAILED", "text 为空")
    top_k = max(1, min(30, int(args.get("top_k") or 8)))
    include = bool(args.get("include_content"))
    budget = min(MAX_CHARS, int(args.get("max_chars") or 3000))
    text_l = text.lower()
    text_grams = _bigrams(text)

    _, rows = _pieces(ref, with_text=True)
    stored = [row for row in rows if row["kind"] == "world_entry"]
    if stored:
        scored = []
        for row in stored:
            meta = row.get("meta") or {}
            if not meta.get("enabled", True):
                continue
            content = row.get("text") or ""
            hits = sum(1 for key in (meta.get("keys") or []) if str(key).lower() in text_l)
            grams = _bigrams(content[:1200])
            overlap = len(grams & text_grams) / len(grams) if grams else 0.0
            score = round(hits + 0.5 * overlap, 3)
            if score <= 0:
                continue
            scored.append((score, row, content))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
        matches = []
        for score, row, content in scored[:top_k]:
            hit = {
                "id": row["id"],
                "comment": row["title"],
                "score": score,
                "preview": content[:300],
            }
            if include:
                hit["content"] = content[:budget]
                hit["truncated"] = len(content) > budget
            matches.append(hit)
        return {"source": "pieces", "matches": matches}

    _, obj = _card_obj(ref)
    scored = []
    for entry in _entries(obj):
        if not entry.get("enabled", True):
            continue
        content = str(entry.get("content") or "")
        hits = sum(1 for key in _keys_of(entry) if key.lower() in text_l)
        grams = _bigrams(content[:1200])
        overlap = len(grams & text_grams) / len(grams) if grams else 0.0
        score = round(hits + 0.5 * overlap, 3)
        if score <= 0:
            continue
        scored.append((score, entry, content))
    scored.sort(key=lambda row: (-row[0], str(row[1].get("comment") or "")))
    matches = []
    for score, entry, content in scored[:top_k]:
        hit = {
            "id": entry.get("id"),
            "comment": entry.get("comment") or "",
            "score": score,
            "preview": content[:300],
        }
        if include:
            hit["content"] = content[:budget]
            hit["truncated"] = len(content) > budget
        matches.append(hit)
    return {"source": "file", "matches": matches}


# --- tools: presets -------------------------------------------------------


def _preset_obj(ref: str) -> dict[str, Any]:
    item = _item_for(ref, "preset")
    path = store().abspath(item["stored_path"])
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("CARD_NOT_FOUND", f"预设无法解析：{exc}") from exc
    if not isinstance(obj, dict):
        raise ToolError("CARD_NOT_FOUND", "预设不是 JSON 对象")
    return obj


def _blocks(preset: dict[str, Any]) -> list[dict[str, Any]]:
    prompts = preset.get("prompts")
    if isinstance(prompts, dict):
        prompts = list(prompts.values())
    return [p for p in (prompts or []) if isinstance(p, dict)]


def preset_blocks(args: dict[str, Any]) -> dict[str, Any]:
    preset = _preset_obj(str(args.get("preset") or ""))
    whitelist_only = bool(args.get("whitelist_only", True))
    blocks, excluded = [], 0
    for block in _blocks(preset):
        ident = str(block.get("identifier") or block.get("name") or "")
        if whitelist_only and ident not in PRESET_WHITELIST:
            excluded += 1
            continue
        blocks.append(
            {
                "identifier": ident,
                "name": block.get("name") or "",
                "role": block.get("role") or "system",
                "enabled": bool(block.get("enabled", True)),
                "chars": len(str(block.get("content") or "")),
            }
        )
    return {
        "blocks": blocks,
        "excluded": excluded,
        "excluded_hint": "jailbreak / regex / 工具块已按白名单过滤" if excluded else "",
    }


def preset_block(args: dict[str, Any]) -> dict[str, Any]:
    preset = _preset_obj(str(args.get("preset") or ""))
    ident = str(args.get("identifier") or "")
    for block in _blocks(preset):
        if str(block.get("identifier") or "") == ident:
            content = str(block.get("content") or "")
            limit = min(MAX_CHARS, int(args.get("max_chars") or DEFAULT_CHARS))
            return {
                "identifier": ident,
                "name": block.get("name") or "",
                "role": block.get("role") or "system",
                "content": content[:limit],
                "chars": len(content),
                "truncated": len(content) > limit,
            }
    raise ToolError("ENTRY_NOT_FOUND", f"预设里没有块：{ident}")


# --- masks ----------------------------------------------------------------


def _mask_path(slug: str) -> Path:
    return MASK_ROOT / f"{slug}.mask.md"


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.isdigit():
        return int(raw)
    return raw


def _frontmatter(text: str) -> tuple[dict[str, Any], str, str]:
    if not text.startswith("---"):
        return {}, text, "缺少 frontmatter"
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text, "frontmatter 未闭合"
    meta: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return meta, text, f"frontmatter 无法解析：{line}"
        key, value = line.split(":", 1)
        meta[key.strip()] = _scalar(value)
    return meta, text[end + 4 :].lstrip("\n"), ""


def _sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    current = ""
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current:
                found[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current:
            buf.append(line)
    if current:
        found[current] = "\n".join(buf).strip()
    return found


def _first_mes_of(obj: dict[str, Any]) -> str:
    value = _payload(obj).get("first_mes") or ""
    return str(value)


def validate_mask(content: str, slug: str, source_card: str = "") -> tuple[list[dict], list[dict], dict, str]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    meta, body, problem = _frontmatter(content)
    if problem:
        return [{"code": "VALIDATION_FAILED", "message": problem}], [], meta, body
    if meta.get("schema") != "tavern-mask/v1":
        errors.append({"code": "VALIDATION_FAILED", "message": "schema 必须是 tavern-mask/v1"})
    if not SLUG_RE.match(slug):
        errors.append({"code": "VALIDATION_FAILED", "message": f"slug 不合法：{slug}"})
    if str(meta.get("slug") or "") != slug:
        errors.append({"code": "VALIDATION_FAILED", "message": "frontmatter 的 slug 与入参不一致"})

    sections = _sections(body)
    for name in REQUIRED_SECTIONS:
        if not sections.get(name):
            errors.append({"code": "VALIDATION_FAILED", "message": f"缺少必填段：## {name}"})

    size = len(body)
    if size < 200 or size > 12000:
        errors.append({"code": "VALIDATION_FAILED", "message": f"正文长度 {size}，应在 200–12000"})

    bad_macros = sorted({m for m in MACRO_RE.findall(body) if m.strip() not in ALLOWED_MACROS})
    if bad_macros:
        errors.append(
            {"code": "VALIDATION_FAILED", "message": f"非白名单宏：{', '.join(bad_macros)}"}
        )

    refs = meta.get("worldbook_refs") or []
    if source_card:
        _, obj = _card_obj(source_card)
        _, stored_rows = _pieces(source_card)
        entries = [row for row in stored_rows if row["kind"] == "world_entry"]
        if entries:
            known = {str(row["id"]) for row in entries}
        else:
            known = {str(entry.get("id")) for entry in _entries(obj)}
        missing = [str(r) for r in refs if str(r) not in known]
        if missing:
            errors.append(
                {"code": "REF_OUT_OF_SCOPE", "message": f"worldbook_refs 含已不存在条目：{', '.join(missing)}"}
            )
        others = [
            it
            for it in store().list_items("character", limit=80)
            if (it.get("title") or "") != (_payload(obj).get("name") or "")
        ]
        for item in others:
            try:
                _, other = _card_obj(item.get("stored_path") or "")
            except ToolError:
                continue
            first = _first_mes_of(other)[:2000]
            if any(first[i : i + 40] in body for i in range(0, max(0, len(first) - 40))):
                warnings.append(
                    {
                        "code": "LEAK_SUSPECT",
                        "message": f"与《{item.get('title')}》的开场白有 40 字以上重合",
                    }
                )
                break
    elif refs:
        warnings.append({"code": "REF_WITHOUT_SOURCE", "message": "有 worldbook_refs 但没有 source_card"})

    status = str(meta.get("status") or "")
    if status and status not in {"draft", "pending", "approved"}:
        warnings.append({"code": "VALIDATION_FAILED", "message": f"未知 status：{status}"})
    return errors, warnings, meta, body


def mask_list(args: dict[str, Any]) -> dict[str, Any]:
    if not MASK_ROOT.is_dir():
        return {"items": []}
    want_status = str(args.get("status") or "")
    want_card = str(args.get("source_card") or "")
    items = []
    for path in sorted(MASK_ROOT.glob("*.mask.md")):
        meta, _, problem = _frontmatter(path.read_text(encoding="utf-8"))
        if problem:
            continue
        if want_status and str(meta.get("status") or "") != want_status:
            continue
        if want_card and str(meta.get("source_card") or "") != want_card:
            continue
        items.append(
            {
                "slug": path.name[: -len(".mask.md")],
                "title": meta.get("title") or "",
                "status": meta.get("status") or "",
                "revision": meta.get("revision") or 0,
                "source_card": meta.get("source_card") or "",
            }
        )
    return {"items": items}


def mask_get(args: dict[str, Any]) -> dict[str, Any]:
    slug = str(args.get("slug") or "")
    path = _mask_path(slug)
    if not path.is_file():
        raise ToolError("CARD_NOT_FOUND", f"面具不存在：{slug}")
    text = path.read_text(encoding="utf-8")
    meta, body, problem = _frontmatter(text)
    if problem:
        raise ToolError("VALIDATION_FAILED", problem)
    return {"slug": slug, "revision": meta.get("revision") or 0, "frontmatter": meta, "body": body}


def mask_validate(args: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content") or "")
    slug = str(args.get("slug") or "")
    errors, warnings, _, _ = validate_mask(content, slug, str(args.get("source_card") or ""))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def mask_save(args: dict[str, Any]) -> dict[str, Any]:
    slug = str(args.get("slug") or "")
    content = str(args.get("content") or "")
    source_card = str(args.get("source_card") or "")
    dry_run = bool(args.get("dry_run", True))
    expected = args.get("expected_revision")

    errors, warnings, meta, _ = validate_mask(content, slug, source_card)
    if errors:
        raise ToolError("VALIDATION_FAILED", f"{len(errors)} 项校验未通过", errors=errors)

    path = _mask_path(slug)
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    current = int(_frontmatter(old)[0].get("revision") or 0) if old else 0
    if old and expected is not None and int(expected) != current:
        raise ToolError("CONFLICT", f"版本冲突：当前 revision={current}，入参 {expected}")

    revision = current + 1
    meta["revision"] = revision
    if args.get("status"):
        meta["status"] = str(args["status"])
    body = content.split("\n---", 1)[1]
    head = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v}" for k, v in meta.items())
    new_text = f"---\n{head}\n---{body}"

    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/masks/{slug}.mask.md",
            tofile=f"b/masks/{slug}.mask.md",
            n=2,
        )
    )
    if dry_run:
        return {"ok": True, "saved": False, "revision": revision, "diff": diff, "warnings": warnings}

    if old:
        backup_dir = MASK_ROOT / ".revisions" / slug
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = str(_frontmatter(old)[0].get("updated_at") or current).replace(":", "").replace("-", "")
        (backup_dir / f"{stamp}.mask.md").write_text(old, encoding="utf-8")
    MASK_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "saved": True, "revision": revision, "diff": diff, "warnings": warnings}


# --- protocol -------------------------------------------------------------

_STR = {"type": "string"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "card_index",
        "description": "列出卡片已存片段（正文 / 开场白 / 世界书条目）的分布与字数，不返回正文。写面具时第一个调用。",
        "inputSchema": {"type": "object", "properties": {"card": _STR}, "required": ["card"]},
    },
    {
        "name": "card_field",
        "description": "读单个字段全文，支持 alternate_greetings[i]。",
        "inputSchema": {
            "type": "object",
            "properties": {"card": _STR, "field": _STR, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}},
            "required": ["card", "field"],
        },
    },
    {
        "name": "card_list",
        "description": "按关键词搜卡片，返回名称与路径。",
        "inputSchema": {"type": "object", "properties": {"query": _STR, "limit": {"type": "integer"}}},
    },
    {
        "name": "worldbook_index",
        "description": "世界书索引：id/keys/comment/字数，不含正文。",
        "inputSchema": {"type": "object", "properties": {"card": _STR}, "required": ["card"]},
    },
    {
        "name": "worldbook_get",
        "description": "按片段 id 批量取正文；超长时用 next_offset 续读。受 max_chars 总预算限制。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card": _STR,
                "ids": {"type": "array", "items": {"type": ["string", "integer"]}},
                "max_chars": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["card", "ids"],
        },
    },
    {
        "name": "worldbook_match",
        "description": "用面具草稿匹配世界书，返回最相关条目的摘要。先匹配再 get。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card": _STR,
                "text": _STR,
                "top_k": {"type": "integer"},
                "include_content": {"type": "boolean"},
                "max_chars": {"type": "integer"},
            },
            "required": ["card", "text"],
        },
    },
    {
        "name": "preset_blocks",
        "description": "列出预设块，默认只保留主提示/角色定义/世界书前后/nsfw。",
        "inputSchema": {
            "type": "object",
            "properties": {"preset": _STR, "whitelist_only": {"type": "boolean"}},
            "required": ["preset"],
        },
    },
    {
        "name": "preset_block",
        "description": "读单个预设块全文。",
        "inputSchema": {
            "type": "object",
            "properties": {"preset": _STR, "identifier": _STR, "max_chars": {"type": "integer"}},
            "required": ["preset", "identifier"],
        },
    },
    {
        "name": "mask_list",
        "description": "列出现有面具。",
        "inputSchema": {"type": "object", "properties": {"status": _STR, "source_card": _STR}},
    },
    {
        "name": "mask_get",
        "description": "读取单个面具。",
        "inputSchema": {"type": "object", "properties": {"slug": _STR}, "required": ["slug"]},
    },
    {
        "name": "mask_validate",
        "description": "只校验不落盘。",
        "inputSchema": {
            "type": "object",
            "properties": {"content": _STR, "slug": _STR, "source_card": _STR},
            "required": ["content", "slug"],
        },
    },
    {
        "name": "mask_save",
        "description": "保存面具。dry_run 默认 true，模型必须显式传 false 才落盘。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": _STR,
                "content": _STR,
                "source_card": _STR,
                "dry_run": {"type": "boolean"},
                "expected_revision": {"type": "integer"},
                "status": _STR,
            },
            "required": ["slug", "content"],
        },
    },
]

HANDLERS = {
    "card_index": card_index,
    "card_field": card_field,
    "card_list": card_list,
    "worldbook_index": worldbook_index,
    "worldbook_get": worldbook_get,
    "worldbook_match": worldbook_match,
    "preset_blocks": preset_blocks,
    "preset_block": preset_block,
    "mask_list": mask_list,
    "mask_get": mask_get,
    "mask_validate": mask_validate,
    "mask_save": mask_save,
}

READONLY = os.environ.get("TAVERN_MCP_READONLY") == "1"
WRITE_TOOLS = {"mask_save"}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError("VALIDATION_FAILED", f"未知工具：{name}")
    if READONLY and name in WRITE_TOOLS:
        raise ToolError("READONLY", "服务以只读模式启动")
    return handler(args)


def _ok(result: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _err(exc: ToolError) -> dict[str, Any]:
    payload = {"code": exc.code, "message": exc.message}
    if exc.details:
        payload["details"] = exc.details
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": True}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    ident = msg.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tavern-mcp", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": ident, "result": _ok(result)}
        except ToolError as exc:
            return {"jsonrpc": "2.0", "id": ident, "result": _err(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": ident, "result": _err(ToolError("INTERNAL", str(exc)))}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if ident is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
