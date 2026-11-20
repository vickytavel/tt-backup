#!/usr/bin/env python3
"""Resource-library web UI for SillyTavern. Listens only on 127.0.0.1:8003."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tavernlib import tagging
from tavernlib.import_st import ImportError_, import_item, import_set
from tavernlib.paths import ALLOWED_ORIGIN, LIBRARY_ROOT, RESOURCE_TYPES
from tavernlib.store import LibraryStore
from tavernlib.taxonomy import LICENSE_FIELDS

LISTEN_HOST = os.environ.get("TAVERN_LIB_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TAVERN_LIB_PORT", "8003"))
PREFIX = "/_lib"
STORE = LibraryStore(Path(os.environ.get("TAVERN_LIBRARY", str(LIBRARY_ROOT))))
PAGE_PATH = Path(__file__).resolve().parent / "web_page.html"


def load_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def is_allowed_origin(handler: BaseHTTPRequestHandler) -> bool:
    origin = (handler.headers.get("Origin") or "").strip()
    if origin:
        return origin == ALLOWED_ORIGIN
    referer = (handler.headers.get("Referer") or "").strip()
    if not referer:
        return False
    parsed = urlparse(referer)
    return f"{parsed.scheme}://{parsed.netloc}" == ALLOWED_ORIGIN


def normalize_path(path: str) -> str:
    parsed = urlparse(path)
    cleaned = parsed.path.rstrip("/") or "/"
    return cleaned


def public_item(item: dict, *, enrich: bool = True) -> dict:
    data = STORE.enrich(item) if enrich and "tags" not in item else dict(item)
    typ = data.get("type")
    return {
        "id": data["id"],
        "type": typ,
        "subtype": data.get("subtype"),
        "title": data.get("title"),
        "original_filename": data.get("original_filename"),
        "note": data.get("note") or "",
        "author": data.get("author") or "",
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "imported_at": data.get("imported_at"),
        "imported_as_name": data.get("imported_as_name"),
        "discord_jump_url": data.get("discord_jump_url"),
        "uncertain": bool(data.get("uncertain")),
        "file_size": data.get("file_size"),
        "importable": STORE.is_importable(data),
        "source_url": data.get("source_url") or "",
        "summary": data.get("summary") or "",
        "recommended_presets": data.get("recommended_presets") or [],
        "recommended_models": data.get("recommended_models") or [],
        "recommended_artist_strings": data.get("recommended_artist_strings") or [],
        "license_repost": data.get("license_repost") or "unset",
        "license_tavern": data.get("license_tavern") or "unset",
        "license_phone": data.get("license_phone") or "unset",
        "license_note": data.get("license_note") or "",
        "review_status": data.get("review_status") or "pending",
        "worldbook_origin": data.get("worldbook_origin") or "",
        "version_group": data.get("version_group") or "",
        "version_label": data.get("version_label") or "",
        "bundle_id": data.get("bundle_id") or "",
        "catalog_only": bool(data.get("catalog_only") or typ == "imagegen"),
        "thumbnail_url": data.get("thumbnail_url") or "",
        "has_file": bool(data.get("stored_path")),
        "themes": data.get("themes") or [],
        "tags": data.get("tags") or [],
        "links": data.get("links") or [],
        "fragments": data.get("fragments") or [],
        "pieces": data.get("pieces") or [],
        "ai_prefill": data.get("ai_prefill"),
    }


class LibHandler(BaseHTTPRequestHandler):
    server_version = "tavern-lib/2.0"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _forbidden(self) -> None:
        self._send_json(403, {"ok": False, "message": "请求被拒绝。"})

    def _not_found(self) -> None:
        self._send_html(404, "<!DOCTYPE html><html><body>Not found</body></html>")

    def _send_html(self, code: int, html: str) -> None:
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 1_000_000:
            return {}
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_file(self, item_id: int, *, as_download: bool, thumb: bool) -> None:
        item = STORE.get(item_id)
        if not item:
            self._not_found()
            return
        if item.get("catalog_only") or not item.get("stored_path"):
            self._not_found()
            return
        try:
            path = STORE.abspath(item["stored_path"])
        except ValueError:
            self._forbidden()
            return
        if not path.is_file():
            self._not_found()
            return
        guess, _ = mimetypes.guess_type(path.name)
        mime = item.get("mime_type") or guess or "application/octet-stream"
        if thumb and not (mime.startswith("image/") or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}):
            self._not_found()
            return
        data = path.read_bytes()
        filename = item.get("original_filename") or path.name
        safe = "".join(ch if 32 <= ord(ch) < 127 and ch not in '\\/"' else "_" for ch in filename) or "file"
        disposition = "attachment" if as_download else "inline"
        extra = {"Content-Disposition": f'{disposition}; filename="{safe}"'}
        self._send(
            200,
            data,
            mime if not as_download or mime.startswith("image/") or mime in {"application/json", "text/css", "text/plain"} else "application/octet-stream",
            extra,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in (PREFIX, f"{PREFIX}/index.html"):
            try:
                self._send_html(200, load_page())
            except OSError:
                self._send_html(500, "<!DOCTYPE html><html><body>页面丢失</body></html>")
            return
        if path == f"{PREFIX}/api/taxonomy":
            self._send_json(200, {"ok": True, **STORE.taxonomy_payload()})
            return
        if path == f"{PREFIX}/api/tag-config":
            cfg = tagging.config()
            stored = STORE.get_setting("tag_model").strip()
            self._send_json(
                200,
                {
                    "ok": True,
                    "model": cfg["model"],
                    "source": "library.db" if stored else "llm.env",
                    "base_url": cfg["base_url"],
                    "user_agent": cfg["user_agent"],
                    "auto": tagging.auto_enabled(),
                },
            )
            return
        if path == f"{PREFIX}/api/tag-models":
            cfg = tagging.config()
            req = urllib.request.Request(
                f"{cfg['base_url']}/models",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "User-Agent": cfg["user_agent"],
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                self._send_json(400, {"ok": False, "message": f"拉取模型列表失败：{exc}"})
                return
            ids = sorted(
                {str(m.get("id") or "").strip() for m in data.get("data", []) if isinstance(m, dict) and m.get("id")}
            )
            if cfg["model"] not in ids:
                ids.insert(0, cfg["model"])
            self._send_json(200, {"ok": True, "models": ids, "current": cfg["model"]})
            return
        if path == f"{PREFIX}/api/items":
            qs = parse_qs(parsed.query)
            typ = (qs.get("type") or [""])[0]
            typ = typ if typ in RESOURCE_TYPES else None
            query = (qs.get("q") or [""])[0]
            items = STORE.list_items(
                type_filter=typ,
                query=query or None,
                theme=(qs.get("theme") or [""])[0] or None,
                tag=(qs.get("tag") or [""])[0] or None,
                bucket=(qs.get("bucket") or [""])[0] or None,
                review_status=(qs.get("review") or [""])[0] or None,
                license_field=(qs.get("license_field") or [""])[0] or None,
                license_value=(qs.get("license_value") or [""])[0] or None,
                enrich=True,
            )
            self._send_json(200, {"ok": True, "items": [public_item(it, enrich=False) for it in items]})
            return
        if path == f"{PREFIX}/api/pieces":
            qs = parse_qs(parsed.query)
            query = (qs.get("q") or [""])[0]
            kind = (qs.get("kind") or [""])[0] or None
            limit = int((qs.get("limit") or ["50"])[0] or 50)
            if not query.strip():
                self._send_json(400, {"ok": False, "message": "缺少 q"})
                return
            rows = STORE.search_pieces(query, kind=kind, limit=limit)
            self._send_json(200, {"ok": True, "pieces": rows})
            return
        if path.startswith(f"{PREFIX}/api/pieces/"):
            rest = path[len(f"{PREFIX}/api/pieces/") :]
            if rest.isdigit():
                piece = STORE.piece(int(rest))
                if not piece:
                    self._send_json(404, {"ok": False, "message": "找不到这个片段"})
                    return
                self._send_json(200, {"ok": True, "piece": piece})
                return
        if path.startswith(f"{PREFIX}/api/items/"):
            rest = path[len(f"{PREFIX}/api/items/") :]
            if rest.isdigit():
                item = STORE.get_enriched(int(rest))
                if not item:
                    self._send_json(404, {"ok": False, "message": "找不到这条资源"})
                    return
                self._send_json(200, {"ok": True, "item": public_item(item, enrich=False)})
                return
        if path.startswith(f"{PREFIX}/thumb/"):
            rest = path[len(f"{PREFIX}/thumb/") :]
            if rest.isdigit():
                self._serve_file(int(rest), as_download=False, thumb=True)
                return
        if path.startswith(f"{PREFIX}/file/"):
            rest = path[len(f"{PREFIX}/file/") :]
            if rest.isdigit():
                self._serve_file(int(rest), as_download=True, thumb=False)
                return
        self._not_found()

    def do_POST(self) -> None:
        if not is_allowed_origin(self):
            self._forbidden()
            return
        path = normalize_path(self.path)
        prefix = f"{PREFIX}/api/items/"
        pieces_prefix = f"{PREFIX}/api/pieces/"
        if path == f"{PREFIX}/api/tag-config":
            body = self._read_json_body()
            model = str(body.get("model") or "").strip()
            if not model:
                try:
                    STORE.set_setting("tag_model", "")
                except Exception:
                    self._send_json(500, {"ok": False, "message": "恢复默认失败"})
                    return
                self._send_json(200, {"ok": True, "model": tagging.config()["model"], "source": "llm.env"})
                return
            if len(model) > 80 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
                self._send_json(400, {"ok": False, "message": "模型名只允许字母、数字、点、横杠、下划线"})
                return
            try:
                STORE.set_setting("tag_model", model)
            except Exception:
                self._send_json(500, {"ok": False, "message": "保存失败"})
                return
            self._send_json(200, {"ok": True, "model": tagging.config()["model"], "source": "library.db"})
            return
        if path.startswith(pieces_prefix) and path.endswith("/tags"):
            pid = path[len(pieces_prefix) : -len("/tags")]
            if pid.isdigit():
                body = self._read_json_body()
                tags = body.get("tags") or []
                if not isinstance(tags, list):
                    self._send_json(400, {"ok": False, "message": "tags 必须是数组"})
                    return
                try:
                    result = STORE.set_piece_tags(int(pid), tags, replace=bool(body.get("replace", True)))
                except Exception:
                    self._send_json(500, {"ok": False, "message": "保存失败"})
                    return
                self._send_json(200, {"ok": True, "tags": result})
                return
        if path.startswith(prefix) and path.endswith("/tag-suggest"):
            mid = path[len(prefix) : -len("/tag-suggest")]
            if mid.isdigit():
                body = self._read_json_body()
                item = STORE.get_enriched(int(mid))
                if not item:
                    self._send_json(404, {"ok": False, "message": "找不到这条资源"})
                    return
                try:
                    result = tagging.suggest(item, STORE.pieces(int(mid), with_text=True))
                except tagging.TaggingError as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                    return
                except Exception:
                    self._send_json(500, {"ok": False, "message": "打标失败，请稍后重试"})
                    return
                if body.get("apply") and result["tags"]:
                    try:
                        tagging.apply(STORE, int(mid), result["tags"])
                    except Exception:
                        self._send_json(500, {"ok": False, "message": "标签写入失败"})
                        return
                self._send_json(200, {"ok": True, **result})
                return
        if path.startswith(prefix) and path.endswith("/import"):
            mid = path[len(prefix) : -len("/import")]
            if mid.isdigit():
                body = self._read_json_body()
                confirm = bool(body.get("confirm"))
                try:
                    result = import_item(STORE, int(mid), confirm_again=confirm)
                    typ = result.get("type")
                    message = f"已导入为「{result['dest_name']}」。请刷新酒馆页面后再查看。"
                    if typ == "persona":
                        message = (
                            f"已复制为「{result['dest_name']}」到酒馆的「User Avatars」。"
                            "请到酒馆里自行添加或选择这张面具，当前设置不会被改动。"
                        )
                    result["message"] = message
                    self._send_json(200, result)
                except ImportError_ as exc:
                    text = str(exc)
                    if text == "already_imported":
                        self._send_json(
                            409,
                            {
                                "ok": False,
                                "code": "already_imported",
                                "message": "这条资源已经导入过。如需再导入一次，请确认后使用新文件名。",
                            },
                        )
                    else:
                        self._send_json(400, {"ok": False, "message": text})
                except Exception:
                    self._send_json(500, {"ok": False, "message": "导入失败，请稍后重试。"})
                return
        if path.startswith(prefix) and path.endswith("/import-set"):
            mid = path[len(prefix) : -len("/import-set")]
            if mid.isdigit():
                body = self._read_json_body()
                confirm = bool(body.get("confirm"))
                try:
                    result = import_set(STORE, int(mid), confirm_again=confirm)
                    self._send_json(200, result)
                except ImportError_ as exc:
                    self._send_json(400, {"ok": False, "message": str(exc)})
                except Exception:
                    self._send_json(500, {"ok": False, "message": "应用失败，请稍后重试。"})
                return
        if path.startswith(prefix) and path.endswith("/delete"):
            mid = path[len(prefix) : -len("/delete")]
            if mid.isdigit():
                try:
                    result = STORE.delete_item(int(mid))
                except Exception:
                    self._send_json(500, {"ok": False, "message": "删除失败，请稍后重试。"})
                    return
                if not result:
                    self._send_json(404, {"ok": False, "message": "找不到这条资源"})
                    return
                message = "已删除；文件已移入回收站 .trash/。"
                if not result.get("moved_to"):
                    message = "已删除（这条没有库内文件可移动）。"
                self._send_json(200, {"ok": True, "deleted": result, "message": message})
                return
        if path.startswith(prefix):
            mid = path[len(prefix) :]
            if mid.isdigit():
                body = self._read_json_body()
                try:
                    item = STORE.apply_metadata(int(mid), body, source="user")
                    if not item:
                        self._send_json(404, {"ok": False, "message": "找不到这条资源"})
                        return
                    self._send_json(200, {"ok": True, "item": public_item(item, enrich=False), "message": "已保存。"})
                except Exception:
                    self._send_json(500, {"ok": False, "message": "保存失败，请稍后重试。"})
                return
        self._send_json(404, {"ok": False, "message": "Not found"})


def main() -> None:
    if LISTEN_HOST not in {"127.0.0.1", "localhost", "::1"}:
        print("Refusing to listen on a non-loopback address", file=sys.stderr)
        sys.exit(2)
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), LibHandler)
    print(f"tavern-lib listening on {LISTEN_HOST}:{LISTEN_PORT}{PREFIX}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()