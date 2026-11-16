#!/usr/bin/env python3
"""Copy one library file into a SillyTavern user-data subdirectory as user `tavern`.

Reads a single JSON object from stdin:
    {"src": "/opt/tavern-library/...", "dest_dir": "/opt/sillytavern/data/default-user/...", "dest_name": "Name.png"}

Validates paths strictly, never overwrites, writes mode 644. Because this runs
as `tavern` via sudo, the resulting file is owned by tavern:tavern.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ALLOWED_LIBRARY = Path("/opt/tavern-library").resolve()
ALLOWED_ST_ROOT = Path("/opt/sillytavern/data/default-user").resolve()
ALLOWED_ST_DIRS = {
    "characters",
    "worlds",
    "OpenAI Settings",
    "instruct",
    "context",
    "sysprompt",
    "reasoning",
    "KoboldAI Settings",
    "NovelAI Settings",
    "TextGen Settings",
    "QuickReplies",
    "themes",
    "User Avatars",
    "backgrounds",
}

# Hard cap so a confused caller cannot copy huge garbage into ST.
MAX_BYTES = 80 * 1024 * 1024


def fail(message: str, code: int = 1) -> None:
    sys.stdout.write(json.dumps({"ok": False, "message": message}, ensure_ascii=False) + "\n")
    sys.exit(code)


def resolve_under(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    if resolved != parent and parent not in resolved.parents:
        raise ValueError("path escapes allowed root")
    return resolved


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        fail("无效的请求")

    src_raw = payload.get("src")
    dest_dir_raw = payload.get("dest_dir")
    dest_name = payload.get("dest_name")
    if not isinstance(src_raw, str) or not isinstance(dest_dir_raw, str) or not isinstance(dest_name, str):
        fail("缺少必要字段")

    # dest_name must be a single basename with no separators.
    if not dest_name or dest_name != Path(dest_name).name or "/" in dest_name or "\\" in dest_name or dest_name in {".", ".."}:
        fail("目标文件名不合法")
    if "\x00" in dest_name:
        fail("目标文件名不合法")

    try:
        src = resolve_under(Path(src_raw), ALLOWED_LIBRARY)
        dest_dir = resolve_under(Path(dest_dir_raw), ALLOWED_ST_ROOT)
    except ValueError:
        fail("路径不在允许范围内")

    if dest_dir.name not in ALLOWED_ST_DIRS or dest_dir.parent.resolve() != ALLOWED_ST_ROOT:
        fail("目标目录不在允许范围内")
    if not src.is_file() or src.is_symlink():
        # After resolve(), a symlink to a file inside the library is a regular file
        # at its target; reject if the original path was a symlink escaping, already
        # handled by resolve_under. Still reject if it's not a file.
        if not src.is_file():
            fail("源文件不存在")
    if src.name.endswith(".part") or src.suffix == ".db" or src.name == "library.db":
        fail("不能导入该文件")
    if not dest_dir.is_dir():
        fail("目标目录不存在")

    dest = dest_dir / dest_name
    try:
        dest_resolved_parent = dest.parent.resolve()
        if dest_resolved_parent != dest_dir:
            fail("目标路径不合法")
    except OSError:
        fail("目标路径不合法")
    if dest.exists():
        fail("目标文件已存在，拒绝覆盖")

    size = src.stat().st_size
    if size > MAX_BYTES:
        fail("文件过大，拒绝导入")

    tmp = dest.with_name(dest.name + ".part")
    try:
        data = src.read_bytes()
        if len(data) != size:
            fail("读取源文件失败")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        os.chmod(dest, 0o644)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        fail(f"写入失败：{exc}")

    sys.stdout.write(json.dumps({"ok": True, "dest_name": dest_name}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
