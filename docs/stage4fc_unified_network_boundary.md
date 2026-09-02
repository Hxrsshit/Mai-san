# Stage 4F-C — Unified LLM Network Boundary

**All application-level external HTTP traffic, including LLM provider
traffic, passes through `SecureHttpClient`.**

```
                    Mai
                     |
          +----------+----------+
          |                     |
     LLM Provider           Web Research
          |                     |
          +----------+----------+
                     |
              SecureHttpClient
                     |
               NetworkPolicy
                     |
                 Internet
```

---

## 1. Why the boundary exists

Stage 4F-A wrote `NetworkPolicy` and enforced it nowhere. Stage 4F-B built
`SecureHttpClient` and routed web research through it, leaving the LLM
provider holding its own `httpx.AsyncClient` — documented as a deliberate
exception, on the grounds that its destination comes from operator
configuration rather than user input.

That reasoning was sound and the conclusion was still wrong, for a reason
that has nothing to do with the threat model: **a documented exception and an
invariant are different things, and only one of them survives a future
developer.** A rule with one exception in it is a rule someone can argue their
way into a second exception under. A rule with none is a wall.

Stage 4F-C removes the exception. The test that used to enumerate it has been
replaced by a repository-wide invariant.

## 2. Where it lives

`app/integrations/http_client.py`, and nowhere else. One module in the entire
application may import an HTTP client library, and a test fails if a second
one appears.

## 3. The conflict this stage had to resolve

`SecureHttpClient` was **GET-only by design**, and Stage 4F-B asserted it:

> `test_the_client_offers_no_write_method` — *"Research is read-only, and a
> client that could POST could be talked into submitting one."*

The LLM provider POSTs completions. Following the stage brief literally —
"make the provider use SecureHttpClient" — by adding a general `post()` would
have handed the **web search integration** a write-capable client. That is a
real weakening of Stage 4F-B, and the brief's own first constraint forbids it.

### Resolution: method capability belongs to the policy, not the class

```python
NetworkPolicy.allowed_methods: FrozenSet[str] = frozenset({"GET"})
```

- Web research declares `{"GET"}` — it cannot POST.
- The LLM provider declares `{"POST"}` — it cannot GET.
- The default is read-only, so a policy that says nothing about methods
  cannot write.
- `PUT`, `PATCH`, `DELETE`, `HEAD` and `OPTIONS` are offered by no method on
  the client at all. Nothing needs them, and an unused write verb is a
  capability waiting to be found.

This is **stronger** than what it replaces. Stage 4F-B proved research could
not POST because *no client could*; that guarantee would have evaporated the
moment any caller needed POST. Now research cannot POST because *its own
policy forbids it* — a property of the instance, checkable per caller, and
load-bearing under mutation.

One boundary, and every caller narrower than the boundary.

## 4. How providers use it

```
GroqProvider
  -> build_provider_client(base_url, timeout)     app/llm/transport.py
       -> NetworkPolicy(allowed_hosts={host}, allowed_methods={"POST"}, ...)
       -> SecureHttpClient
            -> policy.permits("POST")             method gate
            -> policy.check(url)                  destination gate
            -> httpx transport
```

The provider's `_get_client()` returns a `SecureHttpClient`. The direct
`httpx.AsyncClient` that used to live there is gone.

## 5. Trusted destinations vs user-controlled ones

Research and provider traffic have genuinely different semantics:

| | Destination comes from | Checked? |
|---|---|---|
| Web research | a constant in application code | yes |
| LLM provider | operator configuration (`GROQ_BASE_URL`) | yes |

The provider's destination is more trusted — an operator setting a base URL
is not an attacker — and it is checked anyway, for two reasons. A
misconfigured or tampered base URL should not be able to reach the cloud
metadata endpoint. And a policy carrying an exemption is a policy someone
will eventually widen.

So the provider gets a **single-host allow-list derived from its configured
URL**, and every other check applies unchanged: scheme (`https` only), port
(443 only), the private/loopback/link-local/metadata address ranges, DNS
resolution, and redirect refusal.

A base URL that cannot be parsed, names no host, or is not HTTPS raises at
client-construction time — converted to `LLMError`, so it surfaces through the
existing error contract rather than as an unmapped exception.

**Consequence, stated plainly:** a locally-hosted model (for example Ollama on
`http://localhost:11434`) is **not reachable** under this policy. Loopback and
plain HTTP are both refused. That is fail-closed and deliberate; supporting a
local provider would need its own explicitly-designed and separately-audited
allowance, not a hole in this one.

## 6. Credential isolation

The API key used to be baked into the client:

```python
httpx.AsyncClient(headers={"Authorization": f"Bearer {self._api_key}"})   # was
```

That put it on a long-lived object any error handler, debug dump or `repr`
could reach. It is now passed **per request** and applied by the transport at
the last possible moment:

```python
await client.post_json(url, json_body=payload,
                       auth_header=("Authorization", f"Bearer {key}"))
```

Verified absent from: the client object, the request URL, the request body,
every header but `Authorization`, the `HttpResponse` object, error messages,
logs at DEBUG, runtime facts, the database, container logs, and
frontend-visible API responses.

