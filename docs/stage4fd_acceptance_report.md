# Stage 4F-D — Acceptance Report

**Status: PASS.** Every claim below was executed.

Baseline: `0e1f919` (Stage 4F-C).

---

## 1. What was built

Web research is reachable from the normal chat path, behind a **two-turn
consent gate**:

```
turn N     "search the web for X"
           -> proposal recorded, Mai asks for confirmation
           -> nothing sent anywhere

turn N+1   "yes"
           -> proposal approved and run through the Stage 4E dispatcher
           -> results synthesised as untrusted external data
```

Stage 4F-B deliberately required per-query consent before a query reaches a
third party. The design question for this stage was how to reach chat
**without reversing that decision**. Two turns is the answer: the chat path
supplies the human decision the Stage 4E gates were already waiting for, and
skips none of them.

New modules: `app/research/{service,confirmation,schemas}.py`, migration
`0008_execution_conversation.py`.

## 2. What it deliberately is not

`CHAT_CONFIRMABLE_TOOLS` contains exactly one entry, `web_search`. A future
`send_email` does not become chat-executable by being registered — it joins
that set only by being added there, in code, with a reason. The chat path is
not a shortcut around the execution API; it is a narrow, named-tool route to
one read-only capability.

Nothing on this path is decided by a model. Identification reuses Stage 4D's
deterministic phrase table, confirmation is a phrase table in
`confirmation.py`, and the proposal text is written in the service. The
model's only involvement is synthesising an answer from results it is handed,
on the turn after consent was granted.

## 3. Tests

| | Count |
|---|---|
| Before (4F-C) | 2543 |
| **After** | **2618** |
| Added | 75 |
| Failures | 0 |
| Skips | 1 |

New: `tests/test_research_chat.py` (22), `tests/security/test_research_chat_security.py` (21), plus additions to the existing web-search security file.

The one skip is the pre-existing documented case:
`tests/test_migration_compatibility.py:349 — TEST_POSTGRES_URL is not set`.

## 4. Mutation testing — 13/13 caught

| | Mutation | Result |
|---|---|---|
| D1 | Chat may confirm any executable tool | PASS |
| D2 | A research request runs on the first turn, unconfirmed | PASS |
| D2b | Pending proposal skipped so "yes" re-matches as a new request | PASS |
| D3 | An unrelated reply is treated as consent | PASS |
| D4 | A declined proposal runs anyway | PASS |
| D5 | Confirmation accepts anything non-empty | PASS |
| D6 | A proposal from another conversation is confirmable | PASS |
| D7 | An API-created execution is chat-confirmable | PASS |
| D8 | A non-`PROPOSED` execution is confirmable | PASS |
| D9 | Results rendered as trusted text, not external data | PASS |
| D10 | A failed search is reported as a success | PASS |
| D11 | Research runs when the integration is unavailable | PASS |
| D12 | The query is taken from the model rather than the phrase table | PASS |

**One correction during the run.** D2 — the most important mutation, since the
entire design is "consent happens on a second turn" — initially reported
SKIPPED because my anchor text did not exist in the file. A skipped mutation
proves nothing, and reporting 11/12 would have hidden that the central
guarantee was unverified. The anchor was corrected and split into two
variants (run on turn 1; skip the pending proposal so "yes" re-matches as a
fresh request). Both are caught.

## 5. Live verification — **performed**

Executed against the running Docker stack and PostgreSQL, with an
**obviously-invalid** search key (`invalid-key-for-local-verification`) so the
full path could run without inventing a credential for a real account:

```
turn 1  'search the web for Groq news'  -> awaiting_confirmation
        "I can search the web for: ... Reply "yes" to go ahead"
turn 2  'yes'                           -> failed
        "I couldn't complete that web search. Nothing was retrieved."

execution: web_search  state=failed  conv_linked=True
journal:   ['proposed', 'approved', 'execution_started', 'execution_failed']
metadata has key? False
```

This exercised the real `SecureHttpClient`, the real `NetworkPolicy`, real
DNS and TLS, and a real response from `api.search.brave.com` — only the
credential was invalid. It verifies four things at once:

