"""Stage 5D.2: what the user is talking about, resolved from their own words.

The failure this closes was reproduced live in the Stage 5D.0 audit:

    USER: Is Fable better or Asta?
    USER: Search up the net and let me know.
    MAI:  I can search the web for **let me know** ...

The audit found that Mai has two context regimes. The prose path -- the chat
prompt's window and the intent classifier's -- sees prior turns and resolves
references correctly. The *routing* path, which decides what to actually
search for, is a pure function of the current message. So the subject was
regex-extracted from "search up the net and let me know" and came out as
"let me know". The context was not missing; it was computed elsewhere and
discarded.

### The security boundary, which shapes the whole module

The obvious fix -- hand recent conversation to the research recogniser -- is
not acceptable, and the audit said why. Assistant messages can carry
summarised web pages, email bodies and calendar titles. If any of that could
establish what gets searched for, a malicious calendar invitation would be
able to steer an outbound query carrying personal data off-box.

So the input type is `UserTurn`, and there is no constructor here that takes a
conversation. The caller filters by stored role and passes only what the user
typed. That makes the boundary visible in the signature rather than buried in
a filter this module could forget to apply:

    resolve(current_message, user_turns)      not      resolve(conversation)

### Deterministic, and local

No model call, no network, no new dependency. Resolution runs on every turn
that needs it, and a per-turn provider round trip would be both a latency cost
and a second place for the provider to influence routing. A structural test
asserts this module imports nothing that can reach either.

### What it does not do

It does not decide whether Mai may act. A resolved subject means "this appears
to be what the user is asking about" and nothing more -- proposal, consent and
execution gates are entirely unchanged and still mandatory. It also refuses to
guess: where a reference has no safe antecedent, it resolves to nothing and
the caller asks, which is what already happens today for "look this up".
"""

import enum
import re
from typing import List, NamedTuple, Optional, Sequence, Tuple

#: How many prior user turns are inspected. Bounded so a long conversation
#: costs the same as a short one -- resolution runs on the request path.
MAX_USER_TURNS = 6

#: Longest resolved subject. Below the search layer's own bound, so a query is
#: shortened here rather than truncated there.
MAX_SUBJECT_CHARS = 200

#: Most entities tracked from one turn, for ordinal references.
MAX_ENTITIES = 8


class ResolutionSource(str, enum.Enum):
    """Where a resolved subject came from.

    A closed vocabulary, recorded on every resolution. It exists for auditing
    and explainability: "why did Mai search for that?" must have an answer
    that is not "the model decided".
    """

    #: The current message carried its own subject; nothing was inherited.
    CURRENT_MESSAGE = "current_message"
    #: Taken from the most recent user turn that stated a subject.
    RECENT_USER_CONTEXT = "recent_user_context"
    #: Taken from a question the user asked that was never answered.
    PENDING_USER_QUESTION = "pending_user_question"
    #: The user named the topic again themselves ("going back to Fable").
    EXPLICIT_USER_REFERENCE = "explicit_user_reference"
    #: Nothing safe to resolve to. The caller asks.
    UNRESOLVED = "unresolved"


class Ambiguity(str, enum.Enum):
    """Whether resolution succeeded, and if not, why not."""

    #: Nothing needed resolving.
    NOT_REQUIRED = "not_required"
    RESOLVED = "resolved"
    #: Several equally plausible antecedents. Deliberately not a guess.
    AMBIGUOUS = "ambiguous"
    #: A reference with no antecedent at all.
    NO_ANTECEDENT = "no_antecedent"


class UserTurn(NamedTuple):
    """One message the **user** typed.

    The type is the boundary. Nothing else may be constructed into one, and
    the caller builds them from stored `role == user` rows -- never by
    inspecting text to guess who wrote it.
    """

    content: str


class Subject(NamedTuple):
    """What one user turn is about."""

    text: str
    #: The individual things named, for ordinal references ("the second one").
    entities: Tuple[str, ...] = ()


