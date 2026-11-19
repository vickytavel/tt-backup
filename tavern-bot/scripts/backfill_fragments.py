"""Re-extract searchable fragments for every stored item.

Run after any change to fragment extraction (for example PNG/CharX container
support). Safe to re-run: fragments for an item are fully replaced.

    ./venv/bin/python scripts/backfill_fragments.py --dry-run
    ./venv/bin/python scripts/backfill_fragments.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tavernlib.fragments import extract_fragments  # noqa: E402
from tavernlib.paths import LIBRARY_ROOT  # noqa: E402
from tavernlib.store import LibraryStore  # noqa: E402

TYPES = ("character", "world", "preset", "style")


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the fragments index")
    ap.add_argument("--root", default=str(LIBRARY_ROOT), help="library root (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    store = LibraryStore(Path(args.root))
    scanned = updated = 0
    for typ in TYPES:
        for item in store.list_items(type_filter=typ, limit=1000):
            stored = item.get("stored_path") or ""
            if not stored:
                continue
            path = Path(store.abspath(stored))
            scanned += 1
            try:
                data = path.read_bytes()
            except OSError as exc:
                print(f"skip  {item['id']} {typ} {item['title']}: {exc}")
                continue
            frags = extract_fragments(data, typ)
            before = len(store.get_enriched(item["id"])["fragments"])
            if not frags and not before:
                print(f"empty {item['id']} {typ} {item['title']}: no fragments found")
                continue
            if args.dry_run:
                print(f"[dry] {item['id']} {typ} {item['title']}: {before} -> {len(frags)}")
                continue
            store.replace_fragments(item["id"], frags)
            updated += 1
            print(f"      {item['id']} {typ} {item['title']}: {before} -> {len(frags)}")
    print(f"\nscanned {scanned}, updated {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
