"""Stage 5A.1 -- does this question need information that may have changed?

Three subjects: what the assessor classifies as needing current information,
what it must leave alone, and where the assessment sits in the routing chain.
The adversarial matrix lives in `tests/security/test_freshness_security.py`.
"""

from datetime import datetime, timezone

import pytest

from app.language.normalise import normalise
from app.orchestration import freshness
from app.orchestration.freshness import (
    FreshnessRequirement,
    FreshnessSource,
    assess,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)


def route(message: str) -> str:
    """Which layer claims this message, in the order the chat turn asks.

    The order *is* the guarantee for personal data, so the test helper walks
    it rather than calling the assessor directly: freshness is last, and
    anything the calendar or mail grammars claim never reaches it.
    """
    from app.orchestration import calendar_language
    from app.orchestration.mail_language import recognise as recognise_mail
    from app.research import language as research_language
    from app.workflows import briefing

    text = normalise(message).text

    calendar = calendar_language.recognise(text, now=NOW)
    if calendar.is_write_request:
        return "calendar_write"
    if calendar.is_readable:
        return "calendar"
    if calendar.needs_clarification:
        return "calendar_clarify"

    mail = recognise_mail(text)
    if mail.is_write_request:
        return "mail_write"
    if mail.is_readable:
        return "mail"

    if briefing.recognise(text, now=NOW) is not None:
        return "briefing"
    if research_language.recognise(text).is_request:
        return "research_explicit"
    if assess(text).wants_web:
        return "research_freshness"
    return "ordinary"


# --- The reported failure ------------------------------------------------------


REPORTED = (
    "what is the latest model launched by ChatGPT and is it better than "
    "the model by Claude?"
)


def test_the_reported_question_no_longer_answers_from_training_data() -> None:
    """The question that prompted the stage.

    It contains no search verb, so every recogniser up to Stage 5B declined
    and the model answered from knowledge that was already months old --
    confidently, and with nothing in the system aware it might be stale.
    """
    assert route(REPORTED) == "research_freshness"

    assessment = assess(REPORTED)
    assert assessment.requirement is FreshnessRequirement.REQUIRED
    assert assessment.source is FreshnessSource.WEB
    # The subject survives: this is what gets searched for.
    assert "ChatGPT" in assessment.subject
    assert "latest" in assessment.subject


# --- Cross-domain positives ----------------------------------------------------


#: Deliberately spread across unrelated subject areas.
#:
#: The implementation contains no list of companies, products or topics, so
#: these pass or fail together -- which is the property being tested. A
#: detector that knew about OpenAI would pass the AI rows and fail the rest.
REQUIRES_CURRENT_INFORMATION = [
    # Technology
    "what is the latest React version?",
    "what is the latest iPhone?",
    "what is the current MacBook lineup?",
    "what is the newest version of Python?",
    "what changed in Node.js recently?",
    # AI
    "what is the latest OpenAI model?",
    "what is the newest Claude model?",
    "what happened in AI today?",
    # Business
    "what happened to Tesla today?",
    "what is the latest news about Nvidia?",
    "who is the current CEO of Nvidia?",
    "what is Netflix announcing this week?",
    # Finance
    "what is Bitcoin trading at right now?",
    "what is the current gold price?",
    "how much is gold?",
    # Public policy
    "what changed in India's tax rules recently?",
    "what did the government announce today?",
    # News and sport
    "what happened in the world today?",
    "who won the match last night?",
    # Products and local
    "what are the latest AirPods?",
    "what is happening in Bangalore this weekend?",
    "what restaurants are open tonight?",
    # Implicit: the predicate is what moves, not the noun
    "what is the weather?",
    "is Tesla hiring software engineers?",
    "is GitHub down?",
]


@pytest.mark.parametrize("message", REQUIRES_CURRENT_INFORMATION)
def test_currentness_questions_require_fresh_information(message) -> None:
    assessment = assess(message)

    assert assessment.requirement is FreshnessRequirement.REQUIRED, message
    assert assessment.source is FreshnessSource.WEB, message
    assert assessment.subject, message


