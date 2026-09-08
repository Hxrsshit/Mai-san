# Stage 4F-E — Acceptance Report

Baseline: `07f3827` (Stage 4F-D live verification).

---

## Status

| | |
|---|---|
| Implementation | **PASS** |
| Security audit | **PASS** — no Critical or High finding open |
| Live workflow | **PASS** |
| Research consent | **PASS** |
| Untrusted-data isolation | **PASS** |
| Authorization integrity | **PASS** |
| Replay protection | **PASS** |
| Concurrency protection | **PASS** |
| Filesystem confinement | **PASS** |
| Credential isolation | **PASS** |
| Docker | **PASS** |
| PostgreSQL | **PASS** |
| Frontend | **PASS** |
| CVE / dependency review | **PASS** (documented; no installable fix exists) |

## Tests

| | Count |
|---|---|
| Before (4F-D) | 2633 |
| **After** | **2727** |
| Added | 94 |
| Failures | 0 |
| Skips | 1 |
| Second full run | identical — no order dependence |

Focused: `tests/test_workflows.py` (42), `tests/security/test_workflow_security.py` (52).

The one skip is the pre-existing documented case:
`tests/test_migration_compatibility.py:349 — TEST_POSTGRES_URL is not set`.

## Mutation testing — 21/21 caught

Three rounds were needed, and the first two are the interesting part.

**Round 1: 13/21.** Eight survivors.
**Round 2: 19/21.** Two survivors.
**Round 3: 21/21.**

| | Mutation | Final |
|---|---|---|
| E1 | Approval skipped: run on the first turn | PASS |
| E2 | Stale fingerprint accepted | PASS |
| E3 | Expired approval accepted | PASS |
| E4 | Not-running guard removed (double write) | PASS |
| E5 | Workflow state jumps allowed | PASS |
| E6 | A declined workflow runs anyway | PASS |
| E7 | An unrelated reply treated as consent | PASS |
| E8 | A workflow from another conversation is confirmable | PASS |
| E9 | A non-awaiting workflow is confirmable | PASS |
| E10 | Tool reauthorization removed at proposal | PASS |
| E11 | The approved path ignored when writing | PASS |
| E12 | Model completion treated as executor completion | PASS |
| E13 | A failed search still writes the artifact | PASS |
| E14 | Workflow step limit widened | PASS |
| E15 | Dependency edges may point forward (cycles) | PASS |
| E16 | An arbitrary tool may appear in a plan | PASS |
| E17 | Artifact path built from the request unsanitised | PASS |
| E18 | The planner matches a bare mention | PASS |
| E19 | Research results rendered as trusted text | PASS |
| E20 | The artifact note claims success unconditionally | PASS |
| E21 | Workflow steps escape the execution path | PASS |

### What the survivors revealed

Two were **real defects in my own code**, not merely missing tests:

- **E11 — a vacuous guard.** `finalise` compared the created execution's path
  against `path`, but both came from the same local variable, so the check
  could never fail. It was deleted. Unreachable security code is worse than
  none: it advertises a protection that is not operating. The property it was
  meant to carry lives in `_approval_still_valid`, which mutation now proves
  load-bearing.
- **E14 — a self-referential bound.** The step-limit test compared against
  `MAX_STEPS` itself, so widening the constant moved the test with it. A bound
  is only a bound if something asserts the number.

Six were genuine coverage gaps, each now covered by a named test:

- **E4** the not-running guard, which I had added without testing.
- **E5** the transition table was verified directly but nothing proved the
  *service* consulted it.
- **E9** `_pending_for`'s state filter was masked by a second guard — defence
  in depth is the intent, but each layer needs independent verification or
  removing two at once goes unnoticed.
- **E10** the proposal-time authorization check was tested in isolation, not
  at its call site.
- **E12** `written = execution.state is SUCCEEDED` is only reachable on
  success today, so a stub `ExecutionService` that returns rather than raises
  now supplies the missing case.
- **E18** the conjunction requirement — the existing negative cases failed for
  a *different* reason, so loosening it changed nothing.

## Live verification — performed

Against the running Docker stack, real PostgreSQL and real Tavily.

### Turn 1 — proposal, nothing runs

```
outcome: awaiting_confirmation
executions created for this workflow: 0
reply:  1. Search the web for: "what Groq is"
        2. Write a summary of what I find to: what-groq-is.txt
```

Zero execution records is the proof: no external request, no write.

