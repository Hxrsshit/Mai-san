# Stage 5D.0 — Context Continuity & Recall: Architectural Audit

**Audit only.** No production code, schema, or behaviour was changed. HEAD is
`9c4ec8a` and the working tree is clean.

---

## 1. Executive summary

Mai has **two context regimes**, and only one of them has continuity.

| Regime | Sees prior turns? | Evidence |
|---|---|---|
| **Prose** — chat prompt (≤40 msgs), intent classifier (last 4) | **Yes** | Resolves "the second one" → Asta, "its" → Fable, "which one is better?" → Fable vs Asta |
| **Routing/extraction** — freshness, research recogniser | **No** | `assess(message: str)`, `recognise(message: str)` — pure functions of the current message |

The research query is built **entirely inside the routing regime**. So when the
user writes *"search up the net and let me know"*, the subject is extracted by
regex from that string alone and comes out as **`let me know`**.

The context is not missing. It is computed, and then discarded.

This is not merely a UX defect. The same gap produces a **reproducible
fabrication path**: because `"Search it."` is not recognised as a research
request, no consent proposal is registered — but the chat model, which *does*
see history, offers a search anyway; the user says "yes"; no proposal exists;
and the model invents search results with fabricated source attributions.
Reproduced **2/2**, with `research_outcome=not_research`,
`request_path_research_calls=0`, and zero outbound calls in the logs.

**Audit verdict: PASS.** The loss point, mechanism, and fix boundary are all
identified below.

---

## 2. Exact reproduction of the Fable/Asta failure

### 2.1 Unit level — the extractor in isolation

```python
>>> from app.research.language import recognise
>>> recognise("search up the net and let me know")
Recognition(family='scoped_search', query='let me know', needs_clarification='')
```

Adjacent probes from the same run:

| Input | family | query |
|---|---|---|
| `search up the net and let me know` | `scoped_search` | **`let me know`** |
| `can you search online and let me know` | `scoped_search` | **`let me know`** |
| `google it and tell me` | `research_verb` | **`it and tell me`** |
| `search the web and tell me more` | `scoped_search` | `''` → `empty_subject` |
| `search the web and tell me` | *(none)* | — not recognised at all |
| `search it` | *(none)* | — not recognised at all |

### 2.2 Live level — the running system

```
Turn 1  USER: "Is Fable better or Asta?"
        intent.goal       = 'determine which is better, Fable or Asta'   ← context captured
        research.outcome  = None

Turn 2  USER: "Search up the net and let me know."
        intent.goal       = 'Obtain information by searching the internet'
        research.outcome  = awaiting_confirmation
        research.query    = 'let me know'                                ← context gone
        reply: 'I can search the web for **let me know** …'
```

Turn 1 *does* resolve the topic. Turn 2 does not carry it.

---

## 3. End-to-end request flow

```
POST /api/conversations/{id}/messages
  └─ ChatService.send_message(content)
       1. reading    = normalise(content)                    [current msg only]
       2. intent     = IntentService.understand(content, conversation_id)
                         └─ _safe_recent() → last INTENT_CONTEXT_MESSAGES (4)   ◀ HAS CONTEXT
       3. planning   = PlanningService.plan_for(content, intent)
       4. orchestr.  = OrchestrationService.orchestrate(content, intent)
       5. calendar   = CalendarService.handle(..., normalised=reading.text)     ◀ current msg
       6. mail       = MailService.handle(..., normalised=reading.text)         ◀ current msg
       7. workflow   = WorkflowService.handle(..., intent, reading.text)        ◀ current msg
       8. research   = ResearchService.handle(id, content, intent, reading.text)◀ current msg
       9. freshness  = assess_freshness(reading.text)                           ◀ current msg
      10. context    = ContextService.assemble(...)  → ≤ MAX_CONTEXT_MESSAGES (40) ◀ HAS CONTEXT
      11. prompt     = PromptFormatter...
      12. llm_response = provider.generate_response(...)
      13. validated  = validate_response(...)            [Stage 5A.2 contract]
      14. add_message(user) ; add_message(assistant)
```

Steps **5–9 decide what Mai will do**. Every one of them reads only the current
message. Steps **2 and 10 hold the conversation**, and neither feeds 5–9.

