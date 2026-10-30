"""Stage 7A: link-code generation, normalisation, shape and hashing."""

import hashlib
import re

import pytest

from app.identity.constants import LINK_CODE_ALPHABET, LINK_CODE_LENGTH
from app.identity.link_codes import (
    code_sha256,
    generate_code,
    is_well_formed,
    normalise_code,
)


def test_the_alphabet_has_32_unambiguous_symbols_and_codes_are_16_long() -> None:
    assert len(LINK_CODE_ALPHABET) == 32
    assert len(set(LINK_CODE_ALPHABET)) == 32
    for excluded in "IO01":
        assert excluded not in LINK_CODE_ALPHABET
    # Upper-case letters and digits only.
    assert re.fullmatch(r"[A-Z2-9]+", LINK_CODE_ALPHABET)
    assert LINK_CODE_LENGTH == 16
    assert len(generate_code()) == 16


@pytest.mark.parametrize(
    "code",
    [
        "ABCDEFGHJKLMNPQR",
        "2345678923456789",
        "ZZZZZZZZZZZZZZZZ",
        "  ABCDEFGHJKLMNPQR  ",
        "\tABCDEFGHJKLMNPQR\n",
    ],
)
def test_well_formed_codes(code: str) -> None:
    assert is_well_formed(code) is True


@pytest.mark.parametrize(
    "code",
    [
        "",
        "ABCDEFGHJKLMNPQ",  # 15
        "ABCDEFGHJKLMNPQRS",  # 17
        "ABCDEFGHJKLMNPQI",  # I
        "ABCDEFGHJKLMNPQO",  # O
        "ABCDEFGHJKLMNPQ0",  # 0
        "ABCDEFGHJKLMNPQ1",  # 1
        "ABCDEFGH JKLMNPQ",  # inner space
        "ABCDEFGHJKLMNPQ-",
        "ABCDEFGHJKLMNPQR\nABCDEFGHJKLMNPQR",
        "ＡBCDEFGHJKLMNPQR",  # full-width A
    ],
)
def test_malformed_codes(code: str) -> None:
    assert is_well_formed(code) is False


def test_non_string_input_is_not_well_formed() -> None:
    assert is_well_formed(None) is False  # type: ignore[arg-type]
    assert is_well_formed(1234567890123456) is False  # type: ignore[arg-type]


def test_lower_case_input_is_normalised() -> None:
    assert normalise_code("  abcdefghjkLMNPQR ") == "ABCDEFGHJKLMNPQR"
    assert is_well_formed("abcdefghjkmnpqrs") is True
    assert code_sha256("abcdefghjkmnpqrs") == code_sha256("ABCDEFGHJKMNPQRS")
    assert code_sha256(" ABCDEFGHJKMNPQRS\n") == code_sha256("ABCDEFGHJKMNPQRS")


@pytest.mark.parametrize(
    "aliasing",
    [
        "ABCDEFGHJKMNPQß",  # upper-cases to ...SS, 16 symbols
        "ABCDEFGHJKMNPQRſ",  # long s upper-cases to S
        "abcdefghjkmnpqrſ",
        "ﬀABCDEFGHJKMNPQR",  # ligature ff upper-cases to FF
    ],
)
def test_non_ascii_never_aliases_an_alphabet_code(aliasing: str) -> None:
    assert aliasing.upper() != aliasing.strip()  # each would alias if upper-cased
    assert is_well_formed(aliasing) is False
    normalised = normalise_code(aliasing)
    assert normalised == aliasing.strip()
    assert not normalised.isascii()
    assert code_sha256(aliasing) != code_sha256("ABCDEFGHJKMNPQSS")
    assert code_sha256(aliasing) != code_sha256("ABCDEFGHJKMNPQRS")


def test_ascii_upper_casing_is_unchanged() -> None:
    assert normalise_code(" abcdefghjkmnpqrs\t") == "ABCDEFGHJKMNPQRS"
    assert normalise_code("ABCDEFGHJKMNPQßS") == "ABCDEFGHJKMNPQßS"
    # Unicode whitespace around an ASCII code is only stripped.
    assert normalise_code(" abcdefghjkmnpqrs ") == "ABCDEFGHJKMNPQRS"


def test_the_hash_is_stable_64_lower_hex_and_the_sha256_of_the_normalised_code() -> None:
    code = "ABCDEFGHJKMNPQRS"
    digest = code_sha256(code)
    assert digest == code_sha256(code)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hashlib.sha256(b"ABCDEFGHJKMNPQRS").hexdigest()
    assert code_sha256("ABCDEFGHJKMNPQRT") != digest


def test_a_thousand_generated_codes_are_well_formed_and_distinct() -> None:
    codes = [generate_code() for _ in range(1000)]
    assert all(len(code) == LINK_CODE_LENGTH for code in codes)
    assert all(set(code) <= set(LINK_CODE_ALPHABET) for code in codes)
    assert all(is_well_formed(code) for code in codes)
    assert len(set(codes)) == 1000
    # Every symbol of the alphabet shows up across 16000 draws.
    assert set("".join(codes)) == set(LINK_CODE_ALPHABET)
