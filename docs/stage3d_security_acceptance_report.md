# Mai — Stage 3D Security Acceptance Report

**Status: PASS WITH DOCUMENTED LIMITATIONS**

All 40 acceptance criteria met. Every HIGH and MEDIUM finding fixed with an
executed regression test. Four runtime verification gaps are recorded as NOT
VERIFIED and are not claimed as passing.

Full audit: [`stage3_security_audit.md`](stage3_security_audit.md).

---

## Test summary

| | |
| --- | --- |
| **Total** | **1205** |
| **Passed** | **1205** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Baseline before Stage 3D (`8e71003`) | 896 |
| **Security tests added** | **309** |

| Security file | Tests | Covers |
| --- | --- | --- |
| `test_api_security.py` | 173 | SQL injection, malformed ids, wrong types, oversized payloads, unicode, mass assignment, methods, duplicates |
| `test_knowledge_attacks.py` | 29 | Memory poisoning, mass supersession, alias manipulation, external lifecycle control, resource bounds |
| `test_config_and_errors.py` | 21 | Configuration failure modes, error leakage, request-id sanitisation, CORS |
| `test_secrets.py` | 21 | Tracked-file and full-history scans, `.env` hygiene, fixture credentials |
| `test_infrastructure.py` | 20 | Docker/compose static review, frontend static review |
| `test_logging_privacy.py` | 20 | Real log output through both formatters, driver containment, source policy |
| `test_database_integrity.py` | 15 | Foreign keys, cascades, constraints, transaction failure, concurrency |
| `test_provider_disclosure.py` | 10 | Outbound payload allowlist, transport, disclosure controls |

No existing test required modification.

---

## Security findings

| Severity | Count | Status |
| --- | --- | --- |
| **CRITICAL** | 0 | — |
| **HIGH** | 2 | **Both fixed** |
| **MEDIUM** | 2 | **Both fixed** |
| **LOW** | 4 | 3 fixed, 1 documented |
| **INFORMATIONAL** | 4 | Documented |

### HIGH

**I-01 — Live API key would be baked into the Docker image.**
No `.dockerignore` existed and `docker/backend/Dockerfile` does
`COPY backend/ ./`. Confirmed the build context included `backend/.env` holding
a live `gsk_` key. Image layers are permanent. **Fixed** with `.dockerignore`;
9 regression tests, verified failing against the pre-fix state.

**I-02 — Database and unauthenticated API published on all interfaces.**
`ports: "5432:5432"` and `"8000:8000"` bind `0.0.0.0`, with
`POSTGRES_PASSWORD` defaulting to `mai`. The API has no authentication and
exposes `DELETE` routes. **Fixed**: all ports bound to `127.0.0.1`, database
password made required, default removed from `.env.example`.

### MEDIUM

**S-01 — Entire knowledge base written to logs at `LOG_LEVEL=DEBUG`.**
`aiosqlite`/`asyncpg` logged every statement with bound parameters — message
text, memory content, titles, entity names. **Fixed** by pinning eight
data-carrying loggers to WARNING; 8 regression tests.

**S-02 — Database password reachable through structured log fields.**
33 call sites copy `str(exc)` into `extra`; driver exceptions embed the DSN.
**Fixed** with `redact()` applied in both formatters — one choke point covering
all call sites and tracebacks.

### LOW

| ID | Finding | Status |
| --- | --- | --- |
| C-01 | Unbounded client-controlled `X-Request-ID` reflected into logs and responses | **Fixed** — allowlist charset, 64-char cap |
| C-02 | Upstream error text reflected unredacted (generic 4xx branch only) | **Fixed** — envelope redaction |
| C-03 | Test dependencies install into the production image | Documented; deferred |
| C-04 | `CORS_ORIGINS=*` would combine a wildcard with credentials | **Fixed** — credentials dropped on wildcard |

### Informational

No transitive lockfile (backend or frontend); database not encrypted at rest;
`/docs` enabled in all environments; message content is `.strip()`ed at the API
boundary before storage.

---

## Fixes applied

| # | Change | File |
| --- | --- | --- |
| 1 | Build-context exclusions for secrets, databases, venvs | `.dockerignore` (new) |
| 2 | All published ports bound to loopback; DB password required | `docker-compose.yml` |
| 3 | Database password default removed | `.env.example` |
| 4 | Eight data-carrying loggers pinned to WARNING | `app/core/logging.py` |
| 5 | `redact()` applied in both formatters | `app/core/logging.py` |
| 6 | Request-id sanitisation and length cap | `app/api/middleware.py` |
| 7 | Error envelope messages redacted | `app/api/errors.py` |
| 8 | CORS credentials dropped on a wildcard origin | `app/main.py` |