---

## 4. Current context architecture

Four distinct stores exist, and they are cleanly separated today:

| Store | Scope | Bound | Reaches the prompt? |
|---|---|---|---|
| Recent conversation | this conversation | `MAX_CONTEXT_MESSAGES = 40`, message-count based | Yes, as ordered turns |
| Long-term memory (Stage 2A) | cross-conversation | `RETRIEVAL_MAX_MEMORIES = 10` | Yes, inside the reference block |
| Entities / relationships | cross-conversation | 10 each | Yes, reference block |
| Imported archive (Stage 5C) | historical | — | **No** — structurally unreachable |

---

## 5. Recent-turn retrieval behaviour

- **Loaded:** `ConversationService.get_messages(id, limit)` — newest `limit`,
  returned oldest-first. Ordering is correct; this is *not* a bug.
- **Bound:** message-count based (40 for the prompt, 4 for intent). **Not**
  token-based; a conversation of 40 long turns and one of 40 short turns cost
  very differently.
- **Roles:** user and assistant only. Messages are the only conversational
  rows; there is no tool/system message type in `messages`.
- **Truncation:** in `ContextService.assemble` against `MAX_CONTEXT_MESSAGES`,
  plus a character budget (`RETRIEVAL_MAX_CONTEXT_CHARS = 8000`) on the
  *reference block*, not on recent turns.
- **Malformed content:** cannot enter. Stage 5A.2's response contract means
  only validated prose is ever persisted as an assistant message.

---

## 6. Active topic — **does not exist**

There is no representation anywhere of:

- current topic
- current entities under discussion
- current comparison
- current unresolved question
- current user goal (beyond one transient `IntentResult`)
- pending clarification

Searched for and **not found**. The nearest things are:

1. `IntentResult.goal` — a free-text string, recomputed per turn, **never
   persisted**, and (see §7) never consumed by the layer that needs it.
2. `Execution(state=proposed)` — a pending *research proposal*, which is real,
   persisted state, but represents a pending **action**, not a topic. It is
   keyed to a query string, not to a subject the conversation is about.

---

## 7. Intent continuity — **the resolved goal is computed and thrown away**

`ResearchService.handle()` takes `intent: IntentResult`. Every occurrence of
the word `intent` in `app/research/service.py`:

```
 46:  from app.intent.schemas import IntentResult      ← import
 93:      intent: IntentResult,                        ← handle() signature
115:      conversation_id, message, intent, normalised ← pass-through
133:      intent: IntentResult,                        ← _maybe_propose() signature
```

**The parameter is threaded two levels deep and never read.** The subject is
re-derived from the raw message by regex instead.

That would be the obvious fix — and the audit must record that it is *not
sufficient on its own*, because `intent.goal` proved unreliable across runs of
the identical input:

| Run | Turn-2 `intent.goal` |
|---|---|
| 1 | `None` (intent_type `unknown`) |
| 2 | `'Obtain information by searching the internet'` — resolved, but **subject-less** |

And on Turn 1 of one run the classifier emitted
`'Classify the user message "Is Fable better or Asta?"'` — describing **its own
task** rather than the user's goal. A separate defect (§16.4), and a warning
against treating `intent.goal` as a load-bearing contract.

**Conclusion:** no `previous_intent` / `active_intent` / `pending_intent` /
`conversation_state` / `referent_resolution` concept exists. This is the
central architectural gap.

---

## 8. Referential language behaviour

Measured live. The split is consistent and diagnostic:

| Test | Input (turn 2+) | Prose path | Routing path |
|---|---|---|---|
| C | `Search it.` | ✅ resolved → offered to search "Fable" | ❌ not recognised; no proposal |
| D | `Which one is better?` | ✅ `goal` = "which of the two items (Fable and Asta) is better" | n/a |
| E | `What about the second one?` | ✅ `goal` = "the second model mentioned (Asta)"; answered about Asta | n/a |
| F | `What about its pricing?` | ✅ `goal` = "pricing … (likely Fable)" | ❌ pending weather proposal `abandoned` |
| A | `Search up the net and let me know.` | — | ❌ query = `let me know` |
| B | `Search the web and tell me more.` | — | ⚠️ `needs_clarification` (safe) |

