# Mai — Stage 2B: Entity Architecture

Stage 2B lets Mai recognise the identifiable things its memories talk about,
and recognise them as *the same thing* across different memories.

---

## The governing principle

**Memories are statements. Entities are the things named inside them.**

```
Memory:   "User decided to use PostgreSQL for Mai."
Entities: PostgreSQL (technology), Mai (project)
```

The memory remains the source statement and is never replaced. Entities add
structure around what memories mention, joined through a link table. Delete an
entity and the memories survive untouched.

Entities are extracted from **stored memories**, not from raw chat messages.
Memories are already filtered and validated, which is what keeps entity
extraction low-noise: the model never sees "Hey" or "What is Python?".

---

## Pipeline

```
Conversation turn
   │
   ▼
Memory extraction  (Stage 2A)  ──►  memory COMMITTED
   │
   ▼
Entity extraction  (own session, per memory)
   │
   ▼
JSON recovery → Pydantic validation → confidence filter
   │
   ▼
Normalization  ("PostgreSQL database" → "postgresql")
   │
   ▼
Resolution     (exact → normalized → alias → compact)
   │
   ├── found    → reuse the existing entity
   └── not found → create a new one
   │
   ▼
Aliases (only when unambiguous)
   │
   ▼
memory ↔ entity link
```

Both memory and entity extraction run inside the **same** FastAPI background
task, in sequence. That is deliberate: a second concurrent background writer
would race the first on the same tables, which is precisely the bug the Stage
2A audit had to fix.

Memories are committed *before* entity extraction begins, so entity work can
never roll back a memory.

---

## Entity types

Ten types, enforced by a database enum. Deliberately coarse — a granular
taxonomy invites inconsistent labels.

| Type | Meaning | Example |
| --- | --- | --- |
| `person` | a named individual | John Doe |
| `company` | a named business | Groq, Anthropic |
| `organization` | a named non-commercial body | a university, an NGO |
| `project` | a named body of work someone is building | Mai |
| `product` | a named commercial offering or model | Claude, ChatGPT |
| `technology` | a named tool, language, framework, database | PostgreSQL, FastAPI |
| `place` | a named geographic location | Bangalore |
| `concept` | a named field, domain or practice | AI Product Development |
| `event` | a named occurrence | Stage 1 completion |
| `other` | identifiable, but none of the above | |

### Classification rules

The specification notes that something like "Groq" could reasonably be labelled
company *or* technology, and asks for one consistent approach. The rule:

> **A vendor is a `company`. Its released model is a `product`. An open tool,
> language or database is a `technology`.**

So: Groq → `company`, Claude → `product`, PostgreSQL → `technology`.

These rules live in `app/entities/prompts.py` and are given to the model
verbatim, so its labels and this document cannot drift apart.

---

## Database schema

```
entities                              entity_aliases
────────                              ──────────────
id               UUID  PK             id               UUID PK
canonical_name   VARCHAR(200)         entity_id        UUID FK → entities.id
                 display form                          ON DELETE CASCADE
normalized_name  VARCHAR(200)         alias            VARCHAR(200)
                 UNIQUE               normalized_alias VARCHAR(200) UNIQUE
entity_type      ENUM                 created_at       TIMESTAMPTZ
status           ENUM (active|archived)
description      TEXT NULL            memory_entities
created_at       TIMESTAMPTZ          ───────────────
updated_at       TIMESTAMPTZ          memory_id  UUID ─┐ composite
                                      entity_id  UUID ─┘ PRIMARY KEY
                                      mention_text VARCHAR(200) NULL
                                      created_at   TIMESTAMPTZ
```

**Indexes**

| Index | Serves |
| --- | --- |
| `entities.normalized_name` **UNIQUE** | resolution, and the concurrency guarantee |
| `entities.entity_type` | type filtering |
| `entities.(status, created_at)` | the default listing |
| `entity_aliases.normalized_alias` **UNIQUE** | alias resolution and ambiguity prevention |
| `entity_aliases.entity_id` | loading an entity's aliases |
| `memory_entities` composite PK | link uniqueness |
| `memory_entities.entity_id` | "which memories mention this?" |

### Design decisions

