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

## Current state (as of 2026-10-28)

| | |
| --- | --- |
| Latest completed stage | **Stage 7A: Organisation & User Identity**, `8f3ef2f` (2026-10-26; see Completed). Organisation and membership identity with database-backed, fail-closed membership checks; **tenant isolation is not claimed** and member Telegram turns remain disabled |
| Since 6M.1 | Fixes (not stages): the metadata registry registers the workflows models, `107d879` (Known defect 6 resolved); the owner committed the Telegram webhook, the frontend/branding work and the `conversations.py` `payload` fix in `e10b7ca` (Known defect 1 resolved); after 6P, the frontend authentication token flow, the commit titled `Fix frontend authentication token flow` (frontend and docs only); then the Stage 0 consolidation checkpoints (not stages), each committing work that had been uncommitted: C1 `fc80592`, C2 `30a1342`, C3 `7ac52b4`, C4 `4b8d407`, C5 `d4f8339` (documentation only); then the multi-agent workflow configuration, `5c7801c` (tooling and docs, no application code); then the tab accessibility fix, the commit titled `fix: link only the selected tab to its panel` (frontend only) |
| Previous stages | 6P harden monitoring concurrency and truthfulness, the commit titled `Stage 6P: harden monitoring concurrency and truthfulness` (the commit that adds `docs/STAGE_6O_AUDIT.md`; Stage 6O was an inspection-only audit with no commit of its own); 6N confirmed Gmail monitoring, `94ea032`; 6M.4 authentication, `25bfb98`; 6M.3 automatic notification delivery, `1e2ddd2`; 6M.2 notification read API, `922dbbd`; 6M.1 durable notification delivery records, `87365aa`; 6L notification invocation boundary, `a0e16ae`; 6K notification delivery composition, `bb0ea41`; 6E test isolation fix (test only), `20468d3`; 6J Telegram notification adapter, `e6effdb`; Telegram foundation, `6626912`; 6I notification delivery boundary, `3e09853`; 6H durable task notifications, `b6d35ff` |
| Branch | `main`. Nothing is pushed: on 2026-10-29, after Stage 7B L0 (`e1e8b55`), local `main` was 18 commits ahead of `origin/main` (`e10b7ca`) by the local remote-tracking ref, which was not refreshed (no fetch); the `docs:` commit recording it adds one more. |
| Latest migration | Committed head: `0022_scoped_uniqueness.py` (Stage 7B units 5-6; per-scope unique indexes; verified on SQLite and disposable PostgreSQL 16 by `tests/test_scoped_uniqueness_migration_pg.py`). Before it, `0021_knowledge_scope.py` (Stage 7B L0, `e1e8b55`), verified on throwaway SQLite files and, on 2026-10-29, on a disposable PostgreSQL 16 server by the permanent gated tests (`tests/test_knowledge_scope_migration_pg.py`, 17 passed). `0020`: verified on throwaway SQLite files and on disposable PostgreSQL (2026-10-21, and again 2026-10-29 by `tests/test_identity_migration_pg.py`). **The real `mai` database is still at `0019`** (as last recorded; not re-checked): the owner's backend container applied 0017 to 0019 at its startup on 2026-10-02; it runs `alembic upgrade head` at start-up and mounts `./backend` from the working tree, so restarting it now would apply `0020`, `0021` and `0022` (security review H1; changing that needs a separate approved plan). |
| Next stage | **Stage 7B (Multi-Tenant Isolation & Permissions)**, under the owner's autonomous directive of 2026-10-29: units **L0** (`e1e8b55`), **4** (`6ecb048`, scoped conversations) and **5-6** (`1cb1e94`, scoped knowledge) and **7** (background membership gates; see Completed) completed; the remaining units and their status are tracked in `docs/stage7b_progress.md`. Stage 7A is completed (`8f3ef2f`). Member Telegram turns stay disabled. |
| Uncommitted work (2026-10-29) | Only the untracked `MAi branding .png`. Stage 7A's four agents left four worktrees and branches (`worktree-agent-a31992d10364e0824`, `-ab1bb466a8cc75f7d`, `-abaa0ef6a0bf4bbc5`, `-ac85feb454bd0056a`, all at `b7b6680`) under the gitignored `.claude/worktrees/`; their work is all in `8f3ef2f`, and their removal awaits the owner's approval. What is running was last checked on 2026-10-16 (see Stage 0 C5): `mai-backend` started 2026-10-14 09:48 UTC and was still unchanged on 2026-10-29 (same ID and start time, 0 restarts). |

### Uncommitted / in progress (NOT completed)

These are in the working tree and are **not** part of any completed stage. Do
not commit them as part of other work, and do not describe them as done.

- **`MAi branding .png`** at the repository root: untracked.
- **Multi-agent pilot leftovers (2026-10-19/20), under the gitignored
  `.claude/worktrees/`.** *(Resolved 2026-10-20: removed with the owner's
  approval and verified -- only the main worktree remained, both branches
  deleted with `-d`, `frontend/node_modules` untouched.)* All three worktrees were at `5c7801c`:
  `pilot-integration` (detached HEAD), `agent-af25806cd3bbce2df` (branch
  `worktree-agent-af25806cd3bbce2df`) and `agent-a0374c4b9fd2da035` (branch
  `worktree-agent-a0374c4b9fd2da035`). The pilot integration worktree's two
  modified files are byte-identical to the tab accessibility fix. Each agent
  worktree holds an earlier draft of one of those files, which that fix
  supersedes. `.claude/worktrees/frontend/` is not a worktree. It is build
  output: with `output: "standalone"`, `next build` in a worktree follows its
  `node_modules` symlink and writes traced files there. Cleanup awaits the
  owner's approval.
- *(Resolved, 2026-10-16)* The workstreams listed here on 2026-10-13 and
  2026-10-14 (frontend token-storage fix, LLM provider reliability,
  intent-classifier truncation, the UI usability phase, monitor metadata on
  `TaskRead`, the monitor journal patch, the pre-commit cleanup, memory recall
  reliability and memory retrieval quality) are committed as Stage 0 C1 to C4.
  Their entries moved to Completed, corrected where later evidence changed
  them.
- *(Resolved)* The uncommitted Stage 6M.4 work noted here on 2026-10-07 was
  this stage in progress; it is now committed (see Stage 6M.4). The four AST
  failures seen in one shuffled run (seed 1703306132) during the `7e7b978` fix
  came from pins that 6M.4 then deliberately updated while it was being
  written; the final 6M.4 suite is green (see its entry).
- *(Resolved)* The Telegram chat/webhook interface (`routes/telegram.py`,
  `tests/test_telegram_adapter.py` and its `main.py` / `routes/__init__.py` /
  `conversations.py` edits) and the frontend/branding changes were committed
  by the owner in `e10b7ca`, together with the `payload` fix (Known defect 1).
  They were not verified as part of any stage. The webhook's own Telegram
  sender is still to be reconciled with the 6K composition (see Deferred).

### Known defects

1. ~~**`backend/app/api/routes/conversations.py` (~line 238) referenced the
   undefined name `payload`.**~~ **Resolved in `e10b7ca`** (the helper now
   passes its own `content` parameter; one line; chat tests and the webhook
   tests passed, and a live chat turn returned 201 on 2026-10-02). Original
   entry: The extracted helper calls
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
7. ~~**Order-dependent Stage 4C/4D security test.**~~ **Resolved in
   `7e7b978`** (test only: each guard test now removes its throwaway
   subclass and asserts it is gone; the Stage 4C check is unchanged). Original
   entry (found in 6M.3, not caused by 6M.3): `test_tool_mutation_guards.py` and
   `test_orchestration_mutation_guards.py` define a throwaway
   `class _Executable(Tool)` with `execute`. It stays in
   `Tool.__subclasses__()` until garbage collection, so
   `test_orchestration_security.py::test_stage_4c_still_defines_no_way_to_run_a_tool`
   fails when one of them runs first in the same process. It reproduced
   deterministically on clean `922dbbd` (the guard test, then that test: 1
   failed) and surfaced in one shuffled full run (seed 1265397829). It is
   offered as a separate test-only fix.
8. ~~**`TaskService.transition` writes state unconditionally**~~ **Resolved in
   Stage 6P** (compare-and-set on every lifecycle write; see Stage 6P).
   Original entry: (found during
   6N; pre-existing since 6A). It reads the task, checks the edge, then
   updates by id only, so two concurrent transitions on PostgreSQL can lose
   one (the second overwrites the first). 6N does not change it; for the one
   path 6N made reachable -- a cancel racing a monitoring check -- the
   cancel now locks and re-reads the row first (`SELECT ... FOR UPDATE`) and
   the runner's completion is conditional, so that race resolves correctly
   (verified on PostgreSQL with two connections, including a control that
   fails without the lock). Other concurrent transitions remain exposed.
9. ~~**`ChatService.start_conversation_with_message` is broken dead code**~~
   **Resolved in Stage 6P** (confirmed unreachable and removed). Original entry:
   (found during 6N; pre-existing). It unpacks five values from
   `send_message`, which has returned nine since 5B (ten since 6N), so it
   would raise `ValueError`. Nothing calls it. Not fixed in 6N.
10. **Tooling: Claude Code permission rules miss other forms of the same git
    command** (found in the 2026-10-19 multi-agent pilot; not an
    application defect). The pilot session reported that a
    `git -C <path> worktree add` ran without the prompt that
    `Bash(git worktree *)` in `.claude/settings.json` is meant to force. That
    report was not reproduced (dangerous forms were not tested on purpose).
    The cause was confirmed from the Claude Code permission documentation.
    Rules match the command text, and everything before the first
    `*` must appear as written, so a global option before the subcommand
    (`-C`, `-c`, `--git-dir`) escapes every git rule in the file. The same
    applies to the `git -C <worktree> add` that `CLAUDE.md`'s integration
    step 2 tells the lead to run. `5c7801c`'s commit message already says
    these rules are guard-rails, not a security boundary. **Not fixed:**
    the owner has a proposal (extra `ask` rules of the form
    `Bash(git * <subcommand> *)`), not yet approved. It would still miss
    `/usr/bin/git`, `env git`, `sh -c`, quoted subcommands and git aliases.
11. ~~**`tests/security/test_tasks_security.py::test_no_completed_task_can_exist_in_this_stage`
    passed vacuously**~~ (found 2026-10-20 while probing the Stage 7A seam;
    pre-existing). Its `db_session` left a write transaction open on the
    shared SQLite connection, so the request's `/api/tasks/activity` queries
    failed ("cannot start a transaction within a transaction");
    `TaskService.list_tasks` swallowed that as `([], 0)`, and the test's
    `did.total == 0` passed without reading anything. **Resolved in `8f3ef2f`
    (Stage 7A):** the test commits before the request and asserts 200,
    `waiting_for.total == 3` (the three BLOCKED tasks) and `did.total == 0`,
    with no "Failed to list tasks" log. Mutation-tested: making "did" count
    BLOCKED tasks, making `list_tasks` return empty, and removing the commit
    each fail it; every mutated file restored byte for byte. The underlying
    pattern (`list_tasks` and other helpers return empty on a database
    error) is unchanged and noted for a future audit.
12. **Models and migrations disagree in four places (pre-existing; found
    2026-10-28 by Alembic's comparison on PostgreSQL during the `0021`
    verification).** The migrated database has, and the models do not
    declare:
    - `task_steps.arguments` server default `'{}'`
      (`0013_task_capabilities.py`; the model has only `default=dict`);
    - index `ix_task_steps_capability` (`0013`; not in `TaskStep`'s
      `__table_args__`);
    - `workflows.created_at` and `workflows.updated_at` server default
      `now()` (`0009_workflows.py`; the model has only `default=utcnow`).

    Unrelated to Stage 7B L0: L0 does not touch `app/tasks/models.py` and
    only adds the scope mixin and CHECK to `Workflow`; the schema before and
    after `0021` differed only by the scope objects. Impact is low (the
    database is no looser than the models), but `alembic revision
    --autogenerate` would propose dropping the defaults and the index.
    **Not fixed** (owner instruction, 2026-10-29): aligning them needs its
    own approved change.

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
- ~~**Automatic delivery.**~~ Built in 6M.3 (opt-in; see Completed).
- ~~**For 6M.4: the owner of background work / No authentication.**~~ Built
  in 6M.4 (see Completed). Still deferred:
  - **Per-device or revocable tokens:** one token today; rotating it means a
    new hash and a restart. An `api_tokens` table was considered and
    declined for 6M.4.
  - ~~**A masked token prompt:** the browser uses `window.prompt`, which shows
    what is typed.~~ Built in the commit titled `Fix frontend authentication
    token flow`: `frontend/components/AuthDialog.tsx` uses a password field.
  - **No rate limit on failed authentication:** a 256-bit token cannot be
    guessed, but repeated 401s are only logged, not throttled.
  - **`TaskService` and the 6K composition keep their defaults:** the
    configured owner, now from `app.core.owner`. Routes pass the
    authenticated owner explicitly; in-process callers (chat turns, the
    runner) use the default, which is the same owner.
- **The webhook builds its own Telegram sender.** The webhook
  (`backend/app/api/routes/telegram.py`, committed in `e10b7ca`) constructs a
  `BotApiSender` per request in `get_telegram_sender`, a second Telegram
  send path beside the 6K composition. It should be reconciled with the
  composition.
