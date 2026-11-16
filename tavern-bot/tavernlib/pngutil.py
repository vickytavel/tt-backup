"""Minimal PNG chunk reader/writer for SillyTavern character cards.

Supports tEXt, zTXt and iTXt chunks named `chara` or `ccv3`.
No image-processing dependencies.
"""

from __future__ import annotations

import base64
import struct
import zlib
from typing import Iterable

PNG_SIG = b"\x89PNG\r\n\x1a\n"
CHARA_KEYS = frozenset({"chara", "ccv3"})


class PngError(ValueError):
    pass


def is_png(data: bytes) -> bool:
    return len(data) >= 8 and data.startswith(PNG_SIG)


def iter_chunks(data: bytes) -> Iterable[tuple[bytes, bytes]]:
    if not is_png(data):
        return
    pos = 8
    length_data = len(data)
    while pos + 8 <= length_data:
        length = int.from_bytes(data[pos : pos + 4], "big")
        typ = data[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if end + 4 > length_data or length < 0:
            break
        yield typ, data[start:end]
        pos = end + 4
        if typ == b"IEND":
            break


def _split_keyword(payload: bytes) -> tuple[str, bytes] | None:
    nul = payload.find(b"\x00")
    if nul <= 0:
        return None
    try:
        keyword = payload[:nul].decode("latin-1")
    except UnicodeDecodeError:
        return None
    return keyword, payload[nul + 1 :]


def _decode_text_payload(typ: bytes, payload: bytes) -> tuple[str, str] | None:
    """Return (keyword, text) if this is a text chunk we can decode."""
    if typ == b"tEXt":
        split = _split_keyword(payload)
        if not split:
            return None
        keyword, rest = split
        return keyword, rest.decode("latin-1", errors="replace")
    if typ == b"zTXt":
        split = _split_keyword(payload)
        if not split:
            return None
        keyword, rest = split
        if not rest:
            return None
        method, compressed = rest[0], rest[1:]
        if method != 0:
            return None
        try:
            text = zlib.decompress(compressed).decode("latin-1", errors="replace")
        except zlib.error:
            return None
        return keyword, text
    if typ == b"iTXt":
        split = _split_keyword(payload)
        if not split:
            return None
        keyword, rest = split
        if len(rest) < 2:
            return None
        comp_flag, comp_method = rest[0], rest[1]
        rest = rest[2:]
        # language tag \0 translated keyword \0 text
        nul = rest.find(b"\x00")
        if nul < 0:
            return None
        rest = rest[nul + 1 :]
        nul = rest.find(b"\x00")
        if nul < 0:
            return None
        text_bytes = rest[nul + 1 :]
        if comp_flag == 1:
            if comp_method != 0:
                return None
            try:
                text_bytes = zlib.decompress(text_bytes)
            except zlib.error:
                return None
        elif comp_flag != 0:
            return None
        return keyword, text_bytes.decode("utf-8", errors="replace")
    return None


def _decode_card_text(text: str) -> str | None:
    """Character card text chunks are historically base64-encoded UTF-8 JSON."""
    raw = text.strip()
    if not raw:
        return None
    # Prefer base64 (SillyTavern / Character Card v2 convention).
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        decoded = base64.b64decode(padded, validate=False)
        as_text = decoded.decode("utf-8")
        if as_text.lstrip().startswith("{") or as_text.lstrip().startswith("["):
            return as_text
    except (ValueError, UnicodeDecodeError):
        pass
    if raw.lstrip().startswith("{"):
        return raw
    return None


def read_chara_json(data: bytes) -> str | None:
    """Extract character card JSON from a PNG buffer. ccv3 takes precedence."""
    if not is_png(data):
        return None
    found: dict[str, str] = {}
    for typ, payload in iter_chunks(data):
        if typ not in (b"tEXt", b"zTXt", b"iTXt"):
            continue
        decoded = _decode_text_payload(typ, payload)
        if not decoded:
            continue
        keyword, text = decoded
        if keyword.lower() not in CHARA_KEYS:
            continue
        card = _decode_card_text(text)
        if card:
            found[keyword.lower()] = card
    return found.get("ccv3") or found.get("chara")


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def make_solid_png(width: int = 96, height: int = 144, rgb: tuple[int, int, int] = (0x30, 0x38, 0x2E)) -> bytes:
    """A tiny valid RGB PNG used as a fallback avatar for JSON-only cards."""
    if width < 1 or height < 1 or width > 2048 or height > 2048:
        raise PngError("invalid dimensions")
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))
    compressed = zlib.compress(raw, 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


def _text_chunk(keyword: str, text: str) -> bytes:
    payload = keyword.encode("latin-1") + b"\x00" + text.encode("latin-1", errors="replace")
    return _chunk(b"tEXt", payload)


def embed_chara(png_bytes: bytes, card_json: str) -> bytes:
    """Return a new PNG with chara (and ccv3 when possible) tEXt chunks, replacing any existing ones."""
    if not is_png(png_bytes):
        raise PngError("not a PNG")
    kept: list[tuple[bytes, bytes]] = []
    for typ, payload in iter_chunks(png_bytes):
        if typ in (b"tEXt", b"zTXt", b"iTXt"):
            decoded = _decode_text_payload(typ, payload)
            if decoded and decoded[0].lower() in CHARA_KEYS:
                continue
        if typ == b"IEND":
            break
        kept.append((typ, payload))

    encoded = base64.b64encode(card_json.encode("utf-8")).decode("ascii")
    extra = [_text_chunk("chara", encoded)]
    try:
        import json

        v3 = json.loads(card_json)
        if isinstance(v3, dict):
            v3 = dict(v3)
            v3["spec"] = "chara_card_v3"
            v3.setdefault("spec_version", "3.0")
            extra.append(_text_chunk("ccv3", base64.b64encode(json.dumps(v3, ensure_ascii=False).encode("utf-8")).decode("ascii")))
    except (ValueError, TypeError):
        pass

    out = bytearray(PNG_SIG)
    for typ, payload in kept:
        out.extend(_chunk(typ, payload))
    for chunk in extra:
        out.extend(chunk)
    out.extend(_chunk(b"IEND", b""))
    return bytes(out)


def wrap_card_as_png(card_json: str, base_png: bytes | None = None) -> bytes:
    """Embed a character card JSON into a PNG (using a solid placeholder if no image is given)."""
    base = base_png if base_png and is_png(base_png) else make_solid_png()
    return embed_chara(base, card_json)
