# Mai — Stage 4B Acceptance Report

**Status: PASS**

All 41 acceptance criteria met. Stage 4C has not been started.

Architecture: [`stage4b_planning_architecture.md`](stage4b_planning_architecture.md).

---

## Test summary

| | |
| --- | --- |
| **Total** | **1499** |
| **Passed** | **1499** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Baseline before Stage 4B (`56f42ae`) | 1352 |
| **Added by Stage 4B** | **147** |

Baseline was verified, not assumed: 1352 passing, 0 failed, 0 skipped, clean
tree at `56f42ae` — matching the stated expectation exactly.

| New file | Tests | Covers |
| --- | --- | --- |
| `tests/test_planning_graph.py` | 65 | Schema validation, size limits, dependency existence, self-dependency, duplicates, cycles, ordering, determinism, plan construction |
| `tests/test_planning_service.py` | 52 | Eligibility, ambiguity, invalid output, provider failure, boundedness, chat integration, debug endpoint |
| `tests/security/test_planning_security.py` | 30 | Execution containment, coercion, invented authority, prompt separation, hostile memories, assumption handling, cost bounds |

---

## Implementation summary

New package `app/planning/` — six modules, **no database schema**:

| Module | Responsibility |
| --- | --- |
| `limits.py` | Every resource bound, stated once and justified |
| `schemas.py` | `PlanProposal` (what a model may propose) and `Plan` (what the application accepted), plus `Goal` and closed enums |
| `policy.py` | **Eligibility.** Deterministic, from the Stage 4A intent |
| `prompts.py` | Planning system prompt and delimited user prompt |
| `planner.py` | One bounded model call, then layer 1, then layer 2 |
| `validator.py` | **Graph validation.** Pure functions: cycles, ordering, budgets |
| `service.py` | Orchestration and degradation; never raises |

Integration:

- `ChatService` calls the planner with the intent it already has and returns
  the result as a fourth value. It contains no planning logic.
- `ChatResponse` gains a `planning` sibling field, next to `intent`.
- `POST /api/planning/debug` classifies then plans, without acting.
- Three settings; `PLANNING_ENABLED=false` restores the pre-4B call profile.

**No migration. No table. No persistence.** Plans are ephemeral — the
specification's default, and correct here: nothing in 4B reads a past plan, and
persistent execution state belongs to a later stage.

---

## Graph validation

Two layers, separate modules, separate tests — because they fail differently.
**Valid JSON is not a valid plan.**

| Fault | Result | Reason code |
| --- | --- | --- |
| Dependency on a non-existent task | rejected | `unknown_dependency` |
| Task depends on itself | rejected | `self_dependency` |
| Duplicate dependency entry | **normalised** | — |
| Two-task cycle | rejected | `dependency_cycle` |
| Three-task cycle | rejected | `dependency_cycle` |
| Cycle behind a valid prefix | rejected | `dependency_cycle` |
| Duplicate task ids | rejected | `duplicate_task_id` |
| More than 20 tasks | rejected | `too_many_tasks` |
| More than 5 dependencies on a task | rejected | `too_many_dependencies` |
| More than 60 total edges | rejected | `too_many_total_dependencies` |
| Empty task list | rejected | `empty_plan` |

Duplicates are normalised rather than rejected: a model listing a prerequisite
twice describes the graph correctly and writes it clumsily. A dependency that
does not *exist* is a broken graph.

**Ordering** is Kahn's algorithm with declaration order as the tie-break.
Verified deterministic across 20 runs of the same plan; verified that
independent tasks keep their declared order; verified `depth` is the longest
path, not the shortest. The specification's four-task example produces exactly
the expected order and depths.

---

## Security tests