@pytest.mark.parametrize("message", REQUIRES_CURRENT_INFORMATION)
def test_currentness_questions_route_to_research(message) -> None:
    """Through the whole chain, not just the assessor.

    Either research entry point is correct. Stage 4F-F.1's `latest_on` family
    already owns "what's the latest on X", so a handful of these reach the web
    through the explicit grammar rather than through freshness -- and that is
    the right outcome, because what matters is the question getting current
    information, not which door it came through. Asserting the freshness door
    specifically would have been asserting an implementation detail.
    """
    assert route(message) in {"research_freshness", "research_explicit"}, message


def test_the_positive_matrix_spans_many_domains() -> None:
    """A guard against the matrix quietly narrowing to one subject area."""
    assert len(REQUIRES_CURRENT_INFORMATION) >= 20


def test_no_subject_area_is_named_in_the_implementation() -> None:
    """§: domain-agnostic. No company or product list, now or later.

    The detector recognises temporal structure -- properties of English -- so
    it works for entities nobody has thought of. A list would be wrong the day
    after it was written, would grow forever, and would silently fail for
    everything omitted.

    Checked by AST over **string constants**, not by scanning the file. A raw
    text scan flagged the module's own docstring, which explains the stage
    using "what is the latest OpenAI model?" as its example -- prose about a
    company is not a company list, and a test that cannot tell them apart is
    a test that gets silenced.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/orchestration/freshness.py").read_text())

    # Docstrings are documentation; every other string constant is code.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    named = ("openai", "chatgpt", "claude", "anthropic", "nvidia", "tesla",
             "netflix", "iphone", "apple", "bitcoin", "react", "airpods",
             "microsoft", "amazon", "meta", "samsung")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value in docstrings:
            continue
        lowered = node.value.lower()
        for company in named:
            assert company not in lowered, (company, node.value[:60])


# --- Negatives: stable knowledge -------------------------------------------------


STABLE = [
    "what is photosynthesis?",
    "how does TCP work?",
    "what is the capital of France?",
    "explain recursion",
    "what is compound interest?",
    "how does OAuth work?",
    "what is a database index?",
    "write a Python function to reverse a string",
    "help me draft an email",
    "what is Python?",
    "tell me about Apple",
    "who is Elon Musk?",
    "how does Gmail work?",
    "what is the difference between TCP and UDP?",
    "why is the sky blue?",
    "teach me about vectors",
    "summarise this for me",
    "refactor this function",
    "How are you today?",
    "Which LLM provider am I currently using for Mai?",
    "What technology stack am I currently using for Mai?",
]


@pytest.mark.parametrize("message", STABLE)
def test_stable_questions_do_not_require_research(message) -> None:
    """§: the other failure mode. Over-searching is also wrong."""
    assessment = assess(message)

    assert not assessment.wants_web, (message, assessment.reason)


@pytest.mark.parametrize("message", STABLE)
def test_stable_questions_stay_ordinary_turns(message) -> None:
    assert route(message) != "research_freshness", message


def test_a_definitional_shape_is_not_by_itself_stable() -> None:
    """"What is X?" and "what is the latest X?" open identically.

    So the definitional guard cannot mean "stable" on its own -- it is the
    *absence of a recency marker* that means that. Getting this backwards
    would have made every "what is the latest ..." question stale.
    """
    assert not assess("what is Python?").wants_web
    assert assess("what is the latest Python version?").wants_web


# --- Personal data keeps precedence -----------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("what is on my calendar tomorrow?", "calendar"),
        ("what is on my Google Calendar tomorrow?", "calendar"),
        ("check my Google Calendar today", "calendar"),
        ("am I free tomorrow afternoon?", "calendar"),
        ("what meetings do I have tomorrow?", "calendar"),
        ("what are my latest emails?", "mail"),
        ("check my gmail", "mail"),
        ("what emails did I get today?", "mail"),
        ("do I have any unread emails from Netflix?", "mail"),
    ],
)
def test_personal_questions_route_to_their_own_source(message, expected) -> None:
    """§: freshness must not turn a personal question into a web search.

    These are all currentness questions -- "today", "latest", "tomorrow" --
    and none of them is about the world.
    """
    assert route(message) == expected, message


@pytest.mark.parametrize(
    ("message", "source"),
    [
        ("what are my latest emails?", FreshnessSource.MAIL),
        ("my most recent meetings", FreshnessSource.CALENDAR),
        ("what do you know about my project?", FreshnessSource.NONE),
        ("what did I tell you about Mai?", FreshnessSource.NONE),
    ],
)
def test_the_assessor_itself_refuses_personal_scope(message, source) -> None:
    """Defence in depth, since the chain already protects these.

    The recognisers above claim them first, so the assessor is never asked --
    but "never asked" is an ordering property, and orderings get rearranged.
    The assessor declines on its own account too, and names which source
    would have been right.
    """
    assessment = assess(message)

    assert not assessment.wants_web, message
    assert assessment.reason == "personal_scope"
    assert assessment.source is source


def test_a_product_question_about_a_personal_service_reaches_the_web() -> None:
    """§: the distinction that makes source selection about the *domain*.

    "my email" is the user's mailbox; "Gmail" the product is a subject anyone
    can ask a news question about.
    """
    assert route("what is the latest Gmail feature?") == "research_freshness"
    assert route("what changed in Gmail recently?") == "research_freshness"
    assert route("what is the latest calendar app?") == "research_freshness"


def test_an_explicit_search_request_still_takes_the_explicit_path() -> None:
    """Freshness must not intercept what the research grammar already owns."""
    for message in (
        "search the web for the latest news about OpenAI",
        "google Gmail API documentation",
        "research Tesla's quarterly results",
        "look up NASA Artemis III",
    ):
        assert route(message) == "research_explicit", message


# --- Query preservation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "must_contain"),
    [
        ("what is the latest model launched by ChatGPT?", ["latest", "ChatGPT"]),
        ("what is the latest iPhone?", ["latest", "iPhone"]),
        ("what happened with Netflix today?", ["Netflix", "today"]),
        ("what is the newest version of Python?", ["newest", "Python"]),
        ("what changed in Node.js recently?", ["Node.js", "recently"]),
        ("what is the latest React version?", ["latest", "React"]),
        ("who is the current CEO of Nvidia?", ["current", "CEO", "Nvidia"]),
        ("what is happening in Bangalore this weekend?", ["Bangalore", "weekend"]),
    ],
)
def test_the_query_preserves_what_the_user_asked_about(message, must_contain) -> None:
    """§: not "latest", not "model", not "web search".

    The words that make the question answerable are the words a search needs.
    A query of "latest" would return nothing useful and would look, in the
    consent prompt, like Mai had misunderstood.
    """
    subject = assess(message).subject

    for fragment in must_contain:
        assert fragment in subject, (message, subject)


def test_a_domain_name_survives_intact() -> None:
    """§: do not mutate domains or strip meaningful punctuation."""
    assert "Node.js" in assess("what changed in Node.js recently?").subject
    assert "example.com" in assess(
        "what is the latest status of example.com today?"
    ).subject


def test_the_subject_is_bounded() -> None:
    from app.orchestration.freshness import MAX_SUBJECT_CHARS

    assessment = assess("what is the latest " + ("verylongword " * 60) + "today?")
    assert len(assessment.subject) <= MAX_SUBJECT_CHARS


# --- The middle state ----------------------------------------------------------------


def test_a_volatile_role_is_preferred_rather_than_required() -> None:
    """§: the distinction between changing what Mai does and what it says.

    "Who is the CEO of Nvidia?" is answerable and usually right, but it is the
    kind of fact that turns over. Searching unbidden would be over-searching;
    answering as though it were fixed would be the stale-answer failure. The
    middle state is neither.
    """
    assessment = assess("who is the CEO of Nvidia?")

    assert assessment.requirement is FreshnessRequirement.PREFERRED
    assert not assessment.wants_web
    assert not assessment.needs_current_information


def test_the_same_question_with_a_marker_is_required() -> None:
    assessment = assess("who is the current CEO of Nvidia?")

    assert assessment.requirement is FreshnessRequirement.REQUIRED
    assert assessment.wants_web


def test_only_required_routes_anywhere() -> None:
    """PREFERRED changes wording, never routing. A test, not a comment."""
    for requirement in FreshnessRequirement:
        sample = freshness.FreshnessAssessment(
            requirement=requirement,
            source=FreshnessSource.WEB,
            subject="something",
        )
        assert sample.wants_web is (requirement is FreshnessRequirement.REQUIRED)


# --- Typo interaction (Stage 5A) --------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "what is the lates OpenAI model?",
        "what is the curent iphone?",
        "what happened in AI todya?",
        "whats the newst Claude model",
        "what is currenty happening with Tesla?",
    ],
)
def test_typos_still_reach_the_freshness_path(message) -> None:
    """§: Stage 5A's repairs feed the assessor, as they feed every grammar."""
    assert route(message) == "research_freshness", message


