"use client";

/**
 * The unread count on the Inbox tab.
 *
 * Two small reads (unread alerts, unseen reminders) once a minute while the
 * tab is visible, backed off on failure and stopped when the person has not
 * signed in. If a read fails the count is simply not shown: an unknown count
 * is never displayed as zero.
 */

import { useCallback, useEffect, useState } from "react";

import { reminders, taskNotifications } from "./api";
import { badgeCount } from "./inbox";
import { usePolling, type TickResult } from "./polling";

const POLL_MS = 60_000;
const LIMIT = 50;

export function useInboxBadge(enabled: boolean): { badge: string | null; refresh: () => Promise<TickResult> } {
  const [badge, setBadge] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<TickResult> => {
    try {
      const [alerts, fired] = await Promise.all([
        taskNotifications.list({ unreadOnly: true, limit: LIMIT }),
        reminders.fired(LIMIT).catch((error: { status?: number }) => {
          // Reminders switched off on the server (404) simply contribute nothing.
          if (error?.status === 404) return { notifications: [], total: 0 };
          throw error;
        }),
      ]);
      setBadge(badgeCount(alerts.notifications.length, alerts.next_before !== null, fired.total));
      return "ok";
    } catch (error) {
      setBadge(null);
      return (error as { code?: string })?.code === "authentication_cancelled" ? "stop" : "failed";
    }
  }, []);

  // One read now, then the slow loop.
  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);
  usePolling(refresh, POLL_MS, enabled);
  return { badge, refresh };
}
