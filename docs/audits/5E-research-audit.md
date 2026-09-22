# Stage 5E — Research Quality & Search Intelligence: Audit (Phase 1)

**Audit only.** No production code was modified. HEAD `3f4200e`, tree clean.

Evidence was gathered by running the real recognisers, the real integration
and the real provider. Two read-only probe scripts were used and removed; they
are reproduced inline below.

---

## 1. Repository state

| | |
|---|---|
| HEAD | `3f4200e` — Stage 5D.2 |
| Tree | clean |
| Suite | 4405 passed, 1 skipped (verified) |

---

## 2. Research architecture map

| Step | File | Function | In → Out | Security boundary |
|---|---|---|---|---|
| Normalisation | `app/language/normalise.py` | `normalise()` | raw → repaired text | nouns only; **verbs never corrected** |
| Context resolution | `app/orchestration/resolution.py` | `resolve()` | msg + **user turns** → `ResolvedTurn` | user-authored only, typed |
| Intent | `app/intent/service.py` | `understand()` | msg + 4 turns → `IntentResult` | app state; never reaches the prompt |
| Research grammar | `app/research/language.py` | `recognise()` | **current msg only** → `Recognition` | closed families |
| Freshness | `app/orchestration/freshness.py` | `assess()` | **current msg only** → `FreshnessAssessment` | user-typed text only |
| **Query construction (freshness path)** | `app/orchestration/freshness.py:408` | **`_subject_of()`** | msg → subject string | — |
| Query construction (grammar path) | `app/research/language.py` | `recognise().query` | msg → subject | — |
| Proposal | `app/research/service.py:262` | `_propose_query()` | subject → `Execution(proposed)` | consent gate |
| Execution | `app/research/service.py` | `_run()` | approval → tool run | Stage 4E gates |
| Provider request | `app/integrations/web_search.py:233` | `_search()` | query → HTTP POST | `NetworkPolicy`: one host, no redirects |
| Parsing | `app/integrations/search.py:164` | `parse_results()` | payload → `SearchResults` | field-by-field, bounded, drop-not-repair |
| Rendering | `app/integrations/search.py:76` | `as_external_data()` | results → text block | `ExternalData`, untrusted label |
| Pass-through | `app/research/service.py` | `_rendered_results()` | outcome → block | **no ranking, no dedup** |
| Prompt | `app/prompt/formatter.py` | `with_research()` | block → section | untrusted preamble |
| Synthesis | `app/services/chat_service.py` | provider call | prompt → prose | — |
| Claim check | `app/synthesis/execution_truth.py` | `validate()` | prose + record | 5D.1 |
| Shape check | `app/synthesis/contract.py` | `validate()` | prose | 5A.2 |

**There is no ranking, evidence, source-quality, contradiction or
verification layer anywhere in this pipeline.**

---

## 3. Exact Claude/Opus failure path

Run against the real modules:

```
USER MESSAGE   : 'what is the latest model by Claude, what is the latest model of opus ?'
NORMALISED     : (unchanged — no corrections)
RESOLVED       : (none) — source=unresolved, ambiguity=no_antecedent
RESEARCH GRAMMAR: family='' query='' clar=''        ← declines: no research verb
FRESHNESS      : requirement=required  source=web  reason=recency_marker
                 SUBJECT = 'latest model by Claude, what is the latest model of opus'
FINAL QUERY    : 'latest model by Claude, what is the latest model of opus'
```

Matches the reported incident exactly.

`freshness._subject_of` (`freshness.py:408`) strips **one** leading
interrogative frame — `_FRAME.sub(text, count=1)` — plus trailing politeness,
and keeps everything else verbatim. The docstring says why: *"faithful is the
requirement… everything in between survives."* Correct for a single question;
for a **compound** question the second clause survives as query text.

### The exact provider request

```
POST https://api.tavily.com/search
Authorization: Bearer <REDACTED>
{
  "query": "latest model by Claude, what is the latest model of opus",
  "max_results": 5,
  "search_depth": "basic"
}
```

Not sent: `topic`, `days`, `time_range`, `include_answer`,
`include_raw_content`, `include_domains`, `exclude_domains`,
`chunks_per_source`.

### What came back

