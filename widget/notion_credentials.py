"""Read only an explicitly selected Pages credential document, in memory.

No discovery, extraction to disk, logging, network or authentication-file reads.
A configured file is authoritative: failure never selects another credential.
"""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import re
import zipfile
import zlib

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2048
MAX_FRAMES = 4096
MAX_SETTINGS_BYTES = 64 * 1024
DOCUMENT_MEMBER = "Index/Document.iwa"
_INVALID = "Unsupported or invalid credential document."
_TOKEN = re.compile(rb"(?:ntn_|secret_)[A-Za-z0-9_-]+")


def _invalid():
    # Do not include paths, compressed data, text or candidate values.
    raise ValueError(_INVALID)


def decompress_snappy(block: bytes, *, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Decode one bounded raw Snappy block, including overlapping copies."""
    position, expected = 0, 0
    for index in range(5):
        if position >= len(block):
            _invalid()
        value = block[position]
        position += 1
        if index == 4 and value > 15:
            _invalid()
        expected |= (value & 127) << (index * 7)
        if not value & 128:
            break
    else:
        _invalid()
    if expected > limit:
        _invalid()
    output = bytearray()
    while position < len(block):
        tag = block[position]
        position += 1
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length >= 60:
                width = length - 59
                if position + width > len(block):
                    _invalid()
                length = int.from_bytes(block[position:position + width], "little")
                position += width
            length += 1
            if position + length > len(block) or len(output) + length > expected:
                _invalid()
            output.extend(block[position:position + length])
            position += length
        else:
            width = 1 if kind == 1 else 2 if kind == 2 else 4
            if position + width > len(block):
                _invalid()
            if kind == 1:
                length = 4 + ((tag >> 2) & 7)
                offset = ((tag & 224) << 3) | block[position]
            else:
                length = 1 + (tag >> 2)
                offset = int.from_bytes(block[position:position + width], "little")
            position += width
            if not 0 < offset <= len(output) or len(output) + length > expected:
                _invalid()
            # Reading after each append implements Snappy overlap, unlike a
            # slice of the original buffer (e.g. offset 1, copy length 64).
            for _ in range(length):
                output.append(output[-offset])
    if len(output) != expected:
        _invalid()
    return bytes(output)


def read_iwa(data: bytes, *, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Decode Pages' 0 + 24-bit-length framed raw Snappy document stream."""
    position, frames = 0, 0
    output = bytearray()
    while position < len(data):
        if position + 4 > len(data) or frames >= MAX_FRAMES:
            _invalid()
        kind = data[position]
        length = int.from_bytes(data[position + 1:position + 4], "little")
        position += 4
        if kind != 0 or not length or position + length > len(data):
            _invalid()
        block = decompress_snappy(data[position:position + length], limit=limit - len(output))
        output.extend(block)
        position += length
        frames += 1
    return bytes(output)


def pages_token(source: Path | str) -> str | None:
    """Return a unique document-text candidate, or None; expose no failures."""
    try:
        path = Path(source)
        if not path.is_absolute() or path.suffix.lower() != ".pages" or not path.is_file():
            return None
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            return None
        with path.open("rb") as stream:
            archive_bytes = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(archive_bytes) > MAX_ARCHIVE_BYTES:
            return None
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return None
            documents = [item for item in members if item.filename == DOCUMENT_MEMBER]
            if len(documents) != 1:
                return None
            document = documents[0]
            if (document.is_dir() or document.file_size > MAX_DOCUMENT_BYTES or
                    document.compress_size > MAX_DOCUMENT_BYTES):
                return None
            with archive.open(document) as stream:
                data = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(data) > MAX_DOCUMENT_BYTES:
                return None
        raw = read_iwa(data, limit=MAX_DECOMPRESSED_BYTES)
        candidates = set()
        for match in _TOKEN.finditer(raw):
            value = match.group()
            prefix_length = 4 if value.startswith(b"ntn_") else 7
            suffix_length = len(value) - prefix_length
            if suffix_length > 512:
                return None  # Never truncate an oversized candidate.
            if suffix_length >= 20:
                candidates.add(value)
        return next(iter(candidates)).decode("ascii") if len(candidates) == 1 else None
    except (OSError, TypeError, ValueError, UnicodeError, RuntimeError,
            zipfile.BadZipFile, NotImplementedError, EOFError, zlib.error):
        return None


def configured_token(data_dir: Path | str, keychain_provider=None) -> str | None:
    """Use the selected file, or the application's scoped Keychain if unset."""
    try:
        settings_path = Path(data_dir) / "app-settings.json"
        if settings_path.exists():
            if settings_path.stat().st_size > MAX_SETTINGS_BYTES:
                return None
            with settings_path.open("rb") as stream:
                raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                return None
            settings = json.loads(raw)
            if not isinstance(settings, dict):
                return None
        else:
            settings = {}
        source = settings.get("notion_token_source_file")
        if source is not None:
            return pages_token(source) if isinstance(source, str) and source else None
    except (OSError, ValueError, UnicodeError, TypeError):
        return None
    if keychain_provider is None:
        # Lazy import preserves the existing public keychain_token function and
        # its test injection without an import-time circular dependency.
        if __package__:
            from .notion_sync import keychain_token
        else:
            from notion_sync import keychain_token
        keychain_provider = keychain_token
    return keychain_provider()
