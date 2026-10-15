/**
 * Notification and reminder logic that is not presentation.
 *
 * A notification is "read" only when the backend says so: after a mark-read
 * succeeds the notification is read again, and the stored `read_at` is what
 * is shown, never an assumption.
 */

import type { reminders as RemindersApi, taskNotifications as NotificationsApi } from "./api";
import type { TaskNotification } from "./types";

export const KIND_TEXT: Record<string, { title: string; meaning: string }> = {
  condition_met: {
    title: "A monitor's condition was met",
    meaning: "A check found what the monitor was watching for, and the monitor stopped.",
  },
  monitoring_failed: {
    title: "A monitor stopped after failing",
    meaning: "Its checks kept failing, so Mai stopped it. It is no longer watching.",
  },
};

export function kindText(kind: string): { title: string; meaning: string } {
  return (
    KIND_TEXT[kind] ?? { title: "Monitoring update", meaning: "The server reported an event this page does not know." }
  );
}

/** What each channel's delivery state means, using the backend's own words. */
export function deliveryText(state: {
  adapter: string;
  status: string;
  attempts: number;
  retryable: boolean;
}): string {
  const name = state.adapter.charAt(0).toUpperCase() + state.adapter.slice(1);
  const tries = `${state.attempts} attempt${state.attempts === 1 ? "" : "s"}`;
  switch (state.status) {
    case "delivered":
      return `${name}: delivered (${tries})`;
    case "failed":
      return `${name}: failed (${tries})${state.retryable ? ", can be retried" : ""}`;
    case "sending":
      return state.retryable
        ? `${name}: interrupted while sending (${tries})`
        : `${name}: sending now`;
    default:
      return `${name}: ${state.status}`;
  }
}

/** Append an older page, skipping anything already shown. */
export function appendPage(
  current: TaskNotification[],
  page: TaskNotification[],
): TaskNotification[] {
  const seen = new Set(current.map((n) => n.id));
  return [...current, ...page.filter((n) => !seen.has(n.id))];
}

export type MarkReadOutcome =
  | { kind: "read"; notification: TaskNotification }
  | { kind: "gone" }
  | { kind: "failed"; error: unknown };

/**
 * Mark one alert read, then read it back. The 204 alone is not trusted for
 * what to show: the returned `read_at` is. If the write worked but the
 * read-back failed, the optimistic fallback is the current time, flagged
 * through `kind: "read"` only after the write was confirmed.
 */
export async function markAlertRead(
  api: Pick<typeof NotificationsApi, "markRead" | "get">,
  notification: TaskNotification,
  now: () => Date = () => new Date(),
): Promise<MarkReadOutcome> {
  try {
    await api.markRead(notification.id);
  } catch (error) {
    if ((error as { status?: number }).status === 404) return { kind: "gone" };
    return { kind: "failed", error };
  }
  try {
    return { kind: "read", notification: await api.get(notification.id) };
  } catch {
    // The write was confirmed (204); only the read-back failed.
    return { kind: "read", notification: { ...notification, read_at: now().toISOString() } };
  }
}

export type SeenOutcome = { kind: "seen" } | { kind: "gone" } | { kind: "failed"; error: unknown };

export async function markReminderSeen(
  api: Pick<typeof RemindersApi, "markSeen">,
  id: string,
): Promise<SeenOutcome> {
  try {
    await api.markSeen(id);
    return { kind: "seen" };
  } catch (error) {
    if ((error as { status?: number }).status === 404) return { kind: "gone" };
    return { kind: "failed", error };
  }
}

/** The number to show on the Inbox tab; null when it is not known. */
export function badgeCount(unreadAlerts: number, hasMoreAlerts: boolean, pendingReminders: number): string | null {
  const total = unreadAlerts + pendingReminders;
  if (total <= 0) return null;
  return hasMoreAlerts ? `${total}+` : String(total);
}
