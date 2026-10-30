/**
 * How tasks and monitors are presented.
 *
 * Everything here maps what the backend said onto words. Nothing invents a
 * status: each label is keyed by a backend state, an unknown state is shown
 * as the backend sent it, and a task is only ever "finished" because the
 * backend's own state says so.
 */

import { formatInterval } from "./format";
import type { Task, TaskEvent, TaskState } from "./types";

export type Tone = "active" | "waiting" | "success" | "error" | "neutral";

export const TASK_STATES: TaskState[] = [
  "proposed",
  "planned",
  "awaiting_approval",
  "queued",
  "running",
  "paused",
  "blocked",
  "completed",
  "failed",
  "cancelled",
];

const STATE_VIEW: Record<TaskState, { label: string; tone: Tone; meaning: string }> = {
  proposed: { label: "Proposed", tone: "neutral", meaning: "Created; nothing has been planned yet." },
  planned: { label: "Planned", tone: "neutral", meaning: "A plan is attached but it has not been approved to run." },
  awaiting_approval: { label: "Awaiting approval", tone: "waiting", meaning: "Waiting for a person to approve it." },
  queued: { label: "Queued", tone: "active", meaning: "Approved and waiting for the background runner." },
  // "Running" is the backend's state, not a statement about this moment: a
  // monitor stays in it between checks, so it must not read as live work.
  running: {
    label: "Running",
    tone: "active",
    meaning:
      "The runner has started on it. This state does not show that work is happening right now: a monitor stays here between checks.",
  },
  paused: { label: "Paused", tone: "waiting", meaning: "Stopped on request; it can be resumed." },
  blocked: { label: "Blocked", tone: "waiting", meaning: "Stopped by circumstance, such as repeated failures or a missing connection." },
  completed: { label: "Completed", tone: "success", meaning: "The backend recorded it as completed." },
  failed: { label: "Failed", tone: "error", meaning: "It stopped with an error." },
  cancelled: { label: "Cancelled", tone: "neutral", meaning: "It was cancelled and will not run again." },
};

/** The backend's terminal states (`TERMINAL_STATES`). The server still decides. */
const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "cancelled"]);

export function isTerminal(state: string): boolean {
  return TERMINAL.has(state);
}

export function stateView(state: string): { label: string; tone: Tone; meaning: string } {
  return (
    STATE_VIEW[state as TaskState] ?? {
      label: state,
      tone: "neutral",
      meaning: "The server reported a state this page does not know.",
    }
  );
}

/** Application reason codes (never exception text) the person can act on. */
const REASONS: Record<string, string> = {
  monitor_expired: "The monitoring window ended, so Mai stopped it.",
  monitor_ended: "The monitor ended.",
  failures_exhausted: "It failed three times in a row, so Mai stopped retrying.",
  authorization_expired: "Its permission to run expired.",
  task_cancelled: "It was cancelled.",
  task_failed: "It stopped with an error.",
  runner_error: "The runner hit an unexpected error.",
  persistence_failed: "Mai could not save progress.",
  awaiting_human_approval: "It is waiting for a person to approve it.",
  plan_not_authorized: "Its plan was not approved to run.",
  dependencies_incomplete: "An earlier step has not finished.",
  state_changed_concurrently: "It changed while Mai was working on it.",
  execution_disabled: "Running actions is switched off on this server.",
  approval_required: "The action needs approval first.",
  not_authorized: "Mai was not permitted to take the action.",
  integration_not_configured: "The service it needed is not configured.",
  credentials_missing: "Mai is not connected to the service it needed.",
  credentials_expired: "Mai's access to the service it needed has expired.",
  credential_scope_insufficient: "Mai's access to the service does not cover what it needed.",
  provider_unauthorized: "The service rejected Mai's access.",
  provider_forbidden: "The service refused the request.",
  provider_timeout: "The service it needed timed out.",
  provider_unavailable: "The service it needed was unavailable.",
  provider_rate_limited: "The service it needed asked Mai to slow down.",
  provider_invalid_response: "The service sent a reply Mai could not use.",
};

export function reasonText(code: string | null | undefined): string | null {
  if (!code) return null;
  return REASONS[code] ?? null;
}

/**
 * The timestamp that says when a task ended, and what it is called. A failed
 * task has no end time of its own, so its last update is shown as that.
 */
