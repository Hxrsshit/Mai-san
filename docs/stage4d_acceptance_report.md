# Mai — Stage 4D Acceptance Report

**Status: PASS**

All 50 acceptance criteria met. Stage 4E has not been started.

Architecture: [`stage4d_action_orchestration_architecture.md`](stage4d_action_orchestration_architecture.md).

---

## Test summary

| | |
| --- | --- |
| **Total** | **1790** |
| **Passed** | **1790** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Baseline before Stage 4D (`ad5869d`) | 1681 |
| **Added by Stage 4D** | **109** |

Baseline verified, not assumed: 1681 passing, 0 failed, 0 skipped, clean tree
at `ad5869d` — matching the stated expectation exactly.

| New file | Tests | Covers |
| --- | --- | --- |
| `tests/test_orchestration.py` | 56 | Eligibility, matching, proposals, outcomes, cost, determinism, failure, chat integration |
| `tests/security/test_orchestration_security.py` | 43 | The ten named attacks, truthfulness, no-execution, database invariants, logging |
| `tests/security/test_orchestration_mutation_guards.py` | 7 | Proof each orchestration guard is load-bearing |

Three further tests were added to the Stage 4C suite when the catalogue
changed (see *Changes to existing behaviour*).

---

## Architectural review — three conflicts found before implementing

The specification was checked against the code first. Three assumptions did not
hold. Each is resolved, not worked around.

### 1. The action-capable allowlist can only be `{ACTION}`

The spec's pipeline implies RESEARCH and TASK feed action identification.
Measured against the code:

```
Stage 4A  requires_execution = True   only for ACTION
Stage 4C  _intent_rule       FORBIDDEN whenever requires_execution is False
```

Every proposal from a non-ACTION turn is therefore **already deterministically
`FORBIDDEN`** before its tool is examined. A broader allowlist would cost work
on turns whose outcome is known in advance and report a `forbidden` whose real
cause is "Mai was never going to act here".

**Resolved:** the allowlist is exactly `{ACTION}` — the only value consistent
with the two existing boundaries. `test_the_allowlist_matches_stage_4a_execution_capability`
derives Stage 4A's set and asserts equality, so they cannot drift.

### 2. `APPROVAL_REQUIRED` was unreachable end to end

Stage 4C disabled every `future_*` tool, leaving only three of four
authorization outcomes reachable. The spec requires `ACTION_REQUIRES_APPROVAL`
and asks for it to be tested.

**Resolved:** `future_web_search`, `future_generate_document` and
`future_send_email` enabled; `future_delete_file` left disabled **and**
CRITICAL. `enabled` (would we permit it?) and `execution_mode` (can it run?)
answer different questions — only the first changed, and every tool remains
`unavailable` with no implementation. A new invariant test asserts **only a
diagnostic tool may be both enabled and approval-free**.

### 3. A fourth request-path model call on the riskiest turn

**Resolved:** deterministic identification via an application-authored phrase
table. Stage 4D adds **zero** model calls — safer *and* cheaper than the
constrained-selection option the spec also permits.

---

## Implementation summary

New package `app/orchestration/` — four modules, **no database schema**:

| Module | Responsibility |
| --- | --- |
| `schemas.py` | `ActionCandidate`, `ProposalOutcome`, `OrchestrationResult`, the six outcomes and their ordering |
| `eligibility.py` | Deterministic gate, following Stage 4A rather than restating it |
| `matching.py` | The phrase table and deterministic matcher; validated against the registry at import |
| `service.py` | Orchestration: eligibility → match → dedupe → bound → authorize |

Integration:

- `ChatService` orchestrates using the intent it already has; the message is
  classified once.
- `ChatResponse` gains an `orchestration` sibling beside `intent` and
  `planning`.
- `POST /api/orchestration/debug` — propose, authorize, return.
- `MAI_SYSTEM_PROMPT` gained a capability statement (see *Truthfulness*).
- One setting, `ORCHESTRATION_ENABLED`.

---

## Eligibility tests

| Property | Result |
| --- | --- |
| Allowlist equals Stage 4A's execution-granting set | PASS |
| Conversation, question, planning, research, task do zero work | PASS (5 cases) |
| ACTION enters the path | PASS |
| Degraded intent does zero work | PASS |
| Missing intent does zero work | PASS |
| Empty message does zero work | PASS (3 forms) |
| Disabled orchestration does zero work | PASS |

"Zero work" is asserted structurally: no matching, no proposal, no
authorization call, `model_calls == 0`.

---

## Candidate resolution tests

| Property | Result |
| --- | --- |
| Known imperative phrases resolve | PASS (5 tools) |
| Ordinary messages match nothing | PASS (7 cases) |
| Case and punctuation insensitive | PASS |
| Deterministic across 20 runs | PASS |
| Ordered by position in the message | PASS (both directions) |
| Every mapped name exists in the registry | PASS (enforced at import) |
| The matcher cannot emit an unregistered name | PASS (structural) |
| Long messages are bounded before matching | PASS |

