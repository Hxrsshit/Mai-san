# Stage 4F-B — Acceptance Report

**Status: complete.** Every claim below was executed. Where something was not
verified — including live search — it says so.

---

## 1. Tests

| | Count |
|---|---|
| Before Stage 4F-B | 2271 |
| **After** | **2474** |
| Added | 203 |
| Failures | 0 |
| Skipped | 1 (pre-existing, unrelated) |

New test files:

| File | Tests |
|---|---|
| `tests/test_web_search.py` | 30 |
| `tests/security/test_http_client_security.py` | 24 |
| `tests/security/test_web_search_security.py` | 20 |

(The remaining additions are parametrised cases inside those files — 33 SSRF
destinations, 24 injection payload × field combinations, and so on.)

New modules — 918 lines:

```
app/integrations/http_client.py    287   the one policy-enforcing client
app/integrations/search.py         258   result contract, validation, bounds
app/integrations/web_search.py     254   the integration
app/execution/web_search_tool.py    38   the tool
tests/support/stub_transport.py     81   a transport that never networks
```

The suite was run twice end-to-end to check for order dependence. Both clean.

## 2. Mutation testing — 22/22 caught

| | Mutation | Result |
|---|---|---|
| Q1 | NetworkPolicy enforcement removed from the client | PASS |
| Q2 | Arbitrary URLs allowed (host allow-list bypassed) | PASS |
| Q3 | Redirects followed without re-checking policy | PASS |
| Q4 | Private IP ranges permitted | PASS |
| Q5 | DNS resolution check removed | PASS |
| Q6 | Response size limit removed | PASS |
| Q7 | Total timeout removed | PASS |
| Q8 | Arbitrary request headers permitted | PASS |
| Q9 | Credential exposed as a tool argument | PASS |
| Q10 | External content marked trusted | PASS |
| Q11 | Authorization skipped before search | PASS |
| Q12 | Search tool given an arbitrary `url` argument | PASS |
| Q13 | Source attribution dropped from rendered results | PASS |
| Q14 | A failed search reported as success | PASS |
| Q15 | Result URLs no longer validated | PASS |
| Q16 | Snippets no longer flattened (structure forging) | PASS |
| Q17 | Malformed URLs raise instead of failing closed | PASS |
| Q18 | Redirect hop limit removed | PASS |
| Q19 | Compression advertised (defeats the size bound) | PASS |
| Q20 | Retry limit raised beyond policy | PASS |
| Q21 | Credential moved into the query string | PASS |
| Q22 | Capability reported available without a configured key | PASS |

**Q12 and Q16 failed on the first run.** Both were coverage gaps rather than
code bugs, and both were closed with a test rather than a change:

- **Q12** — adding a `url` key to what crosses into the integration went
  unnoticed, because the integration ignores unknown keys. Harmless today,
  and it would not have stayed harmless. A test now pins the exact key set.
- **Q16** — removing the flatten from `_bounded` changed nothing observable,
  because `as_external_data` flattens again at render time. That render-time
  call is load-bearing; the inner one is defence in depth, and depth is only
  depth if something checks it. A test now asserts the *stored* field carries
  no newline.

## 3. Security findings

### An existing unrestricted network path (Part 1)

`app/llm/providers/openai_compatible.py` imports `httpx` and does **not** go
through `NetworkPolicy`. Invariant #3 is therefore not universally true, and
the architecture document states the scope precisely rather than glossing it:
every connection made on behalf of a *tool or integration* — anything whose
destination could be influenced by user input, model output or external
content — goes through `SecureHttpClient`. The LLM provider is a first-party
destination read from operator configuration and predates the policy.

A structural test enumerates it as the single exception, so a *second*
unpoliced path cannot appear silently.

### A committed duplicate of the integrations package

`app/integrations/integrations/` — a byte-identical, unreferenced copy of the
whole package **including the SSRF policy**, created accidentally by a
`docker compose cp` during Stage 4F-A verification and committed. Removed.

