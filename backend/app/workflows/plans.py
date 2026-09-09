"""Recognising the one workflow Mai supports, and planning it.

Deterministic throughout. A phrase table decides whether a message is a
workflow request, and a template decides what the plan is. No model is asked
anything here -- which is what makes `WorkflowPlan` application state rather
than model output, and therefore something authorization can be built on.

**There is exactly one plan shape.** `research -> synthesise -> artifact`. A
message cannot produce a different sequence, a longer one, or one naming a
tool this module does not already name, because the only thing a match
produces is arguments to that one template. Adding a second workflow means
editing this file.

The Stage 4D lesson applies directly: a phrase broad enough to catch a
paraphrase is broad enough to catch a mention. "Tell me about researching and
writing documents" must not plan anything, so the table is narrow and every
phrase requires both halves of the composite request.
"""

import re
from typing import List, Optional, Tuple

from app.core.logging import get_logger
from app.integrations.search import MAX_QUERY_CHARS, normalise_query
from app.workflows.limits import MAX_ARTIFACT_NAME_CHARS, MAX_REQUEST_CHARS
from app.workflows.schemas import StepKind, WorkflowPlan, WorkflowStep

logger = get_logger(__name__)

#: Verbs that open a research request, as an alternation fragment.
_RESEARCH_VERB = r"(?:research|look\s+up|find\s+out\s+about|search(?:\s+the\s+web)?(?:\s+for)?)"

#: Verbs that open an artifact request.
_ARTIFACT_VERB = r"(?:create|write|make|save|generate|produce)"

#: What the artifact may be called in the request. Deliberately concrete
#: nouns: "make me something" plans nothing.
_ARTIFACT_NOUN = r"(?:summary|report|document|doc|file|note|notes|write-?up)"

#: The composite pattern. Both halves are required, in this order, joined by
#: an explicit conjunction.
#:
#: Requiring the conjunction is what keeps "research groq" and "write a note"
#: from combining across a sentence boundary into a workflow neither of them
#: asked for.
_COMPOSITE = re.compile(
    # Two characters, not three: "research Go and write a note" and
    # "research AI and write a report" are both ordinary requests, and a
    # three-character floor silently refused them.
    rf"\b{_RESEARCH_VERB}\s+(?P<query>.{{2,400}}?)"
    # Longest conjunction first. With `and` before `and then`, the shorter
    # alternative matches, the artifact verb does not follow it, and the
    # engine backtracks by *extending the query* -- leaving "and" on the end
    # of what gets searched for.
    rf"[,\s]+(?:and\s+then|and|then)\s+"
    rf"{_ARTIFACT_VERB}\s+(?:me\s+)?(?:an?\s+|the\s+)?"
    rf"(?:(?P<adjective>short|brief|detailed|quick)\s+)?"
    rf"{_ARTIFACT_NOUN}\b"
    rf"(?P<tail>.{{0,120}})",
    re.IGNORECASE | re.DOTALL,
)

#: An explicit filename in the tail: `called x.txt`, `named x`, `as x.txt`.
_EXPLICIT_NAME = re.compile(
    r"\b(?:called|named|titled|as)\s+[\"']?(?P<name>[A-Za-z0-9 ._-]{1,80})[\"']?",
    re.IGNORECASE,
)

#: Characters permitted in a derived filename. Everything else becomes a dash.
_UNSAFE_NAME = re.compile(r"[^a-z0-9]+")

ARTIFACT_EXTENSION = ".txt"
DEFAULT_ARTIFACT_NAME = "summary"


def find_plan(message: str) -> Optional[WorkflowPlan]:
    """Plan a workflow for this message, or return None. Never raises.

    None is the overwhelmingly common answer, and it is the safe one: a
    message that does not clearly ask for both halves is not a workflow.
    """
    if not message or len(message) > MAX_REQUEST_CHARS:
        return None

    match = _COMPOSITE.search(message)
    if match is None:
        return None

    # The subject comes from the same recogniser the plain research path uses.
    # Stage 4F-F.1 unified them: two extraction implementations would drift,
    # and a workflow searching for something subtly different from what a bare
    # request would search for is exactly the kind of divergence nobody
    # notices until the results are wrong.
    #
    # The composite regex still decides *whether* this is a workflow -- it
    # alone knows about the artifact half -- and hands the research half over
    # for the subject.
    from app.research.language import recognise

    research_half = f"research {match.group('query') or ''}"
    recognition = recognise(research_half)

    query = recognition.query
    if not query:
        # Recognised as a workflow whose research subject cannot be read.
        # Refuse rather than search for nothing, exactly as before.
        return None

    try:
        query = normalise_query(query)
    except ValueError:
        return None

    name = _artifact_name(match.group("tail") or "", query)

    return WorkflowPlan(
        request=message[:MAX_REQUEST_CHARS],
        steps=(
            WorkflowStep(
                index=0,
                kind=StepKind.RESEARCH,
                arguments={"query": query[:MAX_QUERY_CHARS]},
            ),
            WorkflowStep(
                index=1,
                kind=StepKind.SYNTHESISE,
                depends_on=(0,),
            ),
            WorkflowStep(
                index=2,
                kind=StepKind.ARTIFACT,
                depends_on=(1,),
                # The path is fixed *now*, at planning time, and is what the
                # user is shown and approves. Nothing downstream may change
                # it -- in particular, nothing a search result says.
                arguments={"path": name},
            ),
        ),
    )


def _artifact_name(tail: str, query: str) -> str:
    """A workspace-relative filename. Always safe, always `.txt`.

    Two sources, in order: a name the user gave, or one derived from the
    query. Both are reduced to the same narrow character set, so neither can
    produce a path -- `../`, a leading slash, a drive letter and a null byte
    are all impossible to express in the output alphabet.

    That is belt and braces. `create_text_file` resolves every path inside the
    workspace and would refuse an escape anyway; this makes the escape
    unrepresentable rather than merely refused.
    """
    explicit = _EXPLICIT_NAME.search(tail or "")
    raw = explicit.group("name") if explicit else query

    slug = _UNSAFE_NAME.sub("-", (raw or "").lower()).strip("-")
    slug = slug[:MAX_ARTIFACT_NAME_CHARS].strip("-") or DEFAULT_ARTIFACT_NAME

    if slug.endswith("-txt"):
        slug = slug[: -len("-txt")] or DEFAULT_ARTIFACT_NAME

    return f"{slug}{ARTIFACT_EXTENSION}"


def known_trigger_shapes() -> Tuple[str, ...]:
    """The patterns this module recognises. For tests and for auditing."""
    return (_COMPOSITE.pattern,)


__all__ = [
    "ARTIFACT_EXTENSION",
    "DEFAULT_ARTIFACT_NAME",
    "find_plan",
    "known_trigger_shapes",
]
