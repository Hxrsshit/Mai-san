# Mai — Stage 1 Acceptance Report

**Date:** 2026-08-29
**Auditor:** automated audit with live execution
**Scope:** Stage 1 foundation only. No Stage 2 work performed.

---

## Stage 1 Status

### PASS WITH MINOR ISSUES

The backend, database layer, migrations, LLM abstraction, Groq integration and
end-to-end chat flow were **executed and verified working**. 105 automated
tests pass.

The qualification is not a defect in the code — it is missing verification.
**Three components could not be executed on this machine** because the required
tooling is not installed:

| Component | Why not verified |
| --- | --- |
| Docker / Docker Compose | Docker is not installed (no `docker`, no Docker.app/OrbStack) |
| Next.js frontend | Node and npm are not installed |
| PostgreSQL at runtime | No PostgreSQL server or `psql` client available |

Per the instruction not to mark anything PASS unless verified, these are
recorded as **NOT VERIFIED**, not as PASS.

Available toolchain: Python 3.9.6 and git only.

---

## Requirements Checklist

### 1. Project structure

| Item | Status | Evidence |
| --- | --- | --- |
| Clean separation (frontend / backend / db / routes / services / llm / config / tests / docs) | **PASS** | 71 files; layered `api → services → database` + `llm` |
| No duplicate files | **PASS** | Full tree inspected |
| No dead code | **PASS** | `pyflakes` clean on `app/`, `tests/`, `alembic/` |
| No unused dependencies | **PASS** | All 12 declared packages imported or runtime drivers |
| No temp/development files | **PASS** | No stray `.db`, `.log`, `.bak`, `.orig` |
| Obsolete OpenRouter files removed | **PASS** | 21 files cleaned; see B10 and Fixes Applied |

### 2. Backend

| Item | Status | Evidence |
| --- | --- | --- |
| Application starts | **PASS** | Clean startup log, `Mai backend ready` |
| Health endpoint | **PASS** | `{"status":"ok","database":{"healthy":true},"llm":{"healthy":true}}` |
| Routes registered | **PASS** | All 8 routes resolve |
| Request validation | **PASS** | Empty, missing, wrong-type, malformed-JSON, blank all → 422 |
| Response schemas consistent | **PASS** | Pydantic models on every route |
| Error handling | **PASS** | Single envelope `{"error":{code,message,request_id}}` |
| CORS works with frontend origin | **PASS** | Preflight allows `localhost:3000`, rejects `evil.example.com`, exposes `X-Request-ID` |
| Environment variables load | **PASS** | 19 config tests, loaded via real env vars |
| Startup failures understandable | **PASS** | DB down logs `Connection refused` + `/health` reports degraded |
| Logging works | **PASS** | All 9 required events logged |

**Endpoint matrix — every case executed against a running server:**

| Request | Expected | Actual |
| --- | --- | --- |
| `GET /health` | 200 | **200** |
| `GET /api/conversations` | 200 | **200** |
| `POST /api/conversations` `{}` | 201 | **201** |
| `POST /api/conversations` `{"title":"..."}` | 201 | **201** |
| `GET /api/conversations/{id}` | 200 | **200** |
| `GET /api/conversations/{unknown uuid}` | 404 | **404** `conversation_not_found` |
| `GET /api/conversations/not-a-uuid` | 422 | **422** `validation_error` |
| `POST .../messages` `{"content":""}` | 422 | **422** `validation_error` |
| `POST .../messages` `{}` | 422 | **422** `content: Field required` |
| `POST .../messages` `{"content":123}` | 422 | **422** `Input should be a valid string` |
| `POST .../messages` malformed JSON | 422 | **422** `JSON decode error` |
| `POST .../messages` unknown conversation | 404 | **404** `conversation_not_found` |
| `PATCH /api/conversations/{id}` | 200 | **200** |
| `PATCH` blank title | 422 | **422** |
| `GET .../messages` | 200 | **200** |
| `GET /api/nope` | 404 | **404** |
| `DELETE` unknown conversation | 404 | **404** |
| `DELETE` existing | 204 | **204** |

