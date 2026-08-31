# Mai — Stage 4D: Action Proposal & Orchestration

Stage 4D connects Mai's decision systems to the Stage 4C authorization
boundary. A message can now travel from intent, through identification, to a
real authorization decision.

The journey **ends there**. Nothing executes, and nothing claims it did.

---

## Three conflicts found before implementing

The specification was reviewed against the existing code first. Three of its
assumptions did not hold, and each is resolved below rather than worked around.

### 1. The action-capable allowlist can only be `{ACTION}`

The spec's pipeline shows action identification fed from both the planning and
conversational paths, implying an allowlist including RESEARCH and TASK.

Checked against the code:

```
Stage 4A  requires_execution = True   only for ACTION
Stage 4C  _intent_rule       FORBIDDEN whenever requires_execution is False
```

So a proposal raised from a RESEARCH, TASK, PLANNING, QUESTION or CONVERSATION
turn is **already deterministically `FORBIDDEN`** before its tool is examined.
A broader allowlist would buy nothing and cost twice: work on turns whose
outcome is known in advance, and a `forbidden` shown to the user whose real
cause is "Mai was never going to act here" — which reads as a refusal rather
than as an absence.

**Resolution:** the allowlist is exactly `{ACTION}`. This is not caution; it is
the only value consistent with the two boundaries already in place. A test
derives Stage 4A's execution-granting set and asserts the two are equal, so
they cannot drift apart silently.

Where a research or planning request should eventually lead to an action, the
route is the one Stage 4B already describes: the request produces a plan, and a
later stage maps a task to a proposal authorized on its own terms.

### 2. `APPROVAL_REQUIRED` was unreachable

Stage 4C registered every `future_*` tool with `enabled=False`, because nothing
consumed the registry. End to end that left only three outcomes reachable:

| Outcome | Reachable in 4C |
| --- | --- |
| ALLOWED | `echo` |
| APPROVAL_REQUIRED | **nothing** |
| FORBIDDEN | every `future_*` |
| UNKNOWN_TOOL | anything else |

The spec requires `ACTION_REQUIRES_APPROVAL` as an outcome and asks for it to
be tested. Leaving it unreachable would make a required state vestigial and
hide the difference between *"we do not permit this"* and *"a human would have
to confirm it"*.

**Resolution:** the operator switch now reflects a real position.
`future_web_search`, `future_generate_document` and `future_send_email` are
enabled; `future_delete_file` stays disabled **and** CRITICAL, refused twice
over.

`enabled` and `execution_mode` answer different questions, and this is where
the difference starts to matter:

- `enabled` — the **operator's** switch: would we permit this capability?
- `execution_mode` — the **application's** statement of fact: can it run?

Only the first changed. Every tool is still `unavailable`, still has no
implementation, and still cannot run. A new invariant test asserts **only a
diagnostic tool may be both enabled and approval-free**.

### 3. A fourth request-path model call, on the riskiest turn

The spec permits a model call for action identification. An ACTION turn already
makes three (classify, plan, generate); a fourth would land on the one intent
that can lead to a real side effect.

**Resolution:** deterministic identification. Zero model calls. Reasoning
below.

---

## Why identification is deterministic

The spec offers three strategies and asks for "the safest approach that fits
the existing architecture". This is the first, and it is better here on four
counts:

- **It cannot invent a capability.** The matcher emits only names drawn from an
  application-authored table, each asserted to exist in the registry at import.
  A model asked to choose from a list can still return something not on it; a
  lookup cannot.
- **It costs nothing.** No fourth call on the one intent that matters most.
- **It is deterministic**, so ordering, deduplication and the tests are exact
  rather than probabilistic.
- **It fails toward no action.** A paraphrase the table lacks yields
  `NO_ACTION` — which is the spec's own instruction for an ambiguous request.

What it gives up is recall. "Fire off a note to Gautam" will not match. That is
a documented limitation, not a defect: **a missed action is a conversation, an
invented one is an incident.**

---

## The pipeline

