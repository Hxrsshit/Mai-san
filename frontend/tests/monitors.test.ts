/**
 * Monitor discovery and cancellation. Cancellation is the one write in this
 * phase, so the rule under test is: nothing says "stopped" before the backend
 * does, and a lost reply is reported as unknown, not as success.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { CancelControlView } from "../components/CancelControl.tsx";
import { MonitorJournal, MonitorsViewBody } from "../components/MonitorsView.tsx";
import { cancelTask } from "../lib/cancel.ts";
import { loadMonitors } from "../lib/monitors.ts";
import { detailKey } from "../lib/tasks.ts";
import type { Task, TaskList, TaskState } from "../lib/types.ts";
import { fakeMonitorDetail, fakeMonitorTask, fakeTask } from "./support/harness.ts";

const ID = "00000000-0000-4000-8000-000000000001";
const NOW = new Date("2026-10-13T10:30:00Z");
const noop = () => undefined;

class StatusError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message = "x") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// --- cancelTask ---------------------------------------------------------------------

test("cancelled means the backend returned the task as cancelled", async () => {
  const outcome = await cancelTask({ cancel: async () => fakeTask({ state: "cancelled" }) }, ID);
  assert.equal(outcome.kind, "cancelled");
});

test("a 200 that does not say cancelled is not treated as cancelled", async () => {
  const outcome = await cancelTask({ cancel: async () => fakeTask({ state: "running" }) }, ID);
  assert.equal(outcome.kind, "failed");
  assert.match((outcome as { message: string }).message, /did not report the task as cancelled/);
});

test("409 means it had already finished; 404 means it is gone", async () => {
  const finished = await cancelTask({ cancel: async () => { throw new StatusError(409, "http_error", "That task has already finished, so it can no longer be changed."); } }, ID);
  assert.equal(finished.kind, "already_finished");
  const gone = await cancelTask({ cancel: async () => { throw new StatusError(404, "not_found", "That task no longer exists."); } }, ID);
  assert.equal(gone.kind, "not_found");
});

test("a lost reply is unknown, never success", async () => {
  const outcome = await cancelTask({ cancel: async () => { throw new StatusError(0, "network_error"); } }, ID);
  assert.equal(outcome.kind, "unknown");
  assert.match((outcome as { message: string }).message, /not known whether the task was stopped/);
});

test("a server error is a failure that says the task may still be running", async () => {
  const outcome = await cancelTask({ cancel: async () => { throw new StatusError(500, "http_error", "The task was not cancelled: the server could not save the change. It may still be running."); } }, ID);
  assert.equal(outcome.kind, "failed");
  assert.match((outcome as { message: string }).message, /may still be running/);
});

// --- The control --------------------------------------------------------------------

const view = (props: Partial<Parameters<typeof CancelControlView>[0]>) =>
  renderToStaticMarkup(
    createElement(CancelControlView, {
      phase: "idle",
      outcome: null,
      label: "Stop monitoring",
      onAsk: noop,
      onConfirm: noop,
      onKeep: noop,
      ...props,
    }),
  );

test("stopping needs a confirmation, with the safe choice focused first", () => {
  const idle = view({});
  assert.match(idle, />Stop monitoring</);
  assert.ok(!idle.includes("Yes, stop it"));

  const confirming = view({ phase: "confirming" });
  assert.match(confirming, /role="group"/);
  assert.match(confirming, /Yes, stop it/);
  // `autoFocus` is applied by React on the safe button, listed before the destructive one.
  assert.ok(confirming.indexOf("Keep it running") < confirming.indexOf("Yes, stop it"));
});

test("while the request is in flight the control is disabled and says so", () => {
  const html = view({ phase: "stopping" });
  assert.match(html, /disabled=""/);
  assert.match(html, /Stopping…/);
  assert.ok(!html.includes("Stopped."));
});

test("success is announced politely; problems are announced as alerts", () => {
  const stopped = view({ outcome: { kind: "cancelled", task: fakeTask({ state: "cancelled" }) } });
  assert.match(stopped, /role="status"/);
  assert.match(stopped, /server confirmed the task is cancelled/);
  assert.match(stopped, /disabled=""/); // cannot be asked twice

  const unknown = view({ outcome: { kind: "unknown", message: "Could not reach the backend, so it is not known whether the task was stopped." } });
  assert.match(unknown, /role="alert"/);
  assert.ok(!unknown.includes("Stopped."));
});

// --- Discovery: from the list alone ---------------------------------------------------

function fakeApi(config: { active?: Task[]; ended?: Task[]; activeTotal?: number }) {
  const calls = { list: [] as Array<TaskState[] | undefined>, detail: 0 };
  return {
    calls,
    api: {
      list: async (states?: TaskState[]): Promise<TaskList> => {
        calls.list.push(states);
        const ended = states?.includes("completed");
        const tasks = (ended ? config.ended : config.active) ?? [];
        return { tasks, total: ended ? tasks.length : (config.activeTotal ?? tasks.length) };
      },
      // Present so a stray call is counted; the loader is not given it at all.
      get: async () => {
        calls.detail += 1;
        throw new Error("a detail must not be requested");
      },
    },
  };
}

test("monitors are found from the list, with no per-task detail request", async () => {
  const { api, calls } = fakeApi({
    active: [
      fakeMonitorTask({ id: "m1", state: "queued" }),
      fakeTask({ id: "p1", state: "queued", objective: "Not a monitor" }),
      fakeMonitorTask({ id: "m2", state: "blocked", next_run_at: null }),
    ],
    ended: [fakeMonitorTask({ id: "m3", state: "completed", completed_at: "2026-10-13T11:00:00Z", next_run_at: null })],
  });
  const result = await loadMonitors(api);
  assert.deepEqual(result.active.map((e) => e.task.id), ["m1", "m2"]);
  assert.deepEqual(result.ended.map((e) => e.task.id), ["m3"]);
  assert.equal(result.unreported, 0);
  assert.equal(calls.list.length, 2, "exactly the two list calls");
  assert.equal(calls.detail, 0);
});

test("the loader is handed no way to fetch a detail", async () => {
  // Typed as list-only: a regression that reintroduced the N+1 would not compile,
  // and at run time `get` does not exist on what it receives.
  const received: string[] = [];
  const listOnly = { list: async (): Promise<TaskList> => { received.push("list"); return { tasks: [], total: 0 }; } };
  await loadMonitors(listOnly);
  assert.deepEqual(received, ["list", "list"]);
});

test("each monitor's facts survive the trip from the row to the summary", async () => {
  const { api } = fakeApi({ active: [fakeMonitorTask({ id: "m1", check_count: 5, next_run_at: "2026-10-13T10:50:00Z" })] });
  const [entry] = (await loadMonitors(api)).active;
  assert.equal(entry.summary.checksRun, 5);
  assert.deepEqual(entry.summary.next, { kind: "due", at: "2026-10-13T10:50:00Z" });
  assert.equal(entry.summary.interval, "every 15 minutes");
});

test("tasks from a backend that does not report monitors are counted, not assumed to be plain tasks", async () => {
  const older = fakeTask({ id: "x1" });
  delete (older as { monitor?: unknown }).monitor;
  const { api } = fakeApi({ active: [older, fakeMonitorTask({ id: "m1" })] });
  const result = await loadMonitors(api);
  assert.equal(result.unreported, 1);
  assert.deepEqual(result.active.map((e) => e.task.id), ["m1"]);
});

test("a failed list call fails the whole load instead of showing half an answer", async () => {
  const api = { list: async () => { throw new StatusError(0, "network_error"); } };
  await assert.rejects(() => loadMonitors(api as never));
  let calls = 0;
  const half = { list: async (states?: TaskState[]) => { calls += 1; if (states?.includes("completed")) throw new StatusError(500, "http_error"); return { tasks: [], total: 0 }; } };
  await assert.rejects(() => loadMonitors(half as never));
  assert.equal(calls, 2);
});

test("truncation is reported when more tasks exist than were listed", async () => {
  const { api } = fakeApi({ active: [fakeMonitorTask({ id: "m1" })], activeTotal: 90 });
  assert.equal((await loadMonitors(api)).truncated, true);
});

// --- The screen ---------------------------------------------------------------------

const readyResult = (over: Partial<Awaited<ReturnType<typeof loadMonitors>>> = {}) => ({
  status: "ready" as const,
  data: { active: [], ended: [], truncated: false, unreported: 0, ...over },
  refreshing: false,
  refreshError: null,
});

const body = (resource: Parameters<typeof MonitorsViewBody>[0]["resource"]) =>
  renderToStaticMarkup(createElement(MonitorsViewBody, { resource, onRetry: noop, onRefresh: noop, now: NOW }));

async function entryFor(task: Task) {
  const { api } = fakeApi({ active: [task] });
  const result = await loadMonitors(api);
  return [...result.active, ...result.ended][0];
}

test("loading, error, empty and populated states", async () => {
  assert.match(body({ status: "loading" }), /Loading monitors/);
  const failed = body({ status: "error", error: new StatusError(0, "network_error", "Could not reach the Mai backend. Is it running?") });
  assert.match(failed, /Monitors could not be loaded/);
  assert.match(failed, />Try again</);
  assert.match(body(readyResult()), /No active monitors/);

  const entry = await entryFor(fakeMonitorTask({ state: "queued" }));
  const populated = body(readyResult({ active: [entry] }));
  assert.match(populated, /Watch Gmail for a reply/);
  assert.match(populated, /every 15 minutes/);
  assert.match(populated, /Checks run<\/dt><dd>2</);
  assert.ok(!populated.includes("Checks started"));
  assert.match(populated, />Stop monitoring</);
  assert.match(populated, /Active: scheduled for checks/);
  assert.ok(!/Mai is checking/.test(populated), "no claim that checking is happening");
});

test("the journal is not loaded until a person opens it", async () => {
  const entry = await entryFor(fakeMonitorTask({ id: "abc" }));
  const html = body(readyResult({ active: [entry] }));
  assert.match(html, /<button[^>]*aria-expanded="false"[^>]*aria-controls="journal-abc"[^>]*>Show last check and journal</);
  assert.ok(!html.includes("Loading the last check"));
  assert.ok(!html.includes('id="journal-abc"'));
});

test("unavailable metadata is stated, not hidden or guessed", async () => {
  const unscheduled = await entryFor(fakeMonitorTask({ state: "queued", next_run_at: null }));
  assert.match(body(readyResult({ active: [unscheduled] })), /Not scheduled: no check is queued/);

  const unreadable = await entryFor(fakeMonitorTask({ monitor: { kind: null, operator: null, interval_seconds: null } }));
  const html = body(readyResult({ active: [unreadable] }));
  assert.match(html, /stored condition could not be read/);
  assert.match(html, /unknown interval/);

  assert.match(body(readyResult({ unreported: 2 })), /did not say which of 2 tasks are monitors/);
});

test("truncation is disclosed on screen", () => {
  assert.match(body(readyResult({ truncated: true })), /older monitors may not be listed/);
});

test("an ended monitor shows no stop control, no next check time and says it stopped", async () => {
  const entry = await entryFor(fakeMonitorTask({ state: "cancelled", cancelled_at: "2026-10-13T11:00:00Z", next_run_at: "2026-10-13T10:45:00Z" }));
  const html = body(readyResult({ ended: [entry] }));
  assert.ok(!html.includes("Stop monitoring"));
  assert.match(html, /Recently ended/);
  assert.match(html, /cancelled and is no longer checking/);
  assert.match(html, /No further checks: the monitor has ended/);
  assert.ok(!html.includes("dateTime=\"2026-10-13T10:45:00Z\""), "a stale schedule is not shown for an ended monitor");
});

test("a monitor that is blocked is not shown as being checked, whatever its schedule says", async () => {
  const entry = await entryFor(fakeMonitorTask({ state: "blocked", next_run_at: "2026-10-13T10:45:00Z" }));
  const html = body(readyResult({ active: [entry] }));
  assert.match(html, /Not checking right now \(blocked\)/);
  assert.match(html, /Not scheduled while blocked/);
});

// --- The open journal follows the task, and nothing else ----------------------------------

type Keyed = { key: string | null; props: { taskId: string } };

/** The journal loader element MonitorJournal puts on screen, or null when closed. */
function journalPanel(task: Task, open: boolean): Keyed | null {
  const element = MonitorJournal({ task, open }) as null | { props: { children: Keyed } };
  return element === null ? null : element.props.children;
}