- **Stage 6M plan (owner decisions, 2026-10-02).** Four commits, in
  dependency order, each verified and committed alone, with an owner
  checkpoint before the next:
  1. 6M.1 durable delivery records: done (see Completed);
  2. 6M.2 task-notification read API: done (see Completed);
  3. 6M.3 automatic delivery: done (see Completed);
  4. 6M.4 authentication: done (see Completed; one hashed token, not a
     table, by the owner's decision on 2026-10-07).

  Each needs its own approval of details before implementation.
- ~~**No read surface for task notifications.**~~ Built in 6M.2 (list, get
  and mark-read). Still deferred: a per-task lookup (6H's `for_task`) and an
  unread count.
- **Two inboxes.** Reminder notifications (5F.1) and task notifications (6H)
  are separate tables. Unifying them behind one read contract is deferred.
- No HTTP or UI surface exists for `TaskService.schedule_background`,
  `TaskService.configure_monitoring` or standing-grant creation. Since 6N
  they are reachable from a person in exactly one way: a confirmed Gmail
  monitoring proposal in an authenticated chat turn (see Stage 6N). Still
  deferred from 6N: ~~a frontend view of monitors and notifications with a
  cancel button~~ (built in Stage 0 C3, `7ac52b4`; not yet deployed); other
  monitorable sources; monitoring requested from
  Telegram; per-argument (rather than capability-wide) grants; amending a
  proposal in place; recurring monitors after a match.
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
- Dependency advisories, **none resolved, and not a complete inventory**:
  `starlette 0.52.1` (the version `backend/constraints.txt` pins; the local
  test venv has 0.49.3, so local tests run against an older Starlette than
  the pin); `next@15.5.4` (CVE-2025-66478). `npm audit` on 2026-10-14
  reported three vulnerable frontend packages: `next` (critical, several
  advisories), `postcss` (high, a copy at or below 8.5.22) and `sharp`
  (high, inherited from libvips, librsvg and libheif). The Debian image layer
  has not been scanned. Remediation is a separate task needing its own plan
  and approval.
- Google Calendar reports `reauthorisation_required`.
- Groq `SSLV3_ALERT_BAD_RECORD_MAC` TLS errors are not investigated.

---

## Completed (newest first)

### Stage 7B unit 7: background membership gates and revocation races (this commit, 2026-10-29)

- **Status:** completed (a Stage 7B unit). No migration, no dependency
  change. Progress: `docs/stage7b_progress.md`.
- **What:**
  - `lock_active_principal` (`app/identity/principal.py`): the one
    active-membership read, holding a share lock (`FOR SHARE OF
    organisation_members` on PostgreSQL) until the caller's transaction ends.
    `load_active_principal` and it now share one statement builder.
    `KnowledgeScope.member_id` names the member a personal scope belongs to
    (refused for the organisation scope).
  - The background knowledge pipeline (`app/memory/tasks.py`): every step's
    transaction -- memory, entities (per memory), relationships (per memory),
    conflicts (per memory) -- starts by re-reading the member behind the
    turn's frozen scope under that lock. Unless the member is still active, in
    an active organisation, with exactly that scope, the step does nothing
    and the chain stops. Revocation (`mark_revoked`) takes the same row FOR
    UPDATE, so a step either sees the revocation or finishes before it
    commits: nothing is written for a member after their revocation. A
    failed check is a stop, never a pass.
- **Not changed, already covered:** the task runtime. Revocation cancels
  every non-terminal task of the member in its own transaction (Stage 7A),
  and a task cancelled between discovery and claim, or after the claim, is
  never executed (existing tests, Stages 6M/6P). `app.background`, `tasks`,
  `authorization` and `execution` must not import identity (a Stage 7A
  structure test), so no gate was added there. Notification delivery only
  ever targets the configured Admin chat; who receives a member's
  notifications is Telegram binding (unit 11).
- **Tests:** `tests/test_background_membership_gates.py` (9): an active
  member is served (not vacuous); a revoked or invited member, a member with
  no membership row, and a suspended organisation get no model call and no
  write; a revocation between the memory and entity steps stops the rest of
  the chain; a member moved to another organisation no longer matches the
  turn's scope; a failing membership read stops the work; the gate's
  statement is FOR SHARE OF the membership row. Gated PostgreSQL
  (`tests/test_background_membership_races_pg.py`, 2, forced races): a
  revocation started while a step holds the gate waits for the step's
  commit and then wins (the step's write stands, nothing after); a step
  started while a revocation holds the row waits and then does nothing.
  One unit 5-6 isolation test now gives the colleague a real active
  membership, so it still tests the scoped read rather than the gate.
- **Mutation check:** four defects, each caught: the memory step ungated (6
  failed), the entity step ungated (1), the scope match dropped (1), the
  share lock dropped (both PostgreSQL races); sources restored byte for
  byte.
- **Verification (SQLite + disposable PostgreSQL 16, 2026-10-29):** full
  suite ordered **7250 passed, 1 xfailed, 0 failed, 0 skipped**; shuffled
  (seed 803511492) **7250 passed, 1 xfailed, 0 failed**.
  `tests/security/test_secrets.py` excluded as before. (A first run failed
  one test: the scope-write tripwire caught the gate naming the scope's
  `scope_id` directly; it now uses the new `KnowledgeScope.member_id`,
  which refuses a non-personal scope.)
- **Consequence:** a step holds the share lock for its own duration,
  including its model call, so a revocation can wait up to one step's
  model timeout. Accepted: bounded, and it is what makes "nothing after
  revocation" true.

### Stage 7B units 5-6: scoped knowledge -- memories, entities, relationships, conflicts and retrieval (`1cb1e94`, 2026-10-29)

- **Status:** completed (two Stage 7B units, landed together so that no
  deployable state has scoped conversations but shared knowledge). Design:
  `docs/stage7b_ownership_design.md` sections 2-5; progress:
  `docs/stage7b_progress.md`. One migration (0022, below); no dependency
  change. **Not applied to the real database** (still at 0019): that is an
  owner-approval boundary.
- **What:**
  - Every knowledge service takes its `KnowledgeScope` at construction,
    required and validated (`require_scope`): `MemoryService`,
    `EntityService`, `EntityResolver`, `RelationshipService`,
    `KnowledgeService` with its `ConflictDetector` and `LifecycleWriter`,
    `RetrievalService` with its `EntityMatcher`, `MemoryRetriever`,
    `RelationshipRetriever` and `evidence_map`. Request-path factories take the
    `Scope` dependency (derived only from `get_principal`); the background
    pipeline (`run_memory_extraction` -> entities -> relationships ->
    conflicts) carries the turn's frozen scope through every step and
    re-reads each memory with a scoped SELECT.
  - Writes use `scope.write_columns()` (memories, entities, aliases,
    relationships, conflict links); every read, deduplication lookup,
    resolution, conflict query, lifecycle UPDATE and retrieval query is
    filtered by `scope.clause(...)`. `get_*` use a filtered SELECT, never
    `session.get`. Memory links and relationship evidence have no scope of
    their own: they are written only between rows the scope owns
    (`KnowledgeScope.owns`) and read only joined to scoped parents.
  - Entity and relationship extraction refuse a memory the scope does not
    own; conflict detection finds nothing for one. `extract_and_store`'s
    `scope` argument is now a cross-check: a turn scope that differs from the
    service's stores nothing.
  - Each scope has its own "User" entity (the implicit subject of every
    memory), created by the entity service the first time a memory of the
    scope links an entity. The migration-seeded singleton is unattributed and
    never matched.
  - **Migration `0022_scoped_uniqueness`:** the global unique indexes on
    memories `(memory_type, normalized_content)`, entities
    `(normalized_name)` and entity aliases `(normalized_alias)` -- which made
    one member's fact a "duplicate" of another's -- are each replaced by two
    partial unique indexes: `(scope_id, ...)` where `scope_id IS NOT NULL`,
    and `(...)` where `scope_id IS NULL` (legacy rows keep exactly their old
    guarantee). No data change. The downgrade refuses, changing nothing, when
    a key is shared across scopes; offline (`--sql`) downgrade is refused.
- **Consequences:** legacy (unattributed) memories, entities and
  relationships are no longer listed, retrieved, matched, deduplicated
  against or superseded until they are attributed by an approved
  operation (design section 7). Entity lists now include the scope's User
  entity, as they always included the seeded one in a real deployment.
- **Tests:** `tests/test_knowledge_scope_isolation.py` (31): every knowledge
  service refuses a non-scope; a colleague and an outsider cannot read,
  list, count, delete, follow links or evidence, or read the lifecycle of
  another scope's knowledge (rows unchanged), even with the rows already in
  the identity map; each scope sees only its own; legacy rows quarantined;
  the same fact stored once per scope, not once globally; entity resolution
  never reuses another scope's entity or alias; extraction and conflict
  evaluation refuse another scope's memory, with no model call; a
  background step never loads another scope's memory; a mismatched turn
  scope stores nothing; one member's replacement supersedes nothing of
  another's and writes no conflict link naming it; retrieval and
  `evidence_map` return nothing of another scope; over HTTP every memory,
  entity, relationship, evidence and lifecycle endpoint 404s another
  member's ids, lists show nothing, the retrieval debug endpoint leaks
  nothing, and a chat turn's prompt contains none of it; each scope gets its
  own User entity and the legacy one is never offered; a model alias "User"
  cannot capture it and a model-proposed "User" reuses it; a turn refuses a
  context service of another scope; the lifecycle writer refuses a decision
  naming another scope's rows; the history API refuses a non-Admin.
  Migration 0022:
  SQLite (`tests/test_migration_compatibility.py`, 6) and gated PostgreSQL
  (`tests/test_scoped_uniqueness_migration_pg.py`, 6). Structure
  (`tests/security/test_knowledge_scope_structure.py`): the per-scope
  indexes pinned in models and migration; four new approved scope writers; a
  new statement-level AST checker -- every select/update/delete of a
  knowledge model or child table must carry `scope.clause` in the same
  statement, with two reviewed child-table exemptions -- and no
  `session.get` of a knowledge row anywhere. 0021's PostgreSQL tests now
  target 0021 itself; the approved-migration regression tests moved to the
  new head. Mechanical: 26 test files pass `ADMIN_SCOPE` to the services
  and write it on raw rows; four entity-count assertions include the
  scope's User; one relationship-skip test now uses a memory with no
  entities.
- **Mutation check:** nine defects, each caught by the behaviour tests and
  (all but the `owns` check, which is not a query) by the structure checker.
  Defects: `get_memory` unscoped (4 failed),
  exact dedup unscoped (1), resolver name lookup unscoped (1),
  `evidence_map` unscoped (2), conflict candidates unscoped (1, after the
  test was strengthened to check conflict links and the checker's reviewed
  exemption narrowed to child-table statements), entity extraction trusting
  any memory (2), keyword retrieval unscoped (5), linked memories filtered
  on the entity only (1), `count_memories` unscoped (4); after the review
  fixes the resolver, conflict-detector and link mutants are caught by
  direct tests under the defence in depth; sources restored byte for byte.
- **Verification (SQLite + disposable PostgreSQL 16, 2026-10-29, after the
  review fixes):** full suite ordered **7238 passed, 1 xfailed, 0 failed, 0
  skipped**; shuffled (seed 1418306627) **7238 passed, 1 xfailed, 0
  failed**. The PostgreSQL migration modules for 0021 and 0022: 23 passed.
  `tests/security/test_secrets.py` excluded as before.
- **Independent review** (the `security-reviewer` agent, read-only): no
  cross-scope read, write, supersede, link or enumeration path found in the
  code units 5-6 cover. Fixed:
  - **M1** (Medium, latent): history import's archives, runs and duplicate
    check are still global (unit 9), while its memories now go to the
    caller's scope -- the whole `/api/history` router is Admin-only until
    unit 9 (a member gets 403; tested);
  - **M2** (Medium): the structure checker missed `select_from`/`join`
    shapes, compound-statement headers, a clause on the wrong model, and
    qualified `session.get` -- rewritten to require `clause(<Model>)` for
    every scoped model a query statement names, wherever it is named, with
    negative cases for each shape;
  - **L1** `ChatService` refuses a context service of another scope
    (`ContextService.scope`);
  - **L2** `LifecycleWriter` refuses a decision naming any memory or
    relationship outside its scope, before writing a link;
  - **L4** the scope's User is looked up by exact scoped name (not the
    resolver, whose alias step could let another entity stand in), is
    provisioned before candidates are stored, and "user" is a reserved
    alias;
  - **L5** a mechanical test edit that had put the scope inside a comment;
  - **L6** a new log line now records the exception class only.
  Deferred, with reason: **L3** `Entity.aliases` and `Relationship.evidence`
  load without a scope filter; safe while aliases and evidence are written
  only within one scope (all writers check), made structural by unit 10's
  same-scope composite foreign keys (design section 4) -- the legacy
  attribution operation must not run before them. Noted: the background
  scope is frozen at the route and not re-checked against membership (unit
  7); attributing legacy rows later must merge, not plain-UPDATE, because
  of the per-scope unique indexes; 0022 takes ordinary (non-concurrent)
  index locks, so it runs with the application stopped.

### Stage 7B unit 4: scoped conversations, messages and extraction context (`6ecb048`, 2026-10-29)

- **Status:** completed (a Stage 7B unit). Design:
  `docs/stage7b_ownership_design.md`; progress: `docs/stage7b_progress.md`.
  No migration, no dependency change.
- **What:**
  - `KnowledgeScope.clause(model)` (`app/identity/scope.py`): the read filter
    on all three scope columns, personal visibility only. With
    `write_columns()` it is the only way application code names the scope
    columns.
  - `ConversationService(session, scope)`: the scope is required and must be
    a `KnowledgeScope`. Create writes the scope; get, list, count, rename and
    delete filter by it (get uses a filtered SELECT, never `session.get`,
    which would bypass the filter through the identity map). Messages are
    read and written only after their conversation is found in the scope.
    A conversation of another member, another organisation, or an
    unattributed legacy row is "not found", exactly like a missing id.
  - Request path: `get_knowledge_scope` derives the scope only from
    `get_principal` (which re-reads the active membership on every request);
    the conversation, context, intent and chat services are built with it.
    `ChatService`, `ContextService` and `IntentService` no longer build an
    unscoped conversation service by default.
  - Telegram's Admin chat no longer takes the bearer-dependent `Chat`
    dependency: it builds the turn with `build_chat_service` from the Admin
    principal it has just loaded from the database (the same composition as
    the dependency chain).
  - Memory extraction: `run_memory_extraction` and `extract_and_store` take
    the turn's scope (a frozen value from the route). The earlier-turn
    context is read only if the source message belongs to a conversation of
    that scope. A model-named `source_message_id` is now trusted only if it
    is a message of the turn's own conversation (before, any existing
    message id anywhere was accepted -- model output could point a memory's
    provenance at another conversation).
- **Not yet scoped (units 5-6, by design):** memories, entities, aliases,
  relationships, evidence, conflicts, retrieval and context assembly. New
  memories are still written unattributed. **Do not deploy** between unit 4
  and the completion of units 5-6: memory writes trigger conflict
  detection and supersession across all memories.
- **Consequence (design section 7, review F4):** existing (legacy,
  unattributed) conversations -- the Admin's web conversations and the
  Telegram ones -- are no longer listed or reachable until they are
  attributed by an explicit, owner-approved operation. A Telegram message in
  such a conversation gets the generic reply and runs no model call.
- **Tests:** `tests/test_conversation_scope.py` (17): the scope written on
  create; a colleague and an outsider refused get, messages, list, count,
  rename, add-message and delete (and the row unchanged); a conversation
  already in the identity map still refused; a legacy conversation
  quarantined; each scope sees only its own; the extraction context empty
  outside the scope; the API 404s another member's conversation and does not
  list it; a model-named foreign source message not trusted; over HTTP,
  rename, read and post-message refused with no model call, and the context
  preview and the context/prompt debug endpoints read no foreign history; a
  turn whose scope differs from its chat service refused before anything
  runs; extraction stores nothing for a turn outside its scope; a Telegram
  message in an unattributed conversation gets the generic reply and runs
  no model call. `tests/test_memory_recall.py`: a source message from
  another conversation now refuses extraction outright (no model call)
  instead of extracting without context.
  `tests/scopes.py` holds the shared trusted test scopes (made with
  `KnowledgeScope.personal`). The scope tripwire now allows `write_columns`
  only in an approved writer list (the conversation service), checks the
  list is not stale, and forbids `session.get` of conversations or messages
  anywhere in the application. A new AST checker (review F2) requires every
  function that selects, updates or deletes conversations or messages to
  bind the query to a scope (`clause`, `_in_scope` or a prior
  `_require_conversation`), with one reviewed child query. 35 test files updated mechanically to pass the Admin
  scope; one test that built an intent service with a session and no scope
  now passes the scope (it failed closed, as intended).
- **Mutation check:** four defects each caught -- get unscoped (6 tests
  failed), add-message without the parent check (2), the extraction context
  unscoped (1), any message id trusted (1); sources restored byte for byte.
- **Verification (SQLite + disposable PostgreSQL 16, 2026-10-29), unit 4
  alone in a clean worktree at `2d2e41e`:** full suite ordered **7165
  passed, 1 xfailed, 0 failed, 0 skipped**; shuffled (seed 2093554871)
  **7165 passed, 1 xfailed, 0 failed**. `tests/security/test_secrets.py` excluded as before. (A
  first run after the review fixes failed one test, the foreign-source-message
  recall test, which expected the old weaker behaviour; it was updated.)
- **Independent review** (the `security-reviewer` agent, read-only): no
  cross-scope read or write path found. Fixed: F1 (Medium) the turn's scope
  is now checked against the chat service's scope, and extraction refuses
  before any model call unless the conversation and source message are in the
  scope; F2 (Medium) the AST query checker above; F3 context-preview and the
  debug endpoints covered by tests; F5 the Telegram path tested; F6 shared
  conversation service between chat, context and intent; F7
  `touch_conversation` and `maybe_autotitle` also require the conversation
  in scope. F4 (by design, tested): see the consequence above.

### Stage 7B L0: tenant-scope foundation (`e1e8b55`, 2026-10-29)

- **Status:** completed (a Stage 7B unit, not the whole stage). Implemented
  2026-10-28, remediated and committed 2026-10-29. Approved by the owner as
  the first unit of Stage 7B (Multi-Tenant Isolation & Permissions; named 7B
  to avoid the existing "Stages 2A-3A"). Planned from the Stage 7B Phase 1
  audit (given in the session, not in the repository).
- **What:** `app/identity/scope.py` `KnowledgeScope` -- frozen, validated
  (UUIDs only, no nil UUID, personal != organisation id, shared ==
  organisation id), to be made only through `KnowledgeScope.personal` /
  `.organisation` from a `Principal`. *(Corrected 2026-10-29: this entry
  first said "derived only from an authenticated Principal". The code does
  not enforce that -- the constructor is public, `dataclasses.replace`
  works, and `Principal` is a plain dataclass anyone can construct -- so
  it is a repository rule, now checked by AST (review M1, below), not an
  unforgeable property.)* `write_columns()` refuses the reserved
  organisation scope (personal is the only writable visibility). `app/database/models/scope.py`
  `KnowledgeScopeColumns` adds nullable `organisation_id`, `scope_id`
  (indexed) and `scope_visibility` to ten tables -- `conversations`,
  `memories`, `entities`, `entity_aliases`, `relationships`,
  `knowledge_conflicts`, `reminders`, `executions`, `workflows`,
  `imported_archives` -- with one NULL-safe CHECK each
  (`ck_<table>_knowledge_scope_valid`). **No default, no server default,
  no foreign key, not NOT NULL**, and nothing writes or filters on a scope:
  no behaviour change, no employee access, no shared-memory writes.
- **Migration `0021_knowledge_scope.py`** (down 0020): adds the columns,
  CHECKs and indexes (SQLite: column-level CHECK as in 0017, no table
  rebuild). **Backfill:** `organisation_id` = the default organisation on
  every existing row; **no scope is backfilled** -- legacy rows stay
  unattributed, because pre-7A Telegram content may not be the Admin's and
  the Telegram conversation cannot be identified without reading
  configuration. Refuses if the default organisation is missing.
  **Downgrade** refuses, changing nothing, if any row has a scope or
  belongs to another organisation; otherwise it drops only the
  reproducible backfill. The global unique indexes are unchanged (scoped
  uniqueness needs NOT NULL scopes first).
- **Migration pins centralised:** `tests/security/approved_migrations.py`
  lists every migration; the five per-stage pins (6J, 6K, 6L, 6M.2, 6M.4)
  keep their windows and exactness against it, and the new
  `test_approved_migrations.py` requires the whole directory to equal the
  list and the revision chain to be linear (stricter than before).
- **Verification actually performed (SQLite, 2026-10-28):**
  - focused L0 and pin tests: 176 passed, 1 skipped (PostgreSQL chain);
  - full suite ordered: **7029 passed, 28 skipped, 1 xfailed, 0 failed**;
    shuffled (scratch `mai_shuffle` plugin, seed 1611307884, reordering
    confirmed): **7029 passed, 28 skipped, 1 xfailed, 0 failed**.
    `tests/security/test_secrets.py` excluded (it would read
    `backend/.env`); all 28 skips are PostgreSQL-gated;
  - mutation harness (scratch copy of `backend/`, every file restored byte
    for byte): two controls survived; eight mutants killed -- no downgrade
    refusal, no organisation backfill, no default-organisation check, a
    default on a scope column, the CHECK without its NOT NULL guard, the
    organisation scope writable, a personal scope equal to the
    organisation id, an unlisted migration.
- **PostgreSQL verification (2026-10-28; prior scratch evidence, not a
  repository test).** Owner-approved disposable container: the existing
  `postgres:16-alpine` image (PostgreSQL 16.15), `--rm`, tmpfs data,
  published on `127.0.0.1:55432` only, label `mai.purpose=pgtest`, a
  restricted `mai_tester` role (LOGIN CREATEDB, not superuser), fresh
  credentials in mode-600 scratch files. Results:
  - the existing `tests/test_identity_migration_pg.py`: **5 passed** (its
    0019 -> head round trip now passes through `0021`, on empty tables);
  - a scratch runner (repository migrations through the `alembic`
    subprocess, real model metadata, one throwaway database per case,
    synthetic rows in all ten tables): **12 of 12 passed** -- upgrade
    0020 -> 0021; exactly the scope columns, CHECKs and non-unique indexes
    added, nothing removed; the CHECK matrix (10 cases x 10 tables); every
    row preserved; organisation-only backfill; refusal without the default
    organisation with no partial change; a planted name collision at the
    ninth table rolled back every table already altered; downgrade refused
    for a scoped row (each table, plus an organisation-scoped row) and for
    another organisation (each table), changing nothing; clean downgrade
    to the exact 0020 schema and data; re-upgrade to the identical schema;
    schema equal to the ORM's scope definitions.
  - Teardown verified: container stopped by exact ID and removed, no
    `pgtest` container left, port free, credentials and runner deleted;
    `mai-db` and `mai-backend` unchanged (same IDs and start times, 0
    restarts). Alembic's comparison also reported four differences
    unrelated to `0021` (Known defect 12).
- **Independent security review (2026-10-28): APPROVE WITH CONDITIONS.**
  No blocker. High: H1, the backend container's start-up `alembic upgrade
  head` over the mounted working tree would apply `0021` to the real
  database on any restart (operational; needs a separate owner decision,
  not changed here). Medium: M1 (scope/principal construction not
  enforced), M2 (no foreign keys binding a scope to a membership --
  deferred to the enforcement migration), M3 (migration discovery missed
  files not matching `0*.py`), M4 (no permanent PostgreSQL test). Low:
  L1 (string-keyed scope writes not caught by the AST tripwire), L2
  (`alembic upgrade --sql` fails at `0021`: `bind.execute()` returns
  `None` offline), L3/L4 (SQLite-only representation and transactional
  caveats), L5 (this entry overstated construction and omitted the
  PostgreSQL results), L6 (post-`0021` rows have a NULL organisation).
- **Review remediation (2026-10-29; owner-approved scope: M1, M3, M4, L5,
  Known defect 12).**
  - **M1:** `tests/security/test_scope_construction_structure.py`. Outside
    `app/identity/scope.py`, `KnowledgeScope` may be named only as a type,
    a plain import, or the receiver of `.personal` / `.organisation`;
    outside `app/identity/principal.py` and `app/identity/service.py`,
    `Principal` only as a type or plain import. Anywhere in `app/`: no
    `dataclasses.replace` / `copy.replace` / `__replace__`, `__new__`,
    `__setattr__`, `__dict__` (except the log formatter) or `vars()`,
    `type(x)(...)`, `x.__class__(...)`, re-run `x.__init__(...)` (only
    `super()`), dynamic import of `dataclasses`/`copy`, or `getattr` of
    those names. The checker is itself tested against 29 bypasses
    (aliasing, rebinding, `partial`, subclassing, shadowing, string
    lookup, star import, in-place re-init, ...) and 5 intended uses, and
    each real construction site is shown to be refused if moved. Plus two
    import-direction tests: the scope-columns module imports nothing from
    `app`, and no model imports `app.identity.scope`. These are
    repository rules, not unforgeability.
  - **M3:** `tests/security/approved_migrations.py` now discovers every
    file Alembic loads (every `*.py` except `__init__*` / `.#*`, checked
    against Alembic's own pattern; the repository sets no `sourceless`,
    `recursive_version_locations` or `version_locations`, checked), and
    `chain_problems()` checks Alembic's own `ScriptDirectory`: one head
    (`0021`), one base, a walk equal to the approved list, no merges,
    branch labels or dependencies. Regression tests on a temporary copy
    of the versions directory: a hex-named migration (which the old
    `0*.py` glob missed) fails discovery, both pin windows and the graph;
    reordered `down_revision`s, an unexpected revision id and a branch
    fail the graph; an `__init__.py` is ignored. The five pins are
    unchanged and now stricter through the shared discovery.
  - **M4:** `tests/test_knowledge_scope_migration_pg.py`, gated on
    `TEST_POSTGRES_URL` only (never `.env`, never the application URL,
    no fallback). Fail-closed guard: not PostgreSQL, the `mai` database,
    a name without "test" or not plain lower-case -> the tests **fail**,
    not skip. Each test uses its own sibling database
    (`<configured>_s0021_<random>`, needs `CREATEDB`), dropped afterwards.
    Nine tests: the exact schema delta, row preservation and
    organisation-only backfill, the CHECK matrix on all ten tables,
    refusal without the default organisation, mid-migration rollback,
    downgrade refusals (scoped / other organisation, every table), clean
    downgrade and exact re-upgrade, ORM agreement (including Alembic's
    comparison, restricted to scope objects). **Not run against
    PostgreSQL** (no approved environment in this task).
  - **L5:** this entry, `AGENTS.md` and the `app/identity/scope.py`
    docstring corrected; PostgreSQL results recorded above.
  - **Verification of the remediation (SQLite, 2026-10-29; no
    PostgreSQL, no Docker):**
    - focused (scope, construction, approved-migration, migration
      compatibility, identity structure and the five pins): **245 tests,
      230 passed, 15 skipped (all PostgreSQL-gated), 0 failed**;
    - with `TEST_POSTGRES_URL` unset, the new module's 8 guard tests pass
      and its 9 PostgreSQL tests skip; set to a URL naming `mai` (port 1,
      nothing listening), all 9 **fail** at the guard before connecting;
    - full suite on the final code, ordered: **7083 passed, 37 skipped,
      1 xfailed, 0 failed** (7121 tests); shuffled (scratch `mai_shuffle`,
      seed 319435001, reordering confirmed): the same counts.
      `tests/security/test_secrets.py` excluded as before; all 37 skips
      are PostgreSQL-gated. (+63 tests over L0: 38 construction, 8
      approved-migration, 17 in the new PostgreSQL module.)
