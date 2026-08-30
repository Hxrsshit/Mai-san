# Mai — Stage 3C: Conflict Resolution & Context Safety

Knowledge changes. A memory recorded six months ago can be true history and
false present, and Stage 3C is what lets Mai tell the difference — without ever
destroying the record.

---

## What already existed

Reading the schema first changed the design substantially.

`MemoryStatus` and `RelationshipStatus` already carried **ACTIVE / SUPERSEDED /
ARCHIVED** from Stages 2A and 2C, and Stage 2D already filtered
`status == ACTIVE` in every query. The lifecycle states existed; nothing wrote
them, and nothing explained them.

So Stage 3C did not need a state machine. It needed two things:

1. something to **decide** when a status should move, and
2. a record of **why** — "what replaced this?" had no answer anywhere.

```
Stage 2A/2C gave us:        status columns, never written
Stage 2D gave us:           ACTIVE-only retrieval
Stage 3C adds:              a decider, an explanation, and historical queries
```

---

## The central difficulty

The specification asks for two things that look identical to a schema:

```
Mai USES OpenRouter   ->  Mai USES Groq      # a replacement: supersede
Mai USES PostgreSQL   +   Mai USES Groq      # both true: leave alone
```

Same source, same relationship type, two targets, opposite correct answers.
No amount of inspecting entity types or timestamps separates them —
`PostgreSQL` and `Redis` are both `technology` and both simultaneously true.

What separates them is **language in the new memory**. Someone who replaced a
provider says so ("switched from OpenRouter to Groq"); someone adding a second
tool does not.

That observation is the design. Supersession is driven by explicit replacement
language, not by two targets coexisting. Coexistence alone is flagged only for
the small set of relationship types where two simultaneous targets are
genuinely incoherent — and even there the outcome is "unresolved", not a
winner.

The governing bias, stated in the specification and worth repeating: **a false
conflict is worse than a missed one.** A missed conflict leaves a stale memory
un-ranked. A false conflict hides true knowledge behind a "historical" label.

---

## Lifecycle model

```
                       new memory stored
                              │
                              ▼
                   ConflictDetector (read-only)
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  no conflict          SUPERSEDED link       UNRESOLVED link
  (the common          + older row's         (both rows stay
   case)                status moves          ACTIVE)
        │                     │                     │
        ▼                     ▼                     ▼
     ACTIVE              historical            both ACTIVE,
                         (retrievable via      uncertainty
                          historical queries)  recorded
```

`ARCHIVED` exists on both enums and is never written by Stage 3C. It means
"do not surface this", and no query intent overrides it.

### Why a link table instead of a `CONFLICTED` status

The obvious alternative was a fourth enum value. It was rejected for three
reasons:

- It answers *is this contested?* but not *by what?* — traceability would need
  a second table anyway.
- Adding a value to a PostgreSQL enum needs `ALTER TYPE … ADD VALUE`, which
  carries transactional restrictions. PostgreSQL is **not verified** on this
  machine, so an untestable migration hazard is a poor trade for no benefit.
- An unresolved conflict must leave *both* items retrievable. A status marking
  one of them as special already implies a winner — exactly the invented
  certainty the design is meant to avoid.

So: `knowledge_conflicts`, one row per detected conflict. Memories and
relationships get their own column pairs with real foreign keys rather than a
polymorphic id, because a conflict never crosses the two kinds. That buys
`ON DELETE CASCADE`, which is what keeps lifecycle links from being orphaned.

The `newer_*` column is nullable: "no longer uses OpenRouter" retires knowledge
without naming a successor, and that is still a lifecycle event worth
recording. `triggering_memory_id` uses `SET NULL`, not `CASCADE` — losing the
memory that caused a decision must not erase the decision.

---

## Conflict detection rules

Three rules, applied in order. Everything else produces no conflict.

| # | Rule | Trigger | Result |
| --- | --- | --- | --- |
| 1 | **Explicit replacement** | new memory names both sides: `switched/migrated/moved/changed from X to Y`, `replaced X with Y`, `uses Y instead of X` | older relationships targeting X, and older memories mentioning X, → **SUPERSEDED** |
| 2 | **Explicit abandonment** | new memory names only what stopped: `no longer uses X`, `stopped using X` | same, with **no successor** recorded |
| 3 | **Exclusive relationship** | an exclusive type gains a second target | **SUPERSEDED** if the memory states the present (`now`, `currently`); otherwise **UNRESOLVED**, both stay ACTIVE |

### Relationship policies

```
EXCLUSIVE (2)                NON-EXCLUSIVE (13)
─────────────                ──────────────────
PREFERS                      USES, BUILDS, WORKS_ON, INTERESTED_IN,
LOCATED_IN                   OWNS, PART_OF, CREATED, FOUNDED,
                             WORKS_WITH, RELATED_TO, DEPENDS_ON,
                             INVOLVED_IN, HAS_GOAL
```