```
user message
      │
      ▼
intent classification (4A)                    ← 1 model call
      │
      ▼
eligibility.decide          ── deterministic; ACTION only
      │
      ├─ not eligible ──────────────────────► NOT_ELIGIBLE   (no work at all)
      ▼
matching.find_candidates    ── phrase table; ZERO model calls
      │
      ├─ nothing matched ───────────────────► NO_ACTION
      ▼
deduplicate → bound to 5
      │
      ▼
ActionProposal (4C) ──► AuthorizationService (4C)   ← per proposal
      │
      ▼
OrchestrationResult         ── most restrictive outcome wins
```

---

## Outcomes

Six explicit states, never a boolean:

| Outcome | Meaning |
| --- | --- |
| `not_eligible` | The intent did not warrant looking. **No work was done.** |
| `no_action` | Eligible; nothing matched a known capability |
| `action_unknown` | Something named no registered tool |
| `action_forbidden` | Policy refuses it |
| `action_requires_approval` | A human would have to confirm |
| `action_allowed_not_executed` | Policy does not forbid it — **and it did not happen** |

Two "nothing happened" states rather than one, because the difference is
operationally important: only `not_eligible` is evidence that gating worked.

The permissive outcome is **named for what it is**. A client cannot read
permission as completion, and a test asserts no outcome value contains
"complete", "success", "done" or "sent".

The overall outcome is the **most restrictive** across every proposal, so one
refused action is never hidden behind another that passed.

---

## Truthfulness

Stage 4D has no execution, so Mai must never claim a side effect occurred. Two
independent measures, because they cover different things.

**1. The result cannot represent completion.** Structural:

- No `executed`, `completed`, `result` or `output` field on any result type.
- `acted` is a **property** returning `False`, so it cannot be set by
  construction, validation or deserialisation. A test round-trips
  `{"acted": true}` through `model_validate` and asserts it comes back false —
  and contrasts it with a field-based model that *would* accept it.
- The wire format states `executed: false` on the result and on every proposal.

**2. The model is told what it cannot do.** Orchestration state deliberately
never enters the prompt — so without a standing statement, the model would
cheerfully report having sent the email.

The capability statement was added to `MAI_SYSTEM_PROMPT`:

> You cannot perform actions outside this conversation. You have no tools: you
> cannot search the web, send email, read or write files, run code, or change
> anything in any external system. Never say or imply that you have done any of
> those things.

It belongs there rather than in per-turn context: it is a standing fact about
the application, not state about this turn. The invariant established in 4A and
4B holds unchanged — **the prompt is byte-identical with orchestration on and
off**, asserted by test.

---

## Authority boundaries

| Guarantee | How |
| --- | --- |
| The model cannot name a tool | Identification is a lookup over an application table; the matcher emits only its own keys |
| The model cannot register a tool | Unchanged from 4C: only `catalog.py` calls `register` |
| A proposal cannot carry authority | Unchanged from 4C: `ActionProposal` has no `approved`, `requires_approval` or `risk_level` field |
| Authorization cannot be skipped | Every candidate goes through `_authorize`, which always calls Stage 4C. Tests assert proposals == decisions |
| Policy is not duplicated | This package calls `AuthorizationService`; it contains no risk or approval logic |
| Source grants nothing | User, model, plan and system receive identical decisions |
| Plans grant nothing | `app/planning/` imports neither `app.tools` nor `app.orchestration`, and the reverse holds |
| Memories grant nothing | The package imports no database module at all |
| More restrictive wins | Overall outcome is a maximum over an explicit ordering |

---

## Multiple actions

A message may imply several. Each is authorized **independently**: one decision
never influences another, and an approval requirement on one action never
covers a second.

**Ordering** is by where the phrase matched, then by tool name — a message
naming two actions almost always means them in the order it names them.

**Deduplication is exact only**: same canonical tool name *and* identical
arguments. Two proposals for the same tool with different arguments are two
different actions and both survive. No fuzzy merging — collapsing near-
identical proposals would mean one decision standing in for an action nobody
authorized.

**Bound: 5 proposals.** Candidates come from matching over a message the user
controls; without a bound, a message naming every tool repeatedly would produce
an unbounded authorization pass. Discards are counted so truncation is visible.

---

## Ambiguity

