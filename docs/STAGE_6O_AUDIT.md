# Stage 6O: Post-6N runtime and security audit

- **Audited commit:** `94ea032` "Stage 6N: confirmed Gmail monitoring" (previous verified baseline `25bfb98`).
- **Kind:** inspection only. No code, tests, schema or docs were changed during the audit, and nothing was committed.
- **Experiments:** run against HEAD code mounted read-only, on a throwaway PostgreSQL database (`mai_audit_6o`), which was dropped afterwards. The real `mai` database stayed at migration `0019`.
- **Follow-up:** Stage 6P fixed M1–M3 and the related low findings; see the note at the end.

## 1. Executive verdict

**SAFE WITH LIMITATIONS.** No blocker was found. Every core invariant held under the concurrent, adversarial and failure conditions that could be constructed, including the one race that first looked like a blocker, which was refuted on PostgreSQL. The remaining issues are a truthfulness gap and a grant-hygiene gap; neither causes unsafe execution.

Strongest evidence:
- **Cancellation and expiry win.** A stale write cannot reopen a cancelled or completed task: the database refuses it through `cancelled_requires_timestamp` and `completed_requires_timestamp` (`backend/app/tasks/models.py`). Proved on PostgreSQL by forcing the races (S1–S3, §9).
- **Every check is re-authorized:** capability re-bound, grant re-checked, arguments re-validated.
- **Notifications are recorded at most once per check,** by a unique index on `(task_id, kind, check_number)`.
- **Gmail is read-only and metadata-only,** and no Gmail content reaches Telegram, the journal or the logs.

## 2. Critical findings

None.

## 3. High-risk findings

None.

## 4. Medium findings

**M1. A confirmation could be silently undone by a concurrent replacement.** EXPOSED BY 6N (the mechanism is Known defect 8).
- "yes" and a new full request (or "no", or anything else) arrive concurrently in the same conversation. The replacing turn's cancel blocks on the confirmation's row lock; once the confirmation commits, the cancel overwrites QUEUED with CANCELLED.
- The user was told "Done. I'm monitoring…", but the monitor was cancelled and its grant left live until the deadline.
- It failed in the safe direction: a cancelled task is never claimed, so nothing executed.
- **Evidence:** reproduced on PostgreSQL (S4). The confirm turn returned `scheduled`; the task ended `cancelled` / `proposal_replaced` with 1 grant still live.
- **Likelihood:** needs two simultaneous turns in one conversation (two tabs, or direct API use).

**M2. A monitor's grant was not released when it completed or was blocked.** INTRODUCED BY 6N.
- `release_monitor_grants` ran only on cancel and expiry. After a match, or after three failures, the HIGH grant stayed live until its TTL (for an "every 6 hours for 7 days" monitor that matched on day 1, about 6 days).
- The TTL bound still held, and nothing but another monitor could use the grant: a least-privilege gap, not an exploit.

**M3. Nothing but the prompt stopped the model claiming monitoring.** PRE-EXISTING gap, more consequential after 6N.
- `backend/app/synthesis/execution_truth.py` had claim channels for web, mail and calendar, but none for monitoring. "I've checked your Gmail" was caught; "I've set up monitoring for you" was not.
- For phrasings the grammar misses, only the prompt line in `backend/app/prompt/formatter.py` stood between the model and a false claim.

## 5. Low findings

