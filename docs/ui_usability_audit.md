# UI and product usability audit (Phase 2A)

Date: 2026-10-13. Scope: map what the backend already exposes to what the
frontend shows, before any UI is written. Method: read `AGENTS.md`, the
changelog, every route and schema named below, and the frontend source. Every
endpoint listed was verified in a route file, not inferred from a service
class. Nothing here proposes new architecture.

*Status, 2026-10-16:* this records what was found on 2026-10-13. The UI it
led to is committed as Stage 0 C3 (`7ac52b4`) and is not yet deployed; the
monitor fields of section 4.1 are committed as C2 (`30a1342`) and `/health`
`reachability` as C4 (`4b8d407`). Statements those commits made untrue are
marked *Updated 2026-10-16*.

## 1. Frontend as found

| Area | Finding |
| --- | --- |
| Structure | Next.js 15 app router, one page (`app/page.tsx`): sidebar (conversations, integrations, history import) plus a chat pane. No routes, no tabs, no shared status model. |
| API client | `lib/api.ts`: one `request()` that adds the bearer token, shares one token-entry dialog, retries after auth failures (6M.4). Wrappers exist for conversations, history import and integrations only. |
| Design system | Tailwind 4, CSS variables in `globals.css` (`--color-*`), hand-written components. No component library. |
| Tests | `node --test tests/*.test.ts` (Node strips types). One file, 21 tests, all on the token flow. No DOM library, no JSX support in the test runner. Baseline: type check clean, 21/21. |
| Dependencies | `next`, `react`, `react-dom` only. Adding any package is an `AGENTS.md` §6 boundary, so none is added. |

### Defects found in the existing UI

1. **Hard-coded green "Present"** in the chat header (`app/page.tsx`). It is
   a constant; it reflects nothing.
2. **Integration "Connected" in green** (`IntegrationsPanel.tsx`) whenever the
   backend says `connected: true`. That field means *a token file with enough
   scopes exists* (`google_calendar.py` `credential_state`). Nothing has asked
   Google whether it still works.
3. **"Checking…" shown forever on an error.** `refresh()` swallows the error
   and sets the status to `null`, which renders as "Checking…". The code
   comment defends this as honesty; it is the opposite: an unreachable backend,
   an expired session and a 500 all look like a slow check.
4. **Raw reason codes shown to the user.** FastAPI `HTTPException`s are turned
   into `{"error": {"code": "http_error" | "not_found", "message": "<detail>"}}`
   (`api/errors.py`). The client only maps `code`, so a refused cancel or a
   missing notification would display `task_is_terminal` or
   `notification_not_found` verbatim.
5. **Provider-specific wording that can be wrong.** `llm_not_configured` and
   `llm_auth_error` tell the user to check `GROQ_API_KEY` whichever provider is
   active (Gemini and Anthropic are selectable by the operator).
6. **Keyboard-unreachable delete button.** The conversation delete control is
   `hidden group-hover:block`: `display: none` until hovered, so it cannot take
   focus.
7. **Messages are plain text.** Assistant replies with Markdown show literal
   `**` and backticks.

## 2. Backend surface, verified

All routes below are mounted with `dependencies=[Depends(get_owner_id)]`
(`main.py`), so each needs the bearer token and is owner-scoped. Exceptions
are `GET /health` (public) and the OAuth callbacks.

### Tasks and background activity (`api/routes/tasks.py`)

