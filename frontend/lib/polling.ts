"use client";

/**
 * Gentle background refresh.
 *
 * One timer chain per caller (never an interval, so a slow request cannot
 * pile up), paused while the tab is hidden, resumed with an immediate refresh
 * when it becomes visible, and backed off exponentially while requests fail.
 * It stops entirely when `tick` reports that waiting is pointless (the person
 * has not signed in).
 */

import { useEffect, useRef } from "react";

export const MAX_BACKOFF_MS = 300_000;

/** The wait before the next refresh, doubling per consecutive failure. */
export function nextDelay(baseMs: number, failures: number, maxMs = MAX_BACKOFF_MS): number {
  const factor = 2 ** Math.max(0, Math.min(failures, 16));
  return Math.min(baseMs * factor, maxMs);
}

/** What a refresh tells the poller: carry on, back off, or stop. */
export type TickResult = "ok" | "failed" | "stop";

export function usePolling(tick: () => Promise<TickResult>, baseMs: number, enabled: boolean): void {
  const tickRef = useRef(tick);
  tickRef.current = tick;

  useEffect(() => {
    if (!enabled || typeof document === "undefined") return;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    let waitingForVisible = false;

    const schedule = () => {
      if (stopped) return;
      timer = setTimeout(run, nextDelay(baseMs, failures));
    };

    const run = async () => {
      timer = null;
      if (document.hidden) {
        waitingForVisible = true;
        return;
      }
      const result = await tickRef.current();
      if (stopped || result === "stop") return;
      failures = result === "ok" ? 0 : failures + 1;
      schedule();
    };

    const onVisible = () => {
      if (!document.hidden && waitingForVisible && !stopped) {
        waitingForVisible = false;
        void run();
      }
    };

    document.addEventListener("visibilitychange", onVisible);
    schedule();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [baseMs, enabled]);
}
