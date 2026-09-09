# Stage 4F-G — Acceptance Report

## Status: **PASS, with one acceptance criterion not executed**

- **Baseline commit:** `86d21c8` (Stage 4F-F.1, verified)
- **Scope:** secure OAuth + read-only Google Calendar

**The not-executed criterion is §19, live verification against a real Google
account.** No Google OAuth client is configured on this machine, so no consent
screen was completed, no real authorization code was exchanged, and no real
calendar event was ever read. Everything that could be verified without those
credentials was, on the running Docker stack, and §"Live verification" below
separates what ran from what did not. This report does not claim that criterion
as met.

---

## Tests

| | |
|---|---|
| Baseline (`86d21c8`) | 2920 |
| Total | **3094** |
| Added | **174** |
| Full suite #1 (declared order) | pass, exit 0 |
| Full suite #2 (randomised order) | pass, exit 0, identical |
| Failures | 0 |
| Skips | 1 — `TEST_POSTGRES_URL is not set`, and see below |

Added, all in three new files:

| File | Tests |
|---|---|
| `tests/security/test_oauth_security.py` | 88 |
| `tests/test_calendar.py` | 59 |
| `tests/security/test_calendar_security.py` | 27 |

**The documented skip was also run.** `test_the_chain_runs_on_postgresql`
skips without `TEST_POSTGRES_URL`; it was run explicitly against the live
PostgreSQL container and passed, along with the other 14 migration tests. The
skip remains in the default suite because the default suite must not require a
database server.

## Mutation testing

- **Total 26 · Caught 26 · Survivors 0 · Score 26/26**

Four rounds: 21/23, then 23/23, then 24/24, then 26/26 — the set grew because
each defect found afterwards was added to it.

### The two survivors of round one

| | What it revealed |
|---|---|
| **H6** — expired `state` accepted | **A guard masked by another guard.** `PendingAuthorizations.consume` swept expired entries *before* checking `pending.expired`, so the check was unreachable and deleting it changed nothing. The test that appeared to cover it monkeypatched the clock and was actually exercising the sweep. `consume` now takes the entry first and sweeps afterwards, which makes the explicit check the one that decides. Same pattern as Stage 4F-F.1's G7. |
| **H23** — a partial grant is stored anyway | **A real path with no test.** Google lets a user deselect scopes at the consent screen. Nothing drove `/google/callback` with a token whose scopes were narrower than requested, so removing the `token.covers(...)` check broke nothing. |

### A vacuous test of my own, found while fixing H23

The first version of the partial-grant test passed for the wrong reason. Called
directly rather than through FastAPI, the route's `error` parameter defaults to
the `Query(...)` object, which is **truthy** — so the user-declined branch fired
and the test never reached the scope check it existed for. It now passes
`error=None` explicitly and asserts *which* failure occurred.

Its complement — a covering grant *is* stored — is what exposed this, and is
the reason to write complements.

## Defects found and fixed

Six, of which **three were found only by live verification** and could not have
been found by the test suite as it stood.

### 1. The calendar integration was never registered (live)

`/api/integrations/google/status` reported `authentication_required` while the
chat path answered *"I don't have a Google Calendar integration configured for
this instance."* — for the same instance, at the same moment.

The route constructed its own `GoogleCalendarIntegration`; the chat path looked
the name up in the integration registry, where `build_integrations` had never
registered it. Two objects, two answers. The user-facing consequence is the
bad one: a person one click away from connecting an account was told to go find
an operator.

Fixed by registering it in `build_integrations` — the one place integrations are
registered — and making the route read the registry rather than build its own.
`GoogleCalendarIntegration` now resolves settings per use rather than binding
them at construction, since it is a module-level singleton built at import.

Three tests now fail if the registration is removed, including one that asserts
the two paths *agree* rather than asserting either one's value.

### 2. The credential volume was not writable (live)

The `mai_credentials` named volume was created root-owned `0755` while the
backend runs as uid 1000, so `save` raised `PermissionError`. In a real
deployment this would fail **at the end of the consent flow, after the user had
already granted access at Google**, as a 500 with nothing actionable in it.

Fixed in `docker/backend/Dockerfile`: the directory is created in the image with
the right owner and mode, which Docker then applies when it initialises an empty
named volume. Verified after rebuilding with a fresh volume: `700 mai`.

### 3. A store failure surfaced as an unhandled `OSError` (live, same path)

`FileTokenStore.save` now raises `TokenStoreError("token_directory_not_writable")`
and the callback converts it to a clear refusal. The grant is not treated as a
connection when nothing was stored, so the integration stays disconnected rather
than half-connected. Two tests, and mutations H25/H26.

