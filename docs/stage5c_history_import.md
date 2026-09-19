# Stage 5C — ChatGPT History Import & Personal Context Ingestion

## 1. The problem

The user has years of ChatGPT conversations describing projects, preferences,
decisions and goals. Mai should understand that history. It must not *become*
that history.

Those are different things, and conflating them causes three specific faults:

- **Stale truth.** A 2023 opinion imported today is newer than everything in
  the database by insert time, and would quietly retire what the user said
  last week.
- **Borrowed authority.** An export contains assistant text, system prompts
  and tool output. None of it is the user speaking, and some of it is
  literally instruction text aimed at a model.
- **Context flooding.** Thousands of historical messages in every prompt would
  bury the current conversation and cost a fortune.

## 2. Two layers, kept apart

```
export file
   │  detect → hash → bound → scrub
   ▼
RAW ARCHIVE            imported_archives / imported_conversations / imported_messages
   │  user-authored messages only
   ▼
DERIVED MEMORY         memories (origin = imported)
   │  existing retrieval / dedup / conflict machinery
   ▼
REFERENCE BLOCK        the one path any knowledge takes to a prompt
```

The archive is **evidence**; the derived layer is **knowledge**. Only the
derived layer can reach a model, and it reaches it through the same code that
carries live memories — so imported knowledge is deduplicated,
conflict-resolved and contained by machinery that already existed.

### Why not `conversations` / `messages`?

Putting imported history in the live tables would make it continuable,
listable beside real chats, and eligible for the live context window. Separate
tables make "raw history is not active memory" a property of the schema rather
than a rule someone has to remember. A structural test asserts no retrieval,
context or prompt module can even import `app.history`.

## 3. Time is two columns

`Memory` gained `stated_at` alongside `created_at`.

| | meaning | live | imported |
|---|---|---|---|
| `created_at` | when the row was written | now | now |
| `stated_at` | when the user said it | now | 2023 |

Conflict detection judges recency on `stated_at`. The migration backfills
`stated_at = created_at` for every existing row, so for live knowledge the two
are identical and the switch is a **no-op** — which the unchanged pre-existing
test suite demonstrates.

### The second guard, and why one is not enough

Ordering handles honest chronology. It does not handle a wrong clock, a
hand-edited export, or a conversation the user had elsewhere after telling Mai
something different. So `_origin_guard` additionally bars an imported memory
from superseding a live one:

```python
if trigger.origin is MemoryOrigin.IMPORTED:
    return (Memory.origin == MemoryOrigin.IMPORTED,)
return ()
```

A live memory remains unrestricted: newer explicit statements supersede older
knowledge exactly as Stage 3C specified, and that includes retiring imported
beliefs. Verified end to end.

The guard permits an imported memory to retire *another imported* memory, but
in practice that case does not fire today — see the limitation in §10 on
entities. The permission is written the honest way round so the rule reads as
what it is (a bar on overruling the present), not as an accident of what
happens to be wired.

## 4. Authority: only the user speaks for the user

```python
EXTRACTABLE_ROLES = frozenset({ImportedRole.USER})
```

A single-member frozenset, so a role added later is excluded by default. The
filter is applied **in the SQL query**, not in Python, so no code path can
obtain assistant or system rows and then decide what to do with them.

Two reinforcing measures:

- assistant, system and tool messages are **archived** (history is preserved)
  but never mined;
- extraction is called with `assistant_message=""`. The extraction prompt
  already forbids deriving facts from assistant text, but a prompt is an
  instruction and this is untrusted content — passing no assistant text makes
  the failure structurally impossible rather than merely disallowed.

Live verification: an export where the assistant said *"you are clearly a
database expert who should start a consultancy"* produced **zero** memories
about consultancies, and a conversation containing a hostile `system` turn
produced none at all.

## 5. Secrets are scrubbed before the first INSERT

An export is years of transcripts; somewhere in them is a pasted API key.
Copying that into new tables would mint a fresh secret-at-rest liability in a
system that has spent nine stages keeping credentials out of its own storage.

`app.history.sanitise` reuses `app.core.logging`'s pattern vocabulary —
`REDACTION_PATTERNS is _REDACTIONS`, asserted by test — rather than starting a
second list that would drift. What it adds is a **count**, because an import
must tell the user how many secrets it found without ever showing one.

One subtlety worth recording: masked output can still match the rule that
produced it. `postgres://u:pw@h` becomes `postgres://u:***@h`, and `u:***`
matches the user:password rule again. So `contains_secret` asks *"would
another pass change this?"* rather than *"does any pattern match?"* — the
naive spelling reports a secret in text that has already been cleaned, and
makes every "nothing survived" assertion unfalsifiable.

