# Mai — PostgreSQL Runtime Compatibility Report

**Status: FIXED AND VERIFIED ON REAL POSTGRESQL 16.15**

The full Alembic chain now executes against a real PostgreSQL container from an
empty volume, the backend starts and stays up, and the deduplication step keeps
the correct row.

This is the first time any part of Mai has run against PostgreSQL. Eight stages
of "PostgreSQL: NOT VERIFIED" ended here, and the first contact found a genuine
bug — plus a second one hiding inside it.

---

## 1. Root cause

```
asyncpg.exceptions.UndefinedFunctionError: function min(uuid) does not exist
HINT: No function matches the given name and argument types.
```

Migration 0003 removed duplicate memories with:

```sql
DELETE FROM memories
WHERE id NOT IN (
    SELECT MIN(id) FROM (SELECT id, memory_type, normalized_content
                         FROM memories) AS m
    GROUP BY memory_type, normalized_content
)
```

`memories.id` is a UUID. **PostgreSQL has no `min(uuid)` aggregate.** Verified
directly against the running container:

```
mai=# SELECT MIN(x) FROM (VALUES (gen_random_uuid())) t(x);
ERROR:  function min(uuid) does not exist

mai=# SELECT x FROM (VALUES (gen_random_uuid()),(gen_random_uuid())) t(x) ORDER BY x LIMIT 1;
e238a2f6-d904-4329-b45b-13009ff033b2          -- ordering works fine
```

The distinction matters for the fix: PostgreSQL **can order** UUIDs (`uuid` has
a btree operator class) — it is only the `min` *aggregate* that does not exist.
So no cast to text is needed, and none was used.

The failure is not data-dependent. PostgreSQL resolves function signatures at
plan time, so the statement fails on an empty table exactly as it does on a
full one. Every backend start hit it, producing the restart loop.

### A second bug in the same statement

The migration's own comment said it kept *"the oldest of each group"*. It did
not. Memory ids are random UUIDv4, so `MIN(id)` selects an **arbitrary** member
of each duplicate group — on SQLite too, where the statement ran without error.

The code and its documentation had disagreed since Stage 2A, silently, on both
dialects. Fixing the portability problem properly meant fixing this as well:
"oldest" requires ordering by `created_at`.

---

## 2. Migration affected

`backend/alembic/versions/0003_memory_unique_constraint.py` — *Stage 2A audit:
enforce memory uniqueness in the database*.

Failure point in the chain:

```
0001 -> 0002  ok
0002 -> 0003  ✗  UndefinedFunctionError
```

It is the only migration with an aggregate over a UUID. The two `op.execute`
sites in the whole tree are this one and a simple `DELETE FROM entities WHERE
normalized_name = 'user'` in 0005, which is portable.

---

## 3. Why the SQLite tests missed it

Two independent gaps, either of which alone would have hidden it.

**SQLite is dynamically typed.** It has no `uuid` type; the value is stored as
a blob or text, and `MIN()` applies SQLite's own ordering to whatever is there.
The statement runs and returns *a* row — so it looked correct.

**No test had ever run a migration.** Every fixture builds the schema from ORM
metadata with `Base.metadata.create_all`, which produces the *post*-migration
shape directly. The migration files were executed for the first time in this
session's Docker run. 1,790 passing tests said nothing about them, because none
of them touched one.

The second gap is the more important of the two. It also explains why the
regression tests below run `alembic upgrade` for real rather than testing the
ORM.

---

## 4. The fix

```sql
DELETE FROM memories
WHERE id IN (
    SELECT id
    FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY memory_type, normalized_content
                ORDER BY created_at ASC, id ASC
            ) AS row_number_in_group
        FROM memories
    ) ranked
    WHERE row_number_in_group > 1
)
```

| Choice | Reason |
| --- | --- |
| `ROW_NUMBER()` | Expresses the intent directly — rank the rows, keep rank 1 — and needs no aggregate over a UUID |
| `ORDER BY created_at ASC` | Delivers "the oldest", which the comment always claimed |
| `, id ASC` | Deterministic tiebreak when two rows share a timestamp |
| No `::text` cast | PostgreSQL can order UUIDs natively; a cast would also defeat any index |
| `IN` rather than `NOT IN` | A `NOT IN` against a subquery that can yield NULL silently matches nothing. This form has no such edge |

**One statement, both dialects — no dialect branch.** PostgreSQL has had window
functions since 8.4; SQLite since 3.25 (2018). The bundled SQLite here is 3.51,
and a test asserts the floor so the behavioural tests cannot pass for the wrong
reason.

