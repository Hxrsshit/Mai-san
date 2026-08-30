# Mai — Stage 2D: Context Retrieval Architecture

Stage 2D makes Mai's stored knowledge *active*. Before every reply, relevant
memories, entities and relationships are retrieved, ranked and assembled into a
bounded context package.

---

## The governing principles

**Rank before truncating.** Candidates are scored first, then the budget is
applied to the ranking. The highest-value knowledge survives — never whichever
rows the database happened to return first.

**Zero additional model calls.** Retrieval is entirely database-driven. No
model is ever asked "what is relevant?". A turn still makes exactly one
request-path model call: chat generation.

**The current message always wins.** Retrieved knowledge is framed as
background, and its own text says so. If it contradicts what the user is saying
now, the user is right.

---

## Pipeline

```
USER MESSAGE
     │
     ▼
 normalize + extract keywords + n-gram phrases      (deterministic, no model)
     │
     ▼
 ENTITY MATCH  ── exact lookup on normalized_name / normalized_alias
     │
     ├──────────────┬────────────────────┐
     ▼              ▼                    ▼
 memories by    memories by         relationships
 entity link    keyword             (ONE HOP only)
     │              │                    │
     └──────────────┴────────────────────┘
                    │
                    ▼
            CANDIDATE POOL  (bounded)
                    │
                    ▼
                 RANKING     (weighted, deterministic)
                    │
                    ▼
              DEDUPLICATION
                    │
                    ▼
             CONTEXT BUDGET
                    │
                    ▼
             CONTEXT PACKAGE ──► chat prompt ──► LLM
```

Retrieval runs on the **request path**, before the model call. Memory, entity
and relationship *extraction* stay on the background path, untouched.

---

## Query normalization

The user's message is never modified — the model receives it exactly as typed.
Normalization produces a separate matching string.

| Step | Example |
| --- | --- |
| lowercase | `Mai` → `mai` |
| strip punctuation | `Postgres?` → `postgres` |
| collapse whitespace | `a   b` → `a b` |

## Keyword extraction

Deterministic, no NLP dependency: tokenize → drop stopwords → drop tokens
shorter than 3 characters → deduplicate, preserving order.

```
"What technology stack did we decide for Mai?"
  → ["technology", "stack", "decide", "mai"]
```

**Phrases** are contiguous word n-grams up to four words, generated
longest-first. These are what let a multi-word entity like
`AI Product Development` be matched exactly, without any fuzzy matching.

---

## Entity matching

Entity matches are the strongest retrieval signal, so matching is deliberately
conservative: **exact lookup of query n-grams** against `normalized_name` and
`normalized_alias`, both UNIQUE-indexed.

| Match | Strength |
| --- | --- |
| canonical name | 1.0 |
| alias | 0.9 |
| normalized form | 0.8 |

Two indexed queries total, regardless of query length. **No substring or fuzzy
matching**: `postgres-like` must not match `PostgreSQL`, and `Claud` must not
match `Claude`. The specification is explicit that false positives are worse
than missed matches.

*Known behaviour:* a query saying "Claude Code" will match a stored `Claude`
entity, because the word genuinely appears. That is mild over-retrieval, not a
fabricated match, and once a `Claude Code` entity exists the longer n-gram
matches it too.

---

## Memory retrieval

Three bounded sources, unioned and keyed by memory id. A memory found by
several sources keeps all of them, which is what lets the ranker reward
multi-signal matches.

| Source | Query | Index used |
| --- | --- | --- |
| entity link | join `memory_entities` on matched entity ids | `ix_memory_entities_entity_id` |
| relationship evidence | join `relationship_evidence` | `uq_relationship_evidence_pair` |
| keyword | `normalized_content LIKE` any keyword, OR'd into **one** query | `ix_memories_status_created_at` for the status filter |

Every source is `LIMIT`-ed to `RETRIEVAL_CANDIDATE_POOL_SIZE`, and the union is
capped again before ranking. **The memory table is never fully loaded.**