### 3. Database

| Item | Status | Evidence |
| --- | --- | --- |
| Connection works | **PASS** | Verified on SQLite; PostgreSQL runtime NOT verified |
| SQLAlchemy models correct | **PASS** | Schema matches spec |
| Relationships correct | **PASS** | `Conversation.messages` ↔ `Message.conversation` |
| Foreign keys enforced | **PASS** | 0 orphans after delete; SQLite pragma set to match PostgreSQL |
| Indexes present | **PASS** | `(conversation_id, created_at)` and `updated_at` |
| UUID generation | **PASS** | Valid UUIDv4 primary keys |
| Timestamps | **PASS** | Timezone-aware, set in Python |
| Messages linked to conversations | **PASS** | Verified via raw SQL |
| **Persists across restart** | **PASS** | 6 messages intact after full backend restart |
| Delete cascade | **PASS** | 6 → 0 messages, 0 orphans, subsequent `GET` → 404 |

**Sequence executed exactly as specified:** create → 3 message pairs → restart
backend → retrieve (title and all 6 messages intact) → delete → cascade
confirmed by direct SQL.

> **Limitation:** runtime testing used SQLite because no PostgreSQL server is
> available. PostgreSQL was verified at the schema level only — see §4.

### 4. Alembic migrations

| Item | Status | Evidence |
| --- | --- | --- |
| Properly configured | **PASS** | Async `env.py`, URL from environment (never in `alembic.ini`) |
| Initial migration exists | **PASS** | `0001_initial_schema.py` |
| Applies to a clean database | **PASS** | Ran on empty DB → `alembic current: 0001 (head)` |
| Does not depend on manual table creation | **PASS** | Only Alembic creates the schema |
| No `create_all()` in production code | **PASS** | `grep` confirms: `app/` never calls it; test fixtures only |
| Migration matches models | **PASS** | DDL diffed statement-by-statement against ORM metadata — **0 drift** |

PostgreSQL DDL was rendered offline (`alembic upgrade head --sql`) and produces
native `UUID`, `TIMESTAMP WITH TIME ZONE`, the `message_role` enum type,
`ON DELETE CASCADE`, and both indexes.

Commands:

```bash
alembic upgrade head          # apply
alembic current               # show applied revision
alembic downgrade -1          # roll back one
```

### 5. LLM provider abstraction

| Item | Status | Evidence |
| --- | --- | --- |
| Base interface exists | **PASS** | `LLMProvider` ABC — `generate_response()`, `health_check()` |
| Groq implementation exists | **PASS** | `GroqProvider` |
| Clean dependency injection | **PASS** | `Depends(get_llm_provider)`; tests override with one line |
| Configurable provider selection | **PASS** | `LLM_PROVIDER` → factory registry |
| **ChatService has no Groq-specific code** | **PASS** | Verified by execution, below |

Structure:

```
ChatService
    ↓ depends only on
LLMProvider (ABC)
    ↓
OpenAICompatibleProvider   ← HTTP, retries, error mapping, parsing
    ↓
GroqProvider               ← name + defaults only
```

The abstraction was verified by **actually exercising it**, not by inspection:
a `ClaudeShapedProvider` with a completely different wire format (subclassing
`LLMProvider` directly, not the OpenAI-compatible transport) was registered at
runtime and driven through a full `ChatService.send_message()` turn. It
returned its own response and received the correct message sequence — with
**zero changes to ChatService, routes, or schemas**.

Adding Claude/OpenAI/Gemini/local requires: one file under
`app/llm/providers/`, three `<PROVIDER>_*` settings, one registry line.

### 6. Groq integration