### 4. The write detector matched a non-calendar deletion

Anchored on the imperative verb alone, `_WRITE_REQUEST` matched *"Delete all my
memories"*. Caught by the existing intent and planning security suites. It now
requires an imperative verb **and** a calendar object.

### 5. Memory extraction persisted calendar contents

Calendar events reach the assistant reply, and memory extraction reads the
assistant reply — so *"you have a 3pm oncology appointment"* would have been
written into durable memory. Suppressed on any turn whose calendar outcome is
not `NOT_CALENDAR`. Mutation H19.

### 6. A caller of `send_message` was missed

`ChatService.send_message` grew from a 7-tuple to an 8-tuple and
`tests/test_providers.py` still unpacked seven. Caught by the full suite. Worth
recording because it is the second element added to that tuple in two stages;
a result object would remove the failure mode, and that is a refactor for its
own change rather than this one.

## Live verification

Real Docker stack, real PostgreSQL, real outbound network. **No real Google
account was connected.**

### What ran

| Check | Result |
|---|---|
| `/google/status`, unconfigured | `not_configured`, names only `calendar.events.readonly`, returns no credential |
| `/google/connect`, unconfigured | `503 integration_not_configured` |
| Authorization URL | host `accounts.google.com`; single scope `calendar.events.readonly`; `S256`, 43-char challenge; loopback redirect; `include_granted_scopes=false` |
| Secret / verifier in URL | absent (asserted on the live response) |
| `state` replay | first callback consumed it, second was refused — one-shot, proven against the running server |
| Forged `state` | refused, message identical to the expired case |
| Credential file | `600 mai`, directory `700 mai`, after a rebuild with a fresh volume |
| `repr` / `str` / f-string of a token | all three mask; no token text |
| **Real request to `www.googleapis.com`** | a synthetic sentinel token was rejected by Google; Mai reported `reauthorisation_required` and told the user to reconnect. The `SecureHttpClient` path, the 401 handling, the refresh attempt and its failure all executed live |
| Sentinel in container logs | 0 |
| Sentinel in API responses | 0 |
| Sentinel in `messages`, `memories`, `executions`, `execution_events` | 0 |
| Sentinel in browser `localStorage` / `sessionStorage` / cookies / DOM | 0 |
| Status and chat agree | `authentication_required` ↔ `not_connected`, after fix 1 |
| Write request, deployed | *"I can only read your calendar…"* |
| Calendar question, execution disabled | *"action execution is switched off for this deployment"* |
| PostgreSQL | healthy, `0009 (head)`, **no migration added by this stage**; full migration chain run against the real server |

The sentinel was a synthetic value (`SENTINEL-ACCESS-…`), never a real
credential, and it was deleted afterwards.

### Frontend (§22)

Deployed UI at `localhost:3000`, calendar turn sent through the real interface.

- The reauthorisation message renders correctly.
- `localStorage`, `sessionStorage` and cookies are **empty** — no token, and no
  `access_token` or `refresh_token` string anywhere in browser state or the DOM.
- **Zero console errors.**
- The `calendar` object on the message response carries exactly
  `{outcome, event_count, window_label, reason}` — no event content.

### What did **not** run

- A real Google consent screen, a real authorization code, a real token
  exchange, a real refresh.
- Any real calendar event. **Data minimisation is verified against fixtures and
  mutation testing, not against a live Google payload.** If Google returns a
  field shape the fixtures do not model, this stage has not proven the
  minimiser handles it.
- The end-to-end connect → ask → answer path with real data.

Configuring a Google OAuth client and repeating §19 is the outstanding work for
this stage. It requires credentials the user must create themselves; they must
not be pasted into chat.

## Security audit

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |
| Informational | 3 |

**Authority** — The scope is a module constant, never derived from a message,
a model output or a request parameter. The time window is computed from the
application clock. `max_results` is bounded at 25 by the schema.

**Token confidentiality** — The token is read and written in exactly one place.
It never enters a prompt, a log line, an API response, the database or browser
state — each verified live against a sentinel. `repr`, `str` and `__format__`
all mask.

**CSRF / replay** — `state` is one-shot and expires; PKCE binds the code to the
process that began the flow; the redirect URI is validated at both ends.

**Partial grants** — A grant that does not cover the read is refused and **not
stored**, so the integration cannot look connected while failing on every use.

**SSRF / network** — Two policies, each narrower than the shared client:
`www.googleapis.com` `GET`-only, `oauth2.googleapis.com` `POST`-only, neither
following redirects. `accounts.google.com` appears in no policy — the user's
browser goes there, Mai does not.

