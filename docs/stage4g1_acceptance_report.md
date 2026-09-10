# Stage 4G.1 — Acceptance Report

## Status: **PASS, with one acceptance criterion unavailable**

- **Baseline commit:** `87ab02a` (Stage 4F-G, verified — working tree clean)
- **Final commit:** _(filled in below)_

**§17 live verification against a real Google account was not performed.** No
Google OAuth client is configured on this machine, which §17 anticipates:
*"If Google OAuth is not configured, report live verification as
unavailable."* Everything verifiable without a real grant was verified on the
running Docker stack, and the §"Live verification" section separates what ran
from what did not.

---

## Root cause

Reproduced against `87ab02a` before any change: **8 of the brief's 19
phrasings were recognised.**

The eleven failures were not near-misses. For every one of them, *no family
head matched at all* — so temporal extraction was never reached, and the
question was never a calendar question as far as the system was concerned.

Stage 4F-G modelled exactly one question shape. All five of its families
required a calendar noun, and the module said why: requiring the noun is what
keeps *"I'm free tomorrow"* from reading someone's schedule. That reasoning
was right about the danger and wrong about the coverage, because ordinary
scheduling language comes in two shapes:

| | asks about | example | 4F-G |
|---|---|---|---|
| object-centric | the calendar | "What's on my calendar tomorrow?" | recognised |
| subject-centric | the person | "Am I free tomorrow afternoon?" | **invisible** |

A subject-centric question names no calendar, so no head could match it. The
turn fell through to an ordinary one and the model answered from the runtime
capability section — telling the user Mai could not see a calendar it could
see. That is the untruthfulness Stage 4E.1 exists to prevent and the same
failure mode Stage 4F-F.1 was written to remove.

A second, narrower cause sat behind it: object-centric phrasings also failed
whenever the connective differed — *"What's **happening on** my calendar"*,
*"What's **my schedule**"*, *"**How packed is** my calendar"*, *"Do I have
anything tomorrow"* (no `scheduled`), *"a free **slot**"* (a noun outside the
calendar-noun set).

**So: not a missing phrase.** A grammar that modelled one question shape, and
within it, a handful of connectives.

## Fix

One recogniser, extended — `app/orchestration/calendar_language.py`. No second
routing system, and nothing downstream of recognition changed.

Subject-centric families cannot rely on the calendar noun for safety, so they
rely on **form** instead:

1. **anchored at the start.** "Am I free tomorrow?" opens the sentence; "I
   wish I were free tomorrow" does not.
2. **an explicit time is required.** "Am I free?" asks which day rather than
   guessing a window of private data.
3. **the complement must be temporal.** "Am I free **to speak my mind**?"
   matches the head and is not a calendar question.

The third guard exists because without it three §6-class sentences reached a
clarification prompt: *"Do I have anything to declare?"*, *"Am I free to speak
my mind?"*, *"What am I doing wrong?"*

Three intents — `calendar_schedule`, `calendar_availability`,
`calendar_next_event` — derived by the application from the grammar, never by
a model. The intent travels in the tool arguments as a closed enum, so it is
part of the approval fingerprint and the audit record.

`app/calendar/availability.py` computes free and busy periods by interval
arithmetic. **The model is given the answer to phrase, not the data to reason
over**: a model asked "is the user free?" over an event list is usually right
and occasionally confidently wrong, and a fabricated free hour double-books
someone.

Full design: `docs/stage4g1_calendar_availability.md`.

## Supported examples

All nineteen from §1, plus variations. Fifteen representative:

```
Am I free tomorrow afternoon?            Am I free tomorrow?
Am I busy Friday afternoon?              When am I free tomorrow?
Do I have a free slot tomorrow?          Find me a free hour tomorrow afternoon.
How packed is my calendar tomorrow?      Is there a free slot tomorrow?
Have I got any free time tomorrow?       Am I around Friday afternoon?
Do I have anything tomorrow afternoon?   What's my schedule this afternoon?
What am I doing tomorrow morning?        What's happening on my calendar this week?
Do I have anything scheduled Friday?
```

## Query / time extraction

Asked on **Thursday 10 September 2026, 14:00**, zone `UTC`:

| Input | Intent | Resolved window |
|---|---|---|
| `Am I free tomorrow afternoon?` | availability | `09-11 12:00` → `09-11 18:00` |
| `Am I free tomorrow?` | availability | `09-11 00:00` → `09-12 00:00` |
| `Am I busy Friday afternoon?` | availability | `09-11 12:00` → `09-11 18:00` |
| `Am I free later today?` | availability | `09-10 14:00` → `09-11 00:00` |
| `Am I free right now?` | availability | `09-10 14:00` → `09-10 15:00` |
| `Am I free in the next few hours?` | availability | `09-10 14:00` → `09-10 18:00` |
| `Am I free next week?` | availability | `09-14 00:00` → `09-21 00:00` |
| `What am I doing tomorrow morning?` | schedule | `09-11 06:00` → `09-11 12:00` |
| `What's on my calendar today?` | schedule | `09-10 00:00` → `09-11 00:00` |
| `Do I have any appointments tonight?` | schedule | `09-10 17:00` → `09-11 00:00` |
| `When is my next meeting?` | next_event | `09-10 14:00` → `09-24 14:00` |
| `Am I free?` | availability | **asks which day** |

**Morning now begins at 06:00**, not midnight. Stage 4F-G used `(0, 12)`,
which is defensible for "what's on tomorrow morning" and absurd for "am I free
tomorrow morning" — whose honest answer is not "yes, from midnight".

## Availability behaviour

An event occupies the half-open interval `[start, end)`. That one choice
settles most of §4:

| Case | Behaviour |
|---|---|
| **overlapping** | merged — `10:00–11:30` + `11:00–12:00` → `10:00–12:00` |
| **adjacent** | merged — a zero-length gap is not a gap anyone can use |
| **all-day** | occupies the whole window; treating it as free books a meeting into a holiday |
| **cancelled** | never arrives — `parse_events` drops it, because it is not on the calendar |
| **recurring** | never arrives as a rule — Google is asked with `singleEvents=true`, so instances arrive individually |
| **missing end** | runs to the end of the window. Over-reporting busy is safe; assuming instantaneous invents free time |
| **straddling the window** | clipped |
| **touching the window edge** | an event ending exactly when the window opens does not occupy it |
| **inverted window** | yields nothing rather than raising |
| **timezone** | every timestamp is aware; the window is built in `MAI_TIMEZONE`, and a day is midnight-to-midnight (25 hours across a DST fall-back), not start + 24h |

Gaps under **15 minutes** are not reported as free. Both lists are bounded at
**40 periods**.

## Timezone

Stage 4F-G computed every window in **UTC** — wrong everywhere else. At 09:00
in `Asia/Kolkata`, "tomorrow" resolved to 05:30 tomorrow → 05:30 the day
after: it missed the user's morning and read part of the following day.

`MAI_TIMEZONE` (IANA name, default `UTC`) now decides, and an unknown name is
**refused at startup**. A silent fallback is the dangerous option: the
deployment looks configured, every window is quietly wrong, and the answers
stay plausible enough that nobody checks.

## Tests

| | |
|---|---|
| Baseline (`87ab02a`) | 3094 |
| Total | **3251** |
| Added | **157** |
| Full suite #1 (declared order) | pass, exit 0 |
| Full suite #2 (randomised order) | pass, exit 0, identical |
| Failures | 0 |
| Skips | 1 — `TEST_POSTGRES_URL is not set` (also run explicitly, see below) |

| File | Tests |
|---|---|
| `tests/test_calendar_availability.py` (new) | 136 |
| `tests/security/test_calendar_security.py` (+21) | 48 |

19 positive phrasings, 14 further variations, **27 negative cases**, 5
ambiguity cases, 16 temporal-resolution cases, 16 availability-arithmetic
cases, and the security matrix.

The documented skip was also run: `test_the_chain_runs_on_postgresql` and the
other 14 migration tests pass against the live PostgreSQL container. It stays
skipped by default because the default suite must not require a server.

## Mutation testing

- **Total 42 · Caught 42 · Survivors 0**

Three rounds: **30/42**, then **40/42**, then **42/42**.

Twelve survivors in round one. Three were defective mutations of mine; the
other nine were real gaps, and they fall into four patterns worth naming.

### Guards that no test could reach (J4, J6, J7)

The negation, first-person-past and wish guards all survived deletion. Not
because they are redundant — because **every negative case in the suite was
rejected earlier**, by no family head matching at all. A guard that only runs
after something else has already refused is a guard nothing is exercising.

