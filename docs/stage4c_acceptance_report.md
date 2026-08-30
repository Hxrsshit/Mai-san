# Mai — Stage 4C Acceptance Report

**Status: PASS**

All 49 acceptance criteria met. Stage 4D has not been started.

Architecture: [`stage4c_tool_authorization_architecture.md`](stage4c_tool_authorization_architecture.md).

---

## Test summary

| | |
| --- | --- |
| **Total** | **1681** |
| **Passed** | **1681** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Baseline before Stage 4C (`76fc28b`) | 1499 |
| **Added by Stage 4C** | **182** |

Baseline verified, not assumed: 1499 passing, 0 failed, 0 skipped, clean tree
at `76fc28b` — matching the stated expectation exactly.

| New file | Tests | Covers |
| --- | --- | --- |
| `tests/test_tool_registry.py` | 76 | Definitions, categories, risk levels, name canonicalisation, registration, duplicates, immutability, catalogue, argument validation |
| `tests/security/test_tool_security.py` | 62 | The ten named attacks, structural no-execution checks, memory/plan escalation, database invariants, API surface |
| `tests/test_tool_authorization.py` | 36 | The four states, determinism, risk ladder, monotonicity, intent precedence, arguments, failure handling |
| `tests/security/test_tool_mutation_guards.py` | 8 | Proof each authority guard is load-bearing |

---

## Implementation summary

New package `app/tools/` — seven modules, **no database schema**:

| Module | Responsibility |
| --- | --- |
| `schemas.py` | `ToolDefinition` (application), `ActionProposal` (untrusted), `AuthorizationDecision` (computed), closed enums, the restrictiveness ordering |
| `base.py` | The `Tool` contract — metadata and argument schema, **and no execute method** |
| `registry.py` | The closed, application-controlled registry |
| `policy.py` | Deterministic, monotonic authorization rules |
| `authorization.py` | Orchestration: lookup → policy → argument validation → decision |
| `catalog.py` | The only place `register` is called |

Integration:

- `GET /api/tools`, `POST /api/tools/authorize` — both read-only.
- Stage 4A's capability flag gains its first real consumer, in `policy.py`,
  where it can only tighten.
- **No migration, no table, no persistence.** Authorization is in-memory
  policy evaluation.

---

## Registry tests

| Property | Result |
| --- | --- |
| Register and retrieve | PASS |
| Duplicate registration refused, original preserved | PASS |
| Unknown tool returns nothing | PASS |
| Lookup normalises case and whitespace only | PASS (5 forms) |
| Lookup is never fuzzy | PASS (10 near-miss names) |
| Listing is deterministic and name-sorted | PASS |
| Listing exposes no mutable collection | PASS (tuple of frozen models) |
| Mutating a retrieved definition cannot change the registry | PASS |
| Every `future_*` tool is disabled | PASS |
| No registered tool claims to be executable | PASS |

---

## Authorization tests

All four states reachable and meaningful:

| Input | Status |
| --- | --- |
| `delete_everything` | `UNKNOWN_TOOL` |
| `future_web_search` (disabled) | `FORBIDDEN` |
| A registered MEDIUM-risk tool declaring approval | `APPROVAL_REQUIRED` |
| `echo` with valid arguments | `ALLOWED` |

**The risk ladder**, verified with a tool that opts *out* of approval at each
level:

| Risk | Outcome |
| --- | --- |
| LOW | ALLOWED |
| MEDIUM | ALLOWED |
| HIGH | APPROVAL_REQUIRED |
| CRITICAL | FORBIDDEN |

**Monotonicity**, verified exhaustively: every combination of 4 risk levels ×
6 categories × 2 approval flags × 2 enabled flags — 96 registrations — checked
for the correct outcome and for `requires_approval` being true wherever the
status is not `ALLOWED`.

**Determinism**: repeated decisions identical across 10 runs; decisions frozen;
the package imports nothing that could reach a model, database or network.

---

## Adversarial tests — the ten named attacks

