#!/usr/bin/env python3
"""Discord ingest bot: store attachments in the VPS library and file them in forum channels."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord
from discord import ChannelType, ForumChannel, InteractionType

from tavernlib.classify import Classification, classify
from tavernlib.discord_form import (
    apply_modal,
    apply_select,
    apply_tag_select,
    build_modal,
    cid,
    dashboard_view,
    edit_form,
    format_form_text,
    modal_values,
    open_form_view,
    parse_cid,
    section_view,
    select_values,
    set_review,
)
from tavernlib.format_card import format_form_text as _form_text
from tavernlib.format_card import format_ingest_reply, format_thread_body
from tavernlib.paths import (
    CATALOG_ONLY_TYPES,
    DISCORD_CATEGORY_NAME,
    FORUM_CHANNEL_NAMES,
    FORUM_PENDING_TAG,
    LIBRARY_ROOT,
    TYPE_LABELS,
)
from tavernlib.store import LibraryStore
from tavernlib.taxonomy import REVIEW_PENDING

LOG = logging.getLogger("tavern-bot")

MAX_ATTACHMENT_BYTES = 80 * 1024 * 1024
THREAD_NAME_MAX = 100

# 无说明的普通图片（截图、预览图）不建库记录，直接忽略。
IGNORED_IMAGE_REASONS = {"png-no-chara", "webp-no-chara", "image-no-chara"}

# 作者简介暂存：先转发一条带文字的消息，紧接着投递的资源会自动挂上这段简介。
NOTE_CACHE_TTL = 15 * 60
NOTE_CLEAR_WORDS = ("清空简介", "清除简介")


def _is_ignored_image_result(result: Classification) -> bool:
    return result.type == "other" and result.reason in IGNORED_IMAGE_REASONS


def _message_texts(message: discord.Message) -> list[str]:
    parts: list[str] = []
    if (getattr(message, "content", "") or "").strip():
        parts.append(message.content.strip())
    for snap in getattr(message, "message_snapshots", None) or []:
        text = (getattr(snap, "content", None) or "").strip()
        if text and text not in parts:
            parts.append(text)
    return parts


def _merge_note(cached: str, own: str) -> str:
    parts = [p for p in (cached, own) if p]
    return "\n\n".join(parts)


# Loaded from /opt/tavern-bot/.env (also systemd EnvironmentFile).
TOKEN = (os.environ.get("DISCORD_TOKEN") or "").strip()
GUILD_ID_RAW = (os.environ.get("DISCORD_GUILD_ID") or "").strip()
ALLOWED_RAW = (os.environ.get("ALLOWED_USER_IDS") or "").strip()


def _parse_id_list(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ if the key is not already set. Never logs values."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def thread_name(title: str, fallback: str = "未命名资源") -> str:
    text = (title or fallback).replace("\n", " ").replace("\r", " ").strip() or fallback
    text = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in text)
    text = " ".join(text.split())
    return text[:THREAD_NAME_MAX]


def _unpack_thread(created: object) -> tuple[object, object | None]:
    thread = getattr(created, "thread", None) or (created[0] if isinstance(created, tuple) else created)
    message = getattr(created, "message", None)
    if message is None and isinstance(created, tuple) and len(created) > 1:
        message = created[1]
    return thread, message


def _forum_channel_of(message: discord.Message) -> ForumChannel | None:
    channel = message.channel
    if isinstance(channel, ForumChannel):
        return channel
    parent = getattr(channel, "parent", None)
    if isinstance(parent, ForumChannel):
        return parent
    return None


def _thread_of(message: discord.Message) -> discord.Thread | None:
    channel = message.channel
    if isinstance(channel, discord.Thread):
        return channel
    return None


class TavernBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
            or ""
        ).strip() or None
        super().__init__(intents=intents, proxy=proxy)
        if proxy:
            LOG.info("Discord traffic will use the local proxy.")
        self.store = LibraryStore(Path(os.environ.get("TAVERN_LIBRARY", str(LIBRARY_ROOT))))
        self.guild_id: int | None = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None
        self.allowed_ids = _parse_id_list(ALLOWED_RAW)
        self._forums: dict[str, ForumChannel] = {}
        self._layout_lock = asyncio.Lock()
        self._layout_ready = False
        self._refile_task: asyncio.Task | None = None
        self._note_cache: dict[tuple[int, int], tuple[float, str]] = {}

    def configured_guild(self) -> discord.Guild | None:
        if self.guild_id:
            return self.get_guild(self.guild_id)
        guilds = list(self.guilds)
        if len(guilds) == 1:
            self.guild_id = guilds[0].id
            LOG.info("Auto-detected guild id (single guild).")
            return guilds[0]
        if not guilds:
            LOG.error("Bot is not in any guild yet.")
            return None
        LOG.error("Bot is in %s guilds; set DISCORD_GUILD_ID to choose one.", len(guilds))
        return None

    async def is_allowed(self, user: discord.abc.User, *, in_guild: discord.Guild | None) -> bool:
        if user.bot:
            return False
        if self.allowed_ids:
            return user.id in self.allowed_ids
        guild = in_guild or self.configured_guild()
        if guild is None:
            return False
        if in_guild and in_guild.id == guild.id:
            return True
        member = guild.get_member(user.id)
        if member is not None:
            return True
        try:
            await guild.fetch_member(user.id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException as exc:
            LOG.warning("Could not verify guild membership: %s", exc)
            return False

    async def setup_hook(self) -> None:
        self._refile_task = asyncio.create_task(self._refile_loop())

    async def close(self) -> None:
        if self._refile_task:
            self._refile_task.cancel()
        await super().close()

    async def on_ready(self) -> None:
        LOG.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        try:
            await self.ensure_layout()
        except Exception:
            LOG.exception("Failed to ensure Discord channel layout on startup")

    async def _refile_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.flush_refiles()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("flush_refiles failed")
            await asyncio.sleep(20)

    async def flush_refiles(self) -> None:
        items = self.store.list_pending_refile()
        for item in items:
            try:
                await self.refile_item(item)
            except Exception:
                LOG.exception("refile failed for item %s", item.get("id"))

    async def ensure_layout(self) -> None:
        async with self._layout_lock:
            guild = self.configured_guild()
            if guild is None:
                return
            me = guild.me or guild.get_member(self.user.id)  # type: ignore[union-attr]
            if me is None:
                try:
                    me = await guild.fetch_member(self.user.id)  # type: ignore[union-attr]
                except discord.HTTPException:
                    me = None
            can_manage = bool(me and me.guild_permissions.manage_channels)
            if not can_manage:
                LOG.error(
                    "Missing Manage Channels permission; will store files and reply, "
                    "but will skip creating the 「酒馆资源库」 category and forum channels."
                )
                self._forums = self._find_existing_forums(guild)
                self._layout_ready = True
                return

            category = self._find_category(guild)
            if category is None:
                try:
                    category = await guild.create_category(DISCORD_CATEGORY_NAME, reason="酒馆资源库")
                    LOG.info("Created category %s", DISCORD_CATEGORY_NAME)
                except discord.Forbidden:
                    LOG.error("Cannot create category: missing permissions")
                    self._forums = self._find_existing_forums(guild)
                    self._layout_ready = True
                    return
                except discord.HTTPException:
                    LOG.exception("Failed to create category")
                    self._forums = self._find_existing_forums(guild)
                    self._layout_ready = True
                    return

            forums = self._find_existing_forums(guild)
            for typ, name in FORUM_CHANNEL_NAMES.items():
                existing = forums.get(typ)
                if existing is not None:
                    continue
                try:
                    forum = await guild.create_forum(
                        name,
                        category=category,
                        reason="酒馆资源库分类频道",
                    )
                    forums[typ] = forum
                    LOG.info("Created forum channel %s", name)
                except discord.Forbidden:
                    LOG.error("Cannot create forum channel %s: missing permissions", name)
                except discord.HTTPException:
                    LOG.exception("Failed to create forum channel %s", name)
            for typ, forum in list(forums.items()):
                updated = await self._ensure_pending_tag(forum)
                if updated is not None:
                    forums[typ] = updated
            self._forums = forums
            self._layout_ready = True

    async def _ensure_pending_tag(self, forum: ForumChannel) -> ForumChannel | None:
        try:
            current = list(forum.available_tags)
            if any(t.name == FORUM_PENDING_TAG for t in current):
                return forum
            if len(current) >= 20:
                return forum
            edited = await forum.edit(available_tags=current + [discord.ForumTag(name=FORUM_PENDING_TAG)])
            return edited or forum
        except discord.HTTPException:
            LOG.warning("Could not add pending tag to forum %s", forum.name)
            return forum

    def _find_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel) and ch.name == DISCORD_CATEGORY_NAME:
                return ch
        return None

    def _find_existing_forums(self, guild: discord.Guild) -> dict[str, ForumChannel]:
        by_name = {name: typ for typ, name in FORUM_CHANNEL_NAMES.items()}
        found: dict[str, ForumChannel] = {}
        for ch in guild.channels:
            if isinstance(ch, ForumChannel) and ch.name in by_name:
                typ = by_name[ch.name]
                found.setdefault(typ, ch)
        return found

    def _pending_tag(self, forum: ForumChannel) -> discord.ForumTag | None:
        for tag in forum.available_tags:
            if tag.name == FORUM_PENDING_TAG:
                return tag
        return None

    async def _sync_forum_tags(self, thread: discord.Thread, item: dict) -> None:
        parent = getattr(thread, "parent", None)
        if not isinstance(parent, ForumChannel):
            return
        pending = self._pending_tag(parent)
        if pending is None:
            return
        current = list(thread.applied_tags)
        has = any(t.id == pending.id for t in current)
        want = (item.get("review_status") or REVIEW_PENDING) == REVIEW_PENDING
        try:
            if want and not has:
                if len(current) >= 5:
                    current = current[:4]
                await thread.edit(applied_tags=current + [pending])
            elif has and not want:
                await thread.edit(applied_tags=[t for t in current if t.id != pending.id])
        except discord.HTTPException:
            LOG.warning("Could not sync forum tags for thread")

    def _ingest_attachments(self, message: discord.Message) -> list[discord.Attachment]:
        found: list[discord.Attachment] = []
        seen: set[str] = set()
        snapshots = list(getattr(message, "message_snapshots", None) or [])
        groups: list[list[discord.Attachment]] = [list(message.attachments or [])]
        for snap in snapshots:
            groups.append(list(getattr(snap, "attachments", None) or []))
        for group in groups:
            for att in group:
                key = (getattr(att, "id", None) and str(att.id)) or (att.url or att.filename or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                found.append(att)
        return found

    def _ingest_note(self, message: discord.Message) -> str:
        return "\n\n".join(_message_texts(message))

    def _cache_key(self, message: discord.Message) -> tuple[int, int]:
        channel_id = getattr(message.channel, "id", 0) or 0
        return (channel_id, message.author.id)

    def _cached_note(self, message: discord.Message) -> str:
        entry = self._note_cache.get(self._cache_key(message))
        if not entry:
            return ""
        stored_at, text = entry
        if time.time() - stored_at > NOTE_CACHE_TTL:
            self._note_cache.pop(self._cache_key(message), None)
            return ""
        return text

    def _store_note_cache(self, message: discord.Message, text: str) -> None:
        if text:
            self._note_cache[self._cache_key(message)] = (time.time(), text)

    def _is_direct_request(self, message: discord.Message) -> bool:
        if message.guild is None or isinstance(message.channel, discord.DMChannel):
            return True
        if self.user and self.user in (message.mentions or []):
            return True
        name = (getattr(message.channel, "name", "") or "").strip()
        if name in {"投递", "其他"} or name.startswith("投递"):
            return True
        return _forum_channel_of(message) is not None

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        attachments = self._ingest_attachments(message)
        if not attachments:
            if self._is_direct_request(message):
                own_note = self._ingest_note(message)
                if not own_note:
                    LOG.info("Received a message with no file (forward snapshots=%s)",
                             len(getattr(message, "message_snapshots", None) or []))
                    try:
                        await message.reply(
                            "我没有读到文件。请直接上传附件，或转发一条带文件的消息（不要只转发纯文字）。",
                            mention_author=False,
                        )
                    except discord.HTTPException:
                        pass
                    return
                guild = message.guild
                if guild is not None:
                    configured = self.configured_guild()
                    if configured is None or guild.id != configured.id:
                        return
                if not await self.is_allowed(message.author, in_guild=guild):
                    return
                if any(word in own_note for word in NOTE_CLEAR_WORDS):
                    self._note_cache.pop(self._cache_key(message), None)
                    try:
                        await message.reply("已清空本频道的简介暂存。", mention_author=False)
                    except discord.HTTPException:
                        pass
                    return
                self._store_note_cache(message, own_note)
                preview = own_note[:60] + ("…" if len(own_note) > 60 else "")
                try:
                    await message.reply(
                        f"已收到作者简介（{len(own_note)} 字），暂存 {NOTE_CACHE_TTL // 60} 分钟。"
                        f"接下来直接转发角色卡即可，会自动附上这段简介；发「清空简介」可作废。\n预览：{preview}",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
            return
        if not self._layout_ready:
            try:
                await self.ensure_layout()
            except Exception:
                LOG.exception("ensure_layout failed on message")

        guild = message.guild
        if guild is not None:
            configured = self.configured_guild()
            if configured is None or guild.id != configured.id:
                return
        if not await self.is_allowed(message.author, in_guild=guild):
            if guild is None:
                try:
                    await message.channel.send("目前只接受资源库所在服务器成员的投递。")
                except discord.HTTPException:
                    pass
            return

        results: list[str] = []
        ingested_ids: list[int] = []
        cached_note = self._cached_note(message)
        own_note = self._ingest_note(message)
        note = _merge_note(cached_note, own_note)
        author = f"{message.author.display_name} ({message.author.id})"
        channel_name = ""
        if guild is not None and not isinstance(message.channel, discord.DMChannel):
            forum = _forum_channel_of(message)
            if forum is not None:
                channel_name = forum.name
            else:
                channel_name = getattr(message.channel, "name", "") or ""

        LOG.info("Ingesting %s file(s) from %s", len(attachments), author)
        for attachment in attachments:
            line, item_id = await self._handle_attachment(
                message, attachment, note=note, author=author, channel_name=channel_name
            )
            results.append(line)
            if item_id:
                ingested_ids.append(item_id)

        if len(ingested_ids) >= 2:
            try:
                self.store.set_bundle(ingested_ids)
            except Exception:
                LOG.exception("Failed to bind bundle for a multi-file message")

        if ingested_ids:
            if cached_note:
                results.append(f"（已自动附加之前暂存的作者简介 {len(cached_note)} 字）")
        elif own_note:
            # 整条消息没有任何可入库文件（例如转发的作者原帖里只有截图），
            # 把文字暂存为简介，等下一条真正的资源投递挂上。
            self._store_note_cache(message, own_note)
            results.append(
                "没有可入库的文件；已把这条消息的文字暂存为作者简介，"
                "接下来直接转发角色卡即可。发「清空简介」可作废。"
            )

        text = "\n".join(results) if results else "没有可入库的附件。"
        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            try:
                await message.channel.send(text)
            except discord.HTTPException:
                LOG.exception("Failed to send ingest reply")

    async def _handle_attachment(
        self,
        message: discord.Message,
        attachment: discord.Attachment,
        *,
        note: str,
        author: str,
        channel_name: str,
    ) -> tuple[str, int | None]:
        filename = attachment.filename or "unnamed"
        if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
            return f"「{filename}」太大，已跳过。", None

        try:
            data = await attachment.read()
        except discord.HTTPException:
            LOG.exception("Failed to download attachment %s", filename)
            return f"「{filename}」下载失败。", None
        if len(data) > MAX_ATTACHMENT_BYTES:
            return f"「{filename}」太大，已跳过。", None

        result = classify(data, filename=filename, hint=note, channel_name=channel_name)
        if _is_ignored_image_result(result):
            return f"「{filename}」是普通图片，未检测到角色卡数据或说明词，未入库。", None
        title = result.title or Path(filename).stem or "未命名"
        catalog_only = result.type in CATALOG_ONLY_TYPES
        thumb = ""
        if catalog_only or result.type in {"character", "persona", "beautify"}:
            thumb = attachment.proxy_url or attachment.url or ""
        discord_meta = {
            "guild_id": str(message.guild.id) if message.guild else "",
            "channel_id": str(getattr(message.channel, "id", "") or ""),
            "message_id": str(message.id),
            "jump_url": message.jump_url,
        }
        catalog_key = ""
        if catalog_only:
            catalog_key = f"catalog:discord:{message.id}:{attachment.id}"

        item, created = self.store.add_file(
            b"" if catalog_only else data,
            type=result.type,
            subtype=result.subtype,
            title=title,
            original_filename=filename,
            note=note,
            author=author,
            uncertain=result.uncertain,
            reason=result.reason,
            mime_type=attachment.content_type or "",
            source_url=message.jump_url,
            summary="",
            thumbnail_url=thumb,
            catalog_only=catalog_only,
            catalog_key=catalog_key,
            discord=discord_meta,
        )
        item_id = int(item["id"])

        jump = item.get("discord_jump_url") or ""
        existing_thread = bool(item.get("discord_thread_id")) and not created
        try:
            jump = await self._file_in_forum(
                item_id=item_id,
                typ=result.type,
                title=title,
                filename=filename,
                data=b"" if catalog_only else data,
                note=note,
                author=author,
                source_url=message.jump_url,
                uncertain=result.uncertain,
                catalog_only=catalog_only,
                current_message=message,
                reuse_existing=existing_thread,
            ) or jump
        except Exception:
            LOG.exception("Failed to organize Discord thread for %s", filename)

        try:
            await self.post_or_refresh_form(item_id)
        except Exception:
            LOG.exception("Failed to post questionnaire for item %s", item_id)

        line = format_ingest_reply(
            title,
            created=created,
            jump=jump,
            typ=result.type,
            uncertain=result.uncertain,
            reason=result.reason,
        )
        return line, item_id

    async def _file_in_forum(
        self,
        *,
        item_id: int,
        typ: str,
        title: str,
        filename: str,
        data: bytes,
        note: str,
        author: str,
        source_url: str,
        uncertain: bool,
        catalog_only: bool,
        current_message: discord.Message,
        reuse_existing: bool,
    ) -> str:
        guild = self.configured_guild()
        if guild is None:
            return ""
        if not self._forums:
            self._forums = self._find_existing_forums(guild)

        existing = self.store.get_enriched(item_id) or self.store.get(item_id) or {}
        if reuse_existing and existing.get("discord_thread_id"):
            return existing.get("discord_jump_url") or ""

        # If the user already posted inside a forum thread, use that thread.
        current_thread = _thread_of(current_message)
        current_forum = _forum_channel_of(current_message)
        if current_thread is not None and current_forum is not None and current_message.guild:
            jump = getattr(current_thread, "jump_url", "") or current_message.jump_url
            self.store.update_discord(
                item_id,
                discord_guild_id=str(current_message.guild.id),
                discord_channel_id=str(current_forum.id),
                discord_thread_id=str(current_thread.id),
                discord_message_id=str(current_message.id),
                discord_jump_url=jump,
            )
            try:
                item = self.store.get_enriched(item_id) or existing
                await current_thread.send(format_thread_body(item, source_url=source_url), view=open_form_view(item_id))
            except discord.HTTPException:
                LOG.warning("Could not post catalog card into the existing thread")
            await self._sync_forum_tags(current_thread, item if "item" in locals() else existing)
            return jump

        forum = self._forums.get(typ) or self._forums.get("other")
        if forum is None:
            LOG.warning("No forum channel available for type %s", typ)
            return ""

        item = self.store.get_enriched(item_id) or existing
        content = format_thread_body(item, source_url=source_url)
        kwargs: dict = {"name": thread_name(title), "content": content, "view": open_form_view(item_id)}
        pending = self._pending_tag(forum)
        if pending is not None and (item.get("review_status") or REVIEW_PENDING) == REVIEW_PENDING:
            kwargs["applied_tags"] = [pending]
        if data and not catalog_only:
            kwargs["file"] = discord.File(fp=io.BytesIO(data), filename=filename)

        try:
            created = await forum.create_thread(**kwargs)
        except discord.HTTPException as exc:
            LOG.warning("create_thread with extras failed (%s); retrying simply", exc)
            try:
                simple = {"name": thread_name(title), "content": content + "\n\n（附件或组件未能全部发出，文件仍已入库。）"}
                created = await forum.create_thread(**simple)
            except discord.HTTPException:
                LOG.exception("create_thread failed entirely")
                return ""

        thread, starter = _unpack_thread(created)
        jump = getattr(thread, "jump_url", "") or (starter.jump_url if starter else "")
        self.store.update_discord(
            item_id,
            discord_guild_id=str(guild.id),
            discord_channel_id=str(forum.id),
            discord_thread_id=str(thread.id) if thread else None,
            discord_message_id=str(starter.id) if starter else (str(thread.id) if thread else None),
            discord_jump_url=jump,
        )
        return jump

    async def post_or_refresh_form(self, item_id: int) -> None:
        item = self.store.get_enriched(item_id)
        if not item or not item.get("discord_thread_id"):
            return
        guild = self.configured_guild()
        if guild is None:
            return
        try:
            thread = guild.get_thread(int(item["discord_thread_id"]))
            if thread is None:
                thread = await guild.fetch_channel(int(item["discord_thread_id"]))  # type: ignore[assignment]
        except (discord.HTTPException, ValueError, TypeError):
            return
        if thread is None:
            return

        text = format_form_text(item, section="home")
        view = dashboard_view(item_id)
        form_id = item.get("discord_form_message_id")
        if form_id:
            try:
                msg = await thread.fetch_message(int(form_id))
                await msg.edit(content=text, view=view)
                await self._sync_forum_tags(thread, item)  # type: ignore[arg-type]
                await self._update_starter(thread, item)  # type: ignore[arg-type]
                return
            except (discord.HTTPException, ValueError, TypeError):
                pass
        try:
            msg = await thread.send(content=text, view=view)
            self.store.update_discord(item_id, discord_form_message_id=str(msg.id))
        except discord.HTTPException:
            LOG.warning("Could not post form message in thread")
        await self._sync_forum_tags(thread, item)  # type: ignore[arg-type]
        await self._update_starter(thread, item)  # type: ignore[arg-type]

    async def _update_starter(self, thread: discord.Thread, item: dict) -> None:
        starter_id = item.get("discord_message_id")
        if not starter_id:
            return
        try:
            msg = await thread.fetch_message(int(starter_id))
        except (discord.HTTPException, ValueError, TypeError):
            return
        if not msg or msg.author != self.user:
            return
        body = format_thread_body(item)
        if msg.content != body:
            try:
                await msg.edit(content=body)
            except discord.HTTPException:
                pass

    async def refile_item(self, item: dict) -> None:
        """Move a thread to the forum that matches the record type, when possible."""
        item_id = int(item["id"])
        typ = item.get("type") or "other"
        guild = self.configured_guild()
        if guild is None:
            return
        if not self._forums:
            self._forums = self._find_existing_forums(guild)
        target = self._forums.get(typ) or self._forums.get("other")
        if target is None:
            self.store.update_fields(item_id, needs_refile=0)
            return
        current_channel = str(item.get("discord_channel_id") or "")
        if current_channel == str(target.id):
            self.store.update_fields(item_id, needs_refile=0)
            try:
                await self.post_or_refresh_form(item_id)
            except Exception:
                LOG.exception("refresh after no-op refile failed")
            return
        old_thread_id = item.get("discord_thread_id")
        data = b""
        filename = item.get("original_filename") or "file"
        if item.get("stored_path") and not item.get("catalog_only"):
            try:
                path = self.store.abspath(item["stored_path"])
                if path.is_file():
                    data = path.read_bytes()
                    filename = path.name
            except (ValueError, OSError):
                data = b""
        enriched = self.store.get_enriched(item_id) or item
        content = format_thread_body(enriched)
        content += f"\n\n（已从原频道改到「{FORUM_CHANNEL_NAMES.get(typ, typ)}」。）"
        kwargs: dict = {"name": thread_name(enriched.get("title") or "未命名"), "content": content, "view": open_form_view(item_id)}
        pending = self._pending_tag(target)
        if pending is not None and (enriched.get("review_status") or REVIEW_PENDING) == REVIEW_PENDING:
            kwargs["applied_tags"] = [pending]
        if data:
            kwargs["file"] = discord.File(fp=io.BytesIO(data), filename=filename)
        try:
            created = await target.create_thread(**kwargs)
        except discord.HTTPException:
            LOG.exception("Failed to create replacement thread for item %s", item_id)
            return
        thread, starter = _unpack_thread(created)
        jump = getattr(thread, "jump_url", "") or (starter.jump_url if starter else "")
        self.store.update_discord(
            item_id,
            discord_guild_id=str(guild.id),
            discord_channel_id=str(target.id),
            discord_thread_id=str(thread.id) if thread else None,
            discord_message_id=str(starter.id) if starter else None,
            discord_jump_url=jump,
            discord_form_message_id=None,
        )
        self.store.update_fields(item_id, needs_refile=0)
        if old_thread_id:
            try:
                old = guild.get_thread(int(old_thread_id)) or await guild.fetch_channel(int(old_thread_id))
                if old:
                    await old.send(f"这条资源已改到「{FORUM_CHANNEL_NAMES.get(typ, typ)}」：{jump}")
                    if isinstance(old, discord.Thread):
                        await old.edit(archived=True, locked=True, reason="类型已更改，帖子已移动")
            except (discord.HTTPException, ValueError, TypeError):
                LOG.warning("Could not archive the old thread after a type change")
        try:
            await self.post_or_refresh_form(item_id)
        except Exception:
            LOG.exception("post form after refile failed")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type not in {InteractionType.component, InteractionType.modal_submit}:
            return
        custom_id = (interaction.data or {}).get("custom_id") or ""
        parsed = parse_cid(custom_id)
        if not parsed:
            return
        action, item_id, extra = parsed
        user = interaction.user
        guild = interaction.guild or self.configured_guild()
        if not await self.is_allowed(user, in_guild=guild):
            msg = "目前只接受资源库所在服务器成员整理这些资源。"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        item = self.store.get_enriched(item_id)
        if not item:
            msg = "找不到这条资源，可能已被移除。"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        try:
            await self._handle_form(interaction, item, action, extra)
        except Exception:
            LOG.exception("form interaction failed")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("操作失败，请稍后再试。", ephemeral=True)
                else:
                    await interaction.followup.send("操作失败，请稍后再试。", ephemeral=True)
            except discord.HTTPException:
                pass

    async def _handle_form(self, interaction: discord.Interaction, item: dict, action: str, extra: str) -> None:
        item_id = int(item["id"])
        store = self.store

        if action == "open":
            await edit_form(interaction, item, store, "home")
            return
        if action == "sec":
            section = extra or "home"
            await edit_form(interaction, item, store, section)
            return
        if action == "tagbucket":
            await edit_form(interaction, item, store, f"tag:{extra}")
            return
        if action == "modal":
            modal = build_modal(item, extra)
            await interaction.response.send_modal(modal)
            return
        if action == "confirm":
            item = set_review(store, item_id, True) or item
            await edit_form(interaction, item, store, "home")
            await self.post_or_refresh_form(item_id)
            return
        if action == "pending":
            item = set_review(store, item_id, False) or item
            await edit_form(interaction, item, store, "home")
            await self.post_or_refresh_form(item_id)
            return
        if action == "sel":
            values = select_values(interaction)
            if extra == "linkkind" and values:
                modal = build_modal(item, f"linkadd:{values[0]}")
                await interaction.response.send_modal(modal)
                return
            item = apply_select(store, item, extra, values) or item
            section = "home"
            if extra == "type":
                section = "type"
            elif extra == "theme":
                section = "theme"
            elif extra in {"license_repost", "license_tavern", "license_phone"}:
                section = "license"
            await edit_form(interaction, item, store, section)
            if extra == "type":
                # Refile in the background so the interaction can finish first.
                asyncio.create_task(self.flush_refiles())
            else:
                asyncio.create_task(self.post_or_refresh_form(item_id))
            return
        if action == "tagsel":
            item = apply_tag_select(store, item, extra, select_values(interaction)) or item
            await edit_form(interaction, item, store, f"tag:{extra}")
            asyncio.create_task(self.post_or_refresh_form(item_id))
            return
        if action == "msub":
            item = apply_modal(store, item, extra, modal_values(interaction)) or item
            section = "home"
            if extra in {"info"}:
                section = "info"
            elif extra in {"rec"}:
                section = "rec"
            elif extra in {"theme"}:
                section = "theme"
            elif extra.startswith("tag:"):
                section = extra
            elif extra == "basic":
                section = "tag:basic"
            elif extra == "licnote":
                section = "license"
            elif extra.startswith("link") or extra.startswith("linkadd") or extra == "linkdel":
                section = "links"
            # Modal submit cannot use edit_message unless we respond first.
            text = format_form_text(item, section=section)
            view = section_view(item, section, store)
            if interaction.message:
                await interaction.response.edit_message(content=text, view=view)
            else:
                await interaction.response.send_message(content=text, view=view, ephemeral=True)
            asyncio.create_task(self.post_or_refresh_form(item_id))
            return
        await edit_form(interaction, item, store, "home")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env_file(Path("/opt/tavern-bot/.env"))
    load_env_file(Path(__file__).resolve().parent / ".env")
    global TOKEN, GUILD_ID_RAW, ALLOWED_RAW
    TOKEN = (os.environ.get("DISCORD_TOKEN") or "").strip()
    GUILD_ID_RAW = (os.environ.get("DISCORD_GUILD_ID") or "").strip()
    ALLOWED_RAW = (os.environ.get("ALLOWED_USER_IDS") or "").strip()
    if not TOKEN:
        LOG.error("DISCORD_TOKEN is empty. Write it to /opt/tavern-bot/.env and start tavern-bot.service.")
        sys.exit(1)
    bot = TavernBot()
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