`HttpResponse` is deliberately **not** an `httpx.Response`. That object
exposes `.request`, including the headers the request carried — one of which
is the credential. What crosses the boundary instead is a status, decoded
headers, the already-bounded body, and the final URL.

### Provider error text is scrubbed

A provider's error body is text Mai did not write, and it reaches the user
through `LLMError.message`. Providers do sometimes echo the request back for
debugging, and a compromised or merely careless one could include the
`Authorization` header it received. `_scrub` removes the key — both bare and
`Bearer`-prefixed — and applies Stage 3D's `redact` for the general
secret-shaped patterns.

## 7. Headers

An allow-list of three caller-settable names: `accept`, `accept-encoding`,
`user-agent`. A caller cannot set `Cookie`, `Host`, `Authorization` or
anything else — refused, not dropped, because a caller trying to set a header
it may not set is a mistake worth surfacing.

`Content-Type: application/json` is set **by the client** when it is given a
body, never accepted from a caller. A caller-chosen content type is half of an
arbitrary-request primitive; this client sends JSON because it serialises
JSON, and the two cannot disagree.

There is no cookie jar, and no raw-bytes body variant.

## 8. Redirects

Not followed for provider traffic at all. A completions endpoint has no reason
to redirect, and following one would mean re-POSTing application data —
including the prompt — to a destination the origin chose.

Where a policy does permit redirects, every hop is re-checked by the same
policy before the next connection, `httpx`'s own redirect handling stays
disabled, and **the body is dropped on redirect**: re-POSTing to a
destination the origin chose is how one approved write becomes two.

## 9. Timeouts and response size

| Bound | Provider | Research |
|---|---|---|
| Connect | 10s | 5s |
| Read | `LLM_TIMEOUT_SECONDS` (default 30s) | 10s |
| Total | connect + read | 20s |
| Response body | 2 MB | 1 MB |
| Redirect hops | not followed | not followed |
| Transport retries | **1** | 3 |

The provider's total bound is exactly `connect + read` — the two phases a
single request has. Redirects are refused and transport retries are disabled,
so there is no third phase to leave room for, and an arbitrary margin above
the sum would be a number nobody could justify.

### Retries are not layered

The provider already has its own retry loop, and it is the better one: it
honours `Retry-After`, distinguishes retryable statuses from caller errors,
and raises typed `LLMError` subclasses the API layer maps to HTTP responses.
The transport contributes **one** attempt. Layering three transport attempts
inside three provider attempts would be nine requests to a rate-limited
endpoint, and would break the bounded-retry guarantee both layers are trying
to make.

## 10. Streaming

There is none, and there never was. `LLMProvider.generate_response` is
documented single-shot and the request payload sets `"stream": False`. This
stage neither added streaming nor removed it, and **no direct `httpx` was
reintroduced to preserve any**. A test pins the absence so the claim stays
honest.

## 11. The repository-wide invariant

`tests/security/test_network_boundary.py` scans every file under `app/` by
AST and asserts:

- Exactly one module imports an HTTP client library, and it is
  `app/integrations/http_client.py`.
- No module imports a network-adjacent library (`ftplib`, `smtplib`,
  `telnetlib`, `imaplib`, `websockets`, …).
- No module constructs `AsyncClient`, `Client`, `ClientSession` or `Session`.
- No module shells out to `curl`, `wget` or `nc`.
- `socket` appears once, in `policy.py`, and is used only for
  `getaddrinfo` — never `socket.socket`, `.connect(` or
  `create_connection`.
- The boundary itself calls `policy.check` and `policy.permits`, keeps
  `follow_redirects=False`, and never disables TLS verification.

### Intentional exclusions, precisely

| Module | Where | Why it is safe |
|---|---|---|
| `urllib.parse` | anywhere | Splits a URL into parts so the policy can inspect it. Opens nothing. |
| `socket` | `app/integrations/policy.py` only | `getaddrinfo` asks DNS where a name points — exactly what the rebinding check needs — and creates no connection. |
| `os` | `app/execution/tools.py`, `workspace.py` | `os.open` with `O_EXCL｜O_NOFOLLOW` and workspace path handling. Neither reaches the network. |
| `httpx` | tests | Test code builds stub transports. Tests are not application code and do not ship. |

Everything else is a failure.

## 12. Known limitations

- **DNS rebinding is mitigated, not eliminated** (unchanged from Stage 4F-B).
  Addresses are validated before each connection, but `httpx` resolves again
  when it connects, leaving a TOCTOU window. Bounded by the single-host
  allow-list on both callers.
- **A locally-hosted LLM is unreachable** under the provider policy — see §5.
- **`request.url` is used for `.path` in request logging**, and two starlette
  advisories concern URL reconstruction. See the dependency section of the
  acceptance report; no fix is currently installable.
- **The frontend's own `fetch` to Mai's API is out of scope.** It is
  browser-side code talking to Mai itself, not application-level outbound
  traffic, and it is not covered by this invariant.
