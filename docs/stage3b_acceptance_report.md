# Mai — Stage 3B Acceptance Report

**Status: PASS**

All 25 acceptance criteria are met and verified by automated tests. The full
regression suite passes with no previous functionality lost.

Two things are explicitly **not** claimed as verified, and are listed in
*Not verified* below rather than being marked PASS: the behaviour of a real
model given the assembled knowledge, and the untested runtime components
(Docker, PostgreSQL, the frontend) carried forward from earlier stages.

---

## Features implemented

- `app/prompt/` — the Stage 3B package: `formatter.py`, `schemas.py`,
  `__init__.py`.
- `PromptFormatter` — the single owner of `ContextPackage` → `LLMMessage`.
- A framed, structurally separated reference-knowledge block.
- `ChatService` refactored from builder to orchestrator.
- Stage 2D's inline prompt injection retired from the chat path.
- A total (non-raising) fallback prompt path that cannot emit knowledge.
- `POST /api/prompt/debug` — prompt inspection with no model call and no writes.
- `detect_duplicates()` — duplication analysis over a finished prompt.
- Per-stage latency and prompt-size instrumentation on every turn.

---

## Architecture — the final request path

```
USER MESSAGE
     ▼
ChatService.send_message      404 check first, before any write
     ▼
ContextService.build          Stage 3A
     ├── recent conversation (bounded, assembled BEFORE the message is stored)
     └── RetrievalService.retrieve → RetrievalResult   Stage 2D
     ▼
ContextPackage
     ▼
PromptFormatter.format        Stage 3B — the only knowledge→prompt path
     ▼
List[LLMMessage]              generic, provider-agnostic
     ▼
provider.generate_response()  ← ONE synchronous model call
     ▼
store reply → BackgroundTasks → memory → entity → relationship extraction
```

---

## Legacy injection migration

**Where it was.** `ChatService._build_context` (old `chat_service.py:142-169`)
called `RetrievalService.render(knowledge)` and appended the result as a second
`system` message. Stage 2D decided both what was relevant *and* how it appeared
to the model.

**How it was retired.** `_build_context` was deleted with the entire manual
message-building path. `ChatService` no longer imports `app.retrieval.service`;
it depends on `ContextService` and `PromptFormatter`.

**What was kept.** No retrieval logic was removed. `RetrievalResult`, ranking,
entity matching, memory and relationship retrieval, and the Stage 2D debug
endpoints are untouched. `RetrievalService.render` still exists with two
callers, neither of which reaches a model:

- `/api/retrieval/debug` and `/api/conversations/{id}/context-preview`;
- `ContextBuilder.build`, which measures rendered length for
  `RETRIEVAL_MAX_CONTEXT_CHARS`.

Both now carry docstrings recording that they are no longer part of any prompt.

**How duplication was prevented.** The old path was removed rather than
disabled, so there is nothing to double-render. On top of that, the current
message is kept out of history by assembling context before persisting it, and
a structural guard in the formatter drops a trailing echoed user message if a
caller ever reverses that order.

**Verification — and proof the tests have teeth.** Six tests of three kinds
cover the migration (behavioural spy on the retired renderer, AST/structural
checks, and duplication counts). The legacy injection was deliberately spliced
back into `ChatService` as a mutation check; **all six failed**, then passed
again on restore. They detect the regression rather than merely describing the
current state.

---

## PromptFormatter — responsibilities and boundaries

| Does | Does not |
| --- | --- |
| Accept a `ContextPackage` | Query the database *(import test)* |
| Preserve the current message exactly | Call an LLM *(import test)* |
| Include recent conversation | Load more context or re-rank |
| Render knowledge as reference data | Mutate stored knowledge |
| Return generic `LLMMessage` | Know any provider exists *(source test)* |
| Be deterministic | Ignore Stage 3A's budget |

Provider independence is enforced structurally: a test parses every file under
`app/prompt/` and fails if it imports provider code *or if the source so much
as mentions a provider name*, so a vendor-specific branch cannot be added
quietly.

`ChatService` orchestrates only. `test_only_the_formatter_builds_prompt_messages`
walks the AST of every module under `app/` and asserts that the set of files
constructing an `LLMMessage` is exactly the formatter, the three background
extractors, and the provider health probe.

---

## Message ordering

```
1. system         application instructions
2. system         REFERENCE KNOWLEDGE block   (omitted when empty)
3. user/assistant recent conversation, chronological
4. user           the current message         ← always last, exactly once
```

Position is not authority. Logical precedence remains: instructions → current
message → recent conversation → retrieved knowledge → assistant defaults.

---

## Context safety

Retrieved knowledge is quoted data, guaranteed by three structural rules rather
than by wording alone:

1. Only `ContextRole.REFERENCE` items are rendered. An item carrying
   `INSTRUCTION` is dropped and counted, never promoted.