No application logic was changed. Every fix is a boundary control.

---

## Executed security checks

All 32 required checks were performed. Classification is exact.

### EXECUTED (attacks run, failures induced, output inspected)

| # | Check | Result |
| --- | --- | --- |
| 1 | Secret scan — 8 patterns × all tracked files | 0 matches |
| 2 | Git history scan — 8 patterns × all reachable commits | 0 matches |
| 3 | `.env` tracking and ignore verification | Ignored, mode 600, never committed |
| 4 | Missing API key | Reported as unconfigured; no crash |
| 5 | Invalid provider / invalid key | Useful message, no secret echoed |
| 6 | Database failure | 500/503, no DSN in body or structured fields |
| 7 | LLM timeout, rate limit, auth, malformed, generic | Stable envelope, no internals |
| 8 | SQL injection — 8 payloads × 3 vectors | All inert; tables intact |
| 9 | Malformed requests — JSON, types, nulls, oversize | 422, no payload echo |
| 10 | Invalid ids — 10 shapes × 10 endpoints | 404/422, no leakage |
| 11 | Mass assignment — ids, timestamps, roles, lifecycle fields | All ignored |
| 12 | Prompt injection via current message | Contained |
| 13 | Prompt injection via memories | Contained |
| 14 | Prompt injection via entities and descriptions | Contained |
| 15 | Prompt injection via relationships | Contained |
| 16 | Privilege escalation (`context_role`, role forging) | Refused |
| 17 | Single knowledge injection path | Behavioural spy + AST scan |
| 18 | Memory poisoning — 8 payloads | Data, never policy |
| 19 | Arbitrary supersession via API | No route exists |
| 20 | Self-supersession | Refused |
| 21 | Circular lifecycle chains | Refused at every length |
| 22 | Mass supersession — 6 phrasings | Retires nothing |
| 23 | Alias manipulation and hijack | Refused |
| 24 | Transaction rollback — 3 failure points | No partial state |
| 25 | Concurrent mutation — turns, deletes, delete/read races | No corruption, no 500 |
| 26 | Log inspection — real output, both formatters | 2 findings, fixed |
| 27 | Error response inspection — 15-marker leak list | Clean |
| 28 | Context budget stress — 200 memories, 40 turns, 150 entities | Bounded |
| 32 | Full regression suite | 1205 passed |

### STATIC REVIEW (configuration inspected; runtime not executed)

| # | Check | Result |
| --- | --- | --- |
| 29 | Dependency review — pins, lockfiles, manifests | 3 LOW observations |
| 30 | Docker and compose security review | 2 HIGH found and fixed |
| 31 | Frontend security review | Clean — no XSS sink, no client-side storage, no public secret |

### NOT VERIFIED

| Check | Reason | What is required |
| --- | --- | --- |
| Docker runtime | Docker not installed | `docker compose up --build`, then re-run the static assertions against running containers |
| PostgreSQL runtime | No server available | A PostgreSQL instance; run migrations and the full suite against it |
| Frontend runtime | No Node toolchain | `npm install && npm run build && npm run dev` |
| Dependency CVE scan | No scanner; no `npm` | `pip-audit -r backend/requirements.txt`; `npm audit` |

---

## Runtime verification gaps

Stated plainly, not softened:

- **Docker: NOT VERIFIED.** Never built or run. The two HIGH findings were
  fixed in configuration and are covered by static tests, but no container has
  ever started.
- **PostgreSQL: NOT VERIFIED.** Every database test — including all integrity,
  constraint and concurrency work above — ran on SQLite. Migration `0006`
  round-trips on SQLite only.
- **Frontend: NOT VERIFIED.** Never built or run. Static review found no XSS
  sink, no browser storage and no client-visible secret; runtime behaviour is
  unknown.
- **Dependency CVEs: NOT VERIFIED.** No claim is made in either direction.

---

## Secrets status

**No secret is committed. No secret is in Git history.**

Verified by 21 executed tests across 8 credential patterns, over every tracked
file and every reachable commit (`git log -S --pickaxe-regex`, which finds keys
added and later removed). `backend/.env` is ignored and mode `600`. Example
env files carry no credential values. No test fixture contains a real key
shape.

**One live key exists**, masked here as `gsk_jN…n8Uc`, in `backend/.env` only.

**Action required by the user, outside the codebase:** four credentials were
pasted into a chat transcript during development — one Groq key, two OpenRouter
keys, one GitHub token. None reached Git, so no history rewriting is needed,
but all four should be **revoked and reissued**.

---

## Privacy status

Personal data leaves the database by five paths, all mapped and controlled.

