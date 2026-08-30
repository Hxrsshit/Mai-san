# Mai — Stage 2A: Memory Architecture

Stage 2A gives Mai the ability to selectively remember meaningful information
from conversations. It is the foundation the later memory stages build on, so
it optimises for correctness and traceability rather than recall volume.

---

## The governing principle

**A message is not a memory.**

Most turns contain nothing worth remembering. Greetings, general-knowledge
questions and passing remarks are not memories. Extracting nothing is the
expected outcome for the majority of turns.

| User says | Result |
| --- | --- |
| "Hey" | no memory |
| "What is Python?" | no memory |
| "I want to move my career toward AI product building." | **goal** |
| "I prefer practical explanations." | **preference** |
| "I've decided to use PostgreSQL for Mai." | **decision** |

It is better to miss a memory than to pollute the store with hundreds of
useless facts.

---

## Pipeline

```
USER MESSAGE
   │
   ▼
NORMAL CHAT FLOW  ─────────────►  ASSISTANT RESPONSE RETURNED TO USER
   │                                        (the user is never blocked)
   ▼
TURN COMMITTED TO POSTGRESQL
   │
   ▼
BACKGROUND TASK STARTS  (own session, own transaction)
   │
   ▼
MEMORY EXTRACTION      ── LLM Provider Interface ──►  active provider
   │
   ▼
JSON PARSING           (fences, prose prefixes, malformed output)
   │
   ▼
PYDANTIC VALIDATION    (enum, score ranges, content sanity)
   │
   ▼
THRESHOLD FILTER       (importance ≥ 5, confidence ≥ 0.7)
   │
   ▼
DEDUPLICATION          (exact → conflict guard → similarity → restatement)
   │
   ▼
POSTGRESQL
```

Two ordering details matter:

1. **The response is returned before extraction starts.** Extraction runs as a
   FastAPI `BackgroundTask`, which Starlette executes after the response is
   sent. No queue, no Redis, no Celery.
2. **The turn is committed before extraction is queued.** FastAPI closes
   dependency scopes *after* background tasks run, so without an explicit
   commit the extraction task would open a second connection while the
   request's transaction was still uncommitted — it would not see the messages
   it is meant to analyse, and on SQLite the two connections deadlock.

---

## Memory types

Five categories, enforced by a database enum. Arbitrary types are impossible.

| Type | Meaning | Example |
| --- | --- | --- |
| `semantic` | durable factual information | "User works on AI-related projects." |
| `preference` | how the user likes things done | "User prefers concise explanations." |
| `goal` | something the user wants to achieve | "User wants to transition into AI product development." |
| `decision` | an explicit choice made | "User decided to use PostgreSQL as Mai's database." |
| `episodic` | a significant event or milestone | "User completed Stage 1 of the Mai project." |

---

## Database schema

```
memories
────────
id                     UUID          PK
content                TEXT          the standalone statement
normalized_content     VARCHAR(1000) UNIQUE per type; exact-duplicate lookup
memory_type            ENUM          semantic|preference|goal|decision|episodic
status                 ENUM          active|superseded|archived
importance_score       INTEGER       1-10, CHECK constrained
confidence_score       FLOAT         0.0-1.0, CHECK constrained
source_conversation_id UUID          FK → conversations.id  ON DELETE CASCADE
source_message_id      UUID NULL     FK → messages.id       ON DELETE SET NULL
created_at             TIMESTAMPTZ
updated_at             TIMESTAMPTZ
```

**Indexes**

| Index | Serves |
| --- | --- |
| `(status, created_at)` | the default listing: active memories, newest first |
| `(memory_type)` | type filtering and the per-type dedup window |
| `(source_conversation_id)` | provenance lookups and cascade deletes |
| `(memory_type, normalized_content)` **UNIQUE** | exact-duplicate lookup, and the database-level guarantee that concurrent extractions cannot both insert the same memory |

**Design decisions**

- **`CHECK` constraints on both scores.** Pydantic already rejects out-of-range
  values; the constraints are the last line of defence against bad model output
  reaching storage through any future path.
- **`normalized_content` is stored, not computed.** Exact-duplicate lookup is
  an indexed read against the whole table rather than a scan — and crucially
  not limited to the recent comparison window, so an identical memory from
  months ago is still recognised.