def test_the_normaliser_stays_closed_and_verb_free() -> None:
    """§: do not expand into an unrestricted spellchecker.

    Stage 5A.1 added six recency words. They are adjectives and adverbs, under
    the same rule as every other entry -- a term an existing grammar matches
    on -- so a broken *verb* still cannot be repaired into a working one.
    """
    from app.language.normalise import CANONICAL_TERMS, KNOWN_MISSPELLINGS

    assert len(CANONICAL_TERMS) < 60
    triggers = {
        "search", "google", "research", "find", "create", "write", "send",
        "delete", "execute", "approve", "authorise", "authorize", "yes",
    }
    assert not (CANONICAL_TERMS & triggers)
    for target in KNOWN_MISSPELLINGS.values():
        assert target in CANONICAL_TERMS, target


# --- Bounds ------------------------------------------------------------------------------


def test_an_over_long_message_is_not_assessed() -> None:
    from app.orchestration.freshness import MAX_MESSAGE_CHARS

    message = "what is the latest thing " * 200
    assert len(message) > MAX_MESSAGE_CHARS

    assessment = assess(message)
    assert not assessment.wants_web
    assert assessment.reason == "message_too_long"


def test_repeated_freshness_words_produce_one_assessment() -> None:
    """§: multiple signals must not mean multiple searches.

    The assessor returns one value whatever the input, so there is no path
    from "latest newest current recent" to more than one proposal.
    """
    assessment = assess("latest newest current recent latest today now " * 8)

    assert isinstance(assessment, freshness.FreshnessAssessment)
    assert assessment.requirement in set(FreshnessRequirement)


