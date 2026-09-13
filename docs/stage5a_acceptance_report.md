# Stage 5A — Acceptance Report

## Status: **PASS**

- **Baseline:** `1d9df26` (Stage 4H acceptance report), `git status --short` empty, 3424 tests
- **Final commit:** `c255b2d`

---

## Root cause: two defects, and only one was a typo

The reported failure, reproduced against `1d9df26`:

> *"if i provide you a task would you give me a notification or add the task
> to my google callender?"* → interpreted as a web search for `"callender"`.

And then, unprompted, this:

> *"what is on my google **calendar** tomorrow?"* → web search for
> `"calendar tomorrow"`.

**Spelled correctly, and still wrong.** A plain calendar question was being
sent to an external search provider.

| | cause | affects | fix |
|---|---|---|---|
| **A** | `google` is a research trigger verb, so `"my google calendar"` parses as `google <subject>` | *correctly spelled* text | two guards on the `google` verb |
| **B** | `"callender"` appears in no grammar's vocabulary | misspelled text | the normalisation layer |

A stage that fixed only B would have left an ordinary, correctly spelled
question misrouted. Both are fixed, and both have regression tests.

A third gap surfaced while fixing A: the calendar grammar required the
possessive to be adjacent to the noun, so `"my google calendar"` matched no
family even once research stopped claiming it. A closed list of qualifiers now
sits optionally between them, and `check`/`see`/`open`/`look at` were added to
the `show_calendar` family — *"check my calendar"* is how most people say it.

## Architecture

```
user message → normalise() → existing grammars → existing gates
     ↓
  stored, prompted, audited — untouched
```

`app/language/normalise.py`. No parallel intent system; the existing pipeline
is unchanged downstream of recognition.

**Two structural properties carry the security argument:**

> It can only ever produce a word Mai already recognises.
> It can never produce a word Mai does not.

The output vocabulary is a **closed set** of ~40 terms, each already matched
by an existing grammar. The most a typo can become is a word that was already
going to be routed somewhere — never a capability, because no capability name
is in the set. A test enumerates the set and asserts every table entry lands
inside it.

**Nouns, not verbs.** A verb turns a sentence into a request, so a layer that
repaired broken verbs could manufacture instructions from noise.
`"serach the web for my passwords"` is left exactly as written and matches
nothing. The one dual-sense word, `schedule`, is present as a noun; its verb
sense reaches only the write-request detector, which performs nothing and
refuses — proved by test, not asserted.

**One string, one place.** Only the user's own message, only in the chat turn.
An AST test walks every module and requires `app.language` to be imported by
exactly one file.

## Normalisation rules

Table first (≈60 audited entries), then bounded Damerau-Levenshtein against
the same closed vocabulary. A correction requires: token 5–24 characters;
distance ≤ 1 below eight characters and ≤ 2 above; exactly one nearest term
unless the tied candidates are inflections; ASCII letters only; not glued to
an identifier or another letter.

| bound | |
|---|---|
| `MAX_INPUT_CHARS` | 2000 — beyond it returned **unchanged, not truncated** |
| `MAX_TOKENS` | 400 |
| `MIN`/`MAX_TOKEN_CHARS` | 5 / 24 |
| `MAX_EDIT_DISTANCE` | 2 |
| `MAX_CORRECTIONS` | 8 |

Cost is `O(tokens × vocabulary × word length)`, every factor a constant, with
the distance matrix banded to the limit. A test runs eight pathological shapes
five times each under a 10-second ceiling; the observed time is milliseconds.

## Examples