- **Two name columns.** `canonical_name` keeps its capitalisation
  ("PostgreSQL"); `normalized_name` is the lowercased matching form
  ("postgresql"). Flattening the canonical form would degrade every future
  display.

- **`normalized_name` is UNIQUE.** Application-level checks cannot see another
  transaction's uncommitted insert, so two concurrent extractions could
  otherwise both create the same entity. The database enforces what the
  application asserts. This is the Stage 2A audit's lesson applied up front
  rather than after the fact.

- **Resolution is by name, not by `(name, type)`.** Models classify the same
  thing inconsistently, and keying on both would fork "Groq" into two entities
  every time the label changed — the *common* failure. Keying on name alone
  risks merging a person named "Mai" with the project — the *rare* one. The
  first classification wins and later ones reuse it.

- **`normalized_alias` is globally UNIQUE.** An alias pointing at two entities
  makes resolution ambiguous, which is worse than having no alias. A clashing
  alias is refused, never reassigned.

- **`memory_entities` uses a composite primary key.** A duplicate link is
  impossible, so re-extracting a memory cannot pile up links.

- **Deleting an entity keeps the memories.** `ON DELETE CASCADE` on the link
  and alias tables removes the entity's links and aliases only. Deleting a
  *memory* likewise removes only its links — the entity survives, because it is
  knowledge rather than conversation content.

**Migration:** `0004_entities.py`, revising `0003`. No earlier migration was
modified.

---

## Normalization

Fully deterministic — no model involved, so resolution cannot drift between
runs. `normalize_name()` applies, in order:

1. Unicode NFKC, trim, collapse internal whitespace.
2. Strip edge punctuation (`"postgresql."` → `postgresql`).
3. Lowercase (matching form only).
4. Drop a leading article (`"the Mai project"` → `mai project`).
5. Drop a trailing descriptor noun — *database, project, platform, framework,
   language, company, tool, service*… (`"PostgreSQL database"` → `postgresql`)
   — but only when something identifying remains, so the entity "Database"
   does not vanish.
6. Drop a possessive (`"Mai's"` → `mai`).

| Input | canonical | normalized |
| --- | --- | --- |
| `postgresql` / `POSTGRESQL` / `PostgreSQL database` | *(as given)* | `postgresql` |
| `the Mai project` / `mai` / `MAI` / `Mai's` | *(as given)* | `mai` |

---

## Entity resolution

Conservative by design. The specification is explicit that a false merge is
worse than a duplicate, and that judgement drives every choice here.

| Step | Match | Example |
| --- | --- | --- |
| 1 | exact canonical name | `PostgreSQL` → `PostgreSQL` |
| 2 | normalized name | `postgresql`, `PostgreSQL database` → `PostgreSQL` |
| 3 | alias | `postgres` → `PostgreSQL` |
| 4 | compact form (punctuation removed) | `Postgre-SQL` → `PostgreSQL` |

**There is no fuzzy or similarity step.** Every similarity rule considered
would also merge `Claude` with `Claude Code`, which the specification names as
a pair that must stay separate. When in doubt, a separate entity is created.

Pairs that stay distinct, by construction: `Claude`/`Claude Code`,
`Mai`/`Mai Chen`, `AI`/`API`, `Groq`/`Grok`, `PostgreSQL`/`MySQL`.

---

## Aliases

An alias is only created when it is unambiguous. It is refused when it:

- equals the entity's own canonical name (adds nothing),
- already names a *different* entity,
- is already registered as an alias of another entity,
- fails name validation (too short, no letters, punctuation only).

At most five aliases per entity, and the model is instructed to propose them
only when the memory itself shows an alternative form or the abbreviation is
unambiguous and widely used. Large invented alias lists are exactly what this
guards against.

---

## Validation

The model proposes; the application decides. Every candidate passes:

1. **JSON recovery** — markdown fences, prose prefixes, malformed payloads,
   non-object payloads, empty responses.
2. **Pydantic validation** — type membership, name sanity, description length,
   confidence range.
3. **Name validation** — rejects fragments (`"x"`), punctuation (`"---"`), bare
   numbers (`"2024"`), and anything without a letter.
