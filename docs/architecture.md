# Mai — Architecture (Stage 1)

## Goals

Stage 1 exists to make later stages cheap to build. Three decisions carry most
of that weight:

1. **The model is behind an interface.** Nothing above `app/llm/base.py` knows
   which vendor is in use.
2. **The database is portable.** No PostgreSQL-only types, so a later move to
   a managed or different engine is a configuration change.
3. **Layers are separated.** Routes do HTTP, services do logic, models do
   persistence. Business logic never lives in a route handler.

## System overview

```
┌───────────────────────────┐
│  Browser — Next.js        │
│  Sidebar · Chat · Input   │
└─────────────┬─────────────┘
              │  HTTP/JSON  (NEXT_PUBLIC_API_URL)
┌─────────────▼─────────────┐
│  FastAPI                  │
│  ┌─────────────────────┐  │
│  │ api/routes          │  │  HTTP concerns only
│  ├─────────────────────┤  │
│  │ services            │  │  ChatService, ConversationService
│  ├──────────┬──────────┤  │
│  │ database │ llm      │  │  SQLAlchemy   │  LLMProvider (ABC)
│  └──────────┴──────────┘  │
└────────┬───────────┬──────┘
         │           │
┌────────▼──────┐ ┌──▼──────────────────┐
│ PostgreSQL    │ │ Groq (gpt-oss-120b) │
└───────────────┘ └─────────────────────┘
```

## Request flow

Sending a message (`POST /api/conversations/{id}/messages`):

1. **Middleware** assigns a request id, carried in a `ContextVar` so every log
   line in the request correlates, and returned as `X-Request-ID`.
2. **Route** validates the body with Pydantic and resolves `ChatService`
   through dependency injection.
3. **ChatService** loads the conversation — a 404 here happens *before*
   anything is written.
4. The user message is stored.
5. An untitled conversation is auto-titled from this first message.
6. History is loaded, capped at `MAX_CONTEXT_MESSAGES` most recent messages.
7. **Context is built**: the system prompt, then the history in order.
8. **LLMProvider.generate_response** is called through the abstraction.
9. **GLMProvider** POSTs to `/chat/completions`, retrying transient failures
   with exponential backoff, and normalises the reply into `LLMResponse`.
10. The assistant message is stored and the conversation's `updated_at` bumped.
11. The route returns both messages.

The session is committed by the request dependency only if the handler returns
normally. **If the model call fails, the whole turn is rolled back** — the user
message does not survive as a dangling row, so a retry starts from a clean
state.

Context comes from the current conversation only. There is no cross-conversation
memory in Stage 1, and a test asserts that no content leaks between them.

## Database design

```
conversations                       messages
─────────────                       ────────
id         UUID     PK              id               UUID  PK
title      VARCHAR(200)             conversation_id  UUID  FK → conversations.id
created_at TIMESTAMPTZ                                     ON DELETE CASCADE
updated_at TIMESTAMPTZ  (indexed)   role             ENUM(user|assistant|system)
                                    content          TEXT
                                    created_at       TIMESTAMPTZ

                                    INDEX (conversation_id, created_at)
```

Decisions worth knowing:

- **UUID keys** via SQLAlchemy's portable `Uuid` type: native `UUID` on
  PostgreSQL, `CHAR(32)` elsewhere. Ids are generated in the application, so
  they exist before a round trip and do not leak row counts.
- **The composite index** `(conversation_id, created_at)` serves the only read
  pattern there is: one conversation's messages, in order. It answers both the
  filter and the sort.
- **`updated_at` is indexed** because the sidebar always lists conversations by
  recency.
- **`ON DELETE CASCADE` at the database level**, not just in the ORM, so a bulk
  delete cannot orphan messages. SQLite needs `PRAGMA foreign_keys=ON` to honour
  this; the engine sets it automatically so behaviour matches PostgreSQL.
- **Timestamps are set in Python** (`default=utcnow`) as well as having a
  `server_default`. A server-side default alone leaves the attribute expired
  after flush, and reading it back would trigger a lazy refresh — which raises
  `MissingGreenlet` under an async session.
- **Migrations are the only way the schema is created.** Application code never
  calls `create_all`.

## LLM abstraction

```
     services/chat_service.py
                │  depends only on ↓
     ┌──────────▼──────────┐
     │ LLMProvider  (ABC)  │   generate_response(messages) -> LLMResponse
     │                     │   health_check()              -> ProviderHealth
     └──────────┬──────────┘
                │ implemented by
     ┌──────────▼───────────────┐
     │ OpenAICompatibleProvider │  HTTP, retries, error mapping, parsing
     └───┬──────────────────┬───┘
         │                  │
   ┌─────▼──────┐   ┌───────▼──────┐
   │GroqProvider│   │ GLMProvider  │   (Claude / Gemini / local go here later)
   └────────────┘   └──────────────┘
```

