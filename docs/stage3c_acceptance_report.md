# Mai — Stage 3C Acceptance Report

**Status: PASS**

All 25 acceptance criteria are met and verified by automated tests. The full
regression suite passes with no previous functionality lost.

Two things are explicitly **not** claimed as verified and are listed under
*Not verified* rather than marked PASS: the PostgreSQL runtime, and the Docker
and frontend components carried forward from earlier stages.

---

## Features implemented

- `app/knowledge/` — the Stage 3C package: `policies.py`, `conflicts.py`,
  `lifecycle.py`, `models.py`, `schemas.py`, `service.py`.
- Deterministic conflict detection: explicit replacement, explicit
  abandonment, exclusive-relationship rules.
- `knowledge_conflicts` link table with real foreign keys and cascade
  behaviour (migration `0006`).
- Supersession that moves a status column and nothing else.
- Cycle prevention and per-decision SAVEPOINT isolation.
- Lifecycle-aware Stage 2D retrieval with historical-intent detection.
- `GET /api/knowledge/debug/{memory_id}` — read-only lifecycle inspection.
- A parametrised prompt-injection regression suite over eleven payloads.
- Two new settings: `CONFLICT_DETECTION_ENABLED`,
  `HISTORICAL_RETRIEVAL_ENABLED`.

---

## Knowledge lifecycle

`MemoryStatus` and `RelationshipStatus` already carried ACTIVE / SUPERSEDED /
ARCHIVED from Stages 2A and 2C, and Stage 2D already filtered on ACTIVE. The
states existed; nothing wrote them and nothing explained them. Stage 3C added
the decider and the explanation, not a new state machine.

| State | Written by 3C | Retrieved |
| --- | --- | --- |
| ACTIVE | yes (default) | always |
| SUPERSEDED | yes | only on explicit historical intent |
| ARCHIVED | never | never |

An **unresolved** conflict is a link between two rows that both stay ACTIVE.
A **resolved** one is a link plus a status change on the older row.

A fourth `CONFLICTED` enum value was considered and rejected: it would not
answer "superseded by what?", extending a PostgreSQL enum needs
`ALTER TYPE … ADD VALUE` (untestable here), and marking one of two contested
items as special already implies a winner.

---

## Conflict detection

| # | Rule | Trigger | Result |
| --- | --- | --- | --- |
| 1 | Explicit replacement | `switched/migrated/moved/changed from X to Y`, `replaced X with Y`, `uses Y instead of X` | older knowledge about X → SUPERSEDED |
| 2 | Explicit abandonment | `no longer uses X`, `stopped using X` | SUPERSEDED, no successor recorded |
| 3 | Exclusive relationship | exclusive type gains a second target | SUPERSEDED if the memory states the present; otherwise UNRESOLVED |

Everything else produces no conflict.

Detection abstains when the named fragment does not resolve to a known entity,
when the candidate is newer than the trigger, when the candidate also mentions
the new entity, or when the trigger reads as a progress report.

Entity matching uses canonical name, every alias, and the `memory_entities`
link. Matching only the canonical name was a real bug found in testing — see
*Bugs found*.

---

## Relationship policies

```
EXCLUSIVE (2)      PREFERS, LOCATED_IN
NON-EXCLUSIVE (13) USES, BUILDS, WORKS_ON, INTERESTED_IN, OWNS, PART_OF,
                   CREATED, FOUNDED, WORKS_WITH, RELATED_TO, DEPENDS_ON,
                   INVOLVED_IN, HAS_GOAL
```

`USES` is deliberately non-exclusive. `Mai USES PostgreSQL` and `Mai USES Groq`
are simultaneously true; making USES exclusive would retire Mai's own database
the moment it learned about Groq.

A test asserts every relationship type is classified exactly once, in exactly
one set, so adding a type forces a deliberate decision.

**Verified with a mutation check.** `USES` was added to the exclusive set and
the suite was re-run: three tests failed, including
`test_two_tools_used_together_do_not_conflict`. The guard detects the
regression rather than describing the current state.

---

## Supersession preserves history

Nothing is deleted. `content`, `normalized_content`, `created_at`,
`importance_score` and `confidence_score` are never modified — a status column
moves, and that is all.