The threshold is the phrase table itself. A message that does not contain an
imperative trigger phrase produces `NO_ACTION`.

"Can you handle my competitor research?" matches nothing — it names no
capability imperatively — so nothing is proposed and nothing is guessed. That
is the specification's instruction ("do not guess a dangerous action")
implemented as the default rather than as a special case.

Phrases are deliberately specific. One was removed during development after
firing on a passing mention: `"web search"` matched *"tell me about web search
engines"*. A noun phrase is a topic, not a request.

---

## Cost

| Turn | Request-path model calls |
| --- | --- |
| Ordinary (conversation, question) | 2 — classify + generate |
| Planning-eligible | 3 — classify + plan + generate |
| **Action** | **3** — classify + plan + generate |

**Stage 4D adds zero.** An ACTION intent is also plannable under 4B, so an
action turn costs the same as a planning turn.

Orchestration itself costs a set membership test, one regex scan and up to five
dictionary lookups. No database query, no network request, no recursion, no
retry.

`ORCHESTRATION_ENABLED=false` removes the field from responses and changes
nothing else.

---

## Failure handling

| Failure | Result |
| --- | --- |
| Intent unclassified or degraded | `not_eligible` — guessing from an unclassified message is where a wrong guess is expensive |
| Empty message | `not_eligible` |
| Matcher raises | `no_action`; logged; the turn continues |
| Candidate names an unknown tool | `action_unknown` for that proposal only — it does not poison valid ones |
| Arguments invalid | `action_forbidden` for that proposal only |
| Orchestration disabled | `not_eligible` |

Every path leaves the chat turn intact, mutates nothing, and produces no
proposal that could be mistaken for a completed action.

---

## No execution

Stage 4C's guarantee is preserved and re-asserted over the new package:

1. **No method to call.** `Tool` still defines no `execute`, `run`, `invoke`,
   `dispatch` or `__call__`. Stage 4D added a *consumer* of the registry, not a
   way to run anything.
2. **No dangerous import.** An AST scan of `app/orchestration/` rejects 14
   modules and 7 builtins, matching the scan already applied to `app/tools/`.
3. **No execution endpoint.** Exactly three routes exist across both packages:
   `/api/tools`, `/api/tools/authorize`, `/api/orchestration/debug`.
4. **No persistence.** No execution table, approval grant or executed state —
   asserted against the live metadata.

---

## API

`POST /api/orchestration/debug` — classify a message, then orchestrate it.
Propose, authorize, return. One model call for classification, none for
orchestration. `executed: false` throughout.

`not_eligible` and `no_action` are normal results, not errors — almost every
message is one or the other.

---

## Future Stage 4E path

Stage 4E adds the approval workflow. What it must supply, and what this stage
has already settled:

| Stage 4E must add | Stage 4D already fixed |
| --- | --- |
| A way to ask a human | The four states an answer applies to |
| An audited approval record | The proposal shape it attaches to |
| A revocable, scoped grant | That a grant is separate from a proposal |
| The architecture to raise `MAX_PERMITTED_RISK` above HIGH | Why CRITICAL is refused today |

Execution remains Stage 4D+E's successor's problem. Adding it requires adding a
method that three tests assert is absent.

---

## Known limitations

- **Recall is limited by the phrase table.** Paraphrases are missed. A missed
  action is a conversation; an invented one is an incident.
- **Arguments are shallow.** Only `echo` takes any; declared future
  capabilities take none, because none has an argument schema.
- **`ambiguity` is not consulted.** Stage 4A's ambiguity reading gates planning
  but not orchestration: a phrase match is already a strong, specific signal,
  and the table's specificity does the work ambiguity would.
- **Nothing maps a plan task to a proposal.** Deliberate — a task is not an
  action, and the mapping needs the approval architecture that does not exist.
- **The model's prose is still the model's.** The system prompt states the
  boundary and the structured result cannot claim completion, but no test here
  can prove a live model never hallucinates a completion. **NOT VERIFIED**
  against a live provider.
- **Docker, PostgreSQL and the frontend remain NOT VERIFIED**, unchanged from
  Stage 3D.