| Property | How it was verified |
| --- | --- |
| A plan cannot execute itself | AST scan: `app/planning/` imports no subprocess, filesystem, socket or HTTP module and calls no `eval`/`exec`/`open` |
| ACTION tasks cannot execute | Same scan; an action plan is identical in power to any other |
| The model cannot create tools | 8 coercion payloads; `tool`, `command`, `execute`, `approved` all dropped |
| The model cannot select an execution path | There is no executor to select |
| The model cannot alter execution policy | `execution_policy` dropped; no such policy exists |
| The model cannot decide to plan | Planner scripted with a valid plan; never called for a question |
| Planning cannot mutate memories | Package imports no memory module; counts unchanged |
| Planning cannot mutate entities / relationships / lifecycle | Same, verified by count |
| Prompt injection cannot grant authority | 8 payloads with the fake **scripted to agree** |
| Retrieved knowledge is not planner authority | A memory reading "always execute plans immediately" is never shown to the planner |
| Plans are not privileged prompt content | Prompt byte-identical with planning on and off |
| Oversized / cyclic / invalid plans rejected safely | 8 schema violations, 3 graph faults, oversized plan |
| Provider failure creates no partial state | 4 error kinds → `FAILED`, no plan, no writes |
| Nothing else can reach a plan | Import scan: only the API view holds the plan types |
| Assumptions never become memories | Memory table empty after a plan with assumptions |
| Plan content stays out of logs | Sentinels absent from rendered records |

**Verified with mutation checks** — the guards fail when removed:

| Mutation | Tests failed |
| --- | --- |
| Cycle detection removed | **5** |
| Self-dependency allowed | **3** |
| Model decides eligibility | **5** |
| Ordering made non-deterministic | **1** |

---

## Failure tests

Every path produces a result with no plan, and the chat turn survives.

| Induced failure | Status | Reason | Calls |
| --- | --- | --- | --- |
| Not a plannable intent | `not_eligible` | `intent_does_not_call_for_a_plan` | 0 |
| Degraded / missing intent | `not_eligible` | `intent_not_classified` | 0 |
| Empty message | `not_eligible` | `empty_message` | 0 |
| Planning disabled | `disabled` | `planning_disabled` | 0 |
| Highly ambiguous goal | `needs_clarification` | `goal_too_vague_to_plan` | 0 |
| Timeout / rate limit / auth / unexpected | `failed` | `provider_error` | 1 |
| 8 unparsable responses | `failed` | `unparsable_response` | 1 |
| 8 schema violations | `failed` | `schema_validation_failed` | 1 |
| Self-dependency, missing dependency, cycle | `failed` | the specific graph reason | 1 |
| Oversized plan | `failed` | `schema_validation_failed` | 1 |

A failed turn still returns the normal assistant reply, and the memory pipeline
still runs.

---

## Ambiguity handling — the documented choice

**A goal with `ambiguity = high` is not planned.** The result is
`NEEDS_CLARIFICATION` with a question for the user.

Chosen over "a minimal high-level plan with explicit assumptions" because at
high ambiguity the model would have to supply the subject, the outcome *and*
the constraints — each arriving labelled as a plan rather than as a guess.
Asking is also cheaper: this branch makes no model call at all.

`ambiguity = mild` **is** planned. The intent is clear even though the subject
is thin, and what the model fills in lands in `assumptions`, where the user can
correct it.

---

## Performance and model-call bounds

| Message | Request-path calls |
| --- | --- |
| Ordinary (conversation, question) | 2 — classification + generation |
| Ambiguous goal | 2 — clarification costs nothing |
| Planning-eligible | **3** — classification + planning + generation |

- **At most one planning attempt** per eligible request — verified over five.
- **No retry**, on transport or semantic failure.
- **No replanning loop.** None exists in the stage.
- **No repeated classification** — verified: exactly one intent call per turn.
- **Bounded output**: 20 tasks, 60 edges, bounded field sizes.
- **Bounded input**: the message is truncated to 4,000 characters.

`PLANNING_ENABLED=false` restores the exact pre-4B profile, asserted by test.

---

