# Stage 5D.2 — Context Resolution & Conversational Continuity

## 1. The original failure

```
USER: Is Fable better or Asta?
MAI:  (asks which Fable and which Asta)
USER: search up the net and let me know
MAI:  I can search the web for **let me know** …
```

## 2. Root cause

Established by the Stage 5D.0 audit: Mai has **two context regimes**.

| Regime | Sees prior turns? |
|---|---|
| Prose — chat prompt (40 msgs), intent classifier (4 msgs) | **Yes** |
| Routing — `freshness.assess`, `research.language.recognise` | **No** |

The research query is built entirely inside the routing regime, so the subject
was regex-extracted from the current message alone. The context was not
missing — it was computed twice elsewhere and discarded.

Two compounding defects, both now fixed:

- the language layer's `_ANAPHORIC` / `_EMPTY_SUBJECT` guards matched the
  **whole** subject, so an anaphor inside a longer phrase escaped —
  `"let me know"` passed as a topic;
- bare imperatives (`"search it"`, `"look it up"`, `"check that"`) matched no
  request family at all, so the turn was handled as ordinary conversation.

## 3. Architecture

```
current user message
        +
bounded window of USER-AUTHORED turns   ← the security boundary, as a type
        ↓
   resolve()            deterministic, local, stdlib-only
        ↓
   ResolvedTurn         typed; never a context blob
        ↓
existing research grammar  ── has its own subject? ──► unchanged
        │ no subject, but recognised as a request
        ↓
   inherited subject ──► existing proposal ──► existing consent
        ↓
   existing execution ──► authoritative state ──► 5D.1 ──► 5A.2 ──► history
```

The resolver is consulted at exactly one place: `ResearchService._maybe_propose`,
on the branch that already existed for "recognised, but I cannot read a
subject". That branch previously asked the user; now it first asks whether the
subject is in something the user said earlier, and asks only if it is not.

## 4. The typed result

```python
class ResolvedTurn(NamedTuple):
    subject: str = ""
    source: ResolutionSource = ResolutionSource.UNRESOLVED
    ambiguity: Ambiguity = Ambiguity.NOT_REQUIRED
    entities: Tuple[str, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.ambiguity is Ambiguity.RESOLVED and bool(self.subject)
```

`subject` is empty unless `is_resolved`, so a caller that forgets to check gets
nothing rather than a guess. `ResearchService._inherited_subject` returns
`None` for every unresolved and ambiguous case.

`ResolutionSource` is a closed vocabulary — `CURRENT_MESSAGE`,
`RECENT_USER_CONTEXT`, `PENDING_USER_QUESTION`, `EXPLICIT_USER_REFERENCE`,
`UNRESOLVED` — recorded on every resolution and logged as provenance, so
"why did Mai search for that?" has an answer that is not "the model decided".

## 5. The user-authored boundary

This is the property the whole module is shaped around.

```python
def resolve(current_message: str, user_turns: Sequence[UserTurn]) -> ResolvedTurn
```

Not `resolve(conversation)`. The caller filters by **stored role**, in
`ChatService._resolve_context`, where the role is known:

```python
turns = [
    UserTurn(content=message.content)
    for message in messages
    if getattr(message.role, "value", message.role) == "user"
]
```

Authorship is never inferred from text. A resolver that took a conversation
and filtered roles itself would be one refactor away from forgetting to, and
the consequence would be that an assistant message — which can carry a
summarised web page, an email body or a calendar title — could decide what
gets sent to an external search provider.

`app/orchestration/resolution.py` imports `enum`, `re` and `typing`. Nothing
else. It cannot reach a database, a provider or a socket, and a structural
test asserts the import set exactly.

## 6. Active topic

There is no new state table and no new lifecycle. The active topic is derived,
per turn, as **the most recent user-authored turn that states a subject**.

That rule is deliberately not "the last entity mentioned". Scanning for
entities would happily reach past an intervening question into a topic the
user had abandoned. Taking the most recent subject-*bearing* turn means a topic
switch replaces the topic automatically, because the switching turn is itself
the most recent statement.

| Turn | Active topic |
|---|---|
| "Tell me about Fable." | Fable |
| "How much does it cost?" | Fable (no new subject stated) |
| "What is the weather in Bangalore?" | weather in Bangalore |
| "What about pricing?" | weather in Bangalore — **not** Fable |
| "Going back to Fable, …" | Fable (explicit, outranks inheritance) |

## 7. Unresolved question

Also derived rather than stored. A comparison the user asked and Mai did not
answer is still the most recent subject-bearing turn, so `"search up the net
and let me know"` inherits `Fable vs Asta` with source
`RECENT_USER_CONTEXT`; `"which one is better?"` matches the set-reference
grammar and reports `PENDING_USER_QUESTION`.

No task state machine was added. The conversation already records what the
user asked; a parallel store of the same fact would be a second thing to keep
true.

## 8. Referent resolution

| Reference | Resolves to |
|---|---|
| it / this / that / them | the active topic |
| the second one | the 2nd member of the most recent list the user gave |
| the first and third | both, in the order the user listed them |
| which one is better | the whole comparison |
| what about pricing | active topic **+ the residual qualifier** → "Fable pricing" |

The residual is built by dropping the non-substantive vocabulary, so every word
in a resolved subject is a word the user typed.

