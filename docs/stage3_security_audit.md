# Mai — Stage 3 Full-System Security & Privacy Audit

**SECURITY SIGN-OFF: APPROVED WITH DOCUMENTED LIMITATIONS**

---

## Executive summary

Mai's application layer holds up well under attack. Injection, mass assignment,
lifecycle manipulation and prompt-injection attempts all fail structurally
rather than by luck, and the boundaries introduced in Stages 3A–3C do the work
they were designed to do.

The real weaknesses were **not in the application** — they were in the
infrastructure around it and in what leaves the process. Four findings were
rated HIGH or MEDIUM and all four are fixed:

| ID | Severity | Finding |
| --- | --- | --- |
| I-01 | **HIGH** | No `.dockerignore`; `COPY backend/ ./` would bake the live API key into an image layer |
| I-02 | **HIGH** | Postgres and the unauthenticated API published on `0.0.0.0`, database password defaulting to `mai` |
| S-01 | **MEDIUM** | At `LOG_LEVEL=DEBUG` the database driver logged every statement *with bound parameters* — the full text of every message, memory and title |
| S-02 | **MEDIUM** | 33 call sites copy `str(exc)` into structured log fields; a driver exception embeds the connection DSN, carrying the database password into logs |

Three further LOW findings were also fixed (unbounded reflected request id,
unredacted error envelope, CORS wildcard with credentials).

**No secret was ever committed.** Tracked files and the full Git history are
clean across eight credential patterns. The live Groq key lives only in
`backend/.env`, which is ignored and mode `600`.

296 security tests were added and executed. Total suite: **1205 passing**.

Not verified, and not claimed: Docker runtime, PostgreSQL runtime, frontend
runtime, and dependency CVE scanning. See *Known limitations*.

---

## Scope

Stages 1, 2A, 2B, 2C, 2D, 3A, 3B and 3C, at commit `8e71003` — 896 tests
passing, clean working tree, verified before any change was made.

Audited: backend application, API surface (25 endpoints), configuration,
logging, database schema and constraints, background pipeline, LLM provider
integration, Docker and compose configuration, frontend source, Git history,
and dependency manifests.

---

## Methodology

The standard applied was **executed, not assumed**.

| Approach | Used for |
| --- | --- |
| **Executed attack** | SQL injection, malformed input, invalid ids, mass assignment, prompt injection, memory poisoning, lifecycle manipulation, resource exhaustion |
| **Induced failure** | Provider errors, database failures, extraction failures, formatting failures, unhandled exceptions |
| **Runtime inspection** | Log output through *both* formatters, response bodies, outbound provider payloads |
| **Structural test** | AST scans for import boundaries, `LLMMessage` construction sites, `print` calls, log field names |
| **Static review** | Docker, compose, frontend, dependency manifests |

Two mutation checks confirmed the new guards have teeth: reverting the Docker
fixes fails nine infrastructure tests, and the pre-fix logging state fails five
privacy tests.

Where infrastructure was unavailable, the result is recorded as **NOT
VERIFIED** rather than PASS.

---

## Threat model

### Assets

| Asset | Sensitivity |
| --- | --- |
| Conversations and messages | High — unfiltered personal speech |
| Memories | High — distilled personal facts, deliberately long-lived |
| Entity and relationship graph | High — a map of the user's life and work |
| Conflict / lifecycle metadata | Medium — reveals what changed and when |
| Provider API key | High — billable, and grants use of the user's account |
| Database credentials | High — full read/write over everything above |

### Threat actors

| Actor | Capability today |
| --- | --- |
| **Malicious content in stored knowledge** | Text reaching the model on every relevant turn. The primary modelled adversary. |
| **Another process/user on the host** | Full filesystem access. `backend/.env` is mode 600; the SQLite database is not encrypted. |
| **Someone on the local network** | Was able to reach the API and database directly (finding I-02). Now loopback-only. |
| **A compromised dependency** | Full in-process access. Not scannable here — see limitations. |
| **Accidental developer exposure** | Committing a key, pushing an image, pasting a key. Covered by executed tests and by `.dockerignore`. |
| **A future second user** | Not defended against at all. Nothing is ownership-scoped. |