export function endMoment(task: Task): { label: string; at: string } | null {
  if (task.completed_at) return { label: "Completed", at: task.completed_at };
  if (task.cancelled_at) return { label: "Cancelled", at: task.cancelled_at };
  if (task.state === "failed") return { label: "Last updated", at: task.updated_at };
  return null;
}

// --- Journal ---------------------------------------------------------------------

const EVENT_LABELS: Record<string, string> = {
  task_created: "Task created",
  plan_attached: "Plan attached",
  state_changed: "State changed",
  task_blocked: "Task blocked",
  task_cancelled: "Task cancelled",
  task_failed: "Task failed",
  task_completed: "Task completed",
  approval_requested: "Approval requested",
  approval_granted: "Approval granted",
  background_scheduled: "Scheduled to run in the background",
  background_claimed: "Picked up by the background runner",
  background_unscheduled: "Removed from the background schedule",
  monitoring_configured: "Monitoring configured",
  monitoring_check_started: "Check started",
  observation_recorded: "Check finished",
  monitoring_triggered: "Condition met",
  monitoring_check_failed: "Check failed",
  notification_created: "Notification recorded",
  execution_created: "Action recorded",
  runner_blocked: "Runner blocked",
  runner_refused: "Runner refused",
  step_started: "Step started",
  step_completed: "Step completed",
  step_failed: "Step failed",
  budget_exceeded: "Budget exceeded",
};

/** The only metadata keys ever rendered. Everything else in a journal entry is dropped. */
const SAFE_KEYS = [
  "check",
  "result",
  "kind",
  "operator",
  "interval_seconds",
  "observed",
  "observed_chars",
  "reason",
  "outcome",
  "state",
  "failure_count",
] as const;

function safeValue(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "yes" : "no";
  // Short, plain tokens only: reason codes and enum values. Free text never passes.
  if (typeof value === "string" && /^[A-Za-z0-9_.:-]{1,48}$/.test(value)) return value;
  return null;
}

export interface EventLine {
  id: string;
  sequence: number;
  at: string;
  label: string;
  details: string[];
}

export function describeEvent(event: TaskEvent): EventLine {
  const details: string[] = [];
  for (const key of SAFE_KEYS) {
    const value = safeValue(event.metadata?.[key]);
    if (value === null) continue;
    if (key === "interval_seconds") details.push(formatInterval(Number(event.metadata[key])));
    else if (key === "reason") details.push(reasonText(value) ?? `reason: ${value}`);
    else details.push(`${key.replace(/_/g, " ")}: ${value}`);
  }
  return {
    id: event.id,
    sequence: event.sequence,
    at: event.occurred_at,
    label: EVENT_LABELS[event.event_type] ?? event.event_type.replace(/_/g, " "),
    details,
  };
}

// --- Monitors --------------------------------------------------------------------

/**
 * When the next check happens, in what the backend can actually support.
 * `due` carries the stored `next_run_at`; every other kind says why there is
 * no time to show.
 */
export type NextCheck =
  | { kind: "due"; at: string }
  | { kind: "none"; reason: string };

export interface MonitorSummary {
  /** What the monitor looks for, from the stored condition's shape. */
  checks: string;
  interval: string;
  /**
   * Checks run so far (`check_count`), or null if the backend does not say.
   * The backend commits the count together with each check's outcome, so it
   * covers checks that finished or failed, not one still in flight.
   */
  checksRun: number | null;
  next: NextCheck;
  /** From the task's own state: still being watched only while non-terminal. */
  active: boolean;
  deadline: string | null;
  /** The stored spec could not be read, so `checks` and `interval` are unknown. */
  unreadable: boolean;
}

const KIND_TEXT: Record<string, string> = {
  count: "a count of matching items",
  value: "a single value",
  contains: "whether some text appears",
};

const OPERATOR_TEXT: Record<string, string> = {
  eq: "equals",
  ne: "differs from",
  gt: "is above",
  gte: "is at least",
  lt: "is below",
  lte: "is at most",
  contains: "contains",
};

/**
 * The identity of one version of a task. Used as a React `key` on anything
 * loaded from a task's detail: when the task changes (the backend bumps
 * `updated_at` on every write to it, including each check) the key changes,
 * the panel is replaced and its detail is read again; while the task is
 * unchanged the key is stable and nothing is fetched.
 */
export function detailKey(task: Pick<Task, "id" | "updated_at">): string {
  return `${task.id}:${task.updated_at}`;
}