---

## Relationship retrieval

Relationships where a matched entity is the **source or the target** — one hop,
full stop. The far endpoint is resolved for display in a single batched query,
but is never fed back in to find further relationships.

```
query → Mai → (Mai USES PostgreSQL)          ✓ retrieved
             → (PostgreSQL USES PostGIS)     ✗ two hops, not retrieved
             → (PostGIS DEPENDS_ON AWS)      ✗ three hops, not retrieved
```

---

## Ranking formula

```
FINAL = 0.30 · text_relevance
      + 0.25 · entity_relevance
      + 0.15 · relationship_relevance
      + 0.15 · importance
      + 0.10 · confidence
      + 0.05 · recency
```

Every component is normalised to `[0, 1]` before weighting, so the weights are
directly comparable and the final score also lands in `[0, 1]`. All six are
configurable.

**Relevance dominates by design.** text + entity + relationship = **0.70**;
importance + confidence + recency = **0.30**. A highly important but irrelevant
memory therefore cannot outrank a directly relevant one — which the
specification requires and a test asserts.

| Component | Definition |
| --- | --- |
| `text_relevance` | share of query keywords present in the memory |
| `entity_relevance` | strength of the strongest matched entity linked to it |
| `relationship_relevance` | 0.6 if the memory is evidence for a relevant relationship |
| `importance` | `(importance_score − 1) / 9` — reuses Stage 2A's 1–10 field |
| `confidence` | `confidence_score` — already 0–1, reused directly |
| `recency` | `0.5 ^ (age_days / 180)` |

### Recency

A gentle exponential decay, half-life **180 days**:

| Age | Score |
| --- | --- |
| today | 1.00 |
| 30 days | 0.89 |
| 180 days | 0.50 |
| 1 year | 0.25 |

At weight 0.05 it can only ever break ties. "User is building Mai" stays
retrievable for months — old knowledge must not disappear merely for being old.

### Relationship scoring

| Situation | Relevance |
| --- | --- |
| both endpoints matched | 1.0 |
| one endpoint matched | 0.6 |
| only a supporting memory matched the query text | 0.3 |

Confidence modulates an already-relevant relationship (`×(0.8 + 0.2·conf)`); it
never promotes an irrelevant one.

### Tie-breaking

Applied in order, so ordering is total and reproducible:

1. final score → 2. entity match strength → 3. importance →
4. confidence → 5. recency → 6. memory id

---

## Deduplication

Affects the assembled context only. **Nothing is ever deleted.**

- **Memories** — by normalised text. (Stage 2A's
  `UNIQUE(memory_type, normalized_content)` already prevents same-type
  duplicates, so this catches the same statement stored under two types.)
- **Relationships** — by rendered triple, so several supporting memories
  produce one line.
- **Entities** — by id.

Ranked order is preserved, so the higher-scoring item is the one kept.

---

## Context budget

Applied *after* ranking, in two stages:

1. **Count budgets** — `RETRIEVAL_MAX_MEMORIES` / `_ENTITIES` /
   `_RELATIONSHIPS`.
2. **Character budget** — `RETRIEVAL_MAX_CONTEXT_CHARS`. Items are removed from
   the *bottom* of the ranking until the rendered block fits.

A section is never partially rendered. If the budget is smaller than the fixed
header and footer, the result is an empty context rather than a corrupted one.

---

## Context assembly

Database rows are never dumped into the prompt. Scores and signals stay out —
they are for debugging.

```
PERSONAL KNOWLEDGE CONTEXT

Relevant memories:
- User is building Mai as a personal AI environment.
- User decided to use PostgreSQL for local storage.
- User switched from OpenRouter to Groq for inference.

Relevant entities:
- Mai (project)
- PostgreSQL (technology)
- Groq (company)

Known connections:
- User BUILDS Mai
- User USES PostgreSQL

This is background knowledge from earlier conversations. Use it only when it is
relevant to the current request. If it conflicts with what the user is saying
now, the user is right -- treat this as possibly out of date. Never present it
as something the user just said, and never claim information that is not here
or in the conversation.
```