Many vendors (Groq, OpenAI, Together, Fireworks, vLLM) speak the same
OpenAI-compatible schema, so the transport lives once in
`OpenAICompatibleProvider` and each concrete provider is only a name plus
defaults. A vendor with a different schema (Claude, Gemini) subclasses
`LLMProvider` directly instead — a test does exactly that and drives a full
chat turn through it, proving `ChatService` needs no change.

The contract is three plain dataclasses — `LLMMessage`, `LLMResponse`,
`ProviderHealth` — and two methods. No provider SDK type crosses the boundary.

`app/llm/factory.py` maps `LLM_PROVIDER` to a builder and caches one instance
per process. `Settings.active_api_key` / `active_base_url` / `active_model`
resolve by the `<PROVIDER>_<SETTING>` naming convention, so nothing downstream
reads a provider-specific field. Routes receive it via `Depends(get_llm_provider)`, which is what
lets tests swap in a fake with one line and no monkeypatching.

**Adding a provider** means writing one file under `app/llm/providers/` and
adding one line to the registry. Nothing else changes.

### Provider specifics

Groq exposes an OpenAI-compatible schema, so the provider uses `httpx`
directly rather than a vendor SDK — fewer dependencies, no SDK types to leak,
and switching gateways is configuration rather than code.

```
POST {base_url}/chat/completions
Authorization: Bearer <api_key>

{"model": …, "messages": [...], "temperature": …,
 "max_tokens": …, "stream": false}
```

| `LLM_PROVIDER` | Base URL                         | Default model         | Free tier          |
| -------------- | -------------------------------- | --------------------- | ------------------ |
| `groq`         | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` | 30/min, 14,400/day |

Stage 1 ships one provider. Adding another means one file under
`app/llm/providers/`, three `<PROVIDER>_*` settings, and one registry line —
no change to routes, services, or schemas.

Failure handling:

| Condition                | Behaviour                                        |
| ------------------------ | ------------------------------------------------ |
| Timeout                  | Retried, then `LLMTimeoutError` → HTTP 504        |
| 429                      | Retried with backoff, then `LLMRateLimitError` → 429 |
| 5xx / transport error    | Retried with backoff, then `LLMError` → 502       |
| 401 / 403                | **Not retried** — `LLMAuthError` → 502            |
| Other 4xx                | **Not retried** — `LLMError` → 502                |
| Empty / filtered content | `LLMResponseError` → 502                          |

Retries use exponential backoff with jitter (~0.5s, ~1s, ~2s), **except when
the server sends `Retry-After`, which takes precedence** (capped at 30s). Free
model pools are shared and answer 429 with a `Retry-After` longer than our own
backoff, so honouring the header is what makes the free tier usable at all.

Client errors are never retried, because a rejected key does not become valid
on attempt two.

## Error handling

Services raise typed errors from `app/core/errors.py`; handlers in
`app/api/errors.py` turn them into one envelope:

```json
{ "error": { "code": "llm_timeout", "message": "…", "request_id": "…" } }
```

The frontend maps `code` to a friendly message, so wording changes do not
require a backend release. Unexpected exceptions log a full traceback server-side
and return a generic 500 — internals are never sent to a client.

## Logging

One JSON object per line (or a readable line when `LOG_FORMAT=console`), with
the request id attached to everything inside a request. Logged: startup,
database connection, conversation and message creation, LLM request start and
outcome, retry attempts, and errors. **API keys are never logged** — a test
asserts this.

## Testing

105 tests, covering configuration loaded from real environment variables,
the health endpoint, conversation CRUD, message persistence
and ordering, context construction and windowing, conversation isolation,
rollback on model failure, the Groq wire format, error mapping, retry
bounds, `Retry-After` handling, provider switching, and secret hygiene.

Config is tested through actual environment variables rather than Python
kwargs: pydantic-settings applies its own decoding to env input, so a setting
that works in Python can still fail at startup.

Tests use in-memory SQLite and a fake provider, so the suite needs no database,
no network, and no API key.

## Known Stage 1 boundaries

- Responses are not streamed; the reply arrives in one piece.
- No authentication — the API assumes a single trusted local user.
- No rate limiting of the client.
- Context is a fixed-size message window, not a summarisation strategy.

Each is a deliberate Stage 1 boundary, not an oversight.
