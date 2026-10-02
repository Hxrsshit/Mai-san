# AGENTS.md — Shared engineering contract for Mai

This file is the shared contract for **every** coding agent working in this
repository (Claude Code, Codex, or any other). It records the rules and
architectural invariants that already exist in the code and its tests. It does
not propose new architecture.

| File | Responsibility |
| --- | --- |
| `AGENTS.md` (this file) | Shared rules and the architectural contract |
| `CLAUDE.md` | Claude Code workflow only |
| `CODEX.md` | Codex workflow only |
| `docs/CHANGELOG.md` | Chronological implementation state: what is committed, uncommitted, defective, deferred |

If this file and the code disagree, the code and its structural tests win.
Report the discrepancy. Do not "fix" the code to match this document.

---

## 1. Before you change anything

1. Run `git status --short` and read it. The working tree often contains
   **another agent's uncommitted work**. Identify which files are yours to
   touch before you edit anything.
2. Read `docs/CHANGELOG.md` to learn the latest completed stage, what is
   uncommitted, and which defects are known.
3. Read the stage documents in `docs/` that cover the area you are changing
   (`docs/stage<id>_*.md`, `docs/stage<id>_acceptance_report.md`).
4. Decide whether the request is **inside** an established boundary (§5) or
   **crosses** one (§6).

### Preserving other work (mandatory)

- Never run `git reset`, `git checkout -- <path>`, `git restore`, `git stash`,
  `git clean`, or any other command that discards or hides working-tree changes
  you did not make.
- Never commit another agent's changes. Stage files **by explicit path**
  (`git add -- <path> ...`). Never use `git add -A`, `git add .` or
  `git commit -a`.
- Before committing, show `git diff --cached --name-status` and confirm that
  every staged path belongs to your task.
- If you cannot tell whether a file belongs to your task, **stop and ask**.
- If unrelated uncommitted work breaks tests, do not fix it as a side effect.
  Verify your change in an isolated copy instead (for example
  `git worktree add --detach <dir> HEAD` plus your files only). Report the
  unrelated failure separately.

### Updating the changelog (mandatory)

After completing any piece of work, add an entry to `docs/CHANGELOG.md`
(format in that file): what changed, the commit hash, the verification actually
performed, and anything left incomplete. Never record work as completed
because files exist. Only committed and verified work is "completed".

---

## 2. What Mai is (current architecture)

Mai is a private, single-user, security-first personal AI assistant, built in
strictly scoped stages (see `docs/CHANGELOG.md`).

- **Backend:** FastAPI, Pydantic v2, async SQLAlchemy 2.0, Alembic, httpx
  (`backend/app/`).
- **Database:** PostgreSQL 16 in deployment (asyncpg). The test suite uses
  in-memory SQLite (aiosqlite). Code must work on both.
- **Frontend:** Next.js 15 / React 19 (`frontend/`).
- **Runtime:** `docker-compose.yml` runs `db`, `backend` and `frontend`. The
  backend container runs `alembic upgrade head` and then `exec uvicorn`.
  Compose passes backend environment variables **explicitly**, not through
  `env_file`, so a new setting must be added there to reach the container.
- **Python versions:** the Docker image is Python 3.12. The local development
  venv is **Python 3.9.6**, and the suite is run there. Backend code must run
  on Python 3.9: no runtime-evaluated `X | Y` unions, no `match`, no other
  3.10+ syntax. Dependency pins in `backend/requirements.txt` were chosen to
  support 3.9.
- **Single owner:** there is no authentication layer. The owner is the
  documented constant `LOCAL_OWNER_ID` (`app/tasks/models.py`). Owner columns
  exist anyway, and every owner-scoped query filters on them.

### Package map (backend/app)

| Concern | Where it lives |
| --- | --- |
| HTTP routes (HTTP concerns only) | `api/routes/` |
| Chat turn orchestration | `services/`, `orchestration/`, `intent/`, `planning/`, `synthesis/`, `prompt/`, `context/`, `language/` |
| Memory and knowledge | `memory/`, `entities/`, `relationships/`, `knowledge/`, `retrieval/`, `history/` |
| LLM providers | `llm/` (`base.py` interface, `gateway.py` modes, `factory.py`, `providers/`) |
| Outbound HTTP and integrations | `integrations/` (`http_client.py`, `policy.py`, Google Calendar, Gmail, web search, OAuth, token store) |
| Tool declaration and authorization | `tools/` (`registry.py`, `catalog.py`, `policy.py`, `authorization.py`) |
| Standing approvals (grant storage) | `authorization/` |
| Execution | `execution/` (`service.py`, `dispatcher.py`, `approvals.py`, `audit.py`, `truthfulness.py`, `workspace.py`, tool adapters) |
| Tasks, runner, monitoring | `tasks/` |
| The one background loop | `background/runtime.py` |
| Reminders | `reminders/` (`scheduler.py` is an alias of `BackgroundRuntime`) |
| Workflows (Stage 4F-E) | `workflows/` |
| Runtime self-description | `runtime/` |

