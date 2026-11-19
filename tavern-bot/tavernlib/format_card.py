"""Plain-text summaries of a library record, used by Discord and tests."""

from __future__ import annotations

from typing import Any

from .paths import FORUM_CHANNEL_NAMES, SUBTYPE_LABELS, TYPE_LABELS
from .taxonomy import (
    BUCKET_LABELS,
    LICENSE_FIELDS,
    LICENSE_LABELS,
    LINK_LABELS,
    REVIEW_CONFIRMED,
    REVIEW_LABELS,
    WORLDBOOK_ORIGIN_LABELS,
    valid_license,
)

AI_PLACEHOLDER = "以后接入 AI 时，提取结果会先填在这里，需要你确认后才保存。当前尚未启用自动打标。"


def type_label(item: dict[str, Any]) -> str:
    typ = item.get("type") or "other"
    base = TYPE_LABELS.get(typ, typ)
    subtype = item.get("subtype")
    if subtype and SUBTYPE_LABELS.get(subtype):
        return f"{base} · {SUBTYPE_LABELS[subtype]}"
    return base


def _join(values: list[str] | None, empty: str = "（无）") -> str:
    items = [v for v in (values or []) if v]
    return "、".join(items) if items else empty


def format_thread_body(item: dict[str, Any], *, source_url: str = "") -> str:
    lines = [
        f"**{item.get('title') or '未命名'}**",
        f"类型：{type_label(item)}",
    ]
    if item.get("original_filename"):
        lines.append(f"原文件名：{item['original_filename']}")
    if item.get("author"):
        lines.append(f"作者 / 来源：{item['author']}")
    src = item.get("source_url") or source_url
    if src:
        lines.append(f"来源链接：{src}")
    if item.get("summary"):
        lines.append("")
        lines.append(item["summary"][:800])
    elif item.get("note"):
        lines.append("")
        lines.append(str(item["note"])[:800])
    licenses = []
    for key, label in LICENSE_FIELDS:
        licenses.append(f"{label}：{LICENSE_LABELS.get(valid_license(item.get(key)), '未填写')}")
    lines.append("")
    lines.append(" · ".join(licenses))
    if item.get("license_note"):
        lines.append(f"许可备注：{item['license_note'][:300]}")
    themes = item.get("themes") or []
    if themes:
        lines.append("主题：" + _join(themes))
    tags = item.get("tags") or []
    if tags:
        by_bucket: dict[str, list[str]] = {}
        for tag in tags:
            by_bucket.setdefault(tag.get("bucket") or "", []).append(tag.get("name") or "")
        for bucket, names in by_bucket.items():
            label = BUCKET_LABELS.get(bucket, bucket)
            lines.append(f"{label}：" + "、".join(n for n in names if n))
    recs = []
    if item.get("recommended_presets"):
        recs.append("预设 " + _join(item["recommended_presets"], ""))
    if item.get("recommended_models"):
        recs.append("模型 " + _join(item["recommended_models"], ""))
    if item.get("recommended_artist_strings"):
        recs.append("画师串 " + _join(item["recommended_artist_strings"], ""))
    if recs:
        lines.append("推荐：" + "；".join(recs))
    if item.get("worldbook_origin"):
        lines.append("世界书来源：" + WORLDBOOK_ORIGIN_LABELS.get(item["worldbook_origin"], item["worldbook_origin"]))
    review = REVIEW_LABELS.get(item.get("review_status") or "", "待确认")
    lines.append(f"状态：{review}")
    if item.get("catalog_only") or item.get("type") == "imagegen":
        jump = item.get("discord_jump_url") or ""
        extra = f" {jump}" if jump else ""
        lines.append(f"生图原图只保留在 Discord，不写入资源库文件。{extra}".strip())
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1890] + "…"
    return text