| Item | Status | Evidence |
| --- | --- | --- |
| API key from environment | **PASS** | `GROQ_API_KEY`, no default |
| Never hardcoded | **PASS** | Key absent from all tracked files; only in gitignored `.env` (mode 600) |
| Correct API usage | **PASS** | `POST https://api.groq.com/openai/v1/chat/completions`, `Bearer` auth — verified against live API |
| Configurable model | **PASS** | `GROQ_MODEL` |
| Configurable base URL | **PASS** | `GROQ_BASE_URL` |
| Timeout handling | **PASS** | `LLM_TIMEOUT_SECONDS=0.001` → 504, server survived |
| Retry handling | **PASS** | Exponential backoff + jitter; `Retry-After` honoured (capped 30s); 4xx never retried |
| Failures handled cleanly | **PASS** | All mapped to typed errors |
| Invalid key → understandable error | **PASS** | 502 `llm_auth_error`, *"groq rejected the API key (HTTP 401)"*, 1 attempt (no wasted retries) |
| Model errors don't crash backend | **PASS** | `/health` still 200 after every induced failure |
| Responses converted to internal format | **PASS** | Normalised to `LLMResponse` |

Groq documentation was checked live: the model list was queried from
`/openai/v1/models` and `openai/gpt-oss-120b` selected as default (131K
context). Free tier is 30 req/min, 14,400/day.

### 7. End-to-end chat flow

**Status: PASS** — every step executed against the real Groq API.

```
User message
  → POST /api/conversations/{id}/messages
  → Pydantic validation (blank/blank-ish rejected)
  → conversation loaded (404 before any write)
  → user message stored in DB
  → history loaded, capped at MAX_CONTEXT_MESSAGES
  → context built: system prompt + history in order
  → LLMProvider.generate_response()  [abstraction boundary]
  → Groq API called
  → response normalised to LLMResponse
  → assistant message stored in DB
  → both messages returned
```

Context retention, exactly as the requirement specifies:

```
You: My name is TestUser.
Mai: Nice to meet you, TestUser! How can I assist you today?
You: What is my name?
Mai: Your name is TestUser.
```

Server logs confirm the context window grew `message_count=2` → `4` → `6`,
proving history is genuinely replayed rather than each turn being independent.

### 8. Frontend

**Status: NOT VERIFIED (runtime) / PASS (static analysis)**

Node and npm are not installed, so the application was never built or run. No
claim is made about runtime behaviour, browser console, or hydration.

What *was* verified statically:

| Item | Status |
| --- | --- |
| All internal imports resolve | **PASS** — 13 named imports across 7 files |
| All named imports actually exported | **PASS** |
| Balanced braces/parens/brackets | **PASS** |
| `"use client"` on every hook-using file | **PASS** |
| Every CSS variable used is defined | **PASS** — 6 used, 6 defined, 0 missing, 0 unused |
| API URL configurable | **PASS** — `NEXT_PUBLIC_API_URL` |

Three frontend defects were found and fixed by inspection (see Bugs). Items
that **cannot** be confirmed without running it: conversation list rendering,
selection, loading indicator, Enter-to-send, empty states, deleted conversations
disappearing, browser console cleanliness, hydration.

### 9. Error handling & resilience

| Scenario | Expected | Actual | Status |
| --- | --- | --- | --- |
| Invalid conversation ID | clean 404 | 404 `conversation_not_found` | **PASS** |
| Invalid request payload | validation error | 422 `validation_error` | **PASS** |
| Database unavailable | useful log | logs `Connection refused`, `/health` degraded, backend still starts | **PASS** |
| Invalid API key | controlled error | 502 `llm_auth_error`, no retry | **PASS** |
| LLM timeout | no crash | 504 `llm_timeout`, `/health` still 200 | **PASS** |
| LLM unavailable | no data corruption | 502, turn rolled back | **PASS** |
| Frontend API failure | readable message | code→message map in `lib/api.ts` | **PASS** (static) |
| **No fake assistant response on failure** | **PASS** | 0 messages stored after every induced failure |
| **User messages handled consistently on failure** | **PASS** | Whole turn rolled back — no orphan user message |

