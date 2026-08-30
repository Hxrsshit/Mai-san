# Mai — Stage 3A: Context Assembly Architecture

Stage 3A takes already-ranked knowledge and combines it with the live
conversation into one bounded, structured package.

---

## The boundary with Stage 2D

The two stages own different problems, and the seam between them is explicit.

```
Stage 2D — RETRIEVAL                     Stage 3A — ASSEMBLY
────────────────────                     ───────────────────
query normalization                      current message
entity matching                          recent conversation
memory / relationship retrieval          RetrievalResult (consumed)
scoring + ranking                        category limits
        │                                total budget
        ▼                                        │
  RetrievalResult  ───────────────────────►      ▼
                                          ContextPackage
```

Stage 2D's result type was named `ContextPackage`; it is now
**`RetrievalResult`**, matching the specification's own wording that "Stage 2D
ends at RetrievalResult". The old name remains as an alias. `ContextPackage`
now means the assembled whole.

**Stage 3A does not**: re-run retrieval, re-score anything, query the knowledge
base independently, search for entities, or call a model. It consumes
`RetrievalService.retrieve()` exactly once and preserves its ordering.

---

## Where Stage 3A stops

Stage 3A produces the package and stops. It is **not wired into the chat
pipeline** — `ChatService` does not import it. Prompt formatting and injection
are Stage 3B.

Stage 2D's own simpler inline rendering remains in `ChatService` untouched, so
chat behaviour is unchanged. Stage 3B will switch the pipeline over to this
package and retire that inline path.

---

## Context priority hierarchy

```
1. system instructions and application rules
2. the current user message
3. recent conversation
4. retrieved long-term knowledge
5. general assistant defaults
```

**The current message always wins.** It is preserved byte-for-byte — never
normalized, rewritten or truncated — and is the one thing no budget can drop,
even when it alone exceeds the total.

```
Old memory:      "User uses OpenRouter."     → reference, may be stale
Current message: "I switched to Groq."       → authoritative
```

Both appear in the package, in separate fields, with the memory marked as
reference. Later stages can act on that distinction without re-deriving it.

---

## Retrieved knowledge is data, not instructions

Every item carries a `context_role`:

| Role | Applies to | May direct behaviour? |
| --- | --- | --- |
| `INSTRUCTION` | reserved — Stage 3A never assigns it | yes |
| `CONVERSATION` | recent messages | no |
| `REFERENCE` | memories, entities, relationships | no |

Nothing retrieved is ever marked `INSTRUCTION`, and the package has no
privileged field for retrieved text to land in. A test asserts that no item in
an assembled package carries the instruction role.

---

## ContextPackage

```
ContextPackage
├── current_message : str                    preserved exactly
├── recent_conversation : [RecentMessage]    short-term, chronological
├── memories       : [ContextMemory]         long-term, Stage 2D order
├── entities       : [ContextEntity]         long-term, Stage 2D order
├── relationships  : [ContextRelationship]   long-term, Stage 2D order
└── metadata       : ContextMetadata
```

Categories stay **structurally separate**. Nothing is flattened into a single
list or pre-rendered into text — that is Stage 3B's job.

### Item shapes

Each is deliberately narrow. Database internals are absent by construction, not
by filtering.

| Type | Carries | Deliberately omits |
| --- | --- | --- |
| `RecentMessage` | role, content, timestamp | message id, conversation id |
| `ContextMemory` | content, type, importance, confidence, created_at, rank, score | `normalized_content`, `source_conversation_id`, `status` |
| `ContextEntity` | name, type, description, rank, match strength | `normalized_name`, aliases, timestamps, status |
| `ContextRelationship` | source, type, target, confidence, rank | evidence rows, endpoint ids, timestamps |

Relationship **evidence is excluded on purpose**: it is storage provenance, and
the claim itself is what carries meaning into a prompt.

### Metadata

Counts per category, per-category character totals, the budget in force,
dropped items with reasons and ranks, degraded sources, and assembly duration.

---

## Context budgeting

Two stages, in order.

**1. Category limits** — no one category may consume the whole context.

```
CONTEXT_RECENT_MESSAGE_LIMIT      12
CONTEXT_MAX_MEMORY_ITEMS          10
CONTEXT_MAX_ENTITY_ITEMS          10
CONTEXT_MAX_RELATIONSHIP_ITEMS    10
```

Recent conversation is trimmed from the **front** — oldest turns go first,
keeping the current exchange intact. Long-term categories keep their
highest-ranked items, since Stage 2D already ordered them.

