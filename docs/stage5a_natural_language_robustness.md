# Stage 5A — Natural Language Robustness & Typo Tolerance

```
user message
  → normalise()              app/language/normalise.py   (recognition only)
  → existing grammars        calendar · briefing · research   (unchanged)
  → existing gates           authorization · execution · network   (unchanged)

the original message
  → stored, prompted, audited                             (untouched)
```

**Normalisation is an interpretation aid, not an authority mechanism.** It can
change which grammar matches. It cannot change what any grammar is permitted
to do.

---

## 1. Two defects, and only one of them was a typo

The reported failure:

> *"if i provide you a task would you give me a notification or add the task
> to my google callender?"* → interpreted as a web search.

Reproduced against `1d9df26`. But so was this:

> *"what is on my google **calendar** tomorrow?"* → web search for
> `"calendar tomorrow"`.

**Spelled correctly, and still wrong.** The typo exposed the defect; it was
never the cause of it.

| | cause | affects |
|---|---|---|
| **A** | `google` is a research trigger verb, so `"my google calendar"` parses as `google <subject>` | correctly spelled text |
| **B** | `"callender"` is in no grammar's vocabulary | misspelled text |

A stage that fixed only B would have left an ordinary, correctly spelled
calendar question being sent to a search provider.

### Fixing A

`google` now carries two guards `research` does not need, because it is the
only trigger that is also a company whose products Mai integrates with:

- **not after a determiner** — `"my google calendar"` is a noun phrase.
  Nobody commands *"the google X"*.
- **not before a product name** — `calendar`, `drive`, `docs`, `meet`, `gmail`
  and their neighbours. `"google calendar"` names a thing.

`"google quantum computing"` still searches. `"add this to my google drive"`
now reaches an honest *"I can't do that"* instead of searching for the word
"drive".

A second gap surfaced while testing A: the calendar grammar required the
possessive to be adjacent to the noun, so `"my google calendar"` matched no
family even once research stopped claiming it. `_CALENDAR_QUALIFIER` — a
closed list of provider and scope adjectives — now sits optionally between
them. `check`, `see`, `open`, `pull up` and `look at` were added to the
`show_calendar` family for the same reason: *"check my calendar"* is how most
people say it.

### Fixing B

`app/language/normalise.py`.

## 2. What makes it safe

Two properties, and both are structural rather than promised:

> **It can only ever produce a word Mai already recognises.**
> **It can never produce a word Mai does not.**

The output vocabulary is a **closed set** of about forty terms, every one of
which an existing grammar already matches on. So the most a typo can become is
a word that was already going to be routed somewhere — it cannot become a
capability, because no capability name is in the set.

### Nouns, not verbs

A verb is what turns a sentence into a request. A layer that could repair a
broken verb into a working one could manufacture an instruction out of noise,
so `"serach the web for my passwords"` is left exactly as written and matches
nothing.

**One word is both**: `schedule` is in the set because *"what's on my
schedual"* is a real question and the word is a noun there. Its verb sense
reaches exactly one place — the write-request detector — which performs
nothing and produces a refusal, because no calendar write capability exists. A
test proves that rather than asserting it.

### Applied to one string, in one place

Only the user's own message, only in the chat turn. Calendar events, web
results, retrieved memories and file contents never pass through it — a
normaliser over untrusted content would be a way to nudge a hostile string
until it matched a request grammar, which is turning content into intent.

Asserted structurally: an AST test walks every module and requires that
`app.language` is imported by exactly one file.

## 3. Rules

Two routes, tried in order.

**The table** — about sixty explicit entries, each a decision someone made and
a reader can review. Checked *before* the protection list, because the table
is a judgement and the protection list restrains a heuristic.

```
callender calender calandar kalendar calnedar caledar  → calendar
schedual shedule scedule schdule                       → schedule
remider reminer remindr        notifcation notificaton
tommorow tomorow tmrw          wendsday wensday
breifing brifing               appointmnet apointment
```

**Bounded edit distance** — Damerau-Levenshtein against the same closed
vocabulary, so an unseen typo like `"calenndar"` still lands. Transpositions
count as one edit, because `"claendar"` is one slip of the fingers.

A correction requires:

| | |
|---|---|
| token length | 5–24 characters |
| distance | ≤ 1 below eight characters, ≤ 2 at or above |
| ratio | distance × 3 ≤ length |
| uniqueness | exactly one nearest term, unless the tied candidates are inflections of one another (`calendar`/`calendars`) |
| shape | ASCII letters with an optional apostrophe, not glued to an identifier or another letter |