/** True when the backend says this task is a monitor. */
export function isMonitor(task: Pick<Task, "monitor">): boolean {
  return task.monitor !== null && task.monitor !== undefined;
}

/** True when this backend sent monitor metadata for the task at all. */
export function reportsMonitorMetadata(task: Pick<Task, "monitor">): boolean {
  return task.monitor !== undefined;
}

/** States the background runtime polls. A schedule in any other state is not acted on. */
const SCHEDULED_STATES: ReadonlySet<string> = new Set(["queued", "running"]);

export function nextCheck(task: Pick<Task, "state" | "next_run_at">): NextCheck {
  if (isTerminal(task.state)) {
    return { kind: "none", reason: "No further checks: the monitor has ended." };
  }
  if (!SCHEDULED_STATES.has(task.state)) {
    return { kind: "none", reason: `Not scheduled while ${stateView(task.state).label.toLowerCase()}.` };
  }
  if (task.next_run_at === null || task.next_run_at === undefined) {
    return { kind: "none", reason: "Not scheduled: no check is queued." };
  }
  return { kind: "due", at: task.next_run_at };
}

export function summariseMonitor(task: Task): MonitorSummary | null {
  const meta = task.monitor;
  if (meta === null || meta === undefined) return null;

  const unreadable = meta.kind === null && meta.operator === null && meta.interval_seconds === null;
  const kind = meta.kind ?? "";
  const operator = meta.operator ?? "";
  const checks = unreadable
    ? "The stored condition could not be read."
    : `Compares ${KIND_TEXT[kind] ?? "an observation"} — it ${
        OPERATOR_TEXT[operator] ?? "matches"
      } a target set when the monitor was created.`;

  return {
    checks,
    interval: formatInterval(meta.interval_seconds),
    checksRun: typeof task.check_count === "number" ? task.check_count : null,
    next: nextCheck(task),
    active: !isTerminal(task.state),
    deadline: task.deadline,
    unreadable,
  };
}

// --- What the journal adds ------------------------------------------------------

const CHECK_RESULT: Record<string, string> = {
  satisfied: "Condition met",
  not_satisfied: "Condition not met yet",
  unable: "Could not be evaluated",
};

export interface JournalChecks {
  /** The latest check event, by sequence. */
  last: { at: string; outcome: string } | null;
  finished: number;
  failed: number;
}

/**
 * The last check and the counts of finished and failed checks. These exist
 * only in the task journal (`GET /api/tasks/{id}`), which is why they load
 * when a person asks for them and not with the list.
 */
export function summariseChecks(events: TaskEvent[]): JournalChecks {
  let last: JournalChecks["last"] = null;
  let finished = 0;
  let failed = 0;
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    if (event.event_type === "observation_recorded") {
      finished += 1;
      const result = safeValue(event.metadata?.result) ?? "";
      last = { at: event.occurred_at, outcome: CHECK_RESULT[result] ?? "Check finished" };
    } else if (event.event_type === "monitoring_check_failed") {
      finished += 1;
      failed += 1;
      const reason = safeValue(event.metadata?.reason);
      last = {
        at: event.occurred_at,
        outcome: `Check failed${reason ? ` (${reasonText(reason) ?? reason})` : ""}`,
      };
    } else if (event.event_type === "monitoring_triggered") {
      last = { at: event.occurred_at, outcome: "Condition met" };
    }
  }
  return { last, finished, failed };
}

/**
 * Whether a monitor is still being watched, in words that do not overclaim.
 * Only the backend's own state decides; "blocked" and "paused" are not watching.
 */
export function watchingText(task: Pick<Task, "state">): string {
  switch (task.state) {
    case "queued":
    case "running":
      // The state says it is scheduled; whether the runner is working is not
      // something this page can see, so it is not claimed.
      return "Active: scheduled for checks.";
    case "paused":
    case "blocked":
      return `Not checking right now (${stateView(task.state).label.toLowerCase()}).`;
    case "awaiting_approval":
    case "proposed":
    case "planned":
      return "Not started: it has not been approved to run.";
    case "completed":
      return "Finished: the condition was met and monitoring stopped.";
    case "failed":
      return "Stopped: it failed and is no longer checking.";
    case "cancelled":
      return "Stopped: it was cancelled and is no longer checking.";
    default:
      return "The server reported a state this page does not know.";
  }
}
