# Stage 4E — Acceptance Report

**Status: complete.** Every claim below was executed, not reasoned about.
Where something was not verified, it says so.

---

## 1. The three acceptance scenarios

| Scenario | Result |
|---|---|
| Propose → approve → execute creates a file | **PASS** — `test_the_full_lifecycle_creates_a_file`, and again on PostgreSQL |
| Execute without approval is refused | **PASS** — 403 `approval_required`, filesystem untouched |
| A model asking to run a tool runs nothing | **PASS** — `test_a_reply_asking_to_run_a_tool_runs_nothing` |

The third is the one that matters most. The provider was made to emit text
containing a tool name, a JSON payload, `APPROVED=true`, `state=approved` and
`authorization_status=allowed`. Nothing parses it. After the turn,
`list(workspace.iterdir()) == []` and the `executions` table has zero rows.

## 2. Test results

```
tests/test_execution_lifecycle.py      21 tests
tests/test_execution_tools.py          18 tests
tests/security/test_execution_security.py  17 tests
tests/test_execution_states.py         11 tests
tests/test_execution_disabled.py        8 tests
tests/test_execution_concurrency.py     5 tests
```

Full suite: **2043 collected, all passing**, no skips introduced by
this stage. (Entering Stage 4E it was 1911 passed, 1 skipped.)

## 3. Mutation testing — 15/15 guards protected

Each guard was reverted in place and the suite re-run. A **PASS** means tests
failed, i.e. the guard is doing work something would miss.

| | Mutation | Result |
|---|---|---|
| M1 | Approval state check removed | PASS |
| M2 | Fingerprint comparison always matches | PASS |
| M3 | Expiry never expires | PASS |
| M4 | Workspace containment check removed | PASS |
| M5 | `..` allowed as a path component | PASS |
| M6 | `EXECUTION_ENABLED` ignored by the dispatcher | PASS |
| M7 | Authorization not re-checked at dispatch | PASS |
| M8 | Claim UPDATE made unconditional | PASS |
| M9 | Claim `rowcount` not checked | PASS |
| M10 | State transition table not enforced | PASS |
| M11 | Idempotency UNIQUE index made non-unique | PASS |
| M12 | `succeeded()` returns true for every state | PASS |
| M13 | Executable-registry check removed at approval | PASS |
| M14 | Arguments not validated before running | PASS |
| M15 | `O_EXCL` dropped from file creation | PASS |

Four of these failed on the first run and led to real work:

- **M7, M10, M14** were unprotected because the happy path reached the same
  outcome through an *earlier* check. A guard whose only proof is another
  guard is not independently verified, so each got a test that exercises it
  directly — M14's drives the dispatcher with a permissive authorization stub,
  because the outer layer would otherwise refuse first.
- **M11** as originally written ("disable the pre-check") passed with no
  effect, which was itself the finding: the pre-check is a fast path, and the
  guarantee lives in the UNIQUE index. The mutation was rewritten to remove
  the constraint, and a test added that asks the database directly.

## 4. PostgreSQL verification — real, not mocked

Migration `0007` against PostgreSQL 16.15 in Docker:

```
upgrade   0006 -> 0007   clean
downgrade 0007 -> 0006   clean, enum types dropped (SELECT returns empty)
upgrade   0006 -> 0007   clean
```

Schema confirmed via `\d executions`: `jsonb` arguments, native enum types
(`execution_state`, `execution_authorization_status`, `execution_risk_level`,
`execution_event_type`), the UNIQUE index on `idempotency_key`, both CHECK
constraints, and the `ON DELETE CASCADE` foreign key from `execution_events`.

The lifecycle itself was then run inside the backend container against that
database — real asyncpg, real jsonb, real native enums, real constraints:

```
PASS  proposed, nothing on disk
PASS  approved, still nothing on disk
PASS  executed, file present
PASS  state is succeeded
PASS  jsonb arguments round-trip
PASS  journal ordered and complete
PASS  journal metadata is jsonb dict
PASS  second run refused (ApprovalRequired)
PASS  concurrent creates make one record
PASS  approved-without-fingerprint refused by CHECK
PASS  traversal refused (WorkspaceViolation)
PASS  nothing written outside the workspace
PASS  refusal is durable in the journal

13/13 checks passed on PostgreSQL
```

Test rows were removed afterwards; the `executions` table is empty.

## 5. Bugs found and fixed during this stage

### The audit journal vanished on refusal

A refusal raised through the route, the session dependency rolled the request
back, and the rollback took the journal entry with it. The result was an audit
trail recording only the attempts that were *permitted*, and a record stuck in
`approved` that read as though nobody had ever tried it.

Found by `test_a_refused_write_leaves_the_record_failed_not_succeeded`, which
expected `failed` and got `approved`. `_record_refusal` now commits before
re-raising.

### `RuntimeFacts` silently swallowed a forged capability

`RuntimeFacts(can_execute_actions=True)` was accepted and ignored — safe,
because the property still computed the real answer, but quiet. Now
`extra="forbid"`, so it is refused.

### Two argument schemas would have been able to drift

The executable tools initially had their own argument classes, separate from
the Stage 4C declarations. Authorization validates against the declaration and
the dispatcher validates again before running; two classes could diverge, and
the gap between them would be a payload that authorizes as one action and runs
as another. The schemas are now defined once in `catalog.py`, and a test
asserts identity (`is`, not equality) between the two registries' models.

## 6. Forbidden capabilities — confirmed absent

`delete_file` · `shell_command` · `python_execution` ·
`arbitrary_http_request` · `database_query` · `email_send`

Asserted by name in `test_none_of_the_forbidden_capabilities_is_executable`,
and structurally: no `eval`, `exec`, `compile`, `__import__`, `importlib`,
`getattr`, `subprocess`, `socket`, `httpx`, `smtplib` or `pickle` anywhere in
`app/execution` — checked by walking the AST rather than searching for
substrings, because a substring scan over a source file also reads its
comments, and a docstring naming what a module must not do failed a test about
what it does.

`MAX_PERMITTED_RISK` remains `HIGH`. Stage 4C's comment anticipated that Stage
4E might raise it once an audited, revocable, bounded executor existed. That
executor now exists and the ceiling did not move: nothing here needed it.

## 7. The guarantee this stage weakened

`RuntimeFacts.can_execute_actions` changed from an unconditional `False` to
`execution_enabled and executable_tool_count > 0`.

This is a genuine reduction. Before, no configuration could make Mai claim it
can act, because no executor existed. Now one line in an environment file is
sufficient. The authority separation survives — it is still a derived property
with no field behind it, and a forged value is refused — but the claim
"structurally impossible" has become "configuration-gated, default off".

Section 7 (C3) of `stage4e_execution_architecture.md` covers this in full.

## 8. What is NOT verified

Stated plainly rather than left implied:

- **The Python test suite runs on SQLite only.** `conftest.py` builds an
  in-memory SQLite schema. PostgreSQL behaviour is covered by the migration
  round-trip and the 13-check script in §4, not by running the suite against
  it.
- **True parallel execution is not proven by the suite.** SQLite serialises
  writers, so the race tests demonstrate the *logic* — the loser sees no
  matching row and refuses. The PostgreSQL script exercised concurrent creates
  on real connections; concurrent *dispatch* on PostgreSQL was not tested
  under genuine parallelism.
- **The frontend has no execution UI.** Nothing was built for it this stage,
  and frontend functionality remains unverified from earlier stages.
- **No dependency CVE scan has been run** at any stage, including this one.
- **`EXECUTION_ENABLED` has never been turned on in the Docker deployment.**
  The container still runs with execution off. The PostgreSQL checks enabled it
  in-process for the duration of a script.

## 9. Outstanding user actions (unchanged, still outstanding)

The credentials exposed earlier in development should be revoked: the Groq API
key, both OpenRouter keys, and the GitHub personal access token.