- **Closing L0 (2026-10-29, second session; the owner's autonomous
  Stage 7B directive, which authorises disposable test databases and
  local commits at verified milestones).**
  - **L2:** `0021` now refuses offline (`--sql`) mode in both directions
    with its own message ("0021 refused: offline ..."): both safety checks
    read the database, and a generated script would silently skip them.
    Before, it crashed with `AttributeError` on `bind.execute()` returning
    None. Tests in `tests/test_migration_compatibility.py`: the refusal for
    upgrade and downgrade (failed before the fix, pass after), and offline
    rendering of `0019:0020` still works.
  - **L1:** the "nothing writes a scope yet" tripwire
    (`tests/security/test_knowledge_scope_structure.py`) now also flags any
    string constant -- including f-string parts and raw SQL -- that names
    `scope_id`, `scope_visibility` or `write_columns` as a whole word, so
    `values(**{"scope_id": ...})`, `setattr(row, "scope_id", ...)`,
    `table.c["scope_visibility"]`, `text("UPDATE ... SET scope_id ...")`
    and `getattr(scope, "write_columns")` are caught. The checker is tested
    on 10 bypasses and 4 innocent forms; a name assembled at run time
    (`"scope" + "_id"`) remains out of reach of any static check.
  - **M4, run live:** a disposable PostgreSQL 16.15 server (the local
    `postgres:16-alpine` image, `--rm`, tmpfs data, `127.0.0.1:55432` only,
    label `mai.purpose=pgtest`, a `mai_tester` role with LOGIN and
    CREATEDB, not superuser; credentials generated into mode-600 scratch
    files, never printed; absent from every test log, checked). All
    PostgreSQL-gated modules: **72 passed, 0 skipped** -- the new
    `test_knowledge_scope_migration_pg.py` (17), `test_identity_migration_pg.py`
    (5), `test_identity_races_pg.py` (5), `test_monitor_races_pg.py` (17)
    and `test_migration_compatibility.py` (28, including the PostgreSQL
    chain test).
  - **Full suite with PostgreSQL enabled** (`test_secrets.py` excluded as
    before): ordered **7137 passed, 1 xfailed, 0 failed, 0 skipped**;
    shuffled (scratch `mai_shuffle`, seed 1724165901) **7137 passed,
    1 xfailed, 1 error**. The error was a *teardown* in
    `test_monitor_races_pg.py::test_grants_are_released_on_completion_and_on_block`:
    its `DROP DATABASE ... WITH (FORCE)` was refused ("permission denied to
    terminate process"), because the restricted role may not terminate a
    backend of another role -- most likely an autovacuum worker on the
    throwaway database (inferred; not logged). The test itself passed.
    Environment fix: `GRANT pg_signal_backend TO mai_tester` (still not
    superuser); the PostgreSQL modules then passed twice more, shuffled
    (seeds 1724165902 and 1724165903): 44 passed each. Recorded in
    `AGENTS.md` section 7 as a test-server requirement.
  - Teardown of the server is recorded in the checkpoint
    (`docs/stage7b_progress.md`).
- **Not done / outstanding:** H1 (start-up migration over the mounted
  tree) needs a separate plan and the owner's approval; M2 belongs to the
  enforcement migration. **Decisions needed:** how unattributed legacy rows
  are attributed or quarantined (the directive's policy: ambiguous
  historical data stays quarantined until safely classified; a quarantine
  marker would be a schema change); whether rows created after `0021` but
  before the scoped services land are attributed the same way.

### Stage 7A: Organisation & User Identity (`8f3ef2f`, 2026-10-26)

- **Status:** committed locally as
  `8f3ef2f1da5246226db76453549acbb5a44ca12b` (parent `b7b6680`; 42 paths,
  8271 insertions, 68 deletions), not pushed, **not deployed**. Implemented
  2026-10-20/21; this record was added in a separate `docs:` commit.
  Approved by the owner; built by the lead and four agents (implementers A,
  B, C and a verifier) under the `CLAUDE.md` multi-agent procedure, each
  card's diff reviewed by the read-only security reviewer, then the
  integrated whole. Design: `stage7a-design-v2.md` with its v2.1 amendments,
  kept only in the session scratchpad and **not in the repository**; on
  2026-10-26 that copy (with the task cards, patches and saved test logs)
  was found deleted from the scratchpad. The implemented contract is
  recorded in `AGENTS.md` and in this entry; the test results below are as
  recorded from the command output at the time.
- **What:** organisations, users, members (`admin` | `member`; `invited`
  | `active` | `revoked`), linked Telegram identities and single-use link
  codes (`app/identity/`, migration `0020`). One default organisation;
  its Admin member **is** `CONFIGURED_OWNER_ID`, so every existing
  owner-scoped row already belongs to that Admin and nothing is rewritten.
  The API token keeps working for an **active** Admin.
- **Seam:** `deps.get_owner_id` is async and checks, in the database on
  every request, that the member is active in an active organisation
  (403 `membership_inactive`; a failed check is 503
  `membership_unavailable`; pending work on the session is 500
  `seam_invariant`). `get_principal` re-reads membership;
  `require_admin` gives 403 `admin_required`.
- **Routes (approved):** `GET /api/organisation`, `GET
  /api/organisation/members`, and three POSTs -- invite, issue a link
  code, revoke -- all Admin-only. Code-bearing responses are `no-store`
  and commit before answering. No unlink, delete or create-Admin route.
- **Link codes:** 16 characters from a 32-symbol alphabet (80 bits),
  SHA-256 at rest, 600 s, single use, atomic redemption (member, then
  code, locked; identity inserted in a savepoint). Never issued to or
  redeemed for an Admin, nor from the Admin's own Telegram chat id.
- **Revocation:** one transaction -- member revoked, open codes
  invalidated, identity detached, every unfinished task cancelled through
  the existing locked `MonitorRequestService.cancel` with the new closed
  reason `member_revoked`, every live grant revoked, postconditions
  counted; any failure rolls all of it back.
- **Telegram:** group, supergroup and channel updates are ignored,
  including the Admin's; a private message whose first token begins with
  `/link` is a link command and never a chat turn; the only turn path is
  the Admin's configured private chat (sender with no linked identity,
  Admin active). Behaviour changes for existing callers: a non-allowed
  chat now gets 200 `ignored` instead of 403; deduplication runs before
  every branch; forwards and bot messages are ignored. No employee turns,
  memory reads or memory writes.
- **Known defect 11 fixed** (see Known defects).
- **`AGENTS.md`** sections 2, 3, 4.5, 4.7 and 7 describe the above.
- **Approved test changes** (each deliberate, none weakened): five
  migration-list pins accept `0020`; the seam signature and order pins;
  both `MonitorRequestService` pins (construction sites, cancel reason);
  the synchronous seam call in `test_authentication.py`; the Telegram
  adapter payloads and the 403 -> 200 case; Known defect 11. New pins:
  the revocation factory's binding, import directions, construction
  sites, no-secret logging.
- **SQLite verification (run by the lead, 2026-10-21, on the final
  code):** full suite ordered **6974 passed, 28 skipped, 1 xfailed, 0
  failed**; shuffled (scratch `mai_shuffle` plugin, seed 728978407,
  reordering confirmed by comparing collection order) **6974 passed, 28
  skipped, 1 xfailed, 0 failed**. An earlier pair on the code before the
  last route hardening (shuffle seed 1397224745) gave 6972 passed, 28
  skipped, 1 xfailed, 0 failed.
  `tests/security/test_secrets.py` was excluded deliberately (the main
  checkout has a `backend/.env`, which may not be read). All 28 skips are
  PostgreSQL-gated: `test_monitor_races_pg.py` (17),
  `test_identity_races_pg.py` (5), `test_identity_migration_pg.py` (5),
  `test_the_chain_runs_on_postgresql` (1). Mutation harnesses with
  surviving controls on every card killed every real mutant (A 25 real
  of 29, B 31 revocation and routes plus 22 in the last route round, C 27,
  verifier 8 named mutations including the three Known defect 11
  mutations; each file restored byte for byte).
