# Mai engineering changelog

The shared, chronological record of what has been built, what is uncommitted,
what is known to be broken, and what is deferred. Every agent reads this
before substantial work and updates it after completing work (see
[`AGENTS.md`](../AGENTS.md)).

**Rules for this file**

- Only **committed and verified** work goes under *Completed*. Files existing
  in the working tree do not make work complete.
- Record commit hashes and the verification **actually performed**. If a
  number is not known, say so. Do not invent one.
- Completed entries are listed **newest first**. Each one points to its
  detailed stage documents in `docs/` where they exist.
- Commit messages are the primary source for Stage 5E onward. Earlier stages
  have acceptance reports in `docs/`. A suite count is quoted only where a
  report states it as the result *after* that stage. Several reports state
  only the *baseline* before the stage; those counts are omitted, not
  inferred.

---

## Current state (as of 2026-10-02)

| | |
| --- | --- |
| Latest completed stage | **Stage 6M.1: durable notification delivery records**: the commit titled `Stage 6M.1: durable notification delivery records` (the commit that adds `backend/app/delivery/records.py`; find it with `git log -1 -- backend/app/delivery/records.py`) |
| Since 6M.1 | Fix (not a stage): the metadata registry registers the workflows models, `107d879` (Known defect 6 resolved) |
| Previous stages | 6L notification invocation boundary, `a0e16ae`; 6K notification delivery composition, `bb0ea41`; 6E test isolation fix (test only), `20468d3`; 6J Telegram notification adapter, `e6effdb`; Telegram foundation, `6626912`; 6I notification delivery boundary, `3e09853`; 6H durable task notifications, `b6d35ff` |
| Branch | `main` (no remote push recorded here) |
| Latest migration | `0019_notification_deliveries.py` (head, 6M.1). The real `mai` database observed at `0016`; it was not migrated by any verification. |
| Next stage | **6M.2: notification read API, not started.** 6M is split into four commits with an owner checkpoint between each: 6M.1 durable delivery records (done), 6M.2 read API, 6M.3 automatic delivery, 6M.4 authentication. Do not begin the next one without an explicit go-ahead. |

### Uncommitted / in progress (NOT completed)

These are in the working tree and are **not** part of any completed stage. Do
not commit them as part of other work, and do not describe them as done.

- **Telegram chat/webhook interface: uncommitted and incomplete.** (The
  Telegram *settings and Bot API sender* are committed in `6626912`, and the
  *notification adapter* in 6J. This is only the conversational webhook.)
  - Files: `backend/app/api/routes/telegram.py`,
    `backend/tests/test_telegram_adapter.py`, plus webhook edits to
    `backend/app/api/routes/__init__.py`, `backend/app/api/routes/conversations.py`
    and `backend/app/main.py`.
  - Its `main.py` and `routes/__init__.py` lines sit beside 6L's router
    registration in the same files. 6L committed only its own lines (staged
    from HEAD plus the 6L edits); the webhook lines remain unstaged.
  - It previously hit a Python 3.9 compatibility issue, which was corrected.
  - Focused testing then exposed a defect in the conversation route (see
    *Known defects*). That defect is unresolved, and the webhook is not
    verified. 6J does not depend on it and does not touch it.
- **Frontend/branding changes: uncommitted.** Edits to
  `frontend/app/{globals.css,layout.tsx,page.tsx}` and
  `frontend/components/{MessageInput,MessageList,Sidebar}.tsx`; new
  `frontend/public/branding/`, `frontend/public/favicon.svg`,
  `frontend/package-lock.json`, and `MAi branding .png` at the repository
  root. Not reviewed or verified as part of any stage.

### Known defects

1. **`backend/app/api/routes/conversations.py` (~line 238) references the
   undefined name `payload`.** The extracted helper calls
   `chat.send_message(..., content=payload.content)`, but only `content` is
   in scope, so every chat turn raises `NameError` and returns 500. The
   defect exists only in the uncommitted Telegram edits; committed `d249170`
   is unaffected. In the working tree it fails 752 tests (measured
   2026-10-01). **Not fixed**: it belongs to the Telegram work.
2. ~~**Order-dependent OAuth test.**~~ **Resolved in `5e1fc51`.**
   `test_a_tampered_redirect_is_refused_at_exchange_time` used
   `asyncio.get_event_loop()` and failed after any test that called
   `asyncio.run()` (Python 3.9 clears the loop). It predated 6G and was
   reproduced on `b14b71a`. After the fix, the reproducing pair passes and
   the whole OAuth security file passes (verified 2026-10-01). A full
   shuffled run has not been repeated since the fix.
3. **Earlier "randomly ordered" suite runs were not randomized.** Commit
   messages from Stage 5F.2 through the Gemini provider report "twice, the
   second randomly ordered". No random-order plugin has ever been in
   `requirements-dev.txt` or the git history, and none is installed, so those
   second runs were in default order. The counts are real; the
   randomization claim is not verifiable. Stage 6G was the first stage
   verified with an explicit shuffle (scratch plugin, seed reported).