### Attack surfaces

```
Git repository ──► secret exposure          [tested, clean]
HTTP API ────────► injection, mass assignment, IDOR   [tested]
Stored knowledge ► prompt injection, poisoning        [tested]
Lifecycle ───────► supersession abuse                 [tested]
Logs ────────────► credential and content leakage     [tested, 2 fixed]
LLM provider ────► third-party data disclosure        [mapped, enforced]
Docker/compose ──► network exposure, secrets in image [static, 2 fixed]
Frontend ────────► XSS, client-visible secrets        [static, clean]
Dependencies ────► supply chain                       [NOT VERIFIED]
```

### Trust boundaries

```
untrusted   User input ──────────┐
                                 ▼
trusted     Application ─────► Database        (parameterised, constrained)
                                 │
trusted     Database ─────────► Context assembly
                                 │  ← re-enters as UNTRUSTED here
untrusted   Retrieved knowledge ─┴─► Prompt (reference block only)
                                 ▼
external    LLM provider          (third party; disclosure mapped below)
                                 
internal    Logs                  (metadata only; redacted)
```

The load-bearing boundary is the third one: **data leaving the database and
re-entering the prompt is treated as untrusted again**, even though the
application wrote it. That is what makes stored prompt injection containable.

---

## Secrets audit

All executed. **No secret found in any tracked file or anywhere in history.**

| Check | Result |
| --- | --- |
| 8 credential patterns × all tracked files | **0 matches** |
| 8 credential patterns × all reachable commits (`git log -S --pickaxe-regex`) | **0 matches** |
| `.env` tracked | **none** (only `.env.example`) |
| `.env` ever committed | **none** |
| `backend/.env` ignored | **yes**, and mode `600` |
| Example env assigns a credential value | **none** after fix |
| Credential shapes in test fixtures | **0** |

Patterns: `gsk_`, `ghp_`, `github_pat_`, `sk-or-v1-`, `sk-`, `AKIA`,
`BEGIN … PRIVATE KEY`, `xox[baprs]-`.

All 21 checks run as tests (`tests/security/test_secrets.py`), so a future
commit fails the suite rather than waiting for the next audit.

### Live key status

One live provider key exists, in `backend/.env` only. It is masked here as
`gsk_jN…n8Uc` and is **not** in the repository or its history.

It is nonetheless **compromised**: it was pasted into a chat transcript during
development. So were two OpenRouter keys and one GitHub token. **All four
should be revoked and reissued.** No history rewriting is required — nothing
reached Git.

---

## API security

25 endpoints. 173 executed attack tests.

| Attack | Result |
| --- | --- |
| SQL injection — 8 payloads × 3 vectors (messages, titles, search) | **All inert.** Stored as literal text; row counts unchanged; every table still queryable afterwards |
| Malformed ids — 10 shapes × 10 endpoints | 404/422; no traceback, SQL, driver name or filesystem path in any body |
| Unknown-but-valid UUIDs × 10 endpoints | 404, never 500 |
| Malformed JSON | 422, clean envelope |
| Wrong types / null / empty — 8 shapes | 422, no payload echoed |
| Oversized payloads | Rejected by schema; error body under 2 KB |
| Unicode, bidi overrides, embedded nulls, CRLF | Stored byte-for-byte (bar documented boundary `.strip()`) |
| Mass assignment — forged `id`, `created_at`, `role` | **Ignored.** Server-assigned values win |
| Lifecycle fields via request body (`status`, `confidence_score`, `superseded_by`, `context_role`) | **Ignored.** Values come from extraction only |
| Write routes for knowledge | **None exist.** `POST`/`PUT`/`PATCH` on memories/entities/relationships → 404/405 |
| Unsupported methods | 404/405, no traceback |
| Duplicate identical requests | No state corruption |

