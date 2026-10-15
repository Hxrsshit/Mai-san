/**
 * Notifications and reminders: what is shown is what the backend recorded,
 * and "read" or "seen" changes only after the backend accepted the change.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AlertsSection, RemindersSection } from "../components/InboxView.tsx";
import { appendPage, badgeCount, deliveryText, kindText, markAlertRead, markReminderSeen } from "../lib/inbox.ts";
import type { Resource } from "../lib/resource.ts";
import type {
  ReminderList,
  ReminderNotificationList,
  TaskNotification,
  TaskNotificationList,
} from "../lib/types.ts";

const NOW = new Date("2026-10-13T10:30:00Z");
const noop = () => undefined;

class StatusError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string) {
    super("x");
    this.status = status;
    this.code = code;
  }
}

function alert(id: string, overrides: Partial<TaskNotification> = {}): TaskNotification {
  return {
    id,
    task_id: "00000000-0000-4000-8000-000000000001",
    kind: "condition_met",
    check_number: 3,
    created_at: "2026-10-13T10:00:00Z",
    read_at: null,
    deliveries: [],
    ...overrides,
  };
}

const ready = <T>(data: T): Resource<T> => ({ status: "ready", data, refreshing: false, refreshError: null });

function renderAlerts(resource: Resource<TaskNotificationList>, extra: Record<string, unknown> = {}) {
  return renderToStaticMarkup(
    createElement(AlertsSection, {
      resource,
      unreadOnly: false,
      onUnreadOnly: noop,
      onRetry: noop,
      onRefresh: noop,
      onMore: noop,
      loadingMore: false,
      moreError: null,
      onMarkRead: noop,
      marking: new Set<string>(),
      markErrors: {},
      now: NOW,
      ...extra,
    } as never),
  );
}

// --- Alerts ------------------------------------------------------------------------

test("alert kinds are described from the backend's closed set", () => {
  assert.match(kindText("condition_met").title, /condition was met/);
  assert.match(kindText("monitoring_failed").meaning, /no longer watching/);
  assert.match(kindText("mystery").meaning, /does not know/);
});

test("unread and read alerts are distinguished in words, not only colour", () => {
  const html = renderAlerts(ready({ notifications: [alert("a"), alert("b", { read_at: "2026-10-13T10:10:00Z" })], next_before: null }));
  assert.match(html, />Unread</);
  assert.match(html, />Read</);
  assert.equal((html.match(/Mark as read/g) ?? []).length, 1, "only the unread alert offers it");
});

test("delivery state uses the backend's own states and never claims more", () => {
  assert.equal(deliveryText({ adapter: "telegram", status: "delivered", attempts: 1, retryable: false }), "Telegram: delivered (1 attempt)");
  assert.equal(deliveryText({ adapter: "telegram", status: "failed", attempts: 2, retryable: true }), "Telegram: failed (2 attempts), can be retried");
  assert.equal(deliveryText({ adapter: "telegram", status: "sending", attempts: 1, retryable: false }), "Telegram: sending now");
  assert.match(deliveryText({ adapter: "telegram", status: "sending", attempts: 1, retryable: true }), /interrupted/);

  const none = renderAlerts(ready({ notifications: [alert("a")], next_before: null }));
  assert.match(none, /Not sent to any outside channel/);
  const failed = renderAlerts(ready({ notifications: [alert("a", { deliveries: [{ adapter: "telegram", status: "failed", attempts: 1, last_attempt_at: "2026-10-13T10:00:00Z", delivered_at: null, retryable: true }] })], next_before: null }));
  assert.match(failed, /Telegram: failed/);
  assert.ok(!failed.includes("Not sent to any outside channel"));
});

test("an alert carries no text of its own, so none is invented", () => {
  const html = renderAlerts(ready({ notifications: [alert("a")], next_before: null }));
  assert.match(html, /Check 3/);
  assert.match(html, /A monitor&#x27;s condition was met|A monitor’s condition was met|condition was met/);
});

test("screen states: loading, error with retry, empty, empty when filtered", () => {
  assert.match(renderAlerts({ status: "loading" }), /Loading alerts/);
  const error = renderAlerts({ status: "error", error: new StatusError(500, "http_error") });
  assert.match(error, /Alerts could not be loaded/);
  assert.match(error, />Try again</);
  assert.match(renderAlerts(ready({ notifications: [], next_before: null })), /No alerts yet/);
  assert.match(renderAlerts(ready({ notifications: [], next_before: null }), { unreadOnly: true }), /No unread alerts/);
});

test("a failed refresh keeps the earlier alerts, marked possibly stale", () => {
  const html = renderAlerts({ status: "ready", data: { notifications: [alert("a")], next_before: null }, refreshing: false, refreshError: new StatusError(0, "network_error") });
  assert.match(html, /could not be refreshed/);
  assert.match(html, /may be out of date/);
  assert.match(html, />Unread</);
});

test("older alerts are offered only when the backend gave a cursor", () => {
  assert.ok(!renderAlerts(ready({ notifications: [alert("a")], next_before: null })).includes("Load older alerts"));
  assert.match(renderAlerts(ready({ notifications: [alert("a")], next_before: "cursor" })), /Load older alerts/);
  assert.match(renderAlerts(ready({ notifications: [alert("a")], next_before: "cursor" }), { loadingMore: true }), /disabled=""[^>]*>Loading…/);
});

test("pages append without duplicates", () => {
  const merged = appendPage([alert("a"), alert("b")], [alert("b"), alert("c")]);
  assert.deepEqual(merged.map((n) => n.id), ["a", "b", "c"]);
});

test("a mark-read in flight disables its button; a failure is announced beside it", () => {
  const html = renderAlerts(ready({ notifications: [alert("a")], next_before: null }), { marking: new Set(["a"]), markErrors: { a: "Not marked as read: The server hit an unexpected error." } });
  assert.match(html, /disabled=""[^>]*>Marking…/);
  assert.match(html, /role="alert"[^>]*>Not marked as read/);
});

// --- Marking read -------------------------------------------------------------------

test("an alert counts as read only after the backend accepted it, with the stored time", async () => {
  const calls: string[] = [];
  const stored = alert("a", { read_at: "2026-10-13T10:20:00Z" });
  const outcome = await markAlertRead(
    {
      markRead: async (id) => { calls.push(`mark ${id}`); },
      get: async (id) => { calls.push(`get ${id}`); return stored; },
    },
    alert("a"),
  );
  assert.deepEqual(calls, ["mark a", "get a"]);
  assert.deepEqual(outcome, { kind: "read", notification: stored });
});

test("a failed write leaves the alert unread", async () => {
  const outcome = await markAlertRead(
    { markRead: async () => { throw new StatusError(500, "http_error"); }, get: async () => { throw new Error("must not be called"); } },
    alert("a"),
  );
  assert.equal(outcome.kind, "failed");
});

test("a missing alert is reported as gone", async () => {
  const outcome = await markAlertRead(
    { markRead: async () => { throw new StatusError(404, "not_found"); }, get: async () => { throw new Error("must not be called"); } },
    alert("a"),
  );
  assert.equal(outcome.kind, "gone");
});

test("if only the read-back fails, the confirmed write still counts", async () => {
  const outcome = await markAlertRead(
    { markRead: async () => undefined, get: async () => { throw new StatusError(0, "network_error"); } },
    alert("a"),
    () => new Date("2026-10-13T10:21:00Z"),
  );
  assert.equal(outcome.kind, "read");
  assert.equal((outcome as { notification: TaskNotification }).notification.read_at, "2026-10-13T10:21:00.000Z");
});

test("reminders are seen only on a confirmed write", async () => {
  assert.deepEqual(await markReminderSeen({ markSeen: async () => undefined }, "r"), { kind: "seen" });
  assert.equal((await markReminderSeen({ markSeen: async () => { throw new StatusError(404, "not_found"); } }, "r")).kind, "gone");
  assert.equal((await markReminderSeen({ markSeen: async () => { throw new StatusError(0, "network_error"); } }, "r")).kind, "failed");
});

test("the unread badge is empty when zero and marks overflow", () => {
  assert.equal(badgeCount(0, false, 0), null);
  assert.equal(badgeCount(2, false, 1), "3");
  assert.equal(badgeCount(50, true, 0), "50+");
});

// --- Reminders ---------------------------------------------------------------------

const scheduledReady: Resource<ReminderList> = ready({
  reminders: [
    {
      id: "r1",
      text: "Call the dentist <b>now</b>",
      state: "scheduled",
      recurrence: "weekly",
      next_run_at: "2026-10-14T09:00:00Z",
      timezone_name: "Europe/London",
      fire_count: 2,
      created_at: "2026-10-01T09:00:00Z",
    },
  ],
  total: 1,
});

const firedReady: Resource<ReminderNotificationList> = ready({
  notifications: [
    { id: "n1", reminder_id: "r1", text: "Take medicine", due_at: "2026-10-13T08:00:00Z", delivered_at: "2026-10-13T08:00:05Z" },
  ],
  total: 1,
});

function renderReminders(over: Record<string, unknown> = {}) {
  return renderToStaticMarkup(
    createElement(RemindersSection, {
      scheduled: scheduledReady,
      fired: firedReady,
      onRetryScheduled: noop,
      onRetryFired: noop,
      onRefresh: noop,
      seen: new Set<string>(),
      marking: new Set<string>(),
      markErrors: {},
      onMarkSeen: noop,
      now: NOW,
      ...over,
    } as never),
  );
}

test("scheduled and fired reminders render with the backend's facts", () => {
  const html = renderReminders();
  assert.match(html, /Take medicine/);
  assert.match(html, />Fired</);
  assert.match(html, /Mark as seen/);
  assert.match(html, />Scheduled</);
  assert.match(html, /Every week/);
  assert.match(html, /fired 2 times/);
});

test("reminder text is escaped, never rendered as markup", () => {
  const html = renderReminders();
  assert.ok(html.includes("Call the dentist &lt;b&gt;now&lt;/b&gt;"));
  assert.ok(!html.includes("<b>now"));
});

test("a fired reminder is described as an in-app record, not as seen or delivered to a person", () => {
  assert.match(renderReminders(), /in-app record, not proof it was seen/);
});

test("a marked-seen reminder shows Seen and offers no second action", () => {
  const html = renderReminders({ seen: new Set(["n1"]) });
  assert.match(html, />Seen</);
  assert.ok(!html.includes("Mark as seen"));
});

test("the screen says what the server cannot list instead of implying a full history", () => {
  assert.match(renderReminders(), /cannot be shown/);
  assert.match(renderReminders(), /cancelled, completed or failed/);
});

test("reminders switched off on the server are reported plainly", () => {
  const html = renderReminders({ fired: { status: "error", error: new StatusError(404, "not_found") }, scheduled: { status: "error", error: new StatusError(404, "not_found") } });
  assert.match(html, /Reminders are not available on this server/);
  assert.ok(!html.includes("Try again"));
});

test("reminder loading, error and empty states", () => {
  assert.match(renderReminders({ fired: { status: "loading" }, scheduled: { status: "loading" } }), /Loading fired reminders/);
  const failed = renderReminders({ fired: { status: "error", error: new StatusError(500, "http_error") } });
  assert.match(failed, /Fired reminders could not be loaded/);
  assert.match(failed, />Try again</);
  const empty = renderReminders({ fired: ready({ notifications: [], total: 0 }), scheduled: ready({ reminders: [], total: 0 }) });
  assert.match(empty, /Nothing waiting/);
  assert.match(empty, /No scheduled reminders/);
});
