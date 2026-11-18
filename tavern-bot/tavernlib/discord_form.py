"""Discord questionnaire views for library records.

Interactions use stable custom_ids (tlib:...) so they survive bot restarts.
The bot dispatches them from on_interaction; this module builds views and
applies the resulting edits to the store.
"""

from __future__ import annotations

from typing import Any, Callable

import discord

from .format_card import format_form_text
from .paths import TYPE_LABELS
from .store import LibraryStore
from .taxonomy import (
    BUCKET_IDS,
    BUCKET_LABELS,
    LICENSE_FIELDS,
    LICENSE_VALUES,
    LINK_KINDS,
    MAX_TAG_NAME,
    MAX_THEME_PATH,
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    THEME_SEEDS,
    normalize_tag_name,
    normalize_theme_path,
    parse_list_field,
)

CUSTOM_PREFIX = "tlib:"


def cid(*parts: object) -> str:
    value = CUSTOM_PREFIX + ":".join(str(p) for p in parts)
    return value[:100]


def parse_cid(custom_id: str) -> tuple[str, int, str] | None:
    if not custom_id or not custom_id.startswith(CUSTOM_PREFIX):
        return None
    rest = custom_id[len(CUSTOM_PREFIX) :]
    parts = rest.split(":")
    if len(parts) < 2:
        return None
    action = parts[0]
    try:
        item_id = int(parts[1])
    except ValueError:
        return None
    extra = ":".join(parts[2:])
    return action, item_id, extra