1. **The consent gate held.** Turn 1 sent nothing; the proposal was recorded.
2. **Turn 2 ran the full Stage 4E path** — approve, claim, dispatch.
3. **Mai reported the failure truthfully.** It did not claim to have searched.
   This is the Stage 4E.1 truthfulness rule holding under a real failure.
4. **The audit journal is complete and carries no credential.**

### The three unavailable states are genuinely distinct

| Configuration | Outcome | Message names |
|---|---|---|
| `EXECUTION_ENABLED=false` (shipped default) | `disabled` | the execution switch |
| Execution on, no `SEARCH_API_KEY` | `not_configured` | the missing provider |
| Execution on, key present | `awaiting_confirmation` | the pending query |

All three observed on the real stack. In the `not_configured` case **no
execution record was created**, so no proposal is stranded, and a subsequent
bare "yes" correctly resolved to `not_research`.

## 6. PostgreSQL

Migration `0008` applied to real PostgreSQL; database at `0008 (head)`.
Verified in the live schema:

```
conversation_id | uuid
"ix_executions_conversation_id" btree (conversation_id)
FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
```

Round-tripped on SQLite: upgrade → downgrade → re-upgrade, all clean. The
migration uses `batch_alter_table` so one migration serves both dialects, and
restates the CHECK constraints because a SQLite table rebuild preserves only
what it is told to.

`ON DELETE SET NULL` rather than `CASCADE`: deleting a conversation should not
delete the audit record of an action it proposed.

## 7. Docker

Stack running (`backend`, `db`, `frontend`), backend restarted with the new
code, health 200, ports loopback-only. Execution remains **off by default** in
the container — the two verification runs above enabled it per-command via
`docker compose exec -e`, and nothing persistent was changed.

## 8. Frontend

**No frontend change was made, and none was needed** — that is a property of
the design rather than an omission. The confirmation prompt is an ordinary
assistant message and the confirmation is an ordinary user message, so the
existing chat UI renders the whole flow.

Verified at `http://localhost:3000`: the conversation list loads from
PostgreSQL, the research turn renders, and the refusal text displays
correctly. **Zero console errors.**

`ChatResponse.research` is exposed for a UI that later wants to show the
pending query explicitly — `awaiting_confirmation` is the state that matters —
but nothing breaks without it.

## 9. Security properties preserved

Every Stage 4E and 4F gate still applies on this path. The chat route supplies
a human decision; it bypasses nothing.

- Approval is a genuine approval — the user was shown the exact query, and the
  fingerprint recorded at approval is over that same payload.
- An execution created through the API carries no `conversation_id`, so it can
  **never** be confirmed by a chat message.
- Only `PROPOSED`, only this conversation, only a chat-confirmable tool, only
  the most recent.
- An unrecognised reply discards the proposal rather than running it — the
  failure direction is correct.
- Search results remain untrusted external data and are never rendered as
  instructions.
- Research failure never fails the chat turn; it degrades to an ordinary turn.

## 10. Known limitations

- **A successful live search has never been observed.** No valid
  `SEARCH_API_KEY` exists. Every success-path test drives a stub transport
  through the real integration, client and policy — only the socket is
  replaced — so no real result, source or attribution has been seen end to end.
- **Only one phrase table identifies research.** "What's the latest on X?" is
  not recognised. Broadening it is a matching problem, not a security one, and
  Stage 4D's rule applies: a phrase broad enough to catch a paraphrase is
  broad enough to catch a mention.
- **The query is the user's whole message**, bounded — not an extracted search
  term. Extracting one would need a model call on a path that currently makes
  none, and would let a model choose what Mai searches for. Visible in the
  live run: the query was the literal sentence.
- **Confirmation is English-only** and the table is short. "sim" or "はい" will
  not be recognised; the proposal is safely discarded.
- **One proposal per conversation at a time.**

## 11. Recommendation

The obvious next step is not a stage. It is **obtaining a Brave Search API key
and re-running the live verification**, because the one thing this stage
cannot claim is that a real search has ever succeeded. Everything else is
verified; that single gap is worth closing before building anything on top of
research.

After that, Stage 4F-E or a broader research UI both become reasonable.

## 12. Outstanding user actions (unchanged)

The credentials exposed earlier in development should still be revoked: the
Groq API key, both OpenRouter keys, and the GitHub personal access token.
