# Mai — Setup

Two ways to run Mai: Docker (recommended) or directly on your machine.

## What you need first

**A Groq API key.** Mai cannot answer without one.

1. Sign up at <https://console.groq.com>
2. Create a key at <https://console.groq.com/keys> (starts `gsk_`)
3. Put it in `.env` as `GROQ_API_KEY`

Free tier: **30 requests/minute, 14,400/day**, no credit card. Available chat
models: `openai/gpt-oss-120b` (default), `openai/gpt-oss-20b`,
`qwen/qwen3.8-27b` — set `GROQ_MODEL` to change it.

Everything else — the database, migrations, dependencies — is automated.

---

## Option 1 — Docker (recommended)

**Requires:** Docker Desktop (or Docker Engine + Compose v2).

```bash
cd mai
cp .env.example .env
```

Open `.env` and set your key:

```
GROQ_API_KEY=your-key-here
```

Then:

```bash
docker compose up --build
```

First build takes a few minutes. When it settles:

| Service   | URL                            |
| --------- | ------------------------------ |
| Frontend  | <http://localhost:3000>        |
| Backend   | <http://localhost:8000>        |
| API docs  | <http://localhost:8000/docs>   |
| Health    | <http://localhost:8000/health> |

Migrations run automatically as the backend container starts.

### Checking it works

```bash
curl http://localhost:8000/health
```

`{"status":"ok",...}` means the database and key are both fine. `"degraded"`
means one of them is not — the response says which.

### Everyday commands

```bash
docker compose up               # start
docker compose up --build       # rebuild after dependency changes
docker compose down             # stop (data is kept)
docker compose down -v          # stop and DELETE the database volume
docker compose logs -f backend  # follow backend logs
docker compose exec backend alembic current   # current migration
```

Data lives in the `mai_pgdata` volume and survives `down`. Only `down -v`
destroys it.

---

## Option 2 — Running locally

**Requires:** Python 3.12+, Node 20+, PostgreSQL 14+.

### 1. Database

Create the database:

```bash
createdb mai
```

Or run just PostgreSQL in Docker and skip installing it:

```bash
docker compose up -d db
```

### 2. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create `backend/.env`:

```
DATABASE_URL=postgresql+asyncpg://mai:mai@localhost:5432/mai
GROQ_API_KEY=your-key-here
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_MODEL=openai/gpt-oss-120b
LOG_LEVEL=INFO
LOG_FORMAT=console
CORS_ORIGINS=http://localhost:3000
```

Apply migrations and start:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Backend is on <http://localhost:8000>.

### 3. Frontend

In a second terminal:

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

Frontend is on <http://localhost:3000>.

---

## Tests

```bash
cd backend
source .venv/bin/activate
pytest
```

```bash
pytest -v                        # verbose
pytest tests/test_chat_flow.py   # one file
```

No database, network, or API key needed — SQLite in memory, LLM mocked.

---

## Migrations

```bash
cd backend
alembic upgrade head          # apply
alembic current               # what is applied
alembic history               # all revisions
alembic downgrade -1          # undo the last one
```

After changing a model:

```bash
alembic revision --autogenerate -m "describe the change"
```

Read the generated file before applying it — autogenerate is a starting point,
not an authority.

---

## Troubleshooting

**`GROQ_API_KEY must be set in .env`** on `docker compose up`
Compose requires a key for the active provider. Set it in `.env` at the
repository root.

**`/health` shows `"llm": {"healthy": false}`**
`GROQ_API_KEY` is missing or empty. The backend still starts — this is intentional, so
the container reports degraded rather than crash-looping.

**`llm_auth_error` when sending a message**
The key is set but rejected by Groq. Check for a typo or an expired key.

**`llm_rate_limited` (429)**
You have exceeded Groq's free tier (30 requests/minute, 14,400/day). The
backend retries automatically and honours the `Retry-After` header; wait and
try again.

**Frontend says "Could not reach the Mai backend"**
The backend is not running, or `NEXT_PUBLIC_API_URL` points somewhere else.
Confirm with `curl http://localhost:8000/health`. Note this variable is inlined
at *build* time — changing it needs a frontend rebuild.

**`connection refused` to PostgreSQL**
Not running, or the wrong host. In Docker the host is `db`; locally it is
`localhost`.

**`InvalidPasswordError` after changing `POSTGRES_PASSWORD`**
The volume kept the old credentials. Reset it (this deletes the data):

```bash
docker compose down -v && docker compose up --build
```

**Port already in use**
Change `BACKEND_PORT` or `FRONTEND_PORT` in `.env`.

**`MissingGreenlet` errors after editing models**
Something is being lazily loaded outside async context. Load the relationship
eagerly, or set the value in Python rather than relying on a server default.
