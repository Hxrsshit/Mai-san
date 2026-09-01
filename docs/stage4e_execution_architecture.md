# Stage 4E — Controlled Execution Foundation

Mai can now perform actions. Three of them, inside one directory, each one
approved by a person against its exact payload, with the whole capability off
by default.

This document explains the design, the conflicts the stage had with existing
guarantees, and — most importantly — the one guarantee it genuinely weakened.

---

## 1. What changed, in one sentence

Through Stage 4D, "Mai cannot perform actions" was a description of the code:
no `Tool` defined an `execute` method, no dispatcher existed, and no
configuration could conjure one. Stage 4E built a dispatcher, so that sentence
is no longer true unconditionally — it is true *by configuration*, and the
configuration ships off.

## 2. The gates

Every side effect passes through `Dispatcher.dispatch`, and every one of these
must agree before a tool is touched:

| Gate | Refusal | What it protects |
|---|---|---|
| `EXECUTION_ENABLED` | `execution_disabled` | The operator switch. Default false. |
| State is `APPROVED` | `approval_required` | There is no edge from `PROPOSED` to `EXECUTING`. |
| Fingerprint matches | `approval_does_not_match_this_action` | Approving A does not run B. |
| Not expired | `approval_expired` | An abandoned grant stops applying. |
| Stage 4C authorizes | `not_authorized` | Re-asked now, not trusted from proposal time. |
| Executor exists | `tool_is_not_executable` | Declared ≠ implemented. |
| Arguments valid | `invalid_arguments` | Against the tool's own schema. |
| Claim succeeds | `execution_already_claimed` | One conditional UPDATE; one winner. |

Only then does `tool.run(...)` happen — one line, in one file.

## 3. Authority separation, again

The pattern from Stages 4A–4D holds without exception here:

- **No request field carries authority.** `ExecutionRequest` has three fields:
  `tool_name`, `arguments`, `idempotency_key`. There is no `state`, no
  `approved`, no `authorization_status`, and `extra="forbid"` means sending one
  is a 422 rather than a silent drop.
- **No model output reaches this code.** Nothing in `app/services`,
  `app/orchestration`, `app/planning`, `app/intent`, `app/prompt` or `app/llm`
  imports `app.execution` — asserted structurally by
  `test_no_chat_or_orchestration_module_imports_the_executor`.
- **No string becomes code.** Tool lookup is a dictionary hit on a name
  registered in `catalog.py`. There is no `eval`, `exec`, `compile`,
  `__import__`, `importlib`, `getattr`, subprocess or socket anywhere in
  `app/execution` — asserted by AST, not by substring search.

## 4. Why approval is a fingerprint

An approval that meant "this execution may run" would be defeated by editing
the execution. So approval records `sha256(tool, arguments)` at the moment it
is granted, and the dispatcher recomputes it before running. Any difference —
a different path, different content, `overwrite` flipped from false to true —
produces a different hash, and a hash that does not match is not an approval.

`overwrite` is a parameter rather than tool behaviour for exactly this reason:
creating a file and clobbering one are different actions, so they need
different approvals.

## 5. Why the claim is a conditional UPDATE

```sql
UPDATE executions SET state = 'executing'
 WHERE id = ? AND state = 'approved'
```

Checking the state in Python and then writing it would lose a race: two
workers both read `approved`, both proceed, and the action happens twice. The
predicate makes the *database* pick a winner, and the loser sees `rowcount = 0`
and refuses. This is why the guarantee survives multiple processes, where an
in-memory lock would not — the same reasoning as the UNIQUE constraint on
`idempotency_key`.

## 6. The audit journal commits before the refusal propagates

Found by a test, not by inspection. A refused execution raised through the
route, the session dependency rolled the request back, and the rollback took
the journal entry with it — leaving an audit trail that recorded only the
attempts that were *permitted*, and a record stuck in `approved` that read as
though nobody had ever tried it.

`_record_refusal` now commits before re-raising. The refused attempt is the
interesting one; it is the one that must survive.

## 7. The conflicts, and how each was resolved

Stage 4E contradicted five guarantees earlier stages had asserted in tests.
None was deleted; each was narrowed to what remains true.

### C1 — "No `Tool` defines a way to be run"

`tests/security/test_tool_security.py` walks `Tool` and every subclass
asserting none defines `execute`, `run`, `invoke`, `dispatch` or `__call__`.

**Resolved by keeping it literally true.** `ExecutableTool` is a *separate
hierarchy* in `app/execution/tools.py` — it does not subclass `Tool`, so
walking `Tool`'s subclasses still enumerates every `Tool` there is, and none
of them can be run. The two are matched by name: the dispatcher looks up a
Stage 4C declaration for authorization metadata and an `ExecutableTool` for
the implementation, and refuses unless both exist.

Being in one registry buys nothing without the other. Two lists that must
agree means one edit cannot add a running capability.

