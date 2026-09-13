"""Hard bounds on what a workflow may be.

Constants in application code. None is read from configuration, none is
derived from model output, and none is a parameter -- so there is no value a
plan, a message or a search result can set to make a workflow larger.

The numbers are deliberately small. Stage 4F-E composes three steps; ten is
already far more headroom than the one supported plan needs, and a limit that
is never approached is doing its job.
"""

#: Most steps any workflow may contain.
MAX_STEPS = 10

#: Most dependency edges across the whole plan.
MAX_DEPENDENCY_EDGES = 20

#: Deepest chain of dependent steps.
#:
#: With `MAX_STEPS` at 10 a chain cannot exceed 10 anyway, so this is not
#: currently reachable. It is stated because depth and count are different
#: properties, and a future plan shape with parallel branches could hit one
#: without approaching the other.
MAX_DEPTH = 10

#: A workflow may not be planned from a message longer than this.
MAX_REQUEST_CHARS = 2000

#: Longest artifact filename a plan may name, before the extension.
MAX_ARTIFACT_NAME_CHARS = 60

#: Longest synthesis Mai will write into an artifact.
#:
#: Below `create_text_file`'s own 100,000-character bound, deliberately: the
#: executor's limit is the wall, and this is the workflow declining to go
#: anywhere near it.
MAX_ARTIFACT_CONTENT_CHARS = 20_000


# --- Composition bounds (Stage 4H) ------------------------------------------
#
# Per-capability ceilings on one composition. Every one is a constant here:
# none is read from configuration, derived from model output, or influenced by
# user text, memory, calendar content or web results. A plan that exceeds any
# of them is refused outright rather than trimmed -- silently reducing a plan
# would run something the user did not ask for and was not shown.

#: Calendar reads one composition may perform.
#:
#: One. A briefing looks at one window; a second lookup would mean either a
#: second window nobody approved or the same window twice.
MAX_CALENDAR_LOOKUPS = 1

#: Web searches one composition may perform.
MAX_RESEARCH_QUERIES = 1

#: Model generations one composition may cause.
#:
#: One, and it is the turn's own synthesis -- the same call the chat path was
#: already making. Composition adds no model call of its own, which is what
#: keeps a composition from becoming a loop that reasons about its own output.
MAX_MODEL_CALLS = 1

#: Files one composition may write.
MAX_ARTIFACT_OPERATIONS = 1

#: Total operations that leave this process, across every capability.
#:
#: Deliberately smaller than the sum of the per-capability limits: it is the
#: ceiling that holds even if a future plan shape combines them differently.
MAX_EXTERNAL_OPERATIONS = 2

#: Longest research subject a composition may carry into a query.
MAX_SUBJECT_CHARS = 120


__all__ = [
    "MAX_ARTIFACT_CONTENT_CHARS",
    "MAX_ARTIFACT_OPERATIONS",
    "MAX_CALENDAR_LOOKUPS",
    "MAX_EXTERNAL_OPERATIONS",
    "MAX_MODEL_CALLS",
    "MAX_RESEARCH_QUERIES",
    "MAX_SUBJECT_CHARS",
    "MAX_ARTIFACT_NAME_CHARS",
    "MAX_DEPENDENCY_EDGES",
    "MAX_DEPTH",
    "MAX_REQUEST_CHARS",
    "MAX_STEPS",
]
