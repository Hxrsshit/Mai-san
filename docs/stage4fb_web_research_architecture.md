# Stage 4F-B — Secure Web Research + Enforced Network Boundary

Mai's first real external capability, and the first time the network policy
written in Stage 4F-A actually stops anything.

---

## 1. What changed

Stage 4F-A built `NetworkPolicy` and enforced it nowhere — there was no client
to enforce it against, and that stage's acceptance report said so as a known
limitation. Stage 4F-B closes that gap in the way the report recommended: the
first integration arrives **together with** a single shared client, so `check`
runs on the request path rather than beside it.

```
Tool "web_search"
 ↓
Authorization (Stage 4C)          — is Mai permitted?
 ↓
Approval (Stage 4E)               — has the user approved this query?
 ↓
Dispatcher (Stage 4E)             — the one place a side effect happens
 ↓
WebSearchIntegration              — builds the request
 ↓
SecureHttpClient                  — enforces NetworkPolicy, itself
 ↓
NetworkPolicy                     — scheme, host, port, address, redirect
 ↓
Provider
 ↓
ExternalResult → Audit → response
```

## 2. Existing network paths, audited first

Part 1 required finding every place a connection could already happen before
adding another. Two findings:

**`app/llm/providers/openai_compatible.py` imports `httpx`.** This is the LLM
provider, and it does **not** go through `NetworkPolicy`. It is a genuine,
pre-existing exception, and invariant #3 is therefore not universally true.
Stated precisely rather than glossed:

> Every outbound connection made on behalf of a *tool or integration* — that
> is, anything whose destination could be influenced by user input, model
> output, or external content — goes through `SecureHttpClient`. The LLM
> provider is a first-party destination read from operator configuration
> (`settings.active_base_url`), reachable by no other route, and it predates
> the policy.

A structural test enumerates it as the single exception: within
`app/integrations`, only `http_client.py` may import an HTTP library, and a
second importer fails the suite. Bringing the provider under the policy is
worth doing and is listed as the recommended next step; doing it here would
have meant reworking working Stage 1 code for a destination the threat model
does not cover.

**`app/integrations/integrations/` — a duplicated copy of the entire
integrations package, committed in Stage 4F-A.** Created accidentally by a
`docker compose cp` during that stage's verification, byte-identical,
unreferenced, and *including a second copy of the SSRF policy*. Removed. That
is exactly the kind of duplicate that rots: someone fixes the address check in
one copy and not the other.

## 3. SecureHttpClient

The only approved path for outbound HTTP, and it enforces policy **itself**
rather than trusting callers:

```python
policy.check(url)   # a caller can forget this
client.get(url)     # this cannot be reached without the check
```

`check` runs before every connection — the initial URL and every redirect
target. There is no parameter that disables it and no method that bypasses it.

**No write methods.** No `post`, `put`, `patch`, `delete`, `request` or `send`.
Research is read-only, and a client that could submit a form is a client that
could be talked into submitting one. A test asserts each is absent.

**No arbitrary headers.** An allow-list of three (`accept`, `accept-encoding`,
`user-agent`), so a caller cannot smuggle a `Cookie`, an `Authorization` it
built itself, or a `Host` override that would defeat the destination check.
Refused rather than dropped — a caller trying to set a header it may not set
is a mistake worth surfacing. Credentials are applied separately, from a value
the credential resolver produced, and cannot be overwritten by the mapping.

**No cookie jar.** A cookie is state a provider sets and Mai then sends back
somewhere; research needs none.

## 4. SSRF protection, at the client boundary

Part 5 is explicit that testing `policy.check(url)` is insufficient. Every SSRF
test drives `SecureHttpClient` against a stub transport and then asserts
`transport.connections == []` — the list of destinations that got as far as the
transport. A refused URL leaves it empty, which is a stronger claim than "a
helper raised": nothing would have been dialled.

Refused, in order, each failing closed:

| Check | Refuses |
|---|---|
| Scheme | `http`, `file`, `ftp`, `gopher`, `data`, `javascript`, `dict`, `ldap` |
| Host literal | `localhost`, `metadata`, `metadata.google.internal`, `instance-data` |
| Port | anything but 443 |
| Allow-list | exact match or a true subdomain — never a substring |
| Resolved address | loopback, private, CGNAT, link-local, multicast, reserved |
| Malformed | anything `urlparse` cannot parse |

Covered addresses include `127.0.0.1`, `0.0.0.0`, `[::1]`, `10/8`, `172.16/12`,
`192.168/16`, `100.64/10`, `fc00::/7`, `fe80::/10`, and the cloud metadata
endpoints `169.254.169.254` and `169.254.170.2`.

