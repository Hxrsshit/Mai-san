/**
 * Finding Mai's monitors through the task API.
 *
 * Since the backend reports a task's monitor metadata on `TaskRead`, the two
 * list calls (active and recently ended) are the whole load: no per-task
 * detail request is made to discover which tasks are monitors or what they
 * check. The journal (last check outcome, failures) is fetched only when a
 * person opens one monitor.
 *
 * A backend that does not send the field is not treated as "no monitors": the
 * tasks it listed are counted as `unreported` and the screen says so.
 */

import type { tasks as TasksApi } from "./api";
import { isMonitor, isTerminal, reportsMonitorMetadata, summariseMonitor, type MonitorSummary } from "./tasks";
import type { Task, TaskState } from "./types";

const ACTIVE: TaskState[] = [
  "proposed",
  "planned",
  "awaiting_approval",
  "queued",
  "running",
  "paused",
  "blocked",
];
const ENDED: TaskState[] = ["completed", "failed", "cancelled"];

export const ACTIVE_LIMIT = 50;
export const ENDED_LIMIT = 10;

export interface MonitorEntry {
  task: Task;
  summary: MonitorSummary;
}

export interface MonitorsResult {
  active: MonitorEntry[];
  ended: MonitorEntry[];
  /** More tasks exist than the two list calls returned. */
  truncated: boolean;
  /** Tasks the backend listed without monitor metadata: whether they are monitors is unknown. */
  unreported: number;
}

export async function loadMonitors(api: Pick<typeof TasksApi, "list">): Promise<MonitorsResult> {
  // Both calls are the whole authority on which tasks exist. If either fails
  // the load fails, so half an answer is never shown as complete.
  const [activeList, endedList] = await Promise.all([
    api.list(ACTIVE, ACTIVE_LIMIT),
    api.list(ENDED, ENDED_LIMIT),
  ]);
  const truncated =
    activeList.total > activeList.tasks.length || endedList.total > endedList.tasks.length;

  const active: MonitorEntry[] = [];
  const ended: MonitorEntry[] = [];
  let unreported = 0;
  for (const task of [...activeList.tasks, ...endedList.tasks]) {
    if (!reportsMonitorMetadata(task)) {
      unreported += 1;
      continue;
    }
    if (!isMonitor(task)) continue;
    const summary = summariseMonitor(task);
    if (!summary) continue;
    (isTerminal(task.state) ? ended : active).push({ task, summary });
  }
  return { active, ended, truncated, unreported };
}