| input | normalised | routes to |
|---|---|---|
| `what is on my google callender tomorrow?` | `…google calendar…` | calendar read |
| `what's on my calender tomorrow` | `…calendar tomorrow` | calendar read |
| `check my calandar` | `check my calendar` | calendar read |
| `can you add this to my google callender?` | `…google calendar?` | **write refused** |
| `set a remider for tomorrow` | `set a reminder…` | ordinary — no such capability |
| `send me a notifcation` | `send me a notification` | ordinary — no such capability |
| `search the web for the latest news about OpenAI` | *(unchanged)* | research |
| `I need to seperate these` | *(unchanged)* | ordinary |
| `the remainder of the money` | *(unchanged)* | ordinary |
| `user@calender.example` | *(unchanged)* | ordinary |
| `serach the web for my passwrd` | *(unchanged)* | ordinary |

## Tests

| | |
|---|---|
| Baseline | 3424 |
| Total | **3574** |
| Added | **150** |
| Full suite #1 (declared order) | pass, exit 0 |
| Full suite #2 (randomised order) | pass, exit 0, identical |
| Failures | 0 |
| Skips | 1 — `TEST_POSTGRES_URL is not set` (also run explicitly against the live server) |

New: `tests/test_normalisation.py`,
`tests/security/test_normalisation_security.py`.

## Adversarial tests

| attack | result |
|---|---|
| broken search verb (`serach`, `gogle`, `reserch`, `seach`, `googel`) | left alone; not a request |
| privileged near-words (`admn`, `roott`, `passwrd`, `delet`, `exeucte`, `emial`, `aproove`) | left alone; outside the vocabulary |
| prompt injection carrying typos (5 variants) | repaired for legibility, routed by the same gates, grants nothing |
| **Unicode confusables** (Cyrillic а/е/г, fullwidth, combining, zero-width) | not corrected |
| punctuation abuse (`c.a.l.l.e.n.d.e.r`, 200 `?`, brackets) | survivable, bounded |
| whitespace abuse (tabs, newlines, 500 spaces, NBSP) | spacing preserved exactly |
| repeated typos (×300) | capped at 8 corrections |
| extremely long input (50,000 chars) | returned whole, unchanged |
| calendar content | never normalised — asserted structurally and behaviourally |
| discussion (`what is a callender`, `I don't want you to check my calandar`) | repaired, then refused by the existing negative guards |
| capability crossing (`add this to my google callender`) | understood as a write, refused, nothing performed |

### A real vulnerability, found and closed in-stage

The tokeniser captures ASCII letters, so a homoglyph splits a word:

```
"cаlendar"  (Cyrillic а)  →  fragment "lendar"
"lendar"    →  two insertions from "calendar"  →  "cаcalendar"
```

Text the user never wrote, assembled out of a confusable, one grammar match
from being a request. Closed twice: a token adjacent to any letter of any
script is a fragment, and two edits now require ≥ 8 characters.

## Mutation testing

- **Total 29 · Caught 29 · Survivors 0**

Two rounds: **21/30**, then **29/29**.

Nine survivors in round one. Eight were guards no test could reach, each
because a different guard refused the input first:

| | why it was unreachable |
|---|---|
| the canonical short-circuit | removing it leaves the *text* identical and only the correction record differs |
| the short-token distance rule | the adjacent-letter check refuses confusable fragments earlier, so only a bare fragment reaches it |
| the unrelated-tie rule | needed a token equidistant from two non-inflections — `"thuesday"` |
| the token-shape check | needed an apostrophe *inside* a word — `"cal'endar"` |
| the `google` product guard | every case had a determiner too, so the determiner guard refused first |
| the `google` determiner guard | and vice versa |
| the calendar qualifier list | needed a message that only over-matches with an open qualifier |
| the explanatory guard | every discussion case failed to match a family head at all, so the guard was never consulted — and repairing the noun is exactly what makes the head match |

**The ninth was unreachable code, and was deleted.** A
`distance × 3 ≤ length` rule sat beside the length rule and could never fire:
distance is capped at 1 below eight characters and 2 above, and `3 > 8` is
false. Rather than keep a protection that does not operate, it was removed and
a test now enumerates the (length, distance) space to prove the reasoning —
so if the length rule is ever loosened, the claim fails loudly.

## Live verification

