# Stage 4F-C — Acceptance Report

**Status: PASS.** Every claim below was executed.

---

## 1. Architecture

```
GroqProvider._get_client()
  -> build_provider_client(base_url, timeout)        app/llm/transport.py
       -> NetworkPolicy(allowed_hosts={"api.groq.com"},
                        allowed_methods={"POST"},
                        follow_redirects=False,
                        retries=max_attempts=1)
       -> SecureHttpClient                           app/integrations/http_client.py
            -> policy.permits("POST")                method gate
            -> policy.check(url)                     destination gate
            -> httpx transport
```

Verified live in the Docker container:

```
provider=groq  client=SecureHttpClient  is SecureHttpClient: True
allowed_hosts=['api.groq.com']  methods=['POST']
redirects=False  transport_retries=1
key on client object: False
```

## 2. The conflict, and how it was resolved

Following the brief literally would have weakened Stage 4F-B.

`SecureHttpClient` was GET-only **by design and by test** — Stage 4F-B
asserted `not hasattr(SecureHttpClient, "post")` with the reasoning that a
client which could POST could be talked into submitting a form. The LLM
provider POSTs. Adding a general `post()` would have handed the *web search
integration* a write-capable client.

**Resolution:** method capability moved from the client class onto the
`NetworkPolicy` instance. Research declares `{"GET"}`, the provider declares
`{"POST"}`, the default is read-only, and `PUT`/`PATCH`/`DELETE`/`HEAD`/
`OPTIONS` exist on the client at all. This is strictly stronger than what it
replaced: research could previously not POST because *no client could*, a
guarantee that would have evaporated the moment any caller needed POST. It now
cannot POST because *its own policy forbids it* — checkable per instance, and
load-bearing under mutation (R12, R13).

Four further conflicts, each resolved without weakening anything:

| | Conflict | Resolution |
|---|---|---|
| C2 | Header allow-list excludes `Content-Type` | Set by the client when it has a body; never caller-settable |
| C3 | Provider retries would layer under transport retries (3×3 = 9) | Transport contributes 1 attempt; the provider's loop stays authoritative |
| C4 | Provider host comes from config, research's from code | Single-host allow-list derived from config, every other check unchanged |
| C5 | `extra_headers` — an unused arbitrary-header parameter on the provider | Removed |

## 3. Repository-wide network inspection

AST scan of every file under `app/`:

| Finding | Status |
|---|---|
| `app/integrations/http_client.py` imports `httpx` | The boundary. Permitted. |
| `app/llm/providers/openai_compatible.py` imported `httpx` | **Removed this stage.** |
| `app/integrations/policy.py` imports `socket` | `getaddrinfo` only; never connects. Asserted. |
| `urllib.parse` | URL parsing. Opens nothing. |
| Anything else | None found. |

**Remaining exceptions: zero.** The frontend's own `fetch` to Mai's API is
browser-side code talking to Mai itself, not application-level outbound
traffic, and is out of scope by definition rather than by exemption.

## 4. Tests

| | Count |
|---|---|
| Before | 2474 |
| **After** | **2543** |
| Added | 69 |
| Failures | 0 |
| Skips | 1 |
| Second run | identical, no order dependence |

New files: `tests/security/test_provider_transport_security.py` (32),
`tests/security/test_network_boundary.py` (11), plus additions to the
existing client-security file.

**The one skip is expected and documented:**
`tests/test_migration_compatibility.py:349 — TEST_POSTGRES_URL is not set`.
Pre-existing, unrelated to this stage.

## 5. Mutation testing — 21/21 caught

| | Mutation | Result |
|---|---|---|
| R1 | Provider bypasses SecureHttpClient (direct httpx shim) | PASS |
| R2 | Method check removed from the client | PASS |
| R3 | Policy check removed from the request path | PASS |
| R4 | Provider allow-list widened to another host | PASS |
| R5 | Provider host derived without validation | PASS |
| R6 | Provider follows redirects | PASS |
| R7 | Credential baked onto the long-lived client | PASS |
| R8 | Provider error message no longer scrubbed | PASS |
| R9 | Transport retries layered under provider retries | PASS |
| R10 | Provider response size bound removed | PASS |
| R11 | Outer total timeout removed | PASS |
| R12 | Provider policy permits GET as well as POST | PASS |
| R13 | Default policy becomes write-capable | PASS |
| R14 | Private ranges permitted | PASS |
| R15 | Policy refusal escapes as a raw exception | PASS |
| R16 | Response headers lose case-insensitivity | PASS |
| R17 | A second HTTP client appears in application code | PASS |
| R17b | A module constructs a client without importing httpx | PASS |
| R18 | Content-type / authorization accepted from the caller | PASS |
| R19 | Body re-POSTed on redirect | PASS |
| R20 | TLS verification disabled | PASS |

**Four survivors on the first run**, all addressed:

- **R2** (method gate) — every test asserted `policy.permits(...)` directly;
  none asked the *client* to perform a forbidden method. Three tests added:
  a read-only policy refusing POST, a write-only policy refusing GET, and the
  gate running before the destination is parsed.
