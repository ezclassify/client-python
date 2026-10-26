"""UTF-8 byte/index conversion contract."""

import pytest

from ezclassify_rest_client import utf8_byte_offset, utf8_index


def test_round_trips_combining_and_non_bmp_offsets() -> None:
    text = "e\u0301😀z"
    for index in range(len(text) + 1):
        byte_offset = utf8_byte_offset(text, index)
        assert utf8_index(text, byte_offset) == index
    assert utf8_byte_offset(text, 3) == 7


def test_rejects_split_code_point_and_out_of_range_offsets() -> None:
    with pytest.raises(ValueError, match="splits"):
        utf8_index("😀", 1)
    with pytest.raises(ValueError, match="outside"):
        utf8_byte_offset("x", 2)


def test_rejects_unpaired_surrogate() -> None:
    with pytest.raises(UnicodeEncodeError):
        utf8_byte_offset("\ud800", 0)