Sentences were added where a head *does* match and only the guard stops the
read — *"Don't tell me my schedule for tomorrow."*, *"I wish you would show me
my calendar tomorrow."* — each with an assertion that the head still matches,
so the test fails loudly if it ever stops testing what it claims to.

### Tests that read the constant they were pinning (J14, J27)

`assert end - start == timedelta(hours=NEXT_HOURS)` moves with `NEXT_HOURS`,
so widening the horizon to a year still passed. Both now assert literal
ceilings alongside the exact value. The second attempt at J27 *also* failed —
a ceiling of 100 was still looser than the 80 periods the fixture produced.

### A guard whose effect only one input reveals (J28)

Removing the inverted-window guard changed nothing for ordinary events: they
are clipped out of a backwards window anyway. An **all-day** event is not
clipped — it is appended as the window itself — so without the guard the
result contains a busy period running from 18:00 back to 12:00.

### A rule that could not be tested where it lived (J18, J20, J34, J42)

`reads_personal_data` was an inline boolean in the route, so narrowing it from
"any calendar outcome" to "only completed" broke nothing — the only turns
reaching it end-to-end were the completed ones. It is now a named function,
`touches_personal_data`, with every outcome enumerated. Similarly, the
recogniser honoured its `tz` argument and every test proved it *by passing
`tz` itself*; nothing checked that the service supplied one.

## Live verification

Real Docker stack, real PostgreSQL, real outbound network.

### OAuth: **not configured** — §17 unavailable

No Google OAuth client exists on this machine, so no consent screen, no real
authorization code, no real event. §17 anticipates this. **Not performed:** a
real grant, real calendar data, and therefore availability arithmetic over a
live Google payload.

### What did run

| Check | Result |
|---|---|
| Recognition, deployed | `Am I free tomorrow afternoon?` → `calendar_availability` |
| **Time window, deployed** | recorded in PostgreSQL as `2026-09-11T12:00:00+05:30` → `T18:00:00+05:30` under `MAI_TIMEZONE=Asia/Kolkata` — **the configured zone, not UTC** |
| Operation | `calendar_list_events`, `intent: calendar_availability`, `max_results: 10` |
| Authorization | OAuth state, connection state and `EXECUTION_ENABLED` each gate independently; with execution off the answer is *"action execution is switched off"*, not a read |
| SecureHttpClient / NetworkPolicy | the request reached `www.googleapis.com`; a synthetic sentinel token was rejected by Google and Mai reported `reauthorisation_required` |
| Clarification | `Am I free?` → *"which day or time did you mean?"*, **zero external requests, zero execution records** |
| **Write rejection (§17)** | `Create an event tomorrow at 5 PM.` → *"I can only read your calendar…"*. No claim of having created anything; no execution record; no outbound request |
| Negative case | `Am I free to speak my mind?` → not a calendar turn |
| PostgreSQL | healthy, `0009 (head)`, **no migration added**; full chain run against the real server |

### Memory isolation, measured live

