# Mai — Stage 2C: Relationship Architecture

Stage 2C lets Mai understand how the things it knows about are connected.

---

## The governing principle

**Entities are individual things. Relationships describe how they connect.**

```
Memory:       "User decided to use PostgreSQL for Mai."
Entities:     Mai, PostgreSQL
Relationship: Mai ──USES──► PostgreSQL
```

Every relationship carries four things: a **source**, a **type**, a **target**,
and the **evidence** that justifies it. The memory remains the source
statement; relationships are derived structure, never a replacement.

Relationships are extracted from **stored memories**, not raw chat messages.
Memories are already filtered and validated, and their entities are already
resolved, which is what keeps relationship extraction low-noise.

---

## Pipeline

```
Conversation turn
   │
   ▼
Memory extraction   (Stage 2A)  ──►  memory COMMITTED
   │
   ▼
Entity extraction   (Stage 2B)  ──►  entities COMMITTED
   │
   ▼
Relationship extraction
   │
   ├─ fewer than 2 entities?  ──►  SKIP (model not even called)
   │
   ▼
JSON recovery → Pydantic validation → type normalization
   │
   ▼
confidence filter
   │
   ▼
resolve BOTH ends against EXISTING entities (Stage 2B resolver)
   │
   ├─ either end unknown?  ──►  REJECT (never creates an entity)
   │
   ▼
reuse an existing relationship, or create one
   │
   ▼
attach the memory as evidence
```

All three extraction stages run inside the **same** FastAPI background task, in
sequence. That is deliberate: a second concurrent background writer would race
the others on the same tables.

Each stage commits before the next begins, so a relationship failure can never
roll back an entity, a memory, or the chat turn.

**The ≥2-entity gate is also the cost control.** Most memories name fewer than
two entities, so the extra model call is skipped entirely rather than spent
producing nothing.

---

## Relationship types

Fifteen types, enforced by a database enum. A small controlled vocabulary is
the point — unlimited relationship strings make the data unusable.

| Type | Meaning |
| --- | --- |
| `USES` | X makes use of Y |
| `BUILDS` | X is building Y |
| `CREATED` | X made Y in the past |
| `WORKS_ON` | X works on Y without being its creator |
| `WORKS_WITH` | X collaborates with person Y |
| `INTERESTED_IN` | X is interested in Y, without commitment |
| `HAS_GOAL` | X wants to achieve Y |
| `PREFERS` | X favours Y over alternatives |
| `OWNS` | X owns Y |
| `PART_OF` | X is a component of Y |
| `DEPENDS_ON` | X requires Y to function |
| `FOUNDED` | X founded organisation Y |
| `LOCATED_IN` | X is situated in place Y |
| `INVOLVED_IN` | X participates in event Y |
| `RELATED_TO` | last resort, only when nothing else fits |

The disambiguation rules live in `app/relationships/prompts.py` and are given
to the model verbatim, so its labels and this document cannot drift apart.

---

## Direction

Relationships are **directional**, and the direction is a distinct claim:

```
Mai ──USES──► PostgreSQL          correct
PostgreSQL ──USES──► Mai          a different, wrong claim
```

The prompt opens with this rule and instructs the model to ask which entity
performs the action. Two worked-example traps are called out explicitly, because
the grammatical subject of a memory is often *not* the source:

```
"User decided to use PostgreSQL for local storage in Mai."
   -> Mai USES PostgreSQL          (the project uses the database)
   NOT User USES PostgreSQL        (the user made the decision; that is not
                                    itself a relationship worth recording)
```

The API keeps direction explicit too: `/api/entities/{id}/relationships`
returns `outgoing` and `incoming` as separate lists rather than merging them.

---

## Database schema

```
relationships                             relationship_evidence
─────────────                             ─────────────────────
id                UUID PK                 id              UUID PK
source_entity_id  UUID FK → entities.id   relationship_id UUID FK → relationships.id
                       ON DELETE CASCADE                       ON DELETE CASCADE
relationship_type ENUM                    memory_id       UUID FK → memories.id
target_entity_id  UUID FK → entities.id                         ON DELETE CASCADE
                       ON DELETE CASCADE  created_at      TIMESTAMPTZ
confidence_score  FLOAT
status            ENUM (active|superseded|archived)
created_at        TIMESTAMPTZ
updated_at        TIMESTAMPTZ
```

