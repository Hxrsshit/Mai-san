# CODEX.md — Codex workflow for Mai

This file covers **how Codex works** in this repository. The shared rules and
the architectural contract are in [`AGENTS.md`](AGENTS.md). Implementation
history and current state are in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).
Read both first. This file does not repeat them.

## What Codex is for

Codex is used primarily for **narrowly scoped** work:

- frontend and UI work in `frontend/` (components, layout, styling,
  branding, copy, accessibility);
- **adapters** that connect an external channel or client to an **existing**
  backend service (for example a messaging webhook that forwards a message
  into the existing chat turn and sends the reply back);
- simple integrations and small, local bug fixes that change no contract.

Core architecture belongs to staged work under the owner's direction. Codex
must not modify it silently. This covers authorization, execution, tasks,
runner, background runtime, monitoring, LLM provider selection, network
policy, the database schema and migrations.

## Start of every task

1. Read [`AGENTS.md`](AGENTS.md), especially §3 (security), §4.5 (adapters),
   §5 (safe changes) and §6 (when to stop).
2. Read [`docs/CHANGELOG.md`](docs/CHANGELOG.md): the latest completed stage,
   uncommitted work, and known defects.
3. **Run `git status --short` before touching any file.** Any change you did
   not make (often Claude's in-progress stage work) must be **preserved**. Do
   not edit, revert, stash, reformat, stage or commit it. If your task needs
   one of those files, stop and ask.

## Simple integration vs. architectural change

| Simple integration: proceed | Architectural change: STOP and report |
| --- | --- |
| UI that calls existing API endpoints | A new backend endpoint that writes, executes, schedules, monitors or grants |
| Adapter that forwards text to the existing chat/conversation service and returns its reply | Adapter that calls tools, the dispatcher, the execution service, the runner or the background runtime directly |
| Adapter authentication (shared secret, allowed sender), inert when unconfigured | A new identity, user or permission system |
| A config value read in one place, added to `.env.example` (no value) and passed explicitly in `docker-compose.yml` | Changing how existing settings, providers or network allow-lists are chosen |
| Outbound calls through `SecureHttpClient` with a single-host `NetworkPolicy` | A vendor SDK, or any HTTP that bypasses `SecureHttpClient` |
| Small, local bug fix inside the module you were asked to change | A fix that changes a state machine, a schema, a migration, or a security test |
| Copying an existing pattern exactly | A second scheduler, poller, worker, queue, runner or execution path |

If the requested integration cannot be done from the left column, **stop and
report** (see `AGENTS.md` §6). State which boundary would be crossed, why the
integration needs it, and the smallest alternative inside the boundary. Do
not implement the crossing.

## Verification

- Frontend: `npm run typecheck`, `npm run lint`, `npm run build` (from
  `frontend/`), where Node is available. If it is not, say so; do not claim
  that the checks passed.
- Backend changes, including adapters: run the focused tests for what you
  touched, then the full suite from `backend/` with
  `.venv/bin/python -m pytest` (see `AGENTS.md` §7). The code must run on
  Python 3.9.
- An adapter needs tests showing that it is inert when unconfigured, that it
  rejects unauthenticated callers, that it never logs message text or secrets,
  and that it reaches only the existing service.
- Report failures caused by **other** uncommitted work separately. Do not fix
  them as part of your task.

## Finishing work

1. Stage only your files, **by explicit path**. Never use `git add -A`,
   `git add .` or `git commit -a`. Confirm that the staged list contains no
   one else's work.
2. Commit only when verification is green.
3. **Update [`docs/CHANGELOG.md`](docs/CHANGELOG.md)** with what changed, the
   commit hash, the verification actually run, and anything incomplete. Work
   that is not committed and verified must stay under "Uncommitted / in
   progress", never under completed stages.
