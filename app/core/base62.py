"""Base62 codec used to turn numeric DB ids into short, URL-safe codes."""

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)


def encode(number: int) -> str:
    if number < 0:
        raise ValueError("Cannot Base62-encode a negative number")
    if number == 0:
        return _ALPHABET[0]

    digits = []
    while number > 0:
        number, remainder = divmod(number, _BASE)
        digits.append(_ALPHABET[remainder])
    return "".join(reversed(digits))


def decode(code: str) -> int:
    number = 0
    for char in code:
        index = _ALPHABET.find(char)
        if index == -1:
            raise ValueError(f"Invalid Base62 character: {char!r}")
        number = number * _BASE + index
    return number