**Verified with a mutation check.** Supersession was changed to `DELETE` the
older memory: **15 tests failed**, including `test_nothing_is_ever_deleted`,
`test_the_original_text_is_never_modified` and
`test_supersession_is_traceable`.

Every decision is traceable. `KnowledgeConflict` records the older item, the
newer one where a successor exists, the resolution, the rule that fired, and
the memory that triggered it. `triggering_memory_id` uses `SET NULL` rather
than `CASCADE`: losing the cause must not erase the decision.

---

## Retrieval behaviour

| Query | Eligible | Verified by |
| --- | --- | --- |
| "What technology stack am I currently using for Mai?" | ACTIVE | returns Groq + PostgreSQL, **not** OpenRouter |
| "What provider did I use before Groq?" | ACTIVE + SUPERSEDED | returns OpenRouter |
| Same subject, both phrasings | — | the retired memory appears in one and not the other |
| `HISTORICAL_RETRIEVAL_ENABLED=false` | ACTIVE | history stays hidden |
| any | never ARCHIVED | — |

A filter only — no scoring change, no re-ranking. Stage 2D's ranking is
untouched.

Historical intent is keyword-based and deliberately narrow: past-tense
auxiliaries (`was`, `were`, `did`) are excluded, so "what was my name?" is not
treated as historical.

---

## Context safety

Stage 3A's `context_role` and Stage 3B's reference block are unchanged and
still enforced. Stage 3C added a regression suite around them.

Four structural invariants, asserted for every payload:

1. never becomes a system message;
2. appears only in the reference block or the user's own turn;
3. never changes any message's role;
4. never displaces or alters the current user message.

Retrieved knowledge still enters the prompt through exactly one path —
Stage 2D → Stage 3A → Stage 3B — re-verified after Stage 3C by spying on the
retired Stage 2D renderer during a chat turn and by re-running the AST scan
that asserts which modules may construct an `LLMMessage`.

---

## Current user priority

Stored "Mai uses OpenRouter", current message "I switched to Groq":

- the message is preserved byte-for-byte;
- it is the final `user` message;
- it appears exactly once;
- the stale memory sits in a reference block that declares itself possibly out
  of date;
- **no database write occurs** — conflict-link count is identical before and
  after.

Verified for the chat path and for `POST /api/prompt/debug`.

---

## Background integration

```
memory extraction → entity extraction → relationship extraction
                                              ↓
                                     conflict evaluation  (Stage 3C)
```

One task, four stages, sequential — no second competing writer. Evaluation runs
last because detection resolves entity names and inspects relationship shape.

**Zero additional model calls, structurally.** `run_conflict_evaluation` takes
no provider argument. A test asserts nothing under `app/knowledge/` imports
`app.llm`; another asserts nothing under `app/retrieval`, `app/context`,
`app/prompt` or `app/services` imports `app.knowledge`, so the request path
cannot mutate lifecycle state because it cannot reach the writer.

The one synchronous request-path generation call is unchanged.

---

## Concurrency

Handled by the database, not by application checks:

- UNIQUE on each ordered conflict pair — a duplicate surfaces as
  `IntegrityError`, counted as `links_already_present`, not an error;
- SAVEPOINT per decision — one undecidable outcome rolls back alone;
- idempotent status updates — the final state does not depend on interleaving.

Verified against a **file-backed** SQLite database (the in-memory fixture
shares one connection via StaticPool and cannot reproduce contention): six
concurrent turns proposing the same supersession produce no duplicate link, no
self-referential link, no orphaned reference, and a deterministic final state.

One collision is documented: `uq_relationships_triple` includes `status`, so a
status move can collide with an existing superseded row carrying the same
triple. It surfaces as `IntegrityError` and the relationship stays ACTIVE —
consistent, not half-written.

---

## Failure isolation

| Failure | Result |
| --- | --- |
| Detection raises | nothing written; knowledge stays ACTIVE |
| One decision cannot apply | it rolls back alone; others proceed |
| Status update collides | link not written; item stays ACTIVE |
| Supersession would close a cycle | refused and counted |
| Whole evaluation task fails | earlier stages keep their committed state |

