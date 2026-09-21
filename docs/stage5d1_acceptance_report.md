# Stage 5D.1 — Acceptance Report

## Status: **PASS**

The fabrication path found in the Stage 5D.0 audit is closed. Mai can no
longer claim an external action the authoritative record does not show.

## Baseline

`e803e4b` (Stage 5D.0 audit). Verified before any change: working tree clean,
**4201 passed, 1 skipped**.

## Root cause

`"Search it."` is anaphoric, so the research recogniser does not claim the
turn — no proposal, no consent gate, no execution. The chat model, which *does*
see history, offers a search the application never registered; `"yes"` finds no
proposal; and the model delivers on its promise by inventing results.

The prompt made this easy: every research, mail and calendar block is attached
only when it has content, so on a turn where nothing ran the prompt said
**nothing at all** about actions. The model filled the silence.

Stage 5A.2 could not catch it. It validates the response's *shape*, and a
paragraph announcing search results is valid prose.

## Execution-truth architecture

Two layers, because either alone is insufficient.

**Preventive** — synthesis is now told the turn's facts, *always*, including
the negative case: `web search: was not requested and did NOT happen`. It is
rendered from the record and attached unconditionally; a test asserts the
attachment is not inside any `if`.

**Detective** — `app/synthesis/execution_truth.py` re-reads the generated prose
and refuses any claim the record does not support. A prompt is an instruction,
and instructions are not a security control.

Five states, not collapsed: `NOT_REQUESTED`, `PROPOSED_NOT_EXECUTED`,
`EXECUTED_SUCCESSFULLY`, `EXECUTED_FAILED`, `UNKNOWN`. `CLAIMABLE_STATES` is a
one-member frozenset, so **UNKNOWN never becomes success** and a state added
later licenses nothing. A channel counts as executed only when its outcome says
`completed` **and** the layer produced the content that implies — an outcome its
own layer does not corroborate resolves *downwards*.

No new authority was invented: the existing `ResearchOutcome` / `MailOutcome` /
`CalendarOutcome` enums already are the record.

## Files changed

**New:** `app/synthesis/execution_truth.py`, `tests/test_execution_truth.py`,
`tests/security/test_execution_truth_security.py`, and these two documents.

**Modified:** `app/services/chat_service.py` (record derivation, unified
recovery, truth enforcement), `app/prompt/formatter.py`
(`with_execution_state`, `with_execution_correction`, `_append_execution_state`,
**root-clone fix**), `app/api/routes/prompt.py` (debug parity),
`tests/security/test_response_contract_structure.py` (enforcement vs rendering),
`tests/test_chat_flow.py`, `tests/test_providers.py`,
`tests/test_prompt_debug_api.py`, `tests/test_retrieval_integration.py`
(prompt-shape assertions).

### A latent defect found and fixed on the way

`PromptFormatter.with_research()` — the *root* clone every other builder goes
through — did not copy `_mail_block` or `_calendar_is_availability`. The
builders were therefore order-dependent: `with_mail(...).with_research(...)`
lost the mail section entirely while the reverse order kept it.

Unreachable before this stage only because the recogniser chain makes mail and
research mutually exclusive — and immediately reachable once
`with_execution_state` began cloning after `with_mail`. **Gmail's own security
test caught it**, which is the system working as intended. Fixed at the root
clone, with both orderings now verified.

## Tests

| Run | Result |
|---|---|
| Baseline (`e803e4b`) | 4201 passed, 1 skipped |
| **After Stage 5D.1, run 1** | **4300 passed, 1 skipped** (173.53s) |
| **After Stage 5D.1, run 2** | **4300 passed, 1 skipped** (170.70s) |

**99 new tests.** Coverage against the §17 matrix:

| | Case | Covered by |
|---|---|---|
| A | search requested but not recognised | the Fable end-to-end test |
| B | search requested, no proposal | same |
| C–E | proposal, no execution / denied / expired | state-mapping parametrisation |
| F | execution fails | `EXECUTED_FAILED` mapping + note wording |
| G | execution succeeds | `test_a_real_search_may_be_described` |
| H–I | no results / malformed | `completed`-without-evidence → UNKNOWN |
| J–K | fabricated citation / results table | isolated table-signal tests |
| L–M | fabricated Gmail / Calendar claim | per-channel claim tests |
| N | false claim then follow-up | `test_a_later_turn_cannot_inherit_a_false_claim` |
| O | false claim entering memory | `test_the_fabricated_text_never_reaches_memory_extraction` |
| P–Q | injection faking execution state | 4 parametrised injections |
| R | ordinary prose not claiming execution | 21 legitimate sentences |
| S–T | existing 5A.2 cases | unchanged and passing |

## Mutation testing

**23 of 24 caught. Harness independently validated.**

```
=== harness validation ===
  all target test files exist        : yes
  every mutation anchor matches      : yes
  tests collected                    : 279
  unmutated test set exits 0         : True
  control survives (a reworded comment        ): yes
  control survives (an unasserted log string  ): yes
  => harness VALID
```

The harness **refused to report a score** on its first run because a mutation
anchor did not match — exactly the check that Stage 5A.1's false 32/32
motivated. It refused again on the control block (a stale symbol). Only the
third run was valid.

First valid run scored **15/24**. The nine survivors were the most useful
output of the stage:

| Survivor | What it exposed |
|---|---|
| T2 | The claim check vetoed the answer but no test proved it *triggered the retry*. Deleting `or not truth.ok` left fabrication blocked and every such turn silently degraded to boilerplate. |
| T10 / T11 | The end-to-end corpus carried both a results heading *and* a table, so neither signal was isolated. Now tested separately. |
| T12 / T17 | The note's per-state wording was untested. T17 in particular: the test looked for the words "sources, URLs, titles, snippets", which a note *permitting* them would also contain. |
| T18 | Nothing asserted which corrective instruction the retry receives. |
| T21 / T22 | **No test reached the negation, modality or question guards at all** — the verb-tense patterns already exclude "I can search" and "I haven't searched", so the guards were dead weight as far as the suite could tell. The cases that reach them are hedged sentences containing a *result-presentation phrase*, and there were none. |
| T19 | Equivalent — see below. |

One further finding: **T11's first mutation was a no-op** — it added a disabled
alternative instead of removing the real branch, and was scored as a survivor.
A broken mutation is a harness defect, not a test gap; it was rewritten.

### The one survivor: T19, equivalent

Removing the early `if not recovered.accepted` return is outcome-neutral. A
refused response carries `text=""` by Stage 5A.2's design, so the claim check
that follows finds nothing, returns ok, and the caller's `elif
validated.accepted` is `False` either way — both paths reach
`_synthesis_failed_reply`. Verified directly across four refused shapes. The
early return is a short-circuit, not a guard, and the property it rests on is
pinned by `test_a_recovery_that_returns_a_blob_is_still_refused`.

## Structural audit

| Check | Result |
|---|---|
| Assistant-history write sites | `services/chat_service.py` only |
| `ExecutionRecord` construction | `execution_truth` (internal), the debug route (empty record), `chat_service` ×2 via `record_for_turn` |
| Generation call sites | **2** — Stage 5A.2's bound preserved |
| `execution_truth` imports | `enum`, `re`, `typing`, `app.core.logging` — it cannot act |
| `record_for_turn` signature | `{research, mail, calendar, workflow}` — no text parameter |
| Enforcement confined to the chat service | asserted, with rendering helpers explicitly classified |

## Live verification

Real stack, real Groq, real PostgreSQL.

| Test | Result |
|---|---|
| **Fable reproduction ×2** | **0/2 fabrication** (was **2/2**). Reply: *"I started to answer as though I had done a web search, but I have not…"* |
| Normal question | answered normally, no claim |
| Explicit research → consent → execution | `completed`, `searched=true`, described **with sources** — no over-blocking |
| Calendar | `reauthorisation_required`, truthful |
| Gmail | `not_connected`, truthful |
| Logs during refusals | channels, states and a length — never the text |

## Browser verification

The exact Fable sequence in the real UI now ends with the truthful refusal.
Checked: no fabricated table, no `Web Search Results` heading, no invented
URLs, no error banner, `localStorage` / `sessionStorage` / cookies / IndexedDB
all empty.

One console 502 appeared during the session: Groq rate-limiting produced
`llm_invalid_response` **before** the execution-truth layer runs ("Chat turn
failed at the model call"). Provider-side, not caused by this stage; the retry
after a pause succeeded.

## Security audit

| Vector | Result |
|---|---|
| Model prose establishing execution | **closed** — `record_for_turn` reads only layer results |
| Execution-state spoofing in the response | refused (a response asserting its own state is still checked against the record) |
| Prompt injection claiming a search succeeded | 4 injections tested; the record is unchanged and the claim refused |
| Provider response spoofing | outcomes come from the layers, not the provider envelope |
| Fabricated citation injection | table + URLs is itself a claim |
| History poisoning | fabricated text is never stored |
| Memory poisoning | extraction never receives it |
| Cross-turn false claims | nothing false exists to inherit |
| Consent / authorization bypass | untouched; no new capability, tool, scope or network destination |
| DoS via recovery | one attempt, two generation sites, structurally asserted |

No dependency was added.

## Remaining risks

1. **Detection is linguistic.** A claim phrased outside the clause patterns
   passes the detective layer. Mitigated by the preventive layer, and by
   keeping the observed fabrication verbatim as a regression corpus that grows
   from reality rather than imagination.
2. **English only.**
3. **Three channels.** A future capability is unadjudicated until added to
   `Channel`; a test pins the set so that is deliberate.
4. **The root cause is untouched.** `"Search it."` still does not become a
   search — Stage 5D.1 makes that honest, Stage 5D.2 makes it work.
5. **Carried forward unchanged:** the three legacy blobs, credential rotation,
   `starlette 0.52.1` (still unreachable — no form parsing was added),
   `next@15.5.4`, and the unscanned Debian layer. Live Calendar remains
   `reauthorisation_required`.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Fable fabrication path closed | **PASS** (live 0/2, was 2/2) |
| 2 | No claim without authoritative evidence | **PASS** |
| 3 | Failed/unexecuted/unknown cannot synthesize success | **PASS** |
| 4 | Fabricated citations blocked | **PASS** |
| 5 | False claims cannot become trusted context | **PASS** |
| 6 | Gmail and Calendar truthfulness intact | **PASS** |
| 7 | 5A.2 response contract intact | **PASS** (23 shapes, 2 generations) |
| 8 | Consent gates intact | **PASS** |
| 9 | Network policy intact | **PASS** |
| 10 | 5C provenance intact | **PASS** |
| 11 | Full test suite passes | **PASS** (4300 ×2) |
| 12 | Mutation harness independently validated | **PASS** (refused twice before reporting) |
| 13 | Live verification | **PASS** |
| 14 | Security audit | **PASS** |
| 15 | Working tree clean | **PASS** |
| 16 | Documentation complete | **PASS** |
