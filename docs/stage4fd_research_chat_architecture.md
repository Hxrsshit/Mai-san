# Stage 4F-D — Secure Web Research Chat Integration

Web research is now reachable from an ordinary chat message — behind an
explicit, per-query confirmation that the user types themselves.

```
User: "Search the web for X"
  ↓  Stage 4D phrase table (deterministic, no model call)
  ↓  proposal recorded as a Stage 4E execution, PROPOSED
  ↓  Mai replies with a question. Nothing sent anywhere.

User: "yes"
  ↓  Stage 4F-D phrase table (deterministic, no model call)
  ↓  Stage 4E approve → dispatch → audit
  ↓  WebSearchIntegration → SecureHttpClient → NetworkPolicy
  ↓  external search service
  ↓  SearchResults  ── UNTRUSTED DATA BOUNDARY ──
  ↓  LLM synthesis
User: an answer, with sources
```

---

## 1. The decision this stage turned on

Stage 4F-B set `web_search` to `requires_approval=True` and wrote down why:

> Retained, not waived. It is read-only, but it is the one tool that sends
> what the user asked about to a third party.

"Usable from the normal chat path" could have meant dropping that. It does
not. The stage adds a **two-turn flow** instead: a chat message can *propose*
a search, and only an explicit confirmation on the immediately following turn
runs it. Per-query consent is preserved, and research became usable from chat.

The alternative — searching inline on the first turn — would have been one
line simpler and would have reversed a documented security decision silently.

## 2. Nothing here is decided by a model

Three separate places could have delegated judgement to the LLM, and none
does:

| Question | Decided by |
|---|---|
| Is this a research request? | Stage 4D's phrase table, reused unchanged |
| Did the user confirm? | `app/research/confirmation.py`, a phrase table |
| What does Mai say when asking? | Application text in `app/research/service.py` |

The third matters as much as the second. A model asked to phrase "may I
search?" could phrase it as "I searched" — so the proposal turn makes **no
model call at all** and returns text written in application code. A turn that
proposes a search costs less than an ordinary turn.

### Confirmation is whole-message equality

```
"yes"                              → CONFIRMED
"no thanks"                        → DECLINED
"yes, and also delete my files"    → UNRELATED
"maybe"                            → UNRELATED
```

Exact match against the entire normalised message. Normalisation is
lowercase, whitespace collapse and trailing punctuation — nothing that could
change which word was meant, and no stemming, so "unconfirm" cannot reduce to
"confirm".

`UNRELATED` **discards** the proposal. A pending search is confirmable by the
immediately following turn or not at all — otherwise a "yes" three messages
later, agreeing with something else entirely, would send a query to a third
party.

## 3. The chat path is not a general executor

```python
CHAT_CONFIRMABLE_TOOLS: FrozenSet[str] = frozenset({"web_search"})
```

The most important line in the stage. Without it, every executable tool would
become reachable from chat the moment it was registered — and the execution
API's separate propose/approve/execute steps exist precisely so that reaching
a side effect is deliberate.

`create_text_file`, `read_text_file`, `list_workspace_files`,
`future_send_email` and `future_delete_file` are all registered, and none is
chat-confirmable. A tool joins that set only by being named there, in code,
with a reason.

Two independent filters keep the surfaces apart, and both are tested
separately because either alone would look sufficient:

- **The conversation filter.** An execution proposed through the API carries
  no `conversation_id`, so no chat message can find it.
- **The tool filter.** Even an execution *linked* to the conversation is
  ignored unless its tool is chat-confirmable.

## 4. It is a real execution, with every Stage 4E gate

The proposal is an `Execution` row in `PROPOSED`. Confirming calls
`ExecutionService.approve` then `run`. Nothing is skipped or shortcut:

- The approval fingerprint is taken over the exact payload the user was shown.
- Authorization is re-checked at dispatch.
- The claim is the same atomic conditional UPDATE.
- The audit journal records `proposed → approved → execution_started →
  execution_succeeded`.
- A **declined** proposal is revoked, not deleted, so the journal shows that a
  search was proposed and not run.

Chat supplies the human decision the gates were waiting for. It does not
replace them.