- **That index is UNIQUE on `(memory_type, normalized_content)`.** Application
  checks cannot see another transaction's uncommitted insert, so two
  concurrent extractions could both store the same memory. The database
  enforces the invariant the application asserts; a unique violation is caught
  and treated as a duplicate.
- **Conversation deletion cascades to memories.** A memory whose source
  conversation is gone cannot answer "where did you learn this?", and deleting
  a conversation should not leave derived facts behind. Later stages may
  revisit this with soft deletion.
- **`source_message_id` is nullable.** A memory may summarise a whole turn
  rather than one exact message, and a model-supplied id is only trusted if it
  actually resolves.
- **`status` ships with three values but only `active` is ever written.**
  `superseded` and `archived` exist so the lifecycle stage has somewhere to go
  without another migration.

**Migration:** `0002_memories.py`, revising `0001`. The Stage 1 migration is
untouched. Verified against a clean database, against an existing Stage 1
database containing data, and as a reversible downgrade.

---

## Extraction

### Provider agnosticism

The memory system never calls Groq. It goes through the same interface the
chat flow uses:

```
MemoryService → MemoryExtractor → LLMProvider (ABC) → active provider
```

`LLMProvider.generate_response()` gained one optional argument, `json_mode`.
It is a *capability request*, not a vendor format: providers with a native JSON
mode use it, providers without may ignore it and rely on the prompt. Callers
parse and validate either way, so ignoring it is always safe. The default is
`False`, so Stage 1 behaviour is unchanged.

### Input scope

Only the most recent turn is analysed — the user message plus the assistant
reply for context. The full conversation is never re-analysed, so cost does not
grow with conversation length.

**The assistant's reply is context only, never a source of facts.** The prompt
labels roles explicitly and states this rule twice, because the main failure
mode is attributing an assistant's inference to the user:

> User: "I think I might be interested in AI."
> Assistant: "You clearly want to become an AI entrepreneur."

Only the user's hedged interest is a supportable memory — and hedged language
is instructed to score low confidence, so it often should not be stored at all.

### Structured output, distrusted

The model proposes; the application decides. Every response passes through:

1. **JSON recovery** — handles markdown fences, prose prefixes, malformed
   payloads, non-object payloads, empty responses.
2. **Pydantic validation** — enum membership, `1 ≤ importance ≤ 10`,
   `0.0 ≤ confidence ≤ 1.0`, content length and non-blankness.
3. **Salvage** — a malformed batch does not discard well-formed entries; each
   candidate is re-validated individually.
4. **Batch cap** — at most 5 memories per turn. A longer list means the model
   is over-extracting, which is exactly what Stage 2A avoids.
5. **Unknown-field rejection** — fields the model invents (`entity_ids`,
   `embedding`) are dropped rather than accepted.

**The LLM never writes to the database.** `MemoryService` is the only writer.

---

## Deduplication

No embeddings and no vector database, so similarity is lexical. That has a
failure mode worth stating plainly:

```
"User completed Stage 1 of Mai."  vs  "User completed Stage 2 of Mai."   → 0.97
"User prefers practical explanations."  vs
"The user likes practical explanations."                                 → 0.83
```

The *different* facts score higher than the genuine reworded duplicate. **No
threshold can separate these**, so a threshold alone is not the algorithm.

Five steps, in order:

0. **Verb normalisation.** A small closed set of high-frequency synonyms
   (`likes`/`enjoys`/`loves` → `prefers`, `wishes`/`desires` → `wants`,
   `chose`/`selected` → `decided`) is collapsed before token comparison.
   Without it, a rewording that changes both the verb and the word order —
   "User prefers practical explanations." vs "User likes explanations that are
   practical." — scores only 0.52. Verbs of *different strength* are kept
   apart: "wants to learn Rust" and "decided to learn Rust" remain distinct.
1. **Exact match** on normalised content → duplicate. Indexed, unbounded, and
   backed by a unique constraint so it holds under concurrency.
2. **Conflict guard.** If the two texts disagree on a number or a proper noun
   present in one and not the other, they are different facts regardless of
   similarity. This is what handles `Stage 1`/`Stage 2`, `Bangalore`/`Berlin`,
   `PostgreSQL`/`Groq`.
