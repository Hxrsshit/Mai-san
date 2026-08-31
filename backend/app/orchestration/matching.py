"""Deterministic action identification.

Turns a message into zero or more `ActionCandidate`s by matching
application-authored phrases. **No model is involved.**

Why deterministic rather than a constrained model call
------------------------------------------------------

The specification offers three strategies and asks for "the safest approach
that fits the existing architecture". This is the first, and it is better here
on four counts:

- **It cannot invent a capability.** The matcher emits only names drawn from
  the table below, each of which is asserted to exist in the Stage 4C
  registry at import. A model asked to choose from a list can still return
  something that is not on it; a lookup cannot.
- **It costs nothing.** An ACTION turn already makes three request-path model
  calls (classify, plan, generate). A fourth on the one intent that can lead
  to a real side effect is the worst place to add cost.
- **It is deterministic**, so ordering, deduplication and the test suite are
  all exact rather than probabilistic.
- **It fails toward no action.** A paraphrase the table does not contain
  produces `NO_ACTION`, which is the safe direction: the specification's own
  instruction for an ambiguous request is not to guess a dangerous action.

What it gives up is recall. "Fire off a note to Gautam" will not match, and
that is a documented limitation rather than a defect -- a missed action is a
conversation, and an invented one is an incident.

The table is application code
-----------------------------

Phrases are written here, by hand, in the same spirit as the Stage 4C
catalogue: what exists is decided in code. The message is data being matched
against them, never a source of new entries.
"""

import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.core.logging import get_logger
from app.orchestration.schemas import ActionCandidate
from app.tools.registry import ToolRegistry, get_registry

logger = get_logger(__name__)

#: Longest message the matcher will scan. Matching is linear in the number of
#: phrases, but the message is user-controlled, so it is bounded anyway.
MAX_MATCHED_CHARS = 4000

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def normalise(message: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used only for matching. The original message is never modified, and
    nothing downstream sees this form.
    """
    if not message:
        return ""
    lowered = _PUNCTUATION.sub(" ", message.lower())
    return _WHITESPACE.sub(" ", lowered).strip()


def _echo_arguments(message: str, normalised: str) -> Dict[str, object]:
    """`echo` takes the message text, bounded to its schema.

    The argument is still validated against the tool's own schema afterwards;
    this only has to produce something plausible, not something trusted.
    """
    text = " ".join(message.split())[:500]
    return {"text": text or "(empty)"}


def _no_arguments(message: str, normalised: str) -> Dict[str, object]:
    """Declared future capabilities have no argument schema, so they take none.

    A candidate carrying arguments for a tool that declares no schema is
    refused by Stage 4C's validation -- which is the correct outcome, and one
    the table should not provoke.
    """
    return {}


#: Phrase table. Each entry is (tool name, trigger phrases, argument builder).
#:
#: Phrases are matched as whole words against the normalised message. They are
#: deliberately specific: a phrase broad enough to catch a paraphrase is also
#: broad enough to fire on a passing mention.
_TABLE: List[Tuple[str, Sequence[str], Callable[[str, str], Dict[str, object]]]] = [
    (
        "echo",
        ("echo this", "repeat this back", "echo back", "say this back"),
        _echo_arguments,
    ),
    (
        "future_web_search",
        # Every phrase here is imperative. "web search" was removed after it
        # fired on "tell me about web search engines" -- a noun phrase is a
        # topic, not a request, and the table's own rule says a phrase broad
        # enough to catch a paraphrase is broad enough to catch a mention.
        ("search the web", "search online", "look this up online",
         "google this for me", "run a web search"),
        _no_arguments,
    ),
    (
        "future_generate_document",
        ("generate a document", "create a document", "write this to a document",
         "produce a document"),
        _no_arguments,
    ),
    (
        "future_send_email",
        ("send an email", "send this email", "email this to",
         "send an e mail"),
        _no_arguments,
    ),
    (
        "future_delete_file",
        ("delete the file", "delete this file", "remove the file"),
        _no_arguments,
    ),
]


def _phrase_pattern(phrase: str) -> re.Pattern:
    """Whole-phrase match on word boundaries.

    Not a substring test: "email this to" must not fire inside a longer word,
    and a phrase must not match across a word it does not contain.
    """
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


_COMPILED = [
    (name, tuple((phrase, _phrase_pattern(phrase)) for phrase in phrases), builder)
    for name, phrases, builder in _TABLE
]


def known_trigger_phrases() -> Dict[str, Tuple[str, ...]]:
    """The table, for documentation and tests."""
    return {name: tuple(phrases) for name, phrases, _ in _TABLE}


def validate_table(registry: Optional[ToolRegistry] = None) -> None:
    """Every mapped name must exist in the registry.

    Called at import. A table entry naming a tool that was never registered
    would be a silent dead branch -- and, worse, a place where a name could
    drift out of step with the authoritative catalogue.
    """
    target = registry if registry is not None else get_registry()
    missing = [name for name, _, _ in _TABLE if not target.contains(name)]
    if missing:
        raise RuntimeError(
            f"action matcher references unregistered tools: {sorted(missing)}"
        )


def find_candidates(message: str) -> List[ActionCandidate]:
    """Every capability the table recognises in a message.

    Ordering is deterministic: by where the phrase matched, then by tool name.
    Position first, because a message naming two actions almost always means
    them in the order it names them.

    Returns an empty list far more often than not, which is intended.
    """
    if not message:
        return []

    normalised = normalise(message[:MAX_MATCHED_CHARS])
    if not normalised:
        return []

    found: List[ActionCandidate] = []
    for name, phrases, builder in _COMPILED:
        best: Optional[Tuple[int, str]] = None
        for phrase, pattern in phrases:
            match = pattern.search(normalised)
            if match is not None and (best is None or match.start() < best[0]):
                best = (match.start(), phrase)
        if best is not None:
            found.append(
                ActionCandidate(
                    tool_name=name,
                    arguments=builder(message, normalised),
                    matched_at=best[0],
                    matched_phrase=best[1],
                )
            )

    found.sort(key=lambda candidate: (candidate.matched_at, candidate.tool_name))
    return found


# Fail at import if the table and the registry have drifted apart.
validate_table()


__all__ = [
    "MAX_MATCHED_CHARS",
    "find_candidates",
    "known_trigger_phrases",
    "normalise",
    "validate_table",
]