**Constraints and indexes**

| Constraint / index | Purpose |
| --- | --- |
| `(source, type, target, status)` **UNIQUE** | duplicate prevention, enforced under concurrency |
| `CHECK source_entity_id <> target_entity_id` | an entity cannot relate to itself |
| `CHECK 0.0 <= confidence_score <= 1.0` | last line of defence on model output |
| `(relationship_id, memory_id)` **UNIQUE** | one memory supports a relationship once |
| `ix_relationships_source_entity_id` / `_target_entity_id` | direction-aware lookups |
| `ix_relationships_type`, `(status, created_at)` | filtering and listing |
| `ix_relationship_evidence_memory_id` | "what does this memory support?" |

### Design decisions

- **Evidence is a table, not a column.** A single `source_memory_id` would
  force a second relationship row every time another memory supported the same
  claim. Instead one relationship accumulates evidence rows.

- **`status` is part of the unique key.** Two concurrent extractions cannot
  both insert the same active claim, and a later lifecycle stage can still
  archive one and record a new active one without colliding.

- **Uniqueness is enforced by the database, not just the application.** An
  application check cannot see another transaction's uncommitted insert. On a
  unique violation the service falls back to reusing the existing row and
  attaching evidence, so nothing is lost.

- **Deleting an entity removes its relationships** (cascade on both ends);
  deleting a **memory** removes only its evidence rows, leaving the
  relationship if other memories still support it; deleting a **relationship**
  removes its evidence and nothing else. Entities and memories are never
  deleted by relationship operations.

- **A seeded `User` entity.** Stage 2B deliberately does not extract "user" as
  an entity, but half the vocabulary (`INTERESTED_IN`, `PREFERS`, `HAS_GOAL`)
  is meaningless without a node for the user. Migration `0005` seeds a
  singleton `User` entity and relationship extraction always offers it as the
  implicit subject. It is seeded by migration rather than created at runtime
  because **the relationship system must never create entities**.

**Migration:** `0005_relationships.py`, revising `0004`. No earlier migration
was modified.

---

## Normalization

Models express the same connection many ways. The database only ever stores the
controlled vocabulary, so equivalent phrasings collapse before storage — or
they would become duplicate relationships.

| Raw label | Stored as |
| --- | --- |
| `UTILIZES`, `utilizes`, `runs on`, `powered by`, `built with`, `leverages` | `USES` |
| `is building`, `develops` | `BUILDS` |
| `collaborating with`, `partners with` | `WORKS_WITH` |
| `wants to`, `aims to`, `aspires to` | `HAS_GOAL` |
| `likes`, `favours` | `PREFERS` |
| `lives in`, `based in` | `LOCATED_IN` |
| `exploring`, `curious about` | `INTERESTED_IN` |

The mapping is a fixed table — deterministic, so the same phrasing always
resolves the same way. **An unmappable label is rejected, not guessed at.**
Missing a relationship is preferable to recording the wrong one.

---

## Entity resolution

Relationships may only join entities that already exist. Both ends are resolved
through the **Stage 2B resolver**, reusing its logic rather than duplicating it:

1. exact canonical name → 2. normalized name → 3. alias → 4. compact form.

If either end fails to resolve, the candidate is **rejected**. The relationship
system never creates an entity, so a hallucinated name simply drops the claim.

Two names can resolve to the same entity (`PostgreSQL` and
`postgresql database`); that is caught as a self-reference and rejected too.

---

## Deduplication

| Scenario | Behaviour |
| --- | --- |
| The identical triple extracted again | reuse; attach evidence |
| `UTILIZES` after `USES` | normalizes to `USES`, reuses, attaches evidence |
| Same pair, different type (`USES` vs `DEPENDS_ON`) | two distinct relationships |
| Reversed direction | a distinct claim, stored separately |
| Repeats within one batch | collapsed before storage, most confident kept |
| The same memory re-extracted | no duplicate evidence row |

---

## Evidence

One relationship, many supporting memories:

```
Memory 1: "Mai uses PostgreSQL for local storage."
Memory 2: "PostgreSQL was selected as Mai's database."
              │
              ▼
   Mai ──USES──► PostgreSQL     evidence_count = 2
```