def test_assessment_is_cheap_on_hostile_input() -> None:
    """§: no network, no database, no model call -- and no pathological cost."""
    import time

    hostile = [
        "latest " * 160,
        "what is the " * 80 + "latest?",
        "a" * 999,
        "?" * 999,
        " " * 999,
        "current current current " * 40,
        "".join(chr(0x0400 + (i % 64)) for i in range(900)),
    ]

    started = time.perf_counter()
    for _ in range(20):
        for message in hostile:
            assess(message[:1000])
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"assessment took {elapsed:.1f}s"


# --- Guards that another guard was hiding -----------------------------------
#
# Mutation testing found several checks here that no test could reach, each
# because a different check refused the input first, or because the assertion
# read the constant it was meant to pin. A guard nothing exercises is a guard
# nobody knows is working.


@pytest.mark.parametrize(
    "message",
    [
        "explain what version of Python is installed",
        "explain who the CEO is",
        "explain which version to use",
        "describe how the CEO is chosen",
        "explain how many employees they have",
    ],
)
def test_the_definitional_guard_beats_a_volatile_role(message) -> None:
    """The definitional guard, reached on its own.

    Every other stable case is refused later, by there being no signal at all,
    so deleting this changed nothing. These carry a volatile-role or version
    shape *and* ask for an explanation -- which leaves the definitional guard
    as the only thing between them and a lookup.
    """
    assessment = assess(message)

    assert assessment.reason == "definitional", message
    assert not assessment.wants_web
    assert assessment.requirement is FreshnessRequirement.NOT_REQUIRED


