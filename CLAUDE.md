# CLAUDE.md — Claude Code workflow for Mai

This file covers **how Claude Code works** in this repository. The shared rules
and the architectural contract are in [`AGENTS.md`](AGENTS.md). Implementation
history and current state are in [`docs/CHANGELOG.md`](docs/CHANGELOG.md). Do
not restate either one here. Read them.

Claude Code is typically used for staged backend work: architecture-bearing
stages, security-sensitive changes, migrations, and verification.

## Start of every session

1. **Read [`AGENTS.md`](AGENTS.md) in full** before changing anything.
2. **Read [`docs/CHANGELOG.md`](docs/CHANGELOG.md)** before beginning any
   substantial work. Identify:
   - the **latest completed stage** and its commit hash (cross-check with
     `git log --oneline -5`);
   - anything recorded as **uncommitted, in progress, defective or deferred**;
   - the follow-up items left by the previous stage.
3. **Run `git status --short`** and classify every changed or untracked path:
   - yours (part of the current task);
   - another agent's or the owner's uncommitted work (often Codex UI/adapter
     work). **Preserve it**: do not edit, revert, stash, reformat, stage or
     commit it;
   - unclear: **stop and ask**.
4. Do not trust conversation memory over the repository. If the changelog,
   `git log` and memory disagree, the repository is the source of truth.

## Scope discipline

- Do exactly the requested stage or task. Do not begin the next stage, even
  when the previous one points to it.
- If the request would alter core architecture without explicit approval
  (see `AGENTS.md` §6), **stop**. Describe the boundary that would be crossed,
  why, and the smallest alternative inside the boundary. Then wait.
- Do not fix unrelated defects you discover. Record them in your final report
  and in `docs/CHANGELOG.md` under known defects. Offer them as separate
  follow-up tasks.
- Do not modify `.env`, credentials, dependency pins or Docker behaviour
  unless the task explicitly calls for it.

## Verification

Run verification appropriate to the change (`AGENTS.md` §7). For a stage this
means focused tests, security and structural AST tests, a validated mutation
harness with controls, the full suite ordered and shuffled on the final code,
and live PostgreSQL against a throwaway database.

Claude-specific practice:

- **Isolate when the tree is shared.** If other uncommitted work breaks or
  could affect tests, verify in `git worktree add --detach <scratch> HEAD`
  with only your files copied in. Before committing, confirm that your files
  are byte-identical between the worktree and the real tree. Remove the
  worktree afterwards.
- **Keep scratch work in the scratchpad directory**: mutation harnesses, live
  verification scripts, shuffle plugins, schema dumps. Never put scratch work
  in the repository.
- **Long runs:** use a long Bash `timeout` or `run_in_background`. Do not
  poll with sleep loops when a completion notification is coming.
- **Shell quoting:** the Bash tool runs zsh, which does not word-split
  `$VAR`. Use arrays (`"${files[@]}"`) or `bash -c` when passing path lists to
  `git` and `grep`, and check that a "clean" result was not vacuous.
- **Secrets:** to reach PostgreSQL or Docker, build connection strings from
  the container's environment inside the shell. Write them only to `chmod 600`
  scratch files, never echo them, and delete them afterwards.
- **Prove a test can fail.** For concurrency and security guards, temporarily
  remove the guard in the isolated copy and confirm the test fails. Then
  restore the file and confirm it is byte-identical.

## Multi-agent work

Used only when the owner asks for it. The main session is the **lead**; the
agents in `.claude/agents/` (`implementer`, `verifier`, `security-reviewer`)
work for it.

- **Task card first.** The lead gives each agent a self-contained card: goal,
  the exact files it owns, the frozen interface, what is out of scope, the
  tests required, and the stop conditions. Agents start without the lead's
  context, so the card must stand alone.
- **One owner per file.** No two concurrent tasks touch the same file. The
  lead alone edits shared files: `docs/CHANGELOG.md`, `AGENTS.md`, this file,
  `backend/app/core/config.py`, `backend/app/main.py`, `backend/app/api/deps.py`,
  `backend/app/database/metadata.py`, `backend/tests/conftest.py`,
  `docker-compose.yml`, `.env.example`, `frontend/lib/api.ts`,
  `frontend/lib/types.ts`, and every migration.
- **Worktrees.** Implementers and the verifier run in their own worktrees
  under `.claude/worktrees/`, branched from local `HEAD`
  (`worktree.baseRef` in `.claude/settings.json`). A worktree has no `.env`,
  `.venv` or `node_modules`: run backend tests with the main checkout's
  `backend/.venv/bin/python`; the lead links `node_modules` for frontend tests.
