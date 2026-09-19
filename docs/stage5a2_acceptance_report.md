# Stage 5A.2 — Acceptance Report

## Status: **PASS**

Synthesis output now crosses an explicit boundary before it can become an
assistant message. The boundary caught a real, previously-unknown defect
during this stage's own live verification — see
[The defect live verification found](#the-defect-live-verification-found),
which is the most important section of this report.

## Baseline

Before any Stage 5A.2 change, on `b8d2ab7` (Stage 5A.1):

```
3992 passed, 1 skipped in 177.71s
```

The skip is `tests/test_migration_compatibility.py:349`, which requires
`TEST_POSTGRES_URL`. It is no longer skipped — see
[Docker / PostgreSQL](#docker--postgresql).

## Root cause

`chat_service` assigned `reply = llm_response.content` and passed it straight
to `add_message`. Nothing in the path distinguished *an answer* from *an
internal representation of work*. When the synthesis model emitted a tool-call
object instead of prose:

1. the object was shown to the user;
2. the object was written to conversation history;
3. the next turn's prompt therefore contained an assistant message that *was* a
   tool-call blob, and the model imitated the pattern.

Step 3 is why this compounds rather than being a one-off glitch.

## Fix

A response contract: `backend/app/synthesis/contract.py`.

```
INTERNAL EXECUTION STATE → SYNTHESIS → [contract] → VALIDATED RESPONSE
                                                  → CONVERSATION HISTORY
                                                  → FRONTEND
```

`validate()` classifies model output as `PROSE`, `TOOL_CALL`,
`INTERNAL_STRUCTURE` or `EMPTY`. Only `PROSE` may be stored. A refused
response carries **`text=""`** — the offending content reaches no caller, no
log line and no database row, so the poisoning is fixed *by absence*.

On refusal, `chat_service` retries exactly once with a corrective system
instruction (built by `app/prompt/formatter.py`, which owns all prompt text —
a Stage 3B invariant with its own test). If the retry also fails, the user gets
a truthful sentence assembled from the execution record, not from the model:

> "I searched the web and got the information back, but I couldn't turn it
> into an answer just then… Nothing was lost; ask me again and I'll have
> another go."

versus, when nothing ran, "Nothing was searched or read, so nothing was lost."

This is **not** a JSON stripper. Refusal requires the response to be
*entirely* a refused object (optionally inside one code fence); `json.loads`
gives "entirely" for free, because text with anything before or after it fails
to parse. Prose that *contains* a tool-call example is an answer, and stays one.

## The defect live verification found

**A blob reached a real user while the contract was running.**

At 11:44:44 UTC on 2026-09-19 — with the contract live in the container since
11:41:52 — this was stored and displayed:

```json
{"action": "web_search", "action_input": {"query": "latest news about OpenAI"}}
```

`{action, action_input}` — the ReAct/LangChain convention — matched no key set,
so it was classified as JSON the user had asked for and accepted.

What this says about the testing, stated plainly:

- **4084 passing tests did not find it.** They could not. Every shape in the
  unit suite was a shape I had already thought of; the suite tested my
  imagination, not the provider's behaviour.
- **One browser session did find it**, because live traffic supplies shapes
  nobody enumerated.
- The spec's insistence on live verification is the only reason this is in the
  report rather than in production.

**Fixed** by widening the closed vocabulary from 10 tool-call key sets to 15:
the ReAct/LangChain family (`action_input`, `tool_input`, `tool_use`) and two
symmetry entries (`{name, input}`, `{function, parameters}`) that were missing
beside shapes already present.

Two guards were added so the lesson survives the fix:

| Guard | What it does |
|---|---|
| `OBSERVED_IN_PRODUCTION` | A regression corpus holding **verbatim** every blob this system is known to have actually emitted — the Stage 5A.1 blob and this one. Reality supplies the corpus, not me. |
| A literal pin at 23 shapes | `known_tool_call_shapes()` is pinned, with a companion check that no key set is a superset of another. Widening means editing the test and writing down why. |
| `test_widening_the_vocabulary_did_not_start_refusing_answers` | Five payloads that use vocabulary words in their ordinary sense (`{"function": "f(x) = 2x", …}`) and must still be accepted. The cost of a false refusal is a **lost answer**, so the fix is bounded in both directions. |

## Tests

| Run | Result |
|---|---|
| Full suite, before 5A.2 | 3992 passed, 1 skipped |
| Full suite, after 5A.2 | **4099 passed, 1 skipped** (146.75s) |
| Response-contract suites only | 107 passed |
| `test_migration_compatibility.py` with `TEST_POSTGRES_URL` | **15 passed, 0 skipped** |

New tests: 107 across three files — behaviour
(`tests/test_response_contract.py`), contamination and injection
(`tests/security/test_response_contract_security.py`), and an AST structural
audit (`tests/security/test_response_contract_structure.py`).

## Mutation testing

**25 of 25 caught. Harness validated.**

```
=== harness validation ===
  all target test files exist        : yes
  every mutation anchor matches      : yes
  tests collected                    : 354
  unmutated test set exits 0         : True
  control survives (a reworded comment        ): yes
  control survives (an unasserted log string  ): yes
  => harness VALID
```

The validation block exists because **Stage 5A.1's first run reported a false
32/32**: the test set named `tests/test_mail.py`, which does not exist, so
pytest exited non-zero for every mutation and everything looked caught. Only
the control mutations exposed it. The harness now refuses to report a score
until it has proved it can tell a caught mutation from a surviving one.

Getting that validation right took two attempts, both worth recording:

- `"passed" in baseline.stdout` — this repo's pytest prints only progress dots
  under `-q`. Replaced with `--collect-only -q`, parsing `path: N` lines.
- The first control mutation changed `reason="oversized"`, which a test *does*
  assert, so the "control" was correctly killed and the harness declared itself
  invalid. Replaced with a genuinely unasserted log string.

Mutations **N23–N25** cover the live-found gap specifically: removing
`action_input`, removing the whole ReAct family, and removing `{name, input}`
are each caught.

Three survivors from the first pass were dispositioned rather than papered over:

| Survivor | Disposition |
|---|---|
| N9 — a `whole` flag in `_structured_payload` | **Dead code.** Every path setting it `False` also returned `None`. Removed; unreachable code advertises a distinction that is not operating. |
| N11 — the `startswith` fast path | **Equivalent, as documented.** `_kind_of` returns `PROSE` for every JSON scalar, so `"42"` reaches the same verdict either way. Pinned by `test_the_startswith_fast_path_changes_no_verdict` so the comment cannot rot into a false claim. |
| N19 — recovery-instruction wording | **Untested.** Added `test_the_recovery_instruction_offers_no_capability`. |

## Live verification

Against the real stack (Groq synthesis, Tavily research, real Google Calendar).

| Flow | Result |
|---|---|
| Ordinary question | prose, no blob |
| Research proposal → `yes` → answer | `RESEARCH/completed searched=True`, sourced prose |
| Google Calendar | `CALENDAR/completed`, real event, prose |
| Gmail (not connected) | `GMAIL/not_connected`, truthful unavailability |
| Research repeated ×8 after the fix | **8/8 prose, 0 contract failures, 0 blobs** |

The 8-run sample matters in both directions: it shows the widened vocabulary is
not refusing legitimate sourced answers.

### The contract firing, observed live twice

**Before the fix**, a 89-char tool-call was refused, the single recovery also
failed, and the user saw the truthful failure line — correct behaviour, answer
lost:

```
WARNING app.synthesis.contract    Refused a model response that was not an answer | kind=tool_call response_chars=89
WARNING app.services.chat_service Synthesis did not meet the response contract; retrying once
WARNING app.synthesis.contract    Refused a model response that was not an answer | kind=tool_call response_chars=89
WARNING app.services.chat_service Synthesis recovery also failed the response contract | kind=tool_call
```

**After the fix**, a 74-char tool-call was refused and **recovery succeeded** —
the user received a real answer and never saw anything:

```
WARNING app.synthesis.contract    Refused a model response that was not an answer | kind=tool_call response_chars=74
WARNING app.services.chat_service Synthesis did not meet the response contract; retrying once
INFO    app.services.chat_service Synthesis recovered on the retry
```

Note what the log lines do **not** contain: the response itself. Only a kind and
a length. A refused response may have been generated on a turn carrying private
calendar or mail data into the prompt, and is exactly the thing not to copy into
a log.

Live recovery sample is small and is reported as such: **2 blob events, 1
recovered, 0 leaked.** Recovery's correctness is established by the
deterministic suite; the live figure is an observation, not a rate.

## Frontend

Verified in the real browser at `http://localhost:3000`:

| Check | Result |
|---|---|
| Normal response | prose |
| Research proposal, approval, sourced answer | correct, full citations |
| Malformed synthesis (occurred naturally) | truthful failure line, no blob |
| Calendar response | real event rendered |
| Gmail unavailable | truthful |
| Follow-up turn *in the conversation whose synthesis was refused* | clean prose — **no imitation**, which is the anti-poisoning claim |
| Raw tool-call JSON in chat | none |
| Raw provider payload | none |
| Credentials in any response | none |
| Console errors | **no console output at all** |
| Browser storage | `localStorage`, `sessionStorage`, cookies, IndexedDB **all empty** |

## Docker / PostgreSQL

- backend, db, frontend running; all bound loopback-only (`127.0.0.1`).
- Migrations at `0009`, single head, no branch.
- `test_migration_compatibility.py` run explicitly against a throwaway
  PostgreSQL database with `TEST_POSTGRES_URL`: **15 passed, 0 skipped**. The
  scratch database was dropped afterwards.
- The container runs as **non-root** (`uid=1000(mai)`).
- OAuth tokens live in a named volume at `/var/lib/mai/credentials`, directory
  mode `0700`, token file mode `0600`, outside the source tree and outside any
  image layer.

### An image-hygiene defect found and fixed

`backend/.pytest_cache/` was being shipped inside the image. The root
`.dockerignore` listed `.pytest_cache/`, but **anchored patterns match only the
context root** — and the build context is the repository root, with
`COPY backend/ ./`. The cache's `nodeids` file contains test names such as
`test_a_gsk_key_is_masked`, which trip a secret scanner on the prefix alone.

No key material, but precisely the false finding that the note at the top of
that file warns will "bury a real finding if one ever appears". Fixed by adding
`**/` variants for `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `htmlcov` and
`.coverage`. After rebuild the image ships only `app/`, `alembic/`,
`alembic.ini`, `constraints.txt`, `pytest.ini`, `requirements.txt`.

## Security audit

| Check | Result |
|---|---|
| Secrets in container logs | 0 matches for `gsk_` / `sk-or-v1-` / `tvly-` / `GOCSPX-` / `ya29.` / `ghp_` |
| Secrets in DB message content | 0 |
| Secrets baked into the image | none — the only file matching secret patterns is `app/core/logging.py`, which **is the redaction pattern list** |
| `.env` inside the image | absent (excluded by `.dockerignore`); present in the container only via the dev bind mount, `0600` on the host and git-ignored |
| Token columns in PostgreSQL | none exist |
| Memory isolation | 27 memories, 24 entities, **0** containing tool-call structure; the refused turn left no memory or entity trace |
| New tool, integration, network destination or OAuth scope | **none added** — the contract module reaches no tool and no network, asserted by AST audit |
| `LLMResponse.raw` reintroduced | no — fields remain `content / model / finish_reason / usage`, asserted by test |
| Assistant-message write sites | exactly 2, both in `chat_service.py`, asserted by AST audit |
| Recovery recursion | structurally impossible, asserted by AST audit |

### Dependency CVEs

`pip-audit` run **inside the runtime image** (Python 3.12.14, Debian 13):

**`starlette 0.52.1` — 5 distinct advisories.** All are pre-existing and none
was introduced by this stage. Each was checked against this codebase rather
than merely listed:

| Advisory | Vulnerable surface | Reachable here? |
|---|---|---|
| PYSEC-2026-161 | `Host` header injection into reconstructed `request.url` | **No** — no code reads `request.url` / `base_url` |
| PYSEC-2026-248 | path not validated before `request.url` reconstruction | **No** — same |
| PYSEC-2026-2280 | `HTTPEndpoint` verb dispatch via `getattr` | **No** — `HTTPEndpoint` is not used |
| PYSEC-2026-2281 | `StaticFiles` UNC path, Windows only | **No** — `StaticFiles` unused; Linux container |
| PYSEC-2026-249 | `max_fields` ignored for `x-www-form-urlencoded` | **No** — JSON-only API, no form parsing |

**Not fixed here, deliberately.** Every fix version is `>= 1.0.1`, and
`fastapi==0.128.8` requires `starlette<1.0.0`. Remediation therefore needs a
FastAPI major upgrade — a framework change with nothing to do with synthesis
reliability, and outside this stage's stated boundary. Recommended as its own
task. Combined with loopback-only binding and zero reachable surfaces, the
practical risk today is low.

**Not audited:** the Debian OS layer. No `trivy` or `grype` is installed, and
`docker scout` requires a Docker Hub login, which I did not perform.

## Defects found and fixed in this stage

1. **`{action, action_input}` blobs reached users with the contract running** —
   the live-verification finding above. Vocabulary widened; regression corpus
   and literal pin added.
2. **`backend/.pytest_cache/` shipped in the image** — `.dockerignore`
   anchoring. Fixed.
3. **`LLMMessage` constructed in `chat_service`** — violated the Stage 3B
   invariant that only the formatter builds prompt messages; caught by
   `test_only_the_formatter_builds_prompt_messages`. Moved into
   `formatter.with_recovery_instruction()`.
4. **Dead `whole` flag** in `_structured_payload` — found by mutation N9,
   removed.
5. **Two "internal structure" test fixtures also carried tool keys**, so they
   classified as `TOOL_CALL`. Fixtures made unambiguous, plus a separate test
   asserting both shapes are refused either way.
6. **My own structural tests were wrong twice** — one counted a code *comment*
   as a `llm_response.content` read (fixed with AST `Attribute` matching); one
   flagged `re.compile` as dynamic execution (a recurring trap in this
   codebase, fixed with named exemptions).

## Residual risks

- **Fail-open on unknown structure.** An unrecognised JSON object is accepted.
  Refusing all JSON would break every legitimate request for structured output.
  The asymmetry is deliberate: a missed blob is an odd answer, a false refusal
  is a lost one. **This stage proved the risk is real, not theoretical** — the
  mitigation is that such failures are observable, cheap to fix, and now carry
  a regression corpus.
- **A tool call wrapped in a sentence of prose is accepted**, by design.
- **Recovery is one attempt.** A model failing twice yields a truthful failure.
- **Three pre-existing blobs remain in the database** from before the fix, in
  conversations `88f48b18…` (two, 2026-09-18) and `d30bedea…` (one,
  2026-09-19 11:44). New turns *in those two conversations* still carry them in
  history and could be imitated. I have **not** deleted them — that is your
  data and your call. Starting a new conversation avoids them entirely.
- **Tests run on Python 3.9 locally; the image runs Python 3.12.** Both were
  exercised for this stage (suite on 3.9, live Docker verification on 3.12),
  but the split is a standing hazard unrelated to 5A.2.
- **Cosmetic, not a contract matter:** the synthesis model sometimes renders
  citations as `【1†source】`, and after a Calendar answer sometimes offers to
  create events, which Mai cannot do (Calendar is read-only). Both are prose,
  so the contract correctly does not refuse them.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Internal tool-call JSON never reaches the user | **PASS** |
| Internal structure never enters conversation history | **PASS** |
| Not implemented as a naive JSON stripper | **PASS** |
| Legitimate structured answers still accepted | **PASS** (8/8 live; dedicated test set) |
| Failure is stated truthfully, from execution state | **PASS** |
| Recovery bounded | **PASS** (exactly one attempt) |
| No new tool, integration, network destination or OAuth scope | **PASS** |
| Full test suite green | **PASS** (4099 passed, 1 skipped; 0 skipped with Postgres) |
| Mutation testing with a *validated* harness | **PASS** (25/25, controls survive) |
| Static/AST audit | **PASS** |
| Docker / PostgreSQL / frontend verification | **PASS** |
| Live verification | **PASS** — and it found the defect the unit suite could not |
| Security audit | **PASS**, with one pre-existing dependency finding reported and assessed |