**2. Total budget** — `CONTEXT_MAX_TOTAL_CHARS`, the final authority. Items that
passed their category limit are still dropped if the whole exceeds it.

### Order of sacrifice

Documented and deterministic, lowest value first:

```
1. lowest-ranked relationships
2. lowest-ranked entities
3. lowest-ranked memories
4. oldest recent messages
   (the current message is never eligible)
```

Long-term knowledge yields before short-term conversation: the exchange the
user is actually in matters more than background.

### Item integrity

**Items are dropped whole.** A memory appears completely or not at all; text is
never cut mid-sentence and no structured item is half-rendered. If an item does
not fit, it is skipped and recorded.

---

## Character accounting

Per-category totals plus a grand total. Characters, not tokens — the
specification permits this for Stage 3A.

All measurement routes through a single `Sizer` function
(`character_sizer` by default). Substituting a token-aware sizer requires no
change to assembly, budgeting or schemas; a test demonstrates this by running
the budgeter with a word-counting sizer.

---

## Short-term context

Reuses `ConversationService.get_messages(limit=N)`, which already selects the
newest N and returns them oldest-first — exactly the strategy required. No
second implementation exists.

```
database:  newest → oldest   (indexed, LIMIT N)
selection: latest N
assembly:  oldest → newest   (natural reading order)
```

Order is never rearranged.

---

## Long-term context

Stage 2D's `RetrievalResult` is consumed as given:

- `retrieval_rank` and `retrieval_score` are **carried through**, never
  recomputed.
- Order is preserved; the only change the budget makes is removal.
- Relationships whose *both* endpoints were matched are **flagged**
  (`connects_matched_entities`) for Stage 3B to prefer when rendering — a flag,
  not a re-ranking.

No new scoring formula exists in Stage 3A.

---

## Failure isolation

Each optional source is gathered independently, so one failing does not take
the others down.

| Failure | Result |
| --- | --- |
| Retrieval unavailable | current message + recent conversation; `degraded_sources: ["long_term_knowledge"]` |
| Conversation unavailable | current message + long-term knowledge; `degraded_sources: ["recent_conversation"]` |
| Both unavailable | current message alone — still a valid package |
| Unknown conversation id | degrades, does not raise |

`ContextService.build()` never raises. The current message is present in every
outcome.

---

## No database mutation

Context assembly is a read-and-transform layer. It creates, updates and deletes
nothing — no memories, entities, relationships or evidence.

Verified two ways: a row-count and content snapshot taken before and after
assembly, and a statement-level listener asserting **zero** `INSERT`, `UPDATE`
or `DELETE` statements are issued.

---

## No model calls

`app/context/` imports nothing from `app.llm` — asserted structurally by a test
that walks the module ASTs. It cannot make a model call. Stage 2D, the only
thing it delegates to, is itself database-only.

---

## Debug endpoint

```
POST /api/context/debug
{
  "conversation_id": "…",           optional
  "message": "What technology stack am I using for Mai?"
}
```

Returns the `ContextPackage` itself rather than a bespoke debug shape, so what
is inspected is exactly what Stage 3B will consume: categories separate, ranks
preserved, budget and dropped items in `metadata`.

Makes no model call, mutates nothing, exposes no secrets.

---

## Performance

| Property | Measured |
| --- | --- |
| SELECTs per assembly | **9, constant** — Stage 2D's 8 plus one for recent conversation |
| Growth with data | none, from 10 to 300 memories and 200 messages |
| Latency | ~9 ms including retrieval |
| Selection | bounded by category limits at every size |
| Context size | bounded by `CONTEXT_MAX_TOTAL_CHARS` |

No N+1: retrieval is called exactly once (asserted), and recent conversation is
one indexed, `LIMIT`-ed query.

---

## Current limitations

- **The ContextPackage is not yet injected into the LLM.** Stage 3A stops at
  the package; Stage 3B wires it in.
- **No prompt formatting.** Nothing is rendered to text.
- **No new LLM behaviour.** Chat still uses Stage 2D's simpler inline
  rendering, unchanged.
- **Character-based budgeting only.** Token-aware budgeting is possible by
  swapping the sizer, but is not implemented.
- **No advanced conflict resolution.** A stale memory and a contradicting
  current message both appear; the package records the distinction but resolves
  nothing.
- **No semantic compression or summarisation.** Items are included whole or
  dropped.
- **No autonomous reasoning.**
- **Final prioritization is dropping only.** Stage 3A never re-orders Stage 2D's
  ranking; it only removes from the tail.