- **PostgreSQL verification (run by the lead, 2026-10-21, each step
  separately approved by the owner)** on a disposable PostgreSQL 16.15
  container (`mai-pgtest`, image `postgres:16-alpine`
  `sha256:cf78e766...0685`, bound only to `127.0.0.1:55432`, `tmpfs`
  data, auto-remove, restricted role `mai_tester` with `LOGIN CREATEDB`
  and not superuser; passwords only in `0600` scratch files, never
  printed):
  - **Migration:** `tests/test_identity_migration_pg.py` **5 passed** on a
    throwaway `mai_chain_test` database -- upgrade -> downgrade to `0019`
    -> upgrade with the seed present exactly once and only 0020's tables
    removed by the downgrade; every CHECK refuses an invalid row; attached
    identities unique, a detached one not counted; both partial unique
    indexes carry `WHERE (detached_at IS NULL)`; constraint names equal
    the models'. An independent read afterwards: `alembic_version` `0020`,
    seed 1/1/1, the seeded member `...a1a1` admin and active.
  - **Concurrency:** `tests/test_identity_races_pg.py` **5 passed** and
    `tests/test_monitor_races_pg.py` **17 passed** (22 passed, 0 failed).
    Each test created and dropped its own `mai_pgtest_*` database; none
    was left, and `mai_chain_test` was unchanged.
  - **Not run on PostgreSQL:** the older upgrade-only
    `test_migration_compatibility.py::test_the_chain_runs_on_postgresql`
    (its variable was set only for the identity migration file, whose
    first test performs the same full chain from empty).
  - **Teardown (2026-10-22):** the container was stopped by its exact id
    and auto-removed (verified absent, no container with its label, port
    free); the scratch credential directory was deleted. The real `mai`
    database and the `mai-db` and `mai-backend` containers were never
    contacted (ids and start times unchanged).
- **Security review:** the read-only reviewer reviewed the design twice
  (v1: three blockers; v2: two textual blockers, both fixed), the lead
  foundation, every card's diff and each follow-up, and the integrated
  whole. No blocker or should-fix finding remains open; its commit gate
  (PostgreSQL verification) is satisfied. The remaining notes are
  recorded as limitations below. This is a review of the stated
  properties, not a proof of every security property.
- **Not done:** no push, no deployment, and migration `0020` has not been
  applied to the real `mai` database (the backend container runs
  `alembic upgrade head` at start-up, so starting it with this code would
  apply it). Stage 7A does **not** make member Telegram turns safe and does
  not enable them: they remain disabled until tenant-aware memory, an unlink
  route and the membership re-check before a turn exist.
- **Limitations (stated, not fixed):** tenant isolation is not claimed --
  conversations, memories, entities, knowledge, reminders and executions
  remain one global store; the background runtime and automatic delivery
  do not check membership; revocation cannot reach reminders, executions
  or execution approvals (no owner column), cannot revoke a member with
  more than 500 unfinished tasks (fails closed), and does not stop a
  request already past the seam; a monitor's grant released by its cancel
  is audited `monitor_ended`, not `member_revoked`; the Telegram failure-
  reply cap (20 per minute) and the update deduplicator are per process;
  `/link` forms that are not the first token (or hidden by non-Cf
  characters) from the Admin's own chat are chat text; every
  authenticated route now needs the database; organisation-route refusals
  carry their reason in `message`, not `code`; no unlink route exists yet
  (required before member turns are enabled); SQL parameters -- for
  example a display name, a code hash or a Telegram id -- can still reach
  logs through SQLAlchemy's or the driver's own logging at INFO or DEBUG
  (the deferred `hide_parameters` change).
- **Route hardening:** database errors while issuing or committing a code
  are 503 `database_error`, rolled back and logged by class name only,
  outside any `except` block; no code is returned.

### Fix: link only the selected tab to its panel (commit titled `fix: link only the selected tab to its panel`, 2026-10-20)

- **Status:** committed locally, not pushed, **not deployed**. Not a stage.
  Frontend only: `frontend/components/Tabs.tsx` and
  `frontend/tests/accessibility.test.ts`. No backend, route, migration or
  dependency change. It resolves the C3 limitation "inactive tabs point
  (`aria-controls`) at unmounted panels". It was also the first task run
  through the `5c7801c` multi-agent workflow.
- **What:** only the selected tab carries `aria-controls`
  (`panelId(selected)`); inactive tabs omit it. `app/page.tsx` renders only
  the selected tab's panel, so an inactive tab's reference pointed at a
  missing element. The header comment states the component's contract:
  `Tabs` renders no panels, the caller renders the selected panel with that
  id, and the component cannot enforce what the caller renders.
- **Tests:** the existing tab test now checks that the selected tab names
  `panel-tasks`, that `panelId` is pinned to that format, and that both
  inactive tabs are present and have no `aria-controls`. A new test, run
  for every selection, renders the tab list beside a caller-style panel. It
  asserts that the selected tab's reference matches exactly one rendered
  element and that the element is the `tabpanel`. **Limitation:** the test
  builds its own panel, so it does not prove that `app/page.tsx` meets the
  contract. That is true from reading the page, not from a test.
- **Pilot history (reported by the pilot session on 2026-10-19, not
  re-run):**
  - Two implementers worked in separate worktrees, one file each. The lead
    integrated their work through the `CLAUDE.md` patch procedure.
  - On the first version: frontend 160 passed; type check and build passed;
    the four frontend-scanning backend files, 112 passed; full backend
    suite 6662 passed, 19 skipped, 1 xfailed.
  - The read-only security reviewer reported no findings.
  - About 9.5 minutes elapsed.
  - The PostgreSQL-gated race and migration tests were skipped: their test
    database variables were not set.
- **Refinements and verification (2026-10-20, on the final code):**
  - The comment was reworded and the panel-resolution test added, in the
    pilot integration worktree.
  - Frontend tests, 161 passed, 0 failed, both in that worktree and after
    applying the patch to the main checkout. The patch's SHA-256 is
    `958306ada2206143a2bd7a586ec8d0f3525726feeeb6f6de035ef674a1d0a783`.
  - `tsc --noEmit` clean in both places. `next build` passed in the
    worktree on byte-identical files; it was not run in the main checkout.
  - The four frontend-scanning backend files, 112 passed (main checkout).
  - Full backend suite 6662 passed, 19 skipped, 1 xfailed (pilot worktree,
    which has no `.env`, so `test_secrets.py` skipped).
  - Mutation check, on a scratch copy of `Tabs.tsx`, restored and
    confirmed byte-identical afterwards. Each of five mutants fails at
    least one tab test: a hard-coded panel id, the tab's own id, a dangling
    id, `aria-controls` on every tab, and a second element carrying the
    panel's id. The first and last are caught only by the new test, and the
    every-tab mutant only by the existing one.
  - The read-only security reviewer did two rounds: no security findings,
    no boundary crossings. Its two low findings in the test's tag matching
    (an unescaped dynamic `RegExp`, and tag names with digits not matched)
    were fixed. The reviewer read the first fix. The second, a
    one-character pattern change, was checked by the test run and the
    mutation check above, not by another review.
  - Not run: `npm run lint` (ESLint is not installed), a live browser or
    screen-reader check, and the PostgreSQL-gated tests (not affected).

### Multi-agent workflow configuration (`5c7801c`, 2026-10-19)

- **Status:** committed locally, not pushed. Not a stage. Tooling and
  documentation only (subject `feat: add multi-agent workflow
  configuration`, 6 files). No application code, route, migration or
  dependency change.
- **What:**
  - `.claude/agents/` adds three agents: `implementer` and `verifier`, each
    working in its own worktree, and a read-only `security-reviewer`.
  - `.claude/settings.json` adds deny and ask permission rules for git,
    `gh`, Alembic, Docker, package installs and `.env` reads. It also bases
    agent worktrees on local `HEAD`.
  - `CLAUDE.md` adds the multi-agent procedure: task cards, one owner per
    file, shared files kept by the lead, and the patch integration checks.
  - `.gitignore` ignores agent worktrees, local settings, a `node_modules`
    symlink and `*.tsbuildinfo`.
- **Verification:** the commit message says agent execution and the
  integration procedure were unverified when it was committed. The
  2026-10-19 pilot then exercised them on one frontend task. **Reported by
  the pilot session, not independently re-checked:** worktree isolation,
  file ownership checks, patch building and integration worked as intended.
  **Checked on 2026-10-20:** the pilot's worktrees were based on `5c7801c`,
  and the final integration patch contained only the two assigned files.
  The pilot also surfaced Known defect 10 (permission rules miss
  `git -C ...` forms). The rules are guard-rails, not a security boundary.

### Stage 0 C5: documentation reconciliation (`d4f8339`, 2026-10-16)

- **Status:** committed locally, not pushed. Not a numbered stage: the last
  Stage 0 consolidation checkpoint. Documentation only (subject `Stage 0
  C5: reconcile documentation with C1-C4`): this file,
  `docs/provider_integration_checklist.md`, `docs/setup.md` and
  `docs/ui_usability_audit.md`.
- **What:**
  - This file gains the C1 to C4 entries, with the uncommitted-work list
    resolved into them.
  - The provider checklist gains a per-provider verification-status table.
    It records what was observed on each date, not that a provider works
    now.
  - `setup.md` describes `/health` as reporting the provider's last
    observed request (`reachability`), not a configured key.
  - The usability audit marks statements that C2 to C4 made untrue as
    *Updated 2026-10-16*.
- **Correction (2026-10-20):** until now, this file's "Current state" said
  C5 was "awaiting review" and uncommitted. It was committed as `d4f8339`
  on 2026-10-16. Whether a separate review took place before that commit is
  not recorded.
- **Verification:** documentation only; no tests are recorded for it.

### Stage 0 C4: backend reliability, memory recall and retrieval (`4b8d407`, 2026-10-16)

- **Status:** committed locally, not pushed. Not a numbered stage: the fourth
  Stage 0 consolidation checkpoint (subject `fix: optimize and bound memory
  retrieval`, 29 files). It commits the 2026-10-13/14 provider, intent, memory
  and retrieval work, plus two fixes found while validating it on 2026-10-16.
  No migration (head still `0019`), dependency, authentication or approval
  change.
- **LLM provider reliability (2026-10-13).**
  - *Gemini timeout, root cause:* provider-side capacity for the configured
    model `gemini-3.8-flash`, not Mai. Evidence (one request each, policed
    client, no data written): listing models with the key returned 200 in
    0.34 s and included the id (key, DNS, TLS, host policy and model id all
    fine); generation returned HTTP 503 in 2.6 s and 6.2 s, and one 200 only
    after 63.7 s for a 1-token answer (just over the 60 s read timeout);
    `gemini-3.5-flash` on the same key answered in 2.2 s. Mai classified
    each case correctly. No Mai change fixes it; a model change is the
    owner's decision. Live smoke after this work: 3 x HTTP 503, failed.
  - *Contract fixes found by a shared contract matrix:* Anthropic now
    retries its documented 529 `overloaded_error`, logs every failed attempt
    in the shared shape, and reports a missing key as `llm_not_configured`
    (was `llm_auth_error`, shown to the user as "the key was rejected"). An
    empty model fails closed before any request (all providers) and makes
    `Settings.is_llm_configured` false.
  - *Smoke test:* `python -m app.llm.smoke` -- one fixed prompt through the
    configured provider, sanitised log line, exit 0/1, no data written
    (added to the two `LLMMessage` allow-lists as a reviewed site).
  - *Tests:* `tests/test_provider_contract.py` (126 cases over groq, gemini,
    anthropic_api; 9 deliberate defects each caught), 8 new health tests
    (`tests/test_health.py`, 11 in all) and 4 smoke tests. OpenAI is not
    implemented and fails closed.
  - *Checklist:* `docs/provider_integration_checklist.md`.
- **Health semantics changed (visible to operators).** `/health` never calls
  the model. Providers record the outcome of their last *real* request (an
  error code and a time, never text), and `/health` adds `llm.reachability`
  (`unverified` / `reachable` / `failing`). `llm.healthy` is true only after
  an observed success, so the top-level `status` is `degraded` after every
  start until the first model request succeeds, and again after any failed
  request (a 429 that exhausted its retries included) until the next success.
  The HTTP status stays 200, and nothing in the repository acts on `status`
  (no compose health check). The existing test
  `test_health_reports_ok_when_dependencies_are_configured` was changed
  deliberately to set an observed success first. A backend with no key also
  reports `unverified`, so "not configured" and "not yet used" are told apart
  only by `detail`. Documented in the provider checklist (step 7.3) and
  `docs/setup.md`.
- **Intent-classifier truncation handling (2026-10-13).** Found live with a
  reasoning model (`gemini-3.5-flash`).
  - *Defect:* `INTENT_CLASSIFICATION_MAX_TOKENS` was 512, and the output cap
    includes a reasoning model's hidden reasoning tokens. About 110 visible
    tokens of JSON plus roughly 250 to 650 hidden ones meant the reply stopped
    at the limit mid-field (`finish_reason=length`, a body ending at
    `"confidence": `) in 4 of 4 probes, and the cut-off text was reported as
    an unparsable reply.
  - *Change 1, the budget:* the default is now **2048** (`app/core/config.py`,
    was 512). It is a ceiling, not a target: a model that stops early costs no
    more. A test pins a floor of 1500 rather than the exact value.
  - *Change 2, truncation is detected and named:* `app/intent/classifier.py`
    checks `finish_reason` against `{"length", "max_tokens"}` (OpenAI-compatible
    APIs and Anthropic respectively) and raises
    `IntentClassificationError("truncated_response")` **before any parsing**,
    so a truncated body is never interpreted, even one that happens to look
    complete. Its warning log carries the provider, model, `finish_reason` and
    `max_tokens` only, never message or reply text (tested).
  - *Safe fallback:* `IntentService` catches that error
    (`app/intent/service.py:89-92`) and returns `policy.fallback(
    "truncated_response")`: intent `unknown`, confidence 0, ambiguity high,
    every `requires_*` flag false, `classified` false, and the reason as an
    application constant. A truncated classification therefore authorises
    nothing and cannot select planning, research or execution; the chat turn
    continues.
  - *Tests:* eight in `backend/tests/test_intent_classification.py` (a reply
    cut off by the limit is named as truncated; a truncated reply is never
    parsed even when it looks complete; the same bad body without truncation
    is an unparsable reply; a normal stop classifies as before; surrounding
    whitespace is accepted; a provider error is a provider error, not a parse
    error; truncation logs codes and never content; the budget floor).
- **Memory recall reliability (2026-10-13).**
  - *Failure:* asked "what is my ideal type of woman?" in a new
    conversation, Mai said it had no information, after earlier saying it
    had saved it. Traced on real data with ids, counts and masked text only:
    the description had been given as a short *answer* to the assistant's
    question; extraction saw the answer without the question and stored the
    traits as a description **of the user**. Retrieval found that memory and
    the prompt carried it; the model correctly found no preference in it.
    The "saved" replies were model text, written before extraction ran.
  - *Root cause:* `MemoryService.extract_and_store` never passed the
    extractor's existing `recent_context`. It now passes the turns before
    the analysed message (`MEMORY_EXTRACTION_CONTEXT_MESSAGES`, each capped
    at `MEMORY_EXTRACTION_CONTEXT_CHARS=500`), and the extraction prompt says
    an answer describes what the question was about. **Enabled at 4 by the
    owner's approval (2026-10-14)**, bounded 0-10 messages x 50-2000 chars
    (out-of-range values are refused at settings load). Stage 3D policy
    changed deliberately: `tests/security/test_provider_disclosure.py` now
    pins the bounded same-conversation window, the per-message cap, that no
    other conversation's turns and no retrieved knowledge reach extraction;
    a source message from another conversation yields no window.
  - *Contributing, fixed:* keyword retrieval missed inflections ("woman"
    vs "women") -- small deterministic variants in `query_normalizer`; the
    bounded candidate pool was filled by importance, so a memory matching
    every query word could be cut before ranking -- now ordered by matched
    keywords first; a retrieval failure looked exactly like "no memories"
    -- inner failures now reach the package as `retrieval:<source>` and the
    prompt says memory was unavailable; "I've saved that" was never true
    when said -- new `Channel.MEMORY` in the execution-truth validator
    (never licensed on a reply; one retry, then a truthful line).
  - *Tests:* `backend/tests/test_memory_recall.py` (40, one of them a strict
    xfail documenting the lexical-retrieval limit) and 4 Stage 3D disclosure
    tests; each of the six fixes and each of the four context bounds was
    removed in turn and a test failed. Channel pin updated deliberately.
