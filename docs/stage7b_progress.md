# Stage 7B progress checkpoint

The durable checkpoint for the owner's autonomous Stage 7B directive
(multi-tenant organisation platform). A new session reads this first, then
`docs/CHANGELOG.md` and `git status`, and continues from **Next action**.
Never record secrets, credentials or personal data here.

## Ownership

One lead session owns the Stage 7B working tree at a time. On 2026-10-29 the
session "Mai San" took over the uncommitted L0 work from the session "Mai
development after multi-agent pilot" (idle, its last verification complete),
at the owner's instruction. Before editing, check that no other session is
running tests or editing in `~/Mai` (`ps`, the peer session list).

## Units

| # | Unit | Status | Evidence |
| --- | --- | --- | --- |
| 1 | L0 tenant-scope foundation + review remediation (M1, M3, M4, L1, L2, L5) | **Completed, `e1e8b55`** (see CHANGELOG, Stage 7B L0) | SQLite and live PostgreSQL, ordered and shuffled |
| 2 | Remaining L0 findings | L1, L2 done. **M2** (foreign keys from a scope to a membership) belongs to unit 10. **H1** is an operational boundary (below) | -- |
| 3 | Ownership model and enforcement-migration design (foreign keys, orphan checks, legacy-row policy) | **Completed** (design: `docs/stage7b_ownership_design.md`) | Repository inventory, 2026-10-29 |
| 4 | Scope conversations, messages, memory-extraction context | **Completed** (see CHANGELOG, Stage 7B unit 4) | SQLite and live PostgreSQL, ordered and shuffled; security review F1-F7 |
| 5 | Personal-memory isolation, scoped deduplication | **Completed with unit 6** (see CHANGELOG, Stage 7B units 5-6) | SQLite and live PostgreSQL, ordered and shuffled; security review M1-M2, L1-L6 |
| 6 | Tenant-aware entities, relationships, knowledge, retrieval, context | **Completed with unit 5** (migration `0022_scoped_uniqueness`) | as unit 5 |
| 7 | Background membership gates, revocation races | **Completed** (see CHANGELOG, Stage 7B unit 7) | SQLite and live PostgreSQL (forced races), ordered and shuffled |
| 8 | Ownership on reminders, executions, workflows, other revocable work | **Completed**: 8a reminders (`f9e22b1`), 8b executions and workflows (see CHANGELOG) | SQLite and live PostgreSQL (forced races), ordered and shuffled; security review M1-M3, L1-L6 |
| 9 | Scope history imports, protect integration state | **Completed** (see CHANGELOG): imports scoped; history API and integration-backed tools stay owner-only by decision | SQLite and live PostgreSQL, ordered and shuffled |
| 10 | Final non-null and relational enforcement migration | **Completed** (see CHANGELOG): composite same-scope foreign keys (0025); membership key and NOT NULL deferred by decision (design 6a) | SQLite and live PostgreSQL, ordered and shuffled |
| 11 | Telegram organisation binding, sender identity, mention/reply, learning | Not started. Member Telegram turns stay **disabled** until units 4-10 and their security tests pass | -- |
| 12 | Provider configuration, secret protection, usage limits | Not started | -- |
| 13 | End-to-end security, concurrency, migration, regression testing, independent review | Not started | -- |
| 14 | Controlled production deployment plan, backup/recovery runbook | Not started | -- |

## Ownership inventory (2026-10-29, from the ORM metadata)

- **Scoped directly (L0 columns):** conversations, memories, entities,
  entity_aliases, relationships, knowledge_conflicts, reminders, executions,
  workflows, imported_archives.
- **Single-owner `owner_id`** (a member id since Stage 7A; the legacy
  Admin is `...a1a1`): tasks, approval_grants, task_notifications,
  notification_deliveries.
- **Owned through a parent:** messages (conversations), imported_conversations
  and imported_messages (imported_archives), reminder_notifications
  (reminders), task_events and task_steps (tasks), execution_events
  (executions), memory_entities (memories, entities), relationship_evidence
  (relationships, memories).
