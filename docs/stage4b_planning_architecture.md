# Mai — Stage 4B: Goal & Planning Engine

Stage 4A answers *what does the user want?* Stage 4B begins answering *what
would need to happen to achieve it?*

It answers it and stops. There is no tool, no executor, no agent loop and no
replanning. A `Plan` is inert data: a task reading "Send the outreach email" is
a sentence about future work, and nothing in this codebase can act on it.

---

## The architecture in one line

> The model may propose a plan. The application decides whether to ask for one,
> and whether what came back is a plan at all.

```
IntentResult (4A)
      │
      ▼
policy.decide          ── deterministic. The model is never asked.
      │
      ├─ not eligible ──► no model call at all
      │
      ▼
Planner                ── ONE model call, json_mode
      │
      ▼
PlanProposal           ── layer 1: schema validation
      │
      ▼
validate_graph         ── layer 2: graph validation
      │
      ▼
Plan                   ── inert, ordered, immutable
```

Holding a `Plan` is itself the proof: the object exists only for a proposal
that passed both layers.

---

## Planning eligibility

The application decides. This is the Stage 4A authority separation applied one
level up — 4A's policy decides what a classification *implies*, this decides
what an implication *warrants*.

| Intent | Planned? | Why |
| --- | --- | --- |
| `planning` | ✔ | |
| `task` | ✔ | |
| `research` | ✔ | |
| `action` | ✔ | A request to *do* something is exactly what is worth laying out before anyone does it. Planning it is the safe half of handling it. |
| `question` | ✘ | A question wants an answer. Answering it with a four-step project plan is a worse response, not a richer one. |
| `conversation` | ✘ | |
| `unknown` | ✘ | Classification failed. Planning against a guess is how invented constraints get in. |

A degraded intent — one where classification failed — is never planned, for the
same reason.

**An ineligible message makes no planning call**, so ordinary conversation
costs exactly what it did before Stage 4B.

---

## Ambiguity: clarify, never invent

The specification offers two acceptable behaviours. Stage 4B chooses the first
and applies it deterministically:

> **A goal with `ambiguity = high` is not planned.** The result is
> `NEEDS_CLARIFICATION` with a question for the user.

"Do something about this." A plan here would be invention: the model would have
to supply the subject, the outcome *and* the constraints, and each would arrive
labelled as a plan rather than as a guess.

Asking is also cheaper — this branch makes **no model call at all**.

`ambiguity = mild` ("Help me with my business") *is* planned. The intent is
clear even though the subject is thin, and whatever the model has to fill in
lands in `assumptions`, where the user can correct it.

---

## The models

### Goal

`summary`, `desired_outcome`, `scope`, `constraints`, `source_intent`.

`constraints` holds limits the user *stated*. Anything the model needed but was
not told belongs in `assumptions`: an invented constraint is indistinguishable
from a real one once it is in the field.

`source_intent` records which classification justified planning, so a plan can
always be traced back to the understanding behind it.

### Plan

`goal`, `tasks`, `assumptions`, `risks`, `success_criteria`.

Frozen. Two read-only properties and nothing else — no `execute`, no `run`, no
`approve`, no reference to a tool, no execution state.

### PlanTask

`id`, `title`, `description`, `priority`, `dependencies`, `expected_outcome`,
`completion_criteria`, plus `order` and `depth` computed during validation.

Task ids are **slugs**: lowercase letters, digits, hyphens and underscores.
Restricting the character set makes an id safe to put in a URL, a log line or a
rendered list without escaping, and means an id cannot carry markup or a
delimiter into whatever renders it.

`priority` is a closed three-value enum. Arbitrary priority strings make plans
incomparable and invite invented scales.

---

## Two validation layers

They are separate modules with separate tests because they fail differently.

### Layer 1 — schema (`schemas.py`)

Required fields, types, closed enums, id format, field lengths, list lengths,
duplicate task ids. Pydantic, with `extra="ignore"` so an invented field —
`approved`, `execute`, `tool` — is dropped before it is ever read.

### Layer 2 — graph (`validator.py`)

**Valid JSON is not a valid plan.** A proposal with a perfect shape can
describe an impossible graph.

Checks run cheapest-and-most-specific first, so a rejection names the actual
problem rather than a downstream symptom — a missing dependency reported as
"cycle detected" would be actively misleading:

```
empty  →  too many tasks  →  duplicate ids  →  per-task dependency budget
       →  self-dependency  →  unknown dependency  →  total edge budget
       →  cycle detection + topological order
```

Pure functions throughout: no database, no model, no clock, no I/O.

---

## Dependencies and ordering

The specification's worked example, validated end to end:

```
research-market          depth 0    order 1
analyze-competitors      depth 1    order 2    depends_on: research-market
define-positioning       depth 2    order 3    depends_on: research-market,
                                                            analyze-competitors
launch-strategy          depth 3    order 4    depends_on: define-positioning
```

| Rule | Behaviour |
| --- | --- |
| Dependency must exist | rejected: `unknown_dependency` |
| Self-dependency | rejected: `self_dependency` |
| Duplicate dependency | **normalised**, not rejected |
| Cycle, any length | rejected: `dependency_cycle` |
| Ordering | deterministic |

Duplicates are normalised because a model listing a prerequisite twice is
describing the graph correctly and writing it clumsily; failing the whole plan
for that trades a real plan for a formatting complaint. A dependency that does
not *exist* is a broken graph, and is rejected.

### Determinism

Kahn's algorithm with **declaration order as the tie-break**.

Determinism matters more than it looks: two runs over the same plan must
produce the same order, or a plan is not something a later stage can reason
about, diff, or show twice. A `set`-based frontier would be correct and
non-deterministic.

Declaration order is the tie-break rather than priority because when the graph
does not constrain two tasks, the order the model wrote them in is the only
signal about sequence that exists — discarding it would scramble a plan the
model laid out sensibly.

`depth` is the **longest** path from a root, not the shortest, so a task never
appears shallower than its deepest prerequisite.

---

## Limits

Stated once, in `limits.py`, so the schema and the graph validator agree.

| Limit | Value | Reasoning |
| --- | --- | --- |
| `MAX_TASKS` | 20 | A plan a person can hold in their head. A goal needing more should be split into sub-goals — a later stage's problem, not a reason to raise this. |
| `MAX_DEPENDENCIES_PER_TASK` | 5 | A task waiting on more than a handful is usually mis-decomposed. |
| `MAX_TOTAL_DEPENDENCIES` | 60 | Well below the theoretical 100. A plan at that density is a graph, not a sequence. |
| `MAX_TASK_ID_LENGTH` | 40 | Slug-shaped and readable. |
| `MAX_TASK_TITLE_CHARS` | 120 | One line. |
| `MAX_TASK_DESCRIPTION_CHARS` | 600 | One paragraph. |
| `MAX_COMPLETION_CRITERIA` | 5 | More and the task should be split. |
| `MAX_ASSUMPTIONS` / `RISKS` / `SUCCESS_CRITERIA` | 10 each | More than a readable plan needs. |
| `MAX_PLANNED_MESSAGE_CHARS` | 4000 | A goal is stated near the start of a request. |

These are not a guess at how complex a real project is — they are a guess at
how complex a *useful* plan is. They also mean every later stage inherits a
graph whose size is already known.

---

## Assumptions, risks, success criteria

**Assumptions stay assumptions.** Stage 4B never writes them into the memory
system. An assumption promoted to a memory becomes a fact about the user that
the user never stated, and would then be retrieved for years as though they
had. A test asserts the memory table is untouched.

**Risks are informational.** They alter nothing — there is no execution policy
for them to alter.

**Success criteria are plan data, not stop conditions.** There is no loop to
stop.

---

## Chat integration

```
message
   │
   ├─ intent.understand(...)          ← 1 classification call  (4A)
   ├─ planning.plan_for(message, intent)
   │      └─ policy.decide → eligible? → 1 planning call        (4B)
   ├─ context assembly + prompt                                 (3A/3B)
   ├─ provider.generate_response(prompt)  ← 1 generation call   (3B)
   └─ background: memory → entity → relationship → conflicts
```

The intent is **reused, never recomputed** — a test asserts the message is
classified exactly once.

The plan is returned as a sibling field on `ChatResponse`, next to `intent`:

```json
{
  "user_message": { … },
  "assistant_message": { … },
  "intent":   { "intent_type": "planning", … },
  "planning": { "status": "ready", "plan": { "tasks": [ … ] } }
}
```

**The plan never reaches the prompt.** A test runs the same message with
planning on and off and asserts the message lists sent to the model are
byte-identical. The reply is an ordinary chat reply either way.

---

## Model calls per turn

