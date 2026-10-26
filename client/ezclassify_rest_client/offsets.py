"""UTF-8 byte-offset helpers for document evidence and authored spans."""

from __future__ import annotations


def utf8_byte_offset(text: str, index: int) -> int:
    """Convert a Python character index to a UTF-8 byte offset.

    Raises:
        ValueError: If ``index`` is outside the string.
        UnicodeEncodeError: If ``text`` contains an unpaired surrogate.
    """
    if index < 0 or index > len(text):
        raise ValueError("character index is outside the document")
    text.encode("utf-8", errors="strict")
    return len(text[:index].encode("utf-8", errors="strict"))


def utf8_index(text: str, byte_offset: int) -> int:
    """Convert a UTF-8 byte offset to a Python character index.

    Raises:
        ValueError: If the offset is outside the encoded document or splits a
            multi-byte code point.
        UnicodeEncodeError: If ``text`` contains an unpaired surrogate.
    """
    encoded = text.encode("utf-8", errors="strict")
    if byte_offset < 0 or byte_offset > len(encoded):
        raise ValueError("byte offset is outside the document")
    try:
        prefix = encoded[:byte_offset].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("byte offset splits a UTF-8 code point") from exc
    return len(prefix)
