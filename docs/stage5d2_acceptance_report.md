# Stage 5D.2 — Acceptance Report

## Status: **PASS**

The Fable/Asta follow-up now inherits its subject from the user's own earlier
turn. Every existing gate is intact, and no non-user content can reach an
outbound query.

## A. Baseline

`25bbcb5` (Stage 5D.1). Verified before any change: working tree clean,
**4300 passed, 1 skipped**.

## B. Implementation commit

See the final line of this report.

## C. Files changed

**New:** `app/orchestration/resolution.py`, `tests/test_context_resolution.py`,
`tests/test_context_continuity.py`,
`tests/security/test_context_resolution_security.py`, and these two documents.

**Modified:** `app/research/language.py` (substantive-subject check, one new
family), `app/research/service.py` (consumes the resolved subject),
`app/services/chat_service.py` (builds the user-only window, resolves once),
`tests/test_research_language.py` (reachability of the new family).

## D. Root cause addressed

The routing layer that builds the research query was a pure function of the
current message, so the subject came out as `"let me know"`. Two compounding
defects: the language layer's anaphora guards matched the *whole* subject, so
an anaphor inside a longer phrase escaped; and bare imperatives like
`"search it"` matched no request family at all.

## E–G. Architecture, typed result, user-authored boundary

`resolve(current_message, user_turns)` — **not** `resolve(conversation)`. The
caller filters by stored role in `ChatService._resolve_context`; authorship is
never inferred from text. `ResolvedTurn` is a closed NamedTuple whose `subject`
is empty unless `is_resolved`.

`app/orchestration/resolution.py` imports `enum`, `re`, `typing` — nothing
else. Deterministic, local, **no model call, no network**.

## H–K. Behaviour

**Active topic** is derived per turn as the most recent user turn that *states
a subject* — deliberately not "the last entity mentioned", which would reach
past an intervening question into an abandoned topic. A topic switch therefore
replaces the topic automatically.

**Unresolved question**: the comparison the user asked and Mai never answered
is still the most recent subject-bearing turn, so the follow-up inherits it.

**Referents**: it/this/that/them → active topic; "the second one" → 2nd member;
"the first and third" → both; "which one is better" → the whole comparison;
"what about pricing" → active topic **+ residual qualifier**.

**Refusing to guess**: no antecedent, or an ordinal outside the list, resolves
to nothing and Mai asks.

## L–O. Integration

- **Research**: the existing grammar still owns recognition. One family added
  (`anaphoric_command`) whose object is a closed anaphor set, anchored to end
  of message. It always yields `anaphoric_subject`, never a query.
- **Freshness**: `assess(message)` unchanged, still single-message. No ordering
  change was needed — freshness runs last, behind every recogniser.
- **Intent**: still not consumed by research, now deliberately. `intent.goal`
  proved unreliable across runs, and one authoritative resolved representation
  is the point.
- **Memory**: nothing merged. Resolving `"search it"` does not require that
  "Fable" ever became a memory.

## P–Q. 5A.2 and 5D.1 compatibility

Verified structurally after the change: **23** contract shapes, **one**
assistant-history write module, **two** generation call sites, execution-truth
adjudicator untouched.

## R. Security audit

| Threat | Result |
|---|---|
| Assistant prose establishing intent | **blocked** — not a user row (6 hostile payloads, parametrised) |
| Gmail body establishing intent | **blocked** (real stubbed transport) |
| Calendar title establishing intent | **blocked** (asserted no event-block fragment reaches the query) |
| Web result establishing intent | **blocked** — same mechanism |
| Imported ChatGPT history | **blocked** — separate tables, unreachable from context |
| Tool output | no tool rows exist in `messages` |
| Consent bypass | resolved subject produces a *proposal*; `searched is False` asserted |
| Direct execution from resolution | `_maybe_propose` may call `_propose_query`, never `_run`/`execute` — AST-asserted |
| Stale-topic leakage | switching turn is the most recent subject |
| Unbounded context | 6 turns / 200 chars / 8 entities, **literal-pinned** |
| New network or model call | none — import set asserted exactly |

The forbidden path *arbitrary conversation content → resolved intent →
external query* does not exist.

## S–T. Tests

| Run | Result |
|---|---|
| Baseline (`25bbcb5`) | 4300 passed, 1 skipped |
| **After Stage 5D.2, run 1** | **4405 passed, 1 skipped** |
| **After Stage 5D.2, run 2** | **4405 passed, 1 skipped** |

**105 new tests**, covering the §27 matrix A–AB.

## U–V. Mutation testing

**24 of 24 caught. Harness independently validated.**

```
=== harness validation ===
  all target test files exist        : yes
  every mutation anchor matches      : yes
  tests collected                    : 324
  unmutated test set exits 0         : True
  control survives (a reworded comment        ): yes
  control survives (an unasserted log string  ): yes
  => harness VALID
```

