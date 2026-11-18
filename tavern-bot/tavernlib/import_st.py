"""Copy a library item into the running SillyTavern data directory without overwriting."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .paths import ALLOWED_ST_DIR_NAMES, IMPORTABLE_TYPES, ST_DEST_DIRS, ST_USER_ROOT
from .pngutil import is_png, wrap_card_as_png
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
            res = import_item(store, row["id"], st_root=st_root, copy_fn=copy_fn, confirm_again=confirm_again)
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
    return {
        "ok": True,
        "anchor": anchor_result,
        "members": member_results,
        "summary": counts,
    }