A real false positive was found and fixed during development: `"web search"`
fired on *"tell me about web search engines"*. A noun phrase is a topic, not a
request; the phrase was removed.

---

## Authorization integration

| Property | Result |
| --- | --- |
| Every candidate reaches Stage 4C | PASS |
| Overall outcome is the most restrictive | PASS |
| One proposal never authorizes another | PASS |
| Unknown candidate does not poison a valid one | PASS |
| Invalid arguments forbid only their own proposal | PASS |
| Proposal count bounded at 5 | PASS |
| Exact duplicates removed; different arguments kept | PASS |
| Source grants no authority | PASS (all 4 sources identical) |

All five action outcomes are reachable through the real catalogue:

| Message | Outcome |
| --- | --- |
| "Echo this back to me" | `action_allowed_not_executed` |
| "Send an email to Gautam" | `action_requires_approval` |
| "Delete the file notes.txt" | `action_forbidden` |
| (candidate naming an unknown tool) | `action_unknown` |
| "Tell me a joke" | `no_action` |

---

## Adversarial tests — the ten named attacks

| # | Attack | Result |
| --- | --- | --- |
| 1 | Tool invention | `UNKNOWN_TOOL`; the matcher emits only table keys |
| 2 | Immediate-execution injection | No authority change; nothing ran |
| 3 | Approval spoofing | Not fields on a proposal; dropped before read |
| 4 | Risk downgrade | Registry metadata authoritative |
| 5 | Plan escalation ("automatically authorized") | Inert text; proposal still gated |
| 6 | Memory poisoning ("all actions pre-approved") | Zero effect; no database access exists |
| 7 | Similar-name confusion | `UNKNOWN_TOOL` (11 near-miss names) |
| 8 | Argument injection (`execute_now`, `approved`) | Refused by `extra="forbid"` |
| 9 | False completion | Structurally impossible — see below |
| 10 | Source escalation | Identical decisions across all four sources |

Eight coercion messages were run end to end through chat with the fake provider
scripted to classify each as an ACTION. Nothing was permitted without approval
except the inert `echo` tool.

---

## Truthfulness verification

Two independent measures, covering different things.

**The result cannot represent completion — structural:**

- No `executed`, `completed`, `result`, `output` or `succeeded` field on any
  result type.
- `acted` is a **property** returning `False`. A test round-trips
  `{"acted": true}` through `model_validate` and asserts it returns false, then
  contrasts it with a field-based model that *would* accept it — so the test
  proves the property is doing the work.
- No outcome value contains "complete", "success", "done" or "sent". The
  permissive one is named `action_allowed_not_executed`.
- The wire format carries `executed: false` on the result and every proposal.

**The model is told what it cannot do:** a capability statement was added to
`MAI_SYSTEM_PROMPT`. Orchestration state never enters the prompt, so without a
standing statement the model would report having sent the email. A test asserts
the statement's presence and content; another asserts the prompt is
byte-identical with orchestration on and off.

---

## Mutation tests

Both forms, as the specification asks.

**Source mutations**, applied to the files during development:

| # | Mutation | Tests failed |
| --- | --- | --- |
| 1 | Action identification runs for every intent | **7** |
| 2 | Unknown tools allowed | **46** |
| 3 | Authorization skipped entirely | **33** |
| 4 | Restrictive precedence removed | **2** |
| 5 | Model approval metadata trusted | **3** |
| 6 | Proposal bound removed | **1** |
| 7 | False completion allowed (`acted` becomes a field) | **2** |
| 8 | Fuzzy tool matching introduced | **4** |
| 9 | No-execution weakened (`Tool` gains `execute`) | **2** |

**In-process guard tests** (7): each disables one guard via monkeypatch, shows
the security assertion *fails*, restores it, and asserts the mutation actually
took effect — so a test that proves nothing fails loudly.

---

## No-execution verification

Stage 4C's guarantee preserved and extended:

1. **No method to call.** `Tool` defines no `execute`, `run`, `invoke`,
   `dispatch` or `__call__`. Verified over `Tool` and every subclass, with a
   contrast case proving the check can see an added method.
2. **No dangerous import.** AST scan of `app/orchestration/` rejects 14 modules
   and 7 builtins, matching the scan already applied to `app/tools/`.
3. **No execution endpoint.** Exactly three routes across both packages.
   `POST`/`PUT`/`PATCH`/`DELETE` on `/api/orchestration/execute` return 404/405.
4. **Packages do not connect.** `app/planning/` imports neither `app.tools` nor
   `app.orchestration`; the reverse also holds.

---

## Database and state invariants

Memory, entity, relationship and lifecycle counts identical before and after
orchestrating four messages, and after a debug request.

Structurally: `app/orchestration/` imports nothing from `app.memory`,
`app.entities`, `app.relationships`, `app.knowledge`, `app.database` or
`sqlalchemy`.

**No persistence added.** A test asserts the live metadata contains no
`actions`, `action_proposals`, `approvals`, `approval_grants`, `executions`,
`tool_runs` or `orchestrations` table.