### Prompt order

```
1. system prompt
2. PERSONAL KNOWLEDGE CONTEXT      ← background
3. recent conversation
4. the user's current message      ← last, closest to the model's attention
```

Knowledge sits *before* the conversation deliberately, so the current turn
stays nearest the model. The block's own text states that the user's message
wins on conflict.

---

## Failure isolation

**Retrieval failure can never break chat.** Each source is wrapped
independently.

| Failure | Behaviour |
| --- | --- |
| Entity matching fails | memories still retrieved by keyword; source recorded in `degraded_sources` |
| Memory retrieval fails | entities and relationships still used |
| Relationship retrieval fails | memories and entities still used |
| Everything fails | empty package; chat proceeds on recent conversation alone |
| Retrieval raises unexpectedly | caught in `ChatService`; turn continues |

`RetrievalService.retrieve()` never raises.

---

## Performance

| Property | Measured |
| --- | --- |
| SELECTs per retrieval | **8, constant** from 10 to 500 memories |
| Candidate pool | bounded by `RETRIEVAL_CANDIDATE_POOL_SIZE` |
| Latency | median **4.2 ms** across live turns |
| Full table load | never |

The eight queries are: entity-by-name, entity-by-alias, relationships, endpoint
names (batched), evidence (batched), and the three memory sources. `noload()`
suppresses the `selectin` collections on `Entity.aliases` and
`Relationship.evidence`, which retrieval does not need.

**No schema changes.** Every query is served by an index that already exists,
so Stage 2D adds no migration.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RETRIEVAL_ENABLED` | `true` | Master switch |
| `RETRIEVAL_MAX_MEMORIES` | `10` | Memories in the context |
| `RETRIEVAL_MAX_ENTITIES` | `10` | Entities in the context |
| `RETRIEVAL_MAX_RELATIONSHIPS` | `10` | Relationships in the context |
| `RETRIEVAL_MAX_CONTEXT_CHARS` | `8000` | Hard cap on assembled size |
| `RETRIEVAL_CANDIDATE_POOL_SIZE` | `50` | Rows considered before ranking |
| `RETRIEVAL_WEIGHT_*` | see formula | Ranking weights |

Retrieval also sits under `MEMORY_ENABLED`, since it reads memories.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/retrieval/debug` | Every step: keywords, matches, candidates with scores, selections, assembled context, weights |
| `GET` | `/api/conversations/{id}/context-preview` | What would be assembled for this conversation now |

The debug endpoint shows candidates *and* selections, so it is visible not only
what was chosen but what was considered and rejected.

---

## Current limitations

- **No embeddings, vector database, or semantic search.** Retrieval matches
  words and entity names.
- **Recall is bounded by lexical overlap.** A query sharing no words and no
  entity with a memory will not find it, however related they are in meaning:
  "professional aspirations" does not retrieve "career toward AI product
  development". This is the single biggest limitation and it needs embeddings
  to close.
- **No RAG framework, no retrieval model, no query rewriting.**
- **One hop only.** Knowledge two relationships away is unreachable.
- **No contradiction resolution.** Conflicting memories can both be retrieved;
  the context block warns the model that background knowledge may be stale.
- **No temporal reasoning.** A superseded fact looks like a current one apart
  from its recency score.
- **No caching.** Every turn re-runs retrieval, which the measured 4 ms makes
  acceptable.
- **Retrieval quality depends on upstream structure.** If memories were
  extracted from independent conversations, the knowledge graph is fragmented —
  "User USES PostgreSQL" rather than "Mai USES PostgreSQL" — and a query about
  Mai will not reach it through relationships.
- **Stopword list is fixed and English-only.**
