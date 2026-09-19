# Stage 5C — Acceptance Report

## Status: **PASS**

ChatGPT history can be imported as a historical archive, and what Mai learns
from it is a separate, derived layer that cannot overrule the present, cannot
instruct, and cannot carry a credential into storage.

## Baseline

Before any Stage 5C change, on `3b69fdb` (Stage 5A.2):

```
4099 passed, 1 skipped
```

## What Stage 5C implemented

| Requirement | Where |
|---|---|
| Secure export ingestion | `app/history/sources.py` — directory, not upload |
| Format detection | `app/history/parser.py` — `chatgpt_zip`, `chatgpt_json`, closed |
| Bounded parsing | 10 limits in `Settings`, every one a hard stop |
| Raw archive storage | `imported_archives` / `imported_conversations` / `imported_messages` |
| Conversation/message provenance | `Memory.source_imported_message_id` + CHECK |
| Canonical memory extraction | `MemoryService.store_imported` — one insert site |
| Confidence/status classification | existing Stage 2A thresholds, unchanged |
| Temporal conflict handling | `Memory.stated_at` + `_origin_guard` |
| Deduplication | the live path's, reused |
| User-vs-assistant separation | `EXTRACTABLE_ROLES`, filtered in SQL |
| Prompt-injection resistance | inherited Stage 3B reference block, re-asserted |
| Secret detection and protection | `app/history/sanitise.py`, before the first INSERT |
| Import idempotency | unique index on the content hash |
| Failed-import recovery | per-conversation savepoints, reason codes, resumable |
| Frontend import status | `components/HistoryImportPanel.tsx` |

## Files changed

**New (backend):** `app/history/{__init__,models,parser,sanitise,schemas,service,sources}.py`,
`app/api/routes/history.py`, `alembic/versions/0010_history_import.py`,
`tests/test_history_import.py`, `tests/test_history_pipeline.py`,
`tests/security/test_history_import_security.py`,
`tests/security/test_history_import_structure.py`

**Modified (backend):** `app/memory/models.py`, `app/memory/service.py`,
`app/knowledge/conflicts.py`, `app/core/config.py`, `app/runtime/facts.py`,
`app/database/metadata.py`, `app/api/deps.py`, `app/api/routes/__init__.py`,
`app/main.py`, `tests/conftest.py`, `tests/security/test_gmail_structure.py`

**New (frontend):** `components/HistoryImportPanel.tsx`
**Modified (frontend):** `lib/api.ts`, `lib/types.ts`, `components/Sidebar.tsx`

**Modified (infra):** `docker-compose.yml`, `.gitignore`, `imports/.gitkeep`

## Database / schema changes

Migration `0010`. Three new tables, four changes to `memories`.

| Change | Note |
|---|---|
| `imported_archives` | unique on `source_sha256` — the idempotency key |
| `imported_conversations` | unique on `(archive_id, external_id)` — resumability |
| `imported_messages` | unique on `(conversation_id, external_id)`, role enum, redaction count |
| `memories.origin` | `live` / `imported`, server default `live` |
| `memories.stated_at` | added nullable, backfilled from `created_at`, then NOT NULL |
| `memories.source_conversation_id` | now nullable |
| `memories.source_imported_message_id` | FK to `imported_messages`, CASCADE |
| CHECK `provenance_matches_origin` | exactly one provenance root, matching the origin |

**Verified on the real database.** After migrating the live `mai` database, all
27 pre-existing memories have `origin = live`, `stated_at = created_at`, a
conversation root and no import root — which is what makes the conflict
detector's switch to `stated_at` a genuine no-op for existing knowledge.

The chain round-trips `upgrade → downgrade → upgrade → downgrade base →
upgrade` on both SQLite and real PostgreSQL. The CHECK was tested by
attempting the two invalid shapes directly against PostgreSQL; both were
refused, and a valid row was accepted.

## Security changes

| Property | How |
|---|---|
| No credential reaches storage | scrubbed before the first INSERT; the archive holds only the masked form |
| No credential reaches a log or response | only a *count* is reported, never a value or an offset |
| Imported content cannot instruct | derived memories are ordinary `Memory` rows, bound by the Stage 3B reference block — re-asserted for 6 hostile payloads |
| Imported content cannot authorize or execute | AST audit: `app/history` imports nothing from `app.tools`, `app.execution`, `app.workflows` |
| Imported content cannot grant a capability | capabilities come from configuration; `can_execute_actions` stays `False` after importing an archive that says otherwise |
| Only the user speaks for the user | `EXTRACTABLE_ROLES` is a one-member frozenset, filtered in SQL |
| Assistant text cannot become a user fact | extraction is called with `assistant_message=""` — structural, not prompted |
| History cannot overrule the present | `stated_at` ordering **and** an origin guard that does not trust the export's clock |
| Raw archive never reaches a prompt | AST audit over `retrieval`, `context`, `prompt`; plus a live check of the assembled prompt |
| No context flooding | retrieval selects; a 40-conversation import did not enlarge an unrelated prompt |
| No path traversal | refusal, not rewriting; parent **identity**, not prefix; symlinks resolved |
| No new network destination | AST audit: no `httpx`, `requests`, `urllib`, `socket`, `app.integrations` |
| No new form-parsing surface | no `UploadFile`, no `python-multipart`, no `request.form` — asserted by AST |