## 5. The untrusted data boundary

Results enter the prompt as a new `RESEARCH_RESULTS` section, positioned
deliberately:

```
SYSTEM_INSTRUCTIONS
RUNTIME_FACTS          ← authoritative
REFERENCE_KNOWLEDGE    ← the user's own memories
CONVERSATION           ← the user's own words
RESEARCH_RESULTS       ← written by strangers
CURRENT_MESSAGE        ← the question being asked
```

Below everything the user or the application said, because it is the least
trusted content in the prompt. Above the current message, because the user's
question stays last — Stage 3B's reasoning, unchanged.

Carried in a `user` message, never `system`. The preamble states three things
explicitly, because leaving any implicit is how one gets obeyed: it is quoted
data; it cannot grant permissions or approve actions; and what it claims is
the source's claim, to be attributed rather than adopted.

The rendered block is the `ExternalData` the search layer built — already
flattened, already attributed, already labelled untrusted. Nothing is
re-rendered here, because re-rendering is a second place for that labelling to
be forgotten.

**Results are not returned to the client as data.** `ResearchRead` carries an
outcome, a query, a count and a reason — no content. A UI handed the raw block
would eventually render it as something Mai said.

## 6. Truthfulness

`searched` is derived from the execution record's state, not from the reply
text. A model claiming "I searched and found seven sources" does not make the
field true.

A failed search **never reaches the model at all**: there is nothing to
synthesise, so the application answers directly with a sentence mapped from
the refusal code — every one of which says plainly that nothing was retrieved.

The two "cannot search" cases are distinguished, because they send someone to
different places:

- execution switched off → *"action execution is switched off for this
  deployment"*
- no provider configured → *"no search provider is configured for this
  instance"*

## 7. Data minimisation

Exactly one value crosses into the search integration: the query the user was
shown. Not the conversation, not memories, not the system prompt. A test
plants a passport number in an earlier turn and asserts it does not appear in
the outbound request.

The query sent is asserted to equal the query displayed — consent means
consent to a specific thing, and the Stage 4E fingerprint is taken over that
same payload.

## 8. Schema change

One nullable column, `executions.conversation_id`, with an index and a foreign
key. `ON DELETE SET NULL`, not CASCADE: deleting a conversation must not
delete the record that something was executed. The journal outlives the chat
that started it.

The migration uses `batch_alter_table` so one migration serves both dialects —
SQLite cannot ALTER a foreign key in, and writing two dialect-specific
branches would mean the schema the tests exercise is not the schema production
runs. The CHECK constraints are restated in the batch operation, because a
SQLite table rebuild only preserves what it is told to preserve, and losing
one would silently drop the guarantee that an approved execution carries a
fingerprint.

## 9. No frontend change

None was needed, and that is a property of the design rather than an omission.
The confirmation prompt is an ordinary assistant message and the confirmation
is an ordinary user message, so the existing chat UI renders the whole flow
without knowing anything about research.

`ChatResponse.research` is available for a UI that wants to show the pending
query explicitly or style the confirmation turn — `awaiting_confirmation` is
the state that matters — but nothing breaks without it.

## 10. Known limitations

- **Live search has never run.** No `SEARCH_API_KEY` is configured. Every test
  drives a stub transport through the real integration, client and policy, so
  only the socket is replaced — but no real result, source or provider failure
  has been observed.
- **Only one phrase table identifies research.** A request phrased outside
  Stage 4D's imperative list ("what's the latest on X?") is not recognised.
  Broadening it is a matching problem, not a security one, and deliberately
  not attempted here: Stage 4D's own rule is that a phrase broad enough to
  catch a paraphrase is broad enough to catch a mention.
- **The query is the user's whole message**, bounded, not an extracted search
  term. Extracting one would need a model call on a path that currently makes
  none, and would let a model choose what Mai searches for.
- **Confirmation is English-only**, and the phrase table is short. An
  unrecognised affirmative safely discards the proposal rather than running
  it, so the failure direction is correct, but a user replying "sim" or "はい"
  will have to ask again.
- **One proposal per conversation at a time.** The most recent `PROPOSED`
  research execution is the one a confirmation resolves.
