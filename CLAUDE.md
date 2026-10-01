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