The uniqueness guarantee is unchanged: the same rows are removed on SQLite, and
a *better-defined* row survives on both.

---

## 5. Other PostgreSQL compatibility issues found

**None.** Every migration was audited for the listed classes:

| Class | Finding |
| --- | --- |
| UUID aggregation | **1 occurrence, fixed.** Now banned by a test across all migrations |
| SQLite-only functions (`julianday`, `strftime`, `ifnull`, `group_concat`, …) | None. Banned by a test |
| Raw SQL | Two sites. 0003 (fixed) and a portable `DELETE` in 0005 |
| `server_default=text("now()")` | PostgreSQL-native. Confirmed applied as `now()` on all timestamp columns |
| Timestamps | Correct: `timestamp with time zone` on PostgreSQL |
| UUID columns | Correct: native `uuid`, not `varchar` |
| Enums | Correct: 9 native PostgreSQL enum types created (`memory_type`, `memory_status`, `relationship_type`, `conflict_reason`, …) |
| Booleans | No boolean columns in any migration |
| JSON | No JSON columns |
| `batch_alter_table` (0003) | Alembic emits a plain `ALTER` on PostgreSQL rather than the SQLite table-rebuild. Executed successfully |
| Foreign keys / `ON DELETE` | Applied natively; no PRAGMA equivalent needed |
| Transactional DDL | PostgreSQL supports it (`Will assume transactional DDL`), which is why the failed 0003 left the database cleanly at 0002 rather than half-migrated |

Nothing else was rewritten. The audit's conclusion is that the tree was
otherwise dialect-clean — the one bug was real and isolated.

---

## 6. Tests added

`backend/tests/test_migration_compatibility.py` — **15 tests**.

They run `alembic upgrade` in a subprocess against a real database, insert the
duplicates the old race produced using raw SQL (the ORM cannot: its schema
carries the very index the migration is clearing the way for), and assert what
the migration *does*.

| Group | Tests |
| --- | --- |
| Chain executes | Empty → head; and head over duplicated data, so the migrations *after* 0003 survive the cleanup |
| Correct row survives | One per group; the **oldest** wins; insertion order does not decide it; ties still leave exactly one |
| Correct grouping | Keyed by `(memory_type, normalized_content)` together; distinct memories untouched |
| Index afterwards | The unique index is creatable and then rejects a duplicate |
| Statement shape | No UUID aggregate; no text cast; deterministic ordering |
| Fault class sweep | No migration aggregates a UUID; none uses a SQLite-only function |
| Environment | SQLite ≥ 3.25, so the behavioural tests cannot pass for a wrong reason |
| **PostgreSQL** | `test_the_chain_runs_on_postgresql`, gated on `TEST_POSTGRES_URL` |

**They catch the original bug.** Reverting the fix and re-running:

```
WITH THE ORIGINAL MIN(id) SQL: 4 failed
  test_the_oldest_row_is_the_one_kept                    ← the behavioural bug
  test_the_deduplication_step_aggregates_no_uuid
  test_the_deduplication_step_orders_deterministically
  test_no_migration_aggregates_a_uuid_column
```

The first is the one that matters: it fails on **SQLite**, where the old
statement ran without error. A test that only banned `MIN(id)` textually would
have been satisfied by any rewrite; this one checks the outcome.

**And they were executed against real PostgreSQL**, inside the container:

```
$ docker exec -e TEST_POSTGRES_URL=postgresql+asyncpg://…@db:5432/mai_pytest \
    mai-backend python -m pytest tests/test_migration_compatibility.py
15 passed in 10.77s
```

All 15 — the PostgreSQL test ran rather than skipping.

---

## 7. Docker runtime results

**Docker 29.7.2 / Compose v5.4.0. PostgreSQL 16.15 on aarch64.**

### The volume question

The specification asked whether the development volume had to be removed, and
for an explanation before doing it. Both were checked before acting:

```
conversations          0 rows
messages               0 rows
memories               0 rows
relationships          0 rows
knowledge_conflicts    0 rows
entities               1 row (User)   ← seeded by migration 0005, not user data
alembic revision:      0006
```

**A reset was not required.** PostgreSQL's transactional DDL meant the failed
0003 rolled back cleanly, and the schema was already at head — the backend's
bind-mounted source had picked up the fix on an automatic restart before this
verification began.

