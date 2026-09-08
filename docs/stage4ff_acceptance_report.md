# Stage 4F-F — Acceptance Report

## Status: **PASS**

- **Baseline commit:** `676b2e9` (Stage 4F-E)
- **Final commit:** `189e458`

---

## Provider status

| Provider | Status | Auth | Network path |
|---|---|---|---|
| Groq | **Available**, live-verified | API key | `SecureHttpClient → NetworkPolicy → api.groq.com` (POST only, no redirects) |
| Anthropic API | **Available**, not live-verified (no key configured) | API key | `SecureHttpClient → NetworkPolicy → api.anthropic.com` (POST only, no redirects) |
| Claude subscription | **Unavailable — reserved slot, no builder** | n/a | none; no host permitted |

## Anthropic / Agent SDK verification

- **SDK/package/version:** none installed, and none added. `claude-agent-sdk`
  is not a dependency; no `claude` CLI is on the host.
- **Official support status:** **Not permitted for this use case.** The Agent
  SDK documentation states, in both the overview and the quickstart's
  authentication section: *"Unless previously approved, Anthropic does not
  allow third party developers to offer claude.ai login or rate limits for
  their products, including agents built on the Claude Agent SDK. Use the API
  key authentication methods described in the Quickstart instead."*
- **Subscription authentication mechanism:** none available to a third-party
  product. Supported methods are `ANTHROPIC_API_KEY`, or Bedrock / Claude
  Platform on AWS / Vertex / Foundry — all API billing.
- **Network transport:** the SDK drives a native Claude Code binary. Its
  `Transport` abstraction is the SDK↔subprocess message channel (`connect`,
  `write`, `read_messages`, `close`, `is_ready`, `end_input`) — **not** an HTTP
  transport. Mai's `SecureHttpClient`/`NetworkPolicy` could not govern its
  outbound traffic.
- **Tool permissions:** built-in file, bash and web-search tools; loads
  `.claude/` and `~/.claude/` config by default. Narrowable via `tools=[]` and
  `setting_sources=[]`, but narrowing a capability set is weaker than not
  having the capability.
- **Important limitations:** two independent blockers, either sufficient alone
  — a permission boundary Anthropic sets, and a network boundary Mai could not
  enforce. Implemented as a reserved, permanently-refused slot.

## Tests

| | |
|---|---|
| Total | **2818** (from 2633 at 4F-E baseline… see note) |
| Added | **117** |
| Full suite run #1 | pass (exit 0) |
| Full suite run #2 | pass (exit 0), identical — no order dependence |
| Failures | 0 |
| Skips | 1 — `test_migration_compatibility.py:349, TEST_POSTGRES_URL is not set` (pre-existing, documented) |

New: `tests/test_llm_gateway.py` (33),
`tests/security/test_provider_gateway_security.py` (55), plus rewrites in five
existing files.

*Note: 2727 was the count at the end of 4F-E; 2818 is the count now.*

## Mutation testing

- **Mutations:** 21
- **Caught:** 21
- **Survivors:** 0
- **Score: 21/21**

| | Mutation | Result |
|---|---|---|
| F1 | Subscription provider marked available | PASS |
| F2 | Availability gate removed from the factory | PASS |
| F3 | Unknown provider falls back to groq | PASS |
| F4 | Provider host allow-list widened | PASS |
| F5 | Network host allow-list accepts any host | PASS |
| F6 | Provider policy permits GET as well as POST | PASS |
| F7 | Redirects followed by the provider policy | PASS |
| F8 | Header allow-list opened globally | PASS |
| F9 | Per-policy header widening dropped | PASS |
| F10 | Anthropic credential moved out of its header | PASS |
| F11 | Anthropic error text no longer scrubbed | PASS |
| F12 | Runtime facts report a fixed auth mode | PASS |
| F13 | `RuntimeFacts` becomes settable | PASS |
| F13b | `ToolCapability` becomes settable | PASS |
| F14 | Anthropic requests native server-side tools | PASS |
| F15 | A subscription credential is read from the environment | PASS |
| F16 | Provider set reopened to arbitrary names | PASS |
| F17 | Provider response copied wholesale into state | PASS |
| F18 | Transport retries layered under provider retries | PASS |
| F19 | Subscription mode gains a builder | PASS |
| F20 | Anthropic prefix collides with Groq's credential | PASS |

**One survivor on the first run, and it was my own error.** F13 used
`.replace(old, new, 1)` against a string that appears twice in
`app/runtime/schemas.py` — so it mutated `ToolCapability`'s config rather than
`RuntimeFacts`'. It also revealed that my mutation test list omitted the file
covering `ToolCapability` entirely. Both were fixed: the mutation was
re-anchored on text unique to each class, split into F13/F13b, and
`test_capability_truthfulness.py` was added to the run. A mis-aimed mutation
proves nothing, and reporting 19/20 would have implied a real gap where there
was a targeting bug.

## Live verification

| | Result |
|---|---|
| **Groq** | **Performed.** Real request through the gateway; Mai answered *"I run on the Groq provider using the openai/gpt-oss-120b model and authenticate via an API key"* — provider, model and auth mode all truthful. Stage 4F-C boundary confirmed active: client is `SecureHttpClient`, policy hosts `['api.groq.com']`. |
| **Anthropic API** | **Not performed** — no `ANTHROPIC_API_KEY` is configured, and I did not ask for one. Exercised against a stub transport through the real provider, client and policy, so only the socket was replaced. |
| **Claude subscription** | **Not performed, and not fakeable.** Refused in-container with `llm_provider_unavailable` and its stated reason. |
| **Tavily regression** | No regression: research and workflow suites pass unchanged. |
| **PostgreSQL** | No migration needed and none added; database remains at `0009 (head)`. Stage 4F-E lifecycle intact (6 workflows, 16 executions readable). |
| **Docker** | Full stack healthy. Provider switching verified in-container for all three modes. |
| **Frontend** | Conversation renders the truthful provider/model/auth answer. Zero console errors. No provider selector added. |