4. **Confidence filter** — below `ENTITY_MIN_CONFIDENCE`, discarded.
5. **Batch cap** — at most `ENTITY_EXTRACTION_MAX_PER_MEMORY`.
6. **Unknown-field rejection** — fields the model invents (`relationships`,
   `embedding`) are dropped rather than accepted.

**The LLM never writes to the database.** `EntityService` is the only writer.

### Descriptions

Optional, capped at 300 characters, and only kept when the source memory
supports them. The prompt explicitly instructs that an unsupported description
must be omitted rather than invented; placeholders (`null`, `unknown`, `N/A`,
`...`) are normalised to `None`.

---

## Transaction integrity

Each candidate is written inside its own **savepoint**: the entity, its aliases
and its memory link either all land or none do. A failure on one candidate
cannot leave a half-written entity behind, and cannot abort the others.

This depends on SAVEPOINT working correctly, which on the pysqlite driver
requires the connection configuration in `app/database/session.py` — a detail
the Stage 2A audit had to discover the hard way.

---

## Failure isolation

**Entity extraction failure can never break chat, a conversation, or a memory.**
By the time it runs, the turn has been answered and the memory committed.

| Failure | Behaviour |
| --- | --- |
| Model times out / rate limited / rejects the key | logged, no entities, memory intact |
| Malformed or non-JSON output | logged, no entities, memory intact |
| Candidate fails validation | that candidate dropped, others continue |
| Database write fails | savepoint rolled back, memory intact, logged |
| Unexpected exception | caught per memory, logged with traceback |
| The chat turn itself failed | no memory, so no entity extraction at all |

`run_entity_extraction` never raises, and contains failures *per memory*, so
one bad memory does not stop the others in the same turn.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENTITY_EXTRACTION_ENABLED` | `true` | Extract entities after each stored memory |
| `ENTITY_MIN_CONFIDENCE` | `0.7` | Minimum confidence to store an entity |
| `ENTITY_EXTRACTION_MAX_PER_MEMORY` | `10` | Cap per memory |
| `ENTITY_EXTRACTION_TEMPERATURE` | `0.1` | Extraction is classification, not generation |
| `ENTITY_EXTRACTION_MAX_TOKENS` | `1024` | Cap on extraction responses |

Entities live under the `MEMORY_ENABLED` master switch too: entities are
derived from memories, so disabling memory disables both.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/entities` | List; filter by `entity_type` and `status`; `limit`/`offset` |
| `GET` | `/api/entities/{id}` | Entity with its aliases and linked-memory count |
| `GET` | `/api/entities/{id}/memories` | Memories that reference the entity |
| `DELETE` | `/api/entities/{id}` | Permanent deletion — **memories are kept** |

The detail endpoint returns a *count* of linked memories rather than inlining
them; the dedicated endpoint returns the memories themselves, paginated.

---

## Provider abstraction

Entity extraction uses the same generic interface as everything else:

```
EntityService → EntityExtractor → LLMProvider (ABC) → active provider
```

No entity module imports a provider implementation, references Groq, or reads
an API key. It required no interface change at all — the `json_mode` argument
added in Stage 2A was sufficient.

---

## Current limitations

Stated plainly, since they define what the next stages must solve.

- **No entity-to-entity relationships and no knowledge graph.** Entities are
  linked to memories, never to each other. That is Stage 2C.
- **No embeddings, vector search or semantic retrieval.** Resolution is
  entirely lexical.
- **No web enrichment.** Descriptions come only from the source memory.
- **No advanced entity resolution.** No fuzzy matching, no coreference, no
  cross-type disambiguation.
- **Same-name collisions merge.** Because resolution is by name alone, a person
  named "Mai" and the project "Mai" would become one entity. This is the
  accepted cost of not forking entities on inconsistent classification.
- **First classification wins permanently.** A mislabelled entity keeps its
  original type; there is no reclassification path yet.
- **Type is never corrected and descriptions are never updated** once an entity
  exists — later mentions only add links.
- **No entity lifecycle.** Everything is `active`; `archived` is schema-only.
- **Extraction costs one model call per stored memory**, on top of the chat and
  memory calls, which counts against the provider's rate limit.
