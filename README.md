# Mai

### A local-first, single-user personal AI assistant

Mai is a personal AI assistant with long-term memory, read-only access to your
calendar and mail, consent-gated web research, reminders, and a guarded task
runtime that can check conditions in the background. It runs on your own
machine with a FastAPI backend, PostgreSQL, a Next.js chat UI, and a hosted LLM
of your choice behind a provider-agnostic gateway.

The project is built in small, strictly scoped stages. Each stage ships with
behavioural tests, AST-based structural security tests, mutation testing and
verification against a real PostgreSQL database.

---

## Overview

Mai's design goal is an assistant that can remember, look things up and act on
your behalf **without ever doing more than the application can prove it did**.
In practice:

- **Memory is structured and auditable.** Facts are extracted into a database
  as memories, entities and relationships, each traceable to the conversation
  it came from. There is no opaque vector store.
- **Actions go through one permission path.** Tools are declared in a
  registry, checked by a single authorization service and run by a single
  execution service with an append-only audit journal. Most actions require
  explicit approval.
- **External content is data, never instructions.** Web pages, email, calendar
  entries, imported history and model output cannot create tasks, grant
  permissions or choose providers.
- **Replies are checked against what actually happened.** A reply that claims
  an action or a result the application did not record is rejected and
  regenerated.

## Current Status