Referential resolution is therefore owned **entirely by model prompting**
(40-turn window) and **partially by the intent classifier** (4-turn window).
There is **no deterministic referent resolution** anywhere.

Note Test F: intent resolved *"its" → Fable* correctly, the pending weather
proposal was abandoned, and the prose answer still waffled between
"weather-service pricing" and Fable — the correct resolution existed and
influenced nothing.

---

## 9. Freshness interaction (Stage 5A.1) — *reported, not modified*

`assess(message: str)` judges **only the current raw user message**, by
deliberate design. Its docstring is explicit:

> "Must not be called on anything a person did not type. Every other string in
> the system is content, and content acquiring intent is the failure this
> boundary exists to prevent."

**That constraint is correct and must survive Stage 5D.** It is what stops an
email body, a calendar title, a search result, or a retrieved memory from
acquiring the authority to route a turn.

Does 5A.1 *cause* the failure? **No.** In the Fable/Asta trace the research
recogniser claimed the turn first, so freshness never ran (it is gated behind
`research.outcome is NOT_RESEARCH`). 5A.1 shares the single-message design but
is not the proximate cause.

It does, however, define the boundary Stage 5D must respect: any resolved
subject drawn from prior turns must be derived from **user-authored** messages
and carried in a **typed field**, never by widening these recognisers' input to
arbitrary conversational text.

---

## 10. Research-query construction — where the subject dies

```
ChatService.send_message
  └─ ResearchService.handle(conversation_id, content, intent, normalised=reading.text)
       └─ _maybe_propose(conversation_id, message, intent, normalised)
            reading = normalised or message                ← current message ONLY
            candidate = self._research_candidate(reading)
                 └─ app/research/language.recognise(message: str)
                      └─ _FAMILIES regex → group("subject") = "let me know"
                      └─ _ANAPHORIC / _EMPTY_SUBJECT exact-match check → passes
            └─ _propose_query(conversation_id, candidate)   ← "let me know" is now the query
```

**First point of loss:** `ResearchService._maybe_propose`, line
`reading = normalised or message`, in `backend/app/research/service.py`.
The conversation is represented there only by `conversation_id`; its *content*
is never in scope.

**Second, compounding defect:** in `backend/app/research/language.py`, the
guards `_ANAPHORIC` and `_EMPTY_SUBJECT` are **exact-match frozensets over the
whole subject string**:

```python
if subject.lower() in _ANAPHORIC:      # "it" caught; "it and tell me" not
if not subject or subject.lower() in _EMPTY_SUBJECT:   # "me" caught; "let me know" not
```

So an anaphor or filler *inside* a longer phrase escapes. `"let me know"` and
`"it and tell me"` are the observed escapes.

---

## 11. Memory vs conversation-state boundary

The boundaries are currently **clean**, and the audit's conclusion is that they
must stay that way:

| Concern | Correct owner | Today |
|---|---|---|
| "Search **it**" — what is *it*? | short-term conversation state | ❌ nothing owns this |
| "I prefer PostgreSQL" | long-term memory | ✅ Stage 2A |
| "a search is pending approval" | task/execution state | ✅ `Execution(proposed)` |
| "Fable is a video-game series" | entity/relationship knowledge | ✅ Stage 2B/2C |

**Mai is not currently using long-term memory to solve the pronoun problem** —
which is the right outcome, and a trap Stage 5D must avoid. Resolving
`"search it"` must **not** require that "Fable" first became a durable memory:
a one-off topic mentioned once is not a personal fact, and promoting it to
memory to enable a pronoun would corrupt the memory store.

---

## 12. Stage 5C interaction — clean, no contribution to the failure

- Imported history lives in `imported_archives` / `imported_conversations` /
  `imported_messages`, **not** in `conversations` / `messages`.
- `app/context`, `app/retrieval` and `app/prompt` import nothing from
  `app.history` — asserted by an existing structural test
  (`test_no_retrieval_path_reads_the_archive_tables`) and re-confirmed here.
- Imported knowledge reaches a turn **only** as derived `Memory` rows through
  ordinary retrieval, inside the reference block.
- Stage 5C therefore **cannot** be a source of recent-turn context and did not
  cause or worsen this failure.