- **L1. A stale write against a cancelled task aborted that task's runtime pass via an exception.** PRE-EXISTING, EXPOSED BY 6N. Safe, contained per task, no state change (S1/S2: `IntegrityError`; S3: a failed `transition` expired the ORM object and `record_outcome` then raised `MissingGreenlet`). Cost: a misleading "Background task pass failed" log; the cancelled row kept `next_run_at` (harmless, claims require an active state).
- **L2. A Gmail read could still happen after a cancel committed, in a narrow window.** EXPOSED BY 6N. If the cancel committed between the runner's read and `_claim_check` (whose UPDATE had no state condition), one Gmail list could run, but only with another active monitor's grant. The conditional completion prevented completion and notification.
- **L3. The grant TTL is `ceil(deadline − now)`.** INTRODUCED BY 6N. The grant can outlive the deadline by under 1 s; the runner's deadline check comes first.
- **L4. A check started just before the deadline may finish, and notify, just after it.** INTRODUCED BY 6N. Ordinary in-flight semantics.
- **L5. A task refused for its budget, or waiting for approval, stays `running` and unscheduled and never expires.** PRE-EXISTING. Not reachable for 6N monitors in normal operation (§8).
- **L6. Expiry racing a cancel records two cancellation events.** Benign.
- **L7. The unexplained shuffled failure from 6N remains unexplained.** UNKNOWN.
- **L8. Conversations have no owner column.** PRE-EXISTING single-owner assumption; monitor lookups filter on the task owner.

## 6. Known defects

**Known defect 8 (`TaskService.transition` can lose a concurrent write):**
- **General problem?** Yes. `transition`, `mark_task_running`, `_record_failure` and the reschedule writes all read, then wrote by id alone.
- **Affected states:** non-terminal states (planned, awaiting_approval, queued, running, blocked, paused). CANCELLED and COMPLETED are protected by the CHECK constraints. FAILED has no such constraint, but no 6N path produces it.
- **Consequences:** execution after cancellation, no (S1/S2); execution after expiry, no (expiry is a cancellation); notification inconsistency, no (S3); incorrect task state, yes for non-terminal states (M1); authorization inconsistency, M1 left `authorized_at` and a live grant on a cancelled task (benign).
- **Is the 6N cancel lock sufficient for its path?** Yes for cancel versus an in-flight check; it did not cover M1.
- **Safe to defer?** Yes. **Mandatory?** Recommended before extending monitoring, not mandatory.

**Known defect 9 (`ChatService.start_conversation_with_message`):** genuinely unreachable (one definition, no reference anywhere in the backend app, tests or frontend). Recommendation: delete it.

## 7. The HIGH Gmail grant

- **HIGH is justified.** Both Gmail reads are declared HIGH in `_declare_gmail_read`; `MAX_PERMITTED_RISK` is HIGH, so the grant is allowed, stored at the capability's own risk.
- **Creation:** only in `MonitorRequestService._confirm`, after explicit confirmation (one pinned production call site).
- **Owner and capability:** owner-scoped; `gmail_list_messages` only (`gmail_get_message` would need its own grant).
- **TTL:** to the deadline, at most 7 days. **Revocation:** on cancel and expiry (not completion or block: M2).
- **Re-validation:** every check re-binds the capability, calls `authorize_with_grants` and re-validates the arguments.

**Could a compromised or buggy monitoring task use it to read Gmail outside its intended scope?** Through current code, no: the only creator of Gmail tasks is 6N, the arguments are application-built, written once and re-validated on every run (one result, no bodies, `after:` the baseline), and Telegram carries no Gmail content. If something could write a step's arguments (database tampering, or a future second creator of Gmail tasks), a live grant would allow any query within the schema (at most 10 messages and 5 bodies per check). **Acceptable limitation now**; it becomes an architectural issue the moment a second production path can create `gmail_list_messages` tasks, at which point the grant should be bound to a task or to arguments.

## 8. The 40-check budget