---

## Performance

| Turn | Request-path model calls |
| --- | --- |
| Ordinary | 2 — classify + generate |
| Planning-eligible | 3 — classify + plan + generate |
| **Action** | **3** — classify + plan + generate |

**Stage 4D adds zero.** Orchestration costs a set membership test, one regex
scan and up to five dictionary lookups. No database query, no network request,
no recursion, no retry.

---

## Changes to existing behaviour

| Change | Why |
| --- | --- |
| Three `future_*` tools enabled | `APPROVAL_REQUIRED` was unreachable; see conflict 2 |
| `MAI_SYSTEM_PROMPT` gained a capability statement | Truthfulness; the model must know it has no tools |
| `ChatService.send_message` returns a fifth value | The result must reach the caller without passing through the prompt |
| `ChatResponse` gains `orchestration` | The turn's action outcome, as a third sibling |
| Stage 4C tests updated for the catalogue change | `test_every_declared_future_tool_is_disabled` became `..._still_requires_approval`, plus two new invariant tests |
| Capability-flag consumer allowlist widened | Stage 4A's guard fired correctly on the new orchestration modules |

No memory, retrieval, context, prompt-security, lifecycle, intent or planning
*logic* was modified.

---

## Known limitations

- **Recall is limited by the phrase table.** Paraphrases are missed. A missed
  action is a conversation; an invented one is an incident.
- **Arguments are shallow.** Only `echo` takes any; declared capabilities take
  none, because none has an argument schema.
- **Stage 4A's ambiguity reading is not consulted.** A specific imperative
  phrase match is already a strong signal, and the table's specificity does the
  work ambiguity would.
- **Nothing maps a plan task to a proposal.** Deliberate: a task is not an
  action, and the mapping needs approval architecture that does not exist.
- **The model's prose is still the model's.** The system prompt states the
  boundary and the structured result cannot claim completion, but no test here
  proves a live model never hallucinates a completion. **NOT VERIFIED against a
  live provider.**
- **Docker, PostgreSQL and the frontend remain NOT VERIFIED**, unchanged from
  Stage 3D.

---

## Acceptance criteria

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Baseline tests still pass | PASS (1681 → 1790) |
| 2 | Explicit application-controlled eligibility | PASS |
| 3 | Ineligible intents do zero work | PASS |
| 4 | Action-capable intents documented | PASS |
| 5 | Model cannot register tools | PASS |
| 6 | Model cannot invent capabilities | PASS |
| 7 | Candidates resolve only through the 4C registry | PASS |
| 8 | Unknown tools fail closed | PASS |
| 9 | No fuzzy matching | PASS |
| 10 | No silent substitution | PASS |
| 11 | Arguments validate before proposal creation | PASS |
| 12 | Authority fields cannot alter policy | PASS |
| 13 | Proposal count bounded | PASS (5) |
| 14 | Duplicates deterministic | PASS |
| 15 | Plan tasks remain inert | PASS |
| 16 | Plans cannot grant authorization | PASS |
| 17 | Memories cannot grant authorization | PASS |
| 18 | User text cannot grant authorization | PASS |
| 19 | Source grants no authority | PASS |
| 20 | Every proposal passes 4C authorization | PASS |
| 21 | Authorization cannot be bypassed | PASS |
| 22 | More restrictive policy wins | PASS |
| 23 | Stage 4A boundaries authoritative | PASS |
| 24 | Stage 4B boundaries intact | PASS |
| 25 | Ordinary conversation functional | PASS |
| 26 | Action outcomes explicit | PASS (6 states) |
| 27 | Approval-required not shown as executed | PASS |
| 28 | Allowed not shown as executed | PASS |
| 29 | Unknown not shown as completed | PASS |
| 30 | No false completion claims | PASS |
| 31 | Identification failures fail safely | PASS |
| 32 | No execution persistence added | PASS |
| 33 | Orchestration mutates no knowledge state | PASS |
| 34 | No `execute()` introduced | PASS |
| 35 | No generic dispatcher | PASS |
| 36 | No shell execution | PASS |
| 37 | No subprocess execution | PASS |
| 38 | No arbitrary Python execution | PASS |
| 39 | No arbitrary filesystem access | PASS |
| 40 | No arbitrary network execution | PASS |
| 41 | No autonomous execution loop | PASS |
| 42 | Structural no-execution checks pass | PASS |
| 43 | Adversarial tests pass | PASS |
| 44 | Memory poisoning tests pass | PASS |
| 45 | Plan escalation tests pass | PASS |
| 46 | False-completion tests pass | PASS |
| 47 | Mutation tests protect boundaries | PASS (9 + 7) |
| 48 | Full regression passes | PASS (1790) |
| 49 | Documentation complete | PASS |
| 50 | **Stage 4E has not started** | **PASS** |

---

## Stage 4E has not been started

No approval workflow, no approval record, no grant, no revocation and no
execution exists in this codebase.

Stage 4D ends at an authorization decision, exactly as specified.