test("a closed monitor mounts no journal loader, so nothing is fetched for it", () => {
  assert.equal(journalPanel(fakeMonitorTask({ id: "m1" }), false), null);
  assert.ok(journalPanel(fakeMonitorTask({ id: "m1" }), true) !== null);
});

test("an open journal is keyed by the task's version: a newer updated_at replaces it", () => {
  const before = journalPanel(fakeMonitorTask({ id: "m1", updated_at: "2026-10-13T10:05:00Z" }), true);
  const unchanged = journalPanel(fakeMonitorTask({ id: "m1", updated_at: "2026-10-13T10:05:00Z" }), true);
  const after = journalPanel(fakeMonitorTask({ id: "m1", updated_at: "2026-10-13T10:20:00Z" }), true);
  assert.ok(before && unchanged && after);
  assert.equal(before.key, unchanged.key, "same version, same key: React keeps the panel and fetches nothing");
  assert.notEqual(before.key, after.key, "newer version, new key: React mounts a fresh loader, which reads the journal again");
  assert.equal(after.props.taskId, "m1");
  assert.equal(after.key, detailKey(fakeMonitorTask({ id: "m1", updated_at: "2026-10-13T10:20:00Z" })));
});

test("the key separates tasks as well as versions", () => {
  const a = detailKey(fakeMonitorTask({ id: "a", updated_at: "T" }));
  const b = detailKey(fakeMonitorTask({ id: "b", updated_at: "T" }));
  assert.notEqual(a, b);
});