The rollback guarantee was verified three separate ways (invalid key, timeout,
unreachable API): in every case the conversation was left with **0 messages**,
never a stranded user message awaiting a reply that never came.

### 10. Logging

| Event | Status |
| --- | --- |
| Application startup | **PASS** |
| Database connection | **PASS** |
| Conversation creation | **PASS** |
| Message creation | **PASS** |
| LLM request start | **PASS** |
| LLM request success | **PASS** |
| LLM failures | **PASS** |
| Unexpected errors | **PASS** |

**Secret hygiene: PASS.** The live Groq key appears **0 times** across all log
output; the string `gsk_` appears **0 times**. No environment dumps. A unit
test asserts the key never reaches the logs.

Every log line inside a request carries a correlated `request_id`, also returned
as the `X-Request-ID` header.

### 11. Environment configuration

| Item | Status |
| --- | --- |
| `.env.example` complete | **PASS** — cross-checked programmatically against `Settings` |
| No unexpected extras | **PASS** — 0 |
| No missing required variables | **PASS** — remaining gaps are optional tuning, documented as commented-out |
| Obsolete OpenRouter/GLM variables removed | **PASS** |
| README matches actual config | **PASS** |

### 12. Docker

**Status: NOT VERIFIED**

Docker is not installed on this machine. `docker compose up --build` was
**never executed**. Per instruction, no claim of correctness is made.

What was verified: `docker-compose.yml` parses as valid YAML; service
definitions, `depends_on: service_healthy`, the `mai_pgdata` named volume, the
healthcheck, and env wiring were reviewed; and one previously latent
compose-breaking bug (`CORS_ORIGINS` parsing) was fixed and covered by tests.

**This must be run once by a human before Docker can be considered working.**

### 13. Testing

| Metric | Count |
| --- | --- |
| **Total** | **105** |
| **Passed** | **105** |
| **Failed** | **0** |
| **Skipped** | **0** |

| File | Tests |
| --- | --- |
| `test_llm_provider.py` | 35 |
| `test_config.py` | 19 |
| `test_error_handling.py` | 14 |
| `test_chat_flow.py` | 13 |
| `test_conversations.py` | 12 |
| `test_providers.py` | 9 |
| `test_health.py` | 3 |

Required coverage: health **✓**, conversation creation **✓**, retrieval **✓**,
message persistence **✓**, invalid conversation handling **✓**, provider
abstraction **✓**. Groq is mocked throughout; **no API key or network is
required** to run the suite.

```bash
cd backend && source .venv/bin/activate && pytest
```

### 14. Code quality

| Item | Status |
| --- | --- |
| No hardcoded configuration | **PASS** |
| No duplicate logic | **PASS** — transport shared via one base class |
| Correct async usage | **PASS** — async engine/sessions throughout |
| No blocking operations | **PASS** — `httpx.AsyncClient`, no sync I/O in handlers |
| Error handling | **PASS** — typed errors, single envelope |
| Loose coupling | **PASS** — proven by the foreign-provider test |
| Security | **PASS** — no hardcoded secrets, key never logged, CORS restricted |
| Dead code | **PASS** — pyflakes clean |
| Unused dependencies | **PASS** — none |
| Consistent naming | **PASS** |
| Type hints | **PASS** — present throughout |

### 15. Stage 1 boundary check

**Status: PASS.** Scanned for 17 future-stage markers (embedding, vector,
pgvector, faiss, chroma, rag, retrieval, agent, tool_call, tools, web_search,
browse, planner, reflect, personality, long_term, memory).

Two matches, both in comments explicitly deferring the feature:

- `config.py`: *"personality lives in a later stage"*
- `chat_service.py`: *"memory is explicitly out of scope for Stage 1"*

**No partial implementations of any future system exist.**

### 16. Final acceptance workflow

