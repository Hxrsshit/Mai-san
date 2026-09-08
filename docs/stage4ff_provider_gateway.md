# Stage 4F-F — Multi-Provider LLM Gateway

```
                         Mai
                          |
                    LLM Gateway
                          |
          +---------------+-------------------+
          |               |                   |
        Groq        Anthropic API     Claude Subscription
       API key         API key          UNAVAILABLE
          |               |                   |
          +-------+-------+                   x
                  |
           SecureHttpClient
                  |
            NetworkPolicy
                  |
        Normalized LLMResponse
```

---

## 1. Three modes, one active, no fallback

`LLM_PROVIDER` selects exactly one of `groq`, `anthropic_api`,
`claude_subscription`. It is operator configuration. Nothing else selects it —
not a user message, not model output, not a search result, not a memory, and
each of those is asserted by a test.

**There is no fallback in any direction, and that is a security property.** A
provider that quietly failed over would send the user's conversation to a
company they did not choose and bill an account they did not mean to use. A
configured provider that fails produces an error. `build_provider` contains no
exception handler at all, and a test asserts that structurally.

## 2. Why `claude_subscription` is reserved but unavailable

Stage 4F-F's own instruction was to implement subscription support *only if
the current official Agent SDK explicitly permits this exact use case*. It
does not. **Two independent reasons, either sufficient on its own.**

### Anthropic does not permit it

The Agent SDK documentation states, in both the overview and the
authentication section of the quickstart:

> "Unless previously approved, Anthropic does not allow third party developers
> to offer claude.ai login or rate limits for their products, including agents
> built on the Claude Agent SDK. Use the API key authentication methods
> described in the Quickstart instead."

Mai is a third-party product. This is a **permission** boundary, not a
technical one, and no amount of engineering makes it appropriate to cross.
Supported authentication is an API key, or Bedrock / Claude Platform on AWS /
Vertex / Foundry — all of which are API billing.

### Mai could not police its network

The Python Agent SDK drives a native Claude Code binary. Its `Transport`
abstraction is the message channel between the SDK and that subprocess —
`connect`, `write`, `read_messages`, `close`, `is_ready`, `end_input` — **not
an HTTP transport**. Mai's `SecureHttpClient` and `NetworkPolicy` cannot
govern where that subprocess connects.

Stage 4F-F's own rule: *"A provider that can make arbitrary external requests
outside Mai's security model must NOT be marked AVAILABLE."*

The SDK also ships built-in file, bash and web-search tools and loads
configuration from `.claude/` and `~/.claude/` by default. Those can be
narrowed (`tools=[]`, `setting_sources=[]`), but narrowing a capability set is
a weaker guarantee than not having the capability, and it does not address
either reason above.

### What "reserved" means concretely

- The mode **exists** in `ProviderMode`, so selecting it produces a specific,
  explanatory refusal rather than a "did you mean?" typo message.
- It has **no builder** in the factory registry. Not a builder that raises —
  no entry at all, because a mode whose builder raised is one refactor away
  from a mode that works.
- It has **no host**, so nothing may connect anywhere on its behalf.
- Mai declares **no setting** for a subscription credential, reads no
  `CLAUDE_CODE_OAUTH_TOKEN`, and imports no SDK. Tests assert all three.

If Anthropic ever approves this use for a partner, the second reason is still
waiting, and it is the harder one.

## 3. Configuration

```bash
# One of: groq | anthropic_api
LLM_PROVIDER=groq

GROQ_API_KEY=
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_MODEL=openai/gpt-oss-120b

ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_MODEL=claude-sonnet-5
```

The settings prefix comes from the provider table, not from the mode name.
Deriving it produced `ANTHROPIC_API_API_KEY` for the `anthropic_api` mode —
not a name anyone would write in a `.env` file, and a convention that
surprises is a convention that gets worked around.

### Billing separation

`ANTHROPIC_API_KEY` is an **API** credential, billed to an API account. It is
not a Claude Pro/Max subscription. Holding both changes nothing: explicit
configuration decides, there is no implicit precedence, and selecting
`claude_subscription` while an Anthropic key is present still refuses rather
than quietly moving usage onto API billing. A test asserts exactly that.