It was removed anyway, deliberately: with the schema already at head the chain
would not re-run, so a from-scratch boot was the only way to *observe* every
migration executing against real PostgreSQL. **Nothing user-created was lost**
— the counts above were re-checked immediately before `docker compose down -v`.

### Cycle

```
docker compose down          → stack stopped, volume preserved
docker compose up --build -d → all three containers healthy, chain idempotent
docker compose down -v       → volume removed (verified empty first)
docker compose up --build -d → from-scratch boot
```

---

## 8. Migration chain result

From an empty PostgreSQL volume, in the real stack:

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema: conversations and messages.
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, Stage 2A: memories table.
INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, Stage 2A audit: enforce memory uniqueness in the database.
INFO  [alembic.runtime.migration] Running upgrade 0003 -> 0004, Stage 2B: entities, aliases and memory links.
INFO  [alembic.runtime.migration] Running upgrade 0004 -> 0005, Stage 2C: relationships, evidence, and the seeded User entity.
INFO  [alembic.runtime.migration] Running upgrade 0005 -> 0006, Stage 3C: knowledge conflict links.
```

All six, including the one that was failing. Result: 10 tables, revision `0006`.

### Deduplication proven on PostgreSQL with real duplicates

Run against a throwaway database in the same container, so the development
volume was untouched:

```
rows before 0003: 4        -- 'newest','middle','oldest' sharing normalized_content,
                           -- plus one distinct row
Running upgrade 0002 -> 0003 … ok

survivors:
  oldest    | same text     ← the OLDEST survived, not an arbitrary row
  untouched | other text    ← the distinct row was left alone

INSERT a duplicate afterwards:
  ERROR: duplicate key value violates unique constraint
         "uq_memories_type_normalized_content"
```

This is the whole fix demonstrated end to end: it runs, it keeps the right row,
and the constraint it exists to enable is enforced afterwards.

---

## 9. Backend health verification

```
$ curl http://127.0.0.1:8000/health
{"status":"ok","app":"Mai","environment":"development",
 "database":{"healthy":true,"detail":null},
 "llm":{"healthy":true,"detail":null}}
HTTP 200
```

A real write/read round-trip against PostgreSQL:

```
POST /api/conversations  → c1e3f000-c242-4b4f-8264-2fbf84eec59d
GET  /api/conversations  → total=1

$ psql -c "SELECT id, title FROM conversations;"
  c1e3f000-c242-4b4f-8264-2fbf84eec59d | New conversation
```

The row created through the API is the row in PostgreSQL. Stage 4 surfaces
respond on the real stack: `/api/tools` 200, `/api/memories` 200,
`/api/entities` 200.

**No restart loop.** `RestartCount` is `0` for all three containers.

Schema landed correctly: `id :: uuid`, `created_at :: timestamp with time zone`,
enums as native PostgreSQL types.

---

## 10. Frontend verification

```
$ curl -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/
200
```

Container `mai-frontend` up, `RestartCount` 0. The Next.js build completed
during `docker compose up --build`.

**This is a runtime smoke test, not a functional one.** The page serves; no
user flow through the UI was exercised.

---

## 11. Remaining unverified

Now verified, and no longer to be listed as NOT VERIFIED in future reports:

- **Docker runtime** — built and run; all three containers healthy, no restart
  loop.
- **PostgreSQL runtime** — full chain executed on PostgreSQL 16.15 from empty;
  API round-trip persists.
- **Frontend runtime** — builds and serves HTTP 200.

Still unverified:

- **Frontend functionality.** It serves a page. No chat flow, rendering, or
  error path was exercised through the UI.
- **PostgreSQL under concurrency.** The concurrency tests still run on SQLite.
  The uniqueness constraints they rely on are now confirmed to exist on
  PostgreSQL, but the races themselves were not re-run against it.
- **PostgreSQL downgrade path.** Only `upgrade` was executed. Downgrades are
  tested on SQLite only.
- **Dependency CVE scanning.** Unchanged from Stage 3D: no scanner installed.
- **Sustained operation.** The stack ran for minutes, not days. No load,
  connection-pool exhaustion, or long-running behaviour was observed.

---

## Test suite

```
1804 passed, 1 skipped in 64.25s
```

The skip is `test_the_chain_runs_on_postgresql` when `TEST_POSTGRES_URL` is
unset — deliberate, so a green local run never implies PostgreSQL coverage it
did not have. With the variable set, that test ran and passed against the
container.

Baseline before this fix: 1,790 passed. Added: 15.