class ResolvedTurn(NamedTuple):
    """The typed result. Never a free-form context blob.

    `subject` is empty unless `ambiguity is RESOLVED`; a caller that reads it
    without checking gets nothing rather than a guess.
    """

    subject: str = ""
    source: ResolutionSource = ResolutionSource.UNRESOLVED
    ambiguity: Ambiguity = Ambiguity.NOT_REQUIRED
    entities: Tuple[str, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.ambiguity is Ambiguity.RESOLVED and bool(self.subject)


# --- Vocabulary ---------------------------------------------------------------
#
# Closed sets throughout. Every one of them is small enough to read, and a word
# not listed fails in the safe direction -- an unrecognised token is treated as
# substantive, which produces a clarification rather than a wrong search.

#: Words that cannot, alone, be what a message is about.
#:
#: Anaphora, the "tell me"/"let me know" politeness family, determiners and the
#: vague nouns people use instead of a topic. A subject made only of these is
#: not a subject, which is the test that "let me know" previously failed.
_NON_SUBSTANTIVE = frozenset({
    # anaphora
    "it", "its", "this", "that", "these", "those", "them", "they", "one",
    "ones", "other", "another", "same", "above", "previous", "former",
    "latter", "both", "either",
    # the politeness / instruction family
    "let", "me", "know", "tell", "show", "give", "share", "say", "please",
    "us", "you", "your", "my", "mine", "i", "we",
    # determiners, conjunctions, prepositions
    "the", "a", "an", "and", "or", "of", "for", "about", "on", "in", "to",
    "up", "with", "from", "at", "by", "as", "then", "also", "too",
    # Pleasantries and confirmations. "yes" matters most: it is how the user
    # approves a research proposal, so without it every confirmed search left
    # "yes" sitting in the window as the most recent thing the user "said",
    # and the next "search it" would have inherited it.
    "hello", "hi", "hey", "thanks", "thank", "ta", "cheers", "ok", "okay",
    "yes", "yep", "yeah", "yup", "sure", "no", "nope", "nah", "cool",
    "great", "nice", "good", "fine", "right", "sorry", "bye", "goodbye",
    "sounds", "perfect", "awesome", "exactly", "correct", "wrong",
    # Bare comparatives. "which one is better" names nothing on its own --
    # the things being compared are in the earlier turn. Without these,
    # "search the web and tell me which one is better" was accepted as a
    # literal query and the comparison was never recovered.
    "better", "worse", "best", "worst", "cheaper", "faster", "slower",
    "bigger", "smaller", "newer", "older", "cheapest", "fastest",
    # Ordinals and positions. "the second one" points *at* a topic; it is not
    # one. Without these, "search the web for the second one" was accepted as
    # a literal query and the reference was never resolved at all.
    "first", "second", "third", "fourth", "fifth", "last", "next", "previous",
    "1st", "2nd", "3rd", "4th", "5th", "former", "latter",
    # the request itself, which is never the topic of the request
    "search", "google", "look", "find", "check", "web", "net", "internet",
    "online", "up", "out", "again", "now", "quickly", "please",
    # vague nouns
    "something", "anything", "stuff", "things", "thing", "info",
    "information", "more", "details", "detail", "results", "result", "news",
    "everything", "some", "any", "all", "what", "which", "who", "how",
    "when", "where", "why", "is", "are", "was", "were", "do", "does", "did",
    "can", "could", "would", "should", "will", "shall", "much", "many",
})

#: Leading scaffolding stripped before reading a subject.
#:
#: Longest first, so "tell me more about" is removed whole rather than leaving
#: "more about". Each is a way of introducing a topic, not part of one.
_LEAD_SCAFFOLDS = (
    "can you please tell me about", "could you please tell me about",
    "can you tell me about", "could you tell me about",
    "please tell me about", "tell me more about", "tell me about",
    "i want to know about", "i'd like to know about",
    "what can you tell me about", "what do you know about",
    "give me information about", "give me info about",
    "search the web for", "search online for", "search the net for",
    "look up", "look for", "google", "research",
    "what is the", "what are the", "what is a", "what is an",
    "what is", "what's", "whats", "who is", "who's", "how much is",
    "how much does", "how many", "compare", "explain",
    "do you know", "any news on", "news about",
)

#: A comparison, in the shapes people write them.
#:
#: Captured as a *pair or list*, because "which one?" and "the second one"
#: need the individual members, not just the phrase.
_COMPARISONS = (
    re.compile(r"^(?:is\s+)?(?P<a>.+?)\s+(?:better|worse|faster|cheaper|bigger)\s+(?:than\s+)?(?:or\s+)?(?P<b>.+)$", re.IGNORECASE),
    re.compile(r"^(?P<a>.+?)\s+(?:vs\.?|versus)\s+(?P<b>.+)$", re.IGNORECASE),
    re.compile(r"^(?P<a>.+?)\s+or\s+(?P<b>.+)$", re.IGNORECASE),
)

#: An explicit return to a named topic. The user has said the word themselves,
#: so this outranks everything inherited.
#:
#: Deliberately narrow. An earlier version also matched a bare "about", which
#: turned every "what about pricing?" into an explicit reference to the topic
#: "pricing" -- reading an ellipsis as a topic switch, and skipping the
#: ordinal handling below on "what about the second one?". A return is a
#: phrase that says *return*, not any use of the word "about".
_EXPLICIT_RETURN = re.compile(
    r"\b(?:back\s+to|going\s+back\s+to|returning\s+to|coming\s+back\s+to|"
    r"on\s+the\s+subject\s+of|as\s+for)\s+(?P<subject>[^,.?!]{2,80})",
    re.IGNORECASE,
)

#: Ordinal references into a list the user gave.
_ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "last": -1,
}
_ORDINAL_REFERENCE = re.compile(
    r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|last)\b",
    re.IGNORECASE,
)