- **Identity (Stage 7A):** organisations, users, organisation_members,
  user_identities, identity_link_codes.

## Test environment (no secrets)

- PostgreSQL-gated tests need a **disposable** server: `TEST_POSTGRES_URL`
  (a database whose name contains "test") and `TEST_POSTGRES_ADMIN_URL` (its
  maintenance database). Role: LOGIN, CREATEDB, member of
  `pg_signal_backend`, not superuser (`AGENTS.md` section 7).
- Proven recipe (2026-10-29): the local `postgres:16-alpine` image, `--rm`,
  `--tmpfs /var/lib/postgresql/data`, `-p 127.0.0.1:55432:5432`,
  `--label mai.purpose=pgtest`, credentials generated into mode-600 scratch
  files and passed with `--env-file`. Remove it by exact container ID
  afterwards and confirm `mai-db` and `mai-backend` are unchanged (same IDs,
  start times, 0 restarts).
- Full suite: `cd backend && .venv/bin/python -m pytest -p no:cacheprovider
  --ignore=tests/security/test_secrets.py` (about 10 minutes; `pytest.ini`
  already sets `-q`). Shuffle with a scratch plugin and report the seed.

## Live test resources

- **2026-10-29:** one disposable PostgreSQL container is running for the
  current session (label `mai.purpose=pgtest`, `127.0.0.1:55432`, tmpfs, no
  data of value). Before the session ends it must be stopped by exact ID and
  its scratch credential files deleted; afterwards confirm
  `docker ps --filter label=mai.purpose=pgtest` is empty and `mai-db` /
  `mai-backend` are unchanged. A later session that finds it running and no
  owner should remove it the same way.

## Approval boundaries (stop here; never cross autonomously)

- **H1:** the backend container runs `alembic upgrade head` on start-up over
  the mounted working tree. Any restart applies `0020` to `0025` to the real
  `mai` database (still at `0019`, as last recorded). Do not restart
  `mai-backend`. Changing the start-up behaviour, or migrating the real
  database, needs the owner's approval with a backup and rollback plan.
- Real-database migrations or data changes; public deployment; pushing;
  live credential rotation; removing the four Stage 7A agent worktrees and
  branches under `.claude/worktrees/`.

## Open items carried forward (from the units 5-6 review)

- **History import is Admin-only** (`/api/history`, router dependency) until
  unit 9 scopes archives, runs and the duplicate-file check.
- **L3 (deferred to unit 10):** `Entity.aliases` and `Relationship.evidence`
  load without a scope filter; safe while every writer keeps aliases and
  evidence within one scope. Unit 10's same-scope composite foreign keys make
  it structural. The legacy-attribution operation must not run before them.
- **Attributing legacy rows** must merge, not plain-UPDATE: the per-scope
  unique indexes (0022) will collide with scoped rows that restate legacy
  facts, and the seeded legacy "User" with each scope's own User.
- *(Resolved by unit 7)* the background knowledge steps now re-check the
  member under a share lock at the start of each step.
- **0022 on the real database** takes ordinary (non-concurrent) index locks:
  apply it with the application stopped, within the approved migration plan.

## Decisions recorded

- **Legacy rows:** the directive's policy is that ambiguous historical data
  stays quarantined until safely classified. Unit 3 designs how (no scope =
  unattributed = never returned to a member until attributed).

## Stopping point (owner instruction, 2026-10-30)

Carry on with Stage 7B through unit 14, then stop and report: unit 14
committed, CHANGELOG and this file updated, the disposable test PostgreSQL
container removed, and a final report (commit hashes for units 10-14,
verification results, every item awaiting the owner, final `git status`).
No work after unit 14. The approval boundaries above are unchanged.

## Next action

Unit 11: Telegram organisation binding, sender identity, mention/reply and
learning. Member Telegram turns stay **disabled** until their security
acceptance tests pass. Then units 12-14.

Open items: per-member reminder fairness (unit 12); the deferred membership
foreign key and NOT NULL (design 6a); per-member import sources and
integration connections (owner decisions).
