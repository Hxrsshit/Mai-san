# Stage 5A.2 — Synthesis Reliability & Response Contract

```
internal execution state
  → synthesis (the model)
  → response contract        app/synthesis/contract.py   ← NEW
  → validated assistant response
  → conversation history
  → frontend
```

**Only a validated response crosses into conversation history.** The model
generates a candidate; the application decides whether it is an answer.

---

## 1. The observed problem

Stage 5A.1 recorded this as a residual risk. Reproduced in the browser:

```json
{
  "tool": "Web search",
  "action": "search",
  "parameters": { "query": "latest Nvidia GPU" }
}
```

That is what the user saw where an answer should have been. It is the model
trying to call a tool at a point where the application wanted prose.

Two things followed, and the second is worse than the first:

1. The user saw JSON.
2. The blob was stored as the assistant's message, so on the **next** turn the
   model read its own output as an example of how Mai replies — and produced
   another one. A single malformed response became a pattern.

A fresh conversation produced correct sourced prose, which is what identified
the mechanism: contamination through history, not a broken provider.

### Root cause, precisely

`reply = llm_response.content` went straight into `add_message`. Mai holds
several internal representations — tool calls, execution records,
authorization envelopes, provider responses — and **nothing distinguished any
of them from an answer.** The write site had no notion of what kind of thing
it was storing.

## 2. The contract

`app/synthesis/contract.py` classifies a model response into one of four
kinds, and exactly one may be stored:

| kind | |
|---|---|
| `PROSE` | a reply. **The only acceptable kind.** |
| `TOOL_CALL` | the model tried to act where prose was required |
| `INTERNAL_STRUCTURE` | an execution, approval or authorization envelope |
| `EMPTY` | nothing usable was generated |

`ACCEPTED_KIND` is a single member rather than "not in REFUSED", so a kind
added later is refused by default.

```python
AssistantResponse(text, kind, accepted, reason)
```

A refused response **carries no text**. The offending content goes nowhere —
not into the return value, not into a log line. It is model output from a turn
that may have carried private calendar or mail data into the prompt.

## 3. Not a JSON stripper

The naive fix — delete anything that looks like JSON — would destroy every
legitimate answer containing JSON, and users ask for JSON constantly.

What matters is not whether the text contains braces but **what kind of thing
the response is**. So a response is refused only when it is *entirely* one of
the refused objects, with no prose around it:

| | |
|---|---|
| `{"tool": "web_search", "query": "x"}` | refused — the whole response is a call |
| `A tool call looks like {"tool": "web_search"} in most APIs.` | **accepted** — prose |
| `Here is the JSON you asked for:\n```json\n{...}\n```` | **accepted** — prose |
| `{"name": "Ada Lovelace", "born": 1815}` | **accepted** — JSON the user asked for |
| `[{"city": "Bangalore"}]` | **accepted** |
| `{ this is not json` | **accepted** — unparseable is prose |

The classifier parses the response as JSON only when the *whole* trimmed
response (optionally inside a single code fence) is an object or array, then
matches its keys against declared shapes.

## 4. Provider independence

The shapes are key sets, not per-provider branches, so a provider's envelope
needs no code of its own:

```
OpenAI       {"name","arguments"}  {"tool_calls"}  {"function_call"}
Anthropic    {"type": "tool_use"}  {"name","input"}
ReAct/LC     {"action","action_input"}  {"tool_input"}
improvised   {"tool"}  {"tool_name"}  {"action","parameters"}
```

### 4.1 The vocabulary is closed, and it grew once — on evidence

The ReAct row is there because live verification caught it escaping.

With the contract already running, a real turn produced

```json
{"action": "web_search", "action_input": {"query": "latest news about OpenAI"}}
```

No key set matched `{action, action_input}`, so it was classified as JSON the
user had asked for, stored, and shown. 4084 passing tests did not find it; one
browser session did, because the unit suite only ever posed shapes I had
already thought of.

Two things follow, and both are now enforced:

1. **A regression corpus.** `OBSERVED_IN_PRODUCTION` in the security suite
   holds every blob this system is known to have actually emitted — the
   Stage 5A.1 blob and this one — verbatim. Reality supplies the corpus.