- **R5** (host derivation) — returning a placeholder instead of raising was
  caught downstream by the host check, leaving the derivation's own guard
  untested. Direct tests added.
- **R19** (redirect body) — no shipped policy both permits POST and follows
  redirects, so nothing exercised it. A test now builds that combination
  explicitly: the guard should be load-bearing *before* the integration that
  needs it arrives, not after.
- **R17** — **my mutation was ill-posed.** It mutated an assertion inside the
  boundary test and then ran that same test, which can never fail. Replaced
  with the meaningful version — introduce a second HTTP client in production
  code the way a future developer would — and split into two variants
  (`import httpx`, and `from httpx import AsyncClient`). Both caught.

No genuinely equivalent mutations remain.

## 6. Security findings

| Finding | Severity | Fixed |
|---|---|---|
| Provider transport bypassed `NetworkPolicy` entirely | The stage's premise | Yes |
| A policy refusal escaped as a raw `NetworkPolicyViolation` instead of `LLMError`, so a misconfigured endpoint would surface as an unhandled 500 of the wrong shape | Medium | Yes |
| Provider-supplied error text was echoed to the user unscrubbed — a provider echoing the request back would surface the `Authorization` header | Medium | Yes |
| The API key was baked into a long-lived client object reachable by any `repr` or debug dump | Medium | Yes — now per request |
| `extra_headers`: an unused arbitrary-header injection point on the provider | Low | Yes — removed |
| **Regression I introduced and caught:** `dict(response.headers)` lost HTTP case-insensitivity, so `Retry-After` was silently no longer honoured | Medium | Yes — `ResponseHeaders` |

The `Retry-After` regression is worth naming: nothing failed loudly. Rate-limit
backoff quietly stopped following the server's guidance, and only an existing
test asserting the exact sleep value caught it.

## 7. Credential security

Verified absent from every location the brief lists:

| Location | Result |
|---|---|
| Prompts | absent (prompt-debug endpoint grepped) |
| Runtime facts | absent (`model_dump_json` grepped) |
| Tool arguments | no field exists |
| Audit journals | absent (`execution_events` queried in PostgreSQL) |
| Application logs | absent at DEBUG (`caplog`) |
| Container logs | absent (`docker compose logs`) |
| Error messages | absent for 400/401/403/404/429/500/503 |
| Frontend-visible responses | absent (conversation API grepped) |
| The client object | absent (`vars()` grepped) |
| The response object | absent — `HttpResponse` carries no `request` |
| Request URL and body | absent |
| Database (`messages`, `execution_events`) | absent |

## 8. Live provider verification — **performed**

A valid Groq credential was already configured. No credential was printed or
reproduced at any point.

1. **Runtime identified correctly** — *"I'm running on Groq with the
   openai/gpt-oss-120b model."*
2. **A normal chat request succeeded** through PostgreSQL, end to end.
3. **The request passed through `SecureHttpClient`** — confirmed in-container:
   `client=SecureHttpClient`, `allowed_hosts=['api.groq.com']`,
   `methods=['POST']`.
4. **No credential in logs** — 0 matches across 200 lines of container output.
5. **No credential in audit data** — 0 rows in `execution_events`.
6. **No credential in model-visible runtime facts** — 0 matches.
7. **Provider failure behaviour is safe** — covered by tests for timeout,
   transport failure, 4xx, 5xx, malformed JSON, oversized body and policy
   refusal; none leaks a key or an internal address.

## 9. PostgreSQL

**No migration was needed**, and none was added. The database remains at
`0007 (head)`.

Verified: the application starts against PostgreSQL, migrations remain valid,
a full chat flow works end to end, no provider content leaked into journals,
and no credential entered any record.

## 10. Docker

`docker compose` stack running: `backend`, `db`, `frontend`. Backend restarted
with the new transport and returns health 200. All ports remain bound to
`127.0.0.1` only. `SEARCH_API_KEY` remains optional and unset; the search
integration still reports `NOT_CONFIGURED`. Containerised provider traffic
confirmed to use `SecureHttpClient`. No credentials in logs.

## 11. Frontend

