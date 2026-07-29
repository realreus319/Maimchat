"""Shared text encoding detection for file tools.

The TS implementation preserves common BOM-based encodings and falls back to
best-effort charset detection before using replacement decoding.  Python keeps
the dependency optional so the core tools still work in minimal environments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DetectedEncoding:
    encoding: str
    source: str


_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def detect_text_encoding(raw: bytes, *, default: str = "utf-8") -> DetectedEncoding:
    for marker, encoding in _BOMS:
        if raw.startswith(marker):
            return DetectedEncoding(encoding=encoding, source="bom")

    if _can_decode(raw, default):
        return DetectedEncoding(encoding=default, source="default")

    heuristic = _detect_utf16_without_bom(raw)
    if heuristic is not None:
        return DetectedEncoding(encoding=heuristic, source="heuristic")

    for encoding in _charset_normalizer_candidates(raw):
        if _can_decode(raw, encoding):
            return DetectedEncoding(encoding=encoding, source="charset_normalizer")

    chardet_encoding = _chardet_candidate(raw)
    if chardet_encoding and _can_decode(raw, chardet_encoding):
        return DetectedEncoding(encoding=chardet_encoding, source="chardet")

    single_byte = _single_byte_candidate(raw)
    if single_byte is not None:
        return DetectedEncoding(encoding=single_byte, source="single_byte")

    return DetectedEncoding(encoding=default, source="fallback")


def decode_text_bytes(raw: bytes, *, default: str = "utf-8") -> tuple[str, str]:
    detected = detect_text_encoding(raw, default=default)
    return raw.decode(detected.encoding, errors="replace"), detected.encoding


def _can_decode(raw: bytes, encoding: str) -> bool:
    try:
        raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return False
    return True


def _detect_utf16_without_bom(raw: bytes) -> str | None:
    if len(raw) < 4:
        return None
    sample = raw[: min(len(raw), 4096)]
    even_nuls = sample[0::2].count(0)
    odd_nuls = sample[1::2].count(0)
    pairs = max(1, len(sample) // 2)
    if odd_nuls / pairs > 0.35 and even_nuls / pairs < 0.10:
        return "utf-16-le"
    if even_nuls / pairs > 0.35 and odd_nuls / pairs < 0.10:
        return "utf-16-be"
    return None


def _charset_normalizer_candidates(raw: bytes) -> Iterable[str]:
    try:
        from charset_normalizer import from_bytes
    except Exception:
        return ()

    try:
        match = from_bytes(raw).best()
    except Exception:
        return ()
    if match is None:
        return ()

    encoding = getattr(match, "encoding", None)
    if not isinstance(encoding, str) or not encoding.strip():
        return ()
    encoding = encoding.strip()
    if _is_utf16_family(encoding) and _detect_utf16_without_bom(raw) is None:
        return ()

    chaos = getattr(match, "chaos", None)
    coherence = getattr(match, "coherence", None)
    if isinstance(chaos, (int, float)) and chaos > 0.30:
        if not isinstance(coherence, (int, float)) or coherence < 0.20:
            return ()
    return (encoding,)


def _chardet_candidate(raw: bytes) -> str | None:
    try:
        import chardet
    except Exception:
        return None

    try:
        detected = chardet.detect(raw)
    except Exception:
        return None
    if not isinstance(detected, dict):
        return None
    confidence = detected.get("confidence")
    encoding = detected.get("encoding")
    if not isinstance(confidence, (int, float)) or confidence < 0.50:
        return None
    if not isinstance(encoding, str) or not encoding.strip():
        return None
    encoding = encoding.strip()
    if _is_utf16_family(encoding) and _detect_utf16_without_bom(raw) is None:
        return None
    return encoding


def _is_utf16_family(encoding: str) -> bool:
    normalized = encoding.replace("_", "-").lower()
    return normalized.startswith("utf-16") or normalized.startswith("utf-32")


def _single_byte_candidate(raw: bytes) -> str | None:
    for encoding in ("cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(text):
            return encoding
    return None


def _looks_like_text(text: str) -> bool:
    if not text:
        return True
    printable = 0
    for char in text:
        if char.isprintable() or char in "\r\n\t":
            printable += 1
    return printable / len(text) >= 0.85


__all__ = ["DetectedEncoding", "decode_text_bytes", "detect_text_encoding"]