| | |
| --- | --- |
| Latest completed stage | **Stage 6H: notification infrastructure** |
| Stages completed | 1 → 6H (foundation, memory, knowledge, agency, integrations, assistant features, task runtime) |
| Database migrations | 18 (`0001` → `0018`) |
| Automated tests | **5,565 passing, 2 skipped** (see [Testing](#testing)) |
| Deployment model | Local, single user. **Not designed to be exposed to a network** (see [Security & Privacy](#security--privacy)) |

Mai is a working personal project, not a production service. The full,
commit-by-commit record is in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

## Core Capabilities

Each item below is implemented in the current code and covered by tests.

**Conversation and knowledge**
- Chat with persistent conversations (create, list, rename, delete)
- Background extraction of **memories**, **entities** and **relationships**
  after each turn, with validation, deduplication and provenance
- Knowledge lifecycle: conflicting facts are detected and superseded, and old
  facts are retrieved only when a question is explicitly historical
- Deterministic **retrieval and context assembly**: ranked, budgeted and
  bounded, with no extra model calls
- **ChatGPT history import**: archive, redact credentials, derive memories with
  provenance (idempotent and resumable)

**Assistant features**
- **Web research** via Tavily or Brave. You confirm the exact query before
  anything is sent, and results are treated as untrusted
- **Google Calendar (read-only)**: schedule and free/busy answers in your
  configured timezone
- **Gmail (read-only)**: listing, reading, triage and "what needs my
  attention", with stated counts checked against what was retrieved
- **Meeting briefings and research-to-document workflows** with fixed,
  deterministic plans
- **Reminders**: one-time, relative, daily and weekly. Parsed
  deterministically, confirmed before saving, fired exactly once, and
  delivered to an in-app inbox

**Agency and task runtime**
- Intent classification and goal planning with dependency-graph validation
- A tool registry, a risk-based policy and a single `AuthorizationService`.
  CRITICAL-risk actions are structurally forbidden
- **Controlled execution** with payload-fingerprinted, time-limited approvals
  and an append-only audit journal. It is **off by default**
- **Standing approvals**: scoped to one capability, expiring (one week at
  most) and revocable
- **Persisted tasks** with validated plans, capability binding, budgets and a
  task journal
- A **task runner** that advances one step per invocation, and **one
  background runtime** that schedules reminders and tasks with database-backed
  claims (restart-safe, exactly-once)
- **Monitoring tasks** that re-check a read-only capability against a typed
  condition until it holds
- **Notifications**: one durable, owner-scoped record per monitoring outcome.
  Delivery channels are not built yet

**Interface**
- Next.js chat UI with a conversation sidebar, an **Integrations** panel to
  connect or disconnect Google Calendar and Gmail, and a **History import**
  panel
- REST API with an OpenAPI explorer at `/docs`

## Architecture

```text
                 ┌───────────────────────────────┐
  Browser ──────▶│  Next.js frontend (port 3000) │
                 └───────────────┬───────────────┘
                                 │ HTTP / JSON
                 ┌───────────────▼───────────────┐
                 │  FastAPI backend (port 8000)  │
                 │                               │
                 │  Chat orchestration           │
                 │   ├─ context assembly ◀── retrieval ◀── memory / entities /
                 │   │                                     relationships / knowledge
                 │   ├─ intent → planning → action proposal
                 │   ├─ recognisers: reminders · calendar · mail · research · workflows
                 │   └─ synthesis → response contract → execution-truth check
                 │                               │
                 │  Tools: registry → policy → AuthorizationService (+ standing grants)
                 │  Execution: ExecutionService → Dispatcher → audit journal
                 │  Tasks: TaskService → TaskRunner (advance / check) → notifications
                 │  BackgroundRuntime: one loop for reminders and tasks
                 │                               │
                 │  SecureHttpClient + NetworkPolicy (the only outbound HTTP path)
                 └──────┬───────────────────┬────┘
                        │                   │ allow-listed HTTPS only
                ┌───────▼──────┐   ┌────────▼──────────────────────────────┐
                │ PostgreSQL 16│   │ LLM provider (Groq · Gemini · Anthropic)│
                └──────────────┘   │ Google Calendar / Gmail (read-only)    │
                                   │ Tavily or Brave search                 │
                                   └────────────────────────────────────────┘
```

| Component | Responsibility |
| --- | --- |
| `frontend/` | Chat UI, integrations panel, history import panel |
| Chat orchestration (`services/`, `orchestration/`, `intent/`, `planning/`) | Runs one turn: context, intent, deterministic recognisers, one response generation, validation |
| Knowledge (`memory/`, `entities/`, `relationships/`, `knowledge/`, `retrieval/`, `context/`, `history/`) | What Mai knows, where it came from, and what reaches a prompt |
| `llm/` | Provider interface, gateway and providers (Groq, Gemini, Anthropic API) |
| `tools/`, `authorization/` | What capabilities exist, and whether one may run |
| `execution/` | The only path that runs a tool, with approvals and an audit journal |
| `tasks/`, `background/` | Persisted tasks, the runner, monitoring, notifications, the background loop |
| `integrations/` | The policed HTTP client, network policy, OAuth, token store and providers |
| `reminders/`, `calendar/`, `mail/`, `research/`, `workflows/` | Assistant features built on the layers above |

**Where things live.** All application data (conversations, memories,
entities, relationships, tasks, executions, journals, reminders,
notifications) is stored in PostgreSQL. OAuth tokens are stored as files in a
separate credentials directory, never in the database. Inference and search
use the external providers you configure. Everything else runs locally.

## How It Works

A chat turn, end to end:

1. **Load and assemble context.** The conversation is loaded, then recent
   messages and ranked knowledge from retrieval are assembled into a bounded
   `ContextPackage`.
2. **Understand the request.** An LLM classifies intent. Planning runs only
   for intents that need it. Action identification is a deterministic phrase
   lookup.
3. **Route deterministically.** Grammar-based recognisers handle reminders,
   calendar questions, mail questions, research proposals and workflows. These
   need no model call, and their replies are written by application code.
4. **Act only with consent.** Research and workflows are proposed first and run
   only after you confirm. Every tool call goes through the
   `AuthorizationService` and the `ExecutionService`.
5. **Generate and verify.** One response generation produces the reply. It is
   checked by a response contract and an execution-truth validator. A reply
   claiming an action or a count that was not recorded is regenerated.
6. **Learn in the background.** After the reply is returned, memory, entity
   and relationship extraction runs on its own database session.

The **background runtime** wakes on a fixed interval, fires due reminders, and
claims due tasks with a conditional database update. It advances each claimed
task through the same runner, authorization and execution path that everything
else uses. A monitoring task is checked on its interval until its condition
holds, then completes and records one notification.

## Memory & Context

- **Extraction:** after each turn, an LLM proposes candidate memories. They
  are validated against confidence and importance thresholds, deduplicated
  lexically and stored with a link to their source message. Entities and
  directed relationships are then extracted from stored memories.
- **No embeddings:** retrieval is lexical and graph-based. Candidates are
  ranked by text relevance, entity and relationship matches, importance,
  confidence and recency, within fixed budgets.
- **Lifecycle:** when facts conflict, the older one is superseded rather than
  deleted. Superseded knowledge is retrieved only for explicitly historical
  questions.
- **Prompt isolation:** retrieved knowledge reaches the model only through the
  prompt formatter, inside a reference block marked as data. Intent labels and
  plans never enter the prompt.
- **Inspection:** memories, entities and relationships can be listed,
  inspected and deleted through the API.

## AI / Inference

- Callers depend only on an `LLMProvider` interface. A gateway declares the
  available providers, and `LLM_PROVIDER` selects exactly one: `groq` (the
  default), `gemini` or `anthropic_api`.
- **No automatic fallback or routing between providers.** A failing provider
  produces an error rather than silently sending your conversation elsewhere.
- Providers are implemented directly over HTTP, without vendor SDKs, so every
  request goes through the same policed client. That client enforces a
  single-host allow-list, refuses redirects, caps response sizes, applies
  timeouts and scrubs any key a provider echoes back.
- OpenAI-compatible vendors (Groq, Gemini) share one implementation, and
  Anthropic has its own.

## Security & Privacy

Mai is a **local, single-user** application. Its security design is about
keeping the assistant itself honest and contained, not about defending a
public service.

**Design**
- **Secrets** are read only from environment variables. `.env` files are
  git-ignored and excluded from Docker builds. The database password has no
  default and must be set.
- **Network exposure:** Docker Compose binds PostgreSQL, the backend and the
  frontend to `127.0.0.1` only.
- **Outbound HTTP:** a single client checks every request and redirect hop
  against a per-integration allow-list, and blocks private, loopback,
  link-local and cloud-metadata addresses.
- **Authorization:** one authorization decision path. Risk-based policy, with
  CRITICAL forbidden. Per-execution approvals are bound to a payload
  fingerprint. Standing grants never widen beyond a single capability.
- **Execution** is disabled by default (`EXECUTION_ENABLED=false`). File tools
  are confined to a workspace directory.
- **Integrations** request read-only Google scopes. Tokens are stored as
  owner-only files (`0600` inside a `0700` directory).
- **Content isolation:** external content and model output cannot create
  tasks, schedule work, create grants, approve executions or select a
  provider. AST-based structural tests enforce these boundaries.
- **Logging** records identifiers, counts and reason codes rather than message
  text, tool arguments or tool output. Error responses and logs pass through a
  redactor.

**Testing:** 2,172 of the tests are security tests, including structural
(AST) audits of module boundaries, secret-leak tests with synthetic sentinel
credentials, and network-policy and SSRF tests.

**Limitations**
- **There is no authentication layer.** Anyone who can reach the backend can
  use it. Do not expose it to a network, put it behind a reverse proxy, or run
  it on a shared host.
- The backend does not validate the `Host` header, and debug endpoints that
  show assembled prompts and context (`/api/*/debug`) are enabled. Both are
  acceptable only on a trusted local machine.
- Some pinned dependencies have published advisories and are pending upgrade
  (tracked in [`docs/CHANGELOG.md`](docs/CHANGELOG.md)).

## Technology Stack

| Layer | Technology |
| --- | --- |
| Backend | Python, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic, httpx |
| Database | PostgreSQL 16 (asyncpg); SQLite (aiosqlite) for the test suite |
| Frontend | Next.js 15 (App Router), React 19, TypeScript, Tailwind CSS 4 |
| LLM providers | Groq (default), Google Gemini, Anthropic API, through OpenAI-compatible and native HTTP |
| Integrations | Google Calendar and Gmail (OAuth 2.0 with PKCE, read-only), Tavily or Brave search |
| Testing | pytest, pytest-asyncio |
| Runtime | Docker Compose (PostgreSQL 16, Python 3.12 and Node 22 images) |

## Repository Structure

```text
mai/
├── backend/
│   ├── app/
│   │   ├── api/            HTTP routes, dependencies, error handling
│   │   ├── services/       chat-turn orchestration
│   │   ├── llm/            provider interface, gateway, providers
│   │   ├── memory/ entities/ relationships/ knowledge/
│   │   ├── retrieval/ context/ prompt/ synthesis/
│   │   ├── intent/ planning/ orchestration/
│   │   ├── tools/ authorization/ execution/
│   │   ├── tasks/          tasks, runner, monitoring, notifications
│   │   ├── background/     the background runtime
│   │   ├── integrations/   policed HTTP client, OAuth, Google, search
│   │   ├── reminders/ calendar/ mail/ research/ workflows/ history/
│   │   └── main.py
│   ├── alembic/            database migrations (0001–0018)
│   └── tests/              behavioural tests; tests/security/ for security tests
├── frontend/               Next.js app (app/, components/, lib/)
├── docker/                 backend and frontend Dockerfiles
├── docs/                   architecture notes, stage reports, CHANGELOG
├── imports/                drop-zone for history exports (contents git-ignored)
├── docker-compose.yml
├── .env.example
└── AGENTS.md               engineering contract for contributors and coding agents
```

## Getting Started

### Prerequisites

- **Docker** with Docker Compose (recommended), or
- **Python 3.9+**, **Node.js 20+** and **PostgreSQL 14+** for a manual setup
- An API key for one LLM provider (the default is Groq)

### 1. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```env
POSTGRES_PASSWORD=<choose-a-password>
GROQ_API_KEY=<your-groq-api-key>
```

Optional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `groq` | `groq`, `gemini` or `anthropic_api` (set the matching `*_API_KEY`) |
| `SEARCH_PROVIDER` / `SEARCH_API_KEY` | `tavily` / empty | Web research provider (`tavily` or `brave`) and its key |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | empty | A "Desktop app" OAuth client, to enable Calendar and Gmail |
| `MAI_TIMEZONE` | `UTC` | IANA timezone used for "today" and "tomorrow" |
| `EXECUTION_ENABLED` | `false` | Enables the execution API and tool execution |
| `MEMORY_ENABLED` | `true` | Memory subsystem master switch |

[`.env.example`](.env.example) documents every setting. Never commit `.env`.

### 2a. Run with Docker (recommended)

```bash
docker compose up --build
```

- Frontend: <http://localhost:3000>
- Backend: <http://localhost:8000> (API explorer at `/docs`)

Migrations run automatically when the backend starts. Data persists in the
`mai_pgdata` Docker volume.

### 2b. Run manually

Start PostgreSQL, create a database and user, then set `DATABASE_URL` in
`backend/.env` to point at it (`postgresql+asyncpg://<user>:<password>@localhost:5432/<db>`).

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -c constraints.txt -r requirements.txt -r requirements-dev.txt
cp ../.env.example .env        # then edit it
alembic upgrade head
uvicorn app.main:app --reload
```

```bash
# Frontend
cd frontend
npm install
cp .env.example .env.local     # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev
```

[`docs/setup.md`](docs/setup.md) has a longer walkthrough, written at Stage 1.

## Testing

```bash
cd backend
source .venv/bin/activate
pytest
```

The suite runs against in-memory SQLite with the LLM provider mocked, so it
needs no database, no network and no API keys.

**Verified result (Stage 6H, commit `b6d35ff`)**: **5,565 passed, 2 skipped**
on Python 3.9, in both default and shuffled order (about 4 minutes).

| Suite | Contents |
| --- | --- |
| `backend/tests/` (89 files) | Behavioural tests through real services, routes and a real (SQLite) database |
| `backend/tests/security/` (52 files, 2,172 tests) | Structural AST audits, secret-leak, SSRF, authorization and content-isolation tests |

The two skips are environment-dependent: a secrets check that runs only when a
local `backend/.env` exists, and a PostgreSQL migration-chain test that runs
when `TEST_POSTGRES_URL` points at a PostgreSQL database.

Beyond the automated suite, each Stage 6 sub-stage (6A–6H) was verified with
a validated mutation-testing harness and live PostgreSQL checks (migrations,
concurrency and cleanup on a throwaway database). Results are recorded in
[`docs/CHANGELOG.md`](docs/CHANGELOG.md). There are no automated frontend
tests.

## Development Roadmap

**Completed**

| Stages | Theme |
| --- | --- |
| 1 | Foundation: chat, FastAPI, PostgreSQL, Alembic, provider abstraction, Docker |
| 2A – 3A | Memory, entities, relationships, retrieval, context assembly |
| 3B – 3D | Knowledge-aware prompting, conflict resolution, full security and privacy audit |
| 4A – 4E | Intent, planning, tool authorization, action proposal, controlled execution |
| 4F – 4H | Network boundary, web research, multi-provider gateway, read-only Google Calendar, briefings |
| 5A – 5F | Language robustness, read-only Gmail, freshness, synthesis reliability, history import, execution truthfulness, conversational continuity, reminders, mail triage |
| 6A – 6H | Persisted tasks, plan validation, capability binding, task runner, standing approvals, background runtime, monitoring, notifications |

**In progress**
- A Telegram adapter, developed as a separate integration track and not part
  of the main branch

**Planned**
- Notification delivery channels, as adapters over the notification contract
- A person-facing surface for tasks, monitoring and notifications (currently
  reachable only in-process)
- A single inbox across reminder and task notifications

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | Every stage: purpose, architecture, verification, known defects |
| [`AGENTS.md`](AGENTS.md) | Architectural invariants and engineering rules |
| [`docs/architecture.md`](docs/architecture.md) | Original Stage 1 system design |
| [`docs/stage2a_memory_architecture.md`](docs/stage2a_memory_architecture.md) | Memory pipeline |
| [`docs/stage2d_context_retrieval_architecture.md`](docs/stage2d_context_retrieval_architecture.md) | Retrieval and ranking |
| [`docs/stage3_security_audit.md`](docs/stage3_security_audit.md) | Full-system security and privacy audit |
| [`docs/stage4c_tool_authorization_architecture.md`](docs/stage4c_tool_authorization_architecture.md) | Tool registry and authorization |
| [`docs/stage4e_execution_architecture.md`](docs/stage4e_execution_architecture.md) | Controlled execution |
| [`docs/stage4fc_unified_network_boundary.md`](docs/stage4fc_unified_network_boundary.md) | The single outbound network boundary |
| [`docs/stage4ff_provider_gateway.md`](docs/stage4ff_provider_gateway.md) | LLM provider gateway |
| [`docs/stage5d1_execution_truthfulness.md`](docs/stage5d1_execution_truthfulness.md) | Execution truthfulness |

Stages 1–5 also have per-stage architecture notes and acceptance reports in
[`docs/`](docs/). Stage 6 is recorded in the changelog.

## Project Philosophy

- **Staged development:** each stage has a narrow scope, ends with full
  verification, and is one commit.
- **One path for each concern:** one authorization service, one execution
  constructor, one outbound HTTP client, one background loop. Structural tests
  stop a second one from appearing.
- **Deterministic where possible:** routing, reminder parsing, workflow plans
  and retrieval are application code, not model output.
- **Truthfulness over fluency:** the application, not the model, decides what
  happened.
- **Privacy by default:** local storage, read-only integrations, consent
  before web research, and no content in logs.

## Disclaimer / Current Limitations

- Single user, local only, with **no authentication**. Do not deploy Mai on a
  network.
- Gmail parsing is verified against fixtures modelled on Gmail's API rather
  than a live mailbox.
- Tasks, monitoring and standing grants have no UI or HTTP surface yet, and
  notifications are recorded but not delivered.
- Retrieval is lexical and graph-based, not semantic. There are no embeddings.
- Quality and cost depend on the LLM provider you configure.
- This is a personal engineering project and makes no production-readiness
  claims.