Worth naming rather than quietly deleting: a second copy of an address
allow-list is precisely the artefact that rots, because a fix lands in one
copy and not the other.

### Malformed URLs did not fail closed

`urlparse` raises `ValueError` on `https://[::1` and on a non-numeric or
out-of-range port. That exception escaped `NetworkPolicy.check` as a raw
crash rather than a refusal — indistinguishable from a bug elsewhere, and not
the fail-closed behaviour Part 4 requires. Now converted to
`NetworkPolicyViolation`. Found by an SSRF test case, and pinned by mutation
Q17.

## 4. Security tests

**SSRF, at the client boundary.** 33 prohibited destinations, each driven
through `SecureHttpClient` and asserted against `transport.connections == []`
— nothing would have been dialled. Covers loopback in every form, private
IPv4 and IPv6, CGNAT, link-local, both cloud metadata endpoints, seven unsafe
schemes, four arbitrary ports, three near-miss hostnames, and malformed input.

**DNS rebinding.** An allow-listed host resolving to `127.0.0.1`,
`169.254.169.254`, `10.1.2.3`, `192.168.0.5`, `::1` or `fc00::5` never
connects. IPv4-mapped IPv6 is unwrapped first. An unresolvable host is
refused.

**Redirects.** Ten unsafe targets tested — loopback, localhost, metadata,
private, IPv6 loopback, scheme downgrade, `file:`, `gopher:`, foreign host,
arbitrary port. In every case exactly one destination reaches the transport
and it is the allowed one. Also: rebinding on the *target*, unbounded chains,
empty `Location`, and relative targets (resolved, then re-checked).

**Prompt injection.** Eight payloads × three result fields, plus a
domain-name forging attempt, plus four driven through the real pipeline. Each
arrives attributed and untrusted; no execution record, no authorization
change, no approval, no capability change.

**Credential leakage.** `SEARCH_SECRET_123` verified absent from the tool
schema (no field exists), the execution record, the audit journal, the
request URL, every header but the provider's own, and the model prompt.

**Data leakage.** A maximum-importance memory containing a passport number
does not reach the outbound request. Structurally, no search module imports
`app.database`, `app.memory`, `app.context`, `app.retrieval`, `app.services`
or `app.llm`.

**Grounding.** A failed search cannot look successful — tested for 401, 403,
429, 500 and 503: the record goes to `FAILED`, `result_summary` stays `None`,
the journal records `execution_failed`.

**Bounds.** Oversized responses refused not truncated; a response exactly at
the limit accepted; compression never advertised; slow responses produce a
structured `TIMEOUT`; retries stop at 3; 401 never retried.

## 5. Live verification

**Live web search was NOT performed. No search provider is configured**, and
none was invented — `SEARCH_API_KEY` is unset in `.env` and in the container.
Part 31 conditions live search on a provider already being available; it is
not, so nothing about real search results, real sources or real provider
failures is claimed here.

What *was* verified live, against the real Groq-backed provider
(`openai/gpt-oss-120b`), is that Mai describes the new capability truthfully:

```
[PASS] Can you search the web?
       "I'm not able to run a web search in this environment."

[PASS] What tools can you currently use?
       "...Other capabilities (e.g., web search) are either di[sabled]..."

[PASS] Search the web for the latest AI news.
       "I'm not able to run a web search in this environment. I can, however,
        share what I know... up to my knowledge cutoff"

[PASS] Do you have a web search tool implemented?
       "Yes. This deployment includes a web-search tool that is implemented,
        but its execution is currently switched off, so I can't run web
        searches for you."

4/4 semantic checks passed
```

The last answer is the one worth noting: it distinguishes *implemented* from
*available* without prompting, which is the distinction Stage 4E.1 built and
this stage's first real test of it.

## 6. PostgreSQL

**No migration was added.** No new tables, no schema change; the database
remains at `0007 (head)`. Stated explicitly as Part 32 requires.

The pipeline was run inside the backend container against real PostgreSQL
16.15:

```
PASS  shipped registry holds web_search only
PASS  shipped integration is NOT_CONFIGURED
PASS  proposed, nothing dialled
PASS  approved, nothing dialled
PASS  executed, exactly one request
PASS  state is succeeded
PASS  journal complete
PASS  integration recorded
PASS  operation recorded
PASS  provider status recorded
PASS  no credential in journal
PASS  no query in journal
PASS  no result content in journal
PASS  failed search refused (provider_unavailable)
PASS  failed search state is FAILED
PASS  failed search has no summary

16/16 checks passed on PostgreSQL
```

## 7. Docker

`SEARCH_API_KEY` and `EXECUTION_ENABLED` added to the backend service.
`SEARCH_API_KEY` deliberately has **no `:?` guard**, unlike the keys the app
cannot start without: a deployment with no search provider is a supported one,
and it reports `NOT_CONFIGURED` rather than failing to boot.

Backend recreated and verified: health 200, all three containers running,
ports still bound to `127.0.0.1` only. Inside the container the integration
reports `not_configured`, the health output names the *setting* and not a
value, and `docker compose logs` contains zero credential-shaped matches.

## 8. Frontend

Verified working at `http://localhost:3000`: the app renders, the conversation
list loads from PostgreSQL, zero console errors.

**No research UI was added, and that is a deliberate omission rather than an
oversight.** There is no chat path that produces search results: search is
reachable only through the explicit execution API (propose → approve →
execute), which has had no UI since Stage 4E. Building a "Researching… →
Sources → Answer" flow would mean showing a sequence that cannot currently
happen, and making it happen would mean adding a chat→execute loop — a
capability in its own right, and not one to add quickly at the end of a
security stage.

## 9. Chat integration

The Stage 4D orchestration matcher pointed at `future_web_search` — a
declaration with no implementation. It now points at the real `web_search`,
with the user's own words as the query.

This is **identification only**. The candidate travels Stage 4C authorization
and Stage 4E approval, and `web_search` requires approval, so no message
becomes an HTTP request. Verified end-to-end: a chat turn whose reply contains
`EXECUTE web_search ... FETCH https://169.254.169.254/...` creates zero
execution records.

## 10. Approval decision

Retained, not waived. `web_search` is `MEDIUM` risk with
`requires_approval=True`. It is read-only, but it is the one tool that sends
what the user asked about to a third party — a privacy risk rather than a
destruction one, and the ladder should say so. Policy was not weakened for
convenience.

## 11. Known limitations

- **DNS rebinding is mitigated, not eliminated.** Addresses are validated
  before each connection, but the connection is not pinned to the validated
  address — `httpx` resolves again itself, leaving a TOCTOU window. Pinning
  requires overriding SNI and `Host` while connecting to an IP, and getting
  certificate verification subtly wrong there would be a worse outcome than a
  documented window. Residual risk is bounded by the allow-list: only
  `api.search.brave.com` is reachable, so exploiting it requires controlling
  that domain's DNS.
- **The LLM provider does not go through `NetworkPolicy`** (§3).
- **Live search is unverified.** No provider configured. The integration's
  behaviour against a real Brave endpoint — its exact payload shape, its real
  error bodies, its real latency — has never been observed.
- **The provider payload parser is written from the documented shape**, not
  from an observed response. `parse_results` reads fields by name and degrades
  to zero results rather than raising, so a shape mismatch is a poor answer
  rather than a crash — but it would be a poor answer.
- **No research UI** (§8).
- **No cache**, deliberately (Part 27).
- **Rate limiting remains an abstraction.** Retries and timeouts are enforced;
  a per-account or per-integration limiter is not implemented.
- **The suite runs on SQLite**; PostgreSQL is covered by the 16 checks above.
- **No dependency CVE scan** has been run at any stage.

## 12. Outstanding user actions (unchanged)

The credentials exposed earlier in development should still be revoked: the
Groq API key, both OpenRouter keys, and the GitHub personal access token.
