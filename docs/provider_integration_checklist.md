# Adding or changing an LLM provider: the checklist

Mai talks to exactly one model provider at a time, chosen by `LLM_PROVIDER`.
Implemented today: `groq`, `gemini` (both through
`app/llm/providers/openai_compatible.py`) and `anthropic_api`
(`app/llm/providers/anthropic.py`). `claude_subscription` is a known mode that
is deliberately unavailable. **OpenAI is not implemented**: `LLM_PROVIDER=openai`
fails closed (`tests/test_provider_contract.py::test_unimplemented_providers_fail_closed`).

The rule this checklist exists for: **a configured key is not a working
provider.** Gemini was "configured" on 2026-10-13 while its requests timed
out or failed with HTTP 503 (`gemini-3.8-flash`, provider capacity; see the
changelog, Stage 0 C4); only live requests showed it. Each step below names
the evidence that proves it, and the level that evidence belongs to.

| Level | What it proves | Where |
| --- | --- | --- |
| Unit test | One behaviour of one class, mocked socket | `tests/test_<provider>_provider.py` |
| Contract test | The provider behaves like every other one | `tests/test_provider_contract.py` |
| Provider smoke test | The real provider answers this key and model, now | `python -m app.llm.smoke` |
| Full application tests | Nothing else changed | `cd backend && .venv/bin/python -m pytest` |
| Live deployment check | The running backend uses it and reports it truthfully | `/health`, logs, smoke test in the container |

None of these substitutes for another. A passing mock test says nothing about
the real endpoint; a successful smoke test says nothing about error handling.

### Verification status (2026-10-16)

| Mode | Unit and contract tests | Live against the real service |
| --- | --- | --- |
| `groq` | Yes | **Yes.** Live in the Stage 1 and 2 acceptance reports; the active provider at the last backend restart (2026-10-14), with live chat and memory recall that day (changelog, Stage 0 C4). |
| `gemini` | Yes | **Mixed.** A live smoke test passed when it was added (`b14b71a`, 2026-09-30). On 2026-10-13 `gemini-3.8-flash` timed out or returned 503 (3 x 503 on the smoke test), while `gemini-3.5-flash` on the same key answered. Not re-verified since. |
| `anthropic_api` | Yes | **Never.** No live test is recorded; it has been exercised only through stub transports (no key was configured at Stage 4F-F, `docs/stage4ff_acceptance_report.md`). |
| `claude_subscription` | Selection only | Deliberately unavailable. |
| `openai` | Fails closed | Not implemented. |

All automated provider tests run on SQLite with mocked sockets; the
PostgreSQL-gated tests have not run. A row says what was observed on its
date, not that the provider works now: confirm with step 7.

---

## 1. Configuration and secrets

- [ ] Add `<PREFIX>_API_KEY`, `<PREFIX>_MODEL` and (if it has one) a
      `<PREFIX>_BASE_URL` default to `Settings` (`app/core/config.py`). Keys
      default to `""`; never a real value in code, tests or docs.
- [ ] Register the mode in `app/llm/gateway.py` (`ProviderMode`, `PROVIDERS`
      with its `settings_prefix`) and its builder in `app/llm/factory.py`
      `_REGISTRY`. Nothing else may select it: no routing, no fallback.
- [ ] Add the key to `docker-compose.yml` as `${NAME:-}` and, for operators,
      to the root `.env` (mode 600). Do not pass a base URL through compose
      unless there is a reason: the default pins the host.
- [ ] `Settings.is_llm_configured` must be false without a key **or** a model.

## 2. Endpoint, authentication, request shape

- [ ] Prefer subclassing `OpenAICompatibleProvider` when the provider offers
      an OpenAI-compatible endpoint (Groq, Gemini). Write a separate class
      only for a different wire format (Anthropic), and then copy the shared
      retry, logging and error semantics -- the contract tests enforce them.
- [ ] Confirm the endpoint, auth header and request schema from the
      provider's **official documentation**, and record the link in the
      provider module's docstring. Never guess a model id: list the models
      the key can see, or cite the docs.
- [ ] The key goes in one header, per request (`auth_header=`), never on the
      client, the URL or the body.

## 3. Network boundary