The strongest structural property: **knowledge is derived, never
client-authored.** No route accepts a memory, entity, relationship or lifecycle
link as input, so mass assignment has nothing to aim at.

---

## Error handling and information leakage

Failures induced at every layer; responses inspected against a 15-marker leak
list (`traceback`, `sqlalchemy`, `asyncpg`, `select `, `site-packages`,
`/Users/`, `gsk_`, …).

| Induced failure | Response |
| --- | --- |
| Provider timeout / rate limit / auth / malformed / generic | 4xx–5xx, stable `{code, message, request_id}` envelope |
| Database unavailable | 500/503, no DSN, no driver detail |
| Unhandled `ZeroDivisionError` | 500 `internal_error`; the exception text never reaches the client |
| Validation failure | 422 with the field and reason, never the rejected value |

The error envelope is fixed-shape — exactly three keys — so an exception cannot
widen it.

**The provider's auth path is the strongest control here:** on 401/403 the
upstream message is *discarded entirely*, so an API that echoes part of a
rejected key ("Incorrect API key provided: sk-…XYZ") cannot reach the client.
Redaction (below) covers the generic 4xx branch, which does pass upstream text.

---

## Logging and privacy

Both formatters were exercised on real requests and real failures.

**Application logging was already clean**: every `logger` call passes ids,
counts, durations and status codes. An AST test now bans field names that would
carry content (`content`, `user_message`, `title`, `api_key`, …), and another
bans `print` outright.

Two findings, both fixed:

### S-01 (MEDIUM) — driver logs leaked the entire knowledge base at DEBUG

`configure_logging` pinned `sqlalchemy.engine` to WARNING but left `aiosqlite`,
`asyncpg`, `sqlalchemy.pool` and `httpx` at the application level. At
`LOG_LEVEL=DEBUG` — an ordinary thing to set while troubleshooting — the driver
emitted every statement *with bound parameters*: full message text, memory
content, conversation titles, entity names.

**Reproduced** by capturing at DEBUG and asserting private strings were absent;
three tests failed. **Fixed** by pinning eight data-carrying loggers to WARNING
in `configure_logging`. **Regression tests**: seven parametrised checks that
each stays pinned at DEBUG, plus one that application loggers are *not* pinned.

### S-02 (MEDIUM) — exception text carried the database password into logs

33 call sites do `extra={"error": str(exc)}` and 19 attach `exc_info`. A driver
exception routinely embeds the connection DSN, so a PostgreSQL deployment would
write `postgresql+asyncpg://mai:PASSWORD@db/mai` into a *structured* field —
indexed and searchable in any log aggregator.

**Reproduced** with a realistic DSN; the password appeared in rendered output.
**Fixed** with a `redact()` pass applied in **both formatters** rather than at
the 33 call sites — one choke point that also covers tracebacks and any future
call site. It masks URL credentials and seven key shapes.

Verified absent from real log output: message bodies, assistant replies, memory
content, retrieved knowledge, conversation titles, rejected input, API keys,
and the database URL.

---

## Personal data exposure map

```
                  ┌─────────────────────────────────────────────┐
   DATABASE ──────┤                                             │
                  ▼                                             ▼
          API responses                                  LLM provider
    (full content, unauthenticated,                (5 categories, enforced)
     loopback-only after I-02)                             │
                  │                                        │
                  ▼                                        ▼
            Debug endpoints                          Logs (metadata
      (no secrets; no system prompt text)             only, redacted)
                                                           │
                                                           ▼
                                                    Errors (fixed
                                                   envelope, redacted)
```

| Path | What leaves | Necessary? | Controls |
| --- | --- | --- | --- |
| API responses | Everything stored | Yes — it is the UI's data source | Loopback-only; no auth (see multi-user risks) |
| LLM provider | 5 categories below | Yes — the product is an LLM assistant | Enforced allowlist; superseded knowledge withheld; both paths disableable |
| Logs | Metadata only | Yes — operability | Field-name ban, redaction, driver pinning |
| Debug endpoints | Knowledge already on the API, plus lifecycle metadata | Yes — inspectability | System-prompt text withheld; no ids in prompt debug; read-only |
| Errors | Classification and request id | Yes | Fixed envelope, redacted |