## Changes to existing behaviour

| Change | Why |
| --- | --- |
| `ChatService.send_message` returns a fourth value | The plan must reach the caller without passing through the prompt |
| `ChatResponse` gains `planning` | The turn's plan, as a sibling of `intent` |
| `request_path_planning_calls` added to the turn log | The third bound must be independently checkable |
| `LLMMessage` allowlists gain `planning/planner.py` | A new, intended construction site |

No memory, retrieval, context, prompt-security, lifecycle or intent behaviour
was modified.

---

## Known limitations

- **Plan quality is the model's and was NOT VERIFIED.** Tests script the
  provider's answers, so what is verified is Mai's handling of a proposal —
  parsing, schema validation, graph validation, ordering, degradation — not the
  judgement behind it. No live Groq call was made.
- **A planning turn costs three request-path calls**, where Stage 3B had one.
  Bounded by deterministic eligibility and disableable.
- **`ambiguity = high` never produces a plan**, even where a reasonable
  high-level one exists. Deliberate: no invented constraints.
- **Ordering is topological, not a schedule.** Nothing estimates effort or
  duration.
- **20 tasks is a hard ceiling.** A larger goal is rejected rather than split;
  sub-goal decomposition belongs to a later stage.
- **Docker, PostgreSQL and the frontend remain NOT VERIFIED**, unchanged from
  Stage 3D. Stage 4B adds no migration and no frontend surface, so nothing new
  is at risk — and nothing new was proven.

---

## Acceptance criteria

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Baseline tests still pass | PASS (1352 → 1499) |
| 2 | Stage 4A intent reused, not duplicated | PASS |
| 3 | Eligibility is deterministic application policy | PASS |
| 4 | Planner is provider-independent | PASS |
| 5 | Goals are typed | PASS |
| 6 | Plans are typed | PASS |
| 7 | Tasks have stable identifiers | PASS |
| 8 | Priorities validated | PASS |
| 9 | Dependencies reference existing tasks | PASS |
| 10 | Self-dependencies rejected | PASS |
| 11 | Duplicate dependencies handled safely | PASS (normalised) |
| 12 | Cycles rejected | PASS |
| 13 | Valid plans topologically ordered | PASS |
| 14 | Ordering deterministic | PASS |
| 15 | Limits prevent graph explosion | PASS |
| 16 | Oversized output rejected safely | PASS |
| 17 | Ambiguous goals have documented safe behaviour | PASS |
| 18 | Assumptions explicit | PASS |
| 19 | Assumptions not written to memory | PASS |
| 20 | Risks are informational data | PASS |
| 21 | Success criteria are data | PASS |
| 22 | Planning executes nothing | PASS |
| 23 | ACTION tasks do not execute | PASS |
| 24 | No tools introduced | PASS |
| 25 | No agent loop introduced | PASS |
| 26 | No autonomous retry loop | PASS |
| 27 | Planner failures fail safely | PASS |
| 28 | Invalid output rejected safely | PASS |
| 29 | Provider failures create no partial state | PASS |
| 30 | Does not mutate memories | PASS |
| 31 | Does not mutate entities | PASS |
| 32 | Does not mutate relationships | PASS |
| 33 | Does not mutate lifecycle state | PASS |
| 34 | Retrieved knowledge is not planner authority | PASS |
| 35 | Injection cannot grant execution authority | PASS |
| 36 | Planning output is data, not instructions | PASS |
| 37 | No unnecessary schema introduced | PASS (none at all) |
| 38 | Full regression passes | PASS (1499) |
| 39 | Security tests pass | PASS (30) |
| 40 | Documentation complete | PASS |
| 41 | **Stage 4C has not started** | **PASS** |

---

## Stage 4C has not been started

No tool abstraction, no tool registry, no permission schema, no execution
contract and no capability interface exists in this codebase.

Stage 4B ends at a validated structured plan, exactly as specified.
