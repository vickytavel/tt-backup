"""SQLite-backed library store. Files live on disk; metadata lives in library.db."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .fragments import extract_fragments
from .pieces import extract_from_bytes
from .paths import CATALOG_ONLY_TYPES, IMPORTABLE_TYPES, LIBRARY_ROOT, TYPE_FOLDERS

logger = logging.getLogger(__name__)
from .taxonomy import (
    BUCKET_IDS,
    LICENSE_FIELDS,
    LINK_IDS,
    MAX_TEXT,
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    SCHEMA_VERSION,
    SCOPE_LABELS,
    TAG_BUCKETS,
    TAGLIB,
    THEME_SEEDS,
    dump_list_field,
    is_deprecated,
    load_list_field,
    normalize_tag_name,
    normalize_theme_path,
    parse_list_field,
    resolve_tag,
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
    ("source_intro_id", "TEXT"),
]

TAG_NEW_COLUMNS: list[tuple[str, str]] = [
    ("tag_id", "TEXT"),
    ("scope", "TEXT"),
    ("aliases", "TEXT"),
    ("definition", "TEXT"),
    ("negative", "TEXT"),
    ("precondition", "TEXT"),
    ("filterable", "TEXT"),
]

# v2.1 six-bucket taxonomy -> v2.2 axes. Names that exist in the vocabulary are
# re-filed by `_seed_vocabulary`; this only rescues leftovers.
LEGACY_BUCKET_MAP = {
    "genre": "plot",
    "purpose": "gameplay",
    "basic": "character",
    "recommend": "plot",
}

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
    tag_id TEXT,
    scope TEXT,
    aliases TEXT,
    definition TEXT,
    negative TEXT,
    precondition TEXT,
    filterable TEXT,
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
CREATE TABLE IF NOT EXISTS pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ord INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pieces_item ON pieces(item_id, kind, ord);
CREATE INDEX IF NOT EXISTS idx_pieces_kind ON pieces(kind);
CREATE TABLE IF NOT EXISTS piece_tags (
    piece_id INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (piece_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_piece_tags_tag ON piece_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_items_review ON items(review_status);
CREATE INDEX IF NOT EXISTS idx_items_bundle ON items(bundle_id);
CREATE TABLE IF NOT EXISTS intros (
    message_id TEXT PRIMARY KEY,
    guild_id TEXT,
    channel_id TEXT,
    author TEXT,
    text TEXT,
    assets TEXT,
    jump_url TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_WRITE_LOCK = threading.Lock()
LOG = logging.getLogger(__name__)


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
        tag_columns = {row[1] for row in conn.execute("PRAGMA table_info(tags)").fetchall()}
        for name, decl in TAG_NEW_COLUMNS:
            if name not in tag_columns:
                conn.execute(f"ALTER TABLE tags ADD COLUMN {name} {decl}")
        current = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        version = int(current["value"]) if current and str(current["value"]).isdigit() else 1
        if version < 5:
            self._backfill_pieces(conn)
        if version < SCHEMA_VERSION:
            for old, new in LEGACY_BUCKET_MAP.items():
                conn.execute("UPDATE tags SET bucket = ? WHERE bucket = ?", (new, old))
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def _backfill_pieces(self, conn: sqlite3.Connection) -> None:
        """One-off: split existing files into pieces. Safe to re-run (replaces)."""
        rows = conn.execute(
            "SELECT id, type, stored_path FROM items WHERE type IN ('character','world','preset','style')"
        ).fetchall()
        for row in rows:
            try:
                data = self.abspath(row["stored_path"]).read_bytes()
            except (OSError, ValueError):
                continue
            self._write_pieces(conn, int(row["id"]), extract_from_bytes(data, row["type"]))

    @staticmethod
    def _write_pieces(conn: sqlite3.Connection, item_id: int, pieces: list[dict[str, Any]]) -> None:
        conn.execute("DELETE FROM pieces WHERE item_id = ?", (item_id,))
        if not pieces:
            return
        now = utcnow_iso()
        conn.executemany(
            "INSERT INTO pieces(item_id, kind, ord, title, text, meta, source_ref, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item_id,
                    piece.get("kind") or "body",
                    int(piece.get("ord") or 0),
                    str(piece.get("title") or "")[:120],
                    str(piece.get("text") or ""),
                    json.dumps(piece.get("meta") or {}, ensure_ascii=False),
                    str(piece.get("source_ref") or ""),
                    now,
                )
                for piece in pieces
            ],
        )

    def _seed(self, conn: sqlite3.Connection) -> None:
        for typ, paths in THEME_SEEDS.items():
            for path in paths:
                conn.execute(
                    "INSERT OR IGNORE INTO theme_categories(type, path) VALUES(?, ?)",
                    (typ, path),
                )
        self._seed_vocabulary(conn)

    @staticmethod
    def _seed_vocabulary(conn: sqlite3.Connection) -> None:
        """Upsert the controlled vocabulary by name so existing rows get their IDs."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            for tag in TAGLIB["tags"]:
                name = tag["name"]
                payload = (
                    tag["axis"],
                    tag.get("scope") or "",
                    json.dumps(tag.get("aliases") or [], ensure_ascii=False),
                    tag.get("definition") or "",
                    tag.get("negative") or "",
                    tag.get("precondition") or "",
                    tag.get("filterable") or "",
                    tag["id"],
                )
                rows = conn.execute(
                    "SELECT id FROM tags WHERE name = ? ORDER BY id", (name,)
                ).fetchall()
                if rows:
                    keep_id = int(rows[0]["id"])
                    conn.execute(
                        "UPDATE tags SET bucket = ?, scope = ?, aliases = ?, definition = ?, "
                        "negative = ?, precondition = ?, filterable = ?, tag_id = ?, locked = 1, pending = 0 "
                        "WHERE id = ?",
                        (*payload, keep_id),
                    )
                    for extra in rows[1:]:
                        drop_id = int(extra["id"])
                        conn.execute(
                            "INSERT OR IGNORE INTO item_tags(item_id, tag_id, source) "
                            "SELECT item_id, ?, source FROM item_tags WHERE tag_id = ?",
                            (keep_id, drop_id),
                        )
                        conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (drop_id,))
                        conn.execute("DELETE FROM tags WHERE id = ?", (drop_id,))
                else:
                    conn.execute(
                        "INSERT INTO tags(bucket, name, scope, aliases, definition, negative, "
                        "precondition, filterable, tag_id, locked, blacklisted, pending) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0)",
                        (payload[0], name, *payload[1:]),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

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

    def save_intro(
        self,
        *,
        message_id: str,
        guild_id: str = "",
        channel_id: str = "",
        author: str = "",
        text: str = "",
        assets: list[dict[str, str]] | None = None,
        jump_url: str = "",
    ) -> None:
        """Remember a forwarded author blurb until its character card arrives."""
        keep = [a for a in (assets or []) if isinstance(a, dict)]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intros(message_id, guild_id, channel_id, author, text, assets, jump_url, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    channel_id = excluded.channel_id,
                    author = excluded.author,
                    text = excluded.text,
                    assets = excluded.assets,
                    jump_url = excluded.jump_url
                """,
                (
                    str(message_id),
                    guild_id,
                    channel_id,
                    author,
                    text,
                    json.dumps(keep, ensure_ascii=False),
                    jump_url,
                    utcnow_iso(),
                ),
            )

    def get_intro(self, message_id: str) -> dict[str, Any] | None:
        key = str(message_id or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM intros WHERE message_id = ?", (key,)).fetchone()
        out = _row_to_dict(row)
        if out is None:
            return None
        try:
            out["assets"] = json.loads(out.get("assets") or "[]")
        except ValueError:
            out["assets"] = []
        return out

    def enrich(self, item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        item_id = int(item["id"])
        with self._connect() as conn:
            tags = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT t.id, t.bucket, t.name, t.tag_id, t.scope, t.locked,
                           t.blacklisted, t.pending, it.source
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
            pieces = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, kind, ord, title, source_ref, meta, length(text) AS chars "
                    "FROM pieces WHERE item_id = ? ORDER BY kind, ord, id",
                    (item_id,),
                )
            ]
            if pieces:
                marks = ",".join("?" * len(pieces))
                grouped: dict[int, list[dict[str, Any]]] = {}
                for row in conn.execute(
                    "SELECT pt.piece_id, t.id, t.bucket, t.name, pt.source "
                    "FROM piece_tags pt JOIN tags t ON t.id = pt.tag_id "
                    f"WHERE pt.piece_id IN ({marks}) ORDER BY t.bucket, t.name",
                    [p["id"] for p in pieces],
                ):
                    grouped.setdefault(int(row["piece_id"]), []).append(
                        {"id": row["id"], "bucket": row["bucket"], "name": row["name"], "source": row["source"]}
                    )
                for piece in pieces:
                    try:
                        piece["meta"] = json.loads(piece.get("meta") or "{}")
                    except (ValueError, TypeError):
                        piece["meta"] = {}
                    piece["tags"] = grouped.get(int(piece["id"]), [])
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
        out["pieces"] = pieces
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
            joins.append("LEFT JOIN pieces pq ON pq.item_id = items.id")
            clauses.append(
                "("
                "items.title LIKE ? OR items.original_filename LIKE ? OR IFNULL(items.note, '') LIKE ? "
                "OR IFNULL(items.author, '') LIKE ? OR IFNULL(items.summary, '') LIKE ? "
                "OR IFNULL(items.source_url, '') LIKE ? OR IFNULL(tgq.name, '') LIKE ? "
                "OR IFNULL(thq.path, '') LIKE ? OR IFNULL(fq.excerpt, '') LIKE ? OR IFNULL(fq.title, '') LIKE ? "
                "OR IFNULL(pq.text, '') LIKE ? OR IFNULL(pq.title, '') LIKE ?"
                ")"
            )
            params.extend([like] * 12)
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
                logger.warning(
                    "fragment indexing failed for item %s (type=%s)", item_id, typ, exc_info=True
                )
            try:
                with self._connect() as conn:
                    self._write_pieces(conn, item_id, extract_from_bytes(data, typ))
            except Exception:
                LOG.exception("extract pieces failed for item %s", item_id)
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

    def list_bundle(self, bundle_id: str) -> list[dict[str, Any]]:
        """All items sharing one bundle id (including ones without explicit links)."""
        if not bundle_id:
            return []
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM items WHERE bundle_id = ? ORDER BY id",
                    (bundle_id,),
                )
            ]

    def delete_item(self, item_id: int) -> dict[str, Any] | None:
        """Remove an item and every related row from the library.

        The stored file is moved into <root>/.trash/<stamp>-<id>/ instead of
        being unlinked, so an accidental delete stays recoverable. Returns a
        summary dict, or None when the id is unknown.
        """
        item = self.get(item_id)
        if not item:
            return None
        moved_to = ""
        stored_path = str(item.get("stored_path") or "")
        if stored_path:
            try:
                src = self.abspath(stored_path)
                if src.is_file():
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    dest_dir = self.root / ".trash" / f"{stamp}-{item_id}"
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / src.name
                    shutil.move(str(src), str(dest))
                    try:
                        os.chmod(dest, 0o664)
                    except OSError:
                        pass
                    moved_to = str(dest.relative_to(self.root))
            except (ValueError, OSError):
                LOG.warning("delete_item: could not move stored file for item %s", item_id, exc_info=True)
        with self._connect() as conn:
            piece_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM pieces WHERE item_id = ?", (item_id,))]
            for pid in piece_ids:
                conn.execute("DELETE FROM piece_tags WHERE piece_id = ?", (pid,))
            conn.execute("DELETE FROM pieces WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM fragments WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM item_themes WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM item_links WHERE from_id = ? OR to_id = ?", (item_id, item_id))
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return {
            "id": item_id,
            "title": item.get("title") or "",
            "stored_path": stored_path,
            "moved_to": moved_to,
        }

    # --- tags / themes / links / fragments ---------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        """Read a library-level key/value setting (empty string if unset)."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return (row["value"] or "") if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

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
        name = normalize_tag_name(name)
        if not name or is_deprecated(name):
            return None
        record = resolve_tag(name)
        if record:
            bucket = record["axis"]
            name = record["name"]
        else:
            bucket = valid_bucket(bucket) or ""
        if not bucket:
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
                if record and not tag.get("tag_id"):
                    conn.execute(
                        "UPDATE tags SET tag_id = ?, scope = ?, aliases = ?, definition = ?, "
                        "negative = ?, precondition = ?, filterable = ?, pending = 0 WHERE id = ?",
                        (
                            record["id"],
                            record.get("scope") or "",
                            json.dumps(record.get("aliases") or [], ensure_ascii=False),
                            record.get("definition") or "",
                            record.get("negative") or "",
                            record.get("precondition") or "",
                            record.get("filterable") or "",
                            tag["id"],
                        ),
                    )
                    tag = dict(conn.execute("SELECT * FROM tags WHERE id = ?", (tag["id"],)).fetchone())
                return tag
            # Unknown tags from AI/heuristics stay 待确认; user-created tags do not.
            is_pending = 1 if (pending or source in {"ai", "heuristic"}) else 0
            conn.execute(
                "INSERT INTO tags(bucket, name, locked, blacklisted, pending, tag_id, scope, "
                "aliases, definition, negative, precondition, filterable) "
                "VALUES(?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bucket,
                    name,
                    1 if (locked or record) else 0,
                    is_pending if not record else 0,
                    record["id"] if record else None,
                    (record.get("scope") or "") if record else None,
                    json.dumps(record.get("aliases") or [], ensure_ascii=False) if record else None,
                    (record.get("definition") or "") if record else None,
                    (record.get("negative") or "") if record else None,
                    (record.get("precondition") or "") if record else None,
                    (record.get("filterable") or "") if record else None,
                ),
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

    # --- pieces: body / greeting / world entry / preset block ---------------

    def pieces(self, item_id: int, *, with_text: bool = False, kind: str | None = None) -> list[dict[str, Any]]:
        """Pieces of one item, ordered by kind then original order."""
        columns = "id, kind, ord, title, source_ref, meta, length(text) AS chars"
        if with_text:
            columns += ", text"
        sql = f"SELECT {columns} FROM pieces WHERE item_id = ?"
        params: list[Any] = [item_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY kind, ord, id"
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
            if not rows:
                return []
            marks = ",".join("?" * len(rows))
            grouped: dict[int, list[dict[str, Any]]] = {}
            for tag in conn.execute(
                "SELECT pt.piece_id, t.id, t.bucket, t.name, pt.source "
                "FROM piece_tags pt JOIN tags t ON t.id = pt.tag_id "
                f"WHERE pt.piece_id IN ({marks}) ORDER BY t.bucket, t.name",
                [r["id"] for r in rows],
            ):
                grouped.setdefault(int(tag["piece_id"]), []).append(
                    {"id": tag["id"], "bucket": tag["bucket"], "name": tag["name"], "source": tag["source"]}
                )
        for row in rows:
            try:
                row["meta"] = json.loads(row.get("meta") or "{}")
            except (ValueError, TypeError):
                row["meta"] = {}
            row["tags"] = grouped.get(int(row["id"]), [])
        return rows

    def piece(self, piece_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pieces WHERE id = ?", (piece_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["meta"] = json.loads(data.get("meta") or "{}")
        except (ValueError, TypeError):
            data["meta"] = {}
        data["tags"] = self.piece_tags(piece_id)
        return data

    def replace_pieces(self, item_id: int, pieces: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            self._write_pieces(conn, item_id, pieces)

    def extract_pieces(self, item_id: int) -> int:
        """Re-read the stored file and rebuild its pieces. Returns piece count."""
        item = self.get(item_id)
        if not item or not item.get("stored_path"):
            return 0
        try:
            data = self.abspath(item["stored_path"]).read_bytes()
        except (OSError, ValueError):
            return 0
        pieces = extract_from_bytes(data, item.get("type") or "")
        self.replace_pieces(item_id, pieces)
        return len(pieces)

    def search_pieces(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 50,
        with_text: bool = False,
    ) -> list[dict[str, Any]]:
        """Search piece text and title. Returns one row per matching piece."""
        term = f"%{(query or '').strip()}%"
        if term == "%%":
            return []
        text_col = "p.text" if with_text else "''"
        sql = (
            f"SELECT p.id, p.item_id, p.kind, p.ord, p.title, p.source_ref, p.meta, {text_col} AS text, "
            "length(p.text) AS chars, i.title AS item_title, i.type AS item_type, i.stored_path "
            "FROM pieces p JOIN items i ON i.id = p.item_id "
            "WHERE (p.text LIKE ? OR p.title LIKE ?)"
        )
        params: list[Any] = [term, term]
        if kind:
            sql += " AND p.kind = ?"
            params.append(kind)
        sql += " ORDER BY p.item_id, p.kind, p.ord LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        for row in rows:
            try:
                row["meta"] = json.loads(row.get("meta") or "{}")
            except (ValueError, TypeError):
                row["meta"] = {}
        return rows

    def piece_tags(self, piece_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT t.id, t.bucket, t.name, pt.source FROM piece_tags pt "
                    "JOIN tags t ON t.id = pt.tag_id WHERE pt.piece_id = ? ORDER BY t.bucket, t.name",
                    (piece_id,),
                )
            ]

    def set_piece_tags(
        self,
        piece_id: int,
        assignments: list[dict[str, str]],
        *,
        replace: bool = True,
        source: str = "user",
    ) -> list[dict[str, Any]]:
        """Bind tags to one piece. assignments: [{bucket, name, source?}]."""
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
            ensured.append((int(tag["id"]), src))
        with self._connect() as conn:
            if replace:
                conn.execute("DELETE FROM piece_tags WHERE piece_id = ?", (piece_id,))
            for tag_id, src in ensured:
                conn.execute(
                    "INSERT INTO piece_tags(piece_id, tag_id, source) VALUES(?, ?, ?) "
                    "ON CONFLICT(piece_id, tag_id) DO UPDATE SET source = "
                    "CASE WHEN excluded.source = 'user' THEN 'user' ELSE piece_tags.source END",
                    (piece_id, tag_id, src),
                )
        return self.piece_tags(piece_id)

    def remove_piece_tag(self, piece_id: int, tag_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM piece_tags WHERE piece_id = ? AND tag_id = ?", (piece_id, tag_id))

    def pieces_by_tag(self, tag_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT p.id, p.item_id, p.kind, p.title, i.title AS item_title "
                    "FROM piece_tags pt JOIN pieces p ON p.id = pt.piece_id "
                    "JOIN items i ON i.id = p.item_id WHERE pt.tag_id = ? LIMIT ?",
                    (tag_id, max(1, min(int(limit), 1000))),
                )
            ]

    def related_pieces(self, item_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """Pieces of this item plus pieces of everything linked to it.

        A card's own greetings/body and its embedded world entries come back
        together; a separately stored worldbook comes back through item_links.
        """
        with self._connect() as conn:
            ids = [item_id] + [
                int(row["other"]) for row in conn.execute(
                    "SELECT CASE WHEN from_id = ? THEN to_id ELSE from_id END AS other "
                    "FROM item_links WHERE from_id = ? OR to_id = ?",
                    (item_id, item_id, item_id),
                )
            ]
            marks = ",".join("?" * len(ids))
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT p.id, p.item_id, p.kind, p.ord, p.title, p.source_ref, p.meta, "
                    "length(p.text) AS chars, i.title AS item_title "
                    f"FROM pieces p JOIN items i ON i.id = p.item_id WHERE p.item_id IN ({marks}) "
                    "ORDER BY p.item_id, p.kind, p.ord LIMIT ?",
                    [*ids, max(1, min(int(limit), 1000))],
                )
            ]
        for row in rows:
            try:
                row["meta"] = json.loads(row.get("meta") or "{}")
            except (ValueError, TypeError):
                row["meta"] = {}
        return rows

    def taxonomy_payload(self) -> dict[str, Any]:
        return {
            "buckets": [{"id": k, "label": v} for k, v in TAG_BUCKETS],
            "axis_subs": __import__("tavernlib.taxonomy", fromlist=["AXIS_SUBS"]).AXIS_SUBS,
            "scopes": [{"id": k, "label": v} for k, v in SCOPE_LABELS],
            "field_enums": __import__("tavernlib.taxonomy", fromlist=["FIELD_ENUMS"]).FIELD_ENUMS,
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