| # | Step | Status |
| --- | --- | --- |
| 1 | Start application | **PASS** (backend); frontend NOT VERIFIED |
| 2 | Open frontend | **NOT VERIFIED** (no Node) |
| 3 | Create conversation | **PASS** (API) |
| 4 | Send message | **PASS** |
| 5 | Message appears in UI | **NOT VERIFIED** |
| 6 | Message stored in database | **PASS** (raw SQL) |
| 7 | Groq receives the conversation | **PASS** (logs show growing context) |
| 8 | Mai responds | **PASS** |
| 9 | Response appears in UI | **NOT VERIFIED** |
| 10 | Response stored in database | **PASS** (raw SQL) |
| 11 | Send another message | **PASS** |
| 12 | Previous context included | **PASS** ("Your name is TestUser.") |
| 13 | Refresh frontend → persists | **NOT VERIFIED** (API re-read does persist) |
| 14 | Conversation persists | **PASS** |
| 15 | Restart backend | **PASS** |
| 16 | Conversation persists after restart | **PASS** — all 6 messages intact |
| 17 | Delete conversation | **PASS** — 204 |
| 18 | Removed correctly | **PASS** — cascade, 0 orphans, 404 after |

Steps 6, 10, 16 and 18 were confirmed by querying the database directly rather
than trusting the API response.

---

## Bugs Found

### Backend

**B1 — Database outage returned 500 instead of 503.** asyncpg raises a bare
`ConnectionRefusedError` (an `OSError`) on connect failure. That is not a
`SQLAlchemyError`, so it escaped the service layer's `except` clause and the
`SQLAlchemyError` handler, falling through to the generic handler. A database
outage was reported to clients as an opaque `internal_error`. *Severity:
medium — misleading diagnostics during an outage.*

**B2 — Whitespace-only messages were accepted.** `min_length=1` admits `"   "`.
Such messages were stored and **sent to the model**, wasting quota and polluting
history. *Severity: medium.*

**B3 — Blank conversation titles accepted.** Same root cause on
`ConversationCreate` / `ConversationUpdate`. *Severity: low.*

**B4 — Provider health check named the wrong variable.** Reported
`"GLM_API_KEY is not configured"` when the setting is `GROQ_API_KEY`. *Severity:
low — actively misleading.*

**B5 — Hyphenated provider names broke settings lookup.** `LLM_PROVIDER=my-provider`
produced `MY-PROVIDER_API_KEY`, not a valid environment variable name. Found by
a test written during this audit. *Severity: low.*

### Frontend

**B6 — Duplicate-send race.** `setIsSending(true)` ran *after*
`await api.createConversation()`. Two quick Enter presses both passed the
disabled check, **creating two conversations and two messages**. Directly
violates the "duplicate sends are prevented" requirement. *Severity: high.*

**B7 — Reply could land in the wrong conversation.** The sidebar allows
switching conversations while a response is in flight; the completion handler
applied the reply to whatever conversation was displayed, not the one it
belonged to. *Severity: medium — visible data corruption.*

**B8 — Dark mode did not work.** `@theme` was nested inside
`@media (prefers-color-scheme: dark)`. Tailwind CSS 4 only supports `@theme` at
the top level, so the dark palette was silently ignored. *Severity: medium.*

**B9 — Error text named the wrong variable.** Told users to set `GLM_API_KEY`
when the backend reads `GROQ_API_KEY`. *Severity: low — actively misleading.*

### Cleanliness

**B10 — OpenRouter/GLM residue across 21 files:** a whole `GLMProvider` class,
`GLM_API_KEY`/`GLM_BASE_URL`/`GLM_MODEL` settings, `OPENROUTER_SITE_URL`/
`OPENROUTER_APP_NAME`, OpenRouter-only `is_paid_model` and `provider_headers`
logic, `GLM_*` aliases, stale compose vars, and outdated documentation.

---

## Fixes Applied