test("the Tasks tab and the Monitors tab use the same keying", () => {
  const tasksTab = readFileSync(new URL("../components/TaskActivity.tsx", import.meta.url), "utf8");
  const monitorsTab = readFileSync(new URL("../components/MonitorsView.tsx", import.meta.url), "utf8");
  assert.match(tasksTab, /key=\{detailKey\(task\)\}/);
  assert.match(monitorsTab, /key=\{detailKey\(task\)\}/);
});

test("ordinary list refreshes cost list calls only; a journal is read once per version while open", async () => {
  // Five refreshes, as the minute timer would do. The task advances (a new
  // updated_at) on the second and fourth.
  const versions = [
    "2026-10-13T10:05:00Z",
    "2026-10-13T10:20:00Z",
    "2026-10-13T10:20:00Z",
    "2026-10-13T10:35:00Z",
    "2026-10-13T10:35:00Z",
  ];
  let listCalls = 0;
  let detailCalls = 0;
  let step = 0;
  const api = {
    list: async (states?: TaskState[]): Promise<TaskList> => {
      listCalls += 1;
      const ended = states?.includes("completed");
      return { tasks: ended ? [] : [fakeMonitorTask({ id: "m1", updated_at: versions[step] })], total: ended ? 0 : 1 };
    },
    get: async () => {
      detailCalls += 1;
      throw new Error("the list load must not read details");
    },
  };

  const keys: Array<string | null> = [];
  for (step = 0; step < versions.length; step += 1) {
    const { active } = await loadMonitors(api);
    keys.push(journalPanel(active[0].task, true)?.key ?? null);
  }

  assert.equal(listCalls, 10, "two list calls per refresh");
  assert.equal(detailCalls, 0, "no refresh reads a detail");
  // React mounts a loader (one journal read) each time the key changes.
  const mounts = keys.filter((key, index) => index === 0 || key !== keys[index - 1]).length;
  assert.equal(mounts, 3, "one read when opened, one per newer version, none for an unchanged one");
});

// --- Wording that must not overclaim ------------------------------------------------------

test("the check counter is labelled for what it counts", async () => {
  const entry = await entryFor(fakeMonitorTask({ state: "queued", check_count: 4 }));
  const html = body(readyResult({ active: [entry] }));
  assert.match(html, /Checks run<\/dt><dd>4</);
  assert.ok(!html.includes("Checks started"));
});

test("an overdue schedule is not presented as the runner being healthy", async () => {
  const entry = await entryFor(fakeMonitorTask({ state: "queued", next_run_at: "2026-10-13T10:00:00Z" }));
  const html = body(readyResult({ active: [entry] }));
  const next = html.match(/Next check<\/dt><dd>(.*?)<\/dd>/)?.[1] ?? "";
  assert.match(next, /scheduled time has passed/);
  assert.match(next, /cannot tell whether the background runner is working/);
  assert.ok(!/waiting for|is running|healthy/.test(next), next);
});
