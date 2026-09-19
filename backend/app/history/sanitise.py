"""Credential scrubbing for imported content.

An export is years of chat transcripts. Somewhere in them there is a decent
chance of an API key, a connection string with a password, or a bearer token
the user pasted while debugging something. Copying those verbatim into new
database tables would not be "preserving history" -- it would be minting a
fresh secret-at-rest liability in a system that has spent nine stages keeping
credentials out of its own storage.

So content is scrubbed **on the way in**, before the first `INSERT`. The
archive holds the masked form; the original never becomes a row.

The pattern vocabulary is `app.core.logging`'s, reused rather than re-written.
A second list would drift from the first, and the first is the one that has
been maintained for nine stages. What this module adds is the *count*: the log
redactor only needs the masked string, whereas an import needs to tell the
user how many secrets it found without ever showing one.
"""

from typing import NamedTuple

from app.core.logging import _REDACTIONS

#: The shared vocabulary, named here so a reader can see the dependency and a
#: test can assert the two never diverge.
REDACTION_PATTERNS = _REDACTIONS


class Scrubbed(NamedTuple):
    """Masked text, and how much was masked.

    `count` is the only thing that ever reaches a log line or an API response.
    Neither the secret nor its offset is reported: an offset plus the
    surrounding archived text would reconstruct most of it.
    """

    text: str
    count: int


def scrub(text: str) -> Scrubbed:
    """Mask every credential shape in `text` and count the substitutions."""
    if not text:
        return Scrubbed(text=text, count=0)

    total = 0
    for pattern, replacement in REDACTION_PATTERNS:
        replaced = pattern.sub(replacement, text)
        if replaced != text:
            # Counted only when the text actually changed. `subn`'s own count
            # is the wrong number here: masked output can still match the rule
            # that produced it -- `postgres://u:***@h` matches the
            # user:password rule again and "substitutes" it to the identical
            # string -- so a second pass over clean text would report finding
            # secrets in it. A redaction that changes nothing is not a
            # redaction, and this count is shown to the user.
            total += len(pattern.findall(text))
            text = replaced
    return Scrubbed(text=text, count=total)


def contains_secret(text: str) -> bool:
    """Whether scrubbing would still change this text.

    Deliberately *not* "does any pattern match", which is the obvious spelling
    and is wrong. Masked output can still match the pattern that produced it:
    the URL-credential rule turns `postgres://u:pw@h` into `postgres://u:***@h`,
    and `u:***` matches `user:password` again. A naive `search()` therefore
    reports a secret in text that has already been cleaned, and every
    "nothing survived scrubbing" assertion becomes unfalsifiable noise.

    Asking whether another pass would change anything answers the question
    that actually matters, and it holds the masking to being idempotent --
    which a test pins.
    """
    if not text:
        return False
    return scrub(text).text != text


__all__ = ["REDACTION_PATTERNS", "Scrubbed", "contains_secret", "scrub"]
