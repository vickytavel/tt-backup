"""Fixed paths and type maps. Overridable via environment for tests."""

from __future__ import annotations

import os
from pathlib import Path

LIBRARY_ROOT = Path(os.environ.get("TAVERN_LIBRARY", "/opt/tavern-library")).resolve()
ST_USER_ROOT = Path(os.environ.get("ST_USER_ROOT", "/opt/sillytavern/data/default-user")).resolve()
ALLOWED_ORIGIN = os.environ.get("TAVERN_ALLOWED_ORIGIN", "https://tavern.vickytavel.online")

# Library on-disk type folders. imagegen is catalog-only and usually stays empty.
TYPE_FOLDERS = {
    "character": "character",
    "world": "world",
    "preset": "preset",
    "persona": "persona",
    "style": "style",
    "beautify": "beautify",
    "imagegen": "imagegen",
    "other": "other",
}

TYPE_LABELS = {
    "character": "角色卡",
    "world": "世界书",
    "preset": "预设",
    "persona": "面具",
    "style": "文风",
    "beautify": "美化",
    "imagegen": "生图",
    "other": "其他",
}

RESOURCE_TYPES = tuple(TYPE_LABELS.keys())

SUBTYPE_LABELS = {
    "openai": "Chat Completion",
    "instruct": "Instruct",
    "context": "Context",
    "sysprompt": "系统提示",
    "reasoning": "推理",
    "kobold": "KoboldAI",
    "novel": "NovelAI",
    "textgen": "TextGen",
    "quick-replies": "快速回复",
    "theme": "主题",
    "css": "CSS",
    "background": "背景",
    "font": "字体",
}

# Discord forum channel names (created under category 「酒馆资源库」)
FORUM_CHANNEL_NAMES = {
    "character": "角色卡",
    "world": "世界书",
    "preset": "预设",
    "persona": "面具",
    "style": "文风",
    "beautify": "美化",
    "imagegen": "生图",
    "other": "其他",
}
DISCORD_CATEGORY_NAME = "酒馆资源库"
FORUM_PENDING_TAG = "待确认"

# Maps (type, subtype) -> directory name under the ST user root.
# These names must match SillyTavern USER_DIRECTORY_TEMPLATE.
ST_DEST_DIRS = {
    ("character", None): "characters",
    ("world", None): "worlds",
    ("preset", "openai"): "OpenAI Settings",
    ("preset", "instruct"): "instruct",
    ("preset", "context"): "context",
    ("preset", "sysprompt"): "sysprompt",
    ("preset", "reasoning"): "reasoning",
    ("preset", "kobold"): "KoboldAI Settings",
    ("preset", "novel"): "NovelAI Settings",
    ("preset", "textgen"): "TextGen Settings",
    ("preset", "quick-replies"): "QuickReplies",
    ("preset", "theme"): "themes",
    ("beautify", "theme"): "themes",
    ("beautify", "css"): "themes",
    ("beautify", "background"): "backgrounds",
    ("persona", None): "User Avatars",
}

# Destinations the import helper is allowed to write into (basenames only).
ALLOWED_ST_DIR_NAMES = frozenset(ST_DEST_DIRS.values())

# Types that may be one-click imported into SillyTavern (file-backed only).
IMPORTABLE_TYPES = frozenset({"character", "world", "preset", "persona", "beautify"})

# Types that must not be stored as files in the library filesystem.
CATALOG_ONLY_TYPES = frozenset({"imagegen"})
