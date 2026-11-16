"""Tag buckets, theme-category seeds, licenses, and link kinds.

The bucket structure is locked. Tags live inside a bucket; unknown AI-invented
tags are flagged 待确认. User corrections always win over heuristics/AI.
"""

from __future__ import annotations

SCHEMA_VERSION = 2

# Fixed tag buckets. Do not add informal buckets at runtime.
TAG_BUCKETS: list[tuple[str, str]] = [
    ("genre", "题材"),
    ("world", "世界观"),
    ("license", "许可"),
    ("recommend", "推荐"),
    ("basic", "基础信息"),
    ("purpose", "用途"),
]
BUCKET_LABELS = dict(TAG_BUCKETS)
BUCKET_IDS = tuple(k for k, _ in TAG_BUCKETS)

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

# Modest starter tags. Not locked; users can add, merge, or blacklist later.
SEED_TAGS: dict[str, list[str]] = {
    "genre": [
        "青梅竹马",
        "强强",
        "年上",
        "年下",
        "暗恋",
        "重生",
        "穿越",
        "契约",
        "校园",
        "宫廷",
        "修仙",
        "机甲",
        "娱乐圈",
        "豪门",
        "治愈",
        "群像",
    ],
    "world": ["现代都市", "古代", "西幻世界", "科幻未来", "末世废土", "原著向"],
    "purpose": ["对话", "叙事", "描写", "合规", "总结", "立绘", "场景", "头像"],
    "basic": ["男", "女", "非二元", "成年"],
    "license": [],
    "recommend": [],
}

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
    import json

    return json.dumps(parse_list_field(items), ensure_ascii=False)


def load_list_field(raw: str | None) -> list[str]:
    import json

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


def valid_link_kind(value: str | None) -> str | None:
    return value if value in LINK_IDS else None


def valid_review(value: str | None) -> str:
    if value in REVIEW_LABELS:
        return value  # type: ignore[return-value]
    return REVIEW_PENDING
