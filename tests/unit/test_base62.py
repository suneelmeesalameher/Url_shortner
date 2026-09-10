"""Unit tests for the Base62 codec (app.core.base62) used to turn DB ids into
short codes. Pure function, no I/O - fast, deterministic, no fixtures needed.
"""
import pytest

from app.core import base62


@pytest.mark.parametrize(
    "value",
    [0, 1, 9, 10, 35, 36, 61, 62, 63, 3843, 123456789, 999_999_999_999],
)
def test_encode_then_decode_round_trips(value):
    assert base62.decode(base62.encode(value)) == value


def test_encode_zero_is_the_first_alphabet_character():
    assert base62.encode(0) == "0"


def test_encode_uses_full_62_character_alphabet_before_rolling_over():
    # 61 is the last single-digit value (10 digits + 26 upper + 26 lower - 1);
    # 62 must roll over into two characters, like counting rolling from 9 to 10.
    assert base62.encode(61) == "z"
    assert base62.encode(62) == "10"


def test_encode_is_shorter_than_decimal_for_large_numbers():
    # The whole point of Base62 encoding is compactness relative to the raw id.
    value = 3_521_614_606_208  # 62**7
    assert len(base62.encode(value)) < len(str(value))


def test_encode_rejects_negative_numbers():
    with pytest.raises(ValueError):
        base62.encode(-1)


def test_decode_rejects_characters_outside_the_alphabet():
    with pytest.raises(ValueError):
        base62.decode("not-base62!")


def test_two_different_ids_never_encode_to_the_same_code():
    # Base62 encoding must be injective - this is what "zero collisions by
    # construction" (from the short-code generation strategy) actually relies on.
    codes = {base62.encode(i) for i in range(5000)}
    assert len(codes) == 5000
