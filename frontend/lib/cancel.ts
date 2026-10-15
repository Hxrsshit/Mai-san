/**
 * Stopping a task, without ever claiming it stopped before the backend did.
 *
 * The result is decided only by the backend's reply. "Cancelled" requires a
 * 200 whose task state is `cancelled`. A network failure is reported as
 * *unknown* -- the request may or may not have been applied -- and the caller
 * is told to re-read the task rather than assume either outcome.
 */

import type { tasks as TasksApi } from "./api";
import { describeError } from "./errors";
import type { Task } from "./types";

export type CancelOutcome =
  | { kind: "cancelled"; task: Task }
  | { kind: "already_finished"; message: string }
  | { kind: "not_found"; message: string }
  | { kind: "unknown"; message: string }
  | { kind: "failed"; message: string };

export async function cancelTask(
  api: Pick<typeof TasksApi, "cancel">,
  taskId: string,
): Promise<CancelOutcome> {
  try {
    const task = await api.cancel(taskId);
    if (task && task.state === "cancelled") return { kind: "cancelled", task };
    // A 200 that does not say "cancelled" proves nothing about stopping.
    return {
      kind: "failed",
      message: "The server answered, but did not report the task as cancelled. Refresh to see its real state.",
    };
  } catch (caught) {
    const error = caught as { code?: string; status?: number };
    const view = describeError(caught);
    if (error.status === 409) return { kind: "already_finished", message: view.message };
    if (error.status === 404) return { kind: "not_found", message: view.message };
    if (error.code === "network_error" || error.status === 0) {
      return {
        kind: "unknown",
        message:
          "Could not reach the backend, so it is not known whether the task was stopped. Refresh to see its real state.",
      };
    }
    return { kind: "failed", message: view.message };
  }
}
