# Stage 7B ownership model (unit 3)

Status: **design, 2026-10-29.** Implemented unit by unit (see
`docs/stage7b_progress.md`). Builds on L0 (`e1e8b55`): the
`KnowledgeScope` value and the nullable scope columns. Nothing here weakens a
Stage 7A or earlier control.

## 1. The rule

Every read, write, update and delete of a scoped row is bounded by a
`KnowledgeScope` that was derived **in application code** from an
authenticated, active `Principal`:

- request path: `get_principal` (Stage 7A; re-reads the active membership in
  the database on every request) -> `KnowledgeScope.personal(principal)`;
- background path (unit 7): the scope stored on the work item, re-checked
  against the membership at execution time.

A scope never comes from a request field, a Telegram payload, a tool
argument, model output or a stored text. Services take it at construction
(`scope=` keyword, required), like `TaskService` takes `owner_id`.

## 2. Visibility

- **Personal** (`scope_id = member_id`): the only visibility read or written
  in units 4-10. A member sees only rows whose `organisation_id` and
  `scope_id` equal their scope.
- **Organisation** (`scope_id = organisation_id`, shared knowledge): stays
  unwritable (`WRITABLE_VISIBILITIES`) and is **not read** either, until a
  separately approved shared-memory policy defines who may write it, with
  what provenance, and how it is reviewed.
- **Unattributed** (`scope_id IS NULL`): legacy rows. **Quarantined**: never
  returned to any member, never used for deduplication, never traversed.

Queries filter on both `organisation_id` and `scope_id` (defence in depth: a
member id is unique, but the organisation filter keeps every query visibly
tenant-bound).

## 3. Which tables carry a scope

| Kind | Tables | How ownership is decided |
| --- | --- | --- |
| Direct scope (L0 columns) | conversations, memories, entities, entity_aliases, relationships, knowledge_conflicts, reminders, executions, workflows, imported_archives | Their own `organisation_id` + `scope_id` |
| Single-owner (`owner_id` = member id) | tasks, approval_grants, task_notifications, notification_deliveries | `owner_id`; the organisation through the membership. Unit 8 decides whether they gain scope columns |
| Child of a scoped parent | messages, imported_conversations, imported_messages, reminder_notifications, task_events, task_steps, execution_events, memory_entities, relationship_evidence | Their parent. Read only through a scope-filtered parent; written only after the parent is loaded inside the scope, in the same transaction |
| Identity (Stage 7A) | organisations, users, organisation_members, user_identities, identity_link_codes | Stage 7A's own rules |

No child table gains scope columns: deriving ownership from the parent
avoids two sources of truth.

## 4. Cross-row references

A reference may only point inside the same scope:

- `memories.source_conversation_id` / `source_message_id` -> a conversation
  and message of the same scope;
- `relationships` endpoints -> entities of the same scope;
- `memory_entities`, `relationship_evidence`, `knowledge_conflicts` -> rows
  of the same scope;
- `executions.conversation_id` / `workflow_id`, `reminders.conversation_id`
  -> the same scope.

Units 4-9 enforce this in code: the referenced row is loaded through the
scope filter before the reference is written. Unit 10 makes it relational
(section 6).

## 5. Uniqueness and deduplication

The global unique indexes leak across members: with
`uq_memories_type_normalized_content`, member B storing the same normalized
fact as member A collides with A's row, and deduplication would return or
reveal it. Unit 5 replaces each affected global unique index with two
partial unique indexes:

- `(scope_id, <columns>) WHERE scope_id IS NOT NULL` -- per scope;
- `(<columns>) WHERE scope_id IS NULL` -- the legacy rows keep their old
  guarantee.