- **Memory retrieval quality (2026-10-14).**
  - *Measured (synthetic baseline, isolated SQLite):* "Which tea do I
    prefer?" ranked the tea memory 9th of 9; three phrasings ranked it 4th-7th.
    Causes: substring matching ("tea" in "team", "steady", "instead");
    generic preference words weighted like the subject, so every "User
    prefers ..." memory tied and importance decided; no equivalence between
    prefer / favourite / liked; and importance (0.15) able to overturn a
    partial-relevance gap.
  - *Changes (`app/retrieval/query_normalizer.py`, `retrievers.py`,
    `ranker.py`):* whole-word matching with bounded word forms; query terms
    classed subject (1.0) / preference (0.35, one explicit family) / frame
    (0.2); only subject words generate candidates when the question has one;
    one concept group (partner ~ woman, wife, ...) that counts only beside a
    preference word; the pool prefers preference-expressing memories for a
    preference question; metadata is gated by relevance in the final score.
    Budgets, pool size and weights unchanged.
  - *Result:* all 7 benchmark phrasings rank the target first (synthetic, and
    read-only against the real database with the new code); 7 mechanisms
    each removed in turn and a test failed. `tests/test_retrieval_quality.py`
    (29). The lexical-retrieval xfail stays: "explaining" vs "explanations".
- **Found and fixed while validating C4 (2026-10-16), never committed broken.**
  - *Unbounded keyword query:* building the keyword query recomputed every
    keyword's word forms once per candidate form, quadratic in a message's
    distinct words (1,000 distinct words: 10.5 s to build, on the request
    path), and a 32,000-character message produced over 43,000 bound
    parameters, above asyncpg's 32,767. Now each keyword's forms are computed
    once (`keyword_form_groups`) and at most `MAX_QUERY_KEYWORDS = 24`
    keywords, the first distinct ones in message order, reach a query
    (`query_keywords`); the ranker scores against the same keywords. A
    question within the cap builds the identical statement. Measured after the
    fix: 308 parameters and about 11 ms to build and compile at every size up
    to 32,000 characters; adversarial maximum-length messages 215 to 546
    parameters (bound under 3,400).
  - *Debug candidate pool unbounded:* `RetrievalService.candidates_for_debug`
    returned the union of the three candidate sources (each capped at 50)
    without the overall cap that chat applies; at `7ac52b4` a probe reached
    100 candidates. One line applies the same bound in the same order, so
    `/api/retrieval/debug` shows the pool chat ranks. The existing security
    test `test_retrieval_stays_bounded_with_many_entities` is unchanged and
    passes.
  - *Tests:* `backend/tests/test_retrieval_query_bounds.py` (16). Five
    deliberate defects (the per-pair scan restored, the cap removed, the
    ranker uncapped, the cap keeping the last keywords, the debug cap
    reverted) were each caught.
- **Verification (automated, isolated worktree, final content):** full backend
  suite 6662 passed, 19 skipped, 1 strict xfail, 0 failed. Skipped: 16
  PostgreSQL race tests (`TEST_POSTGRES_ADMIN_URL` unset), 1 migration test
  (`TEST_POSTGRES_URL` unset) and `test_secrets` (no `backend/.env` in the
  worktree). After the files were written to the main working tree: query
  bounds 16, knowledge attacks 29, retrieval/memory/context 246 + 1 strict
  xfail, disclosure/injection/prompt/logging/integrity 108, all passed. The
  committed diff equals the reviewed patch byte for byte.