Before three calendar turns: `memories=15 entities=13 relationships=10`.
After: **identical**. A control turn ("My favourite programming language is
Rust and I work at Acme") then raised memories to 17 — so extraction was
working, and the calendar turns were being suppressed rather than the pipeline
being idle.

## Privacy audit

Whether calendar or credential data appeared in each place:

| | Result |
|---|---|
| Logs | **no** — 0 occurrences of the sentinel token in container logs |
| Memory | **no** — count unchanged across three calendar turns, control proves extraction live |
| Entities | **no** — count unchanged |
| Relationships | **no** — count unchanged |
| Frontend | **no** — `localStorage`, `sessionStorage`, cookies all empty; 0 matches for `access_token`, `refresh_token`, `bearer`, `ya29` in browser state or the DOM; **zero console errors** |
| Raw API storage | **no** — 0 matches in `messages`, `memories`, `entities`, `relationships`, `executions`, `execution_events`, `conversations` |
| Audit records | execution rows carry the window, the intent and the count — **no event content** |

The wire response carries exactly `{outcome, intent, event_count,
window_label, reason}`. `intent` was added this stage and is metadata about
the *question*, never about the schedule.

**Under the availability intent, no event title, location or organiser reaches
the model at all** — verified with an event whose every field was a distinct
sentinel. The complement is also tested, so that guarantee cannot pass by the
block being empty.

## Network audit

Every permitted outbound host, unchanged from Stage 4F-G:

| Host | Methods | Redirects | Used by |
|---|---|---|---|
| `www.googleapis.com` | `GET` | no | calendar reads |
| `oauth2.googleapis.com` | `POST` | no | token exchange and refresh |
| `api.tavily.com` | `POST` | no | web search |
| `api.groq.com` | `POST` | no | LLM |
| `api.anthropic.com` | `POST` | no | LLM (when configured) |

`accounts.google.com` appears in no policy — the user's browser goes there,
Mai does not. Nothing in this stage adds a host, and no user input reaches a
URL: the window travels as two parameters the application computed.

## Dependency / CVE

**No new dependency was added.** The recogniser, the interval arithmetic and
the timezone handling use `re`, `datetime` and `zoneinfo` from the standard
library.

Stage 4F-G recorded three findings and deferred them. **This stage fixed two
and made the third auditable.**

| | Before | After |
|---|---|---|
| Packages in the runtime image | 36 | **29** |
| `pytest` in the image | yes | **no** — `requirements-dev.txt` |
| Test suite in the image | yes | **no** — `.dockerignore` |
| Transitive versions pinned | no | **yes** — `constraints.txt`, all 29 matched exactly |
| Advisories in the image | 15 across 3 packages | **7 across 1** |

- **`requirements-dev.txt`** splits test tooling out of the runtime install.
- **`constraints.txt`** pins every transitive version, generated from the
  image itself, so "what was audited" and "what runs" are the same question.
  Verified: all 29 installed packages match it exactly.
- **`pip` upgraded in the image**, removing its seven advisories. It is a
  build tool rather than a runtime import, but it is installed and an auditor
  cannot tell that from outside.
- **The test suite is excluded from the image.** It also carried the synthetic
  sentinel credentials the security tests use — harmless, but they make an
  image scan look alarming and would bury a real finding.

**Remaining: `starlette` 0.52.1, 5 unique advisories.** Reassessed against
this stage's new code, none is reachable:

| Advisory | Applies? |
|---|---|
| PYSEC-2026-161, -248 (URL/host reconstruction) | **No** — Mai reads `request.url.path` for logging only, never `.hostname`/`.netloc`; the OAuth redirect comes from settings, never from a request |
| PYSEC-2026-249 (form limits ignored for urlencoded) | **No** — the API is JSON; `request.form()` is never called |
| PYSEC-2026-2281 (StaticFiles SSRF) | **No** — Windows-only; `StaticFiles` is unused |
| PYSEC-2026-2280 (`HTTPEndpoint` getattr dispatch) | **No** — no `HTTPEndpoint`; FastAPI decorator routes always set methods |

Fixing them requires `starlette` ≥ 1.0, which requires a FastAPI major
upgrade. Recommended, as its own change with its own verification.

## Security audit

| Severity | Count | Fixed | Deferred |
|---|---|---|---|
| Critical | 0 | — | — |
| High | **1** | 1 | 0 |
| Medium | 0 | — | — |
| Low | 0 | — | — |
| Informational | 4 | 2 | 2 |

### High — ReDoS in the recogniser (found and fixed in this stage)

Writing the optional opener as a repetition — `(?:hey|so|please|…)*` rather
than `…?` — made **every pattern in the module backtrack catastrophically**.
The input `"so so so so … x"` ran for over four hundred seconds before the
test run was killed.

The message is attacker-controlled text on a path with **no timeout, before
any authentication or gate**, so this is a denial of service, not a slow
parse. Introduced by my own change during this stage and caught by a
deliberate hostile-input probe rather than by the suite.

Fixed by bounding the opener to at most one. Worst-case parse over ten
pathological inputs is now **2.0 ms**, and a regression test asserts it.

### Natural-language routing

- **Can ordinary calendar discussion trigger private-data access?** No — 27
  negative cases, including all nine from §6. Three that reached a
  clarification prompt during development (*"Do I have anything to declare?"*,
  *"Am I free to speak my mind?"*, *"What am I doing wrong?"*) are now
  correctly not calendar turns.
- **Can ambiguous language cause a private query?** No — an availability
  question with no resolvable time asks, and creates no execution record.
- **Can model output force calendar access?** No — `CHAT_CONFIRMABLE_TOOLS` is
  still exactly `{"web_search"}`. The only route to `calendar_list_events` is
  the deterministic recogniser, and it is given the user's message, the clock
  and the zone. Asserted by test.

### Temporal handling

- **Unbounded query from a user-controlled expression?** No — every window is
  bounded: a day, a part of a day, seven days, one hour, or `NEXT_HOURS`.
  Asserted against literal ceilings.
- **Timezone manipulation?** The zone comes from settings; no message can
  change it, and an unknown name stops the process at startup.
- **Malformed input?** The message is capped at 1000 characters, hostile input
  is bounded (above), and the recogniser never raises — not-a-request is the
  default.

### Privacy, authorization, injection

Covered in the sections above and by mutations J29–J42. In particular: OAuth
authorization, Mai operation authorization and execution authorization remain
three separate gates (J38, J39, J40 each caught); an approved availability
read cannot be replayed as a schedule read over the same hours, because the
intent is in the arguments and in the idempotency key (J35, J37).

### Informational

1. **`send_message` returns an 8-tuple.** Unchanged by this stage, and
   carried forward from Stage 4F-G, where growing it from seven silently broke
   a caller. A result object would remove that failure mode. *Deferred.*
2. **The token store is filesystem permissions, not encryption.** Documented
   in the module; an OS keychain or KMS is its own stage. *Deferred.*
3. **Test tooling and the test suite shipped in the runtime image.** *Fixed.*
4. **Transitive dependencies unpinned, so audited ≠ deployed.** *Fixed.*

## Remaining limitations

- **No live verification against a real Google account** — the largest gap.
  Availability arithmetic is verified against fixtures and 42 mutations, not
  against a live Google payload.
- **"next Monday" means the next occurrence of Monday.** English is genuinely
  ambiguous; the alternative needs a convention nobody agrees on.
- **`this week` is a rolling seven days**, inherited from Stage 4F-G.
- **No working-hours concept.** "Am I free tomorrow?" reports 00:00–06:00 as
  free, because Mai does not know when the user sleeps.
- **Day parts are fixed** — morning 06–12, afternoon 12–18, evening 17–24.
- **No frontend connect UI**; single account; `primary` calendar only;
  English only.

## Acceptance criteria (§22)

| | Criterion | |
|---|---|---|
| 1 | "Am I free tomorrow afternoon?" recognised | ✅ |
| 2 | Equivalent language supported | ✅ 19 + 14 variations |
| 3 | Time range deterministic and safe | ✅ application clock, configured zone, bounded |
| 4 | Uses the existing typed `calendar_list_events` | ✅ |
| 5 | No write capability introduced | ✅ absent, not disabled |
| 6 | No over-matching | ✅ 27 negative cases |
| 7 | Authorization application-controlled | ✅ |
| 8 | OAuth separate from operation authorization | ✅ |
| 9 | Calendar data remains private personal data | ✅ |
| 10 | Calendar content cannot become instructions | ✅ absent entirely under availability |
| 11 | Not persisted to memory | ✅ measured live |
| 12 | Data minimisation preserved | ✅ strengthened |
| 13 | SecureHttpClient / NetworkPolicy authoritative | ✅ |
| 14 | Provider selection application-controlled | ✅ no provider consulted in routing |
| 15 | Full regression twice | ✅ 3251, two orders |
| 16 | No security-critical mutation survivors | ✅ 42/42 |
| 17 | Docker | ✅ and hardened |
| 18 | PostgreSQL | ✅ `0009`, no migration |
| 19 | Frontend | ✅ no tokens, no console errors |
| 20 | Runtime dependency/CVE audited | ✅ 15 → 7 advisories |
| 21 | Security audit complete | ✅ |
| 22 | No High/Critical unresolved | ✅ the one High was fixed in-stage |

## Recommendation

**Safe to proceed to Stage 4H**, with §17 outstanding.

Before 4H, two things worth doing, neither blocking:

1. **Create a Google OAuth client and run §17.** Configure it locally; do not
   paste credentials into chat. This is the only claim this report cannot
   make.
2. **Upgrade FastAPI/starlette past 1.0**, closing the five remaining
   advisories. None is reachable today, but "not reachable" is a property of
   current code and this stage added code.

## Outstanding user actions (unchanged, still not done)

The credentials exposed earlier in development should be revoked: the Groq API
key, both OpenRouter keys, the GitHub personal access token, and the first
Tavily key.