**Forward-looking:** imported history is a legitimate future *recall* source
(long-term), but must never become *recent conversational context*. The
`origin` column and the separate tables already make that distinction
enforceable.

---

## 13. Stage 5A.2 interaction — contract intact, but its truth rule is not universal

Re-verified at HEAD: contract vocabulary pinned at 23 shapes, both
production-observed blobs refused, `add_message` confined to
`services/chat_service.py`, `LLMResponse` still has no `raw`, 25/25 mutations
caught with a validated harness.

Nothing in the continuity gap bypasses the response contract, and no internal
artefact can re-enter context — because only validated prose is ever persisted.

**But** §13 exposes a limit of 5A.2's scope. Its "no research claim without
research" principle is enforced through the *structured* path: the application
writes the truthful line when synthesis fails. It does **not** constrain
ordinary prose. A model that simply *writes* "Here are the top results from the
web search" in a normal turn passes the contract, because that text is valid
prose. See §14.2.

---

## 14. Security analysis

### 14.1 Injection boundary — currently strong, and fragile to the obvious fix

Historical conversation content is treated as **data**:

- every router (`calendar`, `mail`, `workflow`, `research`, `freshness`) reads
  only `normalise(content)` — the user's current message;
- prior turns reach only the *prompt*, as role-attributed messages, and the
  Stage 3B/3C containment tests already prove hostile stored text cannot
  become a system instruction or escape the reference block;
- there is no executor below the chat path, so prose cannot act.

**Could a malicious prior message make `"search it"` an unauthorised action
today?** No. The recogniser never reads prior messages, so injected text cannot
reach query construction at all.

**This is precisely the property most at risk in Stage 5D.** The naive fix —
"pass recent turns into the research recogniser" — would newly allow a prior
*assistant* message (which can contain summarised web content, email text, or
calendar titles) to determine what gets sent to an external search provider.
That is an exfiltration channel: a malicious calendar invite title could steer
the next `"search it"` into a query carrying personal data off-box.

**Required boundary for 5D:** a resolved subject may be derived **only from
user-authored turns**, must be **typed and length-bounded**, and must still
pass through the existing consent gate with the resolved query **shown to the
user before anything is sent**.

### 14.2 Fabricated execution claim — **the most serious finding**

Reproduced **2/2** on a clean conversation:

```
USER: Tell me about Fable.
USER: Search it.
  → research.outcome = None      (not recognised; no proposal registered)
  → reply: "…That would require using the web-search tool, which can only run
            after you explicitly approve it. Would you like…"
USER: yes
  → research.outcome = None, searched = None
  → research_outcome=not_research, request_path_research_calls=0,
    actions_executed=0, zero outbound calls in logs
  → reply: "**Web Search Results for "Fable"** | # | Title | Snippet | Source |
            … Fable (video game series) – Wikipedia  https://en.wiki…"
```

**No search ran. Mai fabricated results and attributed them to sources.**

Mechanism, and why it is a *continuity* bug:

1. `"Search it."` is not recognised (anaphoric, no antecedent visible) →
   **no proposal, so the consent gate never engages**;
2. the chat model *does* see history, resolves "it" → Fable, and **offers** a
   search — a capability claim the application has not registered;
3. `"yes"` finds no pending proposal, so it is an ordinary turn;
4. the model, having promised results, invents them.

This violates the standing principle that runtime facts are authoritative and
the model cannot turn "here is a research result" into "the web was searched."
It is a **truthfulness defect with a security dimension** (fabricated sources
presented as real), and it is caused by the continuity gap rather than by the
synthesis contract.

### 14.3 Other checks

| Check | Result |
|---|---|
| Malformed/internal artefacts re-entering context | Not possible — only validated prose is persisted |
| Imported history polluting current-turn reasoning | Not possible — archive unreachable from context/retrieval/prompt |
| Credentials in conversation history | None; log redaction and 5C scrubbing both active |
| Unauthorised execution from prose | No executor below chat; approval gates unchanged |

---

## 15. Exact root cause(s)

**Primary.** The routing/extraction layer that constructs the research query is
a pure function of the current message. `ResearchService._maybe_propose` holds
`conversation_id` but never the conversation's *content*, so the subject is
regex-extracted from `"search up the net and let me know"` → `"let me know"`.