def _btn(label: str, custom_id: str, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> discord.ui.Button:
    return discord.ui.Button(label=label[:80], custom_id=custom_id, style=style)


def dashboard_view(item_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_btn("类型", cid("sec", item_id, "type")))
    view.add_item(_btn("主题分类", cid("sec", item_id, "theme")))
    view.add_item(_btn("标签", cid("sec", item_id, "tags")))
    view.add_item(_btn("许可", cid("sec", item_id, "license")))
    view.add_item(_btn("推荐", cid("sec", item_id, "rec")))
    view.add_item(_btn("基本信息", cid("sec", item_id, "info")))
    view.add_item(_btn("关联", cid("sec", item_id, "links")))
    view.add_item(_btn("确认完毕", cid("confirm", item_id), discord.ButtonStyle.success))
    view.add_item(_btn("标记待确认", cid("pending", item_id), discord.ButtonStyle.secondary))
    return view


def open_form_view(item_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_btn("整理标签", cid("open", item_id), discord.ButtonStyle.primary))
    return view


def _back_and_home(view: discord.ui.View, item_id: int) -> None:
    view.add_item(_btn("返回总览", cid("open", item_id), discord.ButtonStyle.primary))


def type_view(item_id: int, current: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    options = [
        discord.SelectOption(label=label, value=key, default=(key == current))
        for key, label in TYPE_LABELS.items()
    ]
    select = discord.ui.Select(
        placeholder="选择类型",
        custom_id=cid("sel", item_id, "type"),
        min_values=1,
        max_values=1,
        options=options,
    )
    view.add_item(select)
    _back_and_home(view, item_id)
    return view


def theme_view(item_id: int, typ: str, selected: list[str], extra_paths: list[str]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    seeds = list(THEME_SEEDS.get(typ) or [])
    paths: list[str] = []
    for path in [*selected, *seeds, *extra_paths]:
        cleaned = normalize_theme_path(path)
        if cleaned and cleaned not in paths:
            paths.append(cleaned)
        if len(paths) >= 24:
            break
    options = [discord.SelectOption(label="（清空主题）", value="__clear__", default=False)]
    selected_set = set(selected)
    for path in paths:
        options.append(discord.SelectOption(label=path[:100], value=path[:100], default=path in selected_set))
    select = discord.ui.Select(
        placeholder="选择主题分类（可多选）",
        custom_id=cid("sel", item_id, "theme"),
        min_values=1,
        max_values=min(25, len(options)),
        options=options,
    )
    view.add_item(select)
    view.add_item(_btn("自定义主题", cid("modal", item_id, "theme")))
    _back_and_home(view, item_id)
    return view


def tags_home_view(item_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for bucket, label in BUCKET_LABELS.items():
        if bucket == "license":
            continue  # licenses have a dedicated step
        view.add_item(_btn(label, cid("tagbucket", item_id, bucket)))
    _back_and_home(view, item_id)
    return view


def order_tag_choices(choices: list[dict[str, Any]], selected_ids: set[int]) -> list[dict[str, Any]]:
    """已选标签排在前面，未选保持原有顺序。

    tag_bucket_view 和 apply_tag_select 必须用同一个顺序来计算前 24 个可见选项，
    否则整桶替换时会把"已选但没显示出来"的标签悄悄删掉。
    """
    selected = [t for t in choices if int(t["id"]) in selected_ids]
    rest = [t for t in choices if int(t["id"]) not in selected_ids]
    return selected + rest


def tag_bucket_view(item_id: int, bucket: str, choices: list[dict[str, Any]], selected_ids: set[int]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    options: list[discord.SelectOption] = [discord.SelectOption(label="（不选 / 清空这类）", value="__clear__")]
    for tag in order_tag_choices(choices, selected_ids)[:24]:
        flag = ""
        if tag.get("pending"):
            flag = "（待确认）"
        options.append(
            discord.SelectOption(
                label=f"{tag['name']}{flag}"[:100],
                value=str(tag["id"]),
                default=int(tag["id"]) in selected_ids,
            )
        )
    select = discord.ui.Select(
        placeholder=f"选择{BUCKET_LABELS.get(bucket, bucket)}标签",
        custom_id=cid("tagsel", item_id, bucket),
        min_values=1,
        max_values=min(25, len(options)),
        options=options,
    )
    view.add_item(select)
    view.add_item(_btn("自定义标签", cid("modal", item_id, f"tag:{bucket}")))
    if bucket == "basic":
        view.add_item(_btn("填写年龄/称呼", cid("modal", item_id, "basic")))
    view.add_item(_btn("返回标签类别", cid("sec", item_id, "tags")))
    _back_and_home(view, item_id)
    return view


def license_view(item_id: int, item: dict[str, Any]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for field, label in LICENSE_FIELDS:
        current = item.get(field) or "unset"
        options = [
            discord.SelectOption(label=lab, value=val, default=(val == current))
            for val, lab in LICENSE_VALUES
        ]
        view.add_item(
            discord.ui.Select(
                placeholder=label,
                custom_id=cid("sel", item_id, field),
                min_values=1,
                max_values=1,
                options=options,
            )
        )
    view.add_item(_btn("许可备注", cid("modal", item_id, "licnote")))
    _back_and_home(view, item_id)
    return view


def rec_view(item_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_btn("填写推荐", cid("modal", item_id, "rec"), discord.ButtonStyle.primary))
    _back_and_home(view, item_id)
    return view


def info_view(item_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_btn("编辑基本信息", cid("modal", item_id, "info"), discord.ButtonStyle.primary))
    _back_and_home(view, item_id)
    return view


def links_view(item_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    options = [discord.SelectOption(label=label, value=key) for key, lab in LINK_KINDS]
    view.add_item(
        discord.ui.Select(
            placeholder="选择关联类型，再填写对方编号",
            custom_id=cid("sel", item_id, "linkkind"),
            min_values=1,
            max_values=1,
            options=options,
        )
    )
    view.add_item(_btn("删除关联（填写编号）", cid("modal", item_id, "linkdel")))
    _back_and_home(view, item_id)
    return view


def section_view(item: dict[str, Any], section: str, store: LibraryStore) -> discord.ui.View:
    item_id = int(item["id"])
    typ = item.get("type") or "other"
    if section == "home":
        return dashboard_view(item_id)
    if section == "type":
        return type_view(item_id, typ)
    if section == "theme":
        extras = [row["path"] for row in store.list_theme_categories(typ)]
        return theme_view(item_id, typ, list(item.get("themes") or []), extras)
    if section == "tags":
        return tags_home_view(item_id)
    if section.startswith("tag:"):
        bucket = section.split(":", 1)[1]
        if bucket not in BUCKET_IDS:
            return tags_home_view(item_id)
        choices = store.list_tags(bucket)
        selected = {int(t["id"]) for t in (item.get("tags") or []) if t.get("bucket") == bucket}
        return tag_bucket_view(item_id, bucket, choices, selected)
    if section == "license":
        return license_view(item_id, item)
    if section == "rec":
        return rec_view(item_id)
    if section == "info":
        return info_view(item_id)
    if section == "links":
        return links_view(item_id)
    return dashboard_view(item_id)


def build_modal(item: dict[str, Any], kind: str) -> discord.ui.Modal:
    item_id = int(item["id"])
    if kind == "info":
        modal = discord.ui.Modal(title="编辑基本信息", custom_id=cid("msub", item_id, "info"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="标题", custom_id="title", default=(item.get("title") or "")[:100], max_length=100, required=True))
        modal.add_item(discord.ui.TextInput(label="作者", custom_id="author", default=(item.get("author") or "")[:100], max_length=100, required=False))
        modal.add_item(discord.ui.TextInput(label="来源链接", custom_id="source_url", default=(item.get("source_url") or "")[:200], max_length=200, required=False))
        modal.add_item(discord.ui.TextInput(label="简介摘要", custom_id="summary", style=discord.TextStyle.paragraph, default=(item.get("summary") or "")[:800], max_length=800, required=False))
        modal.add_item(discord.ui.TextInput(label="备注", custom_id="note", style=discord.TextStyle.paragraph, default=(item.get("note") or "")[:800], max_length=800, required=False))
        return modal
    if kind == "rec":
        modal = discord.ui.Modal(title="推荐预设 / 模型 / 画师串", custom_id=cid("msub", item_id, "rec"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="推荐预设（逗号分隔）", custom_id="recommended_presets", style=discord.TextStyle.paragraph, default="，".join(item.get("recommended_presets") or []), max_length=500, required=False))
        modal.add_item(discord.ui.TextInput(label="推荐模型（逗号分隔）", custom_id="recommended_models", style=discord.TextStyle.paragraph, default="，".join(item.get("recommended_models") or []), max_length=500, required=False))
        modal.add_item(discord.ui.TextInput(label="推荐画师串（逗号分隔）", custom_id="recommended_artist_strings", style=discord.TextStyle.paragraph, default="，".join(item.get("recommended_artist_strings") or []), max_length=800, required=False))
        return modal
    if kind == "theme":
        modal = discord.ui.Modal(title="自定义主题分类", custom_id=cid("msub", item_id, "theme"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="主题路径", custom_id="path", placeholder="例如 科幻/赛博朋克", max_length=MAX_THEME_PATH, required=True))
        return modal
    if kind.startswith("tag:"):
        bucket = kind.split(":", 1)[1]
        label = BUCKET_LABELS.get(bucket, bucket)
        modal = discord.ui.Modal(title=f"自定义{label}标签", custom_id=cid("msub", item_id, f"tag:{bucket}"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="标签（逗号可多个）", custom_id="names", max_length=200, required=True))
        return modal
    if kind == "basic":
        modal = discord.ui.Modal(title="年龄 / 性别 / 称呼", custom_id=cid("msub", item_id, "basic"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="年龄", custom_id="age", required=False, max_length=20, placeholder="例如 成年 或 22"))
        modal.add_item(discord.ui.TextInput(label="性别", custom_id="gender", required=False, max_length=20, placeholder="男 / 女 / 非二元"))
        modal.add_item(discord.ui.TextInput(label="称呼", custom_id="address", required=False, max_length=40, placeholder="例如 老师"))
        return modal
    if kind == "licnote":
        modal = discord.ui.Modal(title="许可备注", custom_id=cid("msub", item_id, "licnote"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="许可说明", custom_id="license_note", style=discord.TextStyle.paragraph, default=(item.get("license_note") or "")[:800], max_length=800, required=False))
        return modal
    if kind.startswith("linkadd:"):
        kind_id = kind.split(":", 1)[1]
        modal = discord.ui.Modal(title="添加关联", custom_id=cid("msub", item_id, f"linkadd:{kind_id}"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="对方资源编号（数字）", custom_id="to_id", required=True, max_length=10))
        modal.add_item(discord.ui.TextInput(label="说明（可选）", custom_id="note", required=False, max_length=100))
        return modal
    if kind == "linkdel":
        modal = discord.ui.Modal(title="删除关联", custom_id=cid("msub", item_id, "linkdel"), timeout=300)
        modal.add_item(discord.ui.TextInput(label="关联编号（总览里的 id）", custom_id="link_id", required=True, max_length=10))
        return modal
    return discord.ui.Modal(title="未支持", custom_id=cid("msub", item_id, "noop"), timeout=30)


def modal_values(interaction: discord.Interaction) -> dict[str, str]:
    out: dict[str, str] = {}
    data = interaction.data or {}
    for row in data.get("components") or []:
        for comp in row.get("components") or []:
            key = comp.get("custom_id") or ""
            out[key] = comp.get("value") or ""
    return out


def select_values(interaction: discord.Interaction) -> list[str]:
    data = interaction.data or {}
    values = data.get("values") or []
    return [str(v) for v in values]


async def safe_respond_modal(interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
    await interaction.response.send_modal(modal)


async def edit_form(
    interaction: discord.Interaction,
    item: dict[str, Any],
    store: LibraryStore,
    section: str = "home",
) -> None:
    text = format_form_text(item, section=section)
    view = section_view(item, section, store)
    if interaction.response.is_done():
        if interaction.message:
            await interaction.message.edit(content=text, view=view)
        else:
            await interaction.followup.send(content=text, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(content=text, view=view)


def apply_select(store: LibraryStore, item: dict[str, Any], section: str, values: list[str]) -> dict[str, Any] | None:
    item_id = int(item["id"])
    if section == "type" and values:
        typ = values[0]
        if typ in TYPE_LABELS and typ != item.get("type"):
            return store.change_type(item_id, typ)
        return store.get_enriched(item_id)
    if section == "theme":
        paths = [] if "__clear__" in values else [normalize_theme_path(v) for v in values if v != "__clear__"]
        store.set_item_themes(item_id, paths, typ=item.get("type"))
        return store.get_enriched(item_id)
    if section in {f for f, _ in LICENSE_FIELDS} and values:
        store.update_fields(item_id, **{section: values[0]})
        return store.get_enriched(item_id)
    if section == "linkkind" and values:
        # Handled by the caller (opens a modal). No store change.
        return store.get_enriched(item_id)
    return store.get_enriched(item_id)


def apply_tag_select(store: LibraryStore, item: dict[str, Any], bucket: str, values: list[str]) -> dict[str, Any] | None:
    item_id = int(item["id"])
    if "__clear__" in values:
        store.set_item_tags(item_id, [], replace_buckets=[bucket], source="user")
        return store.get_enriched(item_id)
    current = {str(t["id"]): t for t in (item.get("tags") or []) if t.get("bucket") == bucket}
    selected_ids = {int(key) for key in current}
    choices = store.list_tags(bucket, include_blacklisted=False)
    id_map = {str(t["id"]): t for t in choices}
    id_map.update(current)
    # 与 tag_bucket_view 相同的可见窗口：已选优先后的前 24 个。
    visible_ids = {str(t["id"]) for t in order_tag_choices(choices, selected_ids)[:24]}
    names: list[dict[str, str]] = []
    # 没显示在这次下拉里的已选标签原样保留；本次提交只改变可见的那部分。
    for key, tag in current.items():
        if key not in visible_ids:
            names.append({"bucket": bucket, "name": tag["name"], "source": "user"})
    for raw in values:
        tag = id_map.get(raw)
        if tag:
            names.append({"bucket": bucket, "name": tag["name"], "source": "user"})
    store.set_item_tags(item_id, names, replace_buckets=[bucket], source="user")
    return store.get_enriched(item_id)


def apply_modal(store: LibraryStore, item: dict[str, Any], kind: str, values: dict[str, str]) -> dict[str, Any] | None:
    item_id = int(item["id"])
    if kind == "info":
        return store.apply_metadata(
            item_id,
            {
                "title": values.get("title") or item.get("title"),
                "author": values.get("author", ""),
                "source_url": values.get("source_url", ""),
                "summary": values.get("summary", ""),
                "note": values.get("note", ""),
            },
            source="user",
        )
    if kind == "rec":
        return store.apply_metadata(
            item_id,
            {
                "recommended_presets": values.get("recommended_presets", ""),
                "recommended_models": values.get("recommended_models", ""),
                "recommended_artist_strings": values.get("recommended_artist_strings", ""),
            },
            source="user",
        )
    if kind == "theme":
        path = normalize_theme_path(values.get("path") or "")
        if path:
            store.add_item_theme(item_id, path)
        return store.get_enriched(item_id)
    if kind.startswith("tag:"):
        bucket = kind.split(":", 1)[1]
        names = parse_list_field(values.get("names") or "")
        assignments = [{"bucket": bucket, "name": normalize_tag_name(n), "source": "user"} for n in names]
        current = [t for t in (item.get("tags") or []) if t.get("bucket") == bucket]
        assignments = [{"bucket": bucket, "name": t["name"], "source": "user"} for t in current] + assignments
        store.set_item_tags(item_id, assignments, replace_buckets=[bucket], source="user")
        return store.get_enriched(item_id)
    if kind == "basic":
        assignments = [{"bucket": "basic", "name": t["name"], "source": "user"} for t in (item.get("tags") or []) if t.get("bucket") == "basic"]
        # 每个字段只替换自己那一类标签，绝不动同桶的其它标签。
        # 性别比较特殊：允许裸值 男/女/非二元/成年，其它写法统一加 性别: 前缀。
        bare_gender = {"男", "女", "非二元", "成年"}

        def drop_matching(pred: Callable[[str], bool]) -> None:
            assignments[:] = [a for a in assignments if not pred(a["name"])]

        def add_name(name: str) -> None:
            cleaned = normalize_tag_name(name)
            if cleaned and all(a["name"] != cleaned for a in assignments):
                assignments.append({"bucket": "basic", "name": cleaned, "source": "user"})

        if (values.get("age") or "").strip():
            drop_matching(lambda n: n.startswith("年龄:"))
            add_name(f"年龄:{values['age'].strip()}")
        if (values.get("gender") or "").strip():
            g = values["gender"].strip()
            drop_matching(lambda n: n in bare_gender or n.startswith("性别:"))
            add_name(g if g in bare_gender else f"性别:{g}")
        if (values.get("address") or "").strip():
            drop_matching(lambda n: n.startswith("称呼:"))
            add_name(f"称呼:{values['address'].strip()}")
        store.set_item_tags(item_id, assignments, replace_buckets=["basic"], source="user")
        return store.get_enriched(item_id)
    if kind == "licnote":
        store.update_fields(item_id, license_note=values.get("license_note") or "")
        return store.get_enriched(item_id)
    if kind.startswith("linkadd:"):
        kind_id = kind.split(":", 1)[1]
        try:
            other = int((values.get("to_id") or "").strip())
        except ValueError:
            return store.get_enriched(item_id)
        store.add_link(item_id, other, kind_id, values.get("note") or "")
        return store.get_enriched(item_id)
    if kind == "linkdel":
        try:
            store.remove_link(int((values.get("link_id") or "").strip()))
        except ValueError:
            pass
        return store.get_enriched(item_id)
    return store.get_enriched(item_id)


def set_review(store: LibraryStore, item_id: int, confirmed: bool) -> dict[str, Any] | None:
    store.update_fields(item_id, review_status=REVIEW_CONFIRMED if confirmed else REVIEW_PENDING)
    return store.get_enriched(item_id)
