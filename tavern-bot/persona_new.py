#!/usr/bin/env python3
"""Register a persona draft (markdown) into SillyTavern.

Reads a draft written by the workspace assistant, creates (or copies) an avatar
under `User Avatars/`, and registers it in settings.json's
`power_user.personas` / `power_user.persona_descriptions`.

Dry-run by default; pass --apply to write. Always backs up settings.json first
and prints the rollback command. Run as root (it re-chowns files to tavern).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ST_USER = Path(os.environ.get("ST_USER_ROOT", "/opt/sillytavern/data/default-user"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tavernlib.pngutil import make_solid_png  # noqa: E402


def parse_draft(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    title = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            for sep in ("：", ":"):
                if sep in title:
                    head, _, tail = title.partition(sep)
                    if "面具" in head or "persona" in head.lower():
                        title = tail.strip()
                    break
            break
    if not title:
        title = path.stem
    return title, text


def safe_name(name: str) -> str:
    out = "".join(
        ch if (ch.isalnum() or ch in "-_") or ord(ch) > 127 else "_" for ch in (name or "")
    ).strip("_")
    return out[:40] or "persona"


def chown_tavern(path: Path) -> None:
    try:
        shutil.chown(path, user="tavern", group="tavern")
    except (LookupError, PermissionError, OSError):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("draft", help="markdown draft path")
    ap.add_argument("--name", default="", help="override persona name")
    ap.add_argument("--avatar", default="", help="existing image to use as avatar")
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    args = ap.parse_args()

    draft = Path(args.draft)
    if not draft.is_file():
        print(f"draft not found: {draft}")
        return 2
    if draft.name.endswith("-design.md"):
        print("refusing to register a -design.md file: design notes must stay offline.")
        print("import only the clean persona draft (file without the -design suffix).")
        return 2
    title, text = parse_draft(draft)
    title = args.name.strip() or title

    settings_path = ST_USER / "settings.json"
    if not settings_path.is_file():
        print(f"settings.json not found: {settings_path}")
        return 2
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    power = data.setdefault("power_user", {})
    personas = power.setdefault("personas", {})
    descs = power.setdefault("persona_descriptions", {})

    stamp = str(int(time.time() * 1000))
    avatar_name = f"{stamp}-{safe_name(title)}.png"
    template = next(iter(descs.values()), {}) if isinstance(descs, dict) and descs else {}
    desc = {
        "description": text,
        "title": title,
        "position": template.get("position", 0),
        "role": template.get("role", 0),
        "depth": template.get("depth", 4),
        "lorebook": template.get("lorebook", ""),
    }

    print(f"persona name : {title}")
    print(f"avatar file  : User Avatars/{avatar_name}")
    print(f"description  : {len(text)} chars from {draft}")
    print(f"template     : position={desc['position']} role={desc['role']} depth={desc['depth']}")
    print(f"avatar source: {args.avatar or 'generated placeholder (solid png)'}")

    if not args.apply:
        print("\n[dry-run] nothing written. Re-run with --apply to register.")
        return 0

    backup = settings_path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(settings_path, backup)

    avatars = ST_USER / "User Avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    dest = avatars / avatar_name
    if args.avatar:
        src = Path(args.avatar)
        if not src.is_file():
            print(f"avatar not found: {src}")
            return 2
        shutil.copyfile(src, dest)
    else:
        dest.write_bytes(make_solid_png())

    personas[avatar_name] = title
    descs[avatar_name] = desc

    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)

    chown_tavern(settings_path)
    chown_tavern(dest)
    os.chmod(settings_path, 0o644)
    os.chmod(dest, 0o644)

    print(f"\nregistered persona: {avatar_name}")
    print(f"backup: {backup}")
    print("rollback:")
    print(f"  cp -a '{backup}' '{settings_path}' && rm -f '{dest}'")
    print("\nnow refresh the SillyTavern page; if it does not show up, run:")
    print("  systemctl restart sillytavern.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())