**Fixed during this audit:** logs no longer carry message or memory text at any
level, and no longer carry the database password.

**Enforced by test:** no database identifier, score, timestamp or configuration
reaches the LLM provider; another conversation is never sent verbatim — only
extracted memories; superseded knowledge is withheld from ordinary questions;
both disclosure paths can be disabled by configuration.

**Accepted:** the database is not encrypted at rest, and API responses return
full content without authentication — appropriate for a single-user local
system, now bound to loopback.

---

## Prompt injection status

**Structurally contained.** Eleven payload classes across five injection
vectors, each checked against four invariants: never a system message, confined
to the reference block or the user's own turn, never alters a role, never
displaces the current message.

Containment rests on three mechanisms, none of which depends on the model
refusing: only `REFERENCE`-role items render, stored rows may occupy only
`user`/`assistant`, and every knowledge line is flattened so it cannot forge
the block's structure.

The single knowledge injection path was re-verified after Stage 3C by a
behavioural spy on the retired renderer and an AST scan of every
`LLMMessage` construction site.

---

## Knowledge integrity status

**Sound.** Lifecycle state cannot be set externally — no route accepts a
memory, entity, relationship or conflict link as input. Mass supersession
fails because supersession requires a resolvable named entity, so a quantifier
retires nothing. Alias resolution is exact, never fuzzy, and an alias cannot
point at two entities. Self-supersession and circular chains are refused.
Foreign keys prevent broken references, and a conversation delete leaves zero
orphans across all five dependent tables.

---

## Dependency status

**NOT VERIFIED.** No scanner installed (`pip-audit`, `safety`, `bandit` all
absent) and no `npm` on this machine. Three LOW static observations recorded:
no transitive lockfile on the backend, no `package-lock.json` on the frontend,
and test dependencies installing into the production image.

---

## Acceptance criteria

| # | Criterion | Result |
| --- | --- | --- |
| 1 | No active secrets in tracked files | PASS |
| 2 | `.env` files ignored | PASS |
| 3 | Example env files are placeholders only | PASS |
| 4 | Git history inspected | PASS |
| 5 | No secrets printed in reports | PASS (masked) |
| 6 | Errors expose no secrets | PASS |
| 7 | Errors expose no stack traces | PASS |
| 8 | SQL injection fails safely | PASS |
| 9 | Invalid ids fail safely | PASS |
| 10 | Mass assignment cannot reach internal fields | PASS |
| 11 | Memories remain untrusted reference data | PASS |
| 12 | Stored knowledge cannot become instructions | PASS |
| 13 | Injection via memories contained | PASS |
| 14 | Injection via entities contained | PASS |
| 15 | Injection via relationships contained | PASS |
| 16 | Exactly one knowledge injection path | PASS |
| 17 | Current user message retains authority | PASS |
| 18 | Malicious memory cannot modify policy | PASS |
| 19 | Lifecycle cannot be externally controlled | PASS |
| 20 | Self-supersession prevented | PASS |
| 21 | Circular chains prevented | PASS |
| 22 | Broken lifecycle references prevented | PASS |
| 23 | Mass supersession cannot deactivate unrelated knowledge | PASS |
| 24 | Alias handling cannot cause unrelated supersession | PASS |
| 25 | Transaction failures do not corrupt state | PASS |
| 26 | Concurrent operations preserve integrity | PASS |
| 27 | Sensitive content not logged | PASS (2 findings fixed) |
| 28 | Actual failure logs inspected | PASS |
| 29 | Provider errors expose no API key | PASS |
| 30 | Context size bounded under stress | PASS |
| 31 | Docker static review performed | PASS (2 HIGH fixed) |
| 32 | Frontend static review performed | PASS |
| 33 | Multi-user blockers documented | PASS (7 blockers) |
| 34 | Threat model documented | PASS |
| 35 | Full regression passes | PASS (1205) |
| 36 | Regression test per vulnerability | PASS |
| 37 | Critical vulnerabilities fixed | PASS (none found) |
| 38 | High vulnerabilities fixed | PASS (2 fixed) |
| 39 | Unverified runtimes honestly marked | PASS |
| 40 | Stage 4 not started | PASS |

---

## Final verdict

**PASS WITH DOCUMENTED LIMITATIONS**

Two HIGH and two MEDIUM findings were reproduced, fixed, and covered by
regression tests confirmed to fail against the pre-fix state. The application's
own security boundaries required no changes — every fix was at an
infrastructure or output boundary, which is a meaningful result in itself.

The limitations are gaps in verification, not known defects. Docker,
PostgreSQL, the frontend and dependency scanning could not be executed on this
machine and are marked NOT VERIFIED throughout.

Stage 3D is signed off. Stage 4 was not started.