3. **Similarity** — the max of a character-level ratio and content-token
   Jaccard, compared against `MEMORY_DEDUP_THRESHOLD` (0.82).
4. **Restatement** — high token containment (≥ 0.80) *and* comparable length
   (ratio ≥ 0.60). Catches reordered clauses with synonym swaps that
   character similarity misses. Both conditions are required: a short statement
   fully contained in a longer, more specific one scores 1.0 on containment
   alone, yet the longer one carries information the shorter lacks.

Fuzzy comparison (steps 2–4) is scoped to the **same memory type** and a
bounded window of the most recent `MEMORY_DEDUP_CANDIDATES` (50) memories,
because it compares text pairwise. Exact matching is not windowed.

**The bias is deliberate.** Wrongly merging two memories destroys a fact;
wrongly keeping a near-duplicate only adds mild noise. When uncertain, both are
kept.

Every rejection is logged with the reason, the similarity score, and the id of
the memory it matched.

### Contradictions

Not handled in Stage 2A, by design. A new memory that contradicts an old one is
simply stored; the old one is left untouched and is never overwritten. The
`superseded` status exists for the lifecycle stage that will resolve this.

---

## Failure handling

**A memory extraction failure can never affect the chat turn.** By the time
extraction runs, the response has been sent and the turn committed.

| Failure | Behaviour |
| --- | --- |
| Model call times out / rate limited / rejects the key | logged, no memory, chat unaffected |
| Model returns malformed or non-JSON output | logged, no memory, chat unaffected |
| Candidate fails validation | logged with counts, that candidate dropped |
| Database write fails | logged, task returns, chat unaffected |
| One candidate in a batch fails | that insert is rolled back to a savepoint; the batch is abandoned rather than half-written |
| Unexpected exception anywhere in extraction | caught, logged with traceback |

`run_memory_extraction` never raises. Every path returns cleanly.

Extraction also never runs for a turn that failed — a chat error means no
response was stored, so there is nothing to derive a memory from.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEMORY_ENABLED` | `true` | Subsystem master switch; also gates route registration |
| `MEMORY_EXTRACTION_ENABLED` | `true` | Automatic extraction after each turn |
| `MEMORY_MIN_IMPORTANCE` | `5` | Minimum importance to store (1–10) |
| `MEMORY_MIN_CONFIDENCE` | `0.7` | Minimum confidence to store (0.0–1.0) |
| `MEMORY_DEDUP_THRESHOLD` | `0.82` | Similarity at which a candidate is a duplicate |
| `MEMORY_DEDUP_CANDIDATES` | `50` | Size of the comparison window |
| `MEMORY_EXTRACTION_TEMPERATURE` | `0.1` | Extraction is classification, not generation |
| `MEMORY_EXTRACTION_MAX_TOKENS` | `1024` | Cap on extraction responses |

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/memories` | List; filter by `memory_type` and `status`; `limit`/`offset` |
| `GET` | `/api/memories/{id}` | Retrieve one |
| `DELETE` | `/api/memories/{id}` | Permanent deletion |

Primarily a development and debugging surface — memory quality cannot be judged
without reading what was stored and why. `POST /api/memories` was deliberately
not implemented: the primary creation mechanism is automatic extraction.

---

## Current limitations

Stated plainly, since they define what the next stages must solve.

- **No semantic similarity.** Deduplication is lexical. Two memories with no
  shared vocabulary but identical meaning will both be stored. Requires
  embeddings (out of scope).
- **No entity extraction and no relationship graph.**
- **No contradiction resolution.** Conflicting memories coexist.
- **No memory retrieval into chat.** Memories are stored and inspectable but
  are not yet fed back into conversations — that is a later stage.
- **No ranking beyond `importance_score`.** No recency weighting, no decay,
  no reinforcement.
- **No memory lifecycle.** Everything is `active`; nothing is ever superseded
  or archived automatically.
- **One turn at a time.** No cross-conversation synthesis.
- **Extraction costs one extra model call per turn**, which counts against the
  provider's rate limit.
- **Synonym handling is a fixed list, not lemmatisation.** Rewordings that use
  a verb outside that list, or that share little vocabulary, are still missed.
- **SQLite transactions are serialised.** Correct SAVEPOINT behaviour on the
  pysqlite driver requires taking the write lock at transaction start.
  PostgreSQL — the deployment target — is unaffected.