---

## LLM provider disclosure

Mai sends personal data to Groq by design. The categories are enumerated and
enforced by test.

**Request path — one call per turn:**

1. The application system prompt *(not user data)*
2. The current user message, verbatim
3. Recent conversation from **this thread only**
4. Retrieved memories — short extracted statements, ranked and budgeted
5. Retrieved entities and relationships — names, types, and the claim

**Background path — up to three calls per turn:** the current turn's user and
assistant message, for memory, entity and relationship extraction.

Enforced and verified:

- No database identifier, score, rank, timestamp or status reaches the provider.
- No configuration or credential appears as message content.
- **Another conversation is never sent verbatim.** Cross-conversation recall
  travels as extracted memories, never raw transcripts — the difference between
  sending a fact and sending a transcript.
- **Superseded knowledge is withheld** on ordinary questions.
- Extraction sees only the turn it was given, not conversation history.
- The credential travels in an `Authorization` header, never a URL — checked by
  AST over string literals, so a query-string key cannot be added quietly.
- `RETRIEVAL_ENABLED=false` shrinks disclosure to the current thread;
  `MEMORY_EXTRACTION_ENABLED=false` closes the background path entirely.

---

## Prompt injection and context security

Eleven payload classes were stored and retrieved through the real pipeline:
instruction override, system-prompt extraction, secret extraction, role
reassignment, fake developer instructions, context escaping, and destructive
requests — injected via memories, entity names, entity descriptions,
relationship targets, and the current user message.

**All structurally contained.** Four invariants hold for every payload:

1. it never becomes a system message;
2. it appears only inside the reference block or the user's own turn;
3. it never changes any message's role;
4. it never displaces or alters the current user message.

Three mechanisms, unchanged from 3A–3C and re-verified here:

- only `ContextRole.REFERENCE` items render — anything else is dropped, never
  promoted;
- stored conversation rows may occupy only `user`/`assistant`, so database text
  cannot become an instruction;
- every knowledge line is flattened, so retrieved text cannot forge the block's
  headings or appear to close the section.

None of this depends on the model refusing. That is the point.

### Single knowledge injection path

Re-verified after Stage 3C by two independent means: a behavioural spy on the
retired Stage 2D renderer during a real chat turn (never called), and an AST
scan asserting the set of modules constructing an `LLMMessage` is exactly the
formatter, the three background extractors, and the provider health probe.

---

## Memory poisoning

Eight poisoning payloads were stored through the real extraction pipeline and
then retrieved.

- A poisoned memory is retrievable, and remains **data**. It never becomes a
  system message and its `context_role` stays `reference`.
- Hostile *metadata* from the model — `importance_score: 999`,
  `confidence_score: 42.0`, `status: archived`, `context_role: instruction`,
  a forged `id` — is clamped by validation and database `CHECK` constraints.
  Stored values come from validated extraction, never from the payload.

---

## Knowledge lifecycle security

Stage 3C can retire knowledge, which makes it an integrity target.

| Attack | Result |
| --- | --- |
| **Mass supersession** — 6 phrasings ("I replaced everything with this", "I no longer use anything") | **Retires nothing.** Supersession requires a resolvable named entity; a quantifier resolves to none |
| **Cascade beyond subject** — legitimate switch with 4 unrelated memories present | Exactly 1 retired; the other 4 stay ACTIVE |
| **Alias lookalike** — "Groq Inc" used to retire "Groq" | **Fails.** Resolution is exact on normalised name or alias, never fuzzy |
| **Alias hijack** — one alias for two entities | **Refused** by a UNIQUE index; ambiguous resolution is worse than none |
| **Abandonment over-reach** — "no longer uses X" with unrelated memories | Only the named subject retired |
| **External lifecycle control** — 5 route/method combinations | **No route exists.** 404/405; zero links created; all statuses unchanged |
| **Self-supersession** | Refused and counted |
| **Circular chains** (A→B→C→A) | Refused at every length |
| **Broken references** | Refused by foreign keys |
| **Debug endpoint mutation** | Read-only across repeated calls |