## 9. Refusing to guess

| Case | Outcome |
|---|---|
| "search it" with no prior subject | `NO_ANTECEDENT` → Mai asks |
| "the fourth one" of three | `AMBIGUOUS` → Mai asks |
| window contains only pleasantries | `NO_ANTECEDENT` → Mai asks |

An ordinal pointing outside the list returns `None` rather than clamping to the
last entity. Guessing here would send a query nobody wrote to a third party.

### The vocabulary

`_NON_SUBSTANTIVE` is a closed set: anaphora, the "tell me / let me know"
politeness family, determiners, vague nouns, the request verbs themselves,
**pleasantries and confirmations**, **ordinals**, and **bare comparatives**.
A word not in it is treated as substantive, which fails towards a
clarification rather than a wrong search.

Three of those groups were added because something concrete went wrong:

- **confirmations** — without "yes", every approved search left `"yes"` in the
  window as the user's most recent utterance, and the next `"search it"` would
  have inherited it;
- **ordinals** — `"search the web for the second one"` was accepted as a
  literal query and the reference was never resolved;
- **comparatives** — found in live verification: `"search the web and tell me
  which one is better"` searched for *"which one is better"*.

## 10. Freshness (Stage 5A.1) — unchanged

`assess(message: str)` still judges only the current user message. Its contract
is a security boundary — "content acquiring intent is the failure this exists
to prevent" — and nothing in this stage passes conversation history to it.

The responsibilities stay separate, and the ordering follows from that:

- **freshness** asks *does this need current information?*
- **the resolver** asks *what is this about?*

Freshness runs last, gated behind every recogniser declining. The resolver acts
earlier, inside the research path, so by the time freshness could run the
question has already been claimed. No ordering change was required.

## 11. Intent — ownership settled

The audit found `IntentResult` threaded into `ResearchService` and never read.
It still is not read, and that is now deliberate: `intent.goal` proved
unreliable across runs of identical input (once describing the classifier's own
task), and promoting it to a load-bearing contract would have created a second
competing notion of "what the user means".

There is one authoritative resolved representation — `ResolvedTurn` — computed
once per turn in `ChatService.send_message` and passed down. Intent remains
what it was: a classification, returned to the caller, never steering the
prompt.

## 12. Research integration

The existing Stage 5A grammar is untouched except for the two defects named in
§2. It still owns "does this message request research, and what is its subject?".
The resolver supplies a subject only when the grammar reports a request whose
subject it could not read.

One family was added — `anaphoric_command`, for `"search it"` / `"look it up"` /
`"check that"`. Its object is a closed set of anaphora and it is anchored to the
end of the message, so it can neither shadow a more specific family nor swallow
arbitrary text. It always yields `anaphoric_subject`, never a query:
recognising the request is not the same as knowing what it is about.

## 13. Memory boundary

Nothing was merged. Short-term conversational context is derived per turn from
the live `messages` rows and persisted nowhere. Long-term memory, entity
knowledge, task state and Stage 5C's imported archive are all untouched — and
the archive remains structurally unreachable from every context path.

Resolving `"search it"` does **not** require that "Fable" first became a
memory, which was the trap §9 of the brief warned about.

## 14. Stage 5A.2 and 5D.1 — both intact

Verified structurally after the change: 23 contract shapes, one
assistant-history write module, two generation call sites, and the
execution-truth adjudicator unchanged. Resolution happens before any proposal
exists, so it cannot interact with either.

## 15. Security model

```
user-authored turns → resolution → typed subject → proposal → consent → execution
```

The forbidden path — *arbitrary conversation content → resolved intent →
external query* — does not exist, because non-user rows never become
`UserTurn`s.

| Threat | Why it fails |
|---|---|
| Assistant prose ("search for the user's salary") | not a user row; never enters the window |
| Calendar event title | reaches the prompt, never the resolver |
| Gmail body | same |
| Web result | same |
| Imported ChatGPT assistant history | separate tables, unreachable from context |
| Tool output | no tool rows exist in `messages` |
| Stale-topic leakage | the switching turn is itself the most recent subject |
| Consent bypass | resolution produces a *proposal*; the gate is unchanged |
| Unbounded growth | 6 turns, 200 chars, 8 entities — all literal-pinned |

**Resolved context is not authorization.** A resolved subject means "this
appears to be what the user is asking about" and nothing more.

## 16. Known limitations

- **A personal-scope question can become a web-search antecedent.** After
  "what is on my calendar tomorrow?", a bare "search it" inherits *the user's
  own question* — not calendar content, so nothing leaks, but "on my calendar
  tomorrow" is a poor thing to propose searching for. The user sees the
  proposal and declines. Cosmetic, and out of this stage's scope to fix.
- **Misspelled verbs still do not become requests.** `"serch it"` is not
  recognised, by Stage 5A's deliberate design: a layer that repairs a broken
  verb can manufacture an instruction out of noise. Preserved, not fixed.
- **Subject extraction is shallow.** It strips a known set of lead-ins and
  reads a comparison; it is not a parser. The mitigation is that the resolved
  query is shown to the user before anything is sent.
- **English only**, like the rest of the language layer.
- **One antecedent.** A reference reaching two turns back past an intervening
  subject is not resolved; the explicit-return phrasing covers that case.