2. **A literal pin on the vocabulary size.** `known_tool_call_shapes()` is
   pinned at 23 entries, with a companion check that no key set is a superset
   of another (an unreachable entry is false comfort). Widening the set means
   editing that test and writing down why — the same discipline the routing
   enums use, because each addition can turn a legitimate answer into a lost
   one.

`LLMResponse` remains `content / model / finish_reason / usage` — **`raw` was
not reintroduced**, and a test asserts it. Provider-specific normalisation
stays at the provider boundary; the synthesis layer receives a normalised
string.

## 5. Bounded recovery

One attempt. Exactly one.

```
refused → append one corrective instruction → generate once → validate again
        → still refused? a truthful application-written line
```

A model that has just ignored the contract is not obviously going to honour it
on the third ask, and an unbounded loop is a denial of service the model
triggers against itself.

The retry **reuses the original prompt parts exactly**. Nothing is re-retrieved,
re-researched or re-read; no tool is reached and no approval is consulted. The
recovery costs one generation and can acquire nothing.

The corrective instruction lives in `app/prompt/formatter.py`, not in the
contract module — Stage 3B established that chat prompt text has one owner, and
a test enforces it. The instruction states the required shape and deliberately
says nothing about tools: a correction that discussed tools would be inviting
back the mistake it exists to fix.

## 6. Truthfulness

The fallback is built from **execution state**, never from the model's account
of it. The brief's distinction is real and easy to collapse:

| what happened | what Mai says |
|---|---|
| nothing ran, synthesis failed | *"I couldn't produce an answer just then… Nothing was searched or read."* |
| research succeeded, synthesis failed | *"I searched the web and got the information back, but I couldn't turn it into an answer."* |
| calendar read, synthesis failed | *"I read your calendar and got the information back, but…"* |

Reporting a working search as an empty internet would be the same class of
untruth Stage 4E.1 exists to prevent.

## 7. It grants nothing

Recognising a tool call is not running one. The contract module imports no
`app.execution`, no `app.tools`, no `app.integrations`, no HTTP client and no
database session — asserted by AST. A model emitting

```json
{"tool": "create_text_file", "arguments": {"path": "secrets.txt"}}
```

has written a string. What it gets is a rejection; the execution table stays
empty, and a test drives that end to end.

## 8. The history boundary

Two write sites, both in `chat_service.py`, and a test pins the count:

| | |
|---|---|
| `_answer_without_the_model` | application-written text — true by construction |
| the synthesis path | now goes through the contract |

Because a refused response is never stored, **the next turn's prompt contains
no example to imitate.** The poisoning is fixed by absence rather than by
filtering.

## 9. Memory isolation

Malformed output never becomes an assistant message, so it never reaches the
extraction pipeline, which reads stored messages. A test primes extraction to
store something and asserts no memory contains tool-call structure.

## 10. Known limitations

- **A tool call wrapped in one sentence of prose is accepted as prose.** That
  is the deliberate trade: refusing it would mean refusing every answer that
  explains a tool call. The user sees a slightly odd answer, not a poisoned
  history — the blob is still surrounded by text, so the imitation pressure on
  the next turn is much weaker.
- **Recovery is one attempt.** A model failing twice produces a truthful
  failure rather than an answer.
- **The classifier does not read the turn's intent.** It cannot know the user
  asked for a tool-call example, so such a request answered with *only* the
  object would be refused. Asking for one with any surrounding sentence works.
- **Key sets, not schemas.** A provider inventing a wholly new envelope shape
  passes as a structured answer until its keys are added. This is not
  hypothetical: it happened during this stage's own live verification (§4.1).
  The mitigation is not a cleverer classifier — it is that the failure is
  *observable*, cheap to fix, and now carries a regression corpus. Treat live
  verification as the discovery mechanism for this class of bug, because the
  unit suite structurally cannot be.
- **Fail-open on unknown structure.** An unrecognised JSON object is accepted.
  The alternative — refusing all JSON — breaks every legitimate request for
  structured output. The asymmetry is deliberate: a missed blob is an odd
  answer, a false refusal is a lost one.
