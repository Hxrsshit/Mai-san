# Stage 4F-A — External Integration Foundation

The road, with nothing on it yet. Stage 4F-A builds the layer through which
every future external capability must pass, and connects zero external
services. The integration registry ships **empty**, and that is the
deliverable.

---

## 1. Three layers, three responsibilities

```
User
 ↓
Tool             "send_email"        — what operation can Mai perform?
 ↓
Authorization                        — is Mai permitted to perform it?
 ↓
Approval                             — has the user approved this payload?
 ↓
Dispatcher                           — the single place a side effect happens
 ↓
Integration      "gmail"             — which service provides it, and how?
 ↓
Credential       "account's Gmail"   — what authenticates the call?
 ↓
External service
 ↓
ExternalResult → Audit → Mai response
```

No layer does another's job. A tool does not know a URL. An integration does
not know whether Mai is permitted to act. A credential resolver does not know
what is being asked for. The `ToolRegistry` and the `IntegrationRegistry` stay
separate because they answer different questions — one capability may later be
served by a different provider without the capability changing.

## 2. The one method that does not exist

```python
integration.request(url, method, headers, body)   # never
```

That signature is an arbitrary API client. It makes every endpoint the
provider has reachable, turns SSRF into a matter of choosing a string, and
moves the decision about what Mai may do from code review to runtime.

Instead an integration exposes **named operations**, written out by hand in
its own module, each with its own arguments. An operation name that is not in
the table is refused — not guessed at, not forwarded to the provider. A test
asserts the absence of `request`, `http`, `fetch`, `get`, `post`, `send`,
`execute` and nine other names on both the interface and every instance.

There is also no `http_request` tool, and there will not be one.

## 3. The credential boundary

**A credential is never a tool argument, and never reaches the model.**

A tool argument is user input: it arrives over HTTP, it is shown in an
approval prompt, it is fingerprinted, and it is stored in the `executions`
table. A secret there would be persisted in four places and displayed once.

So credentials travel a different road — resolved from configuration at
dispatch time, handed to the integration, never returned to anything that
renders. The split is enforced by the resolver's two methods:

| Method | Returns | Used by |
|---|---|---|
| `describe(requirement)` | `CredentialState` — **no secret** | health checks, capability reporting, logs |
| `resolve_secret(requirement)` | the value | an integration about to authenticate |

`CredentialState` has no `value`, `token`, `key`, `secret` or `password`
field, and a test pins the field set. A tool's argument schema is checked
across the whole registry for anything credential- or URL-shaped.

`resolve_secret` raises rather than returning `""` for a missing credential:
an empty string would be sent to a provider as if it were a key, producing a
confusing 401 instead of a clear local refusal.

## 4. Scopes, from the start

`CredentialRequirement.required_scopes` exists before the first OAuth
integration does, deliberately. `gmail.readonly` and `gmail.send` must be
different grants, because retrofitting scope boundaries onto already-issued
tokens is not possible. A credential is never modelled as all-or-nothing
"Google access".

OAuth itself is **not implemented**. `CredentialType.OAUTH2`, `expires_at` and
`granted_scopes` are the shape it will need.

## 5. Availability is not authorization

`IntegrationState` answers *is the service reachable and authenticated?*
Authorization answers *is Mai permitted to do this?* They are different
questions with different fixes, and conflating them sends someone to look in
the wrong place.

| State | Meaning |
|---|---|
| `NOT_CONFIGURED` | No credential. The normal state for an unused integration. |
| `CONFIGURED` | Configuration present, credential unverified. |
| `AUTHENTICATION_REQUIRED` | OAuth-shaped: no account authorised yet. |
| `AVAILABLE` | Usable. |
| `EXPIRED` | Credential lapsed. |
| `DISABLED` | Switched off by an operator. Deliberate, not a fault. |
| `UNAVAILABLE` | Known to be failing. |

A tool can be perfectly authorized and still unusable because a key is
missing. That reports as `IMPLEMENTED_UNAVAILABLE` in the Stage 4E.1
capability section — availability, not permission.

## 6. The result contract

Ten states, because the response layer can only be truthful about what
happened if it is told what happened:

`SUCCESS` · `FAILED` · `TIMEOUT` · `RATE_LIMITED` · `UNAUTHORIZED` ·
`FORBIDDEN` · `NOT_FOUND` · `VALIDATION_ERROR` · `UNAVAILABLE` ·
`UNKNOWN_ERROR`

`succeeded` is written as identity against a single member rather than as "not
in FAILURES", so a state added later is unsuccessful by default.

An `ExternalResult` carries no provider exception, no URL, no headers and no
raw body. An adapter bug becomes `UNKNOWN_ERROR` with the exception message
dropped — it routinely carries a hostname, a request body, or the
Authorization header that failed.

## 7. The trust boundary

```python
ExternalData(source=..., content=..., retrieved_at=..., classification=...)
```