IPv4-mapped IPv6 is unwrapped before the range check, or `::ffff:127.0.0.1`
would miss every IPv4 rule.

**A malformed URL now fails closed.** Found during this stage: `urlparse`
raises `ValueError` on `https://[::1` and on an invalid port, and that
exception escaped as a raw crash rather than a refusal. It is now converted to
`NetworkPolicyViolation`, because an exception a caller did not expect is not
a refusal — it is indistinguishable from a bug elsewhere.

## 5. DNS rebinding — what is and is not protected

**Implemented:** before each connection, the hostname is resolved and every
returned address is checked against the forbidden ranges. An allow-listed name
that resolves to `127.0.0.1`, `169.254.169.254` or any private range is
refused, and the allow-list alone would have permitted it. An unresolvable
host is refused rather than handed to a client that would resolve it again.
The check runs on redirect targets too, not just the first URL.

**Not implemented, and stated plainly as Part 6 requires:** the connection is
not pinned to the validated address. `httpx` performs its own resolution when
it connects, so a TOCTOU window exists between validation and connection. An
attacker controlling DNS for an allow-listed host, with a TTL short enough to
change the answer inside that window, could in principle be resolved to a
different address than the one validated.

Pinning would mean connecting to the validated IP while overriding SNI and the
`Host` header, and getting certificate verification subtly wrong in the process
is a worse security outcome than a documented window. The residual risk is also
bounded by the allow-list: only `api.search.brave.com` is reachable at all, so
an attacker would need control of that domain's DNS, at which point they
control the search provider.

**This is a documented limitation, not a claim of complete protection.**

## 6. Redirects

Not followed by default. `httpx`'s own redirect handling is disabled outright
(`follow_redirects=False`), because it would follow a hop without the policy
ever seeing it. Redirects are walked manually, one at a time, and **every
target goes through the same policy check before the next connection**.

A safe initial URL does not make an unsafe redirect safe. Tested: safe→safe,
safe→loopback, safe→private, safe→metadata, safe→downgraded scheme,
safe→`file:`, safe→arbitrary port, safe→foreign host, and safe→a host that
rebinds. In every unsafe case exactly one destination reaches the transport,
and it is the one that was allowed.

Chains are bounded by `max_redirects` (3): an endless chain is a denial of
service even when every hop is safe.

The search integration sets `follow_redirects=False` anyway — a search API has
no reason to redirect, and refusing outright is one fewer thing to get right.

## 7. Response and time bounds

| Bound | Value |
|---|---|
| Connect | 5s |
| Read | 10s |
| Total operation | 20s |
| Response body | 1 MB (search) / 2 MB (default) |
| Redirect hops | 3 |
| Retry attempts | 3 |
| Retry total | 20s |

The total bound is an outer `asyncio.wait_for`, so it covers redirect hops and
a slow body together — which the per-phase `httpx` timeouts individually do
not. A timeout produces a structured `TIMEOUT` result; no raw exception text
reaches the user.

**Compression is not advertised.** `accept-encoding: identity`, deliberately.
A bound on decompressed bytes is only enforceable if nothing inflates before
the check — advertising gzip is exactly how a decompression bomb gets past a
byte limit.

Oversized responses are **refused, not truncated**. A truncated body is a
partial answer that looks like a whole one, and a JSON parser would either
fail confusingly or succeed on a prefix.

## 8. WebSearchIntegration

One host, one endpoint, one operation, read-only.

```
https://api.search.brave.com/res/v1/web/search
```

The host is a **constant in application code**, not a setting — no
configuration, argument or model output can point the client elsewhere.

The user supplies a **query**. The integration constructs the URL, the method,
the headers and the parameter names. There is no URL field anywhere on the
path from a chat message to this module, which is what keeps a research
capability from being an arbitrary fetch capability under another name.

A query that *contains* a URL is searched for as a string. Tested explicitly
with `https://169.254.169.254/latest/meta-data/` as the query: one connection,
to the search API, with the text as a search term.

## 9. Search results: validated, never trusted

Nothing is passed through. Each field is extracted **by name**, bounded, and
validated — never `SearchResult(**item)`, so a provider that renames a field,
adds one, or returns the wrong type produces a poorer result rather than an
error or an injection.

| Bound | Value |
|---|---|
| Results | 10 |
| Title | 200 chars |
| Snippet | 500 chars |
| URL | 500 chars |
| Query | 300 chars |

A result whose URL is not `https`/`http`, or which contains a control
character, is **dropped rather than repaired**. A repaired result is one Mai
would then attribute a claim to, and the attribution would be to something
that was never there. `total_available` survives, so truncation is visible.

Titles and snippets are flattened to single lines — a snippet is written by
whoever owns the page, and a newline in one could forge the structure of the
block it is rendered into, including a second attributed source.