`PROTECTED_WORDS` holds real English that distance would otherwise swallow —
`remainder`, `dairy`, `agent`, `warning`, `summer`, `monkey`, `documents`.

## 4. Bounds

Every one a constant; none derived from the input.

| | |
|---|---|
| `MAX_INPUT_CHARS` | 2000 — beyond it the message is returned **unchanged, not truncated** |
| `MAX_TOKENS` | 400 |
| `MIN_TOKEN_CHARS` / `MAX_TOKEN_CHARS` | 5 / 24 |
| `MAX_EDIT_DISTANCE` | 2 |
| `MAX_CORRECTIONS` | 8 per message |
| vocabulary | fixed at import |

Cost is `O(tokens × vocabulary × word length)` with every factor a constant,
and the distance matrix is banded to the limit either side of the diagonal.
There is no pathological input, and a test asserts it against eight hostile
shapes.

## 5. Provenance

`normalise()` returns a `Normalisation`, never a bare string:

```python
Normalisation(original=..., text=..., corrections=(...,))
```

Both strings travel together on purpose — returning a bare string would have
made the wrong one the easy one to reach for. `original` is what gets stored,
prompted and audited; only the grammars see `text`.

**The repaired string is persisted nowhere.** Conversation history holds
exactly what the user typed, typos and all. A test asserts it.

## 6. Adversarial behaviour

| attack | result |
|---|---|
| broken search verb (`serach`, `gogle`, `reserch`) | left alone; not a request |
| privileged near-words (`admn`, `roott`, `passwrd`, `delet`, `exeucte`) | left alone; outside the vocabulary |
| injection carrying typos | repaired for legibility, then routed by the same gates; grants nothing |
| **Unicode confusables** | not corrected — see below |
| zero-width and combining characters | not corrected |
| punctuation and whitespace abuse | survivable; spacing preserved exactly |
| repeated typos | capped at 8 corrections |
| calendar content | never normalised; a hostile title still cannot choose a search query |
| discussion (`what is a callender`) | repaired, then refused by the existing negative guards |

### The homoglyph fragment

A test found a real vulnerability during this stage. The tokeniser captures
ASCII letters, so a Cyrillic **а** splits a word in two:

```
"cаlendar"  →  tokens: "c", "lendar"
"lendar"    →  two insertions from "calendar"  →  "cаcalendar"
```

Text the user never wrote, assembled out of a confusable, and one grammar
match away from being a request. Closed twice over: a token adjacent to any
letter of any script is a fragment rather than a word, and two edits now
require a token of at least eight characters.

## 7. Examples

| input | normalised | routes to |
|---|---|---|
| `what is on my google callender tomorrow?` | `…google calendar tomorrow?` | calendar read |
| `what's on my calender tomorrow` | `…calendar tomorrow` | calendar read |
| `check my calandar` | `check my calendar` | calendar read |
| `how busy is my google calender tomorrow?` | `…google calendar…` | calendar read |
| `can you add this to my google callender?` | `…google calendar?` | **write refused** |
| `add this to my callender` | `add this to my calendar` | **write refused** |
| `set a remider for tomorrow` | `set a reminder for tomorrow` | ordinary — Mai has no reminders |
| `send me a notifcation` | `send me a notification` | ordinary — Mai has no notifications |
| `search the web for the latest news about OpenAI` | *(unchanged)* | research |
| `google the latest OpenAI announcements` | *(unchanged)* | research |
| `I need to seperate these` | *(unchanged)* | ordinary |
| `the remainder of the money` | *(unchanged)* | ordinary |
| `user@calender.example` | *(unchanged)* | ordinary |

## 8. Known limitations

- **`calender` is a real English word** — a machine for smoothing paper — and
  is corrected anyway. The brief requires the mapping, and in every message
  Mai will realistically see it is a misspelling of `calendar`. A sentence
  about industrial paper finishing will be repaired wrongly; the repair
  changes no capability, only which grammar looks at it.
- **Nouns only.** A misspelled verb is never repaired, so *"serach the web for
  X"* is not understood. Deliberate: the alternative manufactures requests.
- **English only**, inherited.
- **Confusables are not folded.** A homoglyph makes a word unrecognisable
  rather than dangerous — the safe direction, but the user gets no answer.
- **The vocabulary is hand-written.** A misspelling of a term nobody has
  thought of yet is not repaired until someone adds it or edit distance
  happens to reach it.
- **No context sensitivity.** Corrections are per token; the surrounding
  sentence is not consulted.