2. Stored conversation rows may only be `user` or `assistant`. A row claiming
   `system` is dropped — database text cannot become an instruction.
3. Every rendered knowledge line is flattened to a single line, so retrieved
   text cannot forge the block's headings or appear to close the section.

The block states in its own text that it is background, not instructions, that
nothing in it may alter the system instructions, and that the user is right if
it conflicts with the current conversation.

No database ids, foreign keys, scores, ranks, confidences, importance values,
memory types, evidence rows or timestamps reach the model.

---

## Current user priority

The current message is preserved byte-for-byte, appears exactly once, is always
the final `user` message, and is never merged into the reference block. It is
the one item no budget may drop: Stage 3A reserves it before applying limits,
and Stage 3B appends it after every other section.

Verified with the specified scenario — memory "User uses OpenRouter", current
message "I switched to Groq": the memory is retrieved and appears inside the
reference block; the current statement is the last message; the block declares
itself possibly out of date. No contradiction resolution is implemented (3C).

---

## One synchronous LLM call guarantee

**Verified.** For every normal chat request exactly one synchronous generation
call occurs — the final response. Stage 3B introduces zero additional
request-path model calls. There is no relevance check, no summarisation pass,
and no second generation.

The fake provider records generation calls (`calls`) separately from
`json_mode` extraction calls, so the assertion is unambiguous. Confirmed for a
turn with knowledge, a turn without, and across three consecutive turns.

**Excluded, as specified:** the background memory, entity and relationship
extraction calls. These run after the response is returned, on their own
session, and are unchanged by Stage 3B.

The invariant is also recorded on every turn as
`request_path_llm_calls: 1`, so it is observable in production.

---

## Failure isolation

| Failure | Result | Knowledge |
| --- | --- | --- |
| One retrieval source | others still used | partial |
| All of Stage 2D | conversation + current message | none |
| Stage 3A assembly | conversation reloaded directly + current message | none |
| `PromptFormatter.format` | fallback on already-assembled conversation | none |
| Conversation history unavailable | current message alone | none |
| The LLM provider | turn fails — the only thing that can | — |

`PromptFormatter.fallback` has no access to a `RetrievalResult` and cannot emit
a knowledge block even in principle. It is *total* — it does not raise — which
is what lets `ChatService` avoid a second inline construction path for the
"everything failed" case. A dedicated test forces formatting to fail while
spying on the retired renderer and confirms it is never reached.

Every degradation logs at ERROR with the cause; nothing is swallowed silently.

---

## Debug API

`POST /api/prompt/debug` returns message roles and ordering, section per
message, the current message's index and whether it is last, per-message and
per-section character counts, memory/entity/relationship counts, a
duplicate-detection report, and the Stage 3A context summary.

- Zero model calls — verified across all four call channels.
- Zero mutations — memory, entity, relationship, message and conversation
  counts are identical before and after; the inspected message is never stored.
- Production logic reused — a test asserts the roles it reports match what the
  provider actually received for the same input.
- System instruction text is reported by size only, never echoed. No API key,
  provider setting, database URL or database identifier appears in the response.

---

## Observability

Three log lines, one per stage, cover the request path: `Context retrieval
completed` (2D), `Context assembled` (3A), and `Chat turn started` (3B).

Stage 3B records prompt message counts by section, rendered
memory/entity/relationship counts, `reference_chars`, `prompt_chars`,
`fallback_prompt`, `request_path_llm_calls`, and three latencies —
`assembly_ms` (Stage 2D + 3A combined), `format_ms`, and `pre_llm_ms`.

**Not logged:** API keys, provider configuration, the database URL, and the
prompt body itself. Retrieved memories are personal data; logging their text on
every request would move it into a system with different retention and a wider
audience than the database it came from. Two tests enforce this.

---

## Tests

| | |
| --- | --- |
| **Total** | **748** |
| **Passed** | **748** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Baseline before Stage 3B | 672 |
| Added by Stage 3B | 76 |

| New file | Tests | Covers |
| --- | --- | --- |
| `test_prompt_formatter.py` | 35 | format, ordering, current message, conversation, reference framing, `context_role` safety, no DB internals, provider independence, fallback, duplication analysis |
| `test_prompt_integration.py` | 24 | one synchronous call, single knowledge path, legacy retirement, duplication, failure isolation, background pipeline, end-to-end recall, budgets |
| `test_prompt_debug_api.py` | 11 | no model call, no mutation, reported structure, no secrets or ids |
| `test_prompt_observability.py` | 6 | metrics, latency, one-call record, no secrets, no prompt body in logs |

Two existing files were updated rather than broken:

- `test_retrieval_integration.py` — the marker it searches for moved from Stage
  2D's `CONTEXT_HEADER` to Stage 3B's `REFERENCE_HEADER`. Every retrieval
  behaviour assertion is unchanged; all 17 tests still pass.
- `test_chat_flow.py::test_context_window_is_capped` — updated for the
  documented window change described below.

