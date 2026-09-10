# Mai — Stage 1 Foundation

Mai is a long-term project: a persistent personal AI environment. This
repository currently contains **Stage 1 only** — a clean, production-minded
foundation for everything that comes later.

## Stage 1 scope

**Stage 2A adds** a memory foundation: Mai analyses each completed turn and
selectively stores meaningful information as structured, validated,
deduplicated memories. See
[docs/stage2a_memory_architecture.md](docs/stage2a_memory_architecture.md).

**Stage 2B adds** an entity system: the people, projects, companies,
technologies and concepts named inside those memories are extracted,
normalized, deduplicated and linked back to the memories that mention them.
See [docs/stage2b_entity_architecture.md](docs/stage2b_entity_architecture.md).

**Stage 2C adds** a relationship system: directional connections between those
entities (`Mai —USES→ PostgreSQL`), each traceable to the memories that support
it. See
[docs/stage2c_relationship_architecture.md](docs/stage2c_relationship_architecture.md).

**Stage 2D adds** context retrieval: before each reply, Mai deterministically
retrieves relevant memories, entities and relationships and assembles them into
a bounded context package — with **no additional model calls**. See
[docs/stage2d_context_retrieval_architecture.md](docs/stage2d_context_retrieval_architecture.md).

**Stage 3A adds** a context assembly layer: it consumes Stage 2D's ranked
result and combines it with the current message and recent conversation into a
bounded, structured `ContextPackage` — categories kept separate, budgets
enforced, nothing mutated. See
[docs/stage3a_context_assembly_architecture.md](docs/stage3a_context_assembly_architecture.md).

**In scope, and working:**