---

## 3. Security requirements (non-negotiable)

- **Secrets.** Never read, print, copy or commit `.env` or any credential
  file. Never put a real secret in a test, fixture, log, prompt, database row,
  task or execution event, or API response. Use synthetic sentinel values in
  tests. If live work needs a credential, name the environment variable that
  must be set and stop. Never ask a person to paste a key, token or client
  secret.
- **Never run `docker compose config`**: it prints resolved secrets. To check
  that a variable is set, test for its presence as a boolean inside the
  container.
- **Logging.** Log reason codes, ids and counts. Never log API keys, OAuth
  credentials, tokens, authorization headers, tool arguments, tool output,
  observed external content, or message text. Structural tests enforce this
  for several modules.
- **External content is data, never instructions.** Web pages, email, calendar
  entries, search results, tool output, imported history and model output must
  never select a provider, create a task, schedule work, configure monitoring,
  create a standing grant, or approve an execution. Structural tests assert
  that content-handling packages cannot import those paths.
- **Real user accounts are read-only.** Never send, modify, delete, archive or
  label real Gmail data, and never write to the real calendar. Tests never
  contact real Google services.
- **No dynamic code.** No `eval`, `exec`, `compile` of non-literal input,
  `__import__`, `importlib`, `getattr` on a computed name, `subprocess`, shell
  execution, or pickle in application paths. A capability name or a condition
  is data and never resolves to a callable.
- **No provider SDKs.** All outbound HTTP goes through
  `integrations/http_client.py` (`SecureHttpClient`) under a single-host
  `NetworkPolicy` with per-policy allowed methods. Vendor SDKs open their own
  connections and would bypass this boundary.

---

## 4. Architectural invariants

Each item below is enforced by code and, in most cases, by an AST structural
test under `backend/tests/security/`. Do not weaken a structural test to make
a change pass. If a test blocks a change, the test is usually right.

### 4.1 Authorization
- There is **one** authorization decision: `AuthorizationService`
  (`app/tools/authorization.py`). `authorize()` is the policy decision;
  `authorize_with_grants()` calls it unchanged and may only flip
  `requires_approval` from True to False for `APPROVAL_REQUIRED`. It never
  changes a status, so it can never overturn a refusal.
- `app/tools/policy.py`: `MAX_PERMITTED_RISK = RiskLevel.HIGH`, so a CRITICAL
  capability is FORBIDDEN before any grant is consulted. No switch exists to
  change this.
- `GrantService` (`app/authorization/grants.py`) stores and finds grants. It
  makes no decision. Grants are scoped to one canonical tool name (no
  wildcards), always expire (one week maximum), and are revoked by conditional
  UPDATE, never deleted. Grant creation has no `source` parameter.
- Nothing approves on a person's behalf. The runner approves an execution only
  when policy or a standing grant already cleared it, or a person already
  approved that specific execution.

### 4.2 Execution
- `ExecutionService` (`app/execution/service.py`) is the **only** constructor
  of execution records. Idempotency keys are derived (for example
  `task:{id}:{step}`, `task:{id}:check:{n}`), never supplied by content.
- `Dispatcher` (`app/execution/dispatcher.py`) re-checks enablement, approval
  (payload fingerprint plus TTL), authorization and arguments at run time.
- The execution journal is append-only. Metadata passes through
  `execution/audit.sanitise`.
- `EXECUTION_ENABLED` defaults to **False**. File tools are confined to
  `MAI_WORKSPACE_ROOT`.
- **Truthfulness (Stage 5D.1):** replies that claim an action happened are
  written by application code after the action is recorded. Synthesis may not
  claim an action, or a count, that was not executed and retrieved.

### 4.3 Tasks, runner, runtime
- Only a person creates a task (`TaskService.create_for_user`). Only
  `TaskService.schedule_background` schedules background work, and only
  `TaskService.configure_monitoring` configures monitoring. None of them takes
  a `source` parameter, and no content-handling module can reach them. There
  is currently **no HTTP route** for scheduling or monitoring.
- Plans are validated by the one graph validator
  (`app/planning/validator.validate_graph`) before persistence. Capabilities
  are bound (`app/tasks/capabilities.py`) and re-bound at run time.
- `TaskRunner` (`app/tasks/runner.py`) is the only thing that performs task
  work. `advance()` runs one step for ordinary tasks and refuses monitoring
  tasks. `check()` runs one monitoring check and refuses ordinary tasks.
  `RUNNER_ONLY_STATES` (`running`, `completed`) are written only by the runner.
