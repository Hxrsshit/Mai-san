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

## Current state (as of 2026-10-01)

| | |
| --- | --- |
| Latest completed stage | **Stage 6H: notification infrastructure**: the commit titled `Stage 6H: notification infrastructure` (the commit that adds `backend/app/tasks/notifications.py`; find it with `git log -1 -- backend/app/tasks/notifications.py`) |
| Previous stage | Stage 6G: monitoring runtime, `d249170` |
| Branch | `main` (no remote push recorded here) |
| Latest migration | `0018_task_notifications.py` (head) |
| Next stage | **6I: not started.** Do not begin it without an explicit request. |

### Uncommitted / in progress (NOT completed)

These are in the working tree and are **not** part of any completed stage. Do
not commit them as part of other work, and do not describe them as done.

- **Telegram adapter: uncommitted and incomplete.**
  - Files: `backend/app/telegram/`, `backend/app/api/routes/telegram.py`,
    `backend/tests/test_telegram_adapter.py`, plus Telegram-related edits to
    `backend/app/api/routes/__init__.py`, `backend/app/api/routes/conversations.py`,
    `backend/app/core/config.py`, `backend/app/main.py`, `docker-compose.yml`
    and `.env.example`.
  - It previously hit a Python 3.9 compatibility issue, which was corrected.
  - Focused testing then exposed a defect in the conversation route (see
    *Known defects*). That defect is unresolved, and the adapter is not
    verified.
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
     across threads (in test fixtures, not the application). Not
     investigated further. Treat a segfaulted run as incomplete, never as a
     pass.
5. **Local Docker builds fail:** `docker build` cannot fetch
   `python:3.12-slim` metadata (`DeadlineExceeded`). Only the cached
   `mai-backend:latest` image is available on this machine.

### Deferred / not built

- **Notification delivery.** 6H records notifications; nothing delivers
  them. External channels (Telegram, email, push) are adapters to be built
  later, reading through `NotificationService`. Telegram is a separate track
  and is not part of 6H.
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

### Stage 6H: Notification infrastructure (commit titled `Stage 6H: notification infrastructure`, 2026-10-01)

- **Status:** completed, verified, committed. A commit cannot contain its
  own hash. It is the commit that introduces this entry and
  `backend/app/tasks/notifications.py`.
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