**Prompt injection** — Event text renders into the untrusted personal-data
section, flattened, under a preamble that says it is data written by whoever
created the event. Attendees, links and descriptions never reach the model at
all, which removes the largest injection surface rather than defending it.

**Personal data** — Attendee lists and organiser emails are dropped inside the
integration. Memory extraction is suppressed on calendar turns. The disclosure
states that events reach the model provider before the user leaves for Google.

**Informational:**

1. **The token store is filesystem permissions, not encryption.** Documented in
   the module. An OS keychain or KMS is the real answer and is its own stage.
2. **`send_message` returns an 8-tuple.** It has grown twice and silently broke
   a caller once. A result object would remove the failure mode.
3. **Availability questions are not recognised** — see Known limitations.

## Dependency review (§23)

**No new dependency was added.** No OAuth library, no Google client library;
the flow is written against `httpx` through the existing `SecureHttpClient`.
The new modules import only `app.*`, the standard library, and the pydantic /
FastAPI / SQLAlchemy already present.

`pip-audit` was run against both the pinned requirements and **the deployed
container**, which resolves different transitive versions:

| Package | In image | Advisories | Applies to Mai? |
|---|---|---|---|
| `starlette` | 0.52.1 | PYSEC-2026-161, -248 (URL/host reconstruction) | **No** — Mai reads `request.url.path` for logging only, never `.hostname`/`.netloc`, and the OAuth redirect URI comes from settings, never from a request |
| `starlette` | 0.52.1 | PYSEC-2026-249 (form limits ignored for urlencoded) | **No** — the API takes JSON; `request.form()` is never called |
| `starlette` | 0.52.1 | PYSEC-2026-2281 (StaticFiles SSRF) | **No** — Windows-only; `StaticFiles` is not used |
| `starlette` | 0.52.1 | PYSEC-2026-2280 (`HTTPEndpoint` getattr dispatch) | **No** — no `HTTPEndpoint`; routes are FastAPI decorators, which always set methods |
| `pytest` | 8.4.2 | PYSEC-2026-1845 (`/tmp/pytest-of-{user}`) | **No** at runtime — but see below |
| `pip` | 25.0.1 | 7 advisories | **No** — not invoked at runtime |

None are introduced by this stage and none are reachable in Mai's code paths.
Two hygiene findings, neither blocking:

- **Test tooling ships in the runtime image.** `pytest` is installed in the
  container because `requirements.txt` is one file. Splitting out a
  `requirements-dev.txt` would shrink the deployed surface.
- **Transitive dependencies are unpinned**, so the audited set and the deployed
  set differ (`starlette` 0.49.3 locally, 0.52.1 in the image). A lock file
  would make "what was audited" and "what runs" the same question.

Upgrading `starlette` past 1.0 requires a FastAPI major upgrade. Recommended,
but as its own change with its own verification.

## Regression

Every prior stage's guarantees confirmed by the full suite, twice, in two
orders: Stage 4C authorization · Stage 4E execution gates · Stage 4E.1 runtime
capability truthfulness · Stage 4F-A integration registry · Stage 4F-B
SSRF/network enforcement · Stage 4F-C unified LLM network boundary · Stage 4F-D
research consent · Stage 4F-E controlled multi-step workflows · Stage 4F-F
multi-provider gateway · Stage 4F-F.1 search-language recognition.

Two Stage 4F-B tests were updated rather than broken: the shipped registry now
holds `("google_calendar", "web_search")`, asserted as a literal so a third
integration cannot appear without review.

## Known limitations

- **No live Google verification** (above). The largest gap in this report.
- **Availability questions are not recognised.** *"Am I free tomorrow
  afternoon?"* is a real calendar read that the five families do not match, so
  the model answers that it cannot see the calendar — untrue, and the same
  class of failure Stage 4F-F.1 existed to remove. Not fixed here because the
  read families are part of this stage's specified contract; it is the first
  thing worth doing next.
- **No frontend connect UI.** The flow is API-only.
- **Single account, `primary` calendar only.**
- **English only.**

## Recommendation

The stage is complete and internally verified, with §19 outstanding. Before
Stage 4F-H:

1. **Create a Google OAuth client and run §19.** Configure it locally; do not
   paste credentials into chat.
2. **Fix availability recognition** — small, and it removes a truthfulness
   failure of exactly the kind Stage 4E.1 exists to prevent.

## Outstanding user actions (unchanged, still not done)

The credentials exposed earlier in development should be revoked: the Groq API
key, both OpenRouter keys, the GitHub personal access token, and the first
Tavily key.