- **Accounting is correct:** the deadline is `t_proposal + min(L, 40·i)`; checks start at confirmation and are at least one interval apart (reschedules clamped to the deadline; failure backoff `max(60·2ⁿ, interval)`; contention and waiting for approval don't spend; a runner error rolls its spend back).
- **No off-by-one:** check k can run only for k = 0…39, so at most 40 checks; the deadline check precedes the budget check, so `budget_exceeded` can't be reached first.
- **No loop:** expiry is terminal; a blocked or stuck task is unscheduled.
- **Truthful end time:** the proposal and the "Done" reply show the same deadline. The interval can't change (written once). Restart: the schedule is persisted and the lease recovers it.

## 9. Concurrency (PostgreSQL)

| Race | Result | Evidence |
|---|---|---|
| Two workers claim one task | Exactly one wins (conditional UPDATE on `next_run_at` and state) | `background/runtime.py` `claim_task`; 6F live tests |
| Check vs cancel during the Gmail read | Cancel waits, then answers "terminal" | 6N live check, plus a control failing without the lock |
| Cancel vs the first check (`mark_task_running`) | Stale write rejected; stays `cancelled`; no read | S1/S2 |
| Cancel vs block after failures | Rejected; no notification | S3 |
| Check vs expiry | Deadline checked first; in-flight reads only (L4) | Code |
| Duplicate confirmation | Exactly one wins; one grant | 6N live E |
| Confirmation vs replacement | **Lost update (M1)** | S4 |
| Duplicate notification | Unique `(task_id, kind, check_number)` | `tasks/models.py` |
| Delivery claim | 6M.1 durable claim; at-least-once window documented | 6M.1 |

**The experiments** (`mai_audit_6o`, stubbed Gmail, timing hooks in the experiment process only):
- **S1, S2:** a cancel committed between the runtime's first read and `mark_task_running`'s write. The stale write raised `IntegrityError` (`cancelled_requires_timestamp`) and the pass rolled back. Final state `cancelled`, 0 Gmail listings, 0 executions, 0 notifications; S2 had a second monitor's grant live and still showed no read.
- **S3:** a stale `transition(BLOCKED)` after a committed cancel returned `failed` / `persistence_failed`; the following `record_outcome` raised `MissingGreenlet` (L1). The task stayed `cancelled` with 0 notifications.
- **S4:** the confirm turn returned `scheduled` with "Done. I'm monitoring"; the concurrent replace turn returned `proposed`. The first task ended `cancelled` / `proposal_replaced` with 1 grant still live (M1).

## 10. Crash and restart

- Proposal, confirmation and scheduling are each one transaction: a crash leaves the previous state, and a lost "Done" reply means nothing committed.
- Runner: a crash after the claim commits leaves a lease (due again after 300 s); a crash during or after the Gmail read rolls the check back, so it re-reads later (read-only) and completes and notifies once; completion and its notification commit together.
- Delivery (pre-existing 6M.3 semantics, documented): a crash after the notification commits but before delivery leaves it in-app and never auto-delivered; a crash during the Telegram call leaves the record `sending`, never auto-retried, and a manual retry after the lease may duplicate.
- Nothing gets stuck except L5 (unreachable for monitors), and the user is never told something false at these boundaries.

## 11. Security

- **Owner spoofing / authentication bypass:** one seam, `get_owner_id`; no route takes an owner from the request (pinned).
- **Proposal and confirmation injection:** the grammar reads only the user's raw message; Telegram passes no owner; confirmation needs this owner's pending row in this conversation.
- **Substitution:** task ids are owner-scoped (404 for another owner); no grant id is ever accepted from a request.
- **Capability, runner and Gmail-tool bypass:** capability re-bound and checked against the monitorable allow-list; one runner, one runtime; two Gmail read operations, GET only, no raw query.
- **Spoofing:** notification writing has no external entry point; Telegram has its own secret and allowed chat.
- **Gmail content is data only:** compared by the evaluator, never sent to a model on the monitor path, kept out of the journal (lengths only), the logs and Telegram (`render_message` is built from kind, task id, check number and time). The sender in task metadata is reduced to a safe alphabet.

## 12. Truthfulness

Every monitor reply is application-written, and "I'm monitoring" existed in exactly one branch, built after the savepoint commits. Weak points: M3 (the model was held back only by the prompt) and M1 ("Done" could be undone by a concurrent turn).

## 13. Architectural duplication

None. At HEAD there was exactly one `AuthorizationService` and one caller of `authorize_with_grants` (the runner); one `ExecutionService` and Execution constructor; one `TaskRunner` and one `BackgroundRuntime`; one notification writer (`record_outcome`) and one delivery-record writer (`delivery/records.py`); one `SecureHttpClient` (the only `httpx` importer); one owner seam (`get_owner_id`). No scheduler imports in `app/tasks`; head migration `0019`. The tool and executable registries are the pre-existing declaration/implementation pair.

## 14. Test coverage gaps (reported, not added during the audit)

1. No test for M1 (confirmation vs concurrent discard).
2. No test proving the CHECK constraints reject stale writes against cancelled or completed tasks.
3. No committed PostgreSQL concurrency tests for 6N (the 6N live checks lived in the session scratchpad).
4. No test pinning grant behaviour on completion or block (M2).
5. No sequence test running a monitor to its natural deadline (40 checks with interleaved failures).
6. No monitoring claim channel in the truthfulness validator, and so no test for one (M3).
7. The `MissingGreenlet` path after a failed transition (L1) was untested.

## 15. Recommended next stage

A small hardening stage before extending monitoring: conditional state writes (Known defect 8, M1); grant release on completion and block; a monitoring claim channel (M3); a committed PostgreSQL race suite; delete the dead method (Known defect 9). Recommended, not mandatory.

## 16. Explicitly deferred issues

- Argument- or task-scoped grants (needed before any second creator of `gmail_list_messages` tasks).
- L5 stranded-task semantics.
- The 6M.3 delivery gaps after a crash (documented).
- Unifying the reminder and task inboxes.
- The webhook's own Telegram sender.
- The unexplained 6N shuffled failure: re-check whenever a run is interrupted, recording the seed first.

## 17. Files and components inspected

- `backend/app/background/runtime.py` (claim, advance, expire, reschedule, `_record_failure`)
- `backend/app/tasks/runner.py` (`_refuse_task`, `_check`, `_claim_check`, `_complete`, `mark_task_running`)
- `backend/app/tasks/service.py` (`authorization_valid`, `transition`, `cancel`, `get`)
- `backend/app/tasks/requests.py` (confirm, cancel, release, discard)
- `backend/app/tasks/models.py` (CHECK constraints, unique indexes), `backend/app/tasks/monitoring.py`, `backend/app/tasks/notifications.py`
- `backend/app/authorization/grants.py`, `backend/app/tools/authorization.py`, `backend/app/tools/policy.py`, `backend/app/tools/catalog.py`
- `backend/app/execution/service.py` (commit points), `backend/app/integrations/gmail_schemas.py`, `backend/app/integrations/google_gmail.py`
- `backend/app/delivery/contract.py`, `backend/app/delivery/status.py`, `backend/app/telegram/notifier.py`
- `backend/app/synthesis/execution_truth.py`, `backend/app/prompt/formatter.py`, `backend/app/services/chat_service.py`
- `backend/app/api/routes/tasks.py`, `conversations.py`, `telegram.py`
- The 6N tests and pins, mutation records and live-check scripts, and the CHANGELOG's known defects.

## 18. Evidence

The experiment outcomes are in §9. Code references: the CHECK constraints and unique notification index in `tasks/models.py`; the conditional completion and the deadline-before-authorization check in `tasks/runner.py`; the cancel lock, the `ceil` TTL and the release-only-on-cancel-and-expiry in `tasks/requests.py`; the missing monitoring channel in `synthesis/execution_truth.py`; the content-free Telegram text in `telegram/notifier.py`. Structural audit at HEAD: PASS, 0 mismatches.

---

### What Stage 6P did with these findings

Stage 6P ("harden monitoring concurrency and truthfulness") addressed M1, M2, M3, Known defect 8 (generally), L1, L2 and Known defect 9, and added the PostgreSQL race suite. See `docs/CHANGELOG.md`, Stage 6P, for the fixes and their verification. The remaining items in §16 stay deferred.