| Route | Behaviour verified |
| --- | --- |
| `GET /api/tasks?state=&limit=` | Owner's tasks, newest first, `limit` 1 to 50, repeated `state` filters, returns `total`. **No cursor**, so "recent tasks" is the honest scope. A database error returns an empty list with `total: 0`, indistinguishable from "none" (`TaskService.list_tasks` swallows `_DB_ERRORS`). |
| `GET /api/tasks/{id}` | Task plus steps plus the whole event journal plus the stored plan. Owner-scoped (another owner's id is a 404). |
| `GET /api/tasks/activity` | Six grouped answers; runs 12 queries. Not used: the plain list is cheaper and carries the same rows. |
| `POST /api/tasks/{id}/cancel` body exactly `{}` | 200 with the task once cancelled (idempotent), 404 `task_not_found`, 409 `task_is_terminal`, 500 `task_not_cancelled` if persistence failed. Uses `MonitorRequests.cancel` (6N), which releases a monitor's grant. |

Task states in the API: `proposed, planned, awaiting_approval, queued,
running, paused, blocked, completed, failed, cancelled`. Terminal: completed,
failed, cancelled. Timestamps: `created_at`, `updated_at`, `completed_at`,
`cancelled_at`. **Failed tasks have no end timestamp of their own**; the UI
shows "last updated" for them and says so.

`TaskRead` also carries `owner_id` and `budget`/`spent`; the UI types omit
`owner_id` and never render it. `result` is application-written text (max
4000); `error_code` is an application reason code. Both are rendered as plain
text, never as Markdown.

### Monitoring: what the API does and does not say

The `tasks` row has `monitor` (condition and interval), `next_run_at` and
`check_count`. **As found, none of these were in `TaskRead` or `TaskDetail`.**
(*Updated 2026-10-16:* all three are now, read from the row; see section
4.1.) What the API exposed, through the event journal in `GET /api/tasks/{id}`:

| Wanted | Available from | Notes |
| --- | --- | --- |
| Is this task a monitor? | an event `monitoring_configured` | Only in task *detail*, not in the list. |
| What it checks | that event's metadata: `kind`, `operator`, `interval_seconds`; plus the task `objective` and `deadline` | The condition's `path` and `expected` value are deliberately not journaled (user text). The objective (e.g. "Watch Gmail for …") is the human description. |
| Last execution | latest `observation_recorded` / `monitoring_check_failed` / `monitoring_triggered` event time | Real, journaled. |
| Most recent outcome | that event's `result` / `reason` metadata | Observed text is never journaled, only counts. |
| Still active? | task `state` (non-terminal) | Backend-authoritative. |
| **Next scheduled execution** | **not exposed** | `next_run_at` exists only on the row. Adding "last check + interval" would be a guess; the UI says "not reported by the API". |

Consequence, as found: listing monitors cost one detail request per candidate
task, and a smallest backend change that removes the N+1 was proposed in
section 4. (*Updated 2026-10-16:* with that change, the Monitors view makes two
list calls, up to 50 active and 10 recently ended tasks, and no per-task detail
request; the journal is read only for a monitor a person opens, keyed by
`(id, updated_at)`. See `frontend/lib/monitors.ts`.)

### Notifications (two separate stores)

**Task notifications** (monitoring outcomes; `notification_delivery.py`, 6M.2):

| Route | Behaviour verified |
| --- | --- |
| `GET /api/task-notifications?limit=&before=&unread_only=` | Newest first, max 50, keyset by `before`, `next_before` for the next page. Items: `id, task_id, kind (condition_met \| monitoring_failed), check_number, execution_id, created_at, read_at, deliveries[]`. **No text, by design.** |
| `GET /api/task-notifications/{id}` | One item with delivery state. |
| `POST /api/task-notifications/{id}/read` body `{}` | 204, idempotent; 404 if absent. Writes only `read_at`. |
| `POST …/deliveries` | Not used: not in this phase's scope and it sends externally. |

Delivery state per channel is `sending \| delivered \| failed` with `attempts`,
`last_attempt_at`, `delivered_at`, `retryable`. "delivered" means the adapter
accepted it.

**Reminders** (`reminders.py`, mounted only if `REMINDERS_ENABLED`):

| Route | Behaviour verified |
| --- | --- |
| `GET /api/reminders?limit=` | **Scheduled reminders only**, soonest first (`ReminderService.active` filters `state == scheduled`). Fields: `id, text, state, recurrence, next_run_at, timezone_name, fire_count, created_at`. |
| `GET /api/reminders/notifications?limit=` | **Pending (unseen) fired occurrences only**, oldest first, with `total`. Fields: `id, reminder_id, text, due_at, delivered_at`. `delivered_at` is when the in-app record was written, not proof a person saw it. |
| `POST /api/reminders/notifications/{id}/read` | 204, 404 `notification_not_found`. No `{}` body guard (pre-existing, unchanged). |
| `POST /api/reminders/{id}/cancel` | Exists; not surfaced in this phase. |

**Gaps (reported, not built):** a reminder in state `completed`, `failed` or
`cancelled`, and any fired occurrence already marked seen, cannot be listed.
So "see reminder outcomes after they fire" is only possible until the person
marks one seen, and a *failed* reminder is invisible. The UI states this
limit rather than implying it shows history.

### Integrations (`integrations.py`)

`GET /api/integrations/{google,gmail}/status` returns
`{integration, state, connected, granted_scopes, required_scopes}` and reads a
local token file only: no call to Google, so it is cheap and safe to repeat.
`state` is one of `not_configured, authentication_required, available,
disabled` for Google, plus `configured, expired, unavailable` in the shared
enum (the last three are not emitted by the Google integrations today).
There is **no field** for last verification, last successful use, or an
observed authentication failure, so "Connected and recently verified",
"Authentication expired or invalid" (observed) and "Temporarily unavailable"
(observed) cannot be derived from integration data. The UI derives what the
evidence supports and shows a neutral "credentials present, not verified"
instead of green. `POST …/disconnect` returns `{disconnected,
revoked_remotely}`; the UI as found ignored `revoked_remotely` (*Updated
2026-10-16:* it now says whether the provider confirmed the withdrawal,
`disconnectMessage` in `frontend/lib/integrationStatus.ts`).

### Provider (`GET /health`, public)

`llm: {healthy, detail, reachability}`. `reachability` (`unverified |
reachable | failing`) is observed from the provider's last real request, never
probed. It was then **uncommitted** provider-reliability work in
`api/routes/health.py` and `schemas/common.py` (*Updated 2026-10-16:*
committed in Stage 0 C4, `4b8d407`; the running backend, restarted
2026-10-14, serves it). The **provider name and model
are not fields**: the name appears only inside the `detail` sentence, the
model nowhere over HTTP (`RuntimeFacts` goes into the prompt only). Provider
selection is operator configuration (`LLM_PROVIDER`); there is no runtime
switching API, so the UI offers none. OpenAI is not implemented and is not
mentioned.

### Conversations (`conversations.py`)

`PATCH /api/conversations/{id}` with `{title}` already exists (rename,
trimmed, 1 to 200 chars, blank rejected 422). As found, the frontend had no
wrapper or control for it (*Updated 2026-10-16:* C3 adds both).
`ConversationRead` is `{id, title, created_at, updated_at}`.

## 3. Feature to authority map

| UI feature | Authoritative API | Ownership / concurrency / paging |
| --- | --- | --- |
| Task list and detail | `GET /api/tasks`, `GET /api/tasks/{id}` | Owner from the token; list max 50, no cursor; detail returns the full journal. |
| Monitor view | `GET /api/tasks` (each task's `monitor`, `next_run_at`, `check_count`; C2); `GET /api/tasks/{id}` for an opened monitor's journal | Two list calls, no per-task detail request; the journal is keyed `(id, updated_at)`. *Updated 2026-10-16:* as first written, derived from the journal with a detail request per task. |
| Stop a monitor | `POST /api/tasks/{id}/cancel {}` | Backend compare-and-set (6P); UI shows "stopped" only from the response state, handles 404/409/network. |
| Monitoring alerts inbox | `GET /api/task-notifications`, `…/{id}`, `POST …/{id}/read` | Keyset paging by `before`; mark-read idempotent; read state taken from a re-read, not assumed. |
| Reminders | `GET /api/reminders`, `GET /api/reminders/notifications`, `POST …/notifications/{id}/read` | Scheduled and unseen only (gap above). |
| Integration status | `GET /api/integrations/*/status` | Local read, no external call. |
| Provider status | `GET /health` | Public; observed outcome, fetched on load, on demand and after each chat turn. |
| Rename | `PATCH /api/conversations/{id}` | UI updates from the response. |
| Markdown | none (client only) | Own parser, no HTML injection, no images, link scheme allow-list. |

## 4. Gaps and the smallest proposed backend changes (not implemented, approval needed)

1. **Monitors in the task API.** **Done (approved and implemented
   2026-10-13; committed as Stage 0 C2, `30a1342`).** Read-only, optional fields on `TaskRead`
   (response schema only: no route, no write, no migration):
   `monitor: {kind, operator, interval_seconds} | null`, `next_run_at`,
   `check_count`, each read from the task row. The monitor UI no longer makes
   a detail request per task; the journal (last check outcome, failures) is
   read only when a person opens one monitor. `path` and `expected` are not
   exposed (the journal never carried them). `next_run_at` is the stored
   schedule: it holds a 5-minute claim lease while a check is in flight and
   cancellation does not clear it, so the UI trusts it only for a `queued` or
   `running` task.
2. **Provider and model in `/health`.** Add optional `provider` and `model`
   strings to `llm` (`ComponentHealth`), and a way to tell "not configured"
   from "configured, not yet used": today both report
   `reachability: "unverified"` with `healthy: false` (found end to end: a
   backend with no key returns `unverified` and the detail "No API key
   configured"). The UI therefore shows the backend's sentence beside a
   neutral "Not yet verified" and does not claim the provider is configured.
   Still proposed, not built. (It would touch the provider-reliability files,
   which were then uncommitted; they are now committed in Stage 0 C4,
   `4b8d407`.) The UI already reads `provider` and `model` if present.
3. **Reminder history.** Either a `state` filter on `GET /api/reminders`
   (read-only, additive) or `include_read=true` on the notifications list.
4. **Integration verification evidence.** A recorded last successful use or
   observed auth failure per integration (needs storage: out of scope).

## 5. Boundaries respected

No new route, no write path, no migration, no dependency, no change to a
structural test. The backend structure tests that scan the frontend
(`test_authentication_security`, `test_gmail_structure`,
`test_infrastructure`) constrain this phase: one `fetch(` and one call site
per token operation in `api.ts`, no browser storage use, no HTML injection
sink anywhere in frontend sources, and no `NEXT_PUBLIC_*` names containing
KEY, TOKEN, SECRET or AUTH.