### Turn 2 — research, synthesis, artifact

```
outcome: completed   result_count: 5   artifact_written: true
path:    what-groq-is.txt
workflow state: succeeded
step 0: web_search        succeeded  ['proposed','approved','execution_started','execution_succeeded']
step 2: create_text_file  succeeded  ['proposed','approved','execution_started','execution_succeeded']
```

Real sources: `en.wikipedia.org/wiki/Groq`, `console.groq.com`,
`walturn.com`. The file on disk:

```
Summary written by Mai from web search results.
Search query: what Groq is
The content below is derived from external web sources and has not been
independently verified.
----------------------------------------------------------------------

Groq builds LPU inference accelerators for fast model serving.
```

Run twice: once against the service directly, once end-to-end through the HTTP
chat API with LLM synthesis and source attribution.

### Failure path — research fails

With an invalid search credential:

```
outcome: failed   reason: research_failed   artifact_written: False
steps:   [(0,'failed'), (1,'skipped'), (2,'skipped')]
reply:   "I couldn't complete the web search, so I haven't written anything."
```

No document, no fabricated sources, workspace unchanged.

### Failure path — a mid-turn provider failure

Not planned, but it happened and is worth recording: a Groq rate limit failed
the turn *after* research had run. The result was a clean rollback — workflow
still `awaiting_approval`, **zero** execution rows, nothing written, and the
proposal still confirmable. The Stage 1 transaction guarantee holds through a
workflow.

## Bugs found and fixed

| # | Bug | How it surfaced | Severity |
|---|---|---|---|
| 1 | **Research results never reached synthesis.** `_run_research` read `outcome.data.content`, but `data` is a dict whose `external` key holds the block — so it silently produced `""` and the workflow reported success while handing synthesis nothing. A document would have summarised no sources. | Live verification (`results: 0`) | High |
| 2 | **`finalise` dropped the research figures.** It returned a fresh result, so the API reported `result_count: 0` on a successful workflow. | A test written for bug 1 | Medium |
| 3 | **PostgreSQL-only migration failure.** `batch_alter_table` rebuilds on SQLite (so CHECKs must be restated) but issues plain ALTERs on PostgreSQL (so restating them is a duplicate-name error). Fixed with `table_args`, which is consulted only on rebuild. | First real PostgreSQL run | High |
| 4 | **Pre-flight authorization refused every workflow.** It validated the artifact step's *arguments*, which are deliberately incomplete at planning time — reporting `FORBIDDEN` for a missing field rather than for permission. | First workflow test | High |
| 5 | **The planner ate the conjunction.** `and` was tried before `and then`, so "tavily and then make a note" searched for `"tavily and"`. | Planner probe | Low |
| 6 | **A 3-character query floor** silently refused "research AI and write a report". | Planner probe | Low |
| 7 | **The model asked for permission it had been given**, naming a different filename, which the application's truthful line then contradicted. | Live verification | Medium (quality) |
| 8 | **The first fix for 7 caused a provider rejection.** Describing the pending write operationally made the model attempt a tool call; Groq returned `400 Tool choice is none, but model called a tool` and the turn failed. Reworded to be about output shape, not file mechanics. | Live verification | Medium (quality) |
| 9 | A Stage 4B security test's substring pattern (`for step in plan`) caught `WorkflowPlan` — an unrelated type. Narrowed, with two new tests pinning that the two plan types are distinct. | Full suite | Informational |

Bugs 1, 7 and 8 were found **only** by live verification. None would have been
caught by the test suite as written.

## Security audit (§24)