| # | Fix |
| --- | --- |
| B1 | Service layer catches `(SQLAlchemyError, OSError)`; commit failures in `get_db_session` also map to `DatabaseError` → **503** |
| B2 | `field_validator` rejects blank content and strips surrounding whitespace |
| B3 | Same validator on conversation create and rename |
| B4 | Health detail now reports the active provider generically |
| B5 | Provider→setting lookup normalises hyphens to underscores |
| B6 | `isSending` claimed **before** any `await`, with a re-entrancy guard |
| B7 | `activeConversationRef` compared before applying a reply; stale replies discarded |
| B8 | Palette rewritten as plain CSS custom properties with a proper media-query override |
| B9 | Error text corrected to `GROQ_API_KEY` |
| B10 | `glm.py` deleted; all OpenRouter settings, headers, aliases, compose vars and docs removed |

**Removed:** `app/llm/providers/glm.py`, `GLM_*` and `OPENROUTER_*` settings,
`is_paid_model`, `provider_headers`, the `glm` registry entry.

**Retained deliberately:** `OpenAICompatibleProvider` as a base class. Groq is
its only subclass today, but it is the correct seam — an OpenAI-compatible
vendor is a subclass, a different-schema vendor subclasses `LLMProvider`
directly. Both paths are covered by tests.

**Tests added:** 105 total (was 89). New `test_error_handling.py` (14 tests)
covers B1–B3; `test_providers.py` rewritten (9 tests) to verify extensibility by
running a foreign provider end-to-end rather than by shipping an unused one.

---

## Remaining Issues

### Blocking further verification (environment, not code)

1. **Docker never executed** — not installed. `docker compose up --build` is unproven.
2. **Frontend never executed** — Node/npm not installed. Static analysis only.
3. **PostgreSQL never executed** — runtime testing used SQLite. Schema verified for PostgreSQL via offline DDL rendering and a zero-drift diff against the models.

### Known Stage 1 boundaries (by design, not defects)

4. Responses are not streamed.
5. No authentication — single trusted local user assumed.
6. No server-side rate limiting.
7. Context is a fixed message window, not summarisation.

### Minor

8. `frontend/package-lock.json` does not exist yet; the first `npm install` will create it, and it should be committed for reproducible builds.
9. The repository is **not under version control**. `git init` is strongly recommended before Stage 2 — there is currently no way to review or revert changes.
10. Dependency pins target Python 3.9 compatibility (this machine's only Python). On 3.12 they can be raised.

---

## Stage 2 Readiness

### Is Mai Stage 1 stable enough to begin Stage 2? — **YES**

The foundation Stage 2 will build on is verified working:

- The **provider abstraction holds** — demonstrated by running a foreign-schema provider through `ChatService` unmodified. Swapping or adding models will not require touching chat logic.
- **Persistence is correct** — data survives restart; cascade deletes leave no orphans; migrations are the single source of schema truth and match the models exactly.
- **The failure model is sound** — every induced failure (bad key, timeout, unreachable API, database down) produced a typed error, left the server healthy, and rolled the turn back with no partial writes. This matters more than the happy path for Stage 2, where memory and retrieval will add failure modes.
- **No future-stage code has leaked in** — Stage 2 starts from a clean boundary.
- **105 tests, no failures, no skips**, requiring no API key or network.

**Two conditions before relying on the unverified parts.** Neither blocks
backend Stage 2 work:

1. Run `npm install && npm run dev` once and confirm the frontend loads and a chat turn completes in the browser.
2. Run `docker compose up --build` once and confirm all three services start and communicate.

Both are marked NOT VERIFIED above precisely so they are not mistaken for tested.

---

## Appendix — How to reproduce

```bash
# Tests (no API key or network needed)
cd backend && source .venv/bin/activate && pytest

# Migrations against a clean database
alembic upgrade head && alembic current

# Render the PostgreSQL schema without a server
DATABASE_URL="postgresql+asyncpg://mai:mai@localhost:5432/mai" \
  alembic upgrade head --sql

# Run the backend
uvicorn app.main:app --reload

# Full stack (UNVERIFIED — needs Docker installed)
cp .env.example .env   # set GROQ_API_KEY
docker compose up --build
```

**Codebase size:** backend 2,128 LOC · tests 1,389 LOC · frontend 631 LOC.
