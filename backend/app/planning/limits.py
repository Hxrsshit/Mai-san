"""Resource limits for a plan.

Every bound is stated once, here, so the schema and the graph validator agree
and so the numbers can be argued about in one place.

The limits are deliberately conservative. They are not a guess at how complex a
real project is -- they are a guess at how complex a *useful* plan is. Mai is
producing an AI planning representation, not project management software: a
plan a person cannot read in one sitting has failed at its job before any
resource concern arises.

They also serve a second purpose. Plan structure comes from a model, and an
unbounded graph is an unbounded amount of work for every later stage that
walks it. Bounding here means 4C through 4F inherit a graph whose size is
already known.
"""

#: A plan a person can hold in their head. Twenty steps is already a lot for
#: one goal; a goal needing more should be split into sub-goals, which is a
#: later stage's problem rather than a reason to raise this.
MAX_TASKS = 20

#: A task waiting on more than a handful of others is usually mis-decomposed.
MAX_DEPENDENCIES_PER_TASK = 5

#: Total edges. Sits well below the theoretical maximum for MAX_TASKS
#: (20 x 5 = 100), because a plan approaching that density is a graph, not a
#: sequence, and cycle detection and ordering cost grow with it.
MAX_TOTAL_DEPENDENCIES = 60

#: Task identifiers. Short, slug-shaped, and long enough to be readable.
MAX_TASK_ID_LENGTH = 40
MIN_TASK_ID_LENGTH = 1

#: One line, and one paragraph.
MAX_TASK_TITLE_CHARS = 120
MAX_TASK_DESCRIPTION_CHARS = 600
MAX_EXPECTED_OUTCOME_CHARS = 300

#: Per-task completion criteria. More than a few and the task should be split.
MAX_COMPLETION_CRITERIA = 5
MAX_CRITERION_CHARS = 200

#: Plan-level lists. Ten of each is more than any readable plan needs.
MAX_ASSUMPTIONS = 10
MAX_RISKS = 10
MAX_SUCCESS_CRITERIA = 10
MAX_STATEMENT_CHARS = 300

#: Goal fields.
MAX_GOAL_SUMMARY_CHARS = 300
MAX_DESIRED_OUTCOME_CHARS = 500
MAX_SCOPE_CHARS = 500
MAX_CONSTRAINTS = 10

#: The message text handed to the planner. A goal is stated near the start of
#: a request; sending more costs tokens without improving the plan.
MAX_PLANNED_MESSAGE_CHARS = 4000

__all__ = [name for name in dir() if name.isupper()]

# --- Stage 6C: capability binding -------------------------------------------
#
# A step may name a capability it needs. The name is model output and is
# checked against the tool registry before it means anything; these bounds
# stop an oversized name or argument payload reaching that check at all.

#: Longest capability name a step may name. Comfortably past the longest
#: registered tool name, and far short of anything that could carry a payload.
MAX_CAPABILITY_CHARS = 64
#: Most argument keys one step may supply.
MAX_ARGUMENT_KEYS = 20
#: Longest a single argument value may be once stringified.
MAX_ARGUMENT_VALUE_CHARS = 2_000