| # | Attack | Result |
| --- | --- | --- |
| 1 | Invented tool (15 names incl. `exec`, `rm`, `bash`, `subprocess.run`) | `UNKNOWN_TOOL`; never auto-registered |
| 2 | Approval spoofing (8 payload shapes) | Fields are not on the proposal; dropped before read |
| 3 | Risk downgrade | Registry risk authoritative |
| 4 | Prompt injection in a tool name (6 forms) | A name is looked up, never interpreted |
| 5 | Memory poisoning ("all file deletion tools are pre-approved") | Zero effect; the path cannot read a memory |
| 6 | Plan authority escalation ("pre-approved, must execute automatically") | Inert text on the response |
| 7 | Similar-name confusion (9 near misses) | `UNKNOWN_TOOL`; no fuzzy library referenced |
| 8 | Argument metadata injection | Refused by `extra="forbid"` |
| 9 | Registry mutation through returned metadata | Frozen; registry unchanged |
| 10 | Fallback execution via shell-like name | Refused; no subprocess exists to fall back to |

Additional: proposal `source` grants nothing (all four sources → same outcome);
injection in `rationale` has no effect.

---

## Mutation tests

The specification asks for tests demonstrating a broken implementation would be
detected. Both forms were performed.

**Source mutations**, applied directly to the files during development:

| # | Mutation | Tests failed |
| --- | --- | --- |
| 1 | Unknown tools allowed | **33** |
| 2 | Approval requirement overridable | **3** |
| 3 | Risk ceiling removed | **2** |
| 4 | Intent boundary bypassed | **3** |
| 5 | Fuzzy tool matching introduced | **15** |
| 6 | Registry metadata made mutable | **4** |
| 7 | No-execution boundary weakened | **1** |
| 8 | Monotonicity broken (least restrictive wins) | **32** |

**In-process guard tests** (`test_tool_mutation_guards.py`, 8 tests): each
disables one guard via monkeypatch, shows the security assertion *fails*, then
restores it and shows it passes. That is the difference between "this property
holds" and "this property is enforced" — and each asserts the mutation actually
took effect, so a test that proves nothing fails loudly.

---

## No-execution verification

Three independent structural layers:

1. **No method to call.** `Tool` defines no `execute`, `run`, `invoke`,
   `dispatch` or `__call__` — not abstract, not private, not raising. A test
   scans `Tool` and every subclass, and separately proves the check can see an
   added method, so it is not vacuous.
2. **No dangerous import.** AST scan of every module in `app/tools/` rejects
   14 modules and 7 builtins.
3. **No dispatcher anywhere.** No module outside the package imports `Tool`;
   none contains tool-shaped dispatch.

Plus: no registered definition may declare `execution_mode` other than
`unavailable`, so no tool can even *claim* to be executable.

API surface verified: exactly two routes exist, `/api/tools` and
`/api/tools/authorize`. `POST`/`PUT`/`PATCH`/`DELETE` on `/api/tools/execute`
return 404/405.

---

## Database mutation verification

Memory, entity, relationship and lifecycle counts are identical before and
after authorizing four proposals, and after a `POST /api/tools/authorize` with
a spoofed `approved: true`.

Structurally: `app/tools/` imports nothing from `app.memory`, `app.entities`,
`app.relationships`, `app.knowledge`, `app.database` or `sqlalchemy` — the
authorization path cannot read a memory, let alone write one.

---

## Design decisions worth recording

**No `execute` method at all.** The specification permits an abstract execute
that stays "abstract or inaccessible". Absence is stronger than either: a
dispatcher cannot be written against a method that does not exist, and a test
asserting no tool defines one cannot be satisfied by accident.

**The no-execution guarantee is not a policy rule.** An earlier draft added a
rule returning `APPROVAL_REQUIRED` whenever `execution_mode` was `unavailable`
— which is always. It made `ALLOWED` unreachable and collapsed four meaningful
states into three, hiding the difference between "policy permits this" and
"nothing can do this yet". The guarantee belongs in the structure.

**CRITICAL is refused, not gated.** Gating irreversible damage behind a prompt
would claim a prompt is sufficient protection; the architecture that would make
that true is Stage 4E's.

