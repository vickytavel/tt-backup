"""SQLite-backed library store. Files live on disk; metadata lives in library.db."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .fragments import extract_fragments
from .paths import CATALOG_ONLY_TYPES, IMPORTABLE_TYPES, LIBRARY_ROOT, TYPE_FOLDERS
from .taxonomy import (
    BUCKET_IDS,
    LICENSE_FIELDS,
    LINK_IDS,
    MAX_TEXT,
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    SCHEMA_VERSION,
    SEED_TAGS,
    TAG_BUCKETS,
    THEME_SEEDS,
    dump_list_field,
    load_list_field,
    normalize_tag_name,
    normalize_theme_path,
    parse_list_field,
    valid_bucket,
    valid_license,
    valid_link_kind,
    valid_review,
)

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    subtype TEXT,
    title TEXT NOT NULL,
    original_filename TEXT,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    note TEXT,
    discord_guild_id TEXT,
    discord_channel_id TEXT,
    discord_thread_id TEXT,
    discord_message_id TEXT,
    discord_jump_url TEXT,
    author TEXT,
    created_at TEXT NOT NULL,
    imported_at TEXT,
    imported_as_name TEXT,
    uncertain INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT,
    file_size INTEGER,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC);
"""

ITEM_NEW_COLUMNS: list[tuple[str, str]] = [
    ("source_url", "TEXT"),
    ("summary", "TEXT"),
    ("recommended_presets", "TEXT"),
    ("recommended_models", "TEXT"),
    ("recommended_artist_strings", "TEXT"),
    ("license_repost", "TEXT DEFAULT 'unset'"),
    ("license_tavern", "TEXT DEFAULT 'unset'"),
    ("license_phone", "TEXT DEFAULT 'unset'"),
    ("license_note", "TEXT"),
    ("review_status", "TEXT DEFAULT 'pending'"),
    ("worldbook_origin", "TEXT"),
    ("version_group", "TEXT"),
    ("version_label", "TEXT"),
    ("bundle_id", "TEXT"),
    ("catalog_only", "INTEGER NOT NULL DEFAULT 0"),
    ("thumbnail_url", "TEXT"),
    ("updated_at", "TEXT"),
    ("ai_prefill", "TEXT"),
    ("needs_refile", "INTEGER NOT NULL DEFAULT 0"),
    ("discord_form_message_id", "TEXT"),
]