---

## Database integrity

| Invariant | Verified |
| --- | --- |
| Foreign keys enforced (SQLite `PRAGMA foreign_keys`) | Orphan message refused |
| Conversation delete cascades | Messages, memories, entity links, evidence and lifecycle rows all removed; **zero orphans** |
| Evidence needs a real relationship | Refused |
| Lifecycle link needs a real memory | Refused |
| No self-relationship | Refused |
| Score ranges (4 out-of-range cases) | Refused by `CHECK` |
| Failed turn leaves no partial conversation | 0 messages after a provider timeout |
| Failed extraction leaves no partial knowledge | Memory committed, 0 entity links, 0 relationships |
| Repeated failures do not advance state | 0 messages, 0 memories after 3 |

**Concurrency** (file-backed database — the in-memory fixture shares one
connection through StaticPool and cannot model contention):

- 6 concurrent identical turns → 1 memory, no duplicate entity, ≤1 relationship
- 5 concurrent deletes of one record → exactly one 204, rest 404, **no 500**
- Delete racing two reads → no message outlives its conversation

---

## Resource exhaustion

Application-level only, as scoped.

| Stress | Result |
| --- | --- |
| 200 matching memories | Prompt capped at the category limits; total under 40 KB |
| 40-turn conversation with large messages | ≤ `CONTEXT_RECENT_MESSAGE_LIMIT` + 3 messages; under 60 KB |
| Maximal 32,000-character message | Accepted; prompt grows by a bounded amount only |
| 150 entities, 150 memories, 4,000-character query | Candidate pool ≤ `RETRIEVAL_CANDIDATE_POOL_SIZE`; selections ≤ limits; context chars ≤ budget |
| 10,000-character `X-Request-ID` | Truncated to 64 characters (fixed — see below) |

---

## Dependency security

**NOT VERIFIED.** No scanner is installed and no Node toolchain exists on this
machine: `pip-audit`, `safety` and `bandit` are all absent, and `npm` is
unavailable. No CVE claim is made in either direction.

To verify: `pip install pip-audit && pip-audit -r backend/requirements.txt`,
and `npm audit` in `frontend/` once Node is installed.

Static observations:

| Observation | Severity |
| --- | --- |
| All 13 direct backend dependencies pinned exactly (`==`) | Good |
| **No transitive lockfile** — `starlette`, `anyio`, `certifi`, `idna` etc. float on rebuild | LOW |
| **No `frontend/package-lock.json`** — the Dockerfile falls back to `npm install`, resolving fresh versions at build time | LOW |
| Test dependencies (`pytest`, `pytest-asyncio`, `aiosqlite`) share `requirements.txt` and install into the production image | LOW |

None is exploitable today; all three widen the supply-chain surface and are
recorded as future work.

---

## Docker security

**STATIC REVIEW: PASS (after fixes). RUNTIME VERIFICATION: NOT VERIFIED** —
Docker is not installed on this machine and no image was built or run.

### I-01 (HIGH) — the live API key would be baked into an image layer

`docker/backend/Dockerfile` does `COPY backend/ ./` with **no `.dockerignore`
present**. Confirmed that the build context included `backend/.env` (holding a
live `gsk_` key) and a 49 MB `.venv`. An image layer is permanent: deleting the
file in a later layer does not remove it, and `docker history`, layer
extraction or a registry push exposes it.

**Fixed** by adding `.dockerignore` excluding `.env` (and `**/.env`), `*.db`,
`.venv/`, `node_modules/`, `.git/` and caches. Secrets reach the container
through compose `environment:` only. **9 regression tests**, confirmed to fail
against the pre-fix state.

### I-02 (HIGH) — services on every interface, database password defaulting to `mai`