- [ ] All traffic goes through `SecureHttpClient` built by
      `app/llm/transport.build_provider_client`: HTTPS only, a single-host
      allow-list derived from the base URL, private ranges refused after DNS,
      redirects refused, 2 MB response cap, POST only. No SDK that opens its
      own connections; no second HTTP client.
- [ ] A provider-specific header (e.g. `anthropic-version`) is named in
      `extra_request_headers`, nothing wider.

## 4. Timeouts, retries, errors

- [ ] Per attempt: `LLM_TIMEOUT_SECONDS` read, 10 s connect. Retries:
      `LLM_MAX_RETRIES` (bounded), backoff with jitter, `Retry-After` honoured
      but capped.
- [ ] Retry only transient failures: timeouts, connection errors, 408, 429,
      500, 502, 503, 504, **plus every status the provider documents as
      transient** (Anthropic: 529 `overloaded_error`). Never retry 400, 401,
      403, 404, a malformed reply or a policy refusal.
- [ ] Map to `app.core.errors`: missing key or model ->
      `LLMNotConfiguredError` (no request is made); 401/403 -> `LLMAuthError`;
      429 -> `LLMRateLimitError`; timeout -> `LLMTimeoutError`; 5xx ->
      `LLMError("... unavailable (HTTP n)")`; other 4xx -> `LLMError` with the
      scrubbed provider message; unusable 200 -> `LLMResponseError`.
- [ ] `generate_response` calls `_record_success()` / `_record_failure(exc)`
      so `/health` can report observed reachability.

## 5. Logging and redaction

- [ ] Log `LLM request started` / `succeeded` / one `LLM request failed` per
      attempt with `provider, model, attempt, max_attempts, reason,
      will_retry` -- the shared shape. Never the key, a prompt, a reply or a
      header.
- [ ] Scrub the key (bare and `Bearer`-prefixed) from any provider error text
      before it reaches an `LLMError` message.

## 6. Tests

- [ ] Add one `ProviderSpec` to `CONTRACT` in `tests/test_provider_contract.py`
      (endpoint path, auth header, reply body, documented overload statuses).
      The whole matrix then runs against it: selection, missing key/model,
      request shape, normalisation, malformed replies, every status class,
      timeouts, retry bounds, redirects, forbidden endpoints, oversized
      replies, key redaction, prompt privacy, attempt logging, outcome
      recording, concurrency, and no import of authorization or execution.
- [ ] Provider-specific unit tests for anything the contract cannot express
      (system-prompt placement, usage field names, refusal stop reasons).
- [ ] Prove the tests bite: reintroduce one defect at a time and watch a test
      fail, then restore.
- [ ] Run the full backend suite. Frontend changes also need the backend
      security suite, which scans frontend source.

## 7. Smoke test, deployment, live verification

1. Smoke test before switching anything:
   `docker exec -w /app mai-backend python -m app.llm.smoke` logs one
   `Provider smoke test` line through Mai's logging (provider, model, `ok`,
   `error_code`, `latency_ms`, reply length -- never the reply, prompt or
   key) and exits 0 only if the provider answered. It writes no data. Run it
   once, not in a loop: it spends real quota, and it runs in its own process,
   so it does not change what the running server's `/health` reports.
2. Switch: back up the root `.env`, set `LLM_PROVIDER` (and the key), then
   recreate **only** the backend: `docker compose up -d --no-deps --no-build backend`.
   Confirm the log line `LLM provider initialised | provider=... model=...`.
3. Verify live: smoke test again inside the container; `/health` shows
   `llm.reachability` `unverified` until the first real request, then
   `reachable` -- or `failing` with an error code; authentication still 401s
   without a token; ports still on `127.0.0.1`. Expect the top-level
   `status` to read `degraded`, with `llm.healthy` false, after every start
   until the first model request succeeds: health reports an observed
   success, never a configured key, and makes no model call itself (the
   endpoint still answers 200).
4. Rollback: restore the `.env` backup and recreate the backend the same way.
   Selecting a provider changes no schema, permission, memory or task.

## 8. Documentation

- [ ] Changelog entry with the evidence actually gathered at each level above.
- [ ] Do not describe a provider as working until step 7 has passed live.
