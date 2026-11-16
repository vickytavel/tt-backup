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
