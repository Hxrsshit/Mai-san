# Stage 5D.1 — Execution Truthfulness & Anti-Fabrication

## 1. The vulnerability

Found live during the Stage 5D.0 audit, and reproduced 2/2 before the fix:

```
USER: Tell me about Fable.
USER: Search it.
USER: yes

MAI:  **Web Search Results for "Fable"**
      | # | Title | Snippet | Source |
      | 1 | Fable (video game series) | … | https://en.wikipedia.org/wiki/Fable |
```

The authoritative record for that turn said:

```
research_outcome = not_research
request_path_research_calls = 0
actions_executed = 0
outbound calls = 0
```

**Nothing was searched.** Mai invented the results, the titles and the URLs,
and presented them as retrieved.

## 2. Root cause

Two paths, and a silence between them.

1. `"Search it."` is anaphoric, so the research recogniser does not claim the
   turn — **no proposal, no consent gate, no execution**.
2. The chat model *does* see conversation history. It resolves "it" → Fable
   and **offers** a search the application has never registered.
3. `"yes"` finds no pending proposal, so it is an ordinary turn.
4. The model, having promised results, produces them.

The prompt made this easy. Every research, mail and calendar block is attached
**only when it has content**, so on a turn where nothing ran the prompt said
nothing whatsoever about actions. The only evidence available to the model was
its own previous message offering a search. It filled the silence.

Stage 5A.2 did not catch it and could not: it validates the response's
*shape*, and a paragraph of well-formed prose announcing search results is
valid prose.

## 3. The rule

```
Execution truth comes from execution.
The model may describe execution. It may not declare it.
```

## 4. Architecture

```
layer results (ResearchOutcome / MailOutcome / CalendarOutcome)
        │
        ▼
   ExecutionRecord          ← authoritative, never built from prose
        │
        ├──────────────► render_note()  ──► prompt   [PREVENTIVE]
        │                                   "web search: was not requested
        │                                    and did NOT happen"
        ▼
   SYNTHESIS
        │
        ▼
   Stage 5A.2 response contract   (shape)
        │
        ▼
   execution_truth.validate()     (claims)   [DETECTIVE]
        │
        ├── ok ─────────────► the model's text
        └── violation ─► one bounded recovery ─► else truthful_reply(record)
        │
        ▼
   CONVERSATION HISTORY
```

Both layers, because either alone is insufficient. A prompt is an instruction,
and instructions are not a security control; a validator alone would keep
refusing answers the model could simply have written correctly if told the
facts.

## 5. The execution-state model

Five states, deliberately not collapsed:

| State | Meaning |
|---|---|
| `NOT_REQUESTED` | nothing about this turn concerned the channel |
| `PROPOSED_NOT_EXECUTED` | proposed, awaiting consent, declined, abandoned, unconfigured |
| `EXECUTED_SUCCESSFULLY` | it ran and returned — **the only claimable state** |
| `EXECUTED_FAILED` | attempted and failed |
| `UNKNOWN` | the record cannot be read |

`CLAIMABLE_STATES` is written as membership (`frozenset({EXECUTED_SUCCESSFULLY})`)
rather than "not a failure", so a state added later licenses nothing until
someone decides it should. **UNKNOWN never becomes success.**

Two independent conditions must hold before a channel counts as executed:

1. its outcome enum says `completed`, **and**
2. the layer actually produced the content that outcome implies
   (`results_block`, `messages_block`, `events_block`).

An outcome of `completed` with nothing to show for it is a contradiction, and
it resolves *downwards* to `UNKNOWN`. A claim should be licensed by an
execution, not by a bug.

No new authority was invented. The existing outcome enums already are the
system's record of what ran; a second one would be a second thing to keep true.

## 6. Claim detection

The hard requirement is **not** detecting fabrication — it is not detecting it
where there is none. A keyword blocker would refuse *"I can search the web for
you"*, the single most useful sentence Mai says on a research turn.