Verified at `http://localhost:3000`: the app renders, the conversation list
loads from PostgreSQL, the live-created conversation appears, and the assistant
response renders correctly (*"I'm running on the Groq provider using the
openai/gpt-oss-120b model."*).

**Zero console errors.** No credential in any browser-visible response. No UI
was redesigned and no Stage 4F-D research UI was built.

## 12. Dependency / CVE scan

No scanner existed; `pip-audit` was installed and run against
`requirements.txt`.

**8 known vulnerabilities in 4 packages** — and a finding that changes the
remediation entirely:

| Package | Installed | Advisory | Reported fix | Fix installable? |
|---|---|---|---|---|
| starlette | 0.49.3 | PYSEC-2026-161 | 1.0.1 | **No** |
| starlette | 0.49.3 | PYSEC-2026-249 | 1.3.1 | **No** |
| starlette | 0.49.3 | PYSEC-2026-248 | 1.3.0 | **No** |
| starlette | 0.49.3 | PYSEC-2026-2281 | 1.1.0 | **No** |
| starlette | 0.49.3 | PYSEC-2026-2280 | 1.1.0 | **No** |
| click | 8.1.8 | PYSEC-2026-2132 | 8.3.3 | **No** |
| python-dotenv | 1.2.1 | PYSEC-2026-2270 | 1.2.2 | **No** |
| pytest | 8.4.2 | PYSEC-2026-1845 | 9.0.3 | **No** |

**None of the eight fixes exists on the configured package index.** The latest
available versions are exactly what is pinned: starlette 0.49.3, click 8.1.8,
python-dotenv 1.2.1, pytest 8.4.2. The advisory database references versions
that have not been released here. Upgrading is therefore not an option to
decline — it is not available.

That makes runtime relevance the operative question:

| Advisory | Vulnerable code path | Used by Mai? |
|---|---|---|
| starlette 2281 | `StaticFiles` on Windows (UNC → SMB) | **No** — `StaticFiles` unused; Linux/macOS |
| starlette 2280 | `HTTPEndpoint` method lookup via `getattr` | **No** — FastAPI decorators, no `HTTPEndpoint` subclass |
| starlette 249 | `request.form()` urlencoded field limits | **No** — JSON APIs only |
| starlette 161, 248 | `request.url` reconstruction from the `Host` header | **Partial** — `request.url.path` is read in request logging; the advisories concern `.hostname`/`.netloc`, which are not read |
| click 2132 | `click.edit()` | **No** — transitive via uvicorn; never called |
| python-dotenv 2270 | `set_key()` / `unset_key()` symlink following | **No** — pydantic-settings only reads |
| pytest 1845 | `/tmp/pytest-of-{user}` local privilege issue | **No** — test-time only, not runtime |

**Remediation options, none taken:**

1. Wait for installable releases and re-run the audit. This is the correct
   default and costs nothing.
2. For starlette 161/248, a compensating control is available today: reject
   or normalise unexpected `Host` values at the edge. Mai is loopback-bound
   with no reverse proxy, so the attack has no reachable position.
3. Do **not** pin starlette directly to escape FastAPI's range — that
   substitutes a resolver conflict for a vulnerability.

No dependency was upgraded. The brief forbids blind upgrades, and here no
upgrade was even possible.

## 13. Mini security review

**Network** — No application code can make direct outbound HTTP (AST-asserted
repository-wide). The provider cannot bypass the client (R1 caught). A user
cannot influence the provider URL (path is a code literal, base is operator
config, and all hostile bases are refused). Redirects cannot bypass policy
(refused for the provider; re-checked and body-dropped elsewhere). DNS is
checked before each connection, with the documented rebinding window. Unsafe
destinations are refused at the client boundary with nothing dialled.

**Credentials** — No to logs, audit, prompts, runtime facts, frontend output
and exception messages. All verified, §7.

**Transport** — Redirects controlled, timeouts bounded and finite, response
sizes bounded, arbitrary headers impossible, cookies impossible, TLS
verification preserved and asserted never disabled.

**Architecture** — No second HTTP abstraction. No duplicated security logic —
the provider reuses `NetworkPolicy` and `SecureHttpClient` unchanged. One
authoritative boundary, named by a test.

**Regression** — Memory, chat, action, execution and orchestration behaviour
unchanged (2543 tests, twice). Runtime capability truthfulness unchanged.
Stage 4F-B search behaviour unchanged.

## 14. Limitations

- **DNS rebinding is mitigated, not eliminated** — unchanged from Stage 4F-B.
  A TOCTOU window remains between validation and `httpx`'s own resolution.
  Bounded by the single-host allow-list on both callers.
- **A locally-hosted LLM is unreachable** — loopback and plain HTTP are both
  refused by the provider policy. Deliberate and fail-closed; supporting one
  needs its own designed allowance.
- **Five starlette advisories have no installable fix** (§12). Runtime
  relevance is assessed as none or partial, but they remain open.
- **`request.url.path` in request logging** is the one place Mai touches the
  API two of those advisories concern.
- **Live verification covered the success path and configuration**, not live
  provider failures — timeout, 5xx and malformed responses were exercised
  against stubs, not against a real degraded Groq.
- **Streaming remains unsupported** — not a limitation introduced here, but
  worth stating: the claim "streaming preserved" is not being made because
  there was none.

## 15. Recommendation

Do not start Stage 4F-D on the strength of this report alone. The evidence
above is the argument for it: 2543 tests twice, 21/21 mutations, a live
provider call through the boundary, and zero remaining unpoliced paths.

The most useful next piece of work is smaller than a stage: **re-run
`pip-audit` once installable fixes appear**, since eight advisories are open
purely because no release exists. After that, Stage 4F-D's research UI is a
reasonable next step — it is the first thing in a while that is a feature
rather than a boundary, and the boundary is now in a state that can support
one.

## 16. Outstanding user actions (unchanged)

The credentials exposed earlier in development should still be revoked: the
Groq API key, both OpenRouter keys, and the GitHub personal access token.