## 6. Bounded everything

An export is a large document from outside the system. Every dimension that
can grow has a hard stop: file bytes, uncompressed bytes, zip members,
conversations, messages per conversation, total messages, message characters,
parts per message, and node-graph depth.

Two deserve comment:

- **Zip bombs.** The declared uncompressed size is checked *before* the member
  is opened, and the actual bytes are capped again on read because the header
  is part of the untrusted document. Both raise the same error, so only a
  structural test can see the order — and the order is the guarantee.
- **Extraction budget.** Parsing is cheap; extraction costs a model call. A
  5,000-conversation archive must not become 5,000 requests the moment someone
  clicks import. The run reports what it did not reach, and re-running
  continues from there.

## 7. The node graph

ChatGPT does not store a conversation as a list. `mapping` is a dict of
parent-linked nodes forming a tree, because editing a message and regenerating
creates a branch. `current_node` names the leaf actually left on screen.

Walking `parent` up from `current_node` reconstructs the conversation as the
user last saw it. Iterating `mapping.values()` — the obvious approach — would
interleave abandoned branches and archive a conversation that never happened.
The walk is depth-capped and cycle-guarded, because `parent` is untrusted data.

## 8. Ingestion is a directory, not an upload

There is no upload endpoint, and that is a security decision.

Multipart form parsing means adding `python-multipart` and putting Starlette's
form parser on a reachable path. This application has **no form parsing at
all** today, which is exactly why `PYSEC-2026-249` does not apply to it, and a
history importer is a poor reason to give that up. It would also push a
multi-hundred-megabyte export through the browser and the ASGI request path.

So the operator bind-mounts a directory (read-only), the UI lists what is in
it, and the import runs server-side against a file the server already has. A
test asserts the absence of `UploadFile`, `python-multipart` and
`request.form` — by AST, not substring, since the source contains comments
explaining why they are absent.

### Path safety

The caller supplies a filename; the directory is configuration. Containment is
decided by **path identity**, not string inspection:

- a name that is not a bare name is *refused*, not rewritten —
  `Path(filename).name` would silently import a different file than the caller
  asked for, with an audit trail that no longer matches the request;
- the resolved parent must *equal* the import directory. `startswith` treats
  `/imports-evil` as inside `/imports`;
- symlinks are resolved before the comparison, so a link containing no
  suspicious characters cannot point outside.

Mutation testing found the tests for all three missing on the first pass.

## 9. Idempotency and recovery

`imported_archives.source_sha256` is unique. Importing the same export twice
returns the first run and does nothing — content-addressed, so a renamed copy
is recognised as the same export.

The unique index is the guarantee, not the application's pre-check: two
concurrent imports would both pass a check-then-act. The pre-check is an
optimisation, and a test asserts it is the branch actually taken (by its log
line) so it cannot quietly become decorative.

Recovery: conversations are written one savepoint each, so a failure rolls
back one conversation rather than the run. Conversations and messages are
unique per archive, so re-running an interrupted import **resumes** rather
than duplicating. A failed run records an application reason code — never an
exception string, which can embed the document fragment that broke the parser.

Deletion cascades: removing an archive removes its conversations, its messages
and the memories derived from them. Imported knowledge is fully retractable,
verified live.

## 10. Known limitations

- **Import is synchronous.** A very large export holds the request open. The
  bounds keep it finite, but a background job queue would be better and is not
  in this stage's scope.
- **Extraction quality is the extractor's.** Stage 5C reuses Stage 2A's
  prompt and thresholds; it does not tune them for historical text.
- **One anchor per conversation.** A derived memory points at the first user
  message, not at the sentence it came from. Finer provenance would need the
  extractor to report offsets, which it does not.
- **Entity and relationship extraction are not run for imported memories**,
  and this has a consequence worth stating plainly. Both are a model call
  *per memory*: a live turn produces one or two, an import produces hundreds,
  so wiring them would turn one click into an unbounded burst of requests.
  Conflict evaluation *does* run for every derived memory, because it costs no
  model call — but its replacement detection resolves entity *names*, so with
  no entities extracted, an imported memory rarely triggers a supersession of
  its own. The direction that matters is unaffected: a live memory has
  entities, and reaches imported ones by text match, so newer explicit
  statements still retire older imported beliefs. A later backfill of entity
  extraction would close the remaining case; imported memories are ordinary
  rows and need nothing special to be picked up.
- **`stated_at` is only as honest as the export.** A doctored timestamp
  misorders history — which is why the origin guard exists as a second,
  clock-independent barrier.