NEW_TABLES = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    name TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    blacklisted INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    UNIQUE(bucket, name)
);
CREATE INDEX IF NOT EXISTS idx_tags_bucket ON tags(bucket, name);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (item_id, tag_id)
);
CREATE TABLE IF NOT EXISTS theme_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    UNIQUE(type, path)
);
CREATE TABLE IF NOT EXISTS item_themes (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    PRIMARY KEY (item_id, path)
);
CREATE INDEX IF NOT EXISTS idx_item_themes_path ON item_themes(path);
CREATE TABLE IF NOT EXISTS item_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    to_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    note TEXT,
    UNIQUE(from_id, to_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_item_links_to ON item_links(to_id);
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT,
    excerpt TEXT,
    source_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_fragments_item ON fragments(item_id);
CREATE INDEX IF NOT EXISTS idx_items_review ON items(review_status);
CREATE INDEX IF NOT EXISTS idx_items_bundle ON items(bundle_id);
"""

_WRITE_LOCK = threading.Lock()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalog_digest(*, typ: str, discord_key: str = "", thumbnail_url: str = "", title: str = "") -> str:
    raw = f"catalog:{typ}:{discord_key}:{thumbnail_url}:{title}".encode("utf-8")
    return "catalog:" + sha256_bytes(raw)


def safe_filename(name: str, default: str = "unnamed") -> str:
    raw = (name or "").strip().replace("\x00", "")
    cleaned = []
    for ch in raw:
        o = ord(ch)
        if ch in '\\/:*?"<>|\n\r\t' or o < 32 or o == 0x7F:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    out = "".join(cleaned).strip(" .")
    if not out or out in {".", ".."}:
        return default
    return out[:150]


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base = Path(filename)
    stem, suffix = safe_filename(base.stem) or "unnamed", base.suffix
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for i in range(2, 10_000):
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("could not allocate a unique filename")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class LibraryStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or LIBRARY_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in TYPE_FOLDERS.values():
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.db"
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(LEGACY_SCHEMA)
            self._migrate(conn)
            self._seed(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        for name, decl in ITEM_NEW_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")
        conn.executescript(NEW_TABLES)
        current = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        version = int(current["value"]) if current and str(current["value"]).isdigit() else 1
        if version < SCHEMA_VERSION:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _seed(self, conn: sqlite3.Connection) -> None:
        for typ, paths in THEME_SEEDS.items():
            for path in paths:
                conn.execute(
                    "INSERT OR IGNORE INTO theme_categories(type, path) VALUES(?, ?)",
                    (typ, path),
                )
        for bucket, names in SEED_TAGS.items():
            for name in names:
                conn.execute(
                    "INSERT OR IGNORE INTO tags(bucket, name, locked, blacklisted, pending) VALUES(?, ?, 0, 0, 0)",
                    (bucket, name),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def get_by_sha(self, sha256: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE sha256 = ?", (sha256,)).fetchone()
        return _row_to_dict(row)

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row)

    def enrich(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        item_id = int(item["id"])
        with self._connect() as conn:
            tags = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT t.id, t.bucket, t.name, t.locked, t.blacklisted, t.pending, it.source
                    FROM item_tags it JOIN tags t ON t.id = it.tag_id
                    WHERE it.item_id = ?
                    ORDER BY t.bucket, t.name
                    """,
                    (item_id,),
                )
            ]
            themes = [
                r["path"]
                for r in conn.execute(
                    "SELECT path FROM item_themes WHERE item_id = ? ORDER BY path",
                    (item_id,),
                )
            ]
            fragments = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, kind, title, excerpt, source_ref FROM fragments WHERE item_id = ? ORDER BY id",
                    (item_id,),
                )
            ]
            links = [self._link_row(conn, r) for r in conn.execute(
                """
                SELECT * FROM item_links
                WHERE from_id = ? OR to_id = ?
                ORDER BY kind, id
                """,
                (item_id, item_id),
            )]
        out = dict(item)
        out["recommended_presets"] = load_list_field(item.get("recommended_presets"))
        out["recommended_models"] = load_list_field(item.get("recommended_models"))
        out["recommended_artist_strings"] = load_list_field(item.get("recommended_artist_strings"))
        out["license_repost"] = valid_license(item.get("license_repost"))
        out["license_tavern"] = valid_license(item.get("license_tavern"))
        out["license_phone"] = valid_license(item.get("license_phone"))
        out["review_status"] = valid_review(item.get("review_status"))
        out["catalog_only"] = bool(item.get("catalog_only"))
        out["uncertain"] = bool(item.get("uncertain"))
        out["needs_refile"] = bool(item.get("needs_refile"))
        out["tags"] = tags
        out["themes"] = themes
        out["fragments"] = fragments
        out["links"] = links
        out["importable"] = self.is_importable(out)
        try:
            out["ai_prefill"] = json.loads(item["ai_prefill"]) if item.get("ai_prefill") else None
        except (ValueError, TypeError):
            out["ai_prefill"] = None
        return out

    def get_enriched(self, item_id: int) -> dict[str, Any] | None:
        return self.enrich(self.get(item_id))

    def _link_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        other_id = data["to_id"]
        # When listing from either end, expose the other item briefly.
        for key in ("from_id", "to_id"):
            peer = conn.execute(
                "SELECT id, type, title, review_status FROM items WHERE id = ?",
                (data[key],),
            ).fetchone()
            data[f"{key}_item"] = dict(peer) if peer else None
        data["other_id"] = other_id
        return data

    @staticmethod
    def is_importable(item: dict[str, Any]) -> bool:
        typ = item.get("type")
        if item.get("catalog_only") or typ in CATALOG_ONLY_TYPES:
            return False
        if typ not in IMPORTABLE_TYPES:
            return False
        if typ == "preset" and not item.get("subtype"):
            return False
        if typ == "beautify" and item.get("subtype") not in {"theme", "css", "background"}:
            return False
        stored = item.get("stored_path") or ""
        return bool(stored)

    def list_items(
        self,
        type_filter: str | None = None,
        query: str | None = None,
        limit: int = 500,
        *,
        theme: str | None = None,
        tag: str | None = None,
        bucket: str | None = None,
        review_status: str | None = None,
        license_field: str | None = None,
        license_value: str | None = None,
        enrich: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT DISTINCT items.* FROM items"
        joins: list[str] = []
        params: list[Any] = []
        clauses: list[str] = []
        if type_filter and type_filter != "all" and type_filter in TYPE_FOLDERS:
            clauses.append("items.type = ?")
            params.append(type_filter)
        if theme:
            joins.append("JOIN item_themes th ON th.item_id = items.id")
            clauses.append("(th.path = ? OR th.path LIKE ?)")
            path = normalize_theme_path(theme)
            params.extend([path, path + "/%"])
        if tag or (bucket and valid_bucket(bucket)):
            joins.append("JOIN item_tags it ON it.item_id = items.id JOIN tags tg ON tg.id = it.tag_id")
            if bucket and valid_bucket(bucket):
                clauses.append("tg.bucket = ?")
                params.append(bucket)
            if tag:
                clauses.append("tg.name LIKE ?")
                params.append(f"%{normalize_tag_name(tag)}%")
        if review_status in {REVIEW_PENDING, REVIEW_CONFIRMED}:
            clauses.append("IFNULL(items.review_status, 'pending') = ?")
            params.append(review_status)
        if license_field in {f for f, _ in LICENSE_FIELDS} and license_value:
            clauses.append(f"IFNULL(items.{license_field}, 'unset') = ?")
            params.append(valid_license(license_value))
        if query:
            like = f"%{query.strip()}%"
            joins.append("LEFT JOIN item_tags itq ON itq.item_id = items.id LEFT JOIN tags tgq ON tgq.id = itq.tag_id")
            joins.append("LEFT JOIN item_themes thq ON thq.item_id = items.id")
            joins.append("LEFT JOIN fragments fq ON fq.item_id = items.id")
            clauses.append(
                "("
                "items.title LIKE ? OR items.original_filename LIKE ? OR IFNULL(items.note, '') LIKE ? "
                "OR IFNULL(items.author, '') LIKE ? OR IFNULL(items.summary, '') LIKE ? "
                "OR IFNULL(items.source_url, '') LIKE ? OR IFNULL(tgq.name, '') LIKE ? "
                "OR IFNULL(thq.path, '') LIKE ? OR IFNULL(fq.excerpt, '') LIKE ? OR IFNULL(fq.title, '') LIKE ?"
                ")"
            )
            params.extend([like] * 10)
        if joins:
            sql += " " + " ".join(joins)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(items.updated_at, items.created_at) DESC, items.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [dict(r) for r in rows]
        if enrich:
            return [self.enrich(it) or it for it in items]
        return items

    def list_pending_refile(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE needs_refile = 1 AND IFNULL(discord_thread_id, '') != ''"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_file(
        self,
        data: bytes,
        *,
        type: str,
        subtype: str | None,
        title: str,
        original_filename: str,
        note: str = "",
        author: str = "",
        uncertain: bool = False,
        reason: str = "",
        mime_type: str = "",
        ext: str | None = None,
        discord: dict[str, str] | None = None,
        source_url: str = "",
        summary: str = "",
        thumbnail_url: str = "",
        catalog_only: bool = False,
        catalog_key: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Persist a file or catalog record. Returns (item, created).

        If sha256 already exists the existing row is updated (not duplicated)
        and created is False.
        """
        typ = type if type in TYPE_FOLDERS else "other"
        catalog_only = bool(catalog_only or typ in CATALOG_ONLY_TYPES)
        if catalog_only:
            digest = catalog_key or catalog_digest(
                typ=typ,
                discord_key=(discord or {}).get("message_id", ""),
                thumbnail_url=thumbnail_url,
                title=title,
            )
            if not digest.startswith("catalog:"):
                digest = "catalog:" + digest
        else:
            digest = sha256_bytes(data)

        existing = self.get_by_sha(digest)
        if existing:
            self._touch_existing(
                existing,
                note=note,
                source_url=source_url,
                summary=summary,
                thumbnail_url=thumbnail_url,
                discord=discord,
            )
            return self.get(int(existing["id"])) or existing, False

        rel = ""
        size = 0
        if not catalog_only:
            folder = TYPE_FOLDERS.get(typ, "other")
            dest_dir = self.root / folder
            if subtype and typ == "preset":
                dest_dir = dest_dir / safe_filename(subtype, "misc")
            elif subtype and typ == "beautify":
                dest_dir = dest_dir / safe_filename(subtype, "misc")
            suffix = ext if ext is not None else Path(original_filename or "").suffix
            if typ == "world" and not suffix:
                suffix = ".json"
            base = f"{safe_filename(title)}_{digest[:10]}{suffix}"
            path = unique_path(dest_dir, base)
            tmp = path.with_name(path.name + ".part")
            try:
                tmp.write_bytes(data)
                os.chmod(tmp, 0o664)
                tmp.replace(path)
            except Exception:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise
            rel = str(path.relative_to(self.root))
            size = len(data)

        created_at = utcnow_iso()
        discord = discord or {}
        with _WRITE_LOCK:
            existing = self.get_by_sha(digest)
            if existing:
                if rel:
                    stored = Path(self.abspath(existing["stored_path"])) if existing.get("stored_path") else None
                    new_path = self.root / rel
                    if stored is None or stored != new_path:
                        new_path.unlink(missing_ok=True)
                self._touch_existing(
                    existing,
                    note=note,
                    source_url=source_url,
                    summary=summary,
                    thumbnail_url=thumbnail_url,
                    discord=discord,
                )
                return self.get(int(existing["id"])) or existing, False
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO items (
                        type, subtype, title, original_filename, stored_path, sha256, note,
                        discord_guild_id, discord_channel_id, discord_thread_id, discord_message_id,
                        discord_jump_url, author, created_at, uncertain, mime_type, file_size, reason,
                        source_url, summary, thumbnail_url, catalog_only, review_status, updated_at,
                        license_repost, license_tavern, license_phone
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unset', 'unset', 'unset')
                    """,
                    (
                        typ,
                        subtype,
                        title or safe_filename(original_filename, "unnamed"),
                        original_filename,
                        rel,
                        digest,
                        note,
                        discord.get("guild_id"),
                        discord.get("channel_id"),
                        discord.get("thread_id"),
                        discord.get("message_id"),
                        discord.get("jump_url"),
                        author,
                        created_at,
                        1 if uncertain else 0,
                        mime_type,
                        size,
                        reason,
                        source_url,
                        summary,
                        thumbnail_url,
                        1 if catalog_only else 0,
                        REVIEW_PENDING,
                        created_at,
                    ),
                )
                item_id = int(cur.lastrowid)
        if not catalog_only and typ in {"world", "preset", "character", "style"} and data:
            try:
                self.replace_fragments(item_id, extract_fragments(data, typ))
            except Exception:
                pass
        item = self.get(item_id)
        assert item is not None
        return item, True

    def _touch_existing(
        self,
        existing: dict[str, Any],
        *,
        note: str = "",
        source_url: str = "",
        summary: str = "",
        thumbnail_url: str = "",
        discord: dict[str, str] | None = None,
    ) -> None:
        fields: dict[str, Any] = {"updated_at": utcnow_iso()}
        if note and note != (existing.get("note") or ""):
            fields["note"] = note
        if source_url and not existing.get("source_url"):
            fields["source_url"] = source_url
        if summary and not existing.get("summary"):
            fields["summary"] = summary
        if thumbnail_url and not existing.get("thumbnail_url"):
            fields["thumbnail_url"] = thumbnail_url
        if discord:
            # Keep the original thread if one already exists.
            if not existing.get("discord_thread_id"):
                for key in (
                    "discord_guild_id",
                    "discord_channel_id",
                    "discord_thread_id",
                    "discord_message_id",
                    "discord_jump_url",
                ):
                    short = key.replace("discord_", "")
                    # keys in discord dict are without prefix sometimes
                    val = discord.get(key) or discord.get(short)
                    if val:
                        fields[key] = val
        self.update_fields(int(existing["id"]), **fields)

    def update_fields(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "type",
            "subtype",
            "title",
            "original_filename",
            "stored_path",
            "note",
            "author",
            "uncertain",
            "reason",
            "mime_type",
            "file_size",
            "source_url",
            "summary",
            "recommended_presets",
            "recommended_models",
            "recommended_artist_strings",
            "license_repost",
            "license_tavern",
            "license_phone",
            "license_note",
            "review_status",
            "worldbook_origin",
            "version_group",
            "version_label",
            "bundle_id",
            "catalog_only",
            "thumbnail_url",
            "updated_at",
            "ai_prefill",
            "needs_refile",
            "discord_form_message_id",
            "discord_guild_id",
            "discord_channel_id",
            "discord_thread_id",
            "discord_message_id",
            "discord_jump_url",
        }
        sets = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"recommended_presets", "recommended_models", "recommended_artist_strings"} and not isinstance(value, str):
                value = dump_list_field(value)
            if key in {"license_repost", "license_tavern", "license_phone"}:
                value = valid_license(value)
            if key == "review_status":
                value = valid_review(value)
            if key in {"catalog_only", "uncertain", "needs_refile"}:
                value = 1 if value else 0
            if key in {"title", "author", "summary", "note", "license_note", "source_url", "thumbnail_url"} and isinstance(value, str):
                value = value.replace("\x00", "")[:MAX_TEXT if key != "title" else 150]
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        if "updated_at" not in fields:
            sets.append("updated_at = ?")
            params.append(utcnow_iso())
        params.append(item_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params)

    def update_discord(self, item_id: int, **fields: str | None) -> None:
        allowed = {
            "discord_guild_id",
            "discord_channel_id",
            "discord_thread_id",
            "discord_message_id",
            "discord_jump_url",
            "discord_form_message_id",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if payload:
            self.update_fields(item_id, **payload)

    def mark_imported(self, item_id: int, imported_as_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE items SET imported_at = ?, imported_as_name = ?, updated_at = ? WHERE id = ?",
                (utcnow_iso(), imported_as_name, utcnow_iso(), item_id),
            )

    def abspath(self, stored_path: str) -> Path:
        if not stored_path:
            raise ValueError("empty stored path")
        root = self.root.resolve()
        path = (root / stored_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError("path escapes library root")
        return path

    # --- type changes / file moves -----------------------------------------

    def change_type(self, item_id: int, new_type: str, subtype: str | None = None) -> dict[str, Any]:
        item = self.get(item_id)
        if not item:
            raise ValueError("item not found")
        if new_type not in TYPE_FOLDERS:
            raise ValueError("unknown type")
        old_type = item["type"]
        catalog_only = new_type in CATALOG_ONLY_TYPES
        fields: dict[str, Any] = {
            "type": new_type,
            "review_status": item.get("review_status") or REVIEW_PENDING,
            "uncertain": 0,
            "catalog_only": catalog_only,
            "needs_refile": 1 if item.get("discord_thread_id") and old_type != new_type else item.get("needs_refile") or 0,
        }
        if subtype is not None:
            fields["subtype"] = subtype
        elif old_type != new_type:
            # Drop a subtype that only makes sense on the previous type.
            if not (old_type in {"preset", "beautify"} and new_type in {"preset", "beautify"}):
                fields["subtype"] = item.get("subtype") if new_type in {"preset", "beautify"} else None

        if catalog_only:
            # Remove any previously stored file (e.g. a forwarded image reclassified as 生图).
            if item.get("stored_path"):
                try:
                    path = self.abspath(item["stored_path"])
                    if path.is_file():
                        path.unlink()
                except (ValueError, OSError):
                    pass
            fields["stored_path"] = ""
            fields["file_size"] = 0
        elif item.get("stored_path") and old_type != new_type:
            try:
                src = self.abspath(item["stored_path"])
            except ValueError:
                src = None
            if src and src.is_file():
                dest_dir = self.root / TYPE_FOLDERS[new_type]
                if item.get("subtype") and new_type in {"preset", "beautify"}:
                    dest_dir = dest_dir / safe_filename(str(item.get("subtype")), "misc")
                dest = unique_path(dest_dir, src.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                try:
                    os.chmod(dest, 0o664)
                except OSError:
                    pass
                fields["stored_path"] = str(dest.relative_to(self.root))

        self.update_fields(item_id, **fields)
        enriched = self.get_enriched(item_id)
        assert enriched is not None
        return enriched

    def set_bundle(self, item_ids: list[int], bundle_id: str | None = None) -> str:
        import uuid

        ids = [int(i) for i in item_ids if i]
        if len(ids) < 2:
            return bundle_id or ""
        bid = bundle_id or str(uuid.uuid4())
        with self._connect() as conn:
            for item_id in ids:
                conn.execute(
                    "UPDATE items SET bundle_id = ?, updated_at = ? WHERE id = ?",
                    (bid, utcnow_iso(), item_id),
                )
            for i, a in enumerate(ids):
                for b in ids[i + 1 :]:
                    conn.execute(
                        "INSERT OR IGNORE INTO item_links(from_id, to_id, kind, note) VALUES(?, ?, 'bundle', '')",
                        (a, b),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO item_links(from_id, to_id, kind, note) VALUES(?, ?, 'bundle', '')",
                        (b, a),
                    )
        return bid

    # --- tags / themes / links / fragments ---------------------------------

    def list_tags(self, bucket: str | None = None, *, include_blacklisted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tags"
        clauses: list[str] = []
        params: list[Any] = []
        if bucket and valid_bucket(bucket):
            clauses.append("bucket = ?")
            params.append(bucket)
        if not include_blacklisted:
            clauses.append("blacklisted = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY bucket, name"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def list_theme_categories(self, typ: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM theme_categories"
        params: list[Any] = []
        if typ and typ in TYPE_FOLDERS:
            sql += " WHERE type = ? OR type = 'any'"
            params.append(typ)
        sql += " ORDER BY type, path"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def ensure_theme_category(self, typ: str, path: str) -> str:
        cleaned = normalize_theme_path(path)
        typ = typ if typ in TYPE_FOLDERS else "other"
        if not cleaned:
            return ""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO theme_categories(type, path) VALUES(?, ?)",
                (typ, cleaned),
            )
        return cleaned

    def ensure_tag(
        self,
        bucket: str,
        name: str,
        *,
        pending: bool = False,
        source: str = "user",
        locked: bool = False,
    ) -> dict[str, Any] | None:
        bucket = valid_bucket(bucket) or ""
        name = normalize_tag_name(name)
        if not bucket or not name:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tags WHERE bucket = ? AND name = ?",
                (bucket, name),
            ).fetchone()
            if row:
                tag = dict(row)
                if tag.get("blacklisted"):
                    return tag
                return tag
            # Unknown tags from AI/heuristics stay 待确认; user-created tags do not.
            is_pending = 1 if (pending or source in {"ai", "heuristic"}) else 0
            conn.execute(
                "INSERT INTO tags(bucket, name, locked, blacklisted, pending) VALUES(?, ?, ?, 0, ?)",
                (bucket, name, 1 if locked else 0, is_pending),
            )
            row = conn.execute(
                "SELECT * FROM tags WHERE bucket = ? AND name = ?",
                (bucket, name),
            ).fetchone()
        return dict(row) if row else None

    def set_item_themes(self, item_id: int, paths: list[str], typ: str | None = None) -> list[str]:
        item = typ and {"type": typ} or self.get(item_id) or {}
        typ = typ or item.get("type") or "other"
        cleaned: list[str] = []
        for path in paths:
            value = self.ensure_theme_category(str(typ), path)
            if value and value not in cleaned:
                cleaned.append(value)
        with self._connect() as conn:
            conn.execute("DELETE FROM item_themes WHERE item_id = ?", (item_id,))
            for path in cleaned:
                conn.execute("INSERT OR IGNORE INTO item_themes(item_id, path) VALUES(?, ?)", (item_id, path))
            conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (utcnow_iso(), item_id))
        return cleaned

    def add_item_theme(self, item_id: int, path: str) -> list[str]:
        item = self.get(item_id)
        if not item:
            return []
        current = [t for t in (self.enrich(item) or {}).get("themes") or []]
        if path and path not in current:
            current.append(path)
        return self.set_item_themes(item_id, current, typ=item.get("type"))

    def set_item_tags(
        self,
        item_id: int,
        assignments: list[dict[str, str]],
        *,
        replace_buckets: list[str] | None = None,
        source: str = "user",
    ) -> list[dict[str, Any]]:
        """assignments: [{bucket, name, source?}]. User source clears pending on that tag if they confirm it."""
        ensured: list[tuple[int, str]] = []
        for raw in assignments:
            bucket = valid_bucket(raw.get("bucket"))
            name = normalize_tag_name(raw.get("name") or "")
            if not bucket or not name:
                continue
            src = raw.get("source") or source
            tag = self.ensure_tag(bucket, name, pending=(src != "user"), source=src)
            if not tag or tag.get("blacklisted"):
                continue
            if src == "user" and tag.get("pending"):
                with self._connect() as conn:
                    conn.execute("UPDATE tags SET pending = 0 WHERE id = ?", (tag["id"],))
                    tag["pending"] = 0
            ensured.append((int(tag["id"]), src))
        with self._connect() as conn:
            if replace_buckets == ["*"]:
                conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
            elif replace_buckets:
                q = ",".join("?" * len(replace_buckets))
                conn.execute(
                    f"DELETE FROM item_tags WHERE item_id = ? AND tag_id IN (SELECT id FROM tags WHERE bucket IN ({q}))",
                    [item_id, *replace_buckets],
                )
            for tag_id, src in ensured:
                conn.execute(
                    "INSERT INTO item_tags(item_id, tag_id, source) VALUES(?, ?, ?) "
                    "ON CONFLICT(item_id, tag_id) DO UPDATE SET source = CASE WHEN excluded.source = 'user' THEN 'user' ELSE item_tags.source END",
                    (item_id, tag_id, src),
                )
            conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (utcnow_iso(), item_id))
        item = self.get_enriched(item_id)
        return (item or {}).get("tags") or []

    def remove_item_tag(self, item_id: int, tag_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?", (item_id, tag_id))
            conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (utcnow_iso(), item_id))

    def merge_tags(self, keep_id: int, drop_id: int) -> None:
        """Merge drop_id into keep_id. Later UI can call this; logic is ready now."""
        if keep_id == drop_id:
            return
        with self._connect() as conn:
            keep = conn.execute("SELECT * FROM tags WHERE id = ?", (keep_id,)).fetchone()
            drop = conn.execute("SELECT * FROM tags WHERE id = ?", (drop_id,)).fetchone()
            if not keep or not drop:
                return
            if keep["locked"] or drop["locked"]:
                raise ValueError("cannot merge a locked tag")
            rows = conn.execute("SELECT item_id FROM item_tags WHERE tag_id = ?", (drop_id,)).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO item_tags(item_id, tag_id, source) VALUES(?, ?, 'user')",
                    (row["item_id"], keep_id),
                )
            conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (drop_id,))
            conn.execute("DELETE FROM tags WHERE id = ?", (drop_id,))

    def set_tag_flags(self, tag_id: int, *, locked: bool | None = None, blacklisted: bool | None = None, pending: bool | None = None) -> None:
        sets = []
        params: list[Any] = []
        if locked is not None:
            sets.append("locked = ?")
            params.append(1 if locked else 0)
        if blacklisted is not None:
            sets.append("blacklisted = ?")
            params.append(1 if blacklisted else 0)
        if pending is not None:
            sets.append("pending = ?")
            params.append(1 if pending else 0)
        if not sets:
            return
        params.append(tag_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE tags SET {', '.join(sets)} WHERE id = ?", params)
            if blacklisted:
                conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (tag_id,))

    def add_link(self, from_id: int, to_id: int, kind: str, note: str = "") -> dict[str, Any] | None:
        kind = valid_link_kind(kind) or ""
        if not kind or from_id == to_id:
            return None
        if not self.get(from_id) or not self.get(to_id):
            return None
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO item_links(from_id, to_id, kind, note) VALUES(?, ?, ?, ?)",
                (from_id, to_id, kind, (note or "")[:200]),
            )
            # Keep the reverse direction for symmetric kinds so either side can find the other.
            if kind in {"character_worldbook", "character_relation", "version", "bundle", "persona_character", "preset_worldbook", "theme_background", "worldbook_extra"}:
                conn.execute(
                    "INSERT OR IGNORE INTO item_links(from_id, to_id, kind, note) VALUES(?, ?, ?, ?)",
                    (to_id, from_id, kind, (note or "")[:200]),
                )
            if kind == "version":
                rows = conn.execute(
                    "SELECT version_group FROM items WHERE id IN (?, ?) AND IFNULL(version_group, '') != ''",
                    (from_id, to_id),
                ).fetchall()
                group = (rows[0]["version_group"] if rows else "") or f"ver-{min(from_id, to_id)}"
                conn.execute(
                    "UPDATE items SET version_group = ?, updated_at = ? WHERE id IN (?, ?)",
                    (group, utcnow_iso(), from_id, to_id),
                )
            if kind == "character_worldbook":
                conn.execute(
                    "UPDATE items SET worldbook_origin = CASE WHEN type = 'world' AND IFNULL(worldbook_origin, '') IN ('', 'standalone') THEN 'bound' ELSE worldbook_origin END, updated_at = ? WHERE id IN (?, ?)",
                    (utcnow_iso(), from_id, to_id),
                )
            if kind == "worldbook_extra":
                conn.execute(
                    "UPDATE items SET worldbook_origin = CASE WHEN type = 'world' THEN 'extra' ELSE worldbook_origin END, updated_at = ? WHERE id = ?",
                    (utcnow_iso(), to_id),
                )
            row = conn.execute(
                "SELECT * FROM item_links WHERE from_id = ? AND to_id = ? AND kind = ?",
                (from_id, to_id, kind),
            ).fetchone()
        return dict(row) if row else None

    def remove_link(self, link_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM item_links WHERE id = ?", (link_id,)).fetchone()
            if not row:
                return
            conn.execute("DELETE FROM item_links WHERE id = ?", (link_id,))
            conn.execute(
                "DELETE FROM item_links WHERE from_id = ? AND to_id = ? AND kind = ?",
                (row["to_id"], row["from_id"], row["kind"]),
            )

    def replace_fragments(self, item_id: int, fragments: list[dict[str, str]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM fragments WHERE item_id = ?", (item_id,))
            for frag in fragments:
                conn.execute(
                    "INSERT INTO fragments(item_id, kind, title, excerpt, source_ref) VALUES(?, ?, ?, ?, ?)",
                    (
                        item_id,
                        frag.get("kind") or "other",
                        (frag.get("title") or "")[:80],
                        (frag.get("excerpt") or "")[:MAX_TEXT],
                        (frag.get("source_ref") or "")[:200],
                    ),
                )

    def taxonomy_payload(self) -> dict[str, Any]:
        return {
            "buckets": [{"id": k, "label": v} for k, v in TAG_BUCKETS],
            "licenses": [{"id": k, "label": v} for k, v in __import__("tavernlib.taxonomy", fromlist=["LICENSE_VALUES"]).LICENSE_VALUES],
            "license_fields": [{"id": k, "label": v} for k, v in LICENSE_FIELDS],
            "link_kinds": [{"id": k, "label": v} for k, v in __import__("tavernlib.taxonomy", fromlist=["LINK_KINDS"]).LINK_KINDS],
            "review_statuses": [{"id": REVIEW_PENDING, "label": "待确认"}, {"id": REVIEW_CONFIRMED, "label": "已确认"}],
            "types": [{"id": k, "label": __import__("tavernlib.paths", fromlist=["TYPE_LABELS"]).TYPE_LABELS[k]} for k in TYPE_FOLDERS],
            "theme_seeds": THEME_SEEDS,
            "tags": self.list_tags(include_blacklisted=False),
            "themes": self.list_theme_categories(),
            "worldbook_origins": [
                {"id": k, "label": v}
                for k, v in __import__("tavernlib.taxonomy", fromlist=["WORLDBOOK_ORIGINS"]).WORLDBOOK_ORIGINS
            ],
        }

    def apply_metadata(self, item_id: int, payload: dict[str, Any], *, source: str = "user") -> dict[str, Any] | None:
        """Apply a metadata patch from the Discord form or the library web UI."""
        item = self.get(item_id)
        if not item:
            return None
        fields: dict[str, Any] = {}
        for key in (
            "title",
            "author",
            "source_url",
            "summary",
            "note",
            "license_note",
            "worldbook_origin",
            "version_label",
            "subtype",
        ):
            if key in payload and payload[key] is not None:
                fields[key] = payload[key]
        for key in ("recommended_presets", "recommended_models", "recommended_artist_strings"):
            if key in payload and payload[key] is not None:
                fields[key] = parse_list_field(payload[key])
        for key, _ in LICENSE_FIELDS:
            if key in payload and payload[key] is not None:
                fields[key] = valid_license(payload[key])
        if "review_status" in payload and payload["review_status"] is not None:
            fields["review_status"] = valid_review(payload["review_status"])
        new_type = payload.get("type")
        if new_type and new_type in TYPE_FOLDERS and new_type != item["type"]:
            self.change_type(item_id, new_type, subtype=payload.get("subtype") if "subtype" in payload else None)
            leftover = {k: v for k, v in fields.items() if k not in {"type"}}
            if leftover:
                self.update_fields(item_id, **leftover)
        elif fields:
            self.update_fields(item_id, **fields)

        if "themes" in payload and payload["themes"] is not None:
            typ = (self.get(item_id) or item).get("type")
            self.set_item_themes(item_id, parse_list_field(payload["themes"]), typ=typ)
        if "tags" in payload and payload["tags"] is not None:
            raw_tags = payload["tags"]
            assignments = []
            if isinstance(raw_tags, list):
                for t in raw_tags:
                    if isinstance(t, dict):
                        assignments.append({"bucket": t.get("bucket", ""), "name": t.get("name", ""), "source": t.get("source", source)})
                    elif isinstance(t, str) and ":" in t:
                        bucket, _, name = t.partition(":")
                        assignments.append({"bucket": bucket, "name": name, "source": source})
            self.set_item_tags(item_id, assignments, replace_buckets=["*"], source=source)
        if "links_add" in payload and isinstance(payload["links_add"], list):
            for link in payload["links_add"]:
                if not isinstance(link, dict):
                    continue
                try:
                    other = int(link.get("item_id") or link.get("to_id"))
                except (TypeError, ValueError):
                    continue
                self.add_link(item_id, other, str(link.get("kind") or ""), str(link.get("note") or ""))
        if "links_remove" in payload and isinstance(payload["links_remove"], list):
            for lid in payload["links_remove"]:
                try:
                    self.remove_link(int(lid))
                except (TypeError, ValueError):
                    continue
        return self.get_enriched(item_id)