`ports: "5432:5432"` binds `0.0.0.0`, and `POSTGRES_PASSWORD: ${…:-mai}` with
user `mai` and database `mai`. Combined: anyone reachable on the network could
read every message, memory and relationship. The backend was likewise on
`0.0.0.0` — and the API has **no authentication at all**, including `DELETE`
routes.

**Fixed**: all three published ports bound to `127.0.0.1`, and
`POSTGRES_PASSWORD` made required (`${…:?}`) with the default removed from
`.env.example`. **Regression tests** assert every mapping is loopback-bound and
that no credential is defaulted or hardcoded.

### Already sound

Both images run as non-root (`mai` uid 1000; `nextjs` uid 1001), the frontend
uses a multi-stage build with a standalone output, no secret is passed as a
build `ARG`, and the database uses a named volume rather than a host mount.

---

## Frontend security

**STATIC REVIEW: PASS. RUNTIME VERIFICATION: NOT VERIFIED** — no Node
toolchain; the frontend has never been built or run.

| Check | Result |
| --- | --- |
| Secret in a `NEXT_PUBLIC_*` variable | **None.** Only `NEXT_PUBLIC_API_URL` |
| `dangerouslySetInnerHTML` | **None** |
| `innerHTML`, `outerHTML`, `document.write`, `eval`, `new Function` | **None** |
| `localStorage`, `sessionStorage`, `document.cookie` | **None** — no personal data persisted client-side |
| Hardcoded non-local URLs | **None** |

Model output and memory text are therefore rendered as text, not markup, which
is what keeps a hostile memory from becoming stored XSS.

---

## Future multi-user risks

Mai is single-user by design and **is not multi-user secure**. Authentication
was explicitly out of scope. These are blockers for any future auth stage:

| # | Blocker | Why it matters |
| --- | --- | --- |
| **M-1** | **No ownership column anywhere.** All nine tables verified: no `user_id`, `owner_id`, `account_id` or `tenant_id` | Every query is global. Adding auth requires a schema migration and a filter on every read |
| **M-2** | **No authentication or authorization on any of the 25 endpoints** | Reachability equals full access, including `DELETE` |
| **M-3** | **Every id is a direct object reference.** `GET /api/memories/{id}` returns any memory to any caller | Becomes IDOR the moment a second user exists |
| **M-4** | **Debug endpoints return global data.** `/api/retrieval/debug` and `/api/context/debug` search the whole knowledge base | Would leak across users; should be gated by environment as well as ownership |
| **M-5** | **The entity graph is globally unique.** `uq_entities_normalized_name` is global, so two users could not both have a private "Mai" | Uniqueness must become per-owner |
| **M-6** | **Conflict evaluation is global.** Supersession searches all memories | One user's message could retire another's knowledge |
| **M-7** | **Retrieval is global.** Candidate collection has no ownership predicate | The core leak path if auth is added without scoping |

Recommended order: schema ownership (M-1, M-5) → request-scoped identity
(M-2) → filters on retrieval and conflicts (M-6, M-7) → object-level checks
(M-3) → debug gating (M-4).

---

## Vulnerabilities found

### I-01 — HIGH — Live API key baked into Docker image
**Reproduced:** build context inspection confirmed `backend/.env` (containing a
live `gsk_` key) and `.venv` were included by `COPY backend/ ./` with no
`.dockerignore`.
**Fix:** added `.dockerignore`.
**Regression:** `test_infrastructure.py` — 9 tests; verified failing pre-fix.

### I-02 — HIGH — Database and unauthenticated API published on all interfaces
**Reproduced:** `docker-compose.yml` bound `0.0.0.0` for all three services;
`POSTGRES_PASSWORD` defaulted to `mai`.
**Fix:** all ports bound to `127.0.0.1`; database password made required;
default removed from `.env.example`.
**Regression:** loopback and no-default tests; verified failing pre-fix.