The exclusive set is deliberately tiny. **`USES` is not in it** — that single
decision is what stops Mai forgetting its own database the moment it learns
about Groq. A test asserts every relationship type is classified exactly once,
so adding a type forces a deliberate decision rather than a silent default.

### Guards against over-reach

Rule 1 and 2 both abstain when:

- the named fragment does not resolve to a known entity (acting on an
  unresolved name would supersede by coincidence);
- the candidate memory is *newer* than the trigger;
- the candidate memory also mentions the **new** entity — it is already
  describing the change rather than the old state;
- the trigger reads as a progress report (`completed`, `shipped`, `paused`),
  in which case only the structural rule 3 is trusted.

Entity matching uses **every surface form**, canonical name and aliases both,
plus the `memory_entities` link where it exists. A memory saying "Mai uses
postgres" is about PostgreSQL, and matching only the canonical name would leave
it active after an explicit migration away from it.

### Type coverage against the specification

| Spec type | Example | Behaviour |
| --- | --- | --- |
| 1 — direct replacement | "switched from OpenRouter to Groq" | Rule 1 → superseded |
| 2 — preference change | "now prefers hybrid work" | Rule 3 → superseded |
| 3 — technology migration | "migrated Mai from OpenRouter to Groq" | Rule 1 → superseded |
| 4 — temporal change | "working on Project A" → "completed Project A" | no conflict — `WORKS_ON` is non-exclusive and the trigger reads as progress |
| 5 — true contradiction | "lives in Hyderabad" / "lives in Bangalore" | Rule 3 → **unresolved**, both stay ACTIVE |

**A documented gap.** The specification's Type 1 example uses bare phrasing —
"User uses OpenRouter." then "User uses Groq." with no linking language. Stage
3C produces **no conflict** for that, deliberately: it is textually
indistinguishable from `Mai USES PostgreSQL` + `Mai USES Groq`, which must not
conflict. Detecting it would require semantics the system does not have, and
guessing would break the more important case. The bare form is caught as soon
as any replacement language appears in a later memory.

---

## Retrieval behaviour

Stage 2D was made lifecycle-aware without being redesigned. Both retrievers
gained a `statuses` parameter defaulting to `(ACTIVE,)` — exactly what every
earlier stage hard-coded.

| Query | Eligible statuses |
| --- | --- |
| "What provider am I using?" | ACTIVE |
| "What provider did I use **before**?" | ACTIVE + SUPERSEDED |
| anything, `HISTORICAL_RETRIEVAL_ENABLED=false` | ACTIVE |
| anything | never ARCHIVED |

No scoring change, no re-ranking — a filter only. Stage 2D's ranking is
untouched.

### Historical intent

Deterministic keyword matching on the normalised query:

```
markers:  previously  before  earlier  formerly  historically
          originally  past  prior  old  older  ago
phrases:  "used to"  "no longer"  "in the past"  "back then"
```

Deliberately narrow. Past-tense auxiliaries (`was`, `were`, `did`) are
**excluded** — "what was my name?" asks about the present, and treating it as
historical would surface retired knowledge on ordinary questions.

This is a keyword test, not an intent model, and both failure modes are safe: a
miss gives normal current-state retrieval, and a false positive merely widens
the candidate pool — ranking still decides what survives. One case worth
knowing: `"use to"` is not `"used to"`, so "what did I use to run inference"
correctly reads as present-tense.

---

## Context precedence

The authority hierarchy, unchanged in prompt position and now enforced in
lifecycle:

1. system / application instructions
2. **current user message**
3. recent conversation
4. active long-term knowledge
5. historical / superseded knowledge
6. archived knowledge (never retrieved)

The current message stays authoritative by construction: it is preserved
byte-for-byte, is the final `user` turn, and old knowledge cannot displace it
because it lives in a reference block that says it may be stale.

**Nothing is written during a turn.** If the current message contradicts stored
knowledge, the contradiction is resolved by *position*, not by mutation. The
background pipeline may record the new fact later; retrieval and assembly never
do.

---

## Context safety

Stage 3A's `context_role` and Stage 3B's reference block are unchanged and
still enforced. Stage 3C adds a regression suite around them, because stored
memories are now provably long-lived and may contain anything the user typed.

Four structural invariants, checked against every payload:

1. it never becomes a system message;
2. it appears only inside the reference block or the user's own turn;
3. it never changes any message's role;
4. it never displaces or alters the current user message.

Payload classes covered: instruction override, system-prompt extraction, secret
extraction, role reassignment, fake developer instructions, context escaping,
and destructive requests — through memories, entity names, entity descriptions,
relationship targets, and the current message.