`trust_level` is a **property returning `UNTRUSTED`**, with no field behind
it. There is deliberately no third trust level: a middle category would become
the place where "this source is fairly reliable" gets written down, and that
is the argument that ends with a web page being obeyed.

The label travels with the content rather than being applied at render time. A
renderer that had to remember to mark something untrusted would eventually
forget; a value that cannot be constructed without a source and a trust level
cannot lose them.

External content is never rendered as an instruction, never grants
authorization, never grants approval, and is **never persisted to the audit
journal** — `audit_metadata()` is an explicit allow-list of numbers and short
labels, and content is absent from it by construction.

## 8. SSRF and network policy

`NetworkPolicy` is an **allow-list of hosts declared by the integration in
code**. There is no wildcard meaning "anywhere", and an empty `allowed_hosts`
refuses everything rather than permitting everything — the direction an empty
collection fails in is the whole point.

Checks, in order, each raising rather than returning a boolean:

1. **Scheme** — `https` only. `file:`, `ftp:`, `gopher:`, `data:` refused.
2. **Host** — not `localhost`, `metadata`, `metadata.google.internal`, …
3. **Port** — 443 only.
4. **Allow-list** — exact match or a true subdomain. Never a substring, which
   would let `evil-example.com` pass a list containing `example.com`.
5. **Resolved address** — every address the host resolves to is checked
   against the private/loopback/link-local ranges. This is the DNS rebinding
   check: an allow-listed name that resolves to `127.0.0.1` or
   `169.254.169.254` is refused, and the allow-list alone would have permitted
   it. IPv4-mapped IPv6 is unwrapped first, or `::ffff:127.0.0.1` would miss
   every IPv4 rule.

An unresolvable host is refused rather than passed to a client that would
resolve it again itself. A refusal names no host and no address — one that
explained itself would be a way to map the network from outside.

Redirects are not followed: a redirect is the provider choosing a destination
*after* the policy approved the first one.

**Stage 4F-A ships no HTTP client.** No module in `app/integrations` imports
`httpx`, `requests`, `urllib.request`, `aiohttp` or `http`. The only socket
use anywhere is `getaddrinfo`, which asks DNS where a name points and opens
nothing.

## 9. Timeouts

Three bounds, because they fail differently. A hanging connect is usually a
firewall dropping packets; a hanging read is usually a provider incident; the
total bound is what stops a sequence of individually-acceptable retries from
holding an execution open for minutes.

| Bound | Default |
|---|---|
| `connect_seconds` | 5 |
| `read_seconds` | 15 |
| `total_seconds` | 30 |

All three are finite and none can be disabled — zero or negative is rejected
at construction, because zero means "no timeout" in most clients and a
misconfigured bound must fail closed rather than become infinite.

## 10. Retries

**Retrying is off by default: `max_attempts = 1`.**

Four independent reasons to refuse a retry, all of which must pass:

1. The operation has a side effect and the policy does not permit retrying
   those.
2. The error is not marked `retryable` on its class.
3. The attempt count is exhausted.
4. The total duration bound is exhausted.

`retry_side_effects` is `False` by default and is the most important line in
the file. A retried `send_email` sends two emails when the first request
arrived and only its response was lost — and the client cannot distinguish
that from a request that never arrived.

A provider's `Retry-After` is honoured but clamped: a provider asking for six
hours must not be able to hold an execution open for six hours. Backoff that
would itself breach the total bound is not taken.

`max_attempts` above 5 is rejected — an unbounded retry count is how a rate
limit becomes an outage.

## 11. Idempotency for external actions

Stage 4E guarantees at most one *local* execution per record, via a UNIQUE
constraint and a conditional UPDATE. That guarantee stops at Mai's boundary.

**Mai's database cannot guarantee an external provider will not process a
duplicated request.** If a request arrives at a provider and the response is
lost, Mai sees a failure and the provider has already acted. No local
constraint can undo that.

The architecture supports passing a provider-level idempotency key where a
provider offers one — Mai execution → Mai idempotency key → provider
idempotency key. Where a provider offers none, exactly-once semantics **cannot
be claimed**, and the honest position is at-most-once by refusing to retry
side effects at all, which is the default.

## 12. Rate limiting

A first-class concern before there is anything to limit. The abstraction
distinguishes per-integration, per-tool, per-account and per-operation limits.
No aggressive global limiter is implemented in this stage — the purpose is
that adding a real external API does not require inventing the concept.

## 13. Audit

Every external operation flows through the Stage 4E journal. `ExternalResult.
audit_metadata()` returns an explicit allow-list:

```
integration · operation · result · reason · latency_ms · attempts · provider_status
```

A status code is a number and cannot carry a secret. Content, headers, bodies,
URLs and credentials are all absent by construction, and `audit.sanitise` is
applied again regardless of who supplied the metadata.

## 14. Logging

