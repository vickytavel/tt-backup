"""Classify SillyTavern resource files by inspecting content, not just the extension."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pngutil import is_png, read_chara_json

PERSONA_HINTS = ("面具", "persona", "mask", "头像", "avatar")
PERSONA_CHANNELS = {"面具"}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
CHARX_EXTS = {".charx"}
JSON_EXTS = {".json"}
CSS_EXTS = {".css"}
CHAT_EXTS = {".jsonl"}

# Keys that strongly identify each preset subtype (need several matches).
_OPENAI_KEYS = {
    "chat_completion_source",
    "openai_model",
    "claude_model",
    "openrouter_model",
    "google_model",
    "custom_url",
    "frequency_penalty",
    "presence_penalty",
}
_INSTRUCT_KEYS = {
    "input_sequence",
    "output_sequence",
    "system_sequence",
    "input_suffix",
    "output_suffix",
    "system_suffix",
    "names_behavior",
    "sequences_as_stop_strings",
}
_CONTEXT_KEYS = {
    "story_string",
    "example_separator",
    "chat_start",
    "story_string_position",
    "story_string_depth",
    "always_force_name2",
}
_KOBOLD_KEYS = {
    "sampler_order",
    "use_default_badwordsids",
    "mirostat_tau",
    "rep_pen_slope",
    "grammar",
}
_NOVEL_KEYS = {
    "phrase_rep_pen",
    "tail_free_sampling",
    "repetition_penalty",
    "repetition_penalty_range",
    "math1_temp",
}
_TEXTGEN_KEYS = {
    "temperature_last",
    "encoder_rep_pen",
    "dynatemp",
    "dynatemp_exponent",
    "smoothing_factor",
    "rep_pen_decay",
    "epsilon_cutoff",
}
_THEME_KEYS = {
    "main_text_color",
    "blur_strength",
    "blur_tint_color",
    "chat_tint_color",
    "bot_mes_blur_tint_color",
    "user_mes_blur_tint_color",
    "shadow_color",
}
_QR_KEYS = {"quickReplyEnabled", "quickReplySlots", "numberOfSlots"}
_REASONING_KEYS = {"prefix", "suffix", "separator"}
_WI_ENTRY_KEYS = {"keys", "key", "content", "uid", "keysecondary", "comment", "constant", "selective", "enabled"}
_CHAR_V1_KEYS = {"first_mes", "personality", "mes_example", "scenario", "description", "char_name"}
_REGEX_KEYS = {"findRegex", "replaceString", "placement", "scriptName"}


@dataclass
class Classification:
    type: str  # character, world, preset, persona, style, beautify, imagegen, other
    subtype: str | None = None
    title: str = ""
    uncertain: bool = False
    reason: str = ""
    card_json: str | None = None  # extracted character card JSON when available
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def importable(self) -> bool:
        if self.type in {"other", "imagegen", "style"}:
            return False
        if self.type == "preset" and self.subtype is None:
            return False
        if self.type == "beautify":
            return self.subtype in {"theme", "css", "background"}
        return self.type in {"character", "world", "preset", "persona"}


def _loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _as_dict(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _title_from(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        for key in ("name", "char_name", "title"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        data = value.get("data")
        if isinstance(data, dict):
            raw = data.get("name")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return fallback


def _stem(filename: str) -> str:
    name = Path(filename or "unnamed").name
    stem = Path(name).stem
    return stem or "unnamed"


def _has_keys(obj: dict, keys: set[str], minimum: int) -> bool:
    return sum(1 for k in keys if k in obj) >= minimum


def _is_chara_spec(obj: dict) -> bool:
    spec = obj.get("spec")
    if isinstance(spec, str) and spec.lower().startswith("chara_card_v"):
        return True
    data = obj.get("data")
    if isinstance(data, dict) and isinstance(obj.get("spec_version"), (str, int)):
        if "name" in data and ("description" in data or "first_mes" in data or "personality" in data):
            return True
    return False


def _is_char_v1(obj: dict) -> bool:
    if _is_chara_spec(obj):
        return True
    name = obj.get("name") or obj.get("char_name")
    if not isinstance(name, str) or not name.strip():
        return False
    return _has_keys(obj, _CHAR_V1_KEYS, 2)


def _is_world(obj: dict) -> bool:
    if "entries" not in obj:
        return False
    entries = obj["entries"]
    sample: Any
    if isinstance(entries, dict):
        if not entries:
            # Empty lorebook still counts if it looks like one (name + entries).
            return "name" in obj or "world" in obj or "description" in obj
        sample = next(iter(entries.values()))
    elif isinstance(entries, list):
        if not entries:
            return "name" in obj
        sample = entries[0]
    else:
        return False
    if isinstance(sample, dict) and _has_keys(sample, _WI_ENTRY_KEYS, 2):
        return True
    # Some lorebooks use a loose shape; if top-level also has common WI fields, accept.
    return isinstance(sample, dict) and ("content" in sample or "keys" in sample or "key" in sample)


def _is_regex(obj: dict) -> bool:
    if _has_keys(obj, _REGEX_KEYS, 2):
        return True
    scripts = obj.get("scripts") or obj.get("regex")
    if isinstance(scripts, list) and scripts and isinstance(scripts[0], dict):
        return _has_keys(scripts[0], _REGEX_KEYS, 2)
    return False


def _is_qr_set(obj: dict) -> bool:
    """SillyTavern Quick Replies v2 set: {"version": 2, "name": ..., "qrList": [{label, message, ...}]}"""
    qr_list = obj.get("qrList")
    if not isinstance(qr_list, list) or not qr_list:
        return False
    first = qr_list[0]
    return isinstance(first, dict) and _has_keys(first, {"label", "message", "contextList"}, 1)


def _looks_like_chat_list(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    first = value[0]
    if not isinstance(first, dict):
        return False
    return any(k in first for k in ("mes", "is_user", "is_name", "send_date", "swipes"))


def classify_json_obj(obj: Any, filename: str = "", hint: str = "") -> Classification | None:
    fallback = _stem(filename)
    if isinstance(obj, list):
        if _looks_like_chat_list(obj):
            return Classification(type="other", title=fallback, reason="chat", uncertain=False)
        # World info sometimes stored as a raw entries array.
        if obj and isinstance(obj[0], dict) and _has_keys(obj[0], _WI_ENTRY_KEYS, 2):
            return Classification(type="world", title=fallback, reason="world-entries-array")
        return None
    data = _as_dict(obj)
    if not data:
        return None

    if _is_chara_spec(data) or _is_char_v1(data):
        title = _title_from(data, fallback)
        return Classification(
            type="character",
            title=title,
            reason="chara-json",
            card_json=json.dumps(data, ensure_ascii=False),
        )

    if _is_world(data):
        return Classification(type="world", title=_title_from(data, fallback), reason="world-json")

    if _is_regex(data):
        return Classification(type="other", title=_title_from(data, fallback), reason="regex", uncertain=False)

    if _has_keys(data, _OPENAI_KEYS, 2) or "chat_completion_source" in data:
        return Classification(type="preset", subtype="openai", title=_title_from(data, fallback), reason="preset-openai")

    if _has_keys(data, _INSTRUCT_KEYS, 3):
        return Classification(type="preset", subtype="instruct", title=_title_from(data, fallback), reason="preset-instruct")

    if _has_keys(data, _CONTEXT_KEYS, 2) or ("story_string" in data and "name" in data):
        return Classification(type="preset", subtype="context", title=_title_from(data, fallback), reason="preset-context")

    # sysprompt is typically {name, content} and maybe post_history. Distinguish from generic docs.
    if (
        isinstance(data.get("name"), str)
        and isinstance(data.get("content"), str)
        and set(data.keys()) <= {"name", "content", "post_history", "postHistory"}
    ):
        return Classification(type="preset", subtype="sysprompt", title=_title_from(data, fallback), reason="preset-sysprompt")

    if (
        isinstance(data.get("name"), str)
        and _has_keys(data, _REASONING_KEYS, 3)
        and "content" not in data
        and "story_string" not in data
    ):
        return Classification(type="preset", subtype="reasoning", title=_title_from(data, fallback), reason="preset-reasoning")

    if _has_keys(data, _NOVEL_KEYS, 2):
        return Classification(type="preset", subtype="novel", title=_title_from(data, fallback), reason="preset-novel")

    if _has_keys(data, _TEXTGEN_KEYS, 2):
        return Classification(type="preset", subtype="textgen", title=_title_from(data, fallback), reason="preset-textgen")

    if _has_keys(data, _KOBOLD_KEYS, 2) or ("rep_pen" in data and "sampler_order" in data):
        return Classification(type="preset", subtype="kobold", title=_title_from(data, fallback), reason="preset-kobold")

    if _is_qr_set(data) or _has_keys(data, _QR_KEYS, 2):
        return Classification(type="preset", subtype="quick-replies", title=_title_from(data, fallback), reason="preset-qr")

    if _has_keys(data, _THEME_KEYS, 3) or ("main_text_color" in data and "name" in data):
        return Classification(type="beautify", subtype="theme", title=_title_from(data, fallback), reason="beautify-theme")

    # A sysprompt-like object with a few extra keys still often is a sysprompt.
    if isinstance(data.get("name"), str) and isinstance(data.get("content"), str) and len(data) <= 6:
        return Classification(
            type="preset",
            subtype="sysprompt",
            title=_title_from(data, fallback),
            reason="preset-sysprompt-loose",
            uncertain=True,
        )

    return None


def _read_text(data: bytes) -> str | None:
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _extract_json_from_bytes(data: bytes) -> Any | None:
    text = _read_text(data)
    if text is None:
        return None
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    return _loads(stripped)


def _hint_wants_persona(filename: str, hint: str, channel_name: str) -> bool:
    blob = " ".join(filter(None, [filename, hint, channel_name])).lower()
    if channel_name in PERSONA_CHANNELS or "面具" in (channel_name or ""):
        return True
    return any(token.lower() in blob for token in PERSONA_HINTS)


def _looks_like_css(data: bytes, filename: str) -> bool:
    if Path(filename).suffix.lower() in CSS_EXTS:
        return True
    text = _read_text(data)
    if not text:
        return False
    sample = text[:4000]
    return bool(re.search(r"[.#]?[\w-]+\s*\{[^}]*:(?:[^;]+;)", sample)) and ("color" in sample or "background" in sample or "--" in sample)


def _is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _is_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff")


def _is_gif(data: bytes) -> bool:
    return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")


def _is_image(data: bytes, filename: str) -> bool:
    if is_png(data) or _is_webp(data) or _is_jpeg(data) or _is_gif(data):
        return True
    return Path(filename).suffix.lower() in IMAGE_EXTS


def _scan_embedded_card(data: bytes, limit: int = 2_000_000) -> str | None:
    """Best-effort scan for an embedded character card in WebP/EXIF/XMP or raw bytes."""
    window = data[:limit]
    # Raw JSON
    for token in (b'"spec":"chara_card_v', b'"spec": "chara_card_v', b"'spec': 'chara_card_v"):
        idx = window.find(token)
        if idx != -1:
            start = window.rfind(b"{", 0, idx)
            if start != -1:
                text = _read_text(window[start : start + 1_000_000])
                if text:
                    obj = _loads(_extract_balanced_json(text))
                    if isinstance(obj, dict) and _is_chara_spec(obj):
                        return json.dumps(obj, ensure_ascii=False)
    # Base64-encoded card
    marker = b"eyJzcGVjI"  # base64 of {"spec":
    idx = window.find(marker)
    if idx != -1:
        sliver = window[idx : idx + 800_000]
        m = re.match(rb"[A-Za-z0-9+/]+=*", sliver)
        if m:
            try:
                decoded = base64_pad_decode(m.group(0))
                text = decoded.decode("utf-8")
                obj = _loads(text)
                if isinstance(obj, dict) and _is_chara_spec(obj):
                    return text
            except (ValueError, UnicodeDecodeError):
                pass
    return None


def _extract_balanced_json(text: str) -> str:
    if not text.startswith("{"):
        return text
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[: i + 1]
    return text


def base64_pad_decode(raw: bytes) -> bytes:
    import base64

    s = raw.strip()
    s += b"=" * (-len(s) % 4)
    return base64.b64decode(s, validate=False)


def read_charx_card(data: bytes) -> tuple[str, bytes | None] | None:
    """Return (card_json, portrait_png_or_none) if this is a CharX archive."""
    if not data.startswith(b"PK"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            card_name = next((n for n in names if n.replace("\\", "/").split("/")[-1].lower() == "card.json"), None)
            if card_name is None:
                return None
            raw = zf.read(card_name)
            text = _read_text(raw)
            if not text:
                return None
            obj = _loads(text)
            if not isinstance(obj, dict) or not (_is_chara_spec(obj) or _is_char_v1(obj)):
                return None
            portrait = None
            for candidate in ("card.png", "image.png", "portrait.png", "avatar.png"):
                match = next((n for n in names if n.replace("\\", "/").split("/")[-1].lower() == candidate), None)
                if match:
                    portrait = zf.read(match)
                    break
            if portrait is None:
                for n in names:
                    if n.lower().endswith(".png"):
                        portrait = zf.read(n)
                        break
            return json.dumps(obj, ensure_ascii=False), portrait
    except (zipfile.BadZipFile, KeyError, OSError):
        return None



FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}

STYLE_HINTS = ("文风", "文笔", "写作风格")
BEAUTIFY_HINTS = ("美化", "主题皮肤", "custom.css", "背景图")
BACKGROUND_HINTS = ("背景", "background")
IMAGEGEN_HINTS = ("生图", "画师串", "画师", "lora", "LoRA", "立绘提示", "prompt")
CHANNEL_TYPE_HINTS = {
    "角色卡": "character",
    "世界书": "world",
    "预设": "preset",
    "面具": "persona",
    "文风": "style",
    "美化": "beautify",
    "生图": "imagegen",
    "其他": "other",
}


def _blob(*parts: str) -> str:
    return " ".join(p for p in parts if p).lower()


def _hint_has(tokens: tuple[str, ...] | list[str], *parts: str) -> bool:
    blob = _blob(*parts)
    return any(token.lower() in blob for token in tokens)


def _hint_type(filename: str, hint: str, channel_name: str) -> str | None:
    if channel_name in CHANNEL_TYPE_HINTS:
        return CHANNEL_TYPE_HINTS[channel_name]
    # Channel names may include prefixes or emoji.
    for name, typ in CHANNEL_TYPE_HINTS.items():
        if name and name in (channel_name or ""):
            return typ
    if _hint_has(IMAGEGEN_HINTS, filename, hint, channel_name):
        return "imagegen"
    if _hint_has(STYLE_HINTS, filename, hint, channel_name):
        return "style"
    if _hint_has(BEAUTIFY_HINTS, filename, hint, channel_name):
        return "beautify"
    return None


def _is_font(filename: str) -> bool:
    return Path(filename).suffix.lower() in FONT_EXTS


def classify(
    data: bytes,
    filename: str = "",
    hint: str = "",
    channel_name: str = "",
) -> Classification:
    """Classify a file. `hint` is the message caption; `channel_name` is the Discord channel."""
    fallback = _stem(filename)
    suffix = Path(filename).suffix.lower()

    # PNG character cards
    if is_png(data):
        card = read_chara_json(data)
        if card:
            obj = _loads(card) or {}
            title = _title_from(obj, fallback) if isinstance(obj, dict) else fallback
            return Classification(type="character", title=title, reason="png-chara", card_json=card)
        return _classify_plain_image(fallback, filename, hint, channel_name, reason_prefix="png")

    # CharX / zip-with-card.json
    if suffix in CHARX_EXTS or data.startswith(b"PK"):
        charx = read_charx_card(data)
        if charx:
            card_json, portrait = charx
            obj = _loads(card_json) or {}
            title = _title_from(obj, fallback) if isinstance(obj, dict) else fallback
            return Classification(
                type="character",
                title=title,
                reason="charx",
                card_json=card_json,
                extra={"portrait": portrait},
            )
        if suffix in CHARX_EXTS:
            return Classification(type="other", title=fallback, reason="charx-invalid", uncertain=True)

    # WebP may carry an embedded card
    if _is_webp(data) or suffix == ".webp":
        card = _scan_embedded_card(data)
        if card:
            obj = _loads(card) or {}
            title = _title_from(obj, fallback) if isinstance(obj, dict) else fallback
            return Classification(type="character", title=title, reason="webp-chara", card_json=card)
        return _classify_plain_image(fallback, filename, hint, channel_name, reason_prefix="webp")

    # Other images (no character chunk)
    if _is_image(data, filename) and suffix not in JSON_EXTS:
        return _classify_plain_image(fallback, filename, hint, channel_name, reason_prefix="image")

    # Chat logs
    if suffix in CHAT_EXTS:
        return Classification(type="other", title=fallback, reason="chat-jsonl")

    # Fonts
    if _is_font(filename):
        return Classification(type="beautify", subtype="font", title=fallback, reason="font-file")

    # CSS themes
    if _looks_like_css(data, filename):
        return Classification(type="beautify", subtype="css", title=fallback, reason="beautify-css")

    # JSON (and JSON-like)
    obj = _extract_json_from_bytes(data)
    if obj is not None:
        result = classify_json_obj(obj, filename=filename, hint=hint)
        if result:
            return _maybe_hint_override(result, filename, hint, channel_name)
        hinted = _hint_type(filename, hint, channel_name)
        if hinted:
            return Classification(type=hinted, title=fallback, reason="json-unknown-hint", uncertain=True)
        return Classification(type="other", title=fallback, reason="json-unknown", uncertain=True)

    if suffix in JSON_EXTS:
        return Classification(type="other", title=fallback, reason="json-invalid", uncertain=True)

    hinted = _hint_type(filename, hint, channel_name)
    if hinted:
        subtype = "font" if hinted == "beautify" and _is_font(filename) else None
        return Classification(type=hinted, subtype=subtype, title=fallback, reason="hint-type", uncertain=True)

    return Classification(type="other", title=fallback, reason="unrecognized", uncertain=True)


def _classify_plain_image(
    fallback: str, filename: str, hint: str, channel_name: str, *, reason_prefix: str
) -> Classification:
    if _hint_wants_persona(filename, hint, channel_name):
        return Classification(type="persona", title=fallback, reason=f"{reason_prefix}-persona-hint")
    hinted = _hint_type(filename, hint, channel_name)
    if hinted == "imagegen":
        return Classification(type="imagegen", title=fallback, reason=f"{reason_prefix}-imagegen-hint")
    if hinted == "beautify" or _hint_has(BACKGROUND_HINTS, filename, hint, channel_name):
        if hinted == "beautify" or _hint_has(BACKGROUND_HINTS, filename, hint):
            # Background screenshots / artist showcases stay imagegen when clearly 生图;
            # otherwise a 背景 hint files the image as beautify/background.
            if hinted != "imagegen":
                return Classification(
                    type="beautify",
                    subtype="background",
                    title=fallback,
                    reason=f"{reason_prefix}-background-hint",
                )
    if hinted == "persona":
        return Classification(type="persona", title=fallback, reason=f"{reason_prefix}-channel-persona")
    return Classification(type="other", title=fallback, reason=f"{reason_prefix}-no-chara", uncertain=True)


def _maybe_hint_override(result: Classification, filename: str, hint: str, channel_name: str) -> Classification:
    """Content classification wins for well-identified ST files. Hints only fill gaps."""
    if result.type in {"character", "world", "preset"} and not result.uncertain:
        return result
    hinted = _hint_type(filename, hint, channel_name)
    if hinted and result.type == "other":
        result.type = hinted
        result.reason = f"{result.reason}+hint"
    return result