- **Live (recorded at the time in the session's command output, with the
  owner's approval each time; not re-run on 2026-10-16):**
  - 2026-10-13, about 20:00 to 21:05 UTC: memory `8cf36676` (the
    mis-attributed record) archived; backend restarted; one re-extraction
    stored the corrected memory `9360eae1`; synthetic cross-conversation
    recall passed with the real Groq model (`15ed39bb`, "favourite tea is
    jasmine tea").
  - 2026-10-14 09:48 UTC: backend restarted (`docker compose restart
    backend`; no migration ran). Two tea questions, each in a new
    conversation, answered from `15ed39bb` via Groq `openai/gpt-oss-120b`;
    the first attempt of one hit HTTP 429 on every retry, failed honestly,
    saved nothing, and passed about a minute later. `/health` read
    `degraded` / `unverified` until that first request.
  - 2026-10-16 (read-only, direct): `GET /health` returned `status: ok`,
    `reachability: reachable`, so a real model request had succeeded since
    the last restart.
- **Not live, not verified:**
  - The 2026-10-16 query bound and debug-pool cap are **not running**
    (inferred, not tested: the backend started before those files were
    written, has not been restarted, and runs without reload), so it is
    expected to still build the unbounded query.
  - PostgreSQL-gated tests have not run; Anthropic has never been tested
    against the real service; the ideal-partner phrasings were checked only
    read-only, not through live chat.
- **Data left in the real database:** the synthetic memory `15ed39bb` and the
  test conversations (cleanup awaits the owner's decision); the archived
  `8cf36676` is kept.
- **Limitations:** in a very long message only the first 24 keywords drive
  memory search; lexical retrieval still misses paraphrases (the strict
  xfail); concurrent conversations share Groq's rate limit.

### Stage 0 C3: frontend usability (`7ac52b4`, 2026-10-16)

- **Status:** committed locally, not pushed; **not deployed** (the running
  frontend container is the 2026-10-12 image, and on 2026-10-16 its served
  bundle had none of C3's UI strings). Not a numbered stage;
  frontend and `docs/ui_usability_audit.md` only (40 files). No backend,
  route, migration or dependency change; the `test` script in
  `package.json` gains a test-only loader.
- **What:** a Tasks view, a Monitors view (with stop), an Inbox (monitoring
  alerts plus reminders), a provider status display, integration status that
  no longer shows green for a credential nobody verified, conversation
  rename, safe Markdown for assistant replies, and worded API errors. Audit
  and findings: `docs/ui_usability_audit.md`. The Monitors view reads C2's
  `TaskRead` monitor fields (two list calls, no per-task detail request); a
  backend without them lists those tasks as "not reported". An open monitor
  journal is keyed by the task's version (id + `updated_at`), so it re-reads
  only when the task changed. Wording: "Checks run"; an overdue schedule says
  the page cannot tell whether the runner is working; `running` no longer
  says work is happening now. The provider display reads `/health`
  `reachability` when present (C4) and otherwise shows
  "Configured, not verified" or "Not available".
- **Files:** new `frontend/components/*` (TaskActivity, MonitorsView,
  InboxView, ProviderStatus, CancelControl, Markdown, Tabs, TaskParts, ui),
  new `frontend/lib/*` (tasks, monitors, inbox, cancel, integrationStatus,
  providerStatus, markdown, resource, polling, errors, format, tabs,
  useInboxBadge), seven new test files plus `tests/support/` (a test-only
  `.tsx` loader), and edits to `app/page.tsx`, `app/globals.css`,
  `components/{IntegrationsPanel,MessageList,Sidebar}.tsx`,
  `lib/{api,types}.ts` and `package.json`.
- **Verification (isolated worktree):** frontend tests 160/160 (21 before),
  type check clean, production build compiles; the backend tests that scan
  the frontend plus C2's tests, 127 passed; full backend suite 6427 passed,
  19 skipped, 0 failed. Every type matches the committed backend's OpenAPI
  schemas and enums. Earlier, during the work (2026-10-13), end to end in a
  real browser against a throwaway SQLite backend with synthetic data: login
  dialog, tabs by keyboard, task list, monitor stop (server state confirmed
  `cancelled`), alert mark-read, reminder rendering, rename, Markdown with
  hostile input (0 scripts, 0 images, 0 handlers), integration state,
  backend-down states. A mutation check then caught 24 real guard removals.
  `npm run lint` was **not run**: ESLint is not installed.
- **Limitations:** ~~inactive tabs point (`aria-controls`) at unmounted
  panels~~ (resolved by the commit titled `fix: link only the selected tab
  to its panel`; see that entry); narrow (phone) layouts were not checked;
  the sidebar keeps its fixed width.

### Stage 0 C2: monitor metadata on task reads (`30a1342`, 2026-10-16)

- **Status:** committed locally, not pushed. Not a numbered stage;
  owner-approved, read-only and additive (3 files).
- **What:** `monitor` (`kind`, `operator`, `interval_seconds` only; never
  `path` or `expected`), `next_run_at` and `check_count` are on every
  `TaskRead` (list, detail, activity, cancel), each read from the task row
  (`tasks.monitor`, `tasks.next_run_at`, `tasks.check_count`) in one helper,
  `monitoring_fields` (`app/tasks/schemas.py`); no new source, route,
  migration, service or write. Optional, so existing clients are unaffected.
  The read model is `TaskMonitorRead`: it had shared a class name with the
  chat-turn `MonitorRead`, which made FastAPI publish the chat schema as
  `app__schemas__monitor__MonitorRead`; a test pins the published names.
  Semantics worth knowing: `next_run_at` is a claim-lease horizon while a
  check is in flight and is **not cleared by cancellation**, so only `state`
  says whether anything will run; `TaskRead` already exposed `owner_id`,
  `budget` and `spent` and still does.
- **Tests:** `backend/tests/test_task_monitor_fields.py` (15). During the
  work, `test_next_run_at_has_exactly_two_writers` failed because it treats
  any keyword argument named `next_run_at` as a write; the test is unchanged
  and the reads moved into `monitoring_fields`.
- **Verification (isolated worktree):** the new file failed to import before
  the change and passed after it (15); 24 related files 1192 passed, 17
  skipped; full backend suite 6427 passed, 19 skipped, 0 failed.
- **Live:** not checked by a request (the task routes need the token). The
  backend restarted on 2026-10-14 after these files last changed, so it is
  inferred, not verified, to serve the fields.

### Stage 0 C1: frontend token-storage regression fix (`fc80592`, 2026-10-15)

- **Status:** committed locally, not pushed; **not deployed** (inferred: the
  running frontend image predates it, and C1 changes no visible text). Frontend only, one hunk in
  `frontend/lib/api.ts`.
- **Defect:** commit `a265df5` added a second
  `window.sessionStorage.getItem(TOKEN_KEY)` in `frontend/lib/api.ts`,
  breaking two backend structure tests that pin one call site per storage
  operation (found in the Stage 6Q audit; 6411 passed, 2 failed).
  `forgetTokenIf` now reads through `storedToken()`; the tests are unchanged.
- **Verification:** both tests pass; frontend 21/21, type check clean (at
  the time of the fix); full backend suite in an isolated worktree, 6412
  passed, 19 skipped, 0 failed.

### Fix: frontend authentication token flow (commit titled `Fix frontend authentication token flow`, 2026-10-13)

- **Status:** completed. Not a stage. Frontend, `docker-compose.yml` comment
  and this file only: no backend, schema or migration change; the 6M.4
  backend contract is unchanged.
- **Defect** (found deploying 6M.4 on 2026-10-12): `frontend/lib/api.ts`
  forgot the token and called `window.prompt` once per failed request, so
  the ~5 requests a page load makes opened ~5 prompts, any empty or
  cancelled one erased a token entered in another, and an embedded browser
  that dismisses `window.prompt` (the Claude desktop browser pane) could
  never sign in.
- **Fix**, keeping the `mai.apiToken` sessionStorage key and the
  `Authorization: Bearer` contract:
  - one shared token entry: every request that needs a token waits for the
    same one, through `frontend/components/AuthDialog.tsx` (mounted once in
    `app/layout.tsx`) instead of `window.prompt`;
  - a token is cleared only on `invalid_credentials` for that very token,
    and only while it is still the stored one, so a stale reply cannot erase
    a newer token; a request that failed for lack of a token retries with
    one another request stored meanwhile; `authentication_required`,
    network errors, 503 `authentication_not_configured` and other HTTP
    errors never clear it, and 503 never prompts;
  - a cancelled, empty or whitespace entry stores and erases nothing; Mai
    then stays locked, with no dialog reopening by itself, until the person
    chooses "Enter API token"; retries per request are bounded (3).
  - The stale `docker-compose.yml` comment saying the API has no
    authentication is corrected.
- **Tests:** `frontend/tests/api-auth.test.ts`, 21 tests on Node's built-in
  runner (`npm test`; no new dependency; `tsconfig.json` gains
  `allowImportingTsExtensions` for its import), driving the real client
  against a fake backend that follows the 6M.4 contract. Nine deliberate
  mutations of `lib/api.ts` (no sharing, unconditional clear, no lock check,
  clear on any 401, no newer-token reuse, cancel not locking, whitespace
  accepted, clear on network error, retry bound 50) were each caught.
- **Verification** (2026-10-13): `npm test` 21/21, `npm run typecheck`
  clean, one `docker compose build frontend` (passed; the image holds no
  token or digest and no `prompt(` call). Deployed with
  `docker compose up -d --no-deps frontend`; the backend was not restarted;
  rollback image `mai-frontend:pre-authfix-20261013`. Live: in the built-in
  browser pane with fake tokens, one dialog per page load and the empty,
  cancel, Escape, "Enter API token" and wrong-token paths as above; in
  Chrome, the owner's reloaded tab kept its session token, and a new tab
  asked once and loaded conversations, integrations and history after the
  real token. Backend still answered 401 without a token and
  `invalid_credentials` for a wrong one. Database unchanged (still `0019`;
  the only new rows are the owner's own chats). Backend tests were not
  re-run: no backend change.
- **Limitations:** Chrome may offer to save the token in its password
  manager (the field is a password field); declining keeps it session-only.
  While Mai is locked the Integrations panel stays on "Checking…" (it
  ignores errors; unchanged). `npm test` prints a harmless Node module-type
  warning.

### Stage 6P: Harden monitoring concurrency and truthfulness (commit titled `Stage 6P: harden monitoring concurrency and truthfulness`, 2026-10-12)

- **Status:** completed, verified, committed. Parent `94ea032` (6N). It is the
  commit that adds `docs/STAGE_6O_AUDIT.md`.
- **Purpose:** fix the three medium findings of the Stage 6O audit
  (`docs/STAGE_6O_AUDIT.md`, verdict SAFE WITH LIMITATIONS), the related low
  findings L1/L2, Known defect 8 generally and Known defect 9, and make the
  critical PostgreSQL races repeatable tests. No new capability.
- **Root causes:**
  - **M1 / Known defect 8:** every lifecycle write read the task, then wrote
    by id alone. A stale copy -- read before another transaction moved the
    task -- could overwrite newer state. Terminal states were already
    protected by the CHECK constraints (6O S1-S3), but a non-terminal one was
    not: a replacement's cancel overwrote a just-confirmed QUEUED monitor
    after the user was told "Done" (6O S4).
  - **M2:** `release_monitor_grants` was called only on cancel and expiry.
  - **M3:** the execution-truth validator had no monitoring channel.
  - **L1:** a failed transition expired the ORM copy, and the next access
    lazy-loaded it (`MissingGreenlet`), aborting the pass.
  - **L2:** the runner's check claim had no state condition.
- **Fixes:**
  - **Compare-and-set** (`TaskService`): `transition` and `authorize_plan`
    write with `UPDATE ... WHERE state = <the state decided from>` (and, for
    authorisation, `authorized_at IS NULL`). A write that matches nothing
    raises `_StaleState` inside its savepoint, writes nothing, and returns
    `state_changed_concurrently` with the fresh state from the new
    `TaskService.refresh` (`populate_existing`); it is never reported as
    success. Successful writes set the in-memory copy with
    `set_committed_value`, and the persistence-failure paths re-read the row,
    so no caller touches an expired copy (L1).
  - **Runner:** `mark_task_running` is conditional on `queued` and refreshes
    the copy when it loses; the check claim (`_claim_check`) is conditional on
    `queued`/`running`, and a lost claim on an ended task is refused as
    `task_not_active` rather than read as contention (L2).
  - **Runtime:** a task that stopped being active after its claim is left as
    it is (no step, no read, no write, no error log). Reschedules go through
    `_schedule_if_active` (conditional on an active state); clearing a
    schedule is unconditional and always safe. `_record_failure` returns for
    an ended task, writes the count and next run only while the task is
    active and unchanged, and rolls everything back -- no count, no block, no
    `monitoring_failed` notification -- if the block loses to a concurrent
    change. Expiry journals `background_unscheduled` only when it is the
    transition that ended the task.
  - **Confirmation vs everything else (M1):** a proposal is withdrawn only
    while still planned and unauthorised (`_discard_by_id`, then the
    compare-and-set), so a concurrent confirmation always wins. A turn that
    lost the race says what happened, application-written: a decline or a
    changed answer gets "That proposal had already been confirmed, so that
    monitor is active..." (`already_monitoring`); a replacement keeps the
    active monitor, proposes the new one and says "Your earlier proposal had
    just been confirmed, so that monitor is active." instead of claiming a
    cancellation; a confirmation that lost gets "That proposal is no longer
    waiting for an answer ... so nothing was started or changed." (new
    outcome `not_pending`) instead of falling through to the model.
  - **Grants (M2):** released in the same transaction as the state change
    on completion (runtime, after `condition_met`) and on block
    (`_record_failure`, only if the block transition won), as well as on
    cancel and expiry -- unless another active task still needs the
    capability. Owner-scoped, capability-wide, unchanged otherwise.
  - **Truthfulness (M3):** `Channel.MONITOR` in
    `app/synthesis/execution_truth.py`. Claims that monitoring started, is
    active, was scheduled, checked Gmail, found a match or sent a
    notification are detected per clause, including promises ("I'll let you
    know when it arrives"), which are judged by their own narrower exemption
    (negations, conditions on the user) rather than the shared offer filter.
    Only a `scheduled` `MonitorResult` with its task id licenses a claim,
    which never occurs on a model-written turn, so every such claim on a
    model reply is refused; the corrective reply is true whatever monitors
    already exist. The execution note lists Gmail monitoring with the other
    channels. Web, mail and calendar behave as before.
  - **Known defect 9:** `start_conversation_with_message` confirmed
    unreachable and removed (with its now-unused `Conversation` import).
- **No migration, no schema change, no CHECK constraint touched, no new
  service, runner, runtime, registry, writer, egress or auth seam, no
  frontend change.** The 40-check budget, 15-minute default and 7-day
  maximum are unchanged.
- **Existing pins changed (deliberately; none loosened):** the execution
  note now has four channels and `known_claim_channels` includes `MONITOR`;
  `record_for_turn` takes `monitor` (two pins); a confirmation that loses
  the race now reports `already_monitoring` instead of falling through; the
  6N propose-path pin lets `_discard_by_id` *read* `authorized_at` (and now
  pins that it never writes it) and covers the two new helpers; the 6N
  reschedule-clamp pin follows the new `_schedule_if_active` calls instead
  of passing vacuously.
- **Verification:**
  - New tests: `tests/test_monitor_hardening.py` 22,
    `tests/test_monitor_truthfulness.py` 67,
    `tests/test_monitor_races_pg.py` 17 (PostgreSQL, skipped without
    `TEST_POSTGRES_ADMIN_URL`), `tests/security/test_task_state_cas_security.py`
    8.
  - **PostgreSQL race suite:** 17/17 on the `mai-db` server (throwaway `mai_pgtest_*` databases, all dropped; the real `mai` database untouched). Repeated: 8 consecutive runs of the 551-test 6P set (including these) and 5 runs of this file under heavy CPU load, all green. Against unmodified `94ea032` the
    same file fails 12 of 17 (the confirmation/replacement and decline races,
    the stale-transition cases, the cancel-vs-runtime races, grant release),
    so it discriminates; the 5 that pass there guard guarantees that already
    held (exactly-once activation and notification, no notification after a
    cancel, grant retention).
  - Full suite, isolated worktree (`94ea032` plus the 6P files), with
    `TEST_POSTGRES_ADMIN_URL` set so the PostgreSQL race tests ran inside it:
    ordered 6429 passed, 2 skipped (6N: 6315 + the 114 new tests; the two
    skips are the usual environment-dependent ones). Three shuffled runs,
    all 6429 passed, 2 skipped: seeds 881268363, 1514827426, 1515442903. A
    first set of three shuffled runs never started: the scratch shuffle
    plugin had been removed from the scratchpad, so each failed at plugin
    import; the plugin was recreated and the three runs above replaced them.
    No throwaway database was left behind.
  - Mutation testing: isolated worktree, harness validated (anchors match, the unmutated set
    of 540/551 tests passes, 3 controls survive), the PostgreSQL race tests
    included.
    - run 1: 33 mutations (compare-and-set, runner, runtime, confirmation
      races, truthfulness), 28 killed. Survivors R4, RT5, RT8 and CS1 led to
      new tests (the `task_not_active` journal entry; a cancel landing
      between the failure read and its write; expiry that lost to a cancel
      journals nothing; the execution note on an abandoned-proposal turn).
    - run 2: those 5 against the strengthened set (plus the new
      compare-and-set pins), 3 killed.
    - 2 survivors are equivalent and documented. RT4: the early return in
      `_record_failure` is redundant with the state predicate on its write
      (RT5, killed); with either present a stale write matches nothing. CS1:
      the record used to *validate* a model reply never licenses a
      monitoring claim either way; the record that renders the execution
      note still gets the monitor result (pinned).
    - **One unexplained failure.** The first attempt at run 2 refused to
      score: one control (a docstring reword in `runtime.py`) was reported
      killed. With that control applied the set passed (551/551), and 8
      further runs plus 5 under CPU load were all green, so the failing test
      was not identified (the harness then printed only a verdict; it now
      prints the failing test's name). A plausible cause was fixed:
      `test_two_replacements_withdraw_the_old_proposal_once` assumed the two
      turns interleave, and now holds under any order. Whether that was the
      cause is unknown..
  - Structural audit: PASS, 0 mismatches: exactly one `AuthorizationService`, one `authorize_with_grants` caller (the runner), one `ExecutionService` and Execution constructor, one `TaskRunner`, one `BackgroundRuntime`, one notification writer, one delivery-record writer, one `SecureHttpClient` (the only `httpx` importer), one owner seam; no scheduler imports in `app/tasks`; latest migration `0019`. The frontend does not read the chat response's `monitor` field, so the new `not_pending` outcome changes no frontend contract.
- **Remaining limitations:**
  - The grant is still capability-wide (argument- or task-scoped grants stay
    deferred; needed before a second creator of `gmail_list_messages` tasks).
  - Monitoring-claim detection is a deterministic heuristic: per clause, first
    person or "the monitor", English. A paraphrase it misses still depends on
    the prompt line; the application remains the only source of monitoring
    state.
  - A cancelled task keeps its last `next_run_at` (it is never claimed: the
    claim requires an active state). Unchanged from before.
  - Known 6O low findings L3 (TTL may exceed the deadline by under 1 s), L4
    (in-flight check finishing just after the deadline), L5, L7 and L8 are
    unchanged and remain documented in the audit.

### Stage 6N: Confirmed Gmail monitoring (commit titled `Stage 6N: confirmed Gmail monitoring`, 2026-10-08)

- **Status:** completed, verified, committed. Parent `25bfb98` (6M.4). It is
  the commit that adds `backend/app/tasks/requests.py`.
- **Purpose:** the front half of the agent loop. "Watch my Gmail for an email
  from X about Y and tell me when it arrives" becomes an application-written
  proposal, then, on the person's plain confirmation, a scheduled monitoring
  task. Everything after scheduling (runtime, runner, authorization with a
  standing grant, execution, Gmail, evaluation, 6H notification, 6M.3
  delivery) already existed and is reused unchanged.
- **V-1 (live, read-only, before implementation; 2026-10-08):** Gmail
  `after:<epoch>` verified against the owner's connected mailbox, counts only:
  - future and current bounds returned 0;
  - `after:<now-7d>` and `newer_than:7d` returned the same 186 ids;
  - 50 sampled timestamps (25 inside, 25 outside) were on the correct side
    of a one-day epoch boundary (second precision, received time);
  - `after:t` includes a message whose received time floors to `t`;
  - combined with `is:unread` and `from:` it was accepted and returned
    subsets.
- **Owner decisions:**
  - R1: the baseline (`received_after`) is the second the proposal is
    persisted; mail between the request and the confirmation counts.
  - R2: expiry uses existing event types with reason codes (no enum, no
    migration).
  - R3: defaults of 15 minutes and 7 days, within 5 minutes to 24 hours and
    1 hour to 7 days.
  - During implementation (2026-10-08), two facts in the code contradicted
    the design, and the owner chose:
    - `gmail_list_messages` is HIGH risk (not MEDIUM), so the grant is at
      the capability's own risk, HIGH;
    - the task budget caps a monitor at 40 checks (`max_tool_calls`), so the
      deadline is `min(requested lifetime, 40 x interval)`. At the
      15-minute default a monitor runs 10 hours; "every 6 hours for 7 days"
      runs the full week. The proposal shows the exact deadline.
- **The front door (`app/tasks/requests.py`):**
  - `recognise()`: a deterministic grammar (no model) over the raw user
    message only. It needs a trigger ("watch", "monitor", "keep an eye on")
    with a mail source plus a notify phrase, or a "tell me when I get an
    email ..." form. It extracts exactly one sender (2 to 96 characters) and
    up to 3 subject words, with optional "every N minutes/hours/day" and
    "for N hours/days/a week". It asks for clarification on a missing,
    vague, multiple or unreadable sender, a bad interval or lifetime, an
    absolute date, or more than 3 subject words. Write verbs are refused
    outright. Anything else falls through to the existing chat.
  - The proposal is one savepoint: `create_for_user` (with the deadline),
    then `attach_plan` (one step, `gmail_list_messages`, sender, subject
    terms, `max_results=1`, `body_count=0`, `received_after`), then
    `configure_monitoring` with the condition `external.content !=
    NO_MESSAGES_MATCHED`. The row is `planned`, unauthorized and
    unscheduled, so it is **runtime-inert**: not claimable, not schedulable,
    not executable, no Gmail access, no grant, no notification (all
    tested).
  - `classify_reply()`: CONFIRM is an exact whitelist (`[please] [yes |
    yeah | yep | ok | okay | sure | confirm | confirmed] [do it | go ahead |
    start it | start monitoring | set it up] [please | thanks | thank you]`,
    at most 40 characters, letters and spaces only). Then DECLINE,
    REPLACE (a full new request), INVALIDATED (an affirmative head,
    spec-change words or any digit, e.g. "yes, every 30 minutes"), and
    OTHER. Everything except CONFIRM cancels the pending proposal (reasons
    `proposal_declined` / `_replaced` / `_invalidated` / `_abandoned`);
    a proposal older than 900 s is `proposal_expired`.
  - Confirmation is bound to the persisted row: a conditional claim
    (`planned` and unauthorized), then one savepoint with `authorize_plan`,
    then `transition(QUEUED, actor="user")`, then
    `GrantService.create("gmail_list_messages", HIGH, ttl=ceil(deadline -
    now))`, then `schedule_background`. Any failure rolls all of it back and
    cancels the proposal (`proposal_failed`). A second "yes" finds nothing
    and falls through.
  - Every reply is application-written; "Done. I'm monitoring" is built
    only after the savepoint held.
  - `release_monitor_grants()` revokes the owner's live grants for the
    capability when no active task still needs it.
- **Gmail:** `received_after` on `GmailListMessagesArguments` and
  `MailQuery` (a strict integer, at most 60 s ahead, at most 30 days old,
  checked by the one `check_received_after` at both layers), rendered only
  as `after:<int>`. `NO_MESSAGES_MATCHED` is the named sentinel. The tool and
  integration forward the field by name.
- **Lifetime and expiry:**
  - `TaskService.authorization_valid`: a monitor with a deadline is valid
    while `now < min(deadline, authorized_at + 7 days)`; everything else
    keeps the unchanged 900 s.
  - The runner refuses a monitor with `monitor_expired` at its deadline
    (checked before authorization) or when that authorization has lapsed.
  - The runtime then cancels it with reason `expired`, clears
    `next_run_at`, records `background_unscheduled` (`monitor_expired`) and
    releases the grant. There is no failure count, no block and no
    notification.
  - Every reschedule (interval, contention, failure backoff) is clamped to
    the deadline.
  - Before 6N, a monitor whose authorization lapsed was silently left
    `running` and unscheduled.
- **Cancellation:** `POST /api/tasks/{task_id}/cancel`, body exactly `{}`,
  authenticated at router level, owner-scoped (another owner's task is 404),
  idempotent (200 for a cancelled task), 409 `task_is_terminal` for a
  completed or failed one. It runs through `TaskService.cancel` and releases
  the grant.
  - Concurrency, found while designing the live check and fixed within 6N's
    own code:
    - the runner's completion is now conditional on the task still being
      active in the database (`task_not_active` otherwise, also for
      `advance`);
    - the cancel locks and re-reads the row (`SELECT ... FOR UPDATE`) before
      deciding, so a cancel racing a check on PostgreSQL waits for the check
      and answers "terminal" instead of overwriting a completion whose
      notification already exists.
  - See Known defect 8 for the wider pre-existing issue.
- **Wiring:**
  - `ChatService.send_message(..., owner_id=None)` runs the monitor step
    first (before calendar, reminders, mail, workflow and research) and
    returns a 10th element. `chat_service.py` imports `app.tasks.requests`
    and nothing else from `app.tasks`.
  - The conversations route passes `OwnerId` from the seam. The Telegram
    webhook passes `owner_id=None`, so a monitoring request there gets
    "only in the Mai app" and nothing is persisted.
  - `ChatResponse.monitor` (`app/schemas/monitor.py`) carries the outcome
    and task id only.
  - `deps.get_monitor_request_service` provides the cancel route's service.
  - The prompt facts gained: "Gmail monitoring: set up only through Mai's
    own confirmed proposal. Never say monitoring has started, changed,
    stopped or found an email."
- **No migration, no new event type, no frontend change, no new scheduler,
  runner, registry, authorization or notification system, no model call.**
  `orchestration/mail_language.py` was not changed: the design's plan to
  expose its helpers proved unnecessary, because the monitor grammar has
  its own stricter sender and subject parsing.
- **Existing pins changed (deliberately, each to an exact allow-list, none
  loosened):**
  - the content-handling import pins (background runtime, task runner,
    tasks) allow exactly `chat_service.py -> app.tasks.requests`;
  - `configure_monitoring` is called only by `requests._propose`;
  - the five "task router is read-only" pins allow exactly one POST,
    `/{task_id}/cancel`, and the old "no write verb" test now checks that
    cancel answers 422 without `{}` and 404 for an unknown task;
  - "no route takes an owner" allows exactly the conversations route's
    `owner_id: OwnerId` and the shared turn helper's
    `Optional[uuid.UUID]`;
  - the Gmail and `MailQuery` field sets gain `received_after`;
  - monitoring imports `datetime`; the runtime imports `clamp_to_deadline`;
  - the planning pin allows `tasks/requests.py` to build its one plan;
  - the task layer may take exactly `NO_MESSAGES_MATCHED` from
    `app.integrations.gmail_schemas`;
  - the capability pin allows exactly one grant `revoke`, in
    `release_monitor_grants`;
  - the task package has `requests.py`;
  - chat may import exactly `app.tasks.requests` and still never names
    `TaskService`;
  - `test_providers` unpacks the 10th element.
- **Verification:**
  - 219 new tests: `tests/test_monitor_requests.py` 155,
    `tests/test_monitor_runtime.py` 17, `tests/test_monitor_api.py` 13,
    `tests/security/test_monitor_requests_security.py` 34.
  - Full suite, isolated worktree (`25bfb98` plus the 6N files): ordered
    6315 passed, 2 skipped (6M.4: 6093 passed, 2 skipped; +219 new tests and
    3 per-module parametrized cases now including `requests.py`). Five
    complete shuffled runs, all 6315 passed, 2 skipped: seeds 928791601,
    892857922, 1533660104, 50230125, 1926282308.
  - **One unexplained failure, not reproduced.** A sixth shuffled run was
    killed by the session's background time limit at about 76%, after
    printing one `F`. The seed and test name are only printed at the end, so
    both were lost. It did not recur in the five complete shuffled runs,
    the ordered run, or 25 further shuffled runs (random seeds) of the 465
    tests in the 6N, monitoring, runner, Gmail and automatic-delivery
    files. Whether it was a 6N test, a pre-existing order dependence (see
    Known defects 4 and 7) or something else is **unknown**.
  - Mutation testing (isolated worktree; harness validated: anchors match,
    the unmutated set passes, 3 controls survive):
    - run 1: 70 mutations, 62 killed. G2, G3, G4, G6, R4, Q2 and Q32 led
      to new tests (baseline bounds, the sentinel pinned as persisted data,
      `advance` after a cancellation, declines with more words, no grant
      attempt after a refused authorization).
    - run 2: those 8 and 2 cancel-lock mutations; 8 killed.
    - 2 survivors are equivalent and documented. G4: a bool baseline is 0
      or 1, always refused by the 30-day bound. Q15: the 40-character reply
      bound can never decide a result, because the longest valid
      confirmation is 37 characters.
  - Live PostgreSQL (throwaway databases in the `mai-db` container, the
    existing `mai-backend:latest` image, a clean worktree mounted
    read-only; both databases dropped afterwards; the real `mai` database
    untouched at `0019`):
    - (a) a script with stubbed Gmail on real PostgreSQL: 24/24, including
      inertness against the runtime, confirmation, the grant, no match then
      a match with exactly one notification, expiry, a cancel racing an
      in-flight check on two real connections, two concurrent
      confirmations (exactly one wins, one grant), and owner scoping. A
      control without the cancel lock failed exactly the race check
      (23/24).
    - (b) a live authenticated HTTP server:
      - the chat and cancel routes answer 401 without or with a wrong token;
      - a proposal stayed inert over runtime ticks;
      - confirmation scheduled the same row with one HIGH grant;
      - the runtime made one **real, read-only** Gmail listing (a
        non-existent sender; 0 results, no message content). The owner's
        credentials volume was mounted read-only, after the running app
        refreshed the token through its normal path;
      - a second "yes" fell through (502 there only because that
        environment's LLM key is deliberately fake);
      - cancel answered 200, 200 again, 422 for a bad body, 404 for an
        unknown or other owner's task and 409 for a completed one, and
        released the grant; a cancelled task was not run;
      - expiry through the live runtime ended `cancelled`/`expired` with no
        failure, read or notification, and the grant released;
      - the server log has no token, hash, password, Google token shape,
        subject or sender, and no traceback.
  - Structural audit: exactly one `AuthorizationService`, one
    `authorize_with_grants` caller (the runner), one `ExecutionService` and
    Execution constructor, one `TaskRunner`, one `BackgroundRuntime`, one
    notification writer, one delivery-record writer, one
    `SecureHttpClient` (the only `httpx` importer), one owner seam
    (`get_owner_id`); no scheduler imports in `app/tasks`; no model call in
    `requests.py`; latest migration `0019`.
- **Known limitations:**
  - The grant is capability-wide (owner plus `gmail_list_messages`, HIGH)
    while any monitor is active; only the runner consults grants and only
    6N creates such tasks.
  - At the default interval a monitor ends after 10 hours (the 40-check
    budget); a longer watch needs a longer interval.
  - The grammar is narrow by design; phrasings it misses fall through to
    the model, which the prompt tells not to claim monitoring.
  - Telegram cannot create monitors.
  - No frontend view.
  - Any digit or spec word in an answer to a pending proposal cancels it,
    even when the message was about something else (the reply says so).

### Fix: order-independent no-execution guard tests (`7e7b978`, 2026-10-07)

- **Status:** completed. Resolves Known defect 7. Test only: no application
  code, schema or migration change.
- `backend/tests/security/test_tool_mutation_guards.py`
  (`test_the_no_execution_guard_is_load_bearing`) and
  `backend/tests/security/test_orchestration_mutation_guards.py`
  (`test_the_no_execution_guard_still_holds`) now take a weak reference to
  their throwaway `_Executable(Tool)` and, in a `finally` block, delete it and
  call `gc.collect()`. They then assert the class is dead and that no subclass
  from their module is left in `Tool.__subclasses__()`.
  `test_orchestration_security.py::test_stage_4c_still_defines_no_way_to_run_a_tool`
  is unchanged. It has no exemption for test modules, so a leaked test
  subclass still fails it.
- **Verification** (isolated worktree of `1e2ddd2` plus the two files; the
  files were byte-identical to the committed ones):
  - The repro on HEAD failed (1 failed) for each guard test followed by the
    Stage 4C test. With the fix, both guard tests pass in both orders with it
    (4 of 4 pairs, 2 passed each).
  - Mutations (scratch harness, anchors matched once, baseline green): a
    blinded check (each guard), removing the added `execute` (each guard),
    removing the cleanup (each guard; also brings back the Stage 4C failure)
    and keeping `del` but dropping `gc.collect()` (each guard) all fail. Adding
    `execute` to the application's `EchoTool` fails the Stage 4C test. Two
    controls (an unasserted comment, an unasserted message) survive. 11 of 11
    as expected.
  - Full suite: ordered 6034 passed, 2 skipped. Shuffled (scratch shuffle
    plugin), each 6034 passed, 2 skipped: seed 1265397829 (the seed that
    exposed the defect), 1102903539, 1703306132 and 369929252. The skips are
    the expected two (`test_secrets.py` with no `.env` in the worktree;
    PostgreSQL migration-chain test with `TEST_POSTGRES_URL` unset). No
    segfaults.
  - Runs made first in the shared tree are not counted. Another agent's
    uncommitted 6M.4 edits landed during them, and seed 1703306132 there
    failed 4 AST tests on that agent's code. The same seed passes in
    isolation.
- No live PostgreSQL run: nothing touches persistence.

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

### Stage 6M.4: Authentication (commit titled `Stage 6M.4: authentication`, 2026-10-07)

- **Status:** completed, verified, committed. Parent `45e0275` (on top of 6M.3
  `1e2ddd2` and the Known-defect-7 fix `7e7b978`, made in a separate session).
  It is the commit that adds `backend/app/core/auth.py`.
- **Purpose:** the HTTP API no longer treats every caller as the owner. Each
  data request must present the one API token, and the owner it acts for is
  the token's.
- **Owner decisions (2026-10-07):**
  - one hashed token in configuration (no token table, no migration);
  - a minimal browser prompt with `sessionStorage`;
  - fail closed with 503 when unconfigured;
  - `/docs` and `/openapi.json` stay public.
- **One owner:** `app/core/owner.py` holds `CONFIGURED_OWNER_ID`, the only
  definition of the owner's id. `app.tasks.models.LOCAL_OWNER_ID` re-exports
  it (same object), so existing rows and imports are unchanged.
- **One authenticator:** `app/core/auth.py`, pure (`hashlib`, `hmac`, `re`
  and settings).
  - `authenticate(token)` returns the configured owner or None: the SHA-256
    of the token compared with `MAI_API_TOKEN_SHA256` by
    `hmac.compare_digest`. It never says why.
  - `is_configured()` asks whether a well-formed 64-hex digest is
    configured.
  - Tokens over 1024 characters are refused before hashing.
- **One request seam:** `deps.get_owner_id` reads
  `Authorization: Bearer <token>`. Its answers:
  - unconfigured: 503 `authentication_not_configured`;
  - missing or malformed header: 401 `authentication_required`, with
    `WWW-Authenticate: Bearer`;
  - wrong token: 401 `invalid_credentials`, with the same header;
  - otherwise it returns the owner.

  The messages are fixed and never echo the token. There is no bypass. The
  owner-bound dependencies take its owner explicitly: `TaskService`, the 6K
  factory (for 6L), `NotificationService` and `DeliveryStatusReader`.
  `MaiError` gained optional response headers, which the error handler now
  passes through.
- **Protection:**
  - `main.py` includes every router with
    `dependencies=[Depends(get_owner_id)]`, except `health`, `telegram` and
    `integrations`. Integrations protects its status, connect and disconnect
    routes per route; its two OAuth callbacks stay public, guarded by
    single-use `state`.
  - Public: `/health`, the Google and Gmail callbacks,
    `/api/telegram/webhook` (its own secret and allowed chat, unchanged; the
    API token grants nothing there), `/docs` and `/openapi.json`.
- **Background:** unchanged behaviour. `deliveries.DELIVERY_OWNER_ID` is now
  `app.core.owner.CONFIGURED_OWNER_ID`, the same value. The runtime imports
  nothing from `app.api` and never authenticates.
- **Frontend (`frontend/lib/api.ts` only):**
  - The token is entered by the person and kept in `sessionStorage` for the
    browser session.
  - It is sent as `Authorization: Bearer` to the Mai API URL only.
  - On a 401 the stored token is forgotten, a `window.prompt` asks once, and
    the request is retried once.
  - It never goes into a build, `NEXT_PUBLIC_*`, `localStorage`, a cookie, a
    URL or a log.
- **Configuration:** `MAI_API_TOKEN_SHA256`, empty by default, in
  `config.py`; passed empty in `docker-compose.yml`; documented empty in
  `.env.example`, with how to generate a token and hash it without saving it
  to shell history. **Until it is set, every data route answers 503.**
- **No migration.**
- **Test fixtures:** `conftest.py` puts the SHA-256 of a deterministic,
  obviously fake test token (`TEST_API_TOKEN`) into the test process's
  environment before the app is imported, and the shared clients send
  `AUTH_HEADERS`. `anon_client` sends nothing. Every existing test client
  (23 sites in 7 files) now sends the header explicitly.
- **Existing pins changed (deliberately; exact, none loosened):**
  - 6M.2 "the owner seam is parameterless" became "it takes exactly the
    settings and the `Authorization` header, and returns what `core.auth`
    authenticated";
  - 6L "the dependency passes only the session" became "it passes the
    session and the authenticated owner";
  - 6L "the router is included unconditionally" now expects the
    `AUTHENTICATED` dependency;
  - 6M.3: the work source imports `app.core.owner`, and the owner constant
    is `CONFIGURED_OWNER_ID`.
  - **Two frontend browser-storage pins** (5B
    `test_the_frontend_stores_no_credential`, infrastructure
    `test_no_personal_data_is_persisted_in_browser_storage`) banned every
    `sessionStorage.` use. The approved design keeps the token there, so
    both now allow exactly three calls (get, set and remove on the one
    `mai.apiToken` key) in `frontend/lib/api.ts`, and nothing else
    anywhere. Probes confirmed they still fail for an extra key,
    `localStorage` or a cookie. The trade-off is accepted by design: a
    script injected into the page could read the token for that session.
  - AGENTS.md §2/§3/§4 now describe the authentication boundary.
- **Verification.** The final suite ran in an isolated worktree of `45e0275`
  plus the 6M.4 files. Earlier runs used `1e2ddd2`; they were superseded when
  `7e7b978`/`45e0275` landed mid-stage. 6M.4 did not touch Known defect 7.
  - Tests: 42 behavioural (`tests/test_authentication.py`) and 17 security
    (`tests/security/test_authentication_security.py`). Covered:
    - the core: right, wrong, case, empty, non-string and bytes tokens; the
      digest presented as the token; oversized (even the right one);
      unconfigured and seven malformed digests; constant-time comparison;
      the default empty;
    - HTTP: missing; seven malformed headers; wrong; the right token under
      three scheme spellings; unconfigured (even with the right token)
      while `/health` stays up; no token echo;
    - coverage: every route the app registers, with every optional router
      on, refuses without a token, and the allow-list stays reachable;
    - spoofing through query, header and body owner values; another owner's
      tasks, task state, notifications, mark-read and delivery stay
      invisible (404) and unchanged;
    - the API token does not authenticate the Telegram webhook, as a bearer
      or as its secret;
    - the token is in no log record or response;
    - AST pins:
      - the pure authenticator, constant-time with no `==`, answering only
        the owner or None;
      - one authenticator, one reader of the digest, and one `Authorization`
        header reader;
      - the fail-closed order, the error codes and the challenge;
      - owner-bound dependencies;
      - route introspection: every registered route but the allow-list
        depends on the seam;
      - `main.py` and integrations protection;
      - one owner literal; the background never authenticates;
      - no migration; empty configuration; no token in `NEXT_PUBLIC_*` or
        the frontend build; `sessionStorage` only, one fetch, no
        `localStorage`, cookie, console or URL token.
  - Full suite (isolated worktree: `45e0275` plus the 6M.4 files): ordered
    6093 passed, 2 skipped (337 s). Shuffled with the scratch shuffle plugin
    (real reordering; 6094 or more of 6095 items moved): seeds 108301676,
    1721985211 and 816789450, each 6093 passed, 2 skipped (326 to 327 s).
    No segfault, and Known defect 7 (fixed in `7e7b978`) did not appear.
    - Earlier full runs on `1e2ddd2` found the two browser-storage pins
      (updated above) and were superseded.
    - Three attempts were killed with no result while the Mac slept. Their
      output stopped at 79%, 0% and 18%, at different places, ruling out a
      hang. They are not counted; the runs above were made with the machine
      held awake.
  - Mutation: 19/19 killed. The validated harness passed (anchors unique,
    green baseline, two controls survived: an unasserted comment and a
    docstring).
    - Targets: bypass, equality instead of `compare_digest`, unhashed
      token, malformed digest as configured, no length bound, the seam
      skipping the configuration check, any scheme, wrong token reported as
      missing, owner without authentication, the task service ignoring the
      owner, no challenge header, 403 for missing, 401 for unconfigured, a
      router or the dependency list left unprotected, Google connect public,
      a callback demanding the token, background for another owner, and
      `localStorage` in the frontend.
    - A5 (no length bound) survived the first run. It was a real gap,
      closed by the oversized-token test, then killed.
    - With behavioural tests alone, 16/19:
      - **S5** (the task service ignores the seam's owner) is
        **equivalent** in a single-owner system: the default is the same
        configured owner.
      - **R1** (the tasks router without the dependency) is **equivalent
        for that router**: every tasks route still needs the owner through
        `get_task_service`. The same mutant on the conversations router
        (R1b) is killed by the route sweep.
      - **F1** (`localStorage`) has no Python behavioural surface; the
        structural frontend pin kills it.
  - **PostgreSQL / Docker** (throwaway database; a synthetic token
    generated into a `600` scratch file and only its hash passed to the
    container; the full-suite worktree mounted read-only; seven mounted
    file hashes checked):
    - health 200 without a token;
    - `/api/tasks` without a token: 401 `authentication_required` with
      `WWW-Authenticate: Bearer`; wrong token 401 `invalid_credentials`;
      malformed 401; right 200;
    - conversations, task notifications, tools, Google status, memories,
      reminders and activity: 401 without a token, 200 with it; creating a
      conversation 401 without, 201 with;
    - the OAuth callbacks gave their own 400, and the webhook with the API
      token gave its own 404 (Telegram unconfigured);
    - **every one of the 63 operations in the live OpenAPI document**: all
      protected ones 401 without a token, none of the public ones;
    - 6M.3 background delivery under 6M.4: 6/6;
    - an unconfigured server: health 200, data routes 503 with or without a
      token;
    - the server log had no token, no token hash, no database password and
      no traceback; migrations stayed at 0019.
    - A first live run was **discarded**: it mounted the mutation worktree
      while the harness was mutating `core/auth.py` (one hash mismatch).
      It was redone against the full-suite worktree.
    - Graceful stop; the throwaway database, environment and token file
      were deleted; the real database was untouched (0019).
  - **Frontend:** `npm run typecheck` passed and `npm run build` succeeded.
    The build contains no test token, and the only `NEXT_PUBLIC_*` variable
    is `NEXT_PUBLIC_API_URL`. `npm run lint` could not run: the project has
    no ESLint configuration and `next lint` stops at an interactive setup
    prompt (pre-existing; not configured as part of 6M.4). The generated
    `tsconfig.tsbuildinfo` was removed. The running `mai-frontend` image is
    not rebuilt by this stage.
  - Structural audit 14/14 on the exact tree committed:
    - one authenticator, called only by the seam; one seam; one owner
      literal, re-exported;
    - the background has no `app.api`, `fastapi` or `core.auth` import; the
      core authenticator is pure;
    - one authorization decision, execution service, runner, runtime,
      notification writer, delivery-record writer and composition root;
    - the webhook's sender unchanged; no migration;
    - every protected file unchanged (delivery, composition, Telegram,
      webhook, conversations, `TaskService`, runner, runtime, execution,
      authorization, tools, LLM, migrations);
    - `deliveries.py` code AST-identical to 6M.3 apart from the owner's
      source; exactly one new setting.
- **Known limitations:** see Deferred (one token without revocation; an
  unmasked prompt; no rate limit). **Operator action needed:** set
  `MAI_API_TOKEN_SHA256` before using a 6M.4 backend, or every data route
  answers 503.

### Stage 6M.3: Automatic notification delivery (`1e2ddd2`, 2026-10-06)

- **Status:** completed, verified, committed as `1e2ddd2`. Parent `922dbbd`
  (6M.2). It adds `backend/app/background/deliveries.py`.
- **Purpose:** a notification Mai records can now reach the owner's channel
  without anyone asking, safely: once, never in a retry loop, never for
  history, never for another owner.
- **Owner decisions (2026-10-06):**
  - boundary = the runtime's start time (no migration);
  - owner = the 6K composition's owner (the request seam does not apply to
    background work);
  - one line each in `docker-compose.yml` and `.env.example`;
  - at most 3 deliveries per tick.
- **Setting:** `NOTIFICATION_AUTO_DELIVER_ADAPTERS`, a comma-separated list
  of registered channel names, **empty by default (off)**. Names only,
  normalised like 6I's (trim, lowercase, de-duplicate); unregistered names
  are skipped. It is passed empty in `docker-compose.yml` and documented
  empty in `.env.example`.
- **Flow:** the third work source in `BackgroundRuntime.tick()`, after
  reminders and tasks (so a notification recorded in a tick is delivered in
  that tick). It calls `app.background.deliveries.run_automatic_deliveries`:
  - Candidates come from `DeliveryStatusReader.awaiting_first_attempt` (one
    read-only, owner-scoped SELECT): the delivery owner's notifications,
    `created_at >= boundary`, with **no delivery record of any status** for
    that channel, oldest first, `LIMIT`ed by the remaining budget.
  - Each candidate goes through
    `notification_delivery_service(session, owner).deliver(id, channel)` in
    its own session, so through 6I's owner-scoped read and 6M.1's committed
    durable claim.
  - At most `HARD_MAX_DELIVERIES_PER_TICK = 3` attempts per tick in total
    (a code constant), sequentially, each bounded by 6I's 10 s timeout. A
    failure in the pass is logged by type and never breaks the tick.
- **Boundary:** fixed once, when the runtime is created with the setting on
  (`datetime.now`). It is never a parameter. Notifications are written by
  the same runtime earlier in the same tick, so it excludes only history,
  plus a notification written in a tick that crashed before delivering. That
  one stays deliverable by a person (6L).
- **No retry:** any record (`failed`, live or abandoned `sending`, or
  `delivered`) excludes a notification from automatic delivery for that
  channel permanently. 6M.1's lease takeover stays exclusive to explicit
  6L calls, and a person's explicit retry after an automatic failure still
  works.
- **Durability:** the candidate query is an optimisation; 6I's committed
  claim decides every send. A restart re-reads nothing it has recorded, and
  the send path answers `DUPLICATE` without calling the channel.
- **Not changed:** 6I, `records.py`, models and migrations; the 6K
  composition; 6J; the Telegram webhook (it still builds its own sender, as
  recorded in Deferred, untouched); all routes and `deps.py`; `main.py`;
  tasks, runner, monitoring, execution, authorization, tools and LLM.
- **Existing pins changed (deliberately; exact allow-lists, none loosened):**
  - 6K: the composition's importers became `deps.py` plus
    `background/deliveries.py`.
  - 6I: the delivery importers add `background/deliveries.py` (`contract`,
    `status`; never the writer, the service or the registry).
  - 6M.2: the reader is built in `deps.py` plus the work source; `status.py`
    may import `app.tasks.models`; its two database calls are both
    owner-scoped SELECTs.
  - The runtime's own pins were untouched and still pass: no
    `LOCAL_OWNER_ID`, no `app.api`, nothing spawned per item.
- **Verification:**
  - Tests: 21 behavioural (`tests/test_automatic_delivery.py`) and 15
    security (`tests/security/test_automatic_delivery_security.py`), through
    the real `tick()`, 6K, 6I with 6M.1's records, and 6J with a stubbed
    transport. Covered:
    - off by default (nothing built); delivered in the tick that recorded
      it;
    - history never delivered; after enablement eligible;
    - not sent again over many ticks; a failure recorded and never retried,
      yet still retryable by a person; an abandoned claim left alone;
    - another channel's record does not block this one;
    - restart (new runtime, rebuilt composition) sends nothing again;
    - two concurrent runtimes held at a barrier until both hold the same
      candidate send once;
    - 3/3/1/0 per tick; the budget shared across channels; oldest first;
    - another owner's never; unregistered or unconfigured channels fail
      closed;
    - a failing pass never breaks the tick; no notification, task or read
      state changes; start and stop.
  - Focused: 21/21 and 15/15. All of `tests/security`: 2290 passed.
    Runtime, monitoring, reminders and delivery groups: 567 passed.
  - Full suite (isolated worktree: `922dbbd` plus the 6M.3 files): ordered
    6034 passed, 2 skipped (298 s). Shuffled with the scratch shuffle plugin
    (real reordering; 6034 or more of 6036 items moved):
    - seeds 20261007, 766895937 and 539397754: each 6034 passed, 2 skipped;
    - seed 1265397829: 1 failed, 6033 passed. The failure is Known defect 7,
      a pre-existing order dependence between two Stage 4C/4D test files.
      It reproduces on clean `922dbbd` without any 6M.3 file, and touches no
      code 6M.3 changed. It is recorded and offered as a separate fix, not
      worked around.
    - No segfault in any run.
  - Mutation: 14/14 killed. The validated harness passed (anchors unique,
    green baseline, two controls survived: an unasserted comment and a
    docstring).
    - Targets: the opt-in check, cap raised, budget not shared, unregistered
      channels used, wrong owner, budget stop removed, boundary ignored,
      candidates not owner-scoped, only delivered excluded (retry), newest
      first, any channel's record excluding all, boundary reaching back,
      the source never running, and running when off.
    - With the behavioural tests alone, 11/14 at first. ST5 (one channel's
      record excluding every channel) was a real gap, closed by a test;
      AU3 was closed beforehand by the shared-budget test. AU1 (the work
      source's own empty check) and AU6 (the budget stop) are
      **equivalent**: the empty setting short-circuits in `usable_channels`
      and in the runtime, and a zero budget asks the reader for zero rows.
  - **PostgreSQL / Docker** (throwaway database, the existing image, the
    isolated source mounted read-only, mounted hashes checked):
    - Phase one, 6/6:
      - one tick both recorded the monitor's notification and delivered it
        (one request);
      - history never delivered; another owner's never;
      - later ticks sent nothing; a failure was recorded and not retried
        over 4 ticks; 7 new notifications went 3/3/1/0.
    - **Cross-process race:** two separate OS processes, each with its own
      composition and adapter, ticked at the same moment. Exactly one
      request and one delivered record. In this run the second process
      queried after the first had committed its claim, so the candidate
      query excluded it; that claim decides a true overlap is shown by the
      barrier test and by 6M.1's 20-connection PostgreSQL race.
    - **After a real `docker restart`**, 2/2: the delivered notification
      answered `DUPLICATE` without a request, and the failed one was not
      retried.
    - The real server with the setting **on** and no Telegram credentials
      started with `deliveries=True`, ticked, and failed closed. It
      delivered nothing and logged no error or traceback, and no sentinel
      or database password. Graceful stop; throwaway database dropped; real
      database untouched (0019).
  - Structural audit 15/15 on the exact tree committed:
    - one authorization decision, execution service, runner, runtime (and
      `create_task` only in it), notification writer, delivery-record writer
      and composition root;
    - 6I `deliver` called only by the 6L route and the work source;
    - exactly one new setting; no migration; the runtime still free of
      owner constants, the API and direct delivery imports; compose and
      `.env.example` add only the empty setting; every protected file
      unchanged.
    - The webhook's second `BotApiSender` is unchanged (known, deferred).
  - **Live Telegram not tested:** no credentials are configured, and none
    were requested.
- **Known limitations:**
  - **A crash mid-tick skips a notification:** one written but not yet
    delivered before a restart falls behind the new boundary. It stays
    deliverable by a person.
  - **Failures are never retried automatically**, by policy: a person
    retries through 6L.
  - **One delivery owner:** see Deferred (6M.4).
  - **A slow channel delays the tick:** up to about 30 s when all three
    attempts time out.

### Stage 6M.2: Notification read API (`922dbbd`, 2026-10-06)

- **Status:** completed, verified, committed as `922dbbd`. Parent `e10b7ca`.
  It adds `backend/app/delivery/status.py`.
- **Purpose:** the owner can read their task notifications over HTTP. Before
  6M.2, 6H's `NotificationService` existed in-process only, and 6L could only
  deliver a notification whose id the client already knew.
- **Owner decisions (2026-10-06):**
  - extend the existing `/api/task-notifications` router (no `main.py` change);
  - include mark-read;
  - delivery state shown as the stored status plus a derived `retryable`;
  - per-task lookup deferred.
- **Endpoints** (all in `app/api/routes/notification_delivery.py`, beside
  6L's delivery POST):
  - `GET /api/task-notifications?limit=&before=&unread_only=`: newest
    first (`created_at DESC, id DESC`); `limit` 1-50 (default 20; 50 is 6H's
    `MAX_LISTED`). Keyset paging: `before=<id of the last row>`, which must
    be one of the owner's notifications, read or unread. The response is
    `{notifications, next_before}`; `next_before` is null on the last page.
    There is no total count.
  - `GET /api/task-notifications/{id}`: the same shape as a list item.
  - `POST /api/task-notifications/{id}/read` with exactly `{}`: 204 whether
    this call marked it or it was already read (idempotent). It delegates to
    6H's conditional `mark_read`.
- **Fields:**
  - Returned: `id`, `task_id`, `kind`, `check_number`, `execution_id`,
    `created_at`, `read_at`, and `deliveries[]`. Each delivery has
    `adapter`, `status` (sending / delivered / failed), `attempts`,
    `last_attempt_at`, `delivered_at` and `retryable` (`failed`, or
    `sending` past its lease: exactly when 6M.1 would grant a new claim).
  - Never returned: owner id, delivery-record id, lease, delivery key, or any
    text, recipient, credential or error (none is stored).
- **Errors:**
  - 404 `notification_not_found`: missing, or another owner's (identical
    body).
  - 422 `validation_error`: a malformed id, limit, cursor or body (including
    any field in the mark-read body, and non-JSON content types).
  - 422 `invalid_cursor`: an unknown or foreign `before` (identical; a
    cursor reveals nothing).
  - Unknown query parameters and headers are ignored.
- **Architecture** (thin adapters; no new service, writer or setting):
  - 6H gains one owner-scoped method, `NotificationService.page()`. It is
    bounded by `MAX_LISTED`, resolves its cursor through the owner-scoped
    `get`, and fetches `limit + 1` to know whether a next page exists. No
    6H semantics changed.
  - `app/delivery/status.py` (new, read-only): `DeliveryStatusReader`, one
    owner-scoped `select` over 6M.1's records (owner **and** notification
    ids). It never imports or calls the 6M.1 writer.
  - `deps.py`: `get_owner_id()` is the one place a request's owner is
    resolved; today it returns `LOCAL_OWNER_ID`, and 6M.4 replaces it.
    `get_notification_service` and `get_delivery_status_reader` bind it. No
    route takes an owner.
  - **Not changed:** 6M.1 (records, models, migration, claim and lease
    semantics), 6I, 6K, 6L's delivery semantics, `main.py`,
    `routes/__init__.py`, the runtime, runner, execution, authorization,
    configuration and migrations.
  - **Known follow-up for 6M.4:** the 6L delivery dependency and `TaskService`
    still take their owner from their own `LOCAL_OWNER_ID` defaults, not from
    `get_owner_id`. Authentication must route them through the seam too.
- **Existing pins changed (deliberately; each an exact allow-list, none
  loosened):**
  - 6H: "no HTTP route reaches notifications" became "only `deps.py` (which
    builds the read service) and the route (which imports `MAX_LISTED`)
    reach it, and no HTTP module names the writer or the row model"; the
    read service's method list adds `page`; "no `get` call" became "the only
    `get` call is the service's own `self.get`".
  - 6I: the module list adds `status.py`; "no loop" now allows exactly one
    `for` in `status.py`, over its query rows, with no await or adapter or
    claim call; the delivery importers list adds `status` to `deps.py` and
    the route.
  - 6K: the route/deps reach-list adds `app.delivery.status`.
  - 6L: the route's imports and endpoint list are exact multi-endpoint lists;
    `Query` is allowed only in the list endpoint; `mark_read` is called only
    by the mark-read endpoint.
- **Verification:**
  - Tests: 51 behavioural (`tests/test_notification_read_api.py`) and 14
    security (`tests/security/test_notification_read_api_security.py`).
    Covered:
    - an empty inbox; the exact contract fields; newest-first pages with no
      repeat or skip; equal timestamps; unread-only with any cursor; the
      default and ceiling;
    - nine malformed list parameters; unknown and foreign cursors
      identical;
    - get equals the list item; missing and foreign identical 404; malformed
      ids on every endpoint; path-traversal shapes;
    - per-owner isolation from both sides (by overriding the seam) and the
      real seam's default; forged owner in query, headers and body;
      enumeration;
    - another owner's delivery rows filtered; a real 6I delivery reported;
      each stored state and its `retryable`; several channels; no internal
      or secret value in any response;
    - mark-read: sets `read_at` once, idempotent, changes nothing else, 404
      for foreign and missing, only `{}` as JSON, only the offered methods;
    - reading writes nothing and never builds the delivery composition;
      five concurrent mark-reads on separate connections.
  - Focused: 6M.2 51/51 and 14/14. All of `tests/security`: 2275 passed.
    Notification, delivery and Telegram groups (6H-6M.2): 361+ passed.
  - Full suite (isolated worktree: `e10b7ca` plus the 6M.2 files): ordered
    5998 passed, 2 skipped (304 s). Shuffled with the scratch shuffle plugin
    (real reordering, seed reported; 5998 or more of 6000 items moved): seeds
    20261006, 937166689 and 1626385444, each 5998 passed, 2 skipped (303 to
    306 s). No segfault. A first set of shuffled runs never ran: the plugin
    had been cleaned from the scratchpad and pytest failed to import it. They
    were discarded, not counted, and rerun after restoring the plugin.
  - Mutation: 24/24 killed. The validated harness passed (anchors unique,
    green baseline, two controls survived: an unasserted comment and an
    unasserted docstring; the new code logs nothing, so there was no log
    string to use as a control).
    - Targets: paging (owner scope, unread filter, the 6H bound, an
      owner-scoped cursor anchor, the cursor ignored, tie order, no
      `next_before`, no look-ahead row, oldest first); delivery state (owner
      scope, `retryable` for failed, the lease comparison, every sending row
      retryable); routes (invalid cursor accepted, missing not 404, mark-read
      not idempotent, missing marked 204, extra fields accepted, delivery
      state dropped, limit ceiling raised, mark-read not delegated); and the
      owner seam (returns another owner; read service or reader ignore it).
    - With the behavioural tests alone, 23/24. N3 (the 6H bound removed)
      survived, because the route's `le=50` hides it over HTTP. It was a
      real gap in `page()`'s own contract, closed by one direct service test;
      re-run: killed.
    - Two harness incidents, both fixed: a paging test looped until
      `next_before` was null, so the "cursor ignored" mutant hung instead of
      failing (the loop is now bounded); and stopping that run left the
      mutant in the scratch copy, which was restored and re-synced before
      the final run.
  - **PostgreSQL / Docker** (the existing `mai-backend:latest` image,
    throwaway database, isolated source mounted read-only, mounted hashes
    checked), 12/12 over real HTTP to the running server, on notifications
    from the real runtime:
    - 25 notifications paged 10/10/5, newest first, with no repeats;
    - another owner's notification never listed, and the same 404 as a
      missing one;
    - contract fields only, with delivery state failed / retryable;
    - malformed limits, ids and bodies gave 422; a foreign cursor gave 422
      `invalid_cursor`;
    - mark-read gave 204 twice, and another owner's gave 404 (it stayed
      unread);
    - unread-only excluded it; no owner id, lease, record id or delivery key
      appeared;
    - no delivery record was written;
    - `EXPLAIN` (sequential scans disabled) shows the page query can use
      `ix_task_notifications_owner_id_read_at_created_at` (then a sort,
      because the `id` tie-break is not in the index: fine at single-owner
      volume), and the delivery-state query can use
      `uq_notification_deliveries_notification_adapter`.
    - The server log had no traceback, ERROR or 5xx, and no database
      password. Graceful stop; throwaway database dropped; real database
      untouched (0019).
  - Structural audit 15/15 on the exact tree committed:
    - one `AuthorizationService`, `ExecutionService`, `TaskRunner`,
      `BackgroundRuntime`, notification writer, notification read service,
      delivery-record writer, composition root and owner seam;
    - no new setting or migration; 6M.1 and every protected file unchanged;
      the writer still imported only by 6I; only 6M.2 paths changed.
    - **Finding (pre-existing, not 6M.2):** "exactly one Telegram sender" no
      longer holds at HEAD. The webhook committed in `e10b7ca` builds its own
      `BotApiSender` (see Deferred).
  - `git diff --check`: clean.
- **Known limitations:**
  - **No authentication:** the owner is the server constant (6M.4).
  - **No total or unread count, and no per-task lookup.**
  - **Delivery state is a snapshot:** a claim can change between the
    notification read and the delivery read in one request (read
    committed). It is reported as stored.

### Stage 6M.1: Durable notification delivery records (`87365aa`, 2026-10-02)

- **Status:** completed, verified, committed as `87365aa`. Parent `a0e16ae`
  (6L). It adds `backend/app/delivery/records.py`.
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