| # | unique URL? | date | domain |
|---|---|---|---|
| 1–5 | **1 unique URL across 5 slots** | **none** | `www.cnbc.com` only |

All five results are the *same CNBC article from 2025-11-24*, four of them the
same URL differing only in tracking fragments. **Mai's evidence set was one
document while `result_count` reported 5.**

Live, 3/3 runs answered "Claude Opus 4.5".

---

## 4. Root cause

A chain. **Tavily is not the primary cause** — proven by holding the provider,
credential and code constant and changing only the request:

| Variant | unique URLs | dated | domains |
|---|---|---|---|
| **A — Mai's current request** | **1 / 5** | 0 | cnbc.com |
| **B — clean query only** | **5 / 5** | 0 | **anthropic.com**, wikipedia, youtube — surfaced *"Claude Opus 4.6"* |
| **C — clean + `topic=news`** | 5 / 5 | **5 / 5** | surfaced *"…Opus to Fable 5.1"* |
| **D — clean + news + `days=30`** | 5 / 5 | **5 / 5** | anthropic.com, Sep-2026 material |
| **E — clean + advanced + news** | 5 / 5 | **5 / 5** | support.claude.com release notes |
| **F — `include_domains=anthropic.com`** | 3 / 5 | 0 | returned *Claude Instant 1.2* (ancient) |

Verdicts:

| Layer | Verdict | Evidence |
|---|---|---|
| Query construction | **PRIMARY** | compound query → 1 unique URL; clean query → 5 |
| Result deduplication | **PRIMARY** | absent; 4 near-identical URLs kept |
| Provider parameters | **CONTRIBUTING** | no `topic=news` ⇒ 0 dates returned |
| `published_at` parsing | **CONTRIBUTING** | field exists on the model, **never populated** (`parse_results` sets title/url/domain/snippet only) — dead |
| Ranking / source quality | **CONTRIBUTING** | none exists; provider order trusted |
| Synthesis | **NOT A FACTOR** | faithfully reported its single source, attributed to CNBC |
| 5A.2 / 5D.1 | **NOT A FACTOR** | worked; 5D.1 kept the claim truthful |
| Tavily | **NOT A FACTOR** | same provider, better request, materially better evidence |

**One-line root cause:** Mai sends a raw, compound question as a literal search
string with no recency parameters, keeps duplicate results, parses no dates and
performs no ranking — so a single stale secondary article became the entire
evidence set, and synthesis reported it correctly.

Variant F matters for design: naive "prefer official domains" returns an
ancient page. Primary-source preference **requires date awareness to be safe**.

---

## 5. Tavily findings

- Endpoint `https://api.tavily.com/search`, POST, `Authorization: Bearer`,
  single-host `NetworkPolicy`, no redirects, credential never in URL or body.
- Request is **hardcoded** in `_search()`; `max_results` defaults to 5 and the
  research path never overrides it (`arguments={"query": candidate}`).
- `search_depth` is `"basic"`; `"advanced"` never used.
- Response fields Tavily returned but Mai discards: **`score`**, **`answer`**,
  `raw_content`, `id`. `published_date` was absent because `topic` was unset.
- `tests/test_tavily_provider.py:121` **pins the body keys** to exactly
  `{query, max_results, search_depth}` — a deliberate guard that Stage 5E will
  need to update consciously.

---

## 6. Query construction findings

| Question | Answer |
|---|---|
| Where is the query created? | Two places: `research.language.recognise()` (grammar path) and **`freshness._subject_of()`** (freshness path) |
| Original wording preserved? | Yes — faithfully, which is the defect for compound questions |
| Context resolution applied? | Only on the grammar path, and only when the grammar found a request with no readable subject |
| Freshness incorporated into the query? | **No** — freshness routes, it does not shape the query |
| Current date incorporated? | **No** |
| Entities extracted / names normalised? | **No** |
| Conversational wording removed? | One leading frame only |
| Query rewriting / expansion / decomposition? | **None** |
| Primary-source targeting? | **None** |
| Multiple queries per turn? | **No** — exactly one search per approved proposal |
| Deterministic? | **Yes**, fully |
| Can an LLM modify the query? | **No** |
| Can external content influence it? | **No** — routers read only `normalise(content)` |

The determinism and the user-authored boundary are strengths and must survive.

---

## 7. Freshness findings