`/api/relationships/{id}/evidence` returns the memories themselves — the
statements that justify the claim, not the conversations they came from. This
is the data a later stage needs to answer *"why do you believe this?"*.

---

## Validation

The model proposes; the application decides. Every candidate passes:

1. **JSON recovery** — fences, prose prefixes, malformed and non-object payloads.
2. **Pydantic validation** — type membership, name presence, confidence range.
3. **Type normalization** — synonyms mapped, unknown labels rejected.
4. **Self-reference rejection** — before *and* after entity resolution.
5. **Entity existence** — both ends must already exist.
6. **Confidence filter** — below `RELATIONSHIP_MIN_CONFIDENCE`, discarded.
7. **Batch cap** — at most `RELATIONSHIP_EXTRACTION_MAX_PER_MEMORY`.
8. **Unknown-field rejection** — invented fields are dropped.

**The LLM never writes to the database.** `RelationshipService` is the only writer.

---

## Transaction integrity

A relationship and its first evidence row are written **in one savepoint**.
Either both land or neither does — a relationship can never exist without the
evidence that justifies it, and a failure on one candidate cannot abort the
others.

---

## Failure isolation

**Relationship extraction failure can never break chat, a conversation, a
memory, or an entity.** By the time it runs, all of those are committed.

| Failure | Behaviour |
| --- | --- |
| Model times out / rate limited / rejects the key | logged, no relationships, everything upstream intact |
| Malformed or non-JSON output | logged, nothing stored |
| Invalid type, confidence or self-reference | that candidate dropped, others continue |
| Names an entity that does not exist | candidate rejected; no entity created |
| Database write fails | savepoint rolled back; no orphan relationship or evidence |
| Entity extraction failed upstream | fewer than two entities, so extraction is skipped |
| Memory extraction failed upstream | no memory, so nothing runs |
| The chat turn itself failed | none of the three stages run |

`run_relationship_extraction` never raises and contains failures **per memory**.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RELATIONSHIP_EXTRACTION_ENABLED` | `true` | Extract relationships after entities |
| `RELATIONSHIP_MIN_CONFIDENCE` | `0.7` | Minimum confidence to store |
| `RELATIONSHIP_EXTRACTION_MAX_PER_MEMORY` | `10` | Cap per memory |
| `RELATIONSHIP_EXTRACTION_TEMPERATURE` | `0.1` | Extraction is classification |
| `RELATIONSHIP_EXTRACTION_MAX_TOKENS` | `1024` | Cap on responses |

Relationships also sit under the `MEMORY_ENABLED` master switch, since they are
derived from memories.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/relationships` | List; filter by type, source, target, status |
| `GET` | `/api/relationships/{id}` | One relationship with its evidence count |
| `GET` | `/api/relationships/{id}/evidence` | The memories supporting it |
| `DELETE` | `/api/relationships/{id}` | Delete the claim; entities and memories kept |
| `GET` | `/api/entities/{id}/relationships` | Incoming and outgoing, kept separate |

---

## Provider abstraction

```
RelationshipService → RelationshipExtractor → LLMProvider (ABC) → active provider
```

No relationship module imports a provider implementation, references Groq, or
reads an API key. No interface change was required.

---

## Current limitations

- **No graph database and no Neo4j.** Relationships are rows in PostgreSQL.
- **No multi-hop or graph reasoning.** There is no traversal beyond one hop.
- **No embeddings, vector search or semantic retrieval.**
- **No RAG.**
- **No contradiction resolution.** `User PREFERS Groq` and `User PREFERS Claude`
  can both exist; nothing is superseded automatically. The `status` column
  exists for the lifecycle stage that will handle this.
- **No temporal reasoning.** Relationships have no validity period, so a claim
  that was true last year looks identical to one that is true now.
- **Confidence is never revised.** It is set once at creation; further evidence
  adds rows but does not raise it.
- **Extraction quality depends on the model.** Direction errors and
  over-general `RELATED_TO` labels were both observed during development and
  reduced by prompt work, not eliminated.
- **One extra model call per memory with two or more entities**, on top of the
  chat, memory and entity calls.
