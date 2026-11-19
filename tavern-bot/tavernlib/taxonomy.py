"""Tag axes and the controlled vocabulary (词库 v2.2).

The axis structure is fixed: seven axes (世界/设定/玩法/角色/关系/情节/氛围).
Concrete tags live in `taglib.json`, imported verbatim from 酒馆标签词库-v2.2.xlsx.
Tags outside the vocabulary may still be created by hand; tags produced by
AI or heuristics are flagged 待确认, and user corrections always win.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 5

_TAGLIB_PATH = Path(__file__).with_name("taglib.json")
with _TAGLIB_PATH.open(encoding="utf-8") as _fh:
    TAGLIB: dict = json.load(_fh)

# Fixed axes. Do not add informal axes at runtime.
TAG_BUCKETS: list[tuple[str, str]] = [(a["id"], a["label"]) for a in TAGLIB["axes"]]
BUCKET_LABELS = dict(TAG_BUCKETS)
BUCKET_IDS = tuple(k for k, _ in TAG_BUCKETS)
AXIS_SUBS: dict[str, list[str]] = {a["id"]: list(a.get("subs") or []) for a in TAGLIB["axes"]}

# Controlled vocabulary, keyed by canonical name.
TAG_RECORDS: dict[str, dict] = {t["name"]: t for t in TAGLIB["tags"]}
TAG_BY_ID: dict[str, dict] = {t["id"]: t for t in TAGLIB["tags"]}
TAG_ALIASES: dict[str, str] = {}
for _tag in TAGLIB["tags"]:
    for _alias in _tag.get("aliases") or []:
        TAG_ALIASES[_alias] = _tag["name"]
DEPRECATED_TAGS: frozenset[str] = frozenset(TAGLIB.get("deprecated") or [])

# Enumerated card facts that are fields, not free tags.
FIELD_ENUMS: dict[str, list[str]] = {k: list(v) for k, v in (TAGLIB.get("fields") or {}).items()}

# Where a tag may be attached. Card tags also apply to single-character cards.
SCOPE_LABELS: list[tuple[str, str]] = [
    ("卡片", "卡片"),
    ("开场白", "开场白"),
    ("角色", "角色"),
    ("卡片/开场白", "卡片 + 开场白"),
    ("卡片/角色", "卡片 + 角色"),
    ("角色/关系", "角色 + 关系"),
]
SCOPE_LABEL_MAP = dict(SCOPE_LABELS)
SCOPE_IDS = tuple(k for k, _ in SCOPE_LABELS)

LICENSE_FIELDS = [
    ("license_repost", "二传二改"),
    ("license_tavern", "酒馆使用"),
    ("license_phone", "小手机使用"),
]
LICENSE_VALUES: list[tuple[str, str]] = [
    ("unset", "未填写"),
    ("allowed", "允许"),
    ("forbidden", "禁止"),
    ("conditional", "有条件"),
    ("pending", "待确认"),
]
LICENSE_LABELS = dict(LICENSE_VALUES)
LICENSE_IDS = tuple(k for k, _ in LICENSE_VALUES)

REVIEW_PENDING = "pending"
REVIEW_CONFIRMED = "confirmed"
REVIEW_LABELS = {
    REVIEW_PENDING: "待确认",
    REVIEW_CONFIRMED: "已确认",
}

# Nested theme paths, seeded per type. Users can add more later.
# A record may have more than one theme path.
THEME_SEEDS: dict[str, list[str]] = {
    "character": ["现代", "古风", "玄幻", "末世", "校园", "西幻", "科幻", "原著向"],
    "persona": ["现代", "古风", "玄幻", "末世", "校园", "西幻", "科幻", "原著向"],
    "preset": ["对话", "叙事", "文风", "合规", "总结"],
    "world": ["世界观设定", "角色补充", "玩法规则", "lorebook风格"],
    "beautify": ["主题", "背景", "CSS", "字体"],
    "imagegen": ["立绘", "场景", "头像"],
    "style": ["叙事", "对话", "描写"],
    "other": [],
}

# Axis -> canonical tag names, kept for callers that only need the names.
SEED_TAGS: dict[str, list[str]] = {axis: [] for axis in BUCKET_IDS}
for _tag in TAGLIB["tags"]:
    SEED_TAGS[_tag["axis"]].append(_tag["name"])

LINK_KINDS: list[tuple[str, str]] = [
    ("character_worldbook", "角色卡 ↔ 世界书"),
    ("worldbook_extra", "世界书 ↔ 番外"),
    ("character_relation", "角色关系"),
    ("version", "版本"),
    ("bundle", "成套包"),
    ("persona_character", "面具 ↔ 角色卡"),
    ("preset_worldbook", "预设 ↔ 世界书"),
    ("theme_background", "主题 ↔ 背景"),
]
LINK_LABELS = dict(LINK_KINDS)
LINK_IDS = tuple(k for k, _ in LINK_KINDS)

WORLDBOOK_ORIGINS: list[tuple[str, str]] = [
    ("standalone", "独立世界书"),
    ("bound", "绑定世界书"),
    ("embedded", "卡内嵌世界书"),
    ("extra", "番外"),
]
WORLDBOOK_ORIGIN_LABELS = dict(WORLDBOOK_ORIGINS)

FRAGMENT_KINDS: list[tuple[str, str]] = [
    ("style", "文风"),
    ("rule", "规则"),
    ("setting", "设定"),
    ("character", "角色补充"),
    ("other", "其他板块"),
]
FRAGMENT_KIND_LABELS = dict(FRAGMENT_KINDS)

TAG_SOURCES = ("user", "ai", "heuristic", "seed")

# Discord / web input limits.
MAX_TITLE = 100
MAX_TEXT = 2000
MAX_TAG_NAME = 40
MAX_THEME_PATH = 80
MAX_LIST_ITEM = 200
MAX_LIST_ITEMS = 30


def normalize_tag_name(name: str) -> str:
    text = " ".join((name or "").replace("\x00", "").split())
    if text.startswith("#"):
        text = text[1:].strip()
    return text[:MAX_TAG_NAME]


def normalize_theme_path(path: str) -> str:
    parts = [p.strip() for p in (path or "").replace("\\", "/").split("/") if p.strip()]
    cleaned = "/".join(parts)
    return cleaned[:MAX_THEME_PATH]


def resolve_tag(name: str) -> dict | None:
    """Return the canonical vocabulary record for a name or alias, else None."""
    cleaned = normalize_tag_name(name)
    if not cleaned:
        return None
    record = TAG_RECORDS.get(cleaned)
    if record:
        return record
    canonical = TAG_ALIASES.get(cleaned)
    return TAG_RECORDS.get(canonical) if canonical else None


def is_deprecated(name: str) -> bool:
    return normalize_tag_name(name) in DEPRECATED_TAGS


def parse_list_field(value: str | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        text = str(value).replace("\r\n", "\n")
        raw = text.replace("，", ",").replace("\n", ",")
        items = raw.split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = " ".join(str(item).split())[:MAX_LIST_ITEM]
        if s and s not in seen:
            out.append(s)
            seen.add(s)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def dump_list_field(items: list[str] | None) -> str:
    return json.dumps(parse_list_field(items), ensure_ascii=False)


def load_list_field(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return parse_list_field(data)
    except (ValueError, TypeError):
        return parse_list_field(raw)


def valid_license(value: str | None) -> str:
    if value in LICENSE_IDS:
        return value  # type: ignore[return-value]
    return "unset"


def valid_bucket(value: str | None) -> str | None:
    return value if value in BUCKET_IDS else None


def valid_scope(value: str | None) -> str | None:
    return value if value in SCOPE_IDS else None


def valid_link_kind(value: str | None) -> str | None:
    return value if value in LINK_IDS else None


def valid_review(value: str | None) -> str:
    if value in REVIEW_LABELS:
        return value  # type: ignore[return-value]
    return REVIEW_PENDING