Freshness **stops at routing.** `FreshnessAssessment(requirement, reason,
source, subject)` chooses *who handles the turn* and supplies a subject string.
Nothing downstream consumes `requirement`.

- Does it reach the provider? **No.**
- Date range / `days` / `time_range`? **No.**
- "today" → an actual date? **No.**
- "latest" treated specially? Only for *routing*.
- Freshness validated after retrieval? **No.**
- Can a stale result be accepted? **Yes — demonstrated.**
- Publication dates inspected? **No** — and with the current request none are returned.
- Old-authoritative vs newer-source conflict? **No concept of it.**
- Results with no dates? Normal path; nothing notices.

---

## 8. Source / evidence findings

Preserved per result: title, URL, domain, snippet. **Dropped:** score,
publication date, provider ordering rationale, raw content.

`as_external_data()` renders `[n] title — domain / URL / Snippet`. **No date
line**, so even a populated `published_at` would not reach synthesis today.

Mai currently: trusts provider ordering ✓; passes all results ✓; filters
duplicates ✗; detects contradictions ✗; detects stale sources ✗; prefers
primary/official/reputable sources ✗; compares publication dates ✗.

No `evidence set`, `source confidence`, `source freshness`, `contradiction` or
`unresolved research` concept exists anywhere in the repository.

---

## 9. Synthesis findings

The prompt receives the rendered block: index, title, domain, URL, snippet —
under `RESEARCH_PREAMBLE`, which instructs attribution and forbids citing or
inventing sources not present. It does **not** receive dates, scores, rankings,
or any authority signal.

**Stage 5A.2 guarantees structure; Stage 5D.1 guarantees execution honesty;
neither guarantees factual correctness.** 5A.2 asks "is this prose?"; 5D.1 asks
"did the action it claims actually happen?" Nothing asks "is the claim
supported by the best available evidence?" — the Opus 4.5 answer passed both
checks because it *was* prose and a search *had* run.

---

## 10. Citation findings

Citations are positional text (`[1] … URL: …`) inside the untrusted block, not
a structured type. They reach synthesis as prose and reach the user as whatever
the model writes. `SearchResults` is not persisted; only `results_block` text
is carried on `ResearchResult`, and conversation history stores only the final
answer.

Consequences: citations are **not tied to individual claims**; source metadata
does not survive the turn; a stale citation can sit beside a current claim.

Mai **cannot** currently answer *"why do you believe this is the latest
source?"* — it has no date, no score and no authority signal to answer with.

---

## 11. Provider-coupling findings

Better than expected. `SearchProvider` (NamedTuple) + `PROVIDERS` dict +
`resolve_provider()` already abstract host/URL/method/auth. Outside
`app/integrations/`, the only mentions of Tavily are **comments** and one
config default (`SEARCH_PROVIDER = "tavily"`).

What is *not* abstracted:

1. the request body is built inline in `_search()` with Tavily's field names;
2. no per-provider **capability** description (which support `topic`, `days`,
   `include_domains`);
3. `parse_results()` reads `description|snippet|content` by name — tolerant,
   but has no place for provider-specific date/score fields;
4. `tests/test_tavily_provider.py` pins the exact body keys.

Adding Exa/Brave is a **small** change: extend the descriptor with a
request-builder and a capability set. No orchestration rewrite needed.

---

## 12. Latency / cost findings

Measured live, 3 runs of the failing query:

| Turn | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Propose (deterministic, no search) | 1173 ms | 1664 ms | 1013 ms |
| Confirm (search + synthesis) | 4465 ms | **19013 ms** | **26857 ms** |

The propose turn is ~1 s and includes one intent classification call. The
confirm turn is dominated by **Groq synthesis**, not by Tavily — the search
integration logs its own `latency_ms`, and provider `response_time` is present
in the payload. Variance of 4.5 s → 26.9 s across identical requests points at
provider-side variability.

Existing instrumentation: `elapsed_ms` on HTTP responses, `latency_ms` logged
by the search integration, `perf_counter` timings in `chat_service`. **No
end-to-end per-stage breakdown is surfaced together**, which is the gap.

Cost per turn today: 1 intent call + 1 synthesis call + 1 search call
(+ at most 1 recovery). Query rewriting would add ≤1 LLM call; a verification
search would add 1 search call; decomposition multiplies search calls by N.

