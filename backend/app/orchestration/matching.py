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


#: `web_search` has no argument builder.
#:
#: It had one until Stage 4F-F.1, which returned the whole message as the
#: query. Recognition for this tool now goes through the grammar in
#: `app.research.language`, which produces the arguments itself -- so the
#: builder became unreachable, and mutation testing found it by showing that
#: breaking it changed nothing. Dead code that looks load-bearing is worse
#: than no code: the next reader would have edited it expecting an effect.


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
        # Stage 4F-B made this real. It pointed at `future_web_search` --
        # a declaration with no implementation -- for as long as there was no
        # search, and pointing it at the working tool is the whole of this
        # stage's chat integration: a research request is now *identified* as
        # the capability that exists rather than the one that does not.
        #
        # Identification only. Nothing here executes: the candidate still
        # travels the Stage 4C authorization and Stage 4E approval path, and
        # `web_search` requires approval, so no message becomes a request.
        "web_search",
        # Stage 4F-F.1 replaced this tool's literal phrases with a grammar in
        # `app.research.language`, reached through `_WEB_SEARCH_RECOGNISER`
        # below. The five literals matched their exact wordings and nothing
        # else -- "search up the web" defeated "search the web" on a single
        # intervening word -- and a list long enough to cover paraphrase is
        # long enough to fire on mention, which is the failure this table's
        # own comment already recorded.
        #
        # Kept here as documentation of the shapes covered, and asserted
        # against the recogniser by a test so the two cannot drift.
        ("search the web", "search online", "look this up online",
         "google this for me", "run a web search"),
        # Arguments come from the grammar. See `_grammar_candidate`.
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


#: Tools whose recognition is a grammar rather than a phrase list.
#:
#: One entry. The rest of the table stays literal, because the rest of the
#: table describes capabilities that do not exist yet and a literal phrase is
#: the right amount of machinery for that.
_GRAMMAR_MATCHED = frozenset({"web_search"})


#: Words any research grammar family opens with, for ordering only.
_TRIGGER_WORDS = re.compile(
    r"\b(?:search|look|check|research|google|find|what)\b", re.IGNORECASE
)


def _first_trigger_position(normalised: str) -> int:
    """Where the request's verb falls, so ordering matches the message.

    Ordering only. It has no bearing on whether a candidate is produced.
    """
    match = _TRIGGER_WORDS.search(normalised)
    return match.start() if match else 0


def _grammar_candidate(message: str) -> Optional[ActionCandidate]:
    """The `web_search` candidate, from the deterministic recogniser.

    Returns a candidate for a *recognised request* even when its subject
    could not be extracted -- carrying `(empty)`. That is deliberate: the
    research service needs to tell "not a research request" apart from "a
    research request I could not read", because the second deserves a
    clarifying question rather than silence.
    """
    from app.research.language import recognise

    recognition = recognise(message)
    if not recognition.is_request:
        return None

    return ActionCandidate(
        tool_name="web_search",
        arguments={"query": recognition.query or "(empty)"},
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
        if name in _GRAMMAR_MATCHED:
            # Recognised by grammar, not by literal phrase. The candidate is
            # positioned by where its trigger word falls, so ordering with the
            # rest of the table stays "in the order the message names them".
            candidate = _grammar_candidate(message)
            if candidate is not None:
                found.append(
                    ActionCandidate(
                        tool_name=candidate.tool_name,
                        arguments=candidate.arguments,
                        matched_at=_first_trigger_position(normalised),
                        matched_phrase="(grammar)",
                    )
                )
            continue

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
