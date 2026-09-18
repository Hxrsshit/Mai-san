# Stage 5A.1 — Acceptance Report

## Status: **PASS**

## Baseline

| | |
|---|---|
| Stage 5A | `c255b2d` |
| Stage 5B | `f9bb895` |
| Stage 5B acceptance report | `4e36849` (HEAD at start) |
| `git status --short` | empty |
| Baseline suite | **3756 passed, 1 skipped** — matches the stated expectation exactly |

Stage 5B verified present before any change: `google_gmail.py`,
`gmail_schemas.py`, `gmail_tools.py`, `mail_language.py`, `mail/service.py`;
registry holds `google_calendar, google_gmail, web_search`; Gmail scope
`gmail.readonly`, host `gmail.googleapis.com`, token key `google_gmail`.

**The baseline was green. Nothing was built on a red tree.**

## Implementation commit

`(recorded below)`

## Root cause

> *"what is the latest model launched by ChatGPT and is it better than the
> model by Claude?"* → answered from stale training data.

Reproduced, and not only for AI. Nine questions across nine domains all routed
to an ordinary turn where the model answered from training knowledge:
markets, CEOs, React versions, gold prices, restaurants, Netflix, iPhones,
Bitcoin.

**Every recogniser up to Stage 5B answers one question: *did the user ask me
to do something?*** Each needs a verb — "search the web for", "check my
calendar", "read my email". None answers a different question: *does answering
this correctly require information that may have changed?*

"What is the latest OpenAI model?" asks Mai to search for nothing. So the
model's own knowledge was being treated as evidence that the knowledge was
current.

## Fix

A typed, deterministic, **advisory** assessment —
`app/orchestration/freshness.py` — that runs **last**, after every other
recogniser has declined, and whose only output is a candidate query handed to
the *existing* research proposal path.

```
FreshnessAssessment(requirement, reason, source, subject)
   NOT_REQUIRED · PREFERRED · REQUIRED        NONE · WEB · CALENDAR · MAIL
```

Domain-agnostic by construction: no company, product or topic appears in the
implementation, and an AST test asserts none ever will. What is detected is
temporal *structure* — recency markers, volatile predicates ("how much is X",
"is X open", "who won"), volatile roles — which are properties of English
rather than of any industry.

Design detail: `docs/stage5a1_universal_freshness.md`.

## Tests

| | |
|---|---|
| Baseline | 3756 |
| Total | **3992** |
| Added | **236** |
| Full suite #1 (declared order) | pass |
| Full suite #2 (randomised order) | pass, identical |
| Failures | 0 |
| Skips | 1 — `TEST_POSTGRES_URL is not set`, and run explicitly against the live server (15 passed) |

New files: `tests/test_freshness.py`,
`tests/security/test_freshness_security.py`,
`tests/security/test_freshness_structure.py`; additions to
`tests/test_normalisation.py`.

## Mutation testing

**32 mutations · 32 caught · 0 survivors**, over four rounds: 20/32 → 29/32 →
31/32 → 32/32.

### The first run was invalid, and controls are why that was found

Round one reported **32/32 with zero survivors** — an implausibly clean
result. Two *control* mutations (reword a comment; change an unasserted reason
code) were run to check the harness discriminates. **Both were reported
"caught", which is impossible.**