@pytest.mark.parametrize(
    "message",
    [
        "write a Python function using the latest asyncio API",
        "help me draft an email about today's meeting",
        "refactor this to use the newest syntax",
        "debug this and tell me what changed",
    ],
)
def test_the_task_guard_beats_a_recency_marker(message) -> None:
    """The task guard, reached on its own.

    A request to *do* work can mention a recency marker in passing. Without
    this guard, "write a Python function using the latest asyncio API" would
    have proposed a web search instead of writing the function -- and every
    earlier task case had no marker, so nothing noticed the guard was there.
    """
    assessment = assess(message)

    assert assessment.reason == "task_request", message
    assert not assessment.wants_web


@pytest.mark.parametrize("message", ["how much is it?", "how much are they?"])
def test_a_currentness_question_with_no_readable_subject_searches_nothing(
    message,
) -> None:
    """Recognised, unreadable, and therefore not searched.

    "How much is it?" is a price question whose subject is a pronoun. Sending
    the leftovers to a search provider would mean sending a string nobody
    wrote, so the turn stays ordinary and the model asks what "it" is.
    """
    assessment = assess(message)

    assert assessment.reason == "subject_unreadable", message
    assert not assessment.wants_web
    assert assessment.subject == ""


def test_the_subject_bound_is_a_literal_not_the_constant() -> None:
    """Two faults at once, in the earlier version of this test.

    It read `MAX_SUBJECT_CHARS`, so raising the constant moved the assertion
    with it -- and its message was 1065 characters, over `MAX_MESSAGE_CHARS`,
    so the assessor returned `message_too_long` with an empty subject and the
    assertion held no matter what. The message below stays under the message
    cap so the subject cap is what it reaches.
    """
    message = "what is the latest " + ("verylongword " * 55) + "today?"
    assert len(message) < freshness.MAX_MESSAGE_CHARS

    assessment = assess(message)

    assert assessment.reason == "recency_marker"
    assert len(assessment.subject) == 240
    assert len(assessment.subject) <= freshness.MAX_SUBJECT_CHARS


def test_wants_web_requires_the_source_as_well_as_the_requirement() -> None:
    """Both halves, tested separately.

    No assessment the module currently produces is REQUIRED with a non-web
    source, so dropping the source check changed nothing observable. It is
    still the check that will matter the moment a second source exists --
    a REQUIRED calendar question must not be answerable by a web search.
    """
    from app.orchestration.freshness import FreshnessAssessment

    for source in (FreshnessSource.CALENDAR, FreshnessSource.MAIL,
                   FreshnessSource.NONE):
        sample = FreshnessAssessment(
            requirement=FreshnessRequirement.REQUIRED,
            source=source,
            subject="something",
        )
        assert not sample.wants_web, source

    web = FreshnessAssessment(
        requirement=FreshnessRequirement.REQUIRED,
        source=FreshnessSource.WEB,
        subject="something",
    )
    assert web.wants_web


@pytest.mark.parametrize(
    ("message", "claimed_by"),
    [
        ("what meetings do I have today?", "calendar"),
        ("what emails did I get today?", "mail"),
        ("do I have any unread emails today?", "mail"),
        ("what appointments do I have this week?", "calendar"),
    ],
)
def test_a_personal_question_carrying_a_marker_is_claimed_before_freshness(
    message, claimed_by
) -> None:
    """The ordering guard, and why it is load-bearing rather than belt-and-braces.

    These say "today" and name no possessive, so the assessor's own
    personal-scope guard does *not* fire -- it would classify them REQUIRED
    and route them to the web. What stops that is position: the calendar and
    mail recognisers claim them first.

    So the ordering is not a tidy-looking redundancy. It is the only thing
    standing between "what emails did I get today?" and a web search.
    """
    assert route(message) == claimed_by, message

    # The premise: on its own the assessor would have sent these to the web.
    assert assess(message).wants_web, (
        f"{message!r} no longer trips freshness; this test has stopped "
        "testing the ordering"
    )