The protections are architectural, not behavioural. Nothing here depends on a
model choosing to refuse.

---

## Background pipeline integration

```
assistant response stored
        │
        ▼
memory extraction        (model call)
        ▼
entity extraction        (model call)
        ▼
relationship extraction  (model call)
        ▼
conflict evaluation      ← Stage 3C, NO model call
```

One task, four stages, sequential — no second competing writer. Conflict
evaluation runs **last** because detection resolves entity names and inspects
relationship shape; running it earlier would judge an incomplete picture.

It takes no provider argument at all, which makes "adds zero model calls"
structural rather than a promise. A test asserts nothing under `app/knowledge/`
imports `app.llm`, and another asserts nothing under `app/retrieval`,
`app/context`, `app/prompt` or `app/services` imports `app.knowledge` — the
request path cannot mutate lifecycle state because it cannot reach the writer.

---

## Concurrency

Background evaluations run on separate sessions and cannot see each other's
uncommitted rows, so application-level checks lose the race. The database
decides:

- **UNIQUE** on each ordered conflict pair. A duplicate surfaces as
  `IntegrityError`, which is expected, counted as `links_already_present`, and
  not an error.
- **SAVEPOINT per decision.** One undecidable outcome rolls back alone; the
  others still apply, and the extraction that ran before them is never
  endangered.
- **Idempotent status updates.** `ACTIVE → SUPERSEDED` applied twice is the
  same as once, so the final state does not depend on interleaving.

One collision is worth naming: `uq_relationships_triple` includes `status`, so
moving a relationship to SUPERSEDED can collide with an existing superseded row
carrying the same triple. That surfaces as `IntegrityError` and the
relationship stays ACTIVE — consistent, not half-written.

---

## Failure handling

| Failure | Result |
| --- | --- |
| Detection raises | nothing written; knowledge stays ACTIVE |
| One decision cannot apply | that decision rolls back; the others proceed |
| Status update collides | link not written; item stays ACTIVE |
| Supersession would close a cycle | refused and counted |
| Whole evaluation task fails | memories, entities and relationships keep their committed state; chat is unaffected |

Chat cannot be affected in any case: evaluation runs after the response has
been sent, on its own session. Every failure logs at ERROR.

The preferred failure state throughout is **ACTIVE and unresolved** rather than
partially mutated — knowledge that is merely un-retired is recoverable, and
knowledge in an inconsistent lifecycle state is not.

---

## Observability

`Knowledge conflicts evaluated` records, per memory:

```
memory_id  conflicts_detected  memories_superseded  relationships_superseded
unresolved  links_created  links_already_present  cycles_prevented
status_updates_failed  duration_ms
```

Retrieval additionally logs `historical_intent` on every query, so it is
visible when a question reached into the past.

**Ids and counts only.** Memory text never appears in a lifecycle log line —
personal knowledge does not belong in a system with different retention and a
wider audience than the database it came from. Two tests enforce this.

---

## Debug API

`GET /api/knowledge/debug/{memory_id}` answers "why is this historical, and
what replaced it?" — status, timestamps, and three link lists from the memory's
own point of view: `superseded_by`, `supersedes`, `triggered`.

Read-only, no model call, and it exposes nothing beyond what `/api/memories`
already returns: no scores, no provider configuration, no system prompt, no
credentials.

---

## Database changes

One new table, `knowledge_conflicts` (migration `0006`). **No existing table is
altered, no column dropped, no row modified.** Existing rows need no backfill —
the absence of a conflict link correctly means "no conflict detected".

Upgrade, downgrade and re-upgrade were executed against SQLite. **PostgreSQL
was not executed** and is not claimed. Downgrade drops only derived metadata:
memories and relationships keep their content and status, so it removes the
explanation of why something is historical, never the history itself.

---

## Known limitations

- **No semantic contradiction reasoning.** Two statements that conflict in
  meaning but share no replacement language are not detected.
- **No LLM conflict judge**, by design. Stage 3C adds zero model calls
  anywhere.
- **The bare-restatement case is not detected** — "uses X" then "uses Y" with
  no linking language. Documented above with the reason.
- **Historical intent is keyword-based.** It will miss "what did I use when I
  started Mai" and fire on "tell me about my old laptop".
- **Memory-level supersession is textual.** A memory mentioning a replaced
  entity in passing can be marked historical. It is never deleted and remains
  reachable through historical queries and the debug API.
- **No embeddings, no vector search, no temporal reasoning.**
- **Ambiguous conflicts stay unresolved** — that is the intended outcome, not a
  gap to close.
- **Docker, PostgreSQL runtime and the frontend remain unverified** — unchanged
  from earlier stages, and not executed on this machine.