## 4. The network boundary is unchanged

Both real providers use the same `SecureHttpClient` as web research, with a
policy carrying:

| | Groq | Anthropic |
|---|---|---|
| Host | `api.groq.com` | `api.anthropic.com` |
| Method | `POST` only | `POST` only |
| Redirects | refused | refused |
| Response cap | 2 MB | 2 MB |
| Transport retries | 1 | 1 |

**Every outbound provider host Mai permits:** `api.groq.com`,
`api.anthropic.com`. That is the whole list, and it is asserted as a set.

### One narrow widening, and where it lives

Anthropic's Messages API rejects a request without an `anthropic-version`
header, and the client's header allow-list is three names. Rather than opening
that list globally — which would hand the header to web research too — the
permission moved onto the policy as `extra_request_headers`, the same shape
`allowed_methods` already had.

The Anthropic provider's policy names one header. Research policies name none.
Every caller stays narrower than the client, and a test asserts that a caller
still cannot set `cookie`, `host`, `authorization` or anything else.

## 5. Credential isolation

The key is passed **per request** and applied at the transport boundary, so no
long-lived object holds it. Verified absent from: the client object, the
request URL, the request body, every header but its own, error messages at
400/401/403/404/429/500/503, logs at DEBUG, runtime facts, the rendered
prompt, the database, container logs, and frontend-visible responses.

Provider error text is scrubbed before it reaches a user — providers do echo
requests back for debugging, and a careless one could include the header it
received.

### `LLMResponse.raw` was removed

It carried the provider's entire response into application state and nothing
ever read it: pure exposure, reachable by any future logger or serialiser, and
exactly what *"never copy raw provider responses wholesale into application
state"* forbids. Everything Mai needs is named: `content`, `model`,
`finish_reason`, `usage`.

## 6. Runtime identity

Runtime facts now report the authentication **method**, never the credential:

```
- LLM provider (the service being called): anthropic_api
- LLM model (an identifier issued by that provider...): claude-sonnet-5
- LLM authentication: an API key (the method only; Mai never sees or
  reports a credential)
```

The structured field keeps its machine value (`api_key`); only the rendered
text differs. That is not cosmetic — a Stage 4F-A security test scans every
prompt for credential-shaped markers and `api_key` is one of them. Writing the
mode as "an API key" keeps that scan blunt rather than teaching it an
exception.

A misconfigured provider reports `unknown` rather than guessing. The field is
derived, frozen and `extra="forbid"`, so nothing can assert it.

## 7. A provider is not a second execution framework

The `app/llm` package imports no `subprocess`, no agent SDK, no
`app.execution`, no `app.tools`, no `app.workflows`, and no database module.
Tests assert each.

The Anthropic provider requests **no** native tools: no `tools`, no
`tool_choice`, no `web_search`, no code execution. Mai's tool registry decides
what exists, Stage 4E's dispatcher is the only execution gateway, and Tavily
remains the consent-gated research path. A provider-native tool would be a
second route around Stage 4C and 4E, so none is requested — asserted by
scanning the module's string literals.

## 8. Known limitations

- **Anthropic API has never been live-tested.** No `ANTHROPIC_API_KEY` is
  configured. Every test drives the real provider, client and policy against a
  stub transport, so only the socket is replaced — but no real Anthropic
  response has been observed.
- **`claude_subscription` is permanently unavailable** on the terms above. It
  is not a "not yet implemented" placeholder in the sense of pending work;
  implementing it would require Anthropic's approval *and* a solution to the
  network-boundary problem.
- **No streaming**, for either provider. `LLMProvider.generate_response` is
  single-shot and always was; this stage neither added nor removed it.
- **The dev compose file bind-mounts `./backend:/app`**, so `backend/.env` is
  visible inside the running container. It is *not* in the image layer — a
  fresh container from the image has no `.env` — and neither `.env` is tracked
  by git. A production deployment would not mount source.
