"""AI 打标：把片段喂给模型，收敛回受控词表。

只做构造 prompt、调用、解析三件事，不落库。
调用方用 store.set_item_tags(..., source="ai") 写入，标签会自动进入待确认。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import taxonomy as tax

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
# Some reverse proxies sit behind a WAF that rejects the default Python UA
# (Cloudflare answers 1010), so the UA is configurable and no longer left implicit.
DEFAULT_USER_AGENT = "tavern-lib/1.0"

MAX_BODY_CHARS = 3000
MAX_GREETING_CHARS = 800
MAX_ENTRY_PREVIEW = 160
MAX_ENTRIES = 60
MAX_TAGS = 20
MAX_EVIDENCE = 60


class TaggingError(RuntimeError):
    """模型不可用、返回不可解析，或这张卡还没有可打标的内容。"""


def _stored_setting(key: str) -> str:
    """Read a key from library.db `settings` (read-only, no LibraryStore init).

    The web UI persists runtime overrides there (e.g. tag_model). A missing
    table, missing database or a locked file all degrade to "" — the
    environment-based defaults then apply, so tagging never breaks.
    """
    import sqlite3

    try:
        # Read TAVERN_LIBRARY at call time (not import time) so tests and
        # embedded usage can point the lookup at a private library root.
        from .paths import LIBRARY_ROOT

        root = Path(os.environ.get("TAVERN_LIBRARY") or str(LIBRARY_ROOT))
        db = root / "library.db"
        if not db.is_file():
            return ""
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row and row[0] is not None else ""
        finally:
            conn.close()
    except Exception:
        return ""


def config() -> dict[str, Any]:
    """Effective tagging config: env defaults, overridden by library.db settings.

    `tag_model` set from the web UI wins over TAVERN_LLM_MODEL, so both the web
    tag button and the bot's auto-tagging pick up a model switch immediately,
    without a service restart.
    """
    cfg = {
        "base_url": (os.environ.get("TAVERN_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        "api_key": os.environ.get("TAVERN_LLM_API_KEY") or "",
        "model": os.environ.get("TAVERN_LLM_MODEL") or DEFAULT_MODEL,
        "timeout": float(os.environ.get("TAVERN_LLM_TIMEOUT") or "60"),
        "json_mode": (os.environ.get("TAVERN_LLM_JSON_MODE") or "1") not in {"0", "false", "off"},
        "user_agent": (os.environ.get("TAVERN_LLM_USER_AGENT") or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
    }
    stored_model = _stored_setting("tag_model").strip()
    if stored_model:
        cfg["model"] = stored_model
    return cfg


def auto_enabled() -> bool:
    return (os.environ.get("TAVERN_TAG_AUTO") or "").strip().lower() in {"1", "true", "yes", "on"}


def is_configured() -> bool:
    cfg = config()
    if not cfg["base_url"] or not cfg["model"]:
        return False
    return bool(cfg["api_key"]) or "localhost" in cfg["base_url"] or "127.0.0.1" in cfg["base_url"]


def _vocab_text() -> str:
    lines: list[str] = []
    for axis, label in tax.TAG_BUCKETS:
        subs = tax.AXIS_SUBS.get(axis) or []
        lines.append(f"### {label}（{axis}）" + (f" 子类：{'、'.join(subs)}" if subs else ""))
        for record in tax.TAGLIB.get("tags") or []:
            if record.get("axis") != axis:
                continue
            definition = (record.get("definition") or "").strip()
            if len(definition) > 60:
                definition = definition[:60] + "…"
            lines.append(f"- {record['name']}：{definition}")
    return "\n".join(lines)


def _card_context(item: dict[str, Any], pieces: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    head = [f"标题：{item.get('title') or ''}"]
    if item.get("author"):
        head.append(f"作者：{item['author']}")
    if item.get("note"):
        note = str(item["note"]).strip()
        head.append(f"作者简介：{note[:1200]}")
    parts.append("\n".join(head))

    bodies = [p for p in pieces if p["kind"] == "body" and (p.get("text") or "").strip()]
    if bodies:
        parts.append("【正文】\n" + "\n".join(str(p["text"])[:MAX_BODY_CHARS] for p in bodies[:4]))

    greetings = [p for p in pieces if p["kind"] == "greeting" and (p.get("text") or "").strip()]
    if greetings:
        blocks = []
        for index, piece in enumerate(greetings[:4]):
            label = "主线" if index == 0 else f"第 {index + 1} 条"
            blocks.append(f"[{label}] {str(piece['text'])[:MAX_GREETING_CHARS]}")
        parts.append("【开场白】\n" + "\n".join(blocks))

    entries = [p for p in pieces if p["kind"] == "world_entry"]
    if entries:
        lines = []
        for piece in entries[:MAX_ENTRIES]:
            keys = "、".join(str(k) for k in ((piece.get("meta") or {}).get("keys") or []))
            preview = re.sub(r"\s+", " ", str(piece.get("text") or ""))[:MAX_ENTRY_PREVIEW]
            lines.append(f"- {piece.get('title') or ''}（{keys}）：{preview}")
        parts.append(f"【世界书条目】共 {len(entries)} 条\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_messages(item: dict[str, Any], pieces: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = (
        "你���给一个角色卡资料库打标签。规则：\n"
        "1. 只能从提供的词表里选标签，不得创造���表外的名字。\n"
        "2. 每个标签必须有原文依据，evidence 里放不超过 30 字的原文片段。\n"
        "3. 没有依据的轴直接省略，不要凑数；同一轴最多 6 个。\n"
        "4. 保留角色的具体设定，不要只写泛化的类型名。\n"
        '5. 只输出 JSON：{"tags":[{"axis":"轴id","name":"标签名","evidence":"依据"}],"confidence":0.0}'
    )
    user = f"{_card_context(item, pieces)}\n\n【受控词表】\n{_vocab_text()}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise TaggingError("模型没有返回 JSON")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TaggingError(f"模型返回的 JSON 无法解析：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TaggingError("模型返回的 JSON 不是对象")
    return payload


def _call(messages: list[dict[str, str]], cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or config()
    body: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.2,
    }
    if cfg.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": cfg["user_agent"],
            **({"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg["timeout"]) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise TaggingError(f"模型接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TaggingError(f"连不上模型接口：{exc}") from exc
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise TaggingError("模型响应结构不符合 chat/completions 规范") from exc


def normalize(raw_tags: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only vocabulary hits; aliases fold to canonical names, deprecated drop."""
    if not isinstance(raw_tags, list):
        return [], ["tags 不是数组"]
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        if tax.is_deprecated(name):
            dropped.append(f"{name}（已废弃）")
            continue
        record = tax.resolve_tag(name)
        if not record:
            dropped.append(f"{name}（不在词表）")
            continue
        if record["name"] in seen:
            continue
        seen.add(record["name"])
        kept.append(
            {
                "bucket": record["axis"],
                "name": record["name"],
                "tag_id": record.get("id") or "",
                "scope": record.get("scope") or "",
                "evidence": str(raw.get("evidence") or "").strip()[:MAX_EVIDENCE],
            }
        )
    return kept[:MAX_TAGS], dropped


def suggest(
    item: dict[str, Any],
    pieces: list[dict[str, Any]],
    *,
    client: Any = None,
) -> dict[str, Any]:
    """Ask the model for tags. Returns normalized suggestions, never writes."""
    if not pieces:
        raise TaggingError("这张卡还没有片段，先跑一次回��再打标")
    if not is_configured():
        raise TaggingError("未配置模型接口：设置 TAVERN_LLM_BASE_URL / TAVERN_LLM_API_KEY / TAVERN_LLM_MODEL")
    messages = build_messages(item, pieces)
    text = (client or _call)(messages)
    payload = _extract_json(text)
    tags, dropped = normalize(payload.get("tags"))
    return {
        "tags": tags,
        "dropped": dropped,
        "confidence": payload.get("confidence"),
    }


def apply(store: Any, item_id: int, tags: list[dict[str, Any]], *, source: str = "ai") -> list[dict[str, Any]]:
    """Write suggestions through the normal tag path, so they land as pending."""
    return store.set_item_tags(
        item_id,
        [{"bucket": tag["bucket"], "name": tag["name"]} for tag in tags],
        source=source,
    )