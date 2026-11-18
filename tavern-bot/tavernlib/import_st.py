"""Copy a library item into the running SillyTavern data directory without overwriting."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .paths import ALLOWED_ST_DIR_NAMES, IMPORTABLE_TYPES, ST_DEST_DIRS, ST_USER_ROOT
from .pngutil import is_png, read_chara_json, wrap_card_as_png
from .store import LibraryStore, safe_filename

HELPER = Path(os.environ.get("TAVERN_IMPORT_HELPER", "/opt/tavern-bot/import_helper.py"))
HELPER_PYTHON = Path(os.environ.get("TAVERN_IMPORT_HELPER_PYTHON", "/opt/tavern-bot/venv/bin/python"))

CopyFn = Callable[[Path, Path], None]


class ImportError_(RuntimeError):
    """Raised when an item cannot be imported. Named to avoid clashing with builtins."""


def dest_dir_for(item: dict, st_root: Path | None = None) -> Path | None:
    typ = item.get("type")
    subtype = item.get("subtype") or None
    root = Path(st_root or ST_USER_ROOT)
    key = (typ, subtype)
    name = ST_DEST_DIRS.get(key)
    if name is None and typ == "preset":
        return None
    if name is None and typ in ST_DEST_DIRS:
        name = ST_DEST_DIRS.get((typ, None))
    if not name or name not in ALLOWED_ST_DIR_NAMES:
        return None
    return root / name


def _unique_name(directory: Path, filename: str) -> str:
    """Return a unique basename. On conflict use suffix _2, _3, ... never overwrite."""
    filename = Path(filename).name
    stem = safe_filename(Path(filename).stem) or "unnamed"
    suffix = Path(filename).suffix
    candidate = f"{stem}{suffix}"
    if not (directory / candidate).exists():
        return candidate
    for i in range(2, 10_000):
        candidate = f"{stem}_{i}{suffix}"
        if not (directory / candidate).exists():
            return candidate
    raise ImportError_("无法分配不冲突的文件名")


def _character_import_bytes(item: dict, src: Path) -> tuple[bytes, str]:
    """Return (file_bytes, dest_basename_hint) for a character card.

    SillyTavern's character list only reads `.png` files, so JSON / CharX cards
    are wrapped into a PNG with a chara chunk at import time. The original
    library file is left unchanged.
    """
    title = safe_filename(item.get("title") or src.stem) or "character"
    suffix = src.suffix.lower()
    data = src.read_bytes()
    if is_png(data):
        return data, f"{title}.png"
    # Already a png on disk (possibly without a recognised extension)
    if suffix == ".png":
        return data, f"{title}.png"

    card_json = None
    portrait = None
    if suffix in {".json"} or data.lstrip().startswith(b"{") or data.lstrip().startswith(b"\xef\xbb\xbf{"):
        text = data.decode("utf-8-sig")
        obj = json.loads(text)
        card_json = json.dumps(obj, ensure_ascii=False)
    else:
        # Prefer card_json already extracted at ingest time? Not stored. Re-classify lightly.
        from .classify import read_charx_card, _scan_embedded_card

        charx = read_charx_card(data)
        if charx:
            card_json, portrait = charx
        else:
            card_json = _scan_embedded_card(data)

    if not card_json:
        # Last resort: copy as-is with original extension (may not appear in the ST list).
        ext = suffix or ".json"
        return data, f"{title}{ext}"

    png = wrap_card_as_png(card_json, portrait if portrait and is_png(portrait) else None)
    return png, f"{title}.png"


def prepare_source(item: dict, src: Path) -> tuple[Path, str, Path | None]:
    """Prepare a file ready to copy into ST. Returns (path, dest_name_hint, temp_to_cleanup)."""
    typ = item.get("type")
    if typ == "character":
        payload, hint = _character_import_bytes(item, src)
        if src.suffix.lower() == ".png" and is_png(src.read_bytes()):
            return src, hint, None
        tmp = tempfile.NamedTemporaryFile(prefix="tavern-import-", suffix=".png", delete=False)
        tmp.write(payload)
        tmp.close()
        os.chmod(tmp.name, 0o644)
        return Path(tmp.name), hint, Path(tmp.name)

    title = safe_filename(item.get("title") or src.stem) or "item"
    suffix = src.suffix
    if typ == "world" and suffix.lower() != ".json":
        suffix = ".json"
    if typ == "preset" and suffix.lower() not in {".json", ".css"}:
        suffix = ".css" if (item.get("subtype") == "theme" and src.suffix.lower() == ".css") else (suffix or ".json")
    if typ == "beautify" and not suffix:
        suffix = ".css" if item.get("subtype") in {"theme", "css"} else (".json" if item.get("subtype") == "theme" else "")
    return src, f"{title}{suffix}", None


def copy_local(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    tmp.replace(dest)


def copy_via_helper(src: Path, dest: Path) -> None:
    """Ask the setuid-via-sudo helper to copy as user `tavern` (so ownership is tavern:tavern)."""
    import json as _json

    payload = {
        "src": str(src.resolve()),
        "dest_dir": str(dest.parent.resolve()),
        "dest_name": dest.name,
    }
    proc = subprocess.run(
        ["sudo", "-u", "tavern", "-n", str(HELPER_PYTHON), str(HELPER)],
        input=_json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        raise ImportError_(err or "导入助手执行失败")
    try:
        result = _json.loads(proc.stdout.decode("utf-8") or "{}")
    except ValueError as exc:
        raise ImportError_("导入助手返回了无法解析的结果") from exc
    if not result.get("ok"):
        raise ImportError_(result.get("message") or "导入失败")


def import_item(
    store: LibraryStore,
    item_id: int,
    *,
    st_root: Path | None = None,
    copy_fn: CopyFn | None = None,
    confirm_again: bool = False,
    name_override: str | None = None,
) -> dict:
    """Copy a library item into SillyTavern. Never overwrites existing files.

    If copy_fn is omitted, uses the privileged helper when ST_USER_ROOT is the
    real path, otherwise a local copy (for tests).
    """
    item = store.get(item_id)
    if not item:
        raise ImportError_("找不到这条资源")
    typ = item.get("type")
    if item.get("catalog_only") or typ == "imagegen":
        raise ImportError_("生图只在 Discord 留图，不导入酒馆，也不写入资源库文件。")
    if typ not in IMPORTABLE_TYPES or (typ == "preset" and not item.get("subtype")):
        raise ImportError_("这类资源不能一键导入酒馆（聊天记录不会导入，正则脚本也不会写入设置）")
    if typ == "beautify" and item.get("subtype") not in {"theme", "css", "background"}:
        raise ImportError_("这类美化文件不能一键导入酒馆。")
    if typ == "other":
        raise ImportError_("这类资源不能一键导入酒馆")

    dest_dir = dest_dir_for(item, st_root)
    if dest_dir is None:
        raise ImportError_("没有对应的酒馆目录，无法导入")
    if not dest_dir.is_dir():
        raise ImportError_(f"酒馆目录不存在：{dest_dir.name}")

    if not item.get("stored_path"):
        raise ImportError_("这条资源没有可导入的文件")
    src = store.abspath(item["stored_path"])
    if not src.is_file():
        raise ImportError_("库里的原文件已经找不到了")

    if item.get("imported_at") and not confirm_again:
        raise ImportError_("already_imported")

    prepared, hint, tmp = prepare_source(item, src)
    if name_override:
        override = safe_filename(name_override)
        if override:
            hint = f"{override}{Path(hint).suffix}"
    try:
        dest_name = _unique_name(dest_dir, hint)
        dest = dest_dir / dest_name
        fn = copy_fn
        if fn is None:
            root = Path(st_root or ST_USER_ROOT).resolve()
            # In tests ST_USER_ROOT is a temp dir; copy directly.
            if root != Path("/opt/sillytavern/data/default-user") or os.environ.get("TAVERN_IMPORT_LOCAL") == "1":
                fn = copy_local
            else:
                fn = copy_via_helper
        fn(prepared, dest)
        if not dest.is_file():
            raise ImportError_("文件没有成功写入酒馆目录")
        store.mark_imported(item_id, dest_name)
        return {
            "ok": True,
            "dest_name": dest_name,
            "dest_dir": dest_dir.name,
            "type": typ,
            "subtype": item.get("subtype"),
        }
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


# --- apply-a-set (character card + linked worldbooks / quick replies) ------

SET_LINK_KINDS = frozenset({"character_worldbook", "worldbook_extra", "bundle"})


def _set_member_allowed(item: dict) -> bool:
    """Whole-set import takes worldbooks and Quick Replies; presets and other cards stay out."""
    typ = item.get("type")
    if typ == "world":
        return True
    return typ == "preset" and (item.get("subtype") or "") == "quick-replies"


def _skipped_reason(item: dict) -> str:
    typ = item.get("type")
    if typ == "preset":
        return "预设不随整套导入，请在酒馆里自行选用"
    if typ == "character":
        return "另一张角色卡，请在它自己的详情页单独导入"
    if typ == "other":
        return "截图等辅助文件不导入酒馆"
    return "该类型不随整套导入"


def collect_set_members(store: LibraryStore, anchor: dict) -> list[dict]:
    """Anchor's companions via links (character_worldbook / worldbook_extra / bundle) and bundle id."""
    enriched = store.get_enriched(anchor["id"]) or anchor
    anchor_id = int(anchor["id"])
    seen: set[int] = set()
    members: list[dict] = []

    def _add(other_id: Any) -> None:
        try:
            oid = int(other_id)
        except (TypeError, ValueError):
            return
        if oid == anchor_id or oid in seen:
            return
        row = store.get(oid)
        if row:
            seen.add(oid)
            members.append(row)

    for link in enriched.get("links") or []:
        if link.get("kind") not in SET_LINK_KINDS:
            continue
        if link.get("from_id") == anchor_id:
            _add(link.get("to_id"))
        else:
            _add(link.get("from_id"))
    bid = enriched.get("bundle_id") or ""
    if bid:
        for row in store.list_bundle(bid):
            _add(row.get("id"))
    return members


def read_card_payload(src: Path) -> dict[str, Any] | None:
    """Read the card JSON out of a library file (PNG chara chunk / JSON / CharX)."""
    try:
        data = src.read_bytes()
    except OSError:
        return None
    try:
        if is_png(data):
            raw = read_chara_json(data) or ""
        elif src.suffix.lower() == ".json" or data.lstrip().startswith(b"{"):
            raw = data.decode("utf-8-sig")
        else:
            from .classify import _scan_embedded_card, read_charx_card

            charx = read_charx_card(data)
            raw = charx[0] if charx else (_scan_embedded_card(data) or "")
        payload = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def card_world_binding(payload: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Return (bound worldbook name, embedded character_book or None).

    SillyTavern keeps the picked worldbook in `data.extensions.world` and an
    exported inline worldbook in `data.character_book`; both travel inside the card.
    """
    if not isinstance(payload, dict):
        return "", None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    name = ""
    extensions = data.get("extensions")
    if isinstance(extensions, dict):
        value = extensions.get("world")
        if isinstance(value, str):
            name = value.strip()
    book = data.get("character_book")
    embedded = book if isinstance(book, dict) and book.get("entries") else None
    return name, embedded


def find_library_world(store: LibraryStore, name: str) -> dict[str, Any] | None:
    """Find the world item whose title or original filename matches the binding."""
    target = (name or "").strip()
    if not target:
        return None
    for row in store.list_items(type_filter="world"):
        if (row.get("title") or "").strip() == target:
            return row
        if Path(row.get("original_filename") or "").stem.strip() == target:
            return row
    return None


def import_set(
    store: LibraryStore,
    item_id: int,
    *,
    st_root: Path | None = None,
    copy_fn: CopyFn | None = None,
    confirm_again: bool = False,
) -> dict:
    """Copy a character plus its linked worldbooks / Quick Replies into SillyTavern.

    Presets are deliberately left out (they are global settings, not per-card companions).
    Already-imported items are reported as such; confirm_again re-copies with new names.
    Never overwrites: on name collision the unique-name suffix rule applies.
    """
    anchor = store.get(item_id)
    if not anchor:
        raise ImportError_("找不到这条资源")
    if anchor.get("type") != "character":
        raise ImportError_("整套应用只对角色卡开放；世界书、QR 等请单独导入。")
    members = collect_set_members(store, anchor)

    # The card itself records which worldbook it belongs to (extensions.world)
    # and may even embed one (character_book). Trust that over hand-made links.
    world_name, embedded_world = ("", None)
    card_payload: dict[str, Any] | None = None
    if anchor.get("stored_path"):
        try:
            card_path = store.abspath(anchor["stored_path"])
        except ValueError:
            card_path = None
        if card_path is not None and card_path.is_file():
            card_payload = read_card_payload(card_path)
            world_name, embedded_world = card_world_binding(card_payload)
    if not world_name and embedded_world:
        world_name = (embedded_world.get("name") or "").strip()
    bound_world = find_library_world(store, world_name) if world_name else None
    if bound_world is not None and all(int(m["id"]) != int(bound_world["id"]) for m in members):
        members.append(bound_world)
    # SillyTavern only links a worldbook whose filename matches `extensions.world`.
    name_overrides: dict[int, str] = {}
    if bound_world is not None and (bound_world.get("title") or "").strip() != world_name:
        name_overrides[int(bound_world["id"])] = world_name

    # Land the inline worldbook so SillyTavern can link it by filename, and
    # remember how many embedded (SCOPED) regex scripts the card carries.
    scoped_regex_count = count_scoped_regex(card_payload)
    embedded_world_result: dict[str, Any] | None = None
    if embedded_world and world_name:
        embedded_world_result = write_embedded_world(
            world_name, embedded_world, st_root=st_root, copy_fn=copy_fn
        )

    counts = {"imported": 0, "already": 0, "skipped": 0, "failed": 0}

    def _import_one(row: dict, *, is_anchor: bool) -> dict:
        entry = {
            "id": row["id"],
            "title": row.get("title") or "",
            "type": row.get("type"),
            "subtype": row.get("subtype"),
            "is_anchor": is_anchor,
        }
        if not is_anchor and not _set_member_allowed(row):
            entry["status"] = "skipped"
            entry["reason"] = _skipped_reason(row)
            counts["skipped"] += 1
            return entry
        try:
            res = import_item(
                store,
                row["id"],
                st_root=st_root,
                copy_fn=copy_fn,
                confirm_again=confirm_again,
                name_override=name_overrides.get(int(row["id"])),
            )
        except ImportError_ as exc:
            if str(exc) == "already_imported":
                entry["status"] = "already"
                entry["reason"] = "已导入过，本次跳过"
                counts["already"] += 1
            else:
                entry["status"] = "failed"
                entry["reason"] = str(exc)
                counts["failed"] += 1
            return entry
        entry["status"] = "imported"
        entry["dest_name"] = res["dest_name"]
        entry["dest_dir"] = res["dest_dir"]
        counts["imported"] += 1
        return entry

    anchor_result = _import_one(anchor, is_anchor=True)
    if anchor_result["status"] == "failed":
        raise ImportError_(anchor_result["reason"])
    member_results = [_import_one(m, is_anchor=False) for m in members]
    if embedded_world_result is not None:
        status = embedded_world_result.get("status")
        row = {
            "id": None,
            "title": f"卡内嵌世界书「{world_name}」",
            "type": "world",
            "subtype": None,
            "is_anchor": False,
        }
        if status == "written":
            row["status"] = "imported"
            row["dest_name"] = embedded_world_result.get("dest_name", "")
            row["dest_dir"] = "worlds"
        elif status == "exists":
            row["status"] = "skipped"
            row["reason"] = "酒馆里已有同名世界书，直接沿用"
        else:
            row["status"] = "failed"
            row["reason"] = embedded_world_result.get("reason") or "世界书没有写入"
        member_results.append(row)
    elif world_name and bound_world is None:
        # Only a real gap is reported: the card points at a worldbook that is
        # neither embedded nor present in the library.
        member_results.append(
            {
                "id": None,
                "title": f"卡内绑定的世界书「{world_name}」",
                "type": "world",
                "subtype": None,
                "is_anchor": False,
                "status": "skipped",
                "reason": "资源库里没有这本，导入后需在酒馆里手动指定",
            }
        )

    scoped_regex_result: dict[str, Any] | None = None
    # Even when the card was imported before, its avatar name is still known.
    avatar_name = anchor_result.get("dest_name") or (anchor.get("imported_as_name") or "")
    if scoped_regex_count and avatar_name:
        scoped_regex_result = allow_scoped_regex(str(avatar_name), st_root=st_root)

    return {
        "ok": True,
        "anchor": anchor_result,
        "members": member_results,
        "summary": counts,
        "card_world": {
            "name": world_name,
            "embedded": embedded_world is not None,
            "matched": bound_world is not None,
            "matched_id": int(bound_world["id"]) if bound_world is not None else None,
        },
        "embedded_world": embedded_world_result,
        "scoped_regex": {
            "count": scoped_regex_count,
            **(scoped_regex_result or {"status": "none"}),
        },
    }


# --- embedded character_book -> worlds/<name>.json -------------------------

WORLD_ENTRY_DEFAULTS: dict[str, Any] = {
    "uid": 0,
    "key": [],
    "keysecondary": [],
    "comment": "",
    "content": "",
    "constant": False,
    "selective": True,
    "order": 100,
    "position": 0,
    "disable": False,
    "displayIndex": 0,
    "addMemo": False,
    "group": "",
    "groupOverride": False,
    "groupWeight": 100,
    "sticky": 0,
    "cooldown": 0,
    "delay": 0,
    "probability": 100,
    "depth": 4,
    "useProbability": True,
    "role": None,
    "vectorized": False,
    "excludeRecursion": False,
    "preventRecursion": False,
    "delayUntilRecursion": False,
    "scanDepth": None,
    "caseSensitive": None,
    "matchWholeWords": None,
    "useGroupScoring": None,
    "automationId": "",
}


def convert_character_book(book: dict) -> dict:
    """Convert a v2 character_book into a SillyTavern world file.

    Mirrors SillyTavern's own `convertCharacterBook()` so the generated file is
    compatible with what the World Info editor would save.
    """
    entries = [e for e in (book.get("entries") or []) if isinstance(e, dict)]
    out: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        uid = entry.get("id")
        if uid is None:
            uid = index
        ext = entry.get("extensions") if isinstance(entry.get("extensions"), dict) else {}
        item = dict(WORLD_ENTRY_DEFAULTS)
        item.update(
            {
                "uid": uid,
                "key": entry.get("keys") or [],
                "keysecondary": entry.get("secondary_keys") or [],
                "comment": entry.get("comment") or entry.get("name") or "",
                "content": entry.get("content") or "",
                "constant": bool(entry.get("constant") or False),
                "selective": bool(entry.get("selective") or False),
                "order": entry.get("insertion_order") if entry.get("insertion_order") is not None else 100,
                "position": ext.get("position", 0 if entry.get("position") == "before_char" else 1),
                "disable": not bool(entry.get("enabled", True)),
                "addMemo": bool(entry.get("comment") or entry.get("name")),
                "displayIndex": ext.get("display_index", index),
                "probability": ext.get("probability", 100),
                "useProbability": ext.get("useProbability", True),
                "depth": ext.get("depth", 4),
                "group": ext.get("group", ""),
                "groupOverride": ext.get("group_override", False),
                "groupWeight": ext.get("group_weight", 100),
                "scanDepth": ext.get("scan_depth"),
                "caseSensitive": ext.get("case_sensitive"),
                "matchWholeWords": ext.get("match_whole_words"),
                "useGroupScoring": ext.get("use_group_scoring"),
                "automationId": ext.get("automation_id", ""),
                "role": ext.get("role"),
                "vectorized": ext.get("vectorized", False),
                "excludeRecursion": ext.get("exclude_recursion", False),
                "preventRecursion": ext.get("prevent_recursion", False),
                "delayUntilRecursion": ext.get("delay_until_recursion", False),
            }
        )
        out[str(uid)] = item
    return {"entries": out, "originalData": book}


def _default_copy_fn(st_root: Path | None) -> CopyFn:
    root = Path(st_root or ST_USER_ROOT).resolve()
    if root != Path("/opt/sillytavern/data/default-user") or os.environ.get("TAVERN_IMPORT_LOCAL") == "1":
        return copy_local
    return copy_via_helper


def write_embedded_world(
    name: str,
    book: dict,
    *,
    st_root: Path | None = None,
    copy_fn: CopyFn | None = None,
) -> dict:
    """Land a card's inline worldbook as worlds/<name>.json (never overwrites)."""
    world_dir = dest_dir_for({"type": "world", "subtype": None}, st_root)
    if world_dir is None or not world_dir.is_dir():
        return {"status": "failed", "reason": "酒馆 worlds 目录不存在"}
    dest_name = f"{safe_filename(name) or 'embedded-world'}.json"
    dest = world_dir / dest_name
    if dest.exists():
        return {"status": "exists", "dest_name": dest_name, "entries": len(book.get("entries") or [])}
    payload = json.dumps(convert_character_book(book), ensure_ascii=False, indent=2).encode("utf-8")
    tmp = tempfile.NamedTemporaryFile(prefix="tavern-world-", suffix=".json", delete=False)
    try:
        tmp.write(payload)
        tmp.close()
        os.chmod(tmp.name, 0o644)
        (copy_fn or _default_copy_fn(st_root))(Path(tmp.name), dest)
        if not dest.is_file():
            return {"status": "failed", "reason": "世界书没有写入成功"}
        return {"status": "written", "dest_name": dest_name, "entries": len(book.get("entries") or [])}
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def allow_scoped_regex(avatar: str, *, st_root: Path | None = None) -> dict:
    """Whitelist a character so its embedded regex_scripts may run.

    SillyTavern keeps those scripts inside the card (SCOPED) and only enables them
    for avatars listed in `extension_settings.character_allowed_regex`. We append
    the avatar there instead of copying scripts into the global list.
    """
    import time

    root = Path(st_root or ST_USER_ROOT)
    settings = root / "settings.json"
    if not settings.is_file():
        return {"status": "failed", "reason": "找不到酒馆 settings.json"}
    stat = settings.stat()
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "reason": f"settings.json 读取失败：{exc}"}
    extensions = data.setdefault("extension_settings", {})
    allowed = extensions.get("character_allowed_regex")
    if not isinstance(allowed, list):
        allowed = []
        extensions["character_allowed_regex"] = allowed
    if avatar in allowed:
        return {"status": "already"}
    backup = settings.with_name(f"{settings.name}.bak-tavernlib-{int(time.time())}")
    try:
        backup.write_bytes(settings.read_bytes())
        os.chmod(backup, stat.st_mode & 0o777)
        os.chown(backup, stat.st_uid, stat.st_gid)
    except OSError:
        pass
    allowed.append(avatar)
    tmp = settings.with_name(f"{settings.name}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, stat.st_mode & 0o777)
        try:
            os.chown(tmp, stat.st_uid, stat.st_gid)
        except OSError:
            pass
        tmp.replace(settings)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return {"status": "failed", "reason": f"settings.json 写入失败：{exc}"}
    return {"status": "added"}


def count_scoped_regex(payload: dict[str, Any] | None) -> int:
    """How many embedded regex scripts the card carries (data.extensions.regex_scripts)."""
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        return 0
    scripts = extensions.get("regex_scripts")
    return len(scripts) if isinstance(scripts, list) else 0