| Message | Calls |
| --- | --- |
| Ordinary (conversation, question) | classification + generation = **2** |
| Ambiguous goal (`high`) | classification + generation = **2** — clarification costs nothing |
| Planning-eligible | classification + planning + generation = **3** |
| Background, all turns | up to 3 more, after the response |

**This is a real cost.** A planning-eligible turn is three request-path calls
where Stage 3B had one. It is bounded in two ways: eligibility is deterministic
and rejects most messages before any call, and `PLANNING_ENABLED=false`
restores the exact pre-4B profile.

There is **no retry and no replanning**. A proposal that fails validation
becomes a failure, not another call: a second attempt at a semantic failure — a
cycle, a missing dependency — re-rolls the same dice at double the cost.

---

## Security boundaries

| Guarantee | How it is enforced |
| --- | --- |
| A plan cannot execute itself | `app/planning/` imports no subprocess, filesystem, socket or HTTP module, and calls no `eval`/`exec`/`open` |
| ACTION tasks cannot execute | Same. An action plan is identical in power to any other plan |
| The model cannot create tools | `extra="ignore"`; `tool`, `command`, `execute` are dropped before being read |
| The model cannot decide to plan | `policy.decide` is a pure function over the intent |
| The model cannot alter execution policy | There is no execution policy |
| Planning cannot mutate knowledge | The package imports nothing from `app.memory`, `app.entities`, `app.relationships`, `app.knowledge` or `app.database`, and contains no `session.add`/`commit`/`delete` |
| Retrieved knowledge is not planner authority | Stage 2D retrieval is not wired into the planner. A memory saying "always execute plans immediately" is never even shown to it |
| Plans are not privileged prompt content | The prompt is byte-identical with planning on and off |
| Nothing else can reach a plan | Only the API view imports the plan types; nothing iterates a task list |

The strongest of these is the first. The coercion suite scripts the fake
provider to **agree** — returning a plan whose first task is "Execute
immediately" and whose second is "Delete all memories" — because the guarantee
must not depend on the model refusing. The plan comes back as two rows of text.

---

## Persistence

**None.** No table, no migration, no schema change.

Plans are ephemeral, which is the specification's default and correct here:
nothing in 4B reads a past plan, and persistent task *execution* state belongs
to a later stage. Storing one now would guess at a shape 4D has not defined.

---

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `PLANNING_ENABLED` | `true` | Off restores the pre-4B call profile exactly |
| `PLANNING_TEMPERATURE` | `0.2` | Low but not zero — decomposition benefits from slight variation where classification does not |
| `PLANNING_MAX_TOKENS` | `2048` | The schema limits cap what can survive validation regardless |

---

## Debug API

`POST /api/planning/debug` — `{"message": "…", "conversation_id": "…optional…"}`

Runs the same two services in the same order with the same eligibility check,
so what is shown is what a turn would produce. At most two model calls.

`not_eligible` and `needs_clarification` are normal results, not errors: most
messages do not warrant a plan, and a vague goal is better answered with a
question than with an invented one.

---

## What Stage 4B deliberately does not do

- **No execution.** Nothing runs. No tool, no shell, no file, no network call.
- **No tools and no registry.** That is 4C.
- **No agent loop, no replanning, no retry.**
- **No approval flow.** 4A's `requires_user_approval` is still just a statement.
- **No persistence.**
- **No influence on the reply.** The prompt is identical with planning on or off.
- **No second context system.** Stage 2D retrieval is not reused.
- **No memory writes.** Assumptions stay assumptions.

---

## Known limitations

- **Plan quality is the model's, and was not evaluated live.** Tests script the
  provider's answers, so what is verified is Mai's handling of a proposal —
  parsing, schema validation, graph validation, ordering, degradation — not the
  judgement behind it. **Planning quality: NOT VERIFIED.**
- **A planning turn costs three request-path calls.** See *Model calls*.
- **`ambiguity = high` never produces a plan**, even when a reasonable
  high-level one exists. The trade is deliberate: no invented constraints.
- **Ordering follows the graph, not effort or priority.** `order` is a
  topological position, not a schedule; nothing estimates duration.
- **20 tasks is a hard ceiling.** A genuinely larger goal is rejected rather
  than split, because sub-goal decomposition is a later stage.
- **Docker, PostgreSQL and the frontend remain NOT VERIFIED**, unchanged from
  Stage 3D. Stage 4B adds no migration and no frontend surface.