#: "compare them", "which one", "both of them" -- a reference to the whole set.
_SET_REFERENCE = re.compile(
    r"\b(?:which\s+one|which\s+is|compare\s+them|compare\s+those|both|"
    r"either\s+one|between\s+them)\b",
    re.IGNORECASE,
)

#: A word. Internal apostrophes and hyphens are part of it; trailing
#: punctuation is not -- "it." must tokenise as "it", or every sentence-final
#: anaphor escapes the vocabulary and reappears in the resolved subject.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")


def _tokens(text: str) -> List[str]:
    return [token.strip("'-") for token in _WORD.findall(text.lower()) if token.strip("'-")]


def is_substantive(text: str) -> bool:
    """Whether this text names anything at all.

    The test "let me know" fails and "the latest news about OpenAI" passes.
    Used both to decide whether a message carries its own subject and to
    reject a would-be resolution that resolves to nothing.
    """
    return any(token not in _NON_SUBSTANTIVE for token in _tokens(text))


def _strip_scaffold(text: str) -> str:
    """Remove one leading way of introducing a topic."""
    lowered = text.lower().strip()
    for scaffold in _LEAD_SCAFFOLDS:
        if lowered.startswith(scaffold + " "):
            return text[len(scaffold):].strip()
        if lowered == scaffold:
            return ""
    return text.strip()


def _clean(text: str) -> str:
    text = " ".join(text.split()).strip(" ,.?!;:")
    return text[:MAX_SUBJECT_CHARS]


def subject_of(message: str) -> Optional[Subject]:
    """What one user turn is about, or None if it states no topic.

    Deterministic and deliberately shallow. It strips a known way of
    introducing a topic, reads a comparison if one is there, and checks that
    what remains names something. It is not a parser, and it does not need to
    be: the result is shown to the user for confirmation before anything is
    sent anywhere.
    """
    if not message or not message.strip():
        return None

    text = _clean(message)
    if not text:
        return None

    stripped = _clean(_strip_scaffold(text))
    if not stripped or not is_substantive(stripped):
        return None

    for pattern in _COMPARISONS:
        match = pattern.match(stripped)
        if match is None:
            continue
        left = _clean(_strip_scaffold(match.group("a")))
        right = _clean(match.group("b"))
        if is_substantive(left) and is_substantive(right):
            entities = _split_entities(left) + _split_entities(right)
            if len(entities) >= 2:
                return Subject(
                    text=" vs ".join(entities[:MAX_ENTITIES]),
                    entities=tuple(entities[:MAX_ENTITIES]),
                )

    entities = _split_entities(stripped)
    if len(entities) >= 2:
        return Subject(text=stripped, entities=tuple(entities[:MAX_ENTITIES]))
    return Subject(text=stripped, entities=(stripped,))


def _split_entities(text: str) -> List[str]:
    """Break "Fable, Asta and Claude" into its members."""
    parts = re.split(r"\s*,\s*|\s+and\s+|\s+or\s+", text)
    return [
        _clean(part)
        for part in parts
        if _clean(part) and is_substantive(part)
    ][:MAX_ENTITIES]


# --- Resolution ------------------------------------------------------------------