---

## 13. Existing test coverage

| Area | Files | Tests | Assessment |
|---|---|---|---|
| Research grammar | `test_research_language.py`, `security/test_research_language_security.py` | 42 | **strong** |
| Freshness | `test_freshness.py`, security + structure | 67 | **strong** (routing only) |
| Web search / provider | `test_web_search.py`, `test_tavily_provider.py`, `security/test_web_search_security.py` | 64 | **strong** on boundary, **absent** on quality |
| Network boundary | `security/test_network_boundary.py` | 12 | strong |
| Research chat flow | `test_research_chat.py`, `security/test_research_chat_security.py` | 43 | strong |
| Context resolution | `test_context_resolution.py`, `test_context_continuity.py`, security | 50 | strong |

**Missing entirely** — grep over `tests/` finds no assertion on any of:

- result **deduplication**
- **publication dates** (`published_at` appears in no search test)
- result **ranking** or ordering rationale
- **source quality / primary-source** preference
- **contradiction** detection
- **staleness** of an accepted answer
- query **quality** (nothing asserts the query is well-formed, only that it is *faithful*)

False-mutation-confidence risk: the provider tests pin the request body keys,
so a mutation that *adds* a parameter is caught, but no test would notice a
mutation that discards dates or duplicates, because nothing reads them.

---

## 14. Missing tests (to be created in 5E)

Per sub-stage, listed in §15.

---

## 15. Proposed Stage 5E breakdown

Ordered by *evidence of impact*, cheapest-first. **5E.1 and 5E.2 alone fix the
reported incident** — B and C/D in the table above are exactly those two.

### 5E.1 — Query construction for compound and noisy questions *(deterministic, no LLM)*
Split a compound question into its clauses; build the query from the clause
set rather than the raw remainder; drop the second interrogative frame. Keep
determinism and the user-authored boundary.
*Tests:* unit on `_subject_of` with compound/nested/multi-clause inputs;
regression pinning the exact incident string; existing freshness suite unchanged.
*Mutation:* remove clause splitting; keep only the first clause; strip all frames.
*Rollback:* pure function, revert in isolation.

### 5E.2 — Freshness reaches the provider
Carry `FreshnessRequirement` into the proposal and into the request: set
`topic="news"` and a bounded `days`/`time_range` when freshness is REQUIRED.
Parse `published_date` into the already-existing `published_at`. Render a date
line in `as_external_data()`.
*Tests:* request-body assertions per freshness state; parse tests with and
without dates; a test that the rendered block carries dates.
*Mutation:* drop the freshness parameter; drop date parsing; drop the date line.
*Rollback:* parameters are additive; revert restores today's body.

### 5E.3 — Deduplication and evidence set
Deduplicate by normalised URL (strip fragment/tracking params) before
bounding, so `max_results` buys distinct documents. Introduce a typed
`EvidenceSet` (results + dates + provider score) replacing the bare block.
*Tests:* the captured 5-identical-CNBC payload as a fixture → 1 result;
`result_count` must reflect distinct documents.
*Mutation:* disable dedup; dedup on full URL including fragments.

### 5E.4 — Source quality and ranking
A bounded, explainable source tier (official > docs > reputable > general >
aggregator/SEO) combined **with recency** — variant F proves tier alone is
unsafe. Ranking must be deterministic and auditable.
*Tests:* ordering fixtures; the F case (official-but-ancient must not win).
*Mutation:* reverse ordering; ignore dates; ignore tier.

### 5E.5 — Contradiction detection and verification
Detect disagreeing claims across the evidence set; for freshness-critical
questions, optionally a second verification query with primary-source
preference. Surface `unresolved_research` rather than silently picking one.
*Tests:* contradictory fixtures; cost bound on verification.
*Mutation:* accept the first source; suppress contradiction.

### 5E.6 — Provider abstraction completion
Move the request body into the descriptor as a builder + capability set, so
Exa/Brave differ by data rather than by code.
*Tests:* per-provider body assertions; capability gating (`topic` only where supported).
*Mutation:* ignore capabilities; send Tavily fields to Brave.