**Secondary.** The resolved context **already exists** in the `IntentResult`
that `ResearchService.handle` is handed, and is **never read** — the parameter
is threaded two levels deep and dropped.

**Tertiary.** `_ANAPHORIC` and `_EMPTY_SUBJECT` are exact-match sets over the
whole subject, so an anaphor or filler embedded in a longer phrase escapes
(`"let me know"`, `"it and tell me"`).

**Quaternary.** No typed representation of active topic / unresolved question /
pending intent exists, so even a correct resolution has nowhere to live. Test F
demonstrates this directly: intent resolved *"its" → Fable* and the resolution
changed nothing.

**Classification (§20.D):** primarily **representation** and **intent
continuity**, surfacing as **query construction**. It is *not* a retrieval
failure — the data is retrieved twice over (intent's 4-turn window, the
prompt's 40-turn window) and then discarded.

---

## 16. Architectural gaps

1. No persisted conversational state between turns beyond raw messages.
2. No deterministic referent resolution; pronouns are handled only by prompting.
3. Routers cannot see anything the user said before this turn.
4. `IntentResult.goal` is free text of variable quality — it once described the
   classifier's own task — and is unfit as a load-bearing contract.
5. No typed "unresolved question" state, so a clarification that goes
   unanswered leaves no trace.
6. Context bounds are message-count based, not token based.
7. **The model can claim an execution that never happened** (§14.2).
8. A pending research proposal survives a topic switch until explicitly
   abandoned (Test F).

---

## 17. What must **not** be changed

- `freshness.assess()`'s user-message-only contract (Stage 5A.1).
- The Stage 5A.2 response contract, its 23-shape vocabulary, and the single
  assistant-message write site.
- The research consent gate: nothing leaves the process before the user
  approves the **exact query shown to them**.
- Stage 5C's archive separation and `Memory.origin` / `stated_at` semantics.
- Provider abstraction and `LLMResponse` normalisation (no `raw`).
- Calendar/Gmail read-only scopes, OAuth, network policy, allowed hosts.
- The rule that prior-turn content is **data**, never instructions.

---

## 18. Proposed Stage 5D architecture

```
USER MESSAGE
   ↓
Conversation Retrieval  (existing, unchanged)
   ↓
┌──────────────────────── Context Resolver (NEW, deterministic) ───────────┐
│  input:  current user message + last N USER-AUTHORED turns (typed)       │
│  output: ResolvedTurn  — typed, bounded, no free-form blob               │
│    · active_topic        : Optional[Topic]                               │
│    · unresolved_question : Optional[str]                                 │
│    · referents           : Tuple[Referent, ...]                          │
│    · resolved_subject    : Optional[str]   ← what "it" means             │
│    · resolution_source   : enum (CURRENT_MESSAGE | PRIOR_USER_TURN | NONE)│
│    · confidence          : float                                         │
└──────────────────────────────────────────────────────────────────────────┘
   ↓
Freshness / research recognisers  (unchanged inputs; additionally may be
   offered `resolved_subject` as an explicit, separately-audited argument)
   ↓
Consent gate — shows the RESOLVED query to the user
   ↓
Execution → Synthesis → Stage 5A.2 contract → AssistantResponse → History
```

**Design rules:**

- **Deterministic first.** Referent resolution should be application logic over
  user turns, not another model call. A model call here would re-import the
  unreliability documented in §7.
- **Typed, never a blob.** `ResolvedTurn` is a closed structure. No
  "context string" is passed anywhere.
- **User-authored only.** The resolver reads user messages. Assistant text —
  which may carry web, email, or calendar content — is excluded, preserving
  §14.1.
- **Resolution is visible.** The consent prompt shows the resolved query
  ("I can search the web for **Fable vs Asta**"), so a wrong resolution is
  refused by the user, not silently sent.
- **Fail closed.** No confident resolution → today's behaviour: ask for the
  topic (the Test B path), which is already safe.

---

## 19. Proposed data structures

```python
class ResolutionSource(str, Enum):
    CURRENT_MESSAGE = "current_message"   # subject was in this message
    PRIOR_USER_TURN = "prior_user_turn"   # carried from an earlier user turn
    NONE            = "none"              # unresolved — ask

class Referent(NamedTuple):
    surface: str          # "it", "the second one"
    resolved: str         # "Fable"
    turn_offset: int      # how far back
    confidence: float

class Topic(NamedTuple):
    subject: str                  # "Fable vs Asta"
    entities: Tuple[str, ...]     # ("Fable", "Asta")
    opened_at_turn: int
    still_open: bool

class ResolvedTurn(NamedTuple):
    active_topic: Optional[Topic] = None
    unresolved_question: Optional[str] = None
    referents: Tuple[Referent, ...] = ()
    resolved_subject: Optional[str] = None
    resolution_source: ResolutionSource = ResolutionSource.NONE
    confidence: float = 0.0
```

Bounds: subject ≤ 200 chars, ≤ 8 referents, ≤ 6 user turns inspected, topic
expires after a configurable number of turns or an explicit topic switch.

**No schema change is required for a first implementation** — `ResolvedTurn`
can be computed per turn from messages already loaded. Persisting active topic
is a later, separable decision.

---

## 20. Proposed context-resolution pipeline

1. **Detect** whether the current message is *subject-bearing* or
   *subject-referring* (reuse `_ANAPHORIC`, widened from exact-match to
   token-level — see §15 tertiary).
2. If subject-bearing → today's behaviour, unchanged.
3. If subject-referring → walk back over **user** turns, newest first, to the
   most recent subject-bearing one; extract its subject.
4. Apply a **topic-switch check**: an intervening user turn that introduced a
   different subject invalidates the older one (Test F's weather case).
5. Emit `ResolvedTurn`; `resolution_source` records provenance.
6. The recogniser proposes the **resolved** query; the consent prompt shows it.
7. No resolution → ask (existing safe path).

---

## 21. Required test matrix

| # | Category | Expected | Failure mode | Owner |
|---|---|---|---|---|
| A | Immediate follow-up | "search the net and let me know" → resolved subject | bogus standalone query | deterministic |
| B | Pronoun | "search it" → prior subject | not recognised at all | deterministic |
| C | Ellipsis | "and availability?" → prior subject | dropped | deterministic |
| D | Comparison continuation | "which one is better?" | loses both operands | deterministic |
| E | Follow-up research | resolved query shown before send | unresolved query sent | deterministic |
| F | After clarification | unanswered clarification stays open | silently forgotten | deterministic |
| G | Multi-entity | "the second one" → 2nd entity | wrong entity | model + deterministic check |
| H | Topic switch | "its" after a switch → new topic or ask | resolves to stale topic | deterministic |
| I | Long conversations | bounded walk-back, no unbounded growth | context blow-up | deterministic |
| J | Long-term vs short-term | "search it" works **without** a memory existing | pronoun requires memory | deterministic |
| K | Imported-history separation | archive never becomes recent context | pollution | structural/AST |
| L | Tool-call contamination | no internal JSON in resolution input | blob becomes subject | structural |
| M | Prompt injection | assistant-authored text never becomes a query | exfiltration via search | **security** |
| N | Research-content injection | "search again for X" in results is inert | auto-research loop | **security** |
| O | Gmail-content injection | email body cannot steer a query | data exfiltration | **security** |
| P | Calendar-content injection | event title cannot steer a query | data exfiltration | **security** |
| Q | Malformed model output | contract still refuses | regression | existing 5A.2 |
| R | Provider independence | resolution is provider-agnostic | Groq-specific | deterministic |
| S | Freshness routing | user-message-only contract preserved | boundary widened | structural |
| T | Response contract | canonical response preserved | bypass | existing 5A.2 |
| **U** | **Execution truthfulness** | **no "here are the results" without a completed search** | **§14.2 fabrication** | **deterministic** |

Category **U** is added by this audit and should be treated as the highest
priority item, independent of the rest of Stage 5D.

---

## 22. Mutation-testing strategy

Harness must be validated **before** any score is reported — per the Stage 5A.1
false 32/32 and the Stage 5A.2 procedure: verify every targeted test file
exists, the baseline exits 0, tests actually execute (parse `--collect-only`
counts rather than scraping `-q`), at least one **control** mutation survives,
and at least one known-real mutation is caught.

Planned mutations:

| # | Mutation | Must be caught by |
|---|---|---|
| 1 | Drop prior-turn retrieval in the resolver | A, B |
| 2 | Reverse conversation ordering | B, H |
| 3 | Include assistant turns in resolution input | M, O, P |
| 4 | Drop the topic-switch check | H |
| 5 | Remove referent resolution entirely | B, D, G |
| 6 | Use the current message only for the query | A, E |
| 7 | Widen the walk-back to unbounded | I |
| 8 | Let imported memories feed active topic | J, K |
| 9 | Allow the resolved query to skip the consent prompt | E, M |
| 10 | Mark `resolution_source` always `CURRENT_MESSAGE` | provenance tests |
| 11 | Let a tool-call-shaped string become a subject | L |
| 12 | Remove the "no claim without execution" guard | **U** |

---

## 23. Live verification plan

1. Fable vs Asta → "search the net and let me know" → resolved query shown
2. Entity → pronoun ("search it")
3. Comparison → "which one?"
4. Clarification → follow-up
5. Topic switch → return to old topic
6. 10+ turn conversation → recall
7. Imported history vs current conversation separation
8. Research result → follow-up question
9. Gmail result → follow-up question
10. Calendar result → follow-up question
11. **"Search it." → "yes" → assert no fabricated results** (§14.2)

Verify each time: no incorrect standalone query, no internal JSON, **no false
tool claims**, no unauthorised execution, no injection escalation, no console
errors, no browser storage, canonical `AssistantResponse` intact.

---

## 24. Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| **Resolution widens the injection surface** (§14.1) | user-authored turns only; typed; consent shows the resolved query |
| Wrong resolution sends the wrong query | the consent gate already shows the query — keep it, never auto-send |
| Over-resolution ("it" that meant nothing) | fail closed: no confident resolution → ask |
| Re-solving this with a model call | reintroduces §7's unreliability; prefer deterministic |
| Simply enlarging the prompt window | does not help at all — the routers never see the prompt |
| Scope creep into a "context blob" | closed `ResolvedTurn` type; no free-form string crosses layers |

---

## 25. Recommended implementation scope

**Split Stage 5D into two, and do the second one first.**

- **Stage 5D.1 — Execution truthfulness (small, urgent, independent).**
  Fix §14.2: Mai must not claim a search, an email read, or a calendar read
  that the runtime record does not show. This is a bounded change at the
  synthesis/response boundary, it does not need the context resolver, and it
  closes a live fabrication path.

- **Stage 5D.2 — Context resolution (the main stage).** `ResolvedTurn`, the
  deterministic resolver, the anaphor-guard widening, and consuming the
  resolved subject in research query construction — with the consent gate and
  the user-authored-only boundary preserved.

Deliberately **out of scope**: persisting topic state to the database,
model-based resolution, entity-linked coreference, and any change to freshness,
Gmail, Calendar, OAuth, providers, or the frontend.

---

## Audit verdict: **PASS**

- **Why the context was lost:** the layer that builds the research query is a
  pure function of the current message; the resolved context exists in two
  other places and is discarded.
- **Where:** `ResearchService._maybe_propose` (`app/research/service.py`) —
  `reading = normalised or message`; compounded by exact-match anaphor guards
  in `app/research/language.py`.
- **What would fix it:** a typed, deterministic `ResolvedTurn` computed from
  user-authored turns and consumed by query construction.
- **How, without weakening anything:** user-authored input only, typed and
  bounded, consent gate unchanged and now showing the resolved query, response
  contract and archive separation untouched.

### Unrelated defects found (documented, not fixed)

1. **Fabricated search results after an unregistered offer** — §14.2. Most
   serious; recommended as Stage 5D.1.
2. **Intent classifier can describe its own task** in `goal`
   (`'Classify the user message "…"'`).
3. **Intent degrades to `unknown`/`None`** intermittently on identical input.
4. **A pending research proposal survives a topic switch** until abandoned.
5. **Context bounds are message-count based, not token based.**
6. Live Calendar is currently `reauthorisation_required` — the OAuth grant has
   lapsed since Stage 5C verification; capability reporting is behaving
   correctly.