## 10. External content is data

Results are wrapped in `ExternalData`, whose `trust_level` is a property
returning `UNTRUSTED` with no field behind it. The label travels with the
content rather than being reapplied by whoever renders it next.

Rendering is deliberately fenced: each result is introduced by its own
numbered source line, so a snippet saying "IMPORTANT SYSTEM MESSAGE" is
visibly a snippet belonging to a named domain rather than a floating
instruction.

Tested with eight injection payloads placed in **every** result field — title,
snippet, URL and domain — driven through the real pipeline. Each arrives, is
attributed, and changes nothing: no execution record, no authorization change,
no approval, no capability change.

Search results are classified `PRIVATE`, not `PUBLIC`. The pages are public;
what the user wanted to know about is not.

## 11. Credentials

The key travels in one place: a provider auth header (`X-Subscription-Token`)
applied by the client from a value the credential resolver produced.

Explicitly **not** in the query string. A query string is logged by proxies,
kept in provider access logs, and would land in any URL Mai recorded.

Tested absent from: the tool's argument schema (there is no field), the
execution record, the audit journal, the request URL, every header but the
provider's own, and the model prompt.

The query itself is **not logged at INFO**. A search query can name a person,
a diagnosis or an employer; the log carries the result count, the query
*length* and the latency, which is enough to debug with.

## 12. Data minimisation

Three values cross into the integration: `query`, `max_results`, `safe_search`
— pinned by a test, after mutation testing found that adding a fourth key went
unnoticed.

Not the conversation, not memories, not the context package. Tested with a
maximum-importance memory containing a passport number: it does not appear in
the outbound request. Structurally, none of `web_search.py`, `search.py` or
`http_client.py` imports `app.database`, `app.memory`, `app.context`,
`app.retrieval`, `app.services` or `app.llm`.

## 13. Retries

Search is read-only, so retrying is safe in a way it is not for a send — and
still bounded: 3 attempts, exponential backoff capped at 4s, 20s total.
`retry_side_effects` stays `False` even here, so the default is not wrong for
whatever gets copied from this file next.

`401` and `403` are never retried: repeating a rejected credential achieves
nothing and can trip a provider lockout. `429` and `5xx` are.

## 14. Failure behaviour and grounding

Provider statuses map to distinct result states — `401→UNAUTHORIZED`,
`403→FORBIDDEN`, `404→NOT_FOUND`, `400/422→VALIDATION_ERROR`,
`429→RATE_LIMITED`, `5xx→UNAVAILABLE` — so the response layer can say
something true rather than "something went wrong".

A failed search **cannot be represented as a successful one**. The tool raises
rather than returning an outcome, the execution record goes to `FAILED`,
`result_summary` stays `None`, and the journal records `execution_failed`.
Tested for 401, 403, 429, 500 and 503.

An empty result set is a **success with zero results**, not a failure. Finding
nothing is an answer.

## 15. Chat integration

The Stage 4D orchestration matcher pointed at `future_web_search` — a
declaration with no implementation — for as long as there was no search. It
now points at the real `web_search`, with the user's own words as the query.

That is the whole of this stage's chat integration, and it is *identification
only*. The candidate still travels Stage 4C authorization and Stage 4E
approval, and `web_search` requires approval, so **no message becomes an HTTP
request**. There is deliberately no chat→execute loop; building one is a
capability in its own right and is not smuggled in here.

The query is the user's message, bounded — not a model-extracted "search
term". Extraction would need a model call on a path that currently makes none,
and it would let a model choose what Mai searches for. What the user typed is
what gets proposed, and they see it in the approval prompt.

## 16. Approval was retained, not waived

Part 23 asks whether a read-only operation should be approval-free. It is not.

`web_search` is `MEDIUM` risk with `requires_approval=True`. It is read-only,
but it is the one tool that sends what the user asked about to a third party.
That is a privacy risk rather than a destruction risk, and the risk ladder
should say so — a per-query approval is exactly where a person decides whether
that is acceptable for *this* query.

Policy was not weakened for convenience.

## 17. Capability reporting

Automatic, from real runtime state. With no `SEARCH_API_KEY`:

```
Implemented, but execution is switched off for this deployment:
- Web search: Search the public web for a query and return sources.
```

With execution enabled but still no key, the integration reports
`NOT_CONFIGURED` and the capability stays `IMPLEMENTED_UNAVAILABLE`. With
both, it becomes `AVAILABLE_WITH_APPROVAL`. Nothing was added to any prompt by
hand.

## 18. Cache

None. Part 27 prefers a simple request-path implementation, and a cache would
mean storing query content — which can be personal — with a TTL and a privacy
story to write. Not built.