### 5E.7 — Benchmark harness
Runner over the §19 query set. **Ground truth stored with the date it was true
and a source URL; the runner flags stale ground truth rather than scoring
against it.** Metrics: query quality, retrieval correctness, freshness,
primary-source rate, citation correctness, contradiction rate, latency, cost,
relevant-result rate. All provider calls go through `SecureHttpClient`/`NetworkPolicy`.

### 5E.8 — Synthesis grounding and citation integrity *(optional, last)*
Give synthesis dates and tiers; tie citations to claims.

**LLM-based query rewriting is deliberately *not* in 5E.1–5E.7.** The evidence
says deterministic fixes recover most of the loss. If it is added later it must:
take **only user-authored turns** (the `resolution.py` boundary), produce a
**bounded proposal** shown in the existing consent step, and never execute a
search directly.

---

## 16. Mutation strategy

Every mutation below must be paired with a named test *before* the campaign
runs. Harness rules carried from 5A.1/5D.1/5D.2:

1. verify every target test file exists;
2. baseline suite exits 0;
3. mutations actually apply — **anchor match is checked first**;
4. at least one **control** mutation must **survive**;
5. at least one known-real mutation must be **caught**;
6. **fixtures literal-pinned, never derived from implementation constants**
   (5D.2 found two self-defeating bound tests this way);
7. every survivor classified: real defect / equivalent (with proof) /
   unreachable (with proof);
8. a no-op mutation is a **harness defect**, not a survivor (5D.1 and 5D.2 each
   had two).

Planned: remove freshness propagation · drop date parsing · drop the date line ·
disable dedup · dedup including fragments · reverse source ordering · ignore
publication date · ignore source tier · remove contradiction detection ·
collapse multi-query · use raw message instead of the constructed query ·
trust the first result · skip verification · bypass provider capabilities.

---

## 17. Live verification plan

For every benchmark query record: user message → resolved subject → final query
→ **exact provider request** → unique-URL count → dates present → domains →
chosen evidence → answer → citations → latency per stage.

Primary gate: *"what is the latest model by Claude, what is the latest model of
opus?"* must produce a clean query, ≥3 distinct domains, dated results, and an
answer consistent with the newest authoritative source — or an explicit
"sources disagree".

Browser checks unchanged: no internal JSON, no fabricated claims, no console
errors, no storage.

---

## 18. Acceptance criteria (for the implementation phase — none are met today)

1. Compound questions produce a clean, well-formed query.
2. Freshness-required turns send recency parameters the provider supports.
3. Publication dates are parsed and reach synthesis.
4. Duplicate documents do not consume result slots.
5. `result_count` reflects **distinct** documents.
6. Source ordering is deterministic, explainable, and combines tier **with** recency.
7. An ancient official page does not outrank a current authoritative one.
8. Contradictions are detected and surfaced rather than silently resolved.
9. Claims are traceable to a specific source with a date.
10. No LLM can execute a search; any rewrite is a bounded proposal under consent.
11. External content still cannot become user intent.
12. Provider swap requires no orchestration change.
13. Per-stage latency is measurable.
14. No secrets in logs; network policy unchanged.
15. Mutation harness independently validated before any score is reported.

---

## 19. Risks

- **Over-reduction of queries.** 5E.1 must not turn a faithful question into a
  keyword soup; the existing "faithful" property is deliberate. Mitigated by
  the consent step showing the query.
- **Naive primary-source preference is actively harmful** — variant F returned
  Claude Instant 1.2. Tier must never be applied without dates.
- **Cost/latency growth** from verification and decomposition. Bound per turn.
- **Provider date fields are unreliable** — dates appear only with
  `topic=news`, and general-topic results carry none. Ranking must degrade
  gracefully when dates are absent.
- **LLM query rewriting** would introduce the first place a model influences an
  outbound request. Deferred deliberately; if added, only as a bounded proposal.
- **Ground truth rots.** Benchmark answers about "latest" expire; the harness
  must flag stale ground truth rather than score against it.

---

## 20. Recommended next implementation step

**Stage 5E.1 + 5E.2 together, as one small stage.**

They are the two changes the evidence directly supports, they are deterministic,
they add no LLM call and no new capability, and variants B/C/D show they recover
source diversity and make freshness decidable. 5E.3 (dedup) is a close third and
could reasonably join them.

Everything after that should wait for the 5E.7 benchmark, so source-quality and
verification work is measured rather than argued.