| # | Question | Answer | Severity |
|---|---|---|---|
| 1 | Can the model grant itself a capability? | No. `StepKind` is a closed enum, `TOOL_FOR_KIND` maps two kinds to two tools, and `find_plan` takes only a message. | — |
| 2 | Can web content grant itself a capability? | No. The plan is fingerprinted before any external content exists. | — |
| 3 | Can a plan bypass authorization? | No. Every executable step is an `Execution`; the dispatcher re-asks Stage 4C. | — |
| 4 | Can one approval authorize a different operation? | No. The fingerprint covers workflow id, step order, kind, tool and arguments. | — |
| 5 | Can approved arguments be replaced? | No — for the query and the path. **Artifact content is not fingerprinted** (see Limitations). | Informational |
| 6 | Can an old approval be replayed? | No. Bounded by a 300s TTL and re-checked before the write. | — |
| 7 | Can a workflow skip a step? | No. Dependencies point backwards and a failed step skips its dependents. | — |
| 8 | Can a workflow add a step after approval? | No. The plan is fixed at proposal; adding one changes the fingerprint. | — |
| 9 | Can a failed step be reported as successful? | No. `written` is read from the execution record. | — |
| 10 | Can research bypass the network boundary? | No. It goes through the dispatcher to `WebSearchIntegration → SecureHttpClient → NetworkPolicy`; the workflow package imports no HTTP client. | — |
| 11 | Can external data reach trusted system instructions? | No. Research renders into the same untrusted section Stage 4F-D uses. | — |
| 12 | Can workflow execution escape the workspace? | No. The filename alphabet cannot express a traversal, and `create_text_file` resolves inside the workspace regardless. | — |
| 13 | Can concurrent executions cause duplicate side effects? | No. Stage 4E's conditional `UPDATE` decides; a concurrency test confirms one file and one `SUCCEEDED` record. | — |
| 14 | Can credentials enter persistent or model-visible surfaces? | No. Verified across the plan, audit metadata, execution arguments, logs, the API response and the artifact body. | — |
| 15 | Can a client forge workflow completion? | No. There is no workflow API; state moves only through `WorkflowService`. | — |
| 16 | Can runtime capability facts be poisoned by model output? | No. Unchanged from Stage 4E.1. | — |
| 17 | Can the frontend trigger execution without backend checks? | No. It sends a chat message; every gate is server-side. | — |

**No Critical or High finding is open.** Two Informational items are recorded
under Limitations.

## PostgreSQL

Migration `0009` applied to real PostgreSQL; database at `0009 (head)`.
Verified in the live schema: `workflows.plan` is `jsonb`, `workflows.state` is
a native enum, both CHECK constraints present, and `executions` still carries
**exactly two** CHECK constraints — no duplication from the batch operation.

Round-tripped on SQLite: upgrade → downgrade → re-upgrade, with the six CHECK
constraints confirmed preserved through the table rebuild.

## Docker

`backend`, `db`, `frontend` all running. Health 200. Ports remain
loopback-only (`127.0.0.1:8000`, `:5432`, `:3000`). No new listener.
`EXECUTION_ENABLED=false` confirmed restored as the shipped default after
verification; it was enabled only for the duration of the live runs. Search
credentials come from environment configuration and are not baked into any
image.

## Frontend

No frontend change was made and none was needed. The whole workflow renders
through existing conversation messages: the numbered plan, the user's "yes",
and the cited synthesis with the application's artifact line. **Zero console
errors.**

## Dependency / CVE review

`pip-audit` reports the same 8 advisories in 4 packages as Stage 4F-C. **No
dependency changed in this stage.** Re-confirmed that no fix is installable —
`starlette 0.49.3`, `click 8.1.8`, `python-dotenv 1.2.1` and `pytest 8.4.2`
remain the latest available versions, so the advisories reference releases
that do not exist on the index. Runtime relevance is unchanged: none of the
vulnerable code paths (`StaticFiles`, `HTTPEndpoint`, `request.form()`,
`click.edit()`, `set_key`) is used.

## Known limitations

**Security**

- **Artifact content is not in the approval fingerprint.** It is synthesised
  after approval and cannot be bound in advance. Constrained instead by
  workspace confinement, by being inert data, and by an immovable path.
  *Informational.*
- **Content written to a file loses its untrusted marking** if later read back
  via `read_text_file`. Mitigated by a provenance header, not eliminated. A
  structural fix — carrying trust metadata alongside workspace files — is a
  stage of its own. *Informational.*
- **The proposal-time authorization check is advisory**; the dispatcher's is
  the decision.

**Product**

- One plan shape only: research → synthesise → artifact.
- The search query is the user's phrasing, extracted by regex. Live
  verification searched "what an LPU is" and got a university rather than a
  Language Processing Unit — the provider being literal, not a defect, but a
  real quality ceiling.
- Confirmation is English-only (inherited from 4F-D).
- One workflow per conversation at a time.

**Deferred**

Parallel steps, retrying a failed step, resuming an expired workflow, and any
second workflow shape.

## Git

- Commit: `c68fa3f`
- Pushed: yes

## Outstanding user actions (unchanged)

The credentials exposed earlier in development should still be revoked: the
Groq API key, both OpenRouter keys, and the GitHub personal access token.