- **Agents never** stage, commit, push, fetch, merge, deploy, restart
  services, run migrations, use the GitHub CLI (`gh`) or read `.env`. That is
  an instruction. The owner runs `gh` and every push. What
  `.claude/settings.json` adds, as listed there:
  - **denied:** `git push`, `reset`, `clean`, `stash`, `rebase`, `merge`,
    `restore`, `checkout --`, `update-ref`, `reflog expire`, `gc`,
    `filter-branch`, `filter-repo` and `replace`; every `gh` command; the
    Alembic invocations it names; reads of `.env`, `.env.bak*` and
    `.env.local`;
  - **ask first:** git `add`, `commit`, `fetch`, `pull`, `worktree`, `branch`,
    `checkout`, `switch`, `rm`, `config`, `tag`, `cherry-pick`, `revert`, `am`
    and `remote`; every `docker` and `docker-compose` command; the npm and pip
    commands it names that install or change dependencies; `npx` and `brew`.

  Everything else has no rule. In particular `ssh`, `scp`, `rsync`,
  `kubectl` and `curl` are installed and unrestricted unless a separate
  control applies, so the settings do not block deployment, network access or
  destructive writes (`rm`, `cp` into the main checkout) as such. The rules
  match command text only: `git -C . push`, an absolute program path or
  `sh -c '...'` is not matched, and a script that opens `.env` itself is not
  stopped. They are guard-rails, not a security boundary.
- **Report** (every agent): files changed, commands run, tests actually run
  with counts, failures, unverified assumptions, security concerns, and what
  integration needs. A claim without command output is not evidence.
- **Integration (lead only).**
  1. **Check the worktree before building anything.** All three checks must
     pass before step 2; each failure stops integration.
     - **Same base.** When the task starts, the lead records its base commit
       (`git rev-parse HEAD` in the main checkout) on the task card. Before
       integration, compare `git -C <worktree> rev-parse HEAD` with it. If they
       differ, stop and report that the agent's worktree contains a commit or
       otherwise has a different `HEAD`. Do not undo, reset or rewrite it: the
       patch in step 2 is built against the worktree's `HEAD`, so committed
       work would be missing from it.
     - **Whole status against the card.** Run
       `GIT_OPTIONAL_LOCKS=0 git -C <worktree> status --porcelain=v1 --untracked-files=all`
       (the variable stops `status` refreshing the worktree's index) and check
       every entry (modified, deleted, renamed and untracked) against the card.
       `git diff --name-only` is not enough: it omits new files. Any path the
       card does not assign stops integration until it is explained. The lead's
       `node_modules` link and `*.tsbuildinfo` are gitignored, so they do not
       appear.
     - **Agent's report against the same status.** Every file the agent
       reports as changed must appear in that status list. A reported file that
       is missing stops integration for investigation; it is never silently
       left out.
  2. **Build the patch so new files cannot be lost.** `git diff` leaves out
     untracked files, so use a temporary index and leave the worktree's index
     and the main index untouched. `<index>` must be an **absolute** path
     **outside the repository** and **new for this operation** (for example
     `<scratchpad>/<task>-<timestamp>.index`, checked not to exist first):
     `GIT_INDEX_FILE=<index> git -C <worktree> read-tree HEAD`, then, with the
     same `GIT_INDEX_FILE`, `git -C <worktree> add -- <assigned paths>` and
     `git -C <worktree> diff --cached --binary > <patch>`, where `<patch>` is
     also absolute and outside the repository. Delete the temporary index
     afterwards.
  3. **Check the patch before using it.** Its file list must equal the status
     list from step 1, new files included, and its hash is recorded for the
     review.
  4. **Apply** it in an integration worktree with `git apply --check`, then
     `git apply`, and stop on any conflict. Never resolve one with reset,
     checkout or `--force`.
  5. **Test** the integrated result: the focused tests, the security tests and
     the full suite once. No implementer reviews its own work.
- **Unfinished or failed work** stays in its worktree with its report until
  the lead has confirmed it holds nothing unique.
- Never choose "Yes, and don't ask again" for git, docker or migration
  prompts: the approval is saved for the whole repository.

## Finishing work

1. Stage **only your files, by explicit path**. Show `git status`,
   `git diff --cached --name-status` and `git diff --cached --stat`, and
   confirm that the unrelated changes are untouched.
2. Commit with the requested message, ending with the attribution trailer
   given in the session instructions.
3. **Update [`docs/CHANGELOG.md`](docs/CHANGELOG.md)**: add the entry for the
   work, with commit hash, purpose, architectural changes, the verification
   actually performed (pass/fail/skip counts, mutation result, live
   PostgreSQL and Docker results), security findings, limitations and
   follow-ups. Update the "Current state" section, including any
   uncommitted work you observed but did not touch.
   - If the task commit must not contain other files, commit the changelog
     update as its own `docs:` commit immediately afterwards. Record the code
     commit's hash; the changelog commit does not need to record its own.
4. Report the commit hash, files committed, verification results, and the
   final `git status`. State plainly what was **not** done or could not be
   verified.