Stage 3D's redaction rules apply unchanged. The credential module logs the
credential *identifier* and the *setting name* — never the value, and never
its length, which is a small leak but a real one. A test walks the AST of
`credentials.py` and asserts no log call receives the resolved value.

## 15. Data minimisation

An integration's constructor takes `credentials` and `enabled`. That is the
complete list of what it can hold — no session, no settings, no provider, no
`ContextPackage`, no conversation, no memory. A test pins the signature.

`IntegrationTool.build_operation_arguments` is written per tool and maps
validated arguments onto operation arguments explicitly, so what crosses the
boundary is a readable list rather than whatever happened to be in scope. Data
minimisation as a function signature instead of a rule someone must remember.

`ExecutionContext.integration` is **one adapter, not the registry** — a tool
that declares `example` receives the `example` adapter and cannot enumerate or
reach another. Least privilege as a field type.

## 16. No dynamic code loading

Operation handlers are bound methods the integration built at construction,
never names resolved at call time. No `eval`, `exec`, `compile`, `__import__`,
`importlib`, `pickle`, `marshal`, `setattr`, `globals`, `locals` or `vars`
appears anywhere in `app/integrations` — checked by AST walk, because a
substring scan over a source file also reads its comments, and these modules
discuss the things they must not do.

Registration happens in exactly one function, `build_integrations`, which
imports concrete classes at module scope. The registry is **sealed** after
startup: registration is a startup activity, and a registry that could grow at
runtime is a registry a request could grow.

## 17. Multi-user ownership

`CredentialRequirement.account` defaults to `"default"` because Mai is
single-user today. The field exists now so that adding users is a change of
value rather than a change of shape.

**In a multi-user deployment no credential may be shared between users.** A
credential belongs to whoever granted it, and a resolver must refuse to hand
one user's token to another user's request. Stage 3D already flagged ownership
as the multi-user blocker; nothing here makes it harder.

## 18. Production secret storage

`EnvironmentCredentialResolver` reads process configuration. That is
appropriate for a single-user local deployment where the operator is the
person whose credentials these are. It is **not** appropriate for production:
configuration has no rotation, no access audit, no per-secret permissions and
no encryption at rest beyond the filesystem's.

Before any integration holds a credential that matters, this resolver should
be joined by one backed by a dedicated secret manager. `CredentialResolver`
exists as an interface so that is a new subclass rather than a rewrite.

Nothing writes a secret to PostgreSQL, and no custom encryption scheme is
invented — both deliberate.

## 19. Conflicts with existing stages, and how each was resolved

Five, none of which required rewriting an earlier stage.

### C1 — `ExecutionContext` was filesystem-only

It carried a workspace root and three size bounds. An integration-backed tool
needs none of those and needs something they do not provide.

**Resolved** by adding one optional field, `integration`, holding a single
resolved adapter. Not the registry: a tool gets its own integration and no
other. Filesystem tools receive `None` and therefore cannot reach the network
at all.

### C2 — `ExecutableTool.run` is synchronous

Real external I/O is async, and blocking the event loop on a socket for
seconds is far worse than the microseconds a local file write costs.

**Resolved** by having the dispatcher await the result when it is awaitable —
three lines. The three filesystem tools stay synchronous and unchanged; a
future integration tool can be `async def run` without the dispatcher contract
or any gate above it changing. Making `run` async outright would have touched
Stage 4E for no benefit this stage can demonstrate.

### C3 — audit metadata had no route from executor to journal

**Resolved** by adding `ExecutionOutcome.audit_metadata`, separate from `data`
because the two go to different places under different rules: `data` is
returned to the caller and may contain external content; `audit_metadata` is
persisted forever and may not.

### C4 — capability reporting did not know about integrations

Stage 4E.1 derived state from executor + policy + execution switch.

**Resolved** by adding an integration-readiness input, mapping to the existing
`IMPLEMENTED_UNAVAILABLE` state. No new capability state was needed: a missing
credential is an availability problem, and reporting it as
`IMPLEMENTED_DISABLED` would send someone to look at policy.

### C5 — `ExecutionService.approve` reached for the process registry

The dispatcher took an injectable executable registry; `approve` called
`get_executable_registry()` directly. The two could disagree about what
exists, and a test tool could be dispatched but never approved.

**Resolved** by making the registry injectable on the service, defaulting to
the same process registry. A latent inconsistency rather than a live bug, but
it blocked end-to-end testing and would eventually have blocked a real
multi-registry deployment.

## 20. What replacing the fake provider requires

Part 31's question, answered concretely. Adding a real integration is:

1. A new `Integration` subclass with named operations.
2. A line in `build_integrations`.
3. An `IntegrationTool` subclass and a `ToolDefinition` in `catalog.py`.

It requires **no change** to `ChatService`, the authorization policy, the
approval system, the dispatcher contract, or `RuntimeFacts`. The end-to-end
tests demonstrate this with a fake at the far end; the only thing a real
provider changes is which class is registered.