- Next.js chat interface (sidebar, message list, input, loading state)
- FastAPI backend with async SQLAlchemy
- PostgreSQL persistence for conversations and messages
- Alembic migrations
- Groq integration behind a provider-agnostic abstraction (adding a provider is one file plus one registry line)
- Conversation history replayed as model context
- Structured logging, error handling, and a health endpoint
- **Stage 2A:** memory extraction, validation, deduplication, and inspection API
- **Stage 2B:** entity extraction, normalization, resolution, aliases, and memory links
- **Stage 2C:** directional entity relationships with evidence and deduplication
- **Stage 2D:** deterministic context retrieval, ranking and knowledge assembly
- **Stage 3A:** context assembly into a bounded, structured `ContextPackage`
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
pip install -c constraints.txt -r requirements.txt -r requirements-dev.txt
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
| `MEMORY_ENABLED`       | No       | `true`                         | Memory subsystem master switch.             |
| `MEMORY_EXTRACTION_ENABLED` | No  | `true`                         | Automatic extraction after each turn.       |
| `MEMORY_MIN_IMPORTANCE`| No       | `5`                            | Minimum importance to store (1-10).         |
| `MEMORY_MIN_CONFIDENCE`| No       | `0.7`                          | Minimum confidence to store (0.0-1.0).      |
| `ENTITY_EXTRACTION_ENABLED` | No  | `true`                         | Extract entities after each stored memory.  |
| `ENTITY_MIN_CONFIDENCE`| No       | `0.7`                          | Minimum confidence to store an entity.      |
| `ENTITY_EXTRACTION_MAX_PER_MEMORY` | No | `10`                  | Cap on entities per memory.                 |
| `RELATIONSHIP_EXTRACTION_ENABLED` | No | `true`                  | Extract relationships after entities.       |
| `RELATIONSHIP_MIN_CONFIDENCE` | No | `0.7`                       | Minimum confidence to store a relationship. |
| `RELATIONSHIP_EXTRACTION_MAX_PER_MEMORY` | No | `10`             | Cap on relationships per memory.            |
| `RETRIEVAL_ENABLED`    | No       | `true`                         | Retrieve knowledge before each reply.       |
| `RETRIEVAL_MAX_MEMORIES` | No     | `10`                           | Memories in the assembled context.          |
| `RETRIEVAL_MAX_CONTEXT_CHARS` | No | `8000`                        | Hard cap on assembled context size.         |
| `RETRIEVAL_CANDIDATE_POOL_SIZE` | No | `50`                        | Rows considered before ranking.             |
| `CONTEXT_RECENT_MESSAGE_LIMIT` | No | `12`                         | Recent messages in the context package.     |
| `CONTEXT_MAX_MEMORY_ITEMS` | No  | `10`                            | Memories in the context package.            |
| `CONTEXT_MAX_TOTAL_CHARS` | No   | `10000`                         | Final authority over all category limits.   |
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
| `GET`    | `/api/memories`                            | List memories (filter, paginate) |
| `GET`    | `/api/memories/{id}`                       | Retrieve one memory              |
| `DELETE` | `/api/memories/{id}`                       | Delete a memory                  |
| `GET`    | `/api/entities`                            | List entities (filter, paginate) |
| `GET`    | `/api/entities/{id}`                       | Entity with aliases + memory count |
| `GET`    | `/api/entities/{id}/memories`              | Memories referencing an entity   |
| `DELETE` | `/api/entities/{id}`                       | Delete an entity (memories kept) |
| `GET`    | `/api/entities/{id}/relationships`         | Incoming + outgoing relationships |
| `GET`    | `/api/relationships`                       | List relationships (filter, paginate) |
| `GET`    | `/api/relationships/{id}`                  | One relationship with evidence count |
| `GET`    | `/api/relationships/{id}/evidence`         | Memories supporting the claim    |
| `DELETE` | `/api/relationships/{id}`                  | Delete a relationship (entities kept) |
| `POST`   | `/api/retrieval/debug`                     | Explain what retrieval returns for a query |
| `GET`    | `/api/conversations/{id}/context-preview`  | Knowledge that would be assembled now |
| `POST`   | `/api/context/debug`                       | The assembled `ContextPackage` for a message |

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
- [docs/stage2a_memory_architecture.md](docs/stage2a_memory_architecture.md) — memory pipeline, schema, deduplication
- [docs/stage1_acceptance_report.md](docs/stage1_acceptance_report.md) — Stage 1 verification results
- [docs/stage2b_entity_architecture.md](docs/stage2b_entity_architecture.md) — entity pipeline, normalization, resolution
- [docs/stage2a_acceptance_report.md](docs/stage2a_acceptance_report.md) — Stage 2A verification results
- [docs/stage2c_relationship_architecture.md](docs/stage2c_relationship_architecture.md) — relationship pipeline, direction, evidence
- [docs/stage2b_acceptance_report.md](docs/stage2b_acceptance_report.md) — Stage 2B verification results
- [docs/stage2d_context_retrieval_architecture.md](docs/stage2d_context_retrieval_architecture.md) — retrieval, ranking, assembly
- [docs/stage2c_acceptance_report.md](docs/stage2c_acceptance_report.md) — Stage 2C verification results
- [docs/stage3a_context_assembly_architecture.md](docs/stage3a_context_assembly_architecture.md) — context assembly and budgeting
- [docs/stage2d_acceptance_report.md](docs/stage2d_acceptance_report.md) — Stage 2D verification results
- [docs/stage3a_acceptance_report.md](docs/stage3a_acceptance_report.md) — Stage 3A verification results

## Project layout

```
mai/
├── backend/
│   ├── app/
│   │   ├── api/          routes, dependencies, error handlers, middleware
│   │   ├── core/         config, logging, error types
│   │   ├── database/     engine, session, ORM models
│   │   ├── llm/          provider abstraction + Groq implementation
│   │   ├── memory/       Stage 2A: extraction, validation, dedup, storage
│   │   ├── entities/     Stage 2B: entity extraction, normalization, resolution
│   │   ├── relationships/ Stage 2C: relationship extraction, evidence, direction
│   │   ├── retrieval/    Stage 2D: query analysis, ranking, retrieval result
│   │   ├── context/      Stage 3A: context assembly, budgeting, ContextPackage
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