## Security audit

| Severity | Count | Items |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 0 | — |
| Low | 0 | — |
| Informational | 2 | see below |

**Fixed during the stage** (found by me, not pre-existing findings):

1. **`LLMResponse.raw` copied the entire provider response into application
   state**, and nothing read it — pure exposure, and precisely what §13
   forbids. Removed.
2. **`anthropic-version` was refused by the client's header allow-list**, so
   the Anthropic provider could not work. Fixed by adding
   `extra_request_headers` to `NetworkPolicy` rather than opening the global
   allow-list — research policies still name no extra headers.
3. **`docker-compose.yml` required `GROQ_API_KEY` unconditionally**, so
   selecting Anthropic still demanded a Groq key. Both are now optional at
   compose time; the configured provider reports its own missing credential.
4. **Settings resolved `ANTHROPIC_API_API_KEY`** for the `anthropic_api` mode,
   by name convention. Replaced with an explicit prefix in the provider table.

**Informational, deferred with reasons:**

- **The dev compose file bind-mounts `./backend:/app`**, so `backend/.env` is
  visible inside the running container. Verified *not* in the image layer — a
  fresh container from `mai-backend:latest` has no `/app/.env` — and neither
  `.env` is git-tracked. Deferred because it is a development convenience, not
  a production deployment shape.
- **Anthropic API is unverified against the real service.** Deferred because
  verifying it requires a credential I will not ask for or handle.

### Checklist answers

- **Provider cannot grant itself permissions / approve tools / bypass Stage
  4C, 4E or research consent** — the `app/llm` package imports no
  `app.execution`, `app.tools`, `app.workflows`, database module, `subprocess`
  or agent SDK. Asserted by AST.
- **No provider requests native tools** — no `tools`, `tool_choice`,
  `web_search`, `computer_use`, `code_execution`, `bash` or `text_editor`
  literal appears in the Anthropic provider. Asserted.
- **API key vs subscription confusion** — impossible: no subscription setting
  exists, no `CLAUDE_CODE_OAUTH_TOKEN` is read, and selecting
  `claude_subscription` with an Anthropic key present still refuses.
- **Unauthorized provider fallback** — `build_provider` has no exception
  handler and exactly one builder call site. Asserted by AST.
- **SSRF / DNS rebinding / redirects / ports / HTTP** — unchanged from Stage
  4F-B/C and re-tested for the new host: 13 hostile Anthropic endpoints all
  refused with nothing dialled.
- **Availability** — transport retries pinned to 1, provider retries bounded,
  response size capped at 2 MB, timeouts finite.

## Credential audit

Synthetic sentinels (`sk-ant-SENTINEL-NEVER-REAL-…`, `gsk-SENTINEL-…`) and the
real configured Groq key were both checked. **Confirmed absent from every
surface:**

| Surface | Result |
|---|---|
| Logs (application at DEBUG, and container logs) | 0 occurrences |
| Database — `messages`, `memories`, `execution_events`, `workflows` | 0 |
| Prompts (rendered runtime facts, and `/api/prompt/debug`) | 0 |
| Memory | 0 |
| Runtime facts | 0 |
| Frontend page and API responses | 0 |
| Errors at 400/401/403/404/429/500/503 | 0 |
| Audit events | 0 |
| Test artifacts / git-tracked files | only deliberate synthetic sentinels |

## Network audit

**Every outbound provider host permitted by policy:**

- `api.groq.com`
- `api.anthropic.com`

That is the complete list, asserted as a set (`PERMITTED_PROVIDER_HOSTS`).
`claude_subscription` has **no** host. Each provider's own client is narrower
still — one host, `POST` only, no redirects, 2 MB cap, one transport attempt.

## Remaining limitations

Stated honestly; none of these is marked resolved.

1. **Anthropic API has never been exercised against the real service.**
2. **`claude_subscription` is permanently unavailable**, not pending work.
   Enabling it would need Anthropic's approval *and* a solution to the network
   boundary — and the second is the harder one.
3. **No streaming** for either provider. Unchanged, not a regression.
4. **The eight dependency advisories remain open**, unchanged from 4F-C: no
   fix is installable (the pinned versions are still the latest available on
   the index), and none of the vulnerable code paths is used. **No new
   dependency was added this stage** — Anthropic is implemented against the
   existing `httpx` boundary rather than the `anthropic` SDK, so there is no
   new transitive tree, network behaviour or credential store to review.
5. **The dev compose bind-mount** described above.

## Recommendation

**Safe to proceed to Stage 4F-G.** Every Stage 4F-E guarantee is intact, the
network boundary is unchanged and now covers a second host under the same
rules, and the one capability that could have weakened it — the Agent SDK
subscription path — is refused rather than accommodated.

The most useful work before or alongside 4F-G is small: **obtain an Anthropic
API key and run the live verification**, since that is the single claim this
report cannot make.

## Outstanding user actions (unchanged)

The credentials exposed earlier in development should still be revoked: the
Groq API key, both OpenRouter keys, the GitHub personal access token, and the
first Tavily key.