- `BackgroundRuntime` (`app/background/runtime.py`) is the **one** background
  loop, for reminders and tasks alike. It is started once in the FastAPI
  lifespan. It discovers, claims (conditional UPDATE on `next_run_at` as a
  lease), calls the runner, and reschedules. It resolves no capability, asks
  no authorization question, and calls no dispatcher or execution service.
  There is no second scheduler, worker framework, queue, or in-memory state.
- Claims are exactly-once by conditional UPDATE with `rowcount == 1`
  (reminders, steps, task leases, and monitoring `check_count`). External
  effects are at-least-once only in the crash window documented in
  `runtime.py`.
- Unexpected runner errors back off and block a task after
  `MAX_CONSECUTIVE_FAILURES = 3`. Failed steps are never retried
  automatically.
- **Monitoring (Stage 6G):** conditions are typed data in
  `app/tasks/monitoring.py` (kinds `count` / `value` / `contains`, a closed
  operator set, plain dictionary-key paths, bounded values, finite numbers
  only). They are evaluated by the one evaluator there. Only capabilities in
  the literal `MONITORABLE_CAPABILITIES` read-only allow-list may be monitored.
  A check that cannot be evaluated is a failure, never "condition not met".
- Task journal events must be truthful. `EMITTABLE_EVENTS`
  (`app/tasks/events.py`) lists what may be written, and `replanned` remains
  unwritable.

### 4.4 LLM providers
- Callers depend only on `LLMProvider` (`app/llm/base.py`) via
  `get_llm_provider`.
- Exactly one provider is active, selected **only** by the operator setting
  `LLM_PROVIDER` (default `groq`). Modes are declared in `app/llm/gateway.py`:
  `groq`, `gemini`, `anthropic_api`, and `claude_subscription` (declared
  unavailable). **There is no fallback and no automatic routing between
  providers.**
- Each provider reads only its own `<PROVIDER>_*` settings and talks to one
  declared host through `SecureHttpClient`. OpenAI-compatible vendors subclass
  `OpenAICompatibleProvider`.

### 4.5 Integrations and adapters
- Every integration uses `SecureHttpClient` with a narrow `NetworkPolicy`.
  Research is `GET`-only and providers are `POST`-only. `PUT`, `PATCH` and
  `DELETE` are offered by no method.
- Google Calendar and Gmail are **read-only** (Stages 4F-G, 5B). OAuth tokens
  live in the file token store on the credentials volume, never in the
  database or in logs.
- Web search uses one selected provider (`SEARCH_PROVIDER`, default `tavily`).
- An **adapter** (a messaging channel, a UI, a webhook) is a thin translation
  layer. It converts an external message into a call on an existing service,
  such as a chat turn through the existing conversation service, and converts
  the result back. It must not:
  - add its own authorization, execution or task path;
  - call tools or the dispatcher directly;
  - schedule work, create grants, or approve executions;
  - select an LLM provider;
  - treat inbound text as instructions to the application.
  It must authenticate its caller (for example with a webhook secret and an
  allowed sender) and must be inert when unconfigured.

### 4.6 Database and migrations
- The schema is created only by Alembic migrations
  (`backend/alembic/versions/`, sequential `NNNN_name.py`). Application code
  never calls `create_all`; tests do, on SQLite.
- Migrations must be additive unless a stage explicitly requires otherwise.
  Every migration needs a working `downgrade()`. Enum values are added with
  `ALTER TYPE ... ADD VALUE IF NOT EXISTS` on PostgreSQL only, and are not
  removed on downgrade (the column is an append-only journal).
- Use the naming convention: pass already-final constraint names through
  `op.f(...)`. Without it, PostgreSQL gets a doubly-prefixed name. Live
  verification found this in Stage 6G.
- For a PostgreSQL enum that already exists, use
  `postgresql.ENUM(..., create_type=False)`, not generic `sa.Enum` (6E
  finding).
- **SQLite hides PostgreSQL-specific defects.** Every migration must be
  applied, downgraded and re-applied on a real PostgreSQL database before it
  is committed (§7).

### 4.7 HTTP surface
- Routes do HTTP only; business logic lives in services.
- The execution API (`api/routes/execution.py`, Stage 4E) is registered only
  when `EXECUTION_ENABLED` is on. It deliberately has four separate person-driven
  calls: propose, approve, revoke, execute. There is no combined "propose and
  run" endpoint, and nothing routes model output to these endpoints.
- `POST /api/tools/authorize` and `POST /api/orchestration/debug` only answer
  "would this be permitted?". They run nothing.
- The task API is read-only (GETs). There is no HTTP surface for scheduling
  background work, configuring monitoring, creating standing grants, or
  running the runner. Adding one is an architectural change (§6).