4. **Host environment:** the local Python 3.9.6 interpreter occasionally
   segfaults during full suite runs. All 12 macOS crash reports on this
   machine share one native signature: an `aiosqlite` worker thread plus
   SQLite closing a "zombie" connection (`_PyTrash_begin` /
   `sqlite3LeaveMutexAndCloseZombie`). The reports start on 2026-09-28,
   before 6F, 6G and 6H.
   - 6G verification: 2 of 3 shuffled runs completed.
   - 6H verification: 4 of 6 shuffled runs completed, all green. In the same
     session, pristine `476a2d3` completed 3 of 3. That sample cannot rule
     out that 6H's 61 extra tests change how often it happens, but 6H adds
     no engine, thread or connection.
   - Likely cause: SQLite connections finalized by the garbage collector
     across threads (in test fixtures, not the application). Treat a
     segfaulted run as incomplete, never as a pass.
   - **6K confirmed the mechanism by bisection.** A 6K test that raced a
     lazy build across 16 Python threads *inside* the test process made
     `test_standing_grants.py::test_concurrent_authorizations_agree` (a 6E
     test, which runs concurrent sessions on the shared SQLite connection)
     segfault 3/3. Each file was clean alone, and without that test.
     Running the thread race in a subprocess removed it (0/3). **Rule: never
     spawn Python threads inside the shared test process; use a
     subprocess.** A 6K draft that raced five SQLite *sessions* with
     `gather` also corrupted the shared connection (the 6F limitation);
     such races belong on live PostgreSQL.
   - **Root cause found and fixed (`20468d3`).** With the thread race moved,
     the ordered full suite *still* segfaulted deterministically with the
     6K files present (3/3, at test #5289 or #5300), while HEAD passed 3/3.
     Bisection: HEAD plus 45 no-op tests did not crash; HEAD plus one file
     that only parses source with `ast` crashed 2/2; the 6K copy without
     `test_concurrent_authorizations_agree` did not crash. So no 6K behaviour
     is involved: shifting collection and GC timing is enough to expose that
     6E test, which raced four sessions on the `StaticPool` connection (one
     connection for every session). `20468d3` gives it a per-test
     file-backed SQLite database with a real pool, so each reader has its
     own connection. Assertions unchanged. Fault injection (the lookup never
     finds the grant; each reader sees a different grant) still fails it.
     After the fix, the ordered suite completed 5/5 with no segfault (HEAD
     plus the fix 2/2, 5775 passed; HEAD plus the fix plus 6K 3/3, 5820
     passed). Other StaticPool fixtures remain; the rule stands: never race
     sessions on the shared SQLite connection.
5. **Local Docker builds fail:** `docker build` cannot fetch
   `python:3.12-slim` metadata (`DeadlineExceeded`). Only the cached
   `mai-backend:latest` image is available on this machine.
6. ~~**`app/database/metadata.py` does not register the workflows
   models.**~~ **Resolved in `107d879`.** Found in 6M.1:
   `executions.workflow_id` references `workflows`, so a standalone process
   that imported the metadata registry but not `app.main` failed to flush
   executions (`NoReferencedTableError`). The server was unaffected because
   `app.main` imports the workflows models through its routes. The registry
   now imports them, and `tests/test_metadata_registry.py` checks the
   registry alone in a subprocess. Live scripts no longer need to import
   `app.main` first.

### Deferred / not built

- **Decision: Stage 6K (delivery orchestration) was not built (2026-10-02).**
  - It was specified as a thin layer that takes a notification id, an owner and
    an adapter name, reads the notification through 6H's owner-scoped
    `NotificationService`, calls 6I's `NotificationDeliveryService`, and
    returns its result, with no read-marking, retry, scheduling or channel
    selection.
  - **6I already is that operation.** `NotificationDeliveryService(session,
    owner_id, registry).deliver(notification_id, adapter_name)` takes an
    explicit adapter name; reads through `NotificationService.get` (so another
    owner's notification is indistinguishable from a missing one); refuses
    unknown adapters, missing notifications and malformed data; returns the 6I
    `DeliveryResult`; and never marks a notification read, changes task state,
    creates a notification, retries, schedules or runs a worker. A class in
    front of it would only forward to it, and doing the owner-scoped lookup
    first would duplicate the owner check and read the notification twice.
    The stage's own rule applied: use 6I rather than widen or replace it.
  - The owner chose to record this and add no code.
  - **What is actually missing is not orchestration:**
    1. **Composition.** Nothing in production builds a registry or constructs
       the Telegram adapter. It must be one process-lifetime instance, because
       the adapter's duplicate memory is per instance: building a new adapter
       per call would silently defeat duplicate protection. Writing that wiring
       imports `app.telegram.notifier`, so it crosses the "orchestration must
       not import Telegram" rule and requires deliberately changing three
       existing pins (6I's "no production code registers an adapter" and
       "exactly one channel adapter imports the delivery contract", and 6J's
       "nothing in production constructs the adapter").
    2. **An invocation surface.** Nothing can ask for a delivery: there is no
       route, command or caller. Adding one is an API and security decision.
  - The smallest sensible future stage is a composition root (one sealed,
    process-lifetime registry with the Telegram adapter built from settings
    when configured), with no endpoint and no worker. It needs explicit
    approval. **That composition root was then approved and built as Stage 6K
    (see Completed).** Gap 2, the invocation surface, remains open.
- **Automatic delivery.** 6L added the explicit trigger: a person's client asks
  for one notification to be delivered through one channel. Nothing delivers
  automatically: 6H's `record_outcome` still only records, and no runtime,
  runner or startup hook calls delivery. Which events (if any) should deliver
  without being asked, and through what (the runtime would be the only loop),
  is a separate architectural decision.
- **No authentication.** Mai has no authentication layer (AGENTS.md §2). The
  6L route follows every other route: the owner is the server's
  `LOCAL_OWNER_ID`, never a request value, and the backend is bound to
  127.0.0.1. A JSON-only body blocks cross-site form posts (CORS preflight).
  A real authentication layer would be its own stage.
- **The uncommitted webhook builds its own Telegram sender.** The webhook
  work (`backend/app/api/routes/telegram.py`, uncommitted) constructs a
  `BotApiSender` per request in `get_telegram_sender`, a second Telegram
  send path beside the 6K composition. When that work is finished it should
  be reconciled with the composition rather than committed as is.
- **Stage 6M plan (owner decisions, 2026-10-02).** Four commits, in
  dependency order, each verified and committed alone, with an owner
  checkpoint before the next:
  1. 6M.1 durable delivery records: done (see Completed);
  2. 6M.2 task-notification read API: thin routes over `NotificationService`,
     plus one keyset-cursor list method;
  3. 6M.3 automatic delivery: a third bounded work source in the existing
     runtime tick. It is opt-in (`NOTIFICATION_AUTO_DELIVER_ADAPTERS`,
     default empty), delivers only notifications created after enablement,
     and never retries a failed attempt (a person retries through 6L);
  4. 6M.4 authentication: hashed owner API tokens (an `api_tokens` table)
     and one `current_owner` dependency. `/health` and the OAuth callbacks
     are exempt. A browser token-entry screen waits for the other agent's
     frontend work.

  Each needs its own approval of details before implementation.
- **No read surface for task notifications.** `NotificationService` (unread,
  for_task, get, mark_read) exists in-process only. Unlike the reminder inbox
  (`GET /api/reminders/notifications`), no HTTP route exposes it, by
  decision. Adding a read-only route is a separate, approved change.
- **Two inboxes.** Reminder notifications (5F.1) and task notifications (6H)
  are separate tables. Unifying them behind one read contract is deferred.
- No HTTP or UI surface exists for `TaskService.schedule_background`,
  `TaskService.configure_monitoring` or standing-grant creation. These
  features are currently reachable only programmatically, by design, until a
  stage adds a person-facing surface.
- `max_model_calls` and `max_seconds` task budgets are recorded but
  deliberately unenforced (nothing measures them).
- The `replanned` task event remains unwritable (no replanning exists).
- Research: no source ranking, contradiction detection or verification
  search (Stage 5E audit recommendations beyond 5E.1/5E.2).

### Open owner actions (operational; never mark resolved without confirmation)

- Rotate the Google OAuth client secret.
- Revoke the Groq key, both OpenRouter keys, the GitHub PAT, and the first
  Tavily key.
- Legacy tool-call blobs remain in stored data.
- Dependency advisories: `starlette 0.52.1`; `next@15.5.4` (CVE-2025-66478);
  the Debian image layer has not been scanned.
- Google Calendar reports `reauthorisation_required`.
- Groq `SSLV3_ALERT_BAD_RECORD_MAC` TLS errors are not investigated.

---

## Completed (newest first)

### Fix: metadata registry registers the workflows models (`107d879`, 2026-10-02)

- **Status:** completed. Resolves Known defect 6. No migration and no schema
  change: the `workflows` table already exists (migration chain unchanged,
  head `0019`).
- `backend/app/database/metadata.py` imports `app.workflows.models` and lists
  it in `__all__`.
- `backend/app/workflows/models.py` now takes `Base` from
  `app.database.models.base`, like every other model module. It previously
  imported `Base` from the registry itself, so registering it would have made
  the two modules import each other. Behaviour is unchanged: it is the same
  `Base` object.
- New `backend/tests/test_metadata_registry.py`. In a fresh subprocess it
  imports only `app.database.metadata`, asserts `app.main` was not loaded
  (so the check cannot pass vacuously), calls `fk.column` for every foreign
  key in `Base.metadata.sorted_tables`, then imports `app.main` and asserts
  the server registers no table that the registry missed. A subprocess is
  needed because `conftest.py` imports `app.main`, which is why the suite
  never caught the defect.
- **Verification** (isolated worktree of `87365aa` plus these three files;
  copied files confirmed byte-identical before commit):
  - Mutants: removing the workflows registration, making the registry import
    `app.main`, dropping the history models and dropping the reminder models
    each fail the new test. The fix passes it.
  - Import order: the registry resolves when imported first, or after
    `app.workflows.models`, `app.execution.models` or `app.main`.
  - Live PostgreSQL: throwaway database `mai_verify_metadata` in `mai-db`,
    `alembic upgrade head` to `0019`. A standalone process that imports only
    the registry and flushes an `executions` row failed on HEAD with the
    original `NoReferencedTableError` and succeeded with the fix. It was
    rolled back (0 rows), the database was dropped (no `mai_verify%`
    databases remain), and the scratch credential file was deleted. The real
    `mai` database and the running containers were not touched.
  - Full suite: ordered 5921 passed, 2 skipped; shuffled (scratch shuffle
    plugin, seed 157093811) 5921 passed, 2 skipped. Skips are the expected
    two (`test_secrets.py`, no `backend/.env`; PostgreSQL migration-chain
    test, `TEST_POSTGRES_URL` unset). No segfault in either run.
- The uncommitted Telegram webhook and frontend/branding work in the tree was
  not touched, staged or verified.

### Stage 6M.1: Durable notification delivery records (commit titled `Stage 6M.1: durable notification delivery records`, 2026-10-02)

- **Status:** completed, verified, committed. Parent `a0e16ae` (6L). It is the
  commit that adds `backend/app/delivery/records.py` (a commit cannot contain
  its own hash).
- **Purpose:** make "delivered once" durable. Until now, duplicate protection
  was 6J's in-process memory: a restart, or a second process, could deliver
  the same notification again. The database is now the source of truth.
- **Owner decisions:**
  - 6M is split into four commits, with a checkpoint between each;
  - at-least-once in the crash window (Telegram has no idempotency key);
  - and, for later sub-stages, the auto-delivery policy and the
    authentication model (see Deferred).
- **Schema** (migration `0019`; `task_notifications` is not altered):
  - `notification_deliveries` holds one row per (notification, adapter),
    made unique by `uq_notification_deliveries_notification_adapter`;
  - columns: id, notification_id (foreign key, cascade), owner_id (copied
    from the notification), adapter, status (`sending` / `delivered` /
    `failed`, enum `notification_delivery_status`), attempts,
    lease_expires_at, created_at, updated_at and delivered_at;
  - check constraints: `delivered` if and only if `delivered_at` is set;
    `sending` requires a lease; `attempts >= 1`;
  - no content, recipient, credential or error text.
- **Protocol** (`app/delivery/records.py`, the one writer; called only by
  6I):
  - **Claim:** after the owner-scoped read and the payload check, and before
    the adapter, 6I claims the row.
    - No row: insert `sending` under a 60 s lease (the adapter timeout is
      10 s).
    - `delivered`: answer `DUPLICATE` without calling the adapter.
    - `failed`, or `sending` past its lease: re-claim by conditional UPDATE,
      fenced on the attempt count.
    - Otherwise: answer `REFUSED delivery_in_progress` (409 on the 6L
      route).
    - The claim is committed before the adapter is called.
  - **Finish:** the outcome is committed after the call.
    - Delivered, or the adapter's own `DUPLICATE`, becomes `delivered`.
    - A failure, timeout, exception or invalid status becomes `failed`,
      which stays retryable.
    - Fenced on the row still being `sending` and still at this claim's
      attempt.
  - The unique index decides two first claims; `rowcount == 1` decides
    re-claims.
- **6I contract changes (deliberate):**
  - 6I now writes delivery records (only those, only through `records`) and
    commits the session it is given. The precedent is `ExecutionService`.
    Callers pass a session with no other pending work; the 6L request
    session qualifies.
  - A new refusal reason, `delivery_in_progress`.
  - Concurrent requests during a live claim now get 409 instead of waiting
    on 6J's lock for `duplicate`.
  - 6I still never writes `task_notifications`, marks nothing read, retries
    nothing and runs no loop. The 6K composition, 6J, `deps.py` and
    `main.py` are unchanged.
- **Existing tests and pins changed (deliberately; none loosened):**
  - 6I package pins: the module list adds `models.py` and `records.py`; the
    imports allow the model and SQLAlchemy core; "never writes" became
    "only `records.py` writes, only `NotificationDelivery`, and the service
    makes no write of its own"; the delivery importers list adds
    `app/database/metadata.py`.
  - The "no migration after 0018" pins in 6J, 6K and 6L now allow exactly
    `0019_notification_deliveries.py`. 6J's "no delivery table" now allows
    exactly `notification_deliveries`.
  - The 6G pin "only the runtime knows a lease" now also allows
    `records.py`, with added assertions that the lease schedules nothing (no
    task, `next_run_at`, loop or sleep).
  - 6H's migration round-trip test is pinned to its own revision (0018)
    instead of `head`.
  - 6K's counterfactual ("a registry rebuilt per call re-sends") now proves
    the opposite: a rebuilt registry, which is what a restart is, sends
    once.
  - 6L's concurrency test accepts `duplicate` or 409 for the losers; still
    exactly one delivered and one Telegram request.
  - AGENTS.md §4.7 records the durable-records invariant; the 6L docstring
    no longer says delivery "records nothing".
- **Verification** (isolated worktree: `a0e16ae` plus the 6M.1 files):
  - Tests: 28 behavioural (`tests/test_delivery_records.py`) and 12
    security (`tests/security/test_delivery_records_security.py`).
    Covered:
    - success recorded once; success then retry is DUPLICATE and unsent;
    - restart replay (a fresh adapter answers DUPLICATE and is never
      called); the adapter's own DUPLICATE is recorded as delivered;
    - per-adapter records; each failure kind recorded `failed`; failure
      then retry delivers once;
    - a live claim refuses; an abandoned claim is taken over after its
      lease;
    - fencing (a superseded attempt cannot overwrite; a reclaim from a
      stale view cannot reuse an attempt number; a finished claim cannot
      finish again);
    - losing the insert race defers to the winner's row; the lease
      outlasts the timeout;
    - six concurrent attempts on separate connections send once (stable
      10/10);
    - refusals record nothing; other-owner isolation; no notification, task
      or read-state change; cascade;
    - database refusals (duplicate pair, four inconsistent rows); exact
      columns.
  - Focused: 6M.1 28/28 and 12/12. All of `tests/security`: 2260 passed, 1
    skipped. The 6I/6J/6K/6L/6H delivery groups pass.
  - Full suite: ordered 5920 passed, 2 skipped (304 s). Shuffled with the
    scratch shuffle plugin (real reordering, seed reported; 5921 or more of
    5922 items moved): seeds 20261004, 1069922067 and 1171424740, each 5920
    passed, 2 skipped (292 to 298 s). No segfault in any run. (An earlier
    ordered run, before the three gap-closing tests, gave 5917 passed; it was
    superseded.)
  - Mutation: 19/20 killed. The validated harness passed (anchors unique,
    green baseline, two controls survived: an unasserted comment and an
    unasserted docstring; neither module logs, so there was no log string to
    use as a control).
    - The first run killed 16/20. Three survivors were real gaps, each
      closed by one deterministic interleaving test:
      - C6, losing the insert race treated as winning: the SQLite
        concurrency tests never reached that branch;
      - C5, the re-claim not fenced on attempts (an ABA reuse of an attempt
        number);
      - F2, a finished claim finished again.
    - The remaining survivor, **C2** (the early "already delivered" check
      removed), is **equivalent**: the conditional UPDATE excludes
      delivered rows, and the fallback re-read answers ALREADY_DELIVERED.
      The check is only a fast path.
    - Other targets, all killed: no commit before sending, lease ignored,
      failed not re-claimable, live claim read as delivered, zero lease,
      finish unfenced, no `delivered_at`, finish not committed, adapter
      called on DUPLICATE or in-progress, failure recorded as delivered,
      timeout left open, wrong owner, adapter DUPLICATE recorded as failed,
      non-unique index, and in-progress not 409.
  - **PostgreSQL** (throwaway database, existing image, isolated source
    mounted read-only):
    - `upgrade 0018 -> head -> downgrade 0018 -> upgrade head`: the
      downgrade restores the 0018 schema byte-for-byte; the re-upgrade
      reproduces head exactly; the enum type is dropped on downgrade; the
      diff is additions only; constraint names are single-prefixed.
    - 14/14 live checks: the constraints (valid row accepted; duplicate
      pair, delivered without time, failed with time, sending without
      lease, zero attempts and orphan notification all refused); cascade;
    - 20 concurrent deliveries on 20 PostgreSQL connections: 1 delivered,
      adapter called once, one row at attempt 1;
    - 20 simultaneous first claims released by a barrier (the insert race):
      exactly one won;
    - failure then retry then duplicate (attempts 2);
    - a live claim refused, then taken over after its lease (attempt 2);
    - the 6L route delivered through the composed Telegram adapter (stubbed
      transport).
    - **Replay after a real `docker restart`** (2/2): the same POST from a
      new process answered 200 `duplicate` with no Telegram request, and the
      record survived (delivered, attempt 1).
    - Two harmless script artifacts on the way: "Event loop is closed" noise
      from disposing the engine in a second event loop (fixed), and a first
      constraint attempt via hand-written SQL that never inserted its setup
      row (discarded and redone in Python on a real notification).
  - Docker (the real server on this code): migrated to `0019`, health 200,
    an unconfigured POST gave 404 `unknown_adapter`, and the log held no
    sentinel, URL, password or traceback. No rows were left, the stop was
    graceful, the throwaway database was dropped, and the real database was
    untouched (`0016`).
  - Structural audit 13/13 on the exact tree committed:
    - one each of: `AuthorizationService`, `ExecutionService`,
      `TaskRunner`, `BackgroundRuntime` (constructed only in `main.py`),
      notification writer (`record_outcome`), delivery-record writer
      (`records.py`), composition root, and Telegram sender (`BotApiSender`
      built only in 6J);
    - no new setting; protected packages, `deps.py` and `main.py`
      unchanged;
    - only 6I imports the writer, and the 6I service makes no direct
      database call;
    - 0019 is head and follows 0018.
  - **Live Telegram not tested:** no credentials are configured, and none
    were requested.
- **Known limitations:**
  - **At-least-once crash window:** a crash after Telegram accepts but
    before the outcome commits can send twice, once the 60 s lease passes.
  - **A stalled attempt can be superseded:** an attempt that outlives its
    lease (60 s, against a 10 s timeout) can be taken over. Its late finish
    is then discarded by the fence.
  - **No automatic retry:** a failed row is retried only when someone asks
    again (6L). Automatic delivery is 6M.3.
  - **No surface for records:** nothing reads them over HTTP yet.

### Stage 6L: Notification invocation boundary (`a0e16ae`, 2026-10-02)

- **Status:** completed, verified, committed as `a0e16ae`. Parent `bb0ea41`
  (6K). It adds `backend/app/api/routes/notification_delivery.py`.
- **Purpose:** the first production caller of delivery. Until 6L, nothing
  asked 6I to deliver anything. A person can now ask for one existing task
  notification to be delivered through one registered channel:
  `POST /api/task-notifications/{notification_id}/deliveries` with
  `{"adapter": "telegram"}`.
- **Decisions, made by the owner before any code (AGENTS.md §6):**
  - an HTTP route is the invocation surface (a new route with an outbound
    effect crosses §6, so it needed explicit approval);
  - the existing single-owner model: no authentication layer exists, and none
    was added; the owner comes only from the server;
  - the route is ungated (not behind `EXECUTION_ENABLED`): delivery is not
    tool execution, and with no channel configured it refuses everything;
  - registration lines in `main.py` and `routes/__init__.py`, which also hold
    the other agent's uncommitted webhook lines, were committed by staging
    HEAD plus the 6L edits only.
- **Architecture:** three small pieces, no new layer.
  - `api/routes/notification_delivery.py`: one POST endpoint. Body
    `DeliveryRequest` is exactly one strict string field, `adapter` (1 to 32
    characters, `extra="forbid"`). The endpoint makes one call,
    `service.deliver(notification_id, request.adapter)`, and maps the 6I
    result: `delivered`/`duplicate` give 200 with the 6I `DeliveryResult`;
    `notification_not_found` and `unknown_adapter` give 404;
    `malformed_notification` gives 422; an unmapped refusal gives 409; a
    channel failure gives 502. Errors carry only 6I's reason code, or the fixed
    `delivery_failed`/`delivery_refused` when 6I gives none. It does no
    logging (6I logs), no writing, no retry and no scheduling.
  - `api/deps.py`: `get_notification_delivery_service(session)` returns
    `notification_delivery_service(session)` from the 6K root, so the owner is
    the composition's `LOCAL_OWNER_ID` default and the registry is the one
    process registry.
  - `main.py` and `routes/__init__.py`: the router is included once,
    unconditionally.
  - Reused unchanged: 6H (ownership, notification rows, read state), 6I
    (validation, refusals, timeout, failure containment), 6J (message, chat,
    duplicate memory), 6K (one registry and adapter), and the foundation
    (host, method, redirects). No new service, registry, sender, setting,
    table, migration, worker, scheduler or retry.
  - **The caller controls only the notification id and the channel name.**
    Owner, token, chat id, URL, host, recipient and text cannot be supplied:
    a body field is a 422, and query parameters and headers are ignored.
    Unknown and unconfigured channels are the same 404. Another owner's
    notification is the same 404 as a missing one.
  - AGENTS.md §4.7 now records the route.
- **Four existing pins changed, each to an exact allow-list (none loosened):**
  - 6I importers of `app.delivery`: adds `deps.py` (`service`, for the
    dependency's type) and the route (`contract`, for the result).
  - 6I "no HTTP route triggers delivery" became "only the 6L route triggers
    delivery". No HTTP module names the registry or constructs the service,
    and every other route still cannot reach delivery.
  - 6K "nothing calls the composition yet" became "only `deps.py` calls it".
  - 6K "no route or lifespan reaches delivery" now allows exactly `deps.py`
    (the service factory) and the route (the contract). `main.py` reaches
    none of it.
- **Verification** (isolated worktree: `bb0ea41` plus the 6L files, with
  `main.py` and `routes/__init__.py` as HEAD plus the 6L lines only):
  - Tests: 46 behavioural (`tests/test_notification_invocation.py`) and 14
    security (`tests/security/test_notification_invocation_security.py`).
    They go through the real app, the 6K composition, 6I and 6J, with
    notifications made by the real 6G/6H runtime path; only the Telegram
    transport is stubbed. Covered:
    - delivery, duplicates, no state change and no read-marking;
    - one 6I call per request, on the process registry with the server's
      owner, and no adapter or sender built per request;
    - missing, other-owner (identical), unknown and unconfigured (identical)
      refusals;
    - malformed requests (12 shapes); 13 forbidden body fields; query and
      header injection; 13 adapter-name injections, with no import of any
      of them;
    - 6I's strip-and-lowercase normalisation;
    - CSRF (form, text and multipart content types refused) and the CORS
      preflight;
    - 502 on channel failure, and a failure is not remembered as delivered;
    - redaction (token, URL, database URL and path in adapter and transport
      exceptions reach no response and no log record); the app's generic
      500;
    - five concurrent requests on separate connections (a file-backed
      SQLite with a real pool): 1 delivered, 4 duplicate, one request.
  - Focused: 6L 46/46, 6L security 14/14. With 6K, 6I, 6J and 6H: 272
    passed. All of `tests/security`: 2248 passed, 1 skipped.
  - Full suite: ordered 5880 passed, 2 skipped (318 s). Shuffled with the
    scratch shuffle plugin (real reordering, seed reported; 5880 or more of
    5882 items moved): seeds 20261003, 2088385192 and 227968032, each 5880
    passed, 2 skipped (280 to 288 s). No segfault in any run.
  - Mutation: 19/19 killed. The validated harness passed (anchors unique,
    green baseline, two controls survived: an unasserted comment and an
    unasserted docstring; the route has no log string to use as the second
    control). Targets: body (extras ignored, not strict, unbounded, empty
    allowed), the 6I call (hard-coded channel, faked success, read-marking),
    status mapping (403 for missing, 400 for unknown adapter, refusal or
    failure as 200, whole result as detail, no fixed code, moved under the
    task API), dependency (another owner, a registry per request, an empty
    registry), and registration (missing, gated on execution). With the
    behavioural tests alone, 18/19: B2 (`strict` removed) survives and is
    **equivalent** on the JSON surface (no JSON value validates differently;
    checked). It stays pinned structurally.
  - Docker (the existing `mai-backend:latest`, throwaway PostgreSQL, the
    isolated source mounted read-only; the mounted route's hash was checked):
    - over real HTTP to the unconfigured server: POST gave 404
      `unknown_adapter`; a form post, an extra `chat_id` and a non-UUID id
      gave 422; GET gave 405; a cross-origin preflight got no allow-origin;
      OpenAPI lists exactly one POST path; no delivery was attempted, and
      the composition was built on first use, not at startup;
    - in the container, the real app over ASGI with real PostgreSQL
      sessions and a sentinel-configured composition (transport stubbed),
      12/12:
      - delivered then duplicate;
      - another owner's notification is the same 404 as a missing one;
      - injection attempts refused with nothing sent;
      - five concurrent POSTs gave 1 delivered and 4 duplicate, with exactly
        one Telegram request per notification;
      - no row, task or read state changed; the same registry throughout;
      - no token, URL or endpoint in any log record.
    - The DEBUG server log held no sentinel, `sendMessage`, `/bot`, database
      password, traceback or ERROR line. Graceful stop; the throwaway
      database was dropped and the real database was untouched (0016).
    - One in-container check ("no connection other than PostgreSQL" while
      unconfigured) may pass vacuously, because the pool can reuse a
      connection, so it is not counted as evidence. The inert claim rests on
      the empty registry and the 404.
  - Structural audit 14/14 on the exact tree committed:
    - one class each, constructed only in the 6K root, and `BotApiSender`
      only in 6J;
    - one service-factory call site (`deps.py`) and one production
      `deliver` caller (the route);
    - no new sender, client, registry, service, worker, setting or migration;
    - no protected package changed (tasks, runtime, execution, authorization,
      tools, llm, delivery, telegram, composition, integrations, core);
    - only 6L paths changed, and the `main.py`/`__init__` diff is exactly
      the 9 lines of the 6L router;
    - the task API is still GET-only, and no new `mark_read` caller exists.
    - The first run reported 13/14 because the audit script's own expected
      line count was wrong (8 instead of 9); the diff was inspected and holds.
  - **Live Telegram not tested:** no credentials are configured, and none
    were requested.
- **Known limitations:**
  - **No authentication:** the route is as protected as every other Mai
    route (127.0.0.1 bind, CORS, JSON-only body), no more. There is no
    "authentication failure" to test because no authentication exists.
  - **Not restart-safe:** duplicate memory is 6J's in-process memory, so
    after a restart the same notification can be delivered again. Durable
    delivery records remain deferred.
  - **The person can deliver one notification more than once across
    restarts**, and can deliver any of their own notifications, read or
    unread. No policy limits which notifications may be delivered beyond 6I's
    checks.
  - **No list surface:** a client must already know a notification id. Task
    notifications still have no HTTP read route (deferred).

### Stage 6K: Notification delivery composition (`bb0ea41`, 2026-10-02)

- **Status:** completed, verified, committed as `bb0ea41` (it adds
  `backend/app/composition/`; recorded by 6L, since a commit cannot contain
  its own hash).
- **Purpose:** the production composition root for notification delivery. It
  builds, once per process, the graph 6I and 6J defined but nothing
  assembled:
  `NotificationDeliveryService -> sealed AdapterRegistry -> TelegramNotificationAdapter
  -> BotApiSender -> Telegram`. It is needed because 6J's duplicate
  protection lives in the adapter instance: an adapter rebuilt per call
  starts with an empty memory and re-sends (proven by a counterfactual
  test).
- **Architecture:** one new module, `app/composition/notification_delivery.py`
  (plus the package's `__init__.py`):
  - `build_delivery_registry(settings)`: a new `AdapterRegistry`; the 6J
    adapter built from settings and registered **only if it reports itself
    configured**; then sealed. It opens no connection (the sender's HTTP
    client is created on its first request).
  - `get_delivery_registry()`: the process's one registry, built on first
    access under a lock with a double check, then reused.
  - `notification_delivery_service(session, owner_id=LOCAL_OWNER_ID)`: a 6I
    service bound to that registry. It is per call because 6I gives the
    service a DB session and an owner; the registry, adapter, sender and
    duplicate memory are shared.
  - **Follows the existing pattern:** `app.integrations.registry` (a
    process-lifetime, sealed registry read through an accessor, whose HTTP
    clients are not closed in the lifespan). One difference: integrations
    read settings lazily but the 6J adapter reads its two at construction,
    so the graph is built on first access rather than at import.
  - **Not in `main.py`:** the lifespan was not used. Nothing needs it (no
    startup work, and no shutdown disposal by precedent), and `main.py`
    carries the other agent's uncommitted webhook edits.
  - The in-memory `LocalRecordingAdapter` is deliberately not registered in
    production. Without Telegram configuration the registry is sealed empty
    and 6I refuses `"telegram"` as `unknown_adapter`.
  - Unchanged: 6H, 6I, 6J, the Telegram foundation, settings, compose,
    `.env.example`, tasks, runtime, monitoring, execution, authorization,
    providers, migrations and `main.py`. No route, worker, scheduler, retry,
    persistence or second sender, registry, service or configuration path.
  - **Nothing calls it yet.** Deciding the trigger is the next stage.
- **Five existing pins changed, deliberately, each to an exact allow-list
  (none loosened):**
  - 6I "nothing outside delivery imports it": now exactly the 6J adapter
    (`contract`) and the composition root (`registry`, `service`).
  - 6I "no production code registers an adapter": now exactly the
    composition root names `AdapterRegistry`, and its only `register` call
    is the Telegram adapter.
  - 6J "nothing reaches the adapter": now exactly the composition root
    imports it.
  - 6J "nothing constructs the adapter": now exactly one construction, in
    the composition root.
  - **Stage 4C `test_only_two_files_register_anything`** (no `.register(`
    outside the tool catalog and registries): the composition root calls
    `registry.register(telegram)` on the *delivery* registry, which holds
    channels, never tools. It is now allowed by exact path, and the test
    additionally asserts the root reaches no tool registry, catalog or
    executor, so "nothing outside the catalog registers a tool" still holds.
    Probes confirmed that a new tool-registration site elsewhere, and the
    root importing a tool registry, both still fail. This pin's failure was
    hidden at first: early full runs crashed (Known defect 4) before
    reaching it, and the focused runs didn't include that file. Found by
    excluding the crash site.
- **Verification:**
  - Tests: 29 behavioural (`tests/test_notification_composition.py`) and 16
    security (`tests/security/test_notification_composition_security.py`).
    Covered: one registry and one adapter for the process (including a
    16-thread race, run in a subprocess); registration only when
    configured; fail-closed cases; no connection; sealed; the 6I service
    type; shared duplicate memory across separate requests; the per-call
    counterfactual; no read-marking or state change; no token in logs.
  - Full suite (isolated copy of `20468d3` plus the 6K files): ordered 5820
    passed, 2 skipped (291 s), plus 3/3 earlier ordered runs with the same
    content (5820 passed each). Shuffled with the scratch shuffle plugin
    (real reordering; seed reported; 5821 or more of 5822 items moved):
    seed 20261002, 298350825 and 673989253, each 5820 passed, 2 skipped
    (272 to 279 s). No segfault in any of the 7 runs. Focused: 6K 29/29, 6K
    security 16/16, 6H/6I/6J delivery and notification tests 197 passed,
    the changed pins' files 139 passed, all of `tests/security` 2234
    passed, 1 skipped.
  - Mutation: 15/15 killed on the first run, and again on the final code
    on top of `20468d3`.
    The validated harness passed (two controls survived). Targets: rebuild
    per access, dropped double check, no caching, fresh registry per
    service, lock removed, registration guard inverted or removed, unsealed,
    local adapter registered, wrong settings, wrong or ignored owner, empty
    registry, and logging of settings or wrong names.
  - Docker (the existing `mai-backend:latest` image, throwaway PostgreSQL,
    isolated source mounted read-only), 12/12:
    - startup built no composition and attempted no delivery;
    - the unconfigured container composed a sealed, empty registry with no
      socket attempt, and refused `"telegram"`;
    - configured with a sentinel token over a stub transport, the same
      registry was returned on every access;
    - **five concurrent explicit deliveries, each on its own real
      PostgreSQL connection and session, gave 1 delivered and 4 duplicate,
      with exactly one Telegram request**, and a later separate request was
      still `DUPLICATE`;
    - no row, task or read state changed, and no token, URL or endpoint
      appeared in any log record.

    Graceful stop; the real database was untouched. Re-run on the final
    code on top of `20468d3` (mounted root's hash checked): 12/12 again.
    The server's DEBUG log held no sentinel, no `sendMessage` or `/bot`,
    no database password and no error.
  - Structural audit: 13/13 on the exact tree committed. One of each class, one
    production construction of the registry, the Telegram adapter and the
    6I service (all in the root); `BotApiSender` built only inside the 6J
    adapter; no new Telegram path, configuration namespace or endpoint; no
    protected file changed. (In the *working tree* the uncommitted webhook
    builds a second `BotApiSender`; see Deferred.)
  - **Live Telegram not tested:** no credentials are configured, and none
    were requested.
- **Findings:**
  - **Two of 6K's own tests destabilised the shared test process** (see Known
    defect 4): five SQLite sessions raced with `gather`, and 16 Python
    threads raced in-process. Both were moved: the session race to live
    PostgreSQL (Docker), and the thread race to a subprocess. The thread
    race was bisected to a deterministic 3/3 crash before the fix and 0/3
    after, and the subprocess version still catches an unlocked lazy build.
  - **A pre-existing 6E test was the real crash site.** Even after both
    moves, the ordered suite still crashed whenever the 6K files were
    present. It was bisected to `test_concurrent_authorizations_agree` (see
    Known defect 4) and fixed in its own commit, `20468d3`, before 6K.
- **Known limitations:**
  - **Not restart-safe:** duplicate memory resets with the process (6J's
    limitation, unchanged).
  - **Telegram client not closed at shutdown:** its `httpx` client is not
    closed in the lifespan, the same as every integration's (precedent).
  - **Tied to one event loop:** the adapter's lazy lock binds to the event
    loop of its first use, which matches Mai's one loop.

### Stage 6J: Telegram notification adapter (`e6effdb`, 2026-10-01)

- **Status:** completed, verified, committed as `e6effdb` (it adds
  `backend/app/telegram/notifier.py`).
- **Purpose:** the first real notification channel. Telegram becomes one
  adapter behind the 6I delivery boundary:
  `6H notification -> 6I NotificationDeliveryService -> TelegramNotificationAdapter
  -> the existing BotApiSender -> Telegram`. Telegram is only a channel.
- **Architecture:**
  - One new module, `app/telegram/notifier.py`. It lives with the channel, so
    the dependency points one way: the channel depends on the 6I contract, and
    `app/delivery` still knows nothing of Telegram.
  - Reuses everything committed in the Telegram foundation (`6626912`) and
    changes none of it: the two existing settings (`TELEGRAM_BOT_TOKEN`,
    `TELEGRAM_ALLOWED_CHAT_ID`), the existing `BotApiSender`, and the
    existing network policy (`api.telegram.org` only, POST only, redirects
    refused, private/loopback/link-local addresses refused). No second
    sender, client, configuration namespace, registry, writer, worker or
    HTTP endpoint.
  - **Destination:** only `TELEGRAM_ALLOWED_CHAT_ID`, read once at
    construction. A 6I payload has no chat, recipient, URL or channel field
    (it is closed), so nothing about a notification can steer where it goes.
  - **Fail closed:** without a well-formed chat id and a plausible token the
    adapter is unconfigured, builds no sender and sends nothing. Verified
    with a socket guard: not one connect attempt.
  - **Message:** built only from the payload's typed fields. A closed
    headline per kind, the task id, the check number and a UTC time, with no
    objective, argument, result or owner, and no `parse_mode`. It is capped
    at Telegram's limit by deterministic truncation. 6I's payload contract
    was not widened.
  - **Idempotency:** the adapter refuses a delivery key not minted for it,
    and remembers delivered keys in memory (bounded to 4096). A repeat is
    `DUPLICATE`, racing calls send once, and a failed send is not remembered,
    so an explicit retry can succeed. There is no automatic retry. The memory
    is process-local: see limitations.
  - **Errors:** whatever the sender raises becomes `FAILED`. The exception is
    never bound, rendered or logged.
- **One existing pin changed, deliberately:**
  `tests/security/test_delivery_security.py` had a 6I test saying nothing
  outside `app/delivery` may import it, which was true only because no
  channel existed. It is now an exact allow-list: exactly
  `app/telegram/notifier.py`, importing only `app.delivery.contract`. A
  second importer, or a channel reaching the delivery service or registry,
  still fails it (verified by probe).
- **Verification:**
  - Tests: 118 behavioural (`tests/test_telegram_notifier.py`) and 25
    security (`tests/security/test_telegram_notifier_security.py`), 143 new.
    They drive the **real** foundation `NetworkPolicy` and `BotApiSender`
    against a stub transport and resolver, so "fixed host", "redirects
    refused", "loopback and private refused" and "GET refused" are proven
    through the real code path.
  - Full suite (isolated copy of HEAD plus the 6J files): ordered 5775
    passed, 2 skipped (278 s); shuffled with seed 20261005, 5775 passed,
    2 skipped (275 s); shuffled with seed 1609842916, 5775 passed, 2 skipped
    (313 s). The baseline was 5632 passed, 2 skipped (+143).
  - Mutation: 42/42 killed. The validated harness passed (two controls
    survived). Targets: the Telegram host and policy (in the foundation, in
    the verification copy only), chat-id enforcement, token handling,
    delivery key and idempotency, failure handling, message construction,
    registration and lock creation. The first run killed 38 of 40; both
    survivors were real gaps (the HTTP status guard was masked by the
    `ok` check, and the unknown-kind headline fallback was untested) and
    each got a test.
  - Docker (the existing `mai-backend:latest` image, throwaway PostgreSQL
    database, isolated source mounted read-only, **no** Telegram settings in
    its environment), 10/10: migrations at 0018; the container's own runtime
    produced a notification; unconfigured, delivery failed with no socket
    attempt; configured with a *sentinel* token over a stub transport, it
    delivered once as `POST https://api.telegram.org/bot…/sendMessage` with a
    body of exactly `chat_id` and `text`; a repeat was `DUPLICATE`; no row,
    task or read state changed. The server's log at `LOG_LEVEL=DEBUG` held
    no token, URL or endpoint. Graceful stop.
  - Structural audit: 18/18. One of each core component, one `BotApiSender`,
    one Telegram adapter, `api.telegram.org` in code in one module, nothing
    imports the adapter, and nothing in tasks, background, execution, tools,
    authorization, LLM, core, migrations, compose or `.env.example` changed.
  - **Live Telegram was NOT tested.** No Telegram credential is configured in
    this environment (checked as booleans in the running container; values
    never read), and none was requested or created. Everything above uses
    sentinel credentials and a stub network. What is therefore unproven
    against the real API: that Telegram accepts the foundation's
    percent-encoded token path (the sender encodes `:` as `%3A`; standard
    servers decode it, but it was not observed), and real chat delivery.
    A failure there would surface safely as a `FAILED` delivery.
- **Findings:**
  - **Defect found by shuffling, in 6J's own code (fixed):** the adapter made
    its `asyncio.Lock` in `__init__`. On Python 3.9 that binds to the current
    event loop, so building the adapter from synchronous code raised
    `RuntimeError: There is no current event loop` after any earlier
    `asyncio.run()`. It appeared only under some test orders. The lock is now
    created on first use, and a regression test reproduces the 3.9 state
    (and fails on the old code).
  - **Token in URL:** unlike every earlier integration, Telegram's secret is
    in the request URL, and `httpx` itself logs that URL at INFO. Production
    is protected only because `configure_logging` pins the `httpx` and
    `httpcore` loggers to WARNING. The redactor has **no** pattern for a
    Telegram token, and the sender's percent-encoded form (`%3A`) would
    evade a naive one. 6J did not change shared logging. Instead it adds a
    test under the real `configure_logging` at DEBUG (JSON and console) and a
    control proving the pin is what protects the token. See follow-ups.
  - An earlier draft's leak assertions looked for the raw token and missed
    the encoded form that is actually logged. They now check the part that
    survives encoding.
- **Known limitations:**
  - Not restart-safe. After a restart the adapter forgets what it delivered
    and could send a notification again if something delivers it again. A
    durable guarantee needs persisted delivery records, which needs approval.
  - Nothing in production constructs the adapter, registers it, or triggers
    delivery (see Deferred).
  - The adapter is bound to one event loop once used, which matches Mai's
    single loop but would matter if it were shared across loops.
  - The message is deliberately terse: a task id, not a description.
- **Follow-ups (not done, separate decisions):**
  - Add a Telegram bot-token pattern (raw and percent-encoded) to
    `app/core/logging.py`'s redactor, as defence in depth behind the logger
    pin.
  - Observe one real delivery to confirm the `%3A` path, once a credential is
    configured by the owner.
  - Decide how delivery is triggered, and whether it needs persisted state.

### Telegram foundation: settings and Bot API sender (`6626912`, 2026-10-01)

- **Status:** committed by the owner as a separate commit before 6J; recorded
  here because 6J builds on it.
- Adds `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID` (and the webhook-only
  `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_CONVERSATION_ID`) to settings, compose
  and `.env.example`, and `app/telegram/client.py`: `BotApiSender`, which
  POSTs through Mai's `SecureHttpClient` under a policy for `api.telegram.org`
  only (POST only, redirects refused).
- Verified when committed: HEAD plus these files in an isolated copy, 5632
  passed, 2 skipped. It had no dedicated tests of its own until 6J's, which
  exercise it through the real network policy.

### Stage 6I: Notification delivery boundary (`3e09853`, 2026-10-01)

- **Status:** completed, verified, committed as `3e09853` (it adds
  `backend/app/delivery/`).
- **Purpose:** a clean boundary through which future channels (Telegram,
  web UI, desktop) can consume 6H notifications without touching the task,
  monitoring, authorization, execution or provider architecture.
- **Architecture:**
  - New package `app/delivery/` (deliberately outside `app/tasks`):
    - `contract.py`:
      - `DeliveryPayload`: frozen, strict, `extra="forbid"`, exactly six
        fields: notification id, task id, kind, check number, created time,
        delivery key;
      - `DeliveryStatus` (delivered / duplicate / failed) and
        `DeliveryOutcome` (adds refused);
      - `DeliveryResult`, whose reason must be a code, never a message;
      - the `NotificationAdapter` base (`name`, `async deliver(payload)`);
      - `delivery_key(notification_id, adapter_name)`.
    - `registry.py`: `AdapterRegistry`, the same shape as
      `IntegrationRegistry`. Explicit registration, canonical lowercase
      names, a closed name pattern, no duplicates, sealable, no dynamic
      loading.
    - `service.py`: `NotificationDeliveryService(session, owner_id,
      registry).deliver(notification_id, adapter_name)`. One attempt, never
      raises, writes nothing.
    - `local.py`: `LocalRecordingAdapter`. In-memory, no network,
      deduplicates on `delivery_key`.
  - **Reused, not rebuilt:** reads go only through 6H's owner-scoped
    `NotificationService.get`. The 6H table, writer, uniqueness rule and
    read/unread semantics are unchanged. No migration, and no change to
    `app/tasks`, `app/background`, execution, authorization, providers,
    Telegram, the frontend or `main.py`.
- **Semantics:**
  - Refusals (nothing reaches an adapter): `unknown_adapter`,
    `notification_not_found` (which also covers another owner's
    notification), `malformed_notification` (a non-UUID id, or row data
    failing the strict payload).
  - Adapter outcomes pass through: delivered, duplicate, failed.
  - Failures are contained, with fixed codes: an exception is
    `adapter_error`, and its text is never logged or returned; a hang is
    `adapter_timeout` (10 s bound); a wrong return type is
    `adapter_invalid_status`.
  - Delivery never marks a notification read, never changes task, execution
    or notification state, never retries, and runs no loop.
- **Idempotency:** stateless, as the stage asked. The `delivery_key` is
  derived only from the notification id and the adapter name, so it is
  stable across calls, restarts and processes. Adapters deduplicate on it,
  and the local one does. There is no durable delivered-state; see
  Deferred.
- **Verification:**
  - Tests: 45 behavioural (`tests/test_delivery.py`), using real
    notifications produced through the 6G/6H path, and 22 security
    (`tests/security/test_delivery_security.py`). No existing test changed.
  - Full suite (isolated copy): ordered 5632 passed, 2 skipped (272 s);
    shuffled 5632 passed, 2 skipped with seed 20261003 (274 s) and seed
    683706454 (259 s).
  - Mutation: 26/26 killed. The validated harness passed (two controls
    survived) and there were no survivors. Targets: refusals, owner scoping,
    delivery identity, payload contract, failure containment, registry, and
    local-adapter deduplication.
  - Docker (the existing `mai-backend:latest` image, throwaway PostgreSQL
    database, isolated source mounted read-only), 9/9 checks:
    - the container's own runtime produced a notification;
    - delivered once, then duplicate with the same key;
    - unknown adapter, missing notification and other owner all refused;
    - the payload carried exactly six fields;
    - no pending writes, and no row, task or read state changed.

    Graceful stop, no errors, no secrets in the logs. No live-PostgreSQL
    schema verification was needed, because there was no schema change.
  - Structural audit: 14/14. Still one runtime, runner, authorization
    service, execution service, dispatcher and notification writer, and
    nothing outside `app/delivery` imports it.
- **Security findings:** none in 6I. The tests found two of their own
  defects, both fixed before verification: an unknown-adapter test that
  asserted on an unregistered spy, and an over-strict `compile` check.
- **Known limitations:**
  - Stateless delivery cannot guarantee once-per-adapter across adapter
    instances or restarts; adapters own deduplication.
  - No adapter is registered in production, and nothing triggers delivery.
  - The payload deliberately carries no human-readable text, so a channel
    must fetch any wording through its own authorised read.
- **Deferred:** real channel adapters; durable delivery records; a trigger
  for delivery (who calls `deliver`, and when).

### Stage 6H: Notification infrastructure (`b6d35ff`, 2026-10-01)

- **Status:** completed, verified, committed as `b6d35ff` (it introduces
  this entry and `backend/app/tasks/notifications.py`).
- **Purpose:** monitoring outcomes become durable, owner-scoped
  notifications. 6H is the internal notification contract and persistence
  layer only. It does no delivery and adds no channel, UI or HTTP endpoint.
- **Existing abstraction reviewed first:** the 5F.1 reminder inbox
  (`reminder_notifications`: pending/read, conditional-UPDATE mark-read,
  unique occurrence index). Its pattern is reused. Its table is not: it
  requires a `reminder_id`, copies reminder text and has no owner column, so
  widening it would change 5F.1's invariants.
- **Architecture:**
  - New `task_notifications` table (migration `0018`, additive):
    `owner_id`, `task_id` (FK, cascade), `kind`, `check_number`,
    `execution_id` (nullable FK, set null), `created_at`, `read_at`.
  - New enum `task_notification_kind` (`condition_met`,
    `monitoring_failed`), and one task event, `notification_created`.
  - **No free-text column**: there is nothing to leak, interpret, or use
    to name a channel or recipient.
  - One writer, `app/tasks/notifications.record_outcome`, called from
    exactly two places (structurally pinned): `TaskRunner._check` when a
    condition is met, and `runtime._record_failure` when it blocks a
    monitoring task.
  - The writer is gated on: the kind being the enum, the task being a
    monitoring task, and the task already being in the outcome's state
    (COMPLETED / BLOCKED).
  - `NotificationService(session, owner_id)` is the read contract: `unread`,
    `for_task`, `get`, `mark_read`. Every query is owner-filtered, and
    `mark_read` is a conditional UPDATE.
  - No new loop, runner, authorization or execution path. The runtime
    gained one call into the writer and nothing else.
- **Notification semantics:**
  - condition not met: monitoring continues, no notification;
  - condition met: the task completes, plus one `condition_met`
    notification referencing the proving check's execution;
  - checks keep failing: the task is BLOCKED by the existing 6G policy,
    plus one `monitoring_failed` notification.
  - Waiting for approval, budget exhaustion and refused or invalid
    configuration stop monitoring **without** a notification (see
    limitations).
  - Ordinary (non-monitoring) tasks never notify.
- **Idempotency:**
  - The notification is written **in the same transaction as the outcome**,
    so a crash rolls back both and the retry produces both, once.
  - The unique index `uq_task_notifications_outcome (task_id, kind,
    check_number)` makes "one notification per outcome" a database
    invariant. A duplicate is absorbed in a savepoint without disturbing
    the outcome's transaction.
  - `check_number` is part of the identity because BLOCKED is resumable: a
    resumed task that fails again after new checks is a new outcome.
- **Verification:**
  - Tests: 34 behavioural (`tests/test_notifications.py`) and 24 security
    (`tests/security/test_notifications_security.py`). One existing pin
    changed: the task-package module list gained `notifications.py`, which
    brings it under that suite's structural audits.
  - New structural test: every `task_event_type` value beyond 6A's set must
    be added to the PostgreSQL enum by some migration. SQLite cannot catch
    a missing value.
  - Full suite (isolated copy): ordered 5565 passed, 2 skipped (258 s).
    Shuffled (scratch shuffle plugin, seeds reported) 5565 passed, 2
    skipped on each of seeds 1573485647, 1452915612, 81492239 and
    1220690816. Two further shuffled runs segfaulted (Known defect 4).
  - Mutation: 30/31 killed. The validated harness passed (two controls
    survived). The one survivor is a documented equivalent: an *extra*
    notification call before the block is refused by the state gate. The
    genuine move of the call ahead of the block is killed. The first run
    found no test gaps; one mutation was rewritten because it added rather
    than moved the call.
  - SQLite migration round trip is exact (test included).
  - Live PostgreSQL 16 (throwaway database):
    - schema diff showed only the new type, table, indexes and one enum
      value, and the downgrade/upgrade cycle was exact (the type is dropped
      on downgrade);
    - 17/17 schema and constraint checks and 27/27 behavioural checks
      passed: 4 racing checks gave 1 notification; 6 racing writers gave
      1 row; racing ticks gave 1; crash, retry and resume all behaved;
      owner isolation held;
    - with the unique index dropped, 3 sequential replays wrote 3 rows
      (1 with it), which proves the index is the guarantee;
    - cleanup left zero rows.
  - Docker (the existing `mai-backend:latest` image, throwaway database):
    migrations ran to 0018, there was one runtime, and the met monitor,
    failing monitor and ordinary task all produced the right notifications.
    After a container restart there were no duplicates. Graceful stop, no
    errors, no secrets in the logs.
  - Structural audit: 18/18.
- **Security findings:**
  - No defect in 6H.
  - Concurrent writers also collide on the journal's `(task_id, sequence)`
    index. That is timing-dependent defence in depth, not the guarantee.
  - Pre-existing, not changed: the 5F.1 reminder inbox has no owner column
    and its read path is unscoped. That is harmless with today's single
    owner, but it must be addressed before a second owner or an external
    channel reads it.
- **Known limitations:**
  - No delivery and no read route (see Deferred).
  - Monitoring that stops for approval, budget or configuration reasons
    is not notified.
  - A resumed task that blocks again with no newly claimed check (runner
    errors only) shares the earlier outcome's identity and is not
    re-notified.
  - Notifications cascade with their task, and downgrade drops them.
  - No retention policy.
- **Deferred:** external delivery adapters (Telegram and others, as
  separate tracks); a person-facing read surface; one inbox across
  reminders and tasks.

### Fix: OAuth test independent of global event-loop state (`5e1fc51`, 2026-10-01)

- **Status:** completed, test-only (one file).
- The tampered-redirect test is now an `async def` test (as its neighbours
  are, under `asyncio_mode = auto`) instead of calling
  `asyncio.get_event_loop()`. The assertions are unchanged.
- **Verification:** the order that used to fail (prompt-formatter test,
  then the OAuth test) now passes, and `tests/security/test_oauth_security.py`
  passes in full. A full shuffled suite run was not repeated.

### Stage 6G: Monitoring runtime (`d249170`, 2026-10-01)

- **Status:** completed, verified, committed.
- **Purpose:** a monitoring task checks one read-only capability repeatedly
  until a typed condition holds. It is built on the existing TaskRunner,
  authorization, execution and BackgroundRuntime. No new loop, lease, runner
  or authorization path was added.
- **Architecture:**
  - New `app/tasks/monitoring.py`:
    - typed condition model (kinds `count`/`value`/`contains`, closed
      operator set, plain lowercase dictionary-key paths of at most 4
      segments);
    - bounded and finite expected values; interval 300 s to 7 days,
      refused rather than clamped;
    - `MONITORABLE_CAPABILITIES` literal read-only allow-list;
    - one pure evaluator returning satisfied / not satisfied / unable.
  - `TaskService.configure_monitoring`: once, before plan authorization, for
    one-step plans only.
  - `TaskRunner.check()`:
    - goes through the same gates as `advance()`;
    - exactly-once per check via a conditional UPDATE on `tasks.check_count`;
    - execution identity `task:{id}:check:{n}`;
    - the check number is released when the check waits for approval.
  - `advance()` refuses monitoring tasks.
  - The runtime routes `task.monitor is not None` to `check()`:
    - not met: reschedule after the interval;
    - met: complete and unschedule;
    - unable to evaluate, or execution failure: bounded `_record_failure`,
      never retried sooner than the interval.
  - Migration `0017` (additive): `tasks.monitor` (JSON/JSONB, nullable),
    `tasks.check_count` (int, default 0, `>= 0` check), and 4 event types.
- **Verification:**
  - Focused and security tests: 140.
  - Mutation: 57/57 killed. The validated harness passed (two controls
    survived). The first run left 6 survivors, all real gaps, each closed
    with a test.
  - Full suite (isolated copy of the commit): ordered 5504 passed, 2 skipped
    (245 s). Shuffled with seed 20261001: 5504 passed, 2 skipped (255 s).
    Shuffled with seed 488176046: 1 failure, the pre-existing OAuth
    order-dependence (Known defect 2).
  - Live PostgreSQL 16 (throwaway database):
    - schema diff showed only the intended changes, and the
      downgrade/upgrade cycle restored it exactly;
    - 77/77 behavioural checks passed: concurrent checks gave one winner and
      one execution; not met / met / failure-to-BLOCKED behaved as designed;
      hostile specs were refused; owner isolation held;
    - cleanup left zero rows.
  - Docker: ran the existing `mai-backend:latest` image against a throwaway
    database. Migrations ran from empty to 0017, there was one runtime, and
    monitoring and ordinary tasks were routed correctly. Graceful stop, no
    secrets in the logs.
- **Security findings:**
  - Live PostgreSQL found the check constraint named
    `ck_tasks_ck_tasks_check_count_non_negative`; it was fixed with `op.f()`
    and pinned by a test.
  - NaN expectations slipped past the range check; they are now refused, and
    observed NaN or infinity evaluates as unable.
- **Known limitations:**
  - The `max_tool_calls` budget (default 40) caps the number of checks.
  - There is no person-facing surface to configure monitoring.
  - There is no notification on trigger (6H).
  - Gmail-backed monitoring needs a HIGH-risk standing grant or per-check
    approval.
  - External effects are at-least-once in the documented crash window.
- **Follow-ups:** the OAuth test order-dependence was fixed afterwards in
  `5e1fc51`. Adopt a real shuffle plugin for randomized runs. 6H when
  requested.

### Gemini as a second LLM provider (`b14b71a`, 2026-09-30)

- **Status:** completed (not a numbered stage).
- **Purpose:** add `LLM_PROVIDER=gemini` alongside Groq. Groq stays the
  default.
- **Architecture:** `GeminiProvider` subclasses `OpenAICompatibleProvider`
  (name only). It goes through `SecureHttpClient` with a single-host policy
  for `generativelanguage.googleapis.com`; no SDK, no fallback, no routing.
  `GEMINI_API_KEY`/`GEMINI_MODEL` are passed explicitly in compose;
  `GEMINI_BASE_URL` deliberately is not.
- **Verification:**
  - 47 new tests.
  - 13/13 mutations killed.
  - Full suite 5363 passed, 1 skipped, twice. The "randomly ordered" claim
    is unverifiable (Known defect 3).
  - Live smoke test against the Gemini API; the key was absent from logs and
    the database.

### Fix: exec uvicorn as PID 1 (`93da641`, 2026-09-29)

- `sh -c` held PID 1 and swallowed SIGTERM, so the lifespan shutdown
  (`BackgroundRuntime.stop`, disposal) never ran. The fix adds `exec` before
  uvicorn in `docker/backend/Dockerfile`. Graceful shutdown was confirmed
  during 6G Docker verification.

### Stage 6F: One durable background runtime (`ad07585`, 2026-09-29)

- **Purpose:** generalise the 5F.1 reminder poller into the one
  `BackgroundRuntime` for reminders and tasks. `ReminderScheduler` is an
  alias.
- **Architecture:**
  - Task claim is a conditional UPDATE moving `tasks.next_run_at` to a lease
    horizon (300 s).
  - Each task has its own transaction.
  - Failures back off 60 s, then 120 s, then the task is BLOCKED after 3.
  - Waiting, finished or refused tasks are unscheduled.
  - `TaskService.schedule_background` is the only scheduler entry point.
  - Migration `0016`.
- **Verification:**
  - 38/38 mutations killed.
  - Full suite 5313 passed, 1 skipped.
  - Live PostgreSQL: 6 concurrent connections gave 1 claim; restart
    survival; exactly one runtime.
- **Findings:** the pre-existing Docker PID 1 SIGTERM issue was fixed
  separately (`93da641`).

### Stage 6E: Standing approvals (`552626a`, 2026-09-29)

- **Purpose:** scoped, expiring (one week maximum), revocable grants that
  remove the approval prompt for one canonical capability and nothing else.
- **Architecture:**
  - `authorize_with_grants` wraps the unchanged `authorize`.
  - CRITICAL stays forbidden.
  - Grant status is derived from timestamps.
  - Revocation is a conditional UPDATE and never deletes.
  - Migration `0015`.
- **Verification:**
  - 36/37 mutations killed (1 documented equivalent).
  - Full suite 5251 passed, 1 skipped.
  - Live PostgreSQL passed.
- **Findings:** generic `sa.Enum(create_type=False)` re-created an existing
  PostgreSQL enum (DuplicateObjectError, hidden by SQLite). Fixed with
  `postgresql.ENUM`.

### Stage 6D: Task runner (`e9cee67`, 2026-09-26)

- **Purpose:** `TaskRunner.advance` runs one ready step per invocation
  through the existing dispatcher. It has no loop of its own.
- **Architecture:**
  - Conditional-UPDATE step claim.
  - Authorization re-bound at run time.
  - Task authorization expires.
  - `max_steps` and `max_tool_calls` budgets are enforced.
  - Only the runner writes `RUNNER_ONLY_STATES`.
  - Migration `0014`.
- **Verification:**
  - 37/39 mutations killed (2 documented equivalents).
  - Full suite 5163 passed, 1 skipped.
  - Live PostgreSQL passed.
- **Findings (live):** a human approval could never take effect, and a
  blocked invocation consumed budget. Both were fixed.

### Stage 6C: Capability binding and plan authorization (`a5d31dc`, 2026-09-25)

- **Purpose:** bind plan steps to declared, available, permitted capabilities;
  authorize plans all-or-nothing.
- **Architecture:**
  - `app/tasks/capabilities.py`.
  - One AuthorizationService and one Execution constructor, both asserted
    structurally.
  - Readiness follows the validated graph.
  - Migration `0013`.
- **Verification:**
  - 39/39 mutations killed.
  - Full suite 5096 passed, 1 skipped.
  - Live PostgreSQL passed.

### Stage 6B: Plan validation and preview (`e4d66af`, 2026-09-25)

- **Purpose:** validate plans with the one graph validator before
  persistence; `GET /api/tasks/{id}/plan` preview.
- **Architecture:** no migration.
- **Verification:**
  - 36/37 mutations killed (1 documented equivalent).
  - Full suite 4979 passed, 1 skipped.
  - Live PostgreSQL passed.

### Stage 6A: Persisted task model and activity stream (`02be9dd`, 2026-09-25)

- **Purpose:** `tasks`, `task_steps` and an append-only `task_events`
  journal.
- **Architecture:**
  - User-created only.
  - Read-only API.
  - Owner column from the start (`LOCAL_OWNER_ID`).
  - Migration `0012`.
- **Verification:**
  - 39/39 mutations killed.
  - Full suite 4881 passed, 1 skipped.
  - Live PostgreSQL passed.

### Stage 5F.2: Gmail daily assistant (`5d891d3`, 2026-09-24)

- **Purpose:** triage/attention intent, wider mail grammar, and count
  grounding. Still read-only, with no new scope.
- **Verification:**
  - 32/32 mutations killed.
  - Full suite 4770 passed, 1 skipped.
  - Live: routing only. There was no Gmail grant, so no mailbox was
    contacted.

### Stage 5F.1: Scheduled reminders (`faf2a5f`, 2026-09-24)

- **Purpose:** set, list and cancel reminders, parsed deterministically.
  Exactly-once firing by conditional UPDATE plus a unique occurrence index.
  Migration `0011`.
- **Verification:**
  - 37/37 mutations killed.
  - Live PostgreSQL end to end.
- **Findings (live):** empty assistant messages, and a calendar grammar
  collision. Both were fixed.

### Stage 5E: Research audit and fixes (`53834df`, `c61bee9`, `f846129`, 2026-09-22/23)

- `53834df`: audit only (`docs/audits/5E-research-audit.md`).
- `c61bee9`: 5E.1 compound-query construction and 5E.2 freshness reaching
  the provider (`prefer_recent` becomes `topic=news`), plus published dates
  and URL deduplication. 24/24 mutations; 4471 passed, 1 skipped.
- `f846129`: report the search provider actually used (was a stale
  "brave"). 9/9 mutations; 4482 passed, 1 skipped.

### Stage 5D: Context continuity and execution truthfulness (2026-09-21/22)

- 5D.0 `e803e4b`: audit (documentation only).
- 5D.1 `25bbcb5`: execution truthfulness and anti-fabrication. 4300 passed,
  1 skipped (acceptance report).
- 5D.2 `3f4200e`: context resolution and conversational continuity. 4405
  passed, 1 skipped (acceptance report).

### Stage 5C: ChatGPT history import (`9c4ec8a`, 2026-09-20)

- Personal-context ingestion with memory provenance. Migration `0010`. 4201
  passed, 1 skipped (acceptance report).

### Stage 5A.1 / 5A.2: Freshness and synthesis reliability (`2392c6d`, `b8d2ab7`, `3b69fdb`, 2026-09-18/19)

- 5A.1: universal freshness and current-information routing.
- 5A.2: synthesis reliability and the response contract. 4099 passed,
  1 skipped (acceptance report).

### Stage 5B: Secure Gmail read-only integration (`f9bb895`, `4e36849`, 2026-09-14)

- Read-only Gmail via the existing OAuth and network boundary. See
  `docs/stage5b_acceptance_report.md` (it states only the pre-stage baseline
  count).

### Stage 5A: Natural-language robustness (`c255b2d`, `46f9e11`, 2026-09-14)

- See `docs/stage5a_acceptance_report.md`.

### Stage 4H: Personal assistant composition (`eef2969`, `1d9df26`, 2026-09-13)

- See `docs/stage4h_acceptance_report.md` (it states only the pre-stage
  baseline count).

### Stage 4F/4G: Integrations, gateway, OAuth, calendar (2026-09-02 to 2026-09-10)

- 4F-A `1d67d50`: external integration foundation (`NetworkPolicy`).
- 4F-B `eebcd2b`: secure web research; enforced network boundary
  (`SecureHttpClient`).
- 4F-C `0e1f919`: LLM traffic brought through the same network boundary.
- 4F-D `9d14888` (+ `953d744` Tavily provider, `07f3827` docs closing the
  live-search verification gap): research in chat. Migration `0008`.
- 4F-E `676b2e9`: controlled multi-step workflows. Migration `0009`.
- 4F-F `87dac87`: multi-provider LLM gateway (no fallback). 4F-F.1
  `86d21c8`: natural-language search bridge.
- 4F-G `87ab02a`: secure OAuth and read-only Google Calendar.
- 4G.1 `b15607d` (+ `c64ab84`): natural-language calendar availability.

### Stage 4E / 4E.1: Controlled execution (`89f38eb`, `822e275`, 2026-09-01)

- Execution records, approvals and the append-only audit journal. Migration
  `0007`. `EXECUTION_ENABLED` defaults to off. See
  `docs/stage4e_acceptance_report.md`.
- 4E.1: runtime capability truthfulness.

### Stage 4D / 4D.1: Action proposal and runtime identity (`07ddfe8`, `33a3dbe`, `a5167e5`, 2026-08-31 to 2026-09-01)

- 4D: propose and authorize, never execute.
- 4D.1: authoritative runtime facts in the prompt; verified against 31
  acceptance requirements.
- `f4cdc2a`: migration 0003 deduplication made PostgreSQL-compatible.

### Stages 4A–4C: Intent, planning, tool authorization (`56f42ae`, `76fc28b`, `ad5869d`, 2026-08-30)

- 4A intent understanding, 4B goal planning and graph validation
  (`validate_graph`), 4C tool registry, policy (`MAX_PERMITTED_RISK`) and
  `AuthorizationService`. See the per-stage acceptance reports.

### Stage 3: Knowledge-aware chat and security audit (`c7a063b`, `8e71003`, `b7a8ef1`, 2026-08-30)

- 3B prompt integration (748 passed), 3C conflict resolution (896 passed;
  migration `0006`), 3D full security and privacy audit (1205 passed).

### Stages 2A–3A: Memory, entities, relationships, retrieval, context (`75c40fe`, 2026-08-30)

- One commit covering 2A memories (migrations `0002`, `0003`), 2B entities
  (`0004`), 2C relationships (`0005`), 2D retrieval, and 3A context assembly.
  See the per-stage acceptance reports.

### Stage 1: Foundation (`4e26dfe`, 2026-08-29)

- FastAPI, SQLAlchemy and Alembic backend; Next.js frontend; Docker compose;
  `LLMProvider` abstraction with Groq. Migration `0001`. See
  `docs/architecture.md` and `docs/stage1_acceptance_report.md`.