### C2 — "No tool may declare `execution_mode` other than `unavailable`"

**Resolved by narrowing the rule to what is still false.** `SYNCHRONOUS` is
now permitted, because an executor genuinely exists for three tools.
`BACKGROUND` is still refused — and not merely because it is unimplemented. It
describes an action running with nobody waiting on it, which is an autonomous
loop by another name, and Stage 4E forbids those.

The invariant is unchanged: *a tool may not declare a mode the application
cannot honour.* Only the set of honourable modes grew.

A related decision: the three tools' argument schemas are defined **once**, in
`catalog.py`, and imported by the executor. Authorization validates a proposal
against the declared schema and the dispatcher validates the same arguments
again before running; if those were two classes they could drift, and the gap
between them would be a payload that passes authorization and then runs as
something else. `ExecutableArguments` is an alias of `ToolArguments`, not a
parallel base class, so the divergence is unrepresentable rather than merely
unlikely.

### C3 — `RuntimeFacts.can_execute_actions` returned an unconditional `False`

**This is a real weakening, and it is the most important thing in this
document.**

Before: `return False`. Not a policy — a description. No configuration could
make it true because no executor existed.

After: `return self.execution_enabled and self.executable_tool_count > 0`.

| | Before | After |
|---|---|---|
| Can it be true? | No, structurally | Yes, if configured |
| What makes it true? | Nothing | `EXECUTION_ENABLED=true` **and** an executor |
| Can a request set it? | No | No |
| Can model output set it? | No | No |
| Default | False | False |

What survives is the authority separation: it is still a derived property with
no field behind it, so nothing — model output, request data, a caller holding
an instance — can assert it. `extra="forbid"` was added during this stage so a
forged `can_execute_actions` is now *refused* rather than silently ignored.

What is gone is that the answer no longer depends only on code that cannot
change at runtime. One line in an environment file is now sufficient. That is
what "configuration-gated" means, and it is exactly why the default is off and
why the endpoints are not even registered when it is.

The rendered prompt block follows the same rule — it derives its sentence from
the facts rather than hardcoding "NOT AVAILABLE", because a fixed string would
now be a claim the formatter cannot know to be true.

### C4 — "No module anywhere dispatches a tool"

**Resolved by changing the shape of the claim rather than dropping it.** The
test now asserts dispatch is *confined* to exactly one file, plus a companion
test that the file actually dispatches — without which, deleting the executor
would leave the confinement test green and prove nothing.

### C5 — "No execution table exists" (found during implementation)

Stage 4D asserted no `executions` table. Narrowed to what it was really
claiming: *orchestration* persists nothing. The test now additionally scans
`app/orchestration` for `session.add(`, `flush(` and `commit(`, which is the
stronger statement and the one Stage 4D actually meant.

## 8. What this stage deliberately did not build

Named in the specification as forbidden, and absent:

`delete_file` · `shell_command` · `python_execution` ·
`arbitrary_http_request` · `database_query` · `email_send`

`future_send_email` and `future_delete_file` remain declared and unimplemented;
the critical one is still disabled outright, and neither gained an execution
mode. `MAX_PERMITTED_RISK` was **not** raised — Stage 4C's comment anticipated
that Stage 4E might lift the CRITICAL ceiling once an audited, revocable,
bounded executor existed. That executor now exists, and the ceiling stayed at
`HIGH` anyway: nothing in this stage needed it, and raising a limit because you
finally could is not a reason.

## 9. Workspace containment

`resolve_in` refuses, before any tool sees the path: empty or whitespace-only,
over 400 characters, containing a null byte or newline, containing a
backslash, absolute, `~`-prefixed, drive-qualified, deeper than 12 components,
any `..` / `.` / empty component, and anything that fails an "is inside the
root" check after `.resolve()`.

Refusal, never sanitisation. Rewriting `../../etc/passwd` into something safe
would perform a *different* action from the one approved — and the fingerprint
would still match the original.

Writes use `os.open` with `O_EXCL | O_NOFOLLOW` rather than an `is_symlink()`
check followed by an open. A check-then-open has a window; the kernel refusing
at open time does not.

## 10. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `EXECUTION_ENABLED` | `false` | The operator switch. Off ships. |
| `MAI_WORKSPACE_ROOT` | `./mai_workspace` | Everything is confined here. |
| `EXECUTION_APPROVAL_TTL_SECONDS` | `900` | Never unbounded. |
| `MAX_WORKSPACE_FILE_SIZE_BYTES` | `1000000` | Read bound. |
| `MAX_WORKSPACE_LIST_RESULTS` | `500` | Listing bound. |
| `MAX_WORKSPACE_LIST_DEPTH` | `6` | Listing bound. |

With `EXECUTION_ENABLED` false the execution routes are not registered at all —
the outer of two doors, the inner being the service's own refusal.