### The upload that was deliberately not built

Adding a file-upload endpoint would have meant `python-multipart` and
Starlette's form parser on a reachable path — the exact reason
`PYSEC-2026-249` is currently inapplicable to this deployment. The Stage 5C
brief said not to upgrade Starlette unless a vulnerability became reachable;
the better answer was not to create the reachability. Imports read from a
read-only bind mount instead, which also keeps a multi-hundred-megabyte export
out of the ASGI request path.

## New tests and test count

| Run | Result |
|---|---|
| Baseline (`3b69fdb`) | 4099 passed, 1 skipped |
| **After Stage 5C** | **4201 passed, 1 skipped** (158.77s) |
| Stage 5C suites alone | 102 tests |
| `test_migration_compatibility.py` with `TEST_POSTGRES_URL` | **15 passed, 0 skipped** |

New tests: **102** across four files — parsing and bounds
(`test_history_import.py`), the pipeline end to end
(`test_history_pipeline.py`), security
(`security/test_history_import_security.py`), and an AST structural audit
(`security/test_history_import_structure.py`).

## Mutation testing

**30 of 30 caught. Harness validated.**

```
=== harness validation ===
  all target test files exist        : yes
  every mutation anchor matches      : yes
  tests collected                    : 243
  unmutated test set exits 0         : True
  control survives (a reworded comment        ): yes
  control survives (an unasserted log string  ): yes
  => harness VALID
```

The first run scored **24/30**, and the six survivors were the most useful
output of this stage:

| Survivor | What it exposed |
|---|---|
| H12 path rewritten to basename | **No traversal test existed.** I had verified it in an ad-hoc shell script and never committed the test. |
| H13 identity check → prefix match | The test I wrote used a traversing *name*, which is refused earlier by the bare-name check — so it never reached the containment logic at all. |
| H14 symlinks listed | Covered only by a symlink to a non-prefix-sharing directory, which `startswith` would also have refused. |
| H15 idempotency pre-check deleted | **Equivalent by design** — the unique index still enforces it. Now pinned by asserting which branch runs, via its log line. |
| H20 zip-bomb check deleted | The structural test looked for a mention of `file_size`; the line computing the total still mentions it. Now pins the *comparison against the limit*. |
| H4 assistant text passed to extractor | The test searched the prompt for an assistant marker; the mutation passed the *user* text twice, so the marker was still absent and the test still passed. Now asserts the argument itself. |

Three of the six were security properties with no test at all. Two were tests
that looked like they covered something and did not.

## Live verification

Against the real running stack (real Groq model, real PostgreSQL, real
browser), using a synthetic export containing a preference conversation, a
credential-bearing conversation and a hostile `system` turn.

| Check | Result |
|---|---|
| Import through the API | `completed`, 3 conversations, 7 messages, 2 redactions, 3 memories |
| Credentials in the archive | **0** — stored as `gsk_***`, `postgresql://u:***@h` |
| Derived provenance | `origin=imported`, import root set, live root null |
| `stated_at` vs `created_at` | **2023-01-01** vs 2026-09-19 — the divergence that matters |
| Assistant's "you should start a consultancy" | **0 memories** |
| Hostile `system` turn | **0 memories** — but archived, so history is preserved |
| Live conversations created | **0** |
| Idempotent re-import | `already_imported: true`, same run id |
| Renamed copy | `already_imported: true` — content-addressed |
| Traversal (`../../etc/passwd`, `../.env`) | refused, `invalid_filename` |
| Secrets in container logs | **0** |
| Archive content in logs | **0** — ids and counts only |
| Assembled prompt (via `/api/prompt/debug`) | 2470 chars; no raw archive text, no system turn, no assistant claim, no secret — only derived memories |
| Answering from imported knowledge | *"You've said that you prefer **PostgreSQL** for your side-project databases"* — correct, attributed, and not repeating the assistant's flattery |
| Cascade deletion | removing the archive removed 3 conversations, 7 messages and 3 derived memories, leaving the 27 pre-existing untouched |

A second live run with a different archive confirmed the same properties after
the design change in §"Provider/runtime behaviour discovered".

### Frontend

| Check | Result |
|---|---|
| Panel lists available exports with sizes | yes |
| Import button runs the import | yes |
| Run summary | "3 conversations, 3 remembered · 2 credentials masked" |
| Idempotent click | "renamed-copy.zip was already imported — nothing to do." |
| Console output | **none** |
| `localStorage` / `sessionStorage` / cookies / IndexedDB | **all empty** |
| Archived content in the DOM | **none** (checked for the secret, the scrubbed key, and archive-only text) |

## Provider/runtime behaviour discovered

**Entity extraction is a precondition for conflict detection, and that changed
the design.**