Chat is unaffected in every case — evaluation runs after the response is sent,
on its own session. Verified: with evaluation forced to fail, the turn returns
201, memories and entities are still stored, and `Conflict evaluation task
failed` is logged.

The preferred failure state is **ACTIVE and unresolved** over partially
mutated, and a test asserts a failed evaluation leaves both memories active
rather than one half-retired.

---

## Security tests executed

| Class | Payloads | Vector |
| --- | --- | --- |
| Instruction override | 2 | memory, current message |
| System-prompt extraction | 1 | memory |
| Secret extraction | 1 | memory |
| Role reassignment | 2 | memory, current message |
| Fake developer instruction | 2 | memory |
| Context escaping | 2 | memory, entity description |
| Destructive request | 1 | memory |

Eleven payloads × four structural invariants, plus dedicated tests for hostile
entity names, hostile entity descriptions, hostile relationship targets, and
`context_role` preservation. Additional checks: the lifecycle debug endpoint
exposes no API key, database URL or system prompt; lifecycle logs carry no
memory text.

Protections are architectural. Nothing depends on a model choosing to refuse.

---

## Database changes

Migration `0006_knowledge_conflicts` — **purely additive.** One new table. No
existing table altered, no column dropped, no row modified. Existing rows need
no backfill: the absence of a link correctly means "no conflict detected".

| Verification | Status |
| --- | --- |
| SQLite upgrade → head | **executed, passed** |
| SQLite downgrade → 0005 | **executed, passed** |
| SQLite re-upgrade (round trip) | **executed, passed** |
| Full suite against migrated schema | **executed, 896 passed** |
| **PostgreSQL upgrade** | **NOT VERIFIED — not executed** |
| **PostgreSQL downgrade** | **NOT VERIFIED — not executed** |

SQLite success is not PostgreSQL verification and is not presented as such.
The migration was written to minimise PostgreSQL-specific risk: no enum was
extended, which is precisely why the `CONFLICTED` status was rejected.

---

## Observability

`Knowledge conflicts evaluated` records `memory_id`, `conflicts_detected`,
`memories_superseded`, `relationships_superseded`, `unresolved`,
`links_created`, `links_already_present`, `cycles_prevented`,
`status_updates_failed`, `duration_ms`.

Retrieval logs `historical_intent` on every query.

**Ids and counts only** — memory text never appears in a lifecycle log line.
Two tests enforce this, plus the existing Stage 3B checks that no API key,
database URL or prompt body reaches the logs.

---

## Tests

| | |
| --- | --- |
| **Total** | **896** |
| **Passed** | **896** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Baseline before Stage 3C | 748 |
| Added by Stage 3C | 148 |

| New file | Tests | Covers |
| --- | --- | --- |
| `test_knowledge_policies.py` | 53 | replacement patterns, exclusivity policy, abstention cases, structural purity |
| `test_knowledge_lifecycle.py` | 22 | supersession, non-conflict, ambiguity, cycles, cascade, check constraints |
| `test_knowledge_retrieval.py` | 28 | historical intent, current vs historical queries, end-to-end evolution, read-only request path |
| `test_knowledge_injection_safety.py` | 36 | eleven payloads × structural invariants, hostile entities and relationships, single knowledge path |
| `test_knowledge_concurrency.py` | 9 | concurrent evaluation, invariants, failure isolation, pipeline ordering, safe logging |

No existing test needed modification. Full regression across Stages 1, 2A, 2B,
2C, 2D, 3A, 3B and 3C: **896 passed**.

---

## Bugs found

1. **Alias surface forms were missed at memory level.** `_retire_memories_mentioning`
   matched only the entity's canonical `normalized_name`. A memory saying "Mai
   uses postgres" would stay ACTIVE after an explicit migration away from
   PostgreSQL, because the text contains `postgres` and not `postgresql`.
   Caught by `test_an_alias_resolves_to_the_canonical_entity`.

2. **Bulk status updates left stale ORM instances.** `synchronize_session=False`
   meant an object already loaded in the session kept reporting ACTIVE after
   being superseded. Harmless in the background task, which closes its session
   immediately, but a trap for any future caller reading back in the same
   session.