The harness **refused to report a score** on its first run — two mutation
anchors did not match (wrong indentation) and the control block named a stale
symbol. It is the third run that was valid.

The first valid run scored **20/24**, and the four survivors split evenly
between my mutations and my tests:

| Survivor | Finding |
|---|---|
| C20 | **No-op mutation** — it appended `# noqa`. A broken mutation is a harness defect, not a test gap. Rewritten to actually bypass the proposal. |
| C22 | **No-op mutation** — `{} or {...}` evaluates to the second dict, so the vocabulary was never emptied. Rewritten to disable the ordinal pattern. |
| C23 | **Self-defeating test** — the window test computed its filler from `MAX_USER_TURNS`, so raising the constant raised the filler too and the test still passed. |
| C24 | **Self-defeating test** — same shape for the subject-length cap. |

C23/C24 are the more interesting pair: a bound test that reads the constant it
is guarding can never fail. Both now use literals, with a separate test pinning
the constants and stating why they are 6 and 200.

## W. Live verification

Real stack, real Groq, real Tavily, real PostgreSQL.

| Test | Resolved query | Outcome |
|---|---|---|
| **1 — primary**: "Is Fable better or Asta?" → "search up the net and let me know" | **`Fable vs Asta`** | was `let me know` |
| 2 — pronoun: "Tell me about Fable." → "search it" | `Fable` | ✅ |
| 3 — comparison: → "…tell me which one is better" | `Fable and Asta` | ✅ |
| 4 — multi-entity: → "search the web for the second one" | `Asta` | ✅ |
| 5 — follow-up research: → "search the web and tell me more" | `Fable` | ✅ |
| 6 — topic switch: Fable → Bangalore → "search it" | `Bangalore` | no stale leakage |
| 7 — explicit return: → "going back to Fable, search it" | `Fable` | ✅ |
| no context: "search it" | — | `needs_clarification`, `searched=False` |

**Full authorised path**, end to end:

```
"Is Fable better or Asta?"       → no research
"search up the net and let me know" → awaiting_confirmation, query="Fable vs Asta", searched=False
"yes"                            → completed, searched=True, result_count=5, sourced prose
```

Live verification also found a real defect before commit: `"search the web and
tell me which one is better"` searched for *"which one is better"*, because
bare comparatives were not in the vocabulary. Fixed, with a regression test.

## X. Browser verification

The primary case in the real UI: *"I can search the web for **Fable vs Asta**"*.
No `ResolvedTurn`, `resolution_source`, `recent_user_context`, `UserTurn`,
`pending_user_question` or `ambiguity` anywhere in the DOM. `localStorage`,
`sessionStorage`, cookies and IndexedDB all empty. No console errors. The final
sourced answer stayed honest about what the search did and did not find —
Stage 5D.1 holding.

## Y. Residual risks

1. **A personal-scope question can become a web-search antecedent.** After
   "what is on my calendar tomorrow?", a bare "search it" inherits the user's
   own question. No calendar content leaks — the security property holds and is
   asserted — but the proposal reads oddly. Cosmetic; the user declines.
2. **Misspelled verbs still do not become requests** (`"serch it"`). Stage 5A's
   deliberate design, preserved.
3. **Subject extraction is shallow** — a lead-in stripper and a comparison
   grammar, not a parser. Mitigated by showing the query before sending.
4. **English only.**
5. **One antecedent**; a reference reaching two turns back past an intervening
   subject needs the explicit-return phrasing.
6. **Carried forward unchanged:** three legacy blobs, credential rotation,
   `starlette 0.52.1` (still unreachable), `next@15.5.4`, unscanned Debian
   layer, Calendar needing reauthorisation.

## Z. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Fable/Asta inherits the preceding subject | **PASS** (live) |
| 2 | "Search it" resolves simple references | **PASS** |
| 3 | "Which one?" works for bounded comparisons | **PASS** |
| 4 | Multiple-entity references work | **PASS** |
| 5 | Topic switches prevent stale leakage | **PASS** |
| 6 | Explicit topic returns work | **PASS** |
| 7 | Ambiguous references trigger clarification | **PASS** |
| 8 | No-context references trigger clarification | **PASS** |
| 9–14 | Assistant / Gmail / Calendar / web / imported / tool content cannot establish intent | **PASS** |
| 15 | Resolved context cannot bypass proposal/consent | **PASS** |
| 16 | Freshness behaviour unchanged | **PASS** |
| 17 | 5A.2 intact | **PASS** |
| 18 | 5D.1 intact | **PASS** |
| 19 | 5C provenance intact | **PASS** |
| 20 | No new external capability | **PASS** |
| 21 | Full suite passes | **PASS** (4405 ×2) |
| 22 | Mutation harness independently validated | **PASS** (refused twice first) |
| 23 | Live verification | **PASS** |
| 24 | Browser verification | **PASS** |
| 25 | Security audit | **PASS** |
| 26 | Working tree clean | **PASS** |
| 27 | Documentation complete | **PASS** |