Full regression across Stages 1, 2A, 2B, 2C, 2D, 3A and 3B: **748 passed**.

---

## Bugs found

1. **Circular import on wiring Stage 3A into chat.** `app/context/service.py`
   imports `ConversationService`, which triggers `app/services/__init__.py`,
   which eagerly imported `chat_service`, which imports `app.context.service` —
   partially initialised. Surfaced immediately as an `ImportError` on
   `import app.main`.

2. **`MAX_CONTEXT_MESSAGES` would have become dead configuration.** Routing the
   chat path through Stage 3A meant the window was governed solely by
   `CONTEXT_RECENT_MESSAGE_LIMIT`. Lowering `MAX_CONTEXT_MESSAGES` would have
   silently had no effect — a config trap, not a crash.

3. **The current message would have been sent twice.** `ChatService` persisted
   the user message before loading history, so the current message was the last
   history row. Formatting that directly would have duplicated the question and
   charged it to the budget twice. Anticipated from the spec and confirmed by
   reading the existing flow before writing any code.

---

## Fixes applied

1. `app/services/__init__.py` re-exports `ChatService` lazily via module
   `__getattr__`, breaking the import cycle without forcing an import order the
   layering cannot satisfy.

2. `ContextService.limits` takes `min(CONTEXT_RECENT_MESSAGE_LIMIT,
   MAX_CONTEXT_MESSAGES)`, so both settings remain meaningful and the Stage 1
   ceiling still binds.

3. Context is assembled *before* the user message is persisted, and
   `_strip_echoed_current` guards the formatter against a caller that reverses
   that order. Only a trailing unanswered `user` row with identical content is
   removed, so a genuine earlier repeat is never lost.

4. `to_recent_messages` was lifted out of `ContextAssembler` to module level in
   `app/context/assembler.py`, so the chat fallback path converts stored rows
   through the same implementation rather than a second copy.

---

## Documented behaviour change

The conversation window's meaning shifted slightly. Because context is now
assembled before the user's message is stored, the cap applies to *history
alone* and the current message is sent on top of it — so the same setting
yields one more message than before Stage 3B. This is deliberate: the current
message is the one thing no budget may drop.

`test_context_window_is_capped` was updated to the new semantics with a
comment explaining the change, and still asserts the cap is real.

---

## Not verified

Stated explicitly rather than marked PASS:

- **Real model behaviour.** The end-to-end test proves PostgreSQL, Groq and
  Claude Code all reach the final LLM call from four separate prior
  conversations, and that the question remains the last message. Whether a real
  model then *uses* that knowledge well is a property of the model, not of this
  pipeline; the suite runs against a fake provider with no network, and no live
  Groq call was made.
- **Docker, PostgreSQL runtime, and the frontend** — unchanged from earlier
  stages. This machine has neither Docker nor Node nor a PostgreSQL server, so
  these remain untested here. Stage 3B adds no migration and no schema change,
  so nothing new is at risk, but nothing new was proven either.

---

## Known limitations

- No advanced conflict resolution.
- No contradiction engine.
- No semantic compression or summarisation.
- No token-aware budgeting — characters, behind a single swappable sizer.
- No proactive reasoning.
- No autonomous planning.
- No personality engine — `MAI_SYSTEM_PROMPT` is used unchanged.
- No additional reasoning model calls.
- Retrieval remains lexical: Stage 3B changed how knowledge is presented, not
  what is found.

---

## Acceptance criteria

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Stage 2D retrieval intact | PASS |
| 2 | Legacy inline injection retired | PASS |
| 3 | Exactly one production knowledge→prompt path | PASS |
| 4 | `ContextPackage` used by the chat pipeline | PASS |
| 5 | `PromptFormatter` is the single owner | PASS |
| 6 | `ChatService` orchestrates, does not format | PASS |
| 7 | Current message appears exactly once | PASS |
| 8 | Current message is the final user message | PASS |
| 9 | Recent conversation not duplicated | PASS |
| 10 | Retrieved knowledge is clearly reference data | PASS |
| 11 | Retrieved knowledge never becomes instructions | PASS |
| 12 | Provider independence intact | PASS |
| 13 | Exactly one synchronous request-path call | PASS |
| 14 | Background extraction still works | PASS |
| 15 | Retrieval failure does not break chat | PASS |
| 16 | Assembly failure does not break chat | PASS |
| 17 | Formatting failure does not break chat | PASS |
| 18 | Fallbacks do not reintroduce legacy injection | PASS |
| 19 | No duplicate long-term knowledge | PASS |
| 20 | Context budgets respected | PASS |
| 21 | Debug inspection makes no model call | PASS |
| 22 | Observability exists | PASS |
| 23 | Full regression suite passes | PASS (748) |
| 24 | No secrets in logs | PASS |
| 25 | Unverified functionality not marked PASS | PASS — see *Not verified* |

Stage 3C was not started.