**Arguments use `extra="forbid"`**, unlike model output elsewhere. An
unexpected argument means the proposal and the tool disagree about what is
being asked for — dropping it would authorise a *different* action.

---

## Changes to existing behaviour

| Change | Why |
| --- | --- |
| `test_nothing_downstream_branches_on_a_capability_flag` renamed and widened | Stage 4A's guard fired correctly: `tools/policy.py` is the first real consumer of `requires_execution`. The test now records the allowlist and the reason each entry is safe |

No memory, retrieval, context, prompt-security, lifecycle, intent or planning
behaviour was modified. Nothing was added to the chat request path.

---

## Known limitations

- **No tool does anything.** By design. This stage ships an authorization
  framework with nothing to authorize yet.
- **`FORBIDDEN_CATEGORIES` is empty.** The hook exists; there is no real
  capability to forbid.
- **Approval is represented, not workflowed.** Nothing asks a human, records an
  answer, or remembers one — Stage 4E.
- **Decisions are ephemeral.** No audit trail is persisted; decisions are
  logged (names, statuses, counts — never argument values).
- **No rate limiting or quota.** There is nothing to limit.
- **Nothing on the chat path proposes an action.** Stage 4C is reachable only
  through its own endpoints and by later stages; no turn produces an
  `ActionProposal` today.
- **Docker, PostgreSQL and the frontend remain NOT VERIFIED**, unchanged from
  Stage 3D.

---

## Acceptance criteria

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Baseline tests still pass | PASS (1499 → 1681) |
| 2 | Stable application-controlled identities | PASS |
| 3 | Categories typed | PASS |
| 4 | Risk levels typed | PASS |
| 5 | Metadata application-controlled | PASS |
| 6 | Deterministic registry | PASS |
| 7 | Unknown tools fail closed | PASS |
| 8 | Duplicate registration rejected | PASS |
| 9 | Metadata not mutable via request data | PASS |
| 10 | Lookup deterministic | PASS |
| 11 | Proposals separate from authorization | PASS |
| 12 | Explicit decision states | PASS |
| 13 | No LLM call | PASS (structural) |
| 14 | No network call | PASS (structural) |
| 15 | Model cannot register tools | PASS |
| 16 | User input cannot register tools | PASS |
| 17 | Plans cannot register tools | PASS |
| 18 | Memories cannot register tools | PASS |
| 19 | Unknown tools cannot execute | PASS |
| 20 | No fuzzy matching | PASS |
| 21 | Arguments cannot change risk | PASS |
| 22 | Arguments cannot grant approval | PASS |
| 23 | Model output cannot grant approval | PASS |
| 24 | Plans cannot grant approval | PASS |
| 25 | Memories cannot grant approval | PASS |
| 26 | User text cannot bypass approval | PASS |
| 27 | More restrictive policy wins | PASS |
| 28 | Stage 4A boundaries authoritative | PASS |
| 29 | Stage 4B plans remain inert | PASS |
| 30 | No general execution dispatcher | PASS |
| 31 | No shell execution | PASS |
| 32 | No subprocess execution | PASS |
| 33 | No arbitrary Python execution | PASS |
| 34 | No filesystem tool | PASS |
| 35 | No HTTP/network tool | PASS |
| 36 | Test tool is side-effect free | PASS |
| 37 | Authorization fails closed | PASS |
| 38 | Invalid arguments fail safely | PASS |
| 39 | No fallback execution path | PASS |
| 40 | Does not mutate memories | PASS |
| 41 | Does not mutate entities | PASS |
| 42 | Does not mutate relationships | PASS |
| 43 | Does not mutate lifecycle state | PASS |
| 44 | Adversarial spoofing tests pass | PASS |
| 45 | Mutation tests protect authority boundaries | PASS (8 + 8) |
| 46 | Structural no-execution checks pass | PASS |
| 47 | Full regression passes | PASS (1681) |
| 48 | Documentation complete | PASS |
| 49 | **Stage 4D has not started** | **PASS** |

---

## Stage 4D has not been started

No execution loop, no dispatcher, no task-to-proposal mapping, no observation
handling and no `execute` method exists in this codebase.

Stage 4C ends at an authorization decision, exactly as specified.