3. **The `instead of` pattern swallowed the subject.** "mai uses groq instead
   of openrouter" captured `new="mai uses groq"`, which would then resolve to
   the entity `Mai` and supersede the wrong thing.

4. **The conflict schema could not express abandonment.** The original check
   constraint required both ends of a link, so "no longer uses OpenRouter" — a
   real lifecycle event with no successor — was unrepresentable.

---

## Fixes applied

1. Detection now matches **every surface form** — canonical name plus all
   aliases — and additionally unions the structured `memory_entities` link,
   which is more reliable than text when present.

2. Status updates use `synchronize_session="fetch"`: one extra single-row
   SELECT, and any loaded instance reports the new status.

3. The `instead of` pattern is anchored on the verb
   (`uses|prefers|chose|selected|…`), so the capture starts after it.

4. The check constraint now keys the *kind* off the populated `older_*` column
   and leaves `newer_*` nullable. A successorless retirement is still fully
   traceable through `triggering_memory_id`.

---

## Not verified

Stated explicitly rather than marked PASS:

- **PostgreSQL runtime and migration `0006` against PostgreSQL.** No PostgreSQL
  server exists on this machine. SQLite upgrade, downgrade and re-upgrade were
  executed and passed; that is not equivalent.
- **Docker.** Configuration exists, has never been executed.
- **Next.js frontend.** Written, never built or run.

Unchanged from earlier stages. Stage 3C adds one purely additive table and no
frontend surface, so nothing new is at risk — but nothing new was proven.

---

## Known limitations

- No advanced semantic contradiction reasoning.
- No LLM-based conflict judge — zero model calls anywhere in Stage 3C.
- No embeddings, no vector search, no semantic temporal reasoning.
- Ambiguous conflicts remain unresolved — the intended outcome, not a gap.
- **The bare-restatement case is not detected**: "uses X" then "uses Y" with no
  linking language produces no conflict, because it is textually
  indistinguishable from two complementary tools. Documented in the
  architecture doc with the reasoning.
- Historical intent is keyword-based; it will miss "what did I use when I
  started Mai" and fire on "tell me about my old laptop".
- Memory-level supersession is textual, so a memory mentioning a replaced
  entity in passing can be marked historical. It is never deleted and stays
  reachable through historical queries and the debug API.
- Docker, PostgreSQL and the frontend remain unverified.

---

## Acceptance criteria

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Historical knowledge not automatically deleted | PASS |
| 2 | Lifecycle states exist and are written | PASS |
| 3 | Deterministic direct supersession works | PASS |
| 4 | Newer knowledge supersedes older | PASS |
| 5 | Historical knowledge remains traceable | PASS |
| 6 | Normal retrieval prefers active knowledge | PASS |
| 7 | Historical queries can retrieve superseded knowledge | PASS |
| 8 | Non-exclusive relationships create no false conflicts | PASS |
| 9 | Ambiguous conflicts produce no invented certainty | PASS |
| 10 | Current user messages remain authoritative | PASS |
| 11 | Zero additional synchronous request-path LLM calls | PASS |
| 12 | Retrieval path does not mutate knowledge | PASS |
| 13 | Integrates into the existing background pipeline | PASS |
| 14 | Concurrent evaluation does not corrupt lifecycle state | PASS |
| 15 | Evaluation failure does not break chat | PASS |
| 16 | Stored malicious text stays untrusted reference data | PASS |
| 17 | Retrieved knowledge never becomes instructions | PASS |
| 18 | `context_role` boundaries intact | PASS |
| 19 | No second knowledge injection path | PASS |
| 20 | `PromptFormatter` remains the single formatting owner | PASS |
| 21 | No secrets in debug tools or logs | PASS |
| 22 | Database lifecycle invariants hold | PASS |
| 23 | Full regression passes | PASS (896) |
| 24 | Documentation complete | PASS |
| 25 | Unverified runtimes not falsely marked PASS | PASS — see *Not verified* |

Stage 4 was not started. The next step is the mandatory full-system Stage 3
Security Audit.