The first implementation chained `run_entity_extraction` — which cascades into
relationships and then conflict evaluation — into a FastAPI background task
after the import response, mirroring the live chat route. Two things came out
of testing it:

1. It segfaulted the test suite. The background task opens its own session,
   which the suite's `StaticPool`-over-`:memory:` fixture cannot support; a
   file-backed fixture fixed that, and then the deeper chain exhausted the
   greenlet stack on Python 3.9.
2. More importantly, the design was wrong. A live turn derives one or two
   memories; an import derives hundreds. Chaining a model call per memory
   turns one click into an unbounded burst of requests.

So the import now runs **conflict evaluation only**, inline, in its own
session — `app.knowledge` imports nothing from `app.llm`, so it costs queries
rather than requests — and does not run entity or relationship extraction.

That has an honest consequence, found by writing the test rather than by
assuming: conflict *detection* resolves entity names, so with no entities
extracted, an imported memory rarely triggers a supersession of its own. The
direction the specification requires is unaffected and is verified end to end
— a live memory has entities and reaches imported ones by text match, so
**newer explicit statements still retire older imported beliefs**. The
remaining case is recorded as a limitation rather than claimed as working.

A second, smaller discovery: masked text can still match the rule that masked
it (`postgres://u:***@h` matches the user:password rule again), so both
`contains_secret` and the redaction *count* had to be defined in terms of
whether another pass would change anything. The naive spelling reports secrets
in text that has already been cleaned.

## Remaining known issues

**Introduced by Stage 5C — none.** No dependency was added: `requirements.txt`,
`constraints.txt` and `package.json` are unchanged.

**Carried forward, unresolved, and deliberately out of scope:**

1. **The three pre-existing malformed blobs.** Inspected as instructed. All
   three contain only a tool name, an action name and a generic public search
   query (`latest Nvidia GPU`, `latest Python version`, `latest news about
   OpenAI`) — **no credentials, no personal data**. Reproduction was tested
   live: a research turn in `88f48b18…` (the conversation holding two of them)
   returned clean sourced prose with no imitation. The same test on
   `d30bedea…` is **inconclusive** — Groq began rate-limiting. Both shapes are
   in the Stage 5A.2 refusal vocabulary, so imitation cannot reach the user
   either way. **Not deleted**: that is the user's data and the user's call.
   Stage 5C does not touch live conversation persistence, so this stays a
   follow-up.
2. **Credential rotation.** Still outstanding: Google OAuth client secret,
   Groq key, both OpenRouter keys, GitHub PAT, first Tavily key. Verified that
   the application takes every secret from environment/configuration — each is
   a `${VAR}` reference in compose with no literals, no `ARG`-carried secrets
   in the Dockerfile, and no credential literals in `backend/app` or
   `frontend`.
3. **`starlette 0.52.1` — five advisories.** Unchanged by this stage and
   **not upgraded**, as instructed. None is reachable: no `request.url` or
   `base_url` reads, no `HTTPEndpoint`, no `StaticFiles`, and no form parsing
   — the last of which Stage 5C actively preserved by not building an upload
   endpoint. Fixing requires a FastAPI major upgrade (`fastapi==0.128.8` pins
   `starlette<1.0.0`).
4. **Container OS layer is not scanned.** No Trivy or Grype available; Docker
   Scout requires a login that was not performed. **No claim is made that the
   Debian layer has been security-scanned.**
5. **`next@15.5.4` reports a security vulnerability (CVE-2025-66478)** in its
   own npm install warning. Pre-existing, not introduced here, and not
   upgraded — a frontend dependency bump is its own task. The frontend binds
   loopback-only.

**Stage 5C's own limitations** are in `docs/stage5c_history_import.md` §10:
synchronous import, extraction quality inherited from Stage 2A, one provenance
anchor per conversation, no entity/relationship extraction for imported
memories, and `stated_at` being only as honest as the export — which is why
the origin guard exists as a clock-independent second barrier.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Raw history preserved as an archive, separate from active memory | **PASS** |
| Derived memories carry provenance, timestamp, confidence, status | **PASS** |
| Explicit user statements distinguished from model-generated content | **PASS** |
| Historical information does not automatically become current truth | **PASS** |
| Newer explicit statements supersede older conflicting information | **PASS** (verified end to end) |
| Imported content cannot grant capabilities, authorize tools, execute, or modify instructions | **PASS** |
| No context flooding; retrieval selects | **PASS** |
| No unnecessary duplicate memories/entities/relationships | **PASS** |
| Secret detection and protection | **PASS** |
| Import idempotency | **PASS** |
| Failed-import recovery | **PASS** |
| Real PostgreSQL verification | **PASS** (15/15, migration chain round-trips) |
| Docker verification | **PASS** |
| Full test suite | **PASS** (4201 passed, 1 skipped) |
| Mutation testing with a validated harness | **PASS** (30/30, controls survive) |
| Live verification | **PASS** |
| Frontend verification | **PASS** |
| Full security audit | **PASS**, with carried-forward findings reported and assessed |