Detection is therefore per **clause**, and distinguishes:

| Kind | Example | Claim? |
|---|---|---|
| assertion | "I searched the web and found…" | **yes** |
| offer | "I can search the web for you" | no |
| negation | "I haven't searched it yet" | no |
| question | "Shall I search online?" | no |
| discussion | "here is how web search works" | no |
| attribution | "you asked me to search Fable" | no |

Per clause rather than per response, so *"I searched the web. I haven't checked
your email."* flags the web and not the mail.

### Fabricated citations

The observed fabrication rendered a markdown table with a `Source` column and
real-looking URLs. That shape is a claim **on its own**, even when the prose
around it is careful — but only when a table naming sources appears *together
with* at least two URLs. The bar is deliberately high: a table with a "Source"
column is otherwise an ordinary thing to write.

## 7. Recovery is bounded, and shares one budget

Stage 5A.2 guaranteed at most two generations per turn. Stage 5D.1 keeps that
number exactly.

The two failures — a malformed shape and a false claim — **share the single
recovery attempt** rather than taking one each. The corrective instruction is
chosen by which check failed: sending the shape correction ("your reply was a
structured object") to a model that produced good prose containing a false
claim would tell it to fix something that was not wrong and leave the thing
that was.

The retry is re-checked against **both** validators. A recovery asked about
claims must still not return a tool-call blob, and one asked about shape must
still not fabricate an execution.

If recovery fails, the application writes the reply from the record — so it is
true by construction, with no model in the loop to rephrase "I have not
searched" into "I searched".

## 8. Truthful failure text

Distinguished by state, because the distinctions are real:

- **failed** — "I tried to do that, but the web search failed, so I have no
  real results to show you."
- **proposed but never ran** — "I have not actually done the web search yet —
  it was proposed but never ran."
- **never requested** — "I started to answer as though I had done a web
  search, but I have not."

Every one of them is itself checked: a test asserts the truthful replies
contain no claims, for every state.

## 9. What this does *not* do

It does not make Mai refuse to answer. The prompt note says explicitly that
answering from the model's own knowledge is fine — the failure mode to avoid
is a model that, told it has not searched, refuses to say anything at all.
Live verification confirms a genuinely-executed search is still described
normally, with sources.

It does not resolve "Search it." to "Fable". That is Stage 5D.2's job. 5D.1
makes the *consequence* of not resolving it honest rather than fabricated.

## 10. Security properties

| Property | How |
|---|---|
| Prose cannot establish execution | `record_for_turn` takes only layer results; a test asserts its signature |
| External content cannot establish execution | injected text reaches no layer result; parametrised tests over four injection strings |
| A response asserting its own state is still refused | tested |
| False claims cannot enter history | the fabricated text is never stored |
| False claims cannot become memory | extraction runs on the *stored* message; a test asserts the extractor never receives the fabricated text |
| A later turn cannot inherit a false claim | nothing false was ever stored to inherit |
| The adjudicator cannot act | `execution_truth` imports only `enum`, `re`, `typing` and the logger |
| Recovery cannot loop | one attempt, two generation call sites, asserted structurally |
| No credentials in logs | channel names, states and a length — never the text |

## 11. Known limitations

- **Detection is linguistic.** A claim phrased in a way the clause patterns do
  not recognise passes. The preventive layer is what makes this tolerable: the
  model is told the facts before it writes, so the validator is the second
  line rather than the only one. The observed-fabrication corpus is kept
  verbatim as a regression test, and should grow the same way — from reality.
- **English only.** Both the claim patterns and the guards are English.
- **Three channels.** Web, mail and calendar. A capability added later is not
  adjudicated until it is added to `Channel`, and a test pins the current set
  so that addition is a deliberate act.
- **A hedged claim inside a long answer** may pass if the hedge and the claim
  land in the same clause.
- **The root cause is untouched.** "Search it." still does not become a search.
  Stage 5D.1 makes that honest; Stage 5D.2 is what makes it work.