Real running instance: real Google Calendar (connected account), real Tavily,
real Groq, real PostgreSQL, real frontend.

| § | request | result |
|---|---|---|
| 26.1 | `what is on my google callender tomorrow?` | **calendar read**, real events, `intent=calendar_schedule`, window `tomorrow`. No web search |
| 26.2 | `can you add this to my google callender?` | `write_not_supported` — *"I can only read your calendar…"*. No execution, no request to Google. It did not pretend the event was created |
| 26.3 | `search the web for the latest news about OpenAI` | research proposal, unchanged: *"I can search the web for **the latest news about OpenAI**…"* |
| 26.4 | Stage 4H briefing, *with typos*: `I have a meetng with Stripe tomorrow. Give me a breifing…` | proposal → `yes` → `completed`, 1 real calendar event, 5 real Tavily results, 4064-character briefing. Boundaries unchanged |
| — | the original motivating message | **no web search.** Truthful capability answer: *"my calendar tool is read-only… I also don't have a way to send you push notifications."* |

**Provenance, visible in the UI:** the message bubble displays
`what is on my google callender tomorrow?` — the typo the user typed. The
normalised string appears nowhere on screen, in storage, or in the API
response.

| infrastructure | |
|---|---|
| PostgreSQL | `0009 (head)`, **no migration added**; full chain run against the live server |
| Docker | image unchanged — 29 packages, no pytest, no tests; **no dependency added** |
| Ports | loopback-only on all three services |
| Frontend | renders correctly, **zero console errors**, storage and cookies empty, no credentials |
| Credentials | 0 occurrences in container logs, `messages`, `workflows`, `executions`, `memories` |
| Existing integrations | calendar, Tavily, briefing, memory and execution gates all verified live |

## Security boundaries

| question | answer |
|---|---|
| Can normalisation grant a capability? | No. Its output vocabulary is closed and contains no capability name |
| Can it authorize a tool or create an approval? | No. It returns a string; every gate runs afterwards, unchanged |
| Can it bypass an approval? | No. Consent turns are read from the **original** text |
| Can it turn untrusted content into intent? | No. It is imported by one module and applied to one string |
| Can a calendar title determine a search query? | No — the Stage 4H invariant, re-tested here with typos in the user's message |
| Can a model's "the user probably meant X" grant X? | No. Normalisation is deterministic and model-free; the LLM is not consulted |
| Is the original preserved? | Yes — stored, prompted, displayed and returned unchanged |

## Remaining limitations

- **`calender` is a real English word** (a paper-smoothing machine) and is
  corrected anyway. The brief requires the mapping; in any message Mai will
  realistically see it is a misspelling. The repair changes no capability,
  only which grammar reads it.
- **Nouns only.** A misspelled verb is never repaired, so *"serach the web for
  X"* is not understood. Deliberate.
- **Confusables are not folded**, only refused. The user gets no answer rather
  than a wrong one.
- **The vocabulary is hand-written.** An unanticipated misspelling is repaired
  only if edit distance happens to reach it.
- **No context sensitivity.** Corrections are per token.
- **An event *title* can contain anything its creator put there**, including a
  URL. Field-level minimisation drops `hangoutLink` and `conferenceData`, but
  a link typed into the title travels with the title. Observed in live
  verification; pre-existing, and not something this stage changes.
- **English only**, inherited.

## Recommendation

Stage 5A is complete. The reported failure is fixed at both its causes, the
normalisation layer adds no authority, and every existing boundary was
re-verified live.

Unrelated and still outstanding: **upgrade FastAPI/starlette past 1.0** to
close the five remaining advisories, none currently reachable.

## Outstanding user actions

- **Rotate the Google OAuth client secret** if not already done — it was
  printed by a Docker Compose parse error earlier in this session.
- The credentials exposed earlier in development should still be revoked: the
  Groq API key, both OpenRouter keys, the GitHub personal access token, and
  the first Tavily key.
