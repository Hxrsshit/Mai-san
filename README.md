# Mai — Stage 1 Foundation

Mai is a long-term project: a persistent personal AI environment. This
repository currently contains **Stage 1 only** — a clean, production-minded
foundation for everything that comes later.

## Stage 1 scope

**In scope, and working:**

- Next.js chat interface (sidebar, message list, input, loading state)
- FastAPI backend with async SQLAlchemy
- PostgreSQL persistence for conversations and messages
- Alembic migrations
- Groq integration behind a provider-agnostic abstraction (adding a provider is one file plus one registry line)
- Conversation history replayed as model context
- Structured logging, error handling, and a health endpoint
- Docker development environment
- Test suite with the LLM mocked

**Deliberately out of scope** (later stages): long-term memory, vector
databases, RAG, agents, learning systems, autonomous behaviour, tool calling,
web search, image or document generation, task planning, personality learning.

## Technology stack

| Layer     | Choice                                             |
| --------- | -------------------------------------------------- |
| Frontend  | Next.js 15 (App Router), TypeScript, Tailwind CSS 4 |
| Backend   | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async) |
| Database  | PostgreSQL 16, Alembic migrations                   |
| Model     | Groq (`openai/gpt-oss-120b` by default), via an OpenAI-compatible API |
| Dev env   | Docker Compose                                      |

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env and set GROQ_API_KEY (from https://console.groq.com/keys)
docker compose up --build
```

- Frontend: <http://localhost:3000>
- Backend: <http://localhost:8000>
- API docs: <http://localhost:8000/docs>

Migrations run automatically when the backend container starts. Database
contents persist in the `mai_pgdata` Docker volume across restarts.

## Running locally without Docker

You need Python 3.12+, Node 20+, and a running PostgreSQL.

**Backend**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env   # set GROQ_API_KEY and DATABASE_URL
alembic upgrade head
uvicorn app.main:app --reload
```

**Frontend**

```bash
cd frontend
npm install
cp .env.example .env.local   # set NEXT_PUBLIC_API_URL
npm run dev
```

See [docs/setup.md](docs/setup.md) for step-by-step instructions.

## Cost and model selection

Groq's free tier is 30 requests/minute and 14,400/day, with no credit card.
Nothing bills by default.

Changing the model is one variable plus a restart:

```bash
GROQ_MODEL=openai/gpt-oss-20b
```

Available chat models: `openai/gpt-oss-120b` (default), `openai/gpt-oss-20b`,
`qwen/qwen3.8-27b`.

## Environment variables

| Variable               | Required | Default                        | Purpose                                     |
| ---------------------- | -------- | ------------------------------ | ------------------------------------------- |
| `LLM_PROVIDER`         | No       | `groq`                         | Provider implementation to build.           |
| `GROQ_API_KEY`         | **Yes**  | —                              | Groq key (`gsk_…`). Chat fails without it.  |
| `GROQ_MODEL`           | No       | `openai/gpt-oss-120b`          | Groq model id.                              |
| `GROQ_BASE_URL`        | No       | `https://api.groq.com/openai/v1` | Gateway base URL.                         |
| `DATABASE_URL`         | Yes      | `postgresql+asyncpg://mai:mai@localhost:5432/mai` | Must use the `asyncpg` driver. |
| `LLM_TIMEOUT_SECONDS`  | No       | `60`                           | Per-request timeout.                        |
| `LLM_MAX_RETRIES`      | No       | `2`                            | Retries on transient failures.              |

| `MAX_CONTEXT_MESSAGES` | No       | `40`                           | Stored messages replayed per turn.          |
| `LOG_LEVEL`            | No       | `INFO`                         | Log threshold.                              |
| `LOG_FORMAT`           | No       | `json`                         | `json` or `console`.                        |
| `CORS_ORIGINS`         | No       | `http://localhost:3000`        | Comma-separated allowed origins.            |
| `NEXT_PUBLIC_API_URL`  | Yes      | `http://localhost:8000`        | Backend URL used by the browser.            |

Secrets are only ever read from the environment. Nothing is hardcoded, and the
API key is never written to logs.

## API

| Method   | Path                                       | Purpose                          |
| -------- | ------------------------------------------ | -------------------------------- |
| `GET`    | `/health`                                  | Database and LLM status          |
| `POST`   | `/api/conversations`                       | Create a conversation            |
| `GET`    | `/api/conversations`                       | List conversations               |
| `GET`    | `/api/conversations/{id}`                  | Conversation with messages       |
| `PATCH`  | `/api/conversations/{id}`                  | Rename a conversation            |
| `DELETE` | `/api/conversations/{id}`                  | Delete a conversation            |
| `POST`   | `/api/conversations/{id}/messages`         | Send a message, get Mai's reply  |
| `GET`    | `/api/conversations/{id}/messages`         | List a conversation's messages   |

Errors always use one envelope:

```json
{ "error": { "code": "llm_timeout", "message": "…", "request_id": "…" } }
```

## Running tests

```bash
cd backend
source .venv/bin/activate
pytest
```

The suite runs against in-memory SQLite with the LLM provider mocked, so it
needs no database, no network, and no API key.

## Documentation

- [docs/architecture.md](docs/architecture.md) — system design, request flow, database, LLM abstraction
- [docs/setup.md](docs/setup.md) — detailed local setup
- [docs/stage1_acceptance_report.md](docs/stage1_acceptance_report.md) — Stage 1 verification results

## Project layout

```
mai/
├── backend/
│   ├── app/
│   │   ├── api/          routes, dependencies, error handlers, middleware
│   │   ├── core/         config, logging, error types
│   │   ├── database/     engine, session, ORM models
│   │   ├── llm/          provider abstraction + Groq implementation
│   │   ├── schemas/      Pydantic request/response models
│   │   ├── services/     conversation and chat orchestration
│   │   └── main.py
│   ├── alembic/          migrations
│   └── tests/
├── frontend/             Next.js app, components, API client
├── docker/               backend and frontend Dockerfiles
├── docs/
└── docker-compose.yml
```