The cause: the test set named `tests/test_mail.py`, which does not exist
(Stage 5B's file is `tests/test_gmail.py`). pytest exited non-zero for every
run, so every mutation looked caught. The real score at that point was
**20/32**.

The controls are now part of the procedure, and they pass — a comment reword
and an unasserted reason code both survive.

### Dispositions

Nine of the twelve genuine survivors were guards no test could reach, in four
recognisable shapes:

| shape | examples |
|---|---|
| **masked by an earlier guard** | the definitional and task guards — every stable case was refused later for having no signal at all, so nothing exercised them. Reached with a definitional question carrying a volatile *role*, and a task request carrying a recency marker |
| **masked by a bound** | the subject-length test used a 1065-character message, over `MAX_MESSAGE_CHARS`, so the assessor returned `message_too_long` and the assertion held regardless — *and* it read the constant it was meant to pin |
| **unreachable by any message** | `wants_web`'s source check: no assessment currently produces REQUIRED with a non-web source. Constructed directly instead; it is the check that matters the moment a second source exists |
| **never driven end to end** | the chat-turn ordering guards, and the "no provider configured" branch |

**One equivalent mutation, documented rather than dismissed.** Removing the
`mail.outcome is NOT_MAIL` check from the chat turn changes nothing
observable, and the reason is worth recording: both Gmail tools require
approval, so a mail read takes two turns, and the turn whose outcome is
COMPLETED is the one where the user typed "yes" — which carries no freshness
signal. The calendar equivalent **is** reachable precisely because a calendar
read needs no approval and completes on the turn that asked. The guard is kept
as defence in depth, and `test_a_gmail_read_always_requires_approval` pins the
policy that makes it equivalent: if Gmail ever becomes approval-free, that
test fails and the guard becomes load-bearing.

### Two real defects the survivors exposed

1. **`"what meetings do I have today?"` would have gone to the web.** It says
   "today" and names no possessive, so the assessor's own personal-scope guard
   does not fire. Only its *position* in the chain keeps it out of web
   research — which makes the ordering a load-bearing control, not a tidy
   redundancy. Now tested through the real chat turn.
2. **The mail word-allowance bound matters.** Widened from two intervening
   words to three so "what **are my latest** emails?" is recognised; at six,
   "what do you think about the new email client" becomes a request to read
   the user's mailbox.

## Defects found and fixed

| | |
|---|---|
| **The stale-answer failure** | the stage's purpose |
| **`"what are my latest emails?"` reached no handler** | the Stage 5B mail grammar allowed two intervening words; this needs three |
| **`"what changed in Gmail recently?"` was claimed by Gmail** | a product/news question about Gmail, not a mailbox request. A product-question guard (change/feature/release word with no possessive) sends it to web research |
| **Three over-matches the existing suite caught** | "How are you today?", "which LLM provider am I currently using?", "what technology stack am I currently using for Mai?" all carry a marker and none is about the world. Fixed with a participant-subject guard |

### Not fixed here — verified here

`"google Gmail API documentation"` reaching web research is often attributed
to this stage in passing. It is not: the Stage 5A product-name guard broke it
and **Stage 5B fixed it**, by anchoring `google` so that it is a search verb
only when it opens the request. `backend/app/research/language.py` is
unchanged by Stage 5A.1 and `git diff` confirms it.

What this stage did was re-verify the property, since freshness routing runs
immediately after the research grammar and a regression there would surface as
a freshness bug:

```
google Gmail API documentation          -> research   (search verb, opens the request)
what is on my google calendar tomorrow? -> calendar   (noun phrase)
add google calendar to my phone         -> not research
google quantum computing                -> research
```

## Live verification

Real Docker stack, real PostgreSQL, real Tavily, real connected Google
Calendar, Gmail not connected.

| # | request | result |
|---|---|---|
| 1 | `what is the latest model launched by ChatGPT?` | proposal → **yes** → real search, 5 results, prose citing CNBC with 2025 dates — not training data |
| 2 | `what is the latest model by Claude?` | research proposal, query `latest model by Claude` |
| 3 | `what is the latest iPhone?` | research proposal, query `latest iPhone` |
| 4 | `what happened in AI today?` | research proposal |
| 5 | `what is the current price of gold?` | research proposal — no specialised provider exists, so the permitted web path is used |
| 6 | `what is on my Google Calendar tomorrow?` | **CALENDAR/completed** — not web |
| 7 | `what are my latest emails?` | **GMAIL/not_connected** — truthful, not web |
| 8 | `what is the latest Gmail feature?` | research — not Gmail |
| 9 | `search the web for the latest news about OpenAI` | explicit path unchanged, query `the latest news about OpenAI` |
| 10 | `what is Python?` | ordinary — no search |
| 11 | `what is the latest Python version?` | research proposal |
| 12 | `what is the lates OpenAI model?` | research, query repaired to `latest OpenAI model` |

**Consent:** every one was a proposal. Zero external requests before "yes".

**Memory:** measured across a confirmed search — memories 26→26, entities
23→23, relationships 19→19.

**Credentials:** 0 occurrences of the live search key, Groq key or OAuth
client secret in container logs, `messages`, `executions`, `execution_events`
or `memories`; 0 `ya29.`/`tvly-` strings in the database.

## Frontend

Proposal, consent and sourced answer all render correctly in a fresh
conversation (verified with `what is the latest MacBook?` — a table of sourced
results with citations). Integration panel shows Calendar *Connected* / Gmail
*Not connected*. `localStorage`, `sessionStorage` and cookies empty; zero
matches for `ya29.`, `tvly-`, `gsk_`, `GOCSPX`, `access_token`,
`refresh_token`, `authorization`, `fingerprint`, `execution_id`. **Zero
console errors.**

## Docker / PostgreSQL

| | |
|---|---|
| Runtime image | 29 packages, **no pytest, no tests, no `.env`**, no secret-shaped strings |
| New dependencies | **none** — the assessor uses `re` and `enum` |
| Advisories | 5 unique, all `starlette` 0.52.1 — unchanged from Stage 5B, none reachable |
| Migrations | `0009 (head)`; **no migration added** |
| PostgreSQL chain | run explicitly against the live server: 15 passed |
| Ports | loopback-only on all three services |

## Structural audit

`tests/security/test_freshness_structure.py`, by AST:

- freshness imports no `app.execution`, `app.tools`, `app.integrations`,
  `app.llm`, `app.research.service`, `app.calendar`, `app.mail`
- no `httpx`, `requests`, `aiohttp`, `socket`, `urllib`, `sqlalchemy`
- no URL, no HTTP verb, no destination string
- no model call, no `eval`/`exec`/`getattr`
- names no tool, no integration, no `ExecutionRequest`, no `register`
- registries and permitted outbound hosts **unchanged** by the stage
- `app.orchestration.freshness` is imported by exactly **one** module
- `assess_freshness(` appears exactly **once**, passing `reading.text`
- no other module reimplements freshness keywords

## Security audit

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |
| Informational | 3 |

| review area | finding |
|---|---|
| Intent boundary | assessment runs on the user's message only; one call site, asserted structurally |
| Model authority | the model is not consulted; no `needs_web` field exists anywhere |
| Authorization / consent | freshness produces a query; `_propose_query` is shared with the typed path so there is one place a search becomes proposable |
| Network / SSRF | no destination added; a user-supplied URL becomes a search *term*, never a host |
| Prompt injection | hostile calendar titles, email bodies and web results tested — none creates research |
| Recursive research | no edge from tool output to intent; a hostile search result produces exactly one request |
| Calendar / Gmail isolation | ordering plus the assessor's own personal-scope guard; both tested |
| DoS / bounds | 1000-char input cap, 240-char subject cap, no model/network/DB call, hostile-input timing test |
| Memory isolation | measured live before and after |
| Capability truthfulness | unavailable research says so and does not answer stale |
| Logging | query *length* only, never the query |

**Informational:**

1. **Ambiguity resolves towards not searching.** "What are Nvidia's results?"
   is answered normally. The cost of guessing wrong is an unwanted external
   request.
2. **`PREFERRED` affects wording only**, from one narrow rule. Deliberately
   small — the brief warns against unreliable taxonomies.
3. **The model occasionally emits a tool-call JSON blob instead of prose**, and
   once it does, the blob is in the conversation history and gets imitated on
   later turns. Reproduced in the browser; **not introduced by this stage** —
   it occurs identically on the pre-existing explicit research path, and a
   fresh conversation produces correct sourced prose. Recorded as a residual
   risk rather than fixed here, since the fix belongs to the synthesis prompt.

## Residual risks

- The synthesis JSON-blob contamination above.
- A long compound question produces a long query, carried verbatim.
- No conversation context: "what about now?" is not recognised as a follow-up.
- The five `starlette` advisories, unreachable but present, awaiting a FastAPI
  major upgrade.

## Acceptance criteria

All met. Specifically: freshness is domain-agnostic (asserted by AST);
Calendar and Gmail take precedence (structurally, and tested); freshness
grants no authorization, bypasses no consent, executes no tool, creates no URL
and adds no network path; the existing Tavily infrastructure is reused
unchanged; queries preserve user intent; failed and unavailable research are
reported truthfully; external content cannot trigger routing; there is no
research recursion; Stage 5A typo tolerance is intact and still closed-
vocabulary; Gmail and Calendar remain intact.

## Recommendation

**PASS.** Stage 5A.1 is complete. Stopping here — Stage 5C is not started.
