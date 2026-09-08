# Stage 4F-E — Controlled Multi-Step Workflows

Mai can now compose two existing capabilities into one bounded workflow:

```
user request → plan → one informed consent → research → synthesis → artifact
```

**The stage adds no authority.** A workflow step *is* an `Execution`, so every
gate Stage 4E built applies to it unchanged. This layer decides *what to
attempt and in what order*; it decides nothing about what is permitted.

---

## 1. The central design decision

A workflow step could have been a new kind of record with its own approval,
its own state and its own dispatcher. It is not. It is an `Execution` with
two extra columns (`workflow_id`, `step_index`), which means each step
inherits, without a second implementation:

| Guarantee | Where it comes from |
|---|---|
| Stage 4C authorization, re-asked at dispatch | `Dispatcher._require_authorized` |
| Approval bound to a payload fingerprint | `ExecutionService.approve` |
| Atomic claim — one winner per step | the conditional `UPDATE` in `_claim` |
| Append-only audit journal | `ExecutionEvent` |
| Workspace confinement | `create_text_file`'s own path resolution |
| Network boundary | `WebSearchIntegration → SecureHttpClient → NetworkPolicy` |

A `workflow_steps` table would have been a second place for each of those to
be got right, and eventually a second place for one of them to be got wrong.

## 2. One consent, both operations disclosed

```
turn N      "Research what Groq is and create a short summary document"
            → "1. Search the web for: "what Groq is"
               2. Write a summary of what I find to: what-groq-is.txt
               Reply "yes" to go ahead."
            → nothing sent, nothing written, no execution record created

turn N+1    "yes"
            → research runs, synthesis happens, the file is written
```

The specification's distinction is between *disclosed* and *unrelated*. A
research approval must not silently acquire a filesystem write — and this one
does not, because the write, and its exact path, are in the sentence the user
answers. Requiring a second confirmation after synthesis was considered and
rejected: it would ask the user to approve something they had already
approved, which trains people to click through prompts.

## 3. What the approval binds, and what it cannot

`plan_fingerprint` covers the workflow id, and for every step: its position,
kind, tool, dependencies and approval-time arguments.

**The artifact's content is deliberately absent, and could not be present.**
It is synthesised from research that has not run when the user answers. What
*is* bound is the artifact's **path**, and `_approval_still_valid` recomputes
the fingerprint before the write — so a stored plan edited after approval no
longer matches, and the write is refused.

Content is constrained by three other things instead:

1. It is written inside the workspace, by the Stage 4E executor.
2. It is inert data — a text file body, never anything executable.
3. The path it lands at cannot move after approval.

This is a real, stated limitation rather than an oversight. Mutation testing
confirms the fingerprint check is load-bearing: removing it makes a tampered
path executable and a test fails.

### A guard that was removed

An earlier version also compared the created execution's path against the
plan's path. Mutation testing found that deleting it changed nothing — both
sides came from the same local variable, so the comparison could never fail.
It was removed. Unreachable security code is worse than none: it advertises a
protection that is not operating.

## 4. Provenance survives becoming a file

Every artifact opens with:

```
Summary written by Mai from web search results.
Search query: <the approved query>
The content below is derived from external web sources and has not been
independently verified.
----------------------------------------------------------------------
```

The reason is what happens *later*. Inside the workflow, research is
`ExternalData` with `trust_level=untrusted`. Once written, it is an ordinary
file — and if it is read back through `read_text_file` it arrives with no
untrusted marking at all. A header is the cheapest way to keep the origin
visible to whoever, or whatever, reads it next.

## 5. Closed-world planning

`find_plan` is a phrase table and one template. There is exactly one plan
shape, and a message cannot produce a different sequence, a longer one, or one
naming a tool this module does not already name:

- `StepKind` is a closed enum; an invented step cannot be represented.
- `TOOL_FOR_KIND` maps exactly two kinds to exactly two tools.
- The `SYNTHESISE` step has **no tool**, so it cannot be dispatched at all.

The Stage 4D lesson applies: a phrase broad enough to catch a paraphrase is
broad enough to catch a mention. Both halves of the request are required, in
order, joined by an explicit conjunction — so "Tell me about researching and
writing documents" plans nothing, and two separate sentences do not combine.

### Artifact names cannot express an escape

The filename alphabet is `[a-z0-9-]` plus a forced `.txt`. `../../etc/passwd`
becomes `summary.txt`; `.env` becomes `env.txt`; `C:\Windows\system32` becomes
`c.txt`. Traversal is *unrepresentable*, not merely refused — and
`create_text_file` would refuse an escape anyway, so this is the second of two
independent defences.

## 6. Failure is reported from the executor

```python
written = execution.state is ExecutionState.SUCCEEDED
```

The final line of a workflow reply is written by the application from that
value, never by the model — which could not know the outcome in any case,
because the write happens after it has finished speaking. A model scripted to
claim "I have successfully created the document at /etc/passwd" still produces
a reply ending "I couldn't save this to a file", and a test proves it.

| Situation | Outcome | What the user is told |
|---|---|---|
| Research fails | `failed` | "I couldn't complete the web search, so I haven't written anything." |
| Research returns nothing readable | `failed` | same — a summary of nothing is worse than no summary |
| Research succeeds, write fails | `partial` | the summary, then "I couldn't save this to a file" |
| Both succeed | `completed` | the summary, then the file name |

## 7. Limits

`MAX_STEPS = 10`, `MAX_DEPENDENCY_EDGES = 20`, `MAX_DEPTH = 10`,
`MAX_ARTIFACT_CONTENT_CHARS = 20,000`. Constants in application code — none is
read from configuration, derived from model output, or a parameter. Dependency
edges must point *backwards*, so a cycle cannot be written down. No step is
created during a run.

## 8. Cost

Zero additional model calls. Planning is a phrase table, the proposal text is
application-written, and the synthesis is the turn's own single generation —
the same call the chat path was already making. A proposal turn costs *less*
than an ordinary turn, because it answers without the model at all.

## 9. Database

One table (`workflows`) plus two columns on `executions`. The plan is stored
as JSON because it is written once and read whole, and because the approval
fingerprint is computed over it — so it must be recoverable byte-for-byte.

`ON DELETE SET NULL` on both foreign keys: deleting a conversation should not
delete the audit record of an action it proposed.

The migration uses `batch_alter_table` with `table_args`. That spelling
matters, and a first run against real PostgreSQL is what proved it: SQLite
rebuilds the table and so must be told about the existing CHECK constraints,
while PostgreSQL performs no rebuild and rejects an attempt to create
constraints that already exist. `table_args` is consulted only when a rebuild
happens, so one spelling is correct on both.

## 10. Known limitations

**Security**

- **Artifact content is not in the approval fingerprint** (§3). Bound by
  workspace confinement, inertness, and an immovable path instead.
- **Content written to a file loses its untrusted marking** if later read back
  through `read_text_file`. Mitigated by the provenance header (§4), not
  eliminated. A structural fix — carrying trust metadata alongside workspace
  files — is a stage of its own.
- **The proposal-time authorization check is advisory.** The dispatcher's
  check is the decision; this one exists so a workflow whose tools can never
  run is refused before the user is asked.

**Product**

- **One plan shape.** "Research X and write a document" and nothing else.
- **The query is the user's phrasing**, extracted by regex, not a search term
  chosen for quality. Live verification searched for "what Groq is" and got
  good results, but that is the provider being tolerant.
- **Confirmation is English-only**, inherited from Stage 4F-D.
- **One workflow per conversation at a time.**

**Deferred**

- Parallel steps, retries of a failed step, resuming an expired workflow, and
  any second workflow shape.