Partial indexes work on SQLite and PostgreSQL and need no NOT NULL. The
affected indexes: `uq_memories_type_normalized_content`,
`uq_entities_normalized_name`, `uq_entity_aliases_normalized_alias`
(migration `0022_scoped_uniqueness`, units 5-6), and
`uq_imported_archives_source_sha256`, which moves with the history-import
scoping in unit 9 (an archive's duplicate check is part of that flow).
`uq_executions_idempotency_key` is already per call; unit 8 decides whether
it is scoped. Deduplication looks only inside the caller's scope.

**The "User" entity (units 5-6).** Memories are written about "User", and
relationship extraction always offers that entity. Migration 0005 seeded one
global row; it is unattributed, so it is quarantined like any legacy row and
never matched. Each scope gets its own, created by the entity service (the
only entity writer) the first time a memory of the scope links an entity;
relationship extraction only looks it up.

## 6. The enforcement migration (unit 10)

Only after units 4-9 write a scope on every new row, and after the legacy
quarantine is resolved:

1. orphan and consistency checks first; refuse, changing nothing, if any row
   references a row of another scope or a missing row;
2. `UNIQUE (id, organisation_id, scope_id)` on each scoped parent, and
   composite foreign keys from each child reference to it, so a reference
   across scopes is impossible in the database (the standard tenant-safe
   foreign key; both dialects support composite keys);
3. a foreign key from `(organisation_id, scope_id)` to the membership for
   personal rows (review M2);
4. `NOT NULL` on the scope columns only if no unattributed row remains;
   otherwise unattributed rows stay quarantined and NOT NULL waits.

Tested on disposable PostgreSQL: upgrade, refusal with orphans, refusal of a
downgrade that loses information, re-upgrade, schema equals the ORM.

### 6a. Unit 10 decisions (2026-10-30, before implementation)

- **Which references.** Every reference from a scoped table to a scoped
  parent: `memories.source_conversation_id`, `entity_aliases.entity_id`,
  `relationships.source_entity_id` / `target_entity_id`, the five
  `knowledge_conflicts` references, `reminders.conversation_id`,
  `workflows.conversation_id`, `executions.conversation_id` /
  `workflow_id`. References to an unscoped table (`memories.source_message_id`,
  `source_imported_message_id`) and child tables without scope columns
  (`memory_entities`, `relationship_evidence`, ...) stay enforced in code:
  child tables gain no scope columns (section 3).
- **How, without breaking the existing delete behaviour.** Each such
  reference keeps its existing single-column foreign key (with its CASCADE
  or SET NULL action) and gains a *second*, composite foreign key
  `(ref, organisation_id, scope_id) -> parent (id, organisation_id,
  scope_id)` with no action (NO ACTION, checked at the end of the
  statement). On CASCADE the child row is gone before the check; on SET NULL
  the reference column is NULL, and a composite key with a NULL component is
  not checked (MATCH SIMPLE) -- so the scope columns are never nulled. Legacy
  rows (scope NULL) are likewise unchecked: quarantine keeps working.
  Parents gain `UNIQUE (id, organisation_id, scope_id)`.
- **Preconditions.** The upgrade refuses, changing nothing, if any scoped
  row references a row of another scope or an unattributed row.
- **Deferred, with reasons -- a change to steps 3 and 4 above:**
  - the foreign key from `(organisation_id, scope_id)` to the membership
    (L0's M2): an organisation-visibility row (`scope_id` = the
    organisation's id) can never satisfy it, so it belongs with the
    shared-memory policy that will make that visibility writable. Until
    then, scopes are only ever derived from an authenticated, active
    `Principal` (units 4-9, structure-tested).
  - `NOT NULL` on the scope columns: legacy rows remain unattributed until
    the owner-approved attribution operation (section 7) has run.

## 7. Legacy rows (quarantine)

The owner's directive: ambiguous historical data stays quarantined until
safely classified. Before Stage 7A the Telegram webhook did not check the
sender, so legacy rows may not be the Admin's.

- Unattributed rows are excluded from every member read, retrieval,
  deduplication and traversal. **Consequence:** once scoped reads ship, the
  Admin's existing memories and conversations are not shown until they are
  attributed.
- Attribution is an explicit, audited operation (a later unit): it lists
  legacy rows by origin, lets the owner attribute rows to the Admin's
  personal scope (or leave them quarantined), and writes nothing without a
  confirmation. Running it against the real database is a **real-data
  mutation**: it needs the owner's approval, a backup and a rollback plan.

## 8. Testing

Each unit adds: positive and negative tests through the real services (a
second member in the same organisation, and a member of a second
organisation, never see or alter the first's rows, by id, filter or
enumeration); structural tests that every query on a scoped table is scope
filtered (AST); PostgreSQL tests for each migration; the full suite ordered
and shuffled. Test fixtures that write raw rows set an explicit scope; a test
that needs a legacy row writes one deliberately.