### S-01 — MEDIUM — Full personal data in logs at DEBUG
**Reproduced:** capturing at DEBUG showed `aiosqlite` emitting INSERT
statements with bound parameter values — message and memory text.
**Fix:** eight data-carrying third-party loggers pinned to WARNING.
**Regression:** 8 tests, including one asserting application loggers stay
unpinned.

### S-02 — MEDIUM — Database password reachable through structured log fields
**Reproduced:** an induced driver error carrying a realistic DSN appeared in
rendered log output with the password intact.
**Fix:** `redact()` applied in both formatters — one choke point for all 33
call sites and 19 tracebacks.
**Regression:** asserts the password is absent and `mai:***@db` present.

### C-01 — LOW — Unbounded client-controlled request id reflected into logs
**Reproduced:** a 10,000-character `X-Request-ID` was echoed back verbatim and
repeated in every log line for the request.
**Fix:** `_sanitise_request_id` — allowlist charset, 64-character cap.
**Regression:** bounded-echo and CRLF/null/oversize sanitisation tests.

### C-02 — LOW — Upstream error text reflected to the client unredacted
**Reproduced:** an error message carrying a `gsk_`-shaped value reached the
response body. Note the auth path already discarded upstream text; this affects
the generic 4xx branch.
**Fix:** error envelope messages passed through `redact()`.
**Regression:** asserts the shape is masked to `gsk_***`.

### C-03 — LOW — Test dependencies ship in the production image
Documented, not fixed — see *Known limitations*.

### C-04 — LOW — CORS wildcard combined with credentials
**Reproduced by inspection:** `allow_credentials=True` was unconditional, and
Starlette resolves `allow_origins=["*"]` by echoing the caller's origin — so
`CORS_ORIGINS=*` would let any website read every endpoint from a visitor's
browser.
**Fix:** credentials are dropped when the origin list contains `*`, with a
warning. The default list stays explicit.
**Regression:** three tests covering default, wildcard and explicit cases.

---

## Known limitations

**Not verified — infrastructure unavailable:**

- **Docker runtime.** Not installed. No image built or run. Static review only.
- **PostgreSQL runtime.** No server available. All database tests run on
  SQLite; migration `0006` round-trips on SQLite only.
- **Frontend runtime.** No Node toolchain. Never built or run. Static review
  only.
- **Dependency CVE scanning.** No scanner installed, no `npm`. No claim made.

**Accepted, documented:**

- **The SQLite/PostgreSQL database is not encrypted at rest.** Anyone with
  filesystem access reads everything. Appropriate for a single-user local
  system; revisit before any shared or hosted deployment.
- **No authentication.** By design at this stage; mitigated by loopback-only
  binding. See multi-user blockers.
- **Redaction is shape-based.** It masks credentials that look like
  credentials. A secret indistinguishable from prose cannot be caught by any
  pattern — which is why the real control is that application code logs ids and
  counts, with redaction as the net beneath it.
- **`/docs` and `/openapi.json` are enabled.** Useful locally, and now
  loopback-only. Should be disabled when `APP_ENV != development`.
- **Test dependencies install into the production image** (C-03). Splitting
  `requirements.txt` is deferred to avoid churning a build that has never been
  executed.
- **No transitive lockfiles** on either backend or frontend.

**Outstanding action for the user, outside the codebase:** four credentials
were exposed in a chat transcript during development — one Groq key, two
OpenRouter keys, one GitHub token. None reached Git. All should be revoked.

---

## Final security verdict

**SECURITY SIGN-OFF: APPROVED WITH DOCUMENTED LIMITATIONS**

Every HIGH and MEDIUM finding is fixed with an executed regression test. The
application's own boundaries — privilege separation, the single knowledge
injection path, derived-only knowledge, database-level integrity — held under
attack without modification.

The limitations are honest gaps in *verification*, not known defects: three
runtimes and one scanner could not be executed on this machine. They are listed
as NOT VERIFIED and must not be read as passing.

Approved to proceed to Stage 4, with the multi-user blockers treated as
prerequisites for any authentication work and the exposed credentials revoked.