def format_form_text(item: dict[str, Any], *, section: str = "home") -> str:
    title = item.get("title") or "未命名"
    review = REVIEW_LABELS.get(item.get("review_status") or "", "待确认")
    header = f"**整理资源** · {title}\n类型：{type_label(item)}　状态：{review}\n"
    if section == "home":
        themes = _join(item.get("themes"))
        tag_bits = []
        for tag in item.get("tags") or []:
            label = BUCKET_LABELS.get(tag.get("bucket"), tag.get("bucket"))
            tag_bits.append(f"{label}:{tag.get('name')}")
        licenses = "，".join(
            f"{label} {LICENSE_LABELS.get(valid_license(item.get(key)), '未填写')}"
            for key, label in LICENSE_FIELDS
        )
        rec = []
        if item.get("recommended_presets"):
            rec.append("预设：" + _join(item["recommended_presets"], ""))
        if item.get("recommended_models"):
            rec.append("模型：" + _join(item["recommended_models"], ""))
        if item.get("recommended_artist_strings"):
            rec.append("画师串：" + _join(item["recommended_artist_strings"], ""))
        links = item.get("links") or []
        link_bits = []
        for link in links:
            other = link.get("to_id_item") if link.get("from_id") == item.get("id") else link.get("from_id_item")
            name = (other or {}).get("title") or f"#{link.get('to_id')}"
            link_bits.append(f"{LINK_LABELS.get(link.get('kind'), link.get('kind'))} → {name}")
        frags = item.get("fragments") or []
        frag_bits = [f"{f.get('title') or f.get('kind')}" for f in frags[:8]]
        lines = [
            header,
            f"作者：{item.get('author') or '（未填）'}",
            f"来源链接：{item.get('source_url') or '（未填）'}",
            f"简介：{(item.get('summary') or '（未填）')[:400]}",
            f"备注：{(item.get('note') or '（无）')[:300]}",
            f"主题分类：{themes}",
            f"标签：{_join(tag_bits)}",
            f"许可：{licenses}",
        ]
        if item.get("license_note"):
            lines.append(f"许可备注：{item['license_note'][:300]}")
        lines.append("推荐：" + ("；".join(rec) if rec else "（未填）"))
        lines.append("关联：" + (_join(link_bits) if link_bits else "（无）"))
        if frag_bits:
            lines.append("已拆出的可检索片段：" + _join(frag_bits) + ("…" if len(frags) > 8 else ""))
        if item.get("ai_prefill"):
            lines.append("AI 建议（待确认）：已预填部分字段，请核对自己的选择优先。")
        else:
            lines.append(AI_PLACEHOLDER)
        lines.append("请用下面的按钮点选，不必打字回复。自定义标签可以点「自定义」。")
        text = "\n".join(lines)
    elif section == "type":
        text = header + "请选择资源类型。文件内容只作为预填，你的选择优先。改类型会尽量把 Discord 子区移到对应频道。"
    elif section == "theme":
        text = header + "请选择主题分类，可多选。没有的话可以点「自定义主题」填写路径，例如 科幻/赛博朋克。"
    elif section == "tags":
        text = header + "请先选一个标签维度，再勾选或自定义。维度固定为世界/设定/玩法/角色/关系/情节/氛围，词表外的标签会标为待确认。你的修改优先于预填。"
    elif section.startswith("tag:"):
        bucket = section.split(":", 1)[1]
        text = header + f"正在编辑「{BUCKET_LABELS.get(bucket, bucket)}」标签，可多选。没有的项目请点「自定义标签」。"
    elif section == "license":
        text = header + "请分别选择二传二改、酒馆使用、小手机使用许可。有条件或其他说明请点「许可备注」。"
    elif section == "rec":
        text = header + "推荐预设、推荐模型和生图画师串用表单填写，多项请用逗号分隔。"
    elif section == "info":
        text = header + "编辑标题、作者、来源链接、简介和备注。"
    elif section == "links":
        text = header + "关联其他资源（角色卡↔世界书、番外、版本、成套包等）。对方编号可以在资源库页面卡片里看到。"
    elif section == "basic":
        text = header + "为角色卡或面具填写年龄、性别、称呼，会记入「基础信息」标签，方便筛选。"
    else:
        text = header
    if len(text) > 1900:
        text = text[:1890] + "…"
    return text


def format_ingest_reply(title: str, *, created: bool, jump: str = "", typ: str = "", uncertain: bool = False, reason: str = "") -> str:
    link = f" {jump}" if jump else ""
    if not created:
        return f"「{title}」已存在，已更新记录。{link}请在子区里点选或修改分类和标签。".strip()
    suffix = ""
    if typ == "other" and reason in {"chat-jsonl", "regex", "chat"}:
        suffix = " 已入库，但不提供一键导入（聊天记录或正则不会写入酒馆设置）。"
    elif typ == "imagegen":
        suffix = " 生图只在 Discord 留图，资源库只记条目。"
    elif uncertain:
        suffix = " 类型是预填的，请在子区里确认或修改。"
    return f"「{title}」已入库。{link}请在子区里点选分类和标签。{suffix}".strip()
