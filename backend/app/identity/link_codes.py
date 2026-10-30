"""Stage 7A: link codes. Pure functions: no database, no I/O, no logging.

A link code is 16 characters from a 32-symbol alphabet with no I, O, 0 or 1
(80 bits from `secrets`). It is shown to a person once and stored only as the
SHA-256 of its normalised form, so the plaintext never reaches a row or a log.

Imports only the standard library and the identity constants.
"""

import hashlib
import re
import secrets

from app.identity.constants import LINK_CODE_ALPHABET, LINK_CODE_LENGTH

#: Exactly `LINK_CODE_LENGTH` symbols of the alphabet. Built from the
#: constants, and used with `fullmatch`, so nothing before or after counts.
_WELL_FORMED = re.compile(
    "[" + re.escape(LINK_CODE_ALPHABET) + "]{" + str(LINK_CODE_LENGTH) + "}"
)


def generate_code() -> str:
    """A fresh code: `LINK_CODE_LENGTH` symbols drawn with `secrets.choice`."""
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH))


def normalise_code(text: str) -> str:
    """What a person typed, without surrounding space, upper-cased.

    ASCII-exact: non-ASCII input is returned stripped but otherwise unchanged,
    so it can never be well formed. Upper-casing it first would let
    characters alias alphabet symbols (`ß` becomes `SS`, `ſ` becomes `S`).
    """
    stripped = text.strip()
    if not stripped.isascii():
        return stripped
    return stripped.upper()


def is_well_formed(code: str) -> bool:
    """True only for exactly `LINK_CODE_LENGTH` alphabet symbols, after
    normalising. Says nothing about whether such a code was ever issued."""
    if not isinstance(code, str):
        return False
    return _WELL_FORMED.fullmatch(normalise_code(code)) is not None


def code_sha256(code: str) -> str:
    """The stored form: SHA-256 of the normalised code, 64 lower-case hex."""
    return hashlib.sha256(normalise_code(code).encode("utf-8")).hexdigest()


__all__ = ["code_sha256", "generate_code", "is_well_formed", "normalise_code"]