def resolve(
    current_message: str, user_turns: Sequence[UserTurn]
) -> ResolvedTurn:
    """Work out what the current message is about.

    `user_turns` is the bounded window of what the **user** typed, oldest
    first, *excluding* the current message. Nothing else is accepted: see the
    module docstring for why the signature is the security boundary.

    Never raises. Every failure resolves to nothing, and nothing means the
    caller asks -- which is the behaviour that already exists for a research
    request with no readable subject.
    """
    if not current_message or not current_message.strip():
        return ResolvedTurn(ambiguity=Ambiguity.NO_ANTECEDENT)

    window = [turn for turn in user_turns if turn.content.strip()][-MAX_USER_TURNS:]

    # 1. The user named it themselves. Outranks anything inherited, because it
    #    is the one case where there is no inference at all.
    explicit = _EXPLICIT_RETURN.search(current_message)
    if explicit:
        named = _clean(explicit.group("subject"))
        if is_substantive(named):
            return ResolvedTurn(
                subject=named,
                source=ResolutionSource.EXPLICIT_USER_REFERENCE,
                ambiguity=Ambiguity.RESOLVED,
                entities=(named,),
            )

    # There is deliberately no "does the current message have its own
    # subject?" branch here. The research grammar in `app.research.language`
    # already answers that, far better than a second implementation would --
    # it knows "search the web for X" yields X. Duplicating it here produced a
    # resolver that read "search up the net and let me know" as a topic. This
    # module resolves *references*; the caller decides when one needs
    # resolving.
    if not window:
        return ResolvedTurn(ambiguity=Ambiguity.NO_ANTECEDENT)

    # 3. The most recent user turn that stated a subject.
    #
    #    Deliberately *not* "the last entity mentioned". Scanning for entities
    #    would happily reach past an intervening question into an abandoned
    #    topic; taking the most recent subject-bearing user turn means a topic
    #    switch replaces the topic, because the switching turn is itself the
    #    most recent one that states something.
    antecedent: Optional[Subject] = None
    for turn in reversed(window):
        candidate = subject_of(turn.content)
        if candidate is not None:
            antecedent = candidate
            break

    if antecedent is None:
        return ResolvedTurn(ambiguity=Ambiguity.NO_ANTECEDENT)

    # 4. An ordinal reference picks a member out of that turn's list.
    ordinal = _ORDINAL_REFERENCE.search(current_message)
    if ordinal and len(antecedent.entities) >= 2:
        picked = _pick_ordinals(current_message, antecedent.entities)
        if picked is None:
            return ResolvedTurn(
                source=ResolutionSource.RECENT_USER_CONTEXT,
                ambiguity=Ambiguity.AMBIGUOUS,
                entities=antecedent.entities,
            )
        return ResolvedTurn(
            subject=" and ".join(picked),
            source=ResolutionSource.EXPLICIT_USER_REFERENCE,
            ambiguity=Ambiguity.RESOLVED,
            entities=tuple(picked),
        )

    # 5. A reference to the whole set ("which one is better?").
    if _SET_REFERENCE.search(current_message) and len(antecedent.entities) >= 2:
        return ResolvedTurn(
            subject=antecedent.text,
            source=ResolutionSource.PENDING_USER_QUESTION,
            ambiguity=Ambiguity.RESOLVED,
            entities=antecedent.entities,
        )

    # 6. A plain reference inherits the antecedent, qualified by whatever the
    #    current message adds. "What about pricing?" after "Tell me about
    #    Fable" is asking about Fable's pricing, and answering about Fable
    #    alone would drop half of what was asked. Every word in the result is
    #    still one the user typed.
    qualifier = _residual(current_message)
    subject = (
        f"{antecedent.text} {qualifier}" if qualifier else antecedent.text
    )
    return ResolvedTurn(
        subject=_clean(subject),
        source=ResolutionSource.RECENT_USER_CONTEXT,
        ambiguity=Ambiguity.RESOLVED,
        entities=antecedent.entities,
    )


def _residual(message: str) -> str:
    """What the current message adds beyond the reference itself.

    "what about pricing?" -> "pricing";  "search it" -> "".

    Built by dropping the non-substantive vocabulary, so the result is only
    ever words the user wrote. Bounded to a few tokens: a long residual means
    the message had its own subject and should never have reached here.
    """
    kept = [
        token for token in _tokens(message) if token not in _NON_SUBSTANTIVE
    ]
    return " ".join(kept[:6])


def _pick_ordinals(message: str, entities: Sequence[str]) -> Optional[List[str]]:
    """Resolve every ordinal in the message against a list.

    Returns None when any ordinal points outside the list -- "the fourth one"
    of three things is a misunderstanding, and guessing which one was meant is
    exactly the over-reach this stage must not commit.
    """
    picked: List[str] = []
    for word in _ORDINAL_REFERENCE.findall(message):
        index = _ORDINALS.get(word.lower())
        if index is None:
            return None
        if index == -1:
            picked.append(entities[-1])
            continue
        if index >= len(entities):
            return None
        picked.append(entities[index])

    if not picked:
        return None
    # Preserve the order the user listed them in, without duplicates.
    seen = []
    for item in picked:
        if item not in seen:
            seen.append(item)
    return seen


__all__ = [
    "MAX_ENTITIES",
    "MAX_SUBJECT_CHARS",
    "MAX_USER_TURNS",
    "Ambiguity",
    "ResolutionSource",
    "ResolvedTurn",
    "Subject",
    "UserTurn",
    "is_substantive",
    "resolve",
    "subject_of",
]