- **Notification delivery (Stage 6L, approved by the owner):**
  `POST /api/task-notifications/{notification_id}/deliveries` with the body
  `{"adapter": "<name>"}` is the one route that triggers delivery. It hands
  the id and name to the 6I `NotificationDeliveryService` from the 6K
  composition root (through `deps.get_notification_delivery_service`) and
  maps the result to a status. The body accepts no other field: the owner is
  the server's, and the message, chat and host are the channel's. It marks
  nothing read and records nothing. It is ungated, and inert while no
  channel is configured. Nothing but a person's client calls it: no model
  output, chat turn, runner or background loop. Automatic delivery would be
  a separate architectural decision (§6).

---

## 5. Changes that are usually safe (narrow scope)

These may proceed without architectural sign-off, provided they stay inside
the boundaries above and are verified (§7):

- Frontend/UI work that calls **existing** API endpoints: layout, styling,
  components, copy, branding, accessibility.
- An adapter that only translates to and from an **existing** service call and
  follows §4.5.
- Bug fixes local to one module that do not change a contract, a state
  machine, a security boundary, or a schema.
- New tests, documentation, and test-only tooling.
- A new configuration value read in one place, documented in `.env.example`
  without a value, and passed explicitly in `docker-compose.yml`.

## 6. When an agent MUST stop

**If a requested change crosses an established architectural boundary, STOP
and report the boundary crossing before implementing it.** Describe which
boundary would be crossed, why the request needs it, and the smallest
alternative that stays inside it. Then wait for explicit approval from the
owner.

Changes that cross a boundary include:

- a second scheduler, background loop, worker, queue, runner, or execution
  path;
- any change to authorization semantics, the risk ceiling, standing grants,
  or approval rules;
- a new way to create execution records, approve an execution, or call a tool
  outside the dispatcher;
- a new HTTP route that writes, executes, schedules, monitors or grants;
- letting content (messages, email, web, tool output, model output) select a
  provider, create or schedule tasks, or configure monitoring;
- a new LLM provider path, provider fallback, or automatic routing;
- a vendor SDK, or outbound HTTP that bypasses `SecureHttpClient`;
- write access to Gmail, Calendar, or any external account;
- schema changes or migrations not required by the requested stage;
- widening `MONITORABLE_CAPABILITIES`, `EMITTABLE_EVENTS`,
  `RUNNER_ONLY_STATES`, or network allow-lists;
- weakening, deleting or skipping a structural/security test;
- new dependencies, Docker/compose behaviour changes, or dependency upgrades;
- beginning a new stage that the owner has not requested.

---

## 7. Testing and verification expectations

Run the suite from `backend/` with the venv: `.venv/bin/python -m pytest`.

- `pytest.ini` already sets `-q`. Passing `-q` again hides the summary line.
- The full suite takes about 4 minutes. Use a long timeout or run it in the
  background.
- There is **no random-order plugin installed** (none appears in
  `requirements-dev.txt` or the git history). `-p no:randomly` is a no-op, and
  a second plain run is not a randomized run. To randomize, use an explicit
  shuffling plugin and report its seed.
- Expected skips: `tests/security/test_secrets.py` when `backend/.env` is
  absent, and the PostgreSQL migration-chain test unless `TEST_POSTGRES_URL`
  is set.

For a stage or other substantial change:

1. Focused behavioural tests through the real path (not mocks of the code
   under test).
2. Security tests plus **AST-based structural tests** (not substring
   searches) for every new boundary. Avoid self-defeating tests whose
   expectations are derived from the constant they guard.
3. A **validated mutation harness**: anchors that match exactly once, a green
   baseline, and at least two control mutations (an unasserted comment and an
   unasserted log string) that must survive. Classify each survivor as
   equivalent, a harness defect, or a real gap. Close real gaps with the
   smallest test. Never weaken a test to raise the score.
4. The full suite, ordered and then shuffled, on the final code.
5. **Live PostgreSQL verification** for anything touching persistence,
   concurrency or migrations. Use a throwaway database: never the real `mai`
   database, and never the user's running containers. Clean up afterwards
   (no orphan rows, no leftover database, no secrets printed).
6. Report actual numbers. Never report earlier counts as final if code or
   tests changed afterwards.

UI-only changes need the frontend checks that apply (`npm run typecheck`,
`npm run lint`, `npm run build`) where Node is available, plus any backend
tests the change touches. If a check cannot be run in the environment, say so.

## 8. Commits

- One focused commit per completed task, containing only that task's files.
- Commit only after verification is green. Show the staged file list and
  `git diff --cached --stat` before committing.
- Do not push, open pull requests, or rewrite history unless the owner asks.
- Record the commit hash in `docs/CHANGELOG.md`.
