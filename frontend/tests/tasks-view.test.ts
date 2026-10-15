/**
 * Task and monitor presentation: states come from the backend, nothing is
 * invented, and every screen state (loading, empty, error, retry) renders.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { TaskDetailView, TaskListView } from "../components/TaskActivity.tsx";
import { formatInterval, formatRelative, parseMoment } from "../lib/format.ts";
import {
  TASK_STATES,
  describeEvent,
  endMoment,
  isMonitor,
  isTerminal,
  nextCheck,
  reasonText,
  reportsMonitorMetadata,
  stateView,
  summariseChecks,
  summariseMonitor,
  watchingText,
} from "../lib/tasks.ts";
import type { Resource } from "../lib/resource.ts";
import type { TaskList } from "../lib/types.ts";
import { fakeEvent, fakeMonitorDetail, fakeMonitorTask, fakeTask } from "./support/harness.ts";

const NOW = new Date("2026-10-13T10:30:00Z");
const noop = () => undefined;

function renderList(resource: Resource<TaskList>, filter: "all" | string = "all", openId: string | null = null) {
  return renderToStaticMarkup(
    createElement(TaskListView, {
      resource,
      filter: filter as never,
      onFilter: noop,
      onRetry: noop,
      onRefresh: noop,
      openId,
      onToggle: noop,
      renderDetail: () => createElement("p", null, "detail body"),
      now: NOW,
    }),
  );
}

const ready = (tasks: TaskList["tasks"], total = tasks.length): Resource<TaskList> => ({
  status: "ready",
  data: { tasks, total },
  refreshing: false,
  refreshError: null,
});

// --- States ----------------------------------------------------------------------

test("every backend task state has a label, and no state is invented", () => {
  assert.equal(TASK_STATES.length, 10);
  for (const state of TASK_STATES) {
    const view = stateView(state);
    assert.notEqual(view.label, state, `${state} has a readable label`);
    assert.ok(view.meaning.length > 10);
  }
  // Exactly the backend's three terminal states.
  assert.deepEqual(TASK_STATES.filter(isTerminal).sort(), ["cancelled", "completed", "failed"]);
});

test("no state description claims the runner is working at this moment", () => {
  // A monitor stays `running` between checks, and this page cannot see the
  // runner, so a description may say what the state means but not that work
  // is happening now.
  for (const state of TASK_STATES) {
    assert.ok(!/working on it|is working|working now|is running now/.test(stateView(state).meaning), state);
  }
  const running = stateView("running").meaning;
  assert.doesNotMatch(running, /working on it now/);
  assert.match(running, /does not show that work is happening right now/);
  assert.match(running, /monitor stays here between checks/);
});

test("a running monitor reads the same everywhere: the state note and the monitor section agree", () => {
  const html = renderToStaticMarkup(
    createElement(TaskDetailView, {
      task: fakeMonitorTask({ state: "running" }),
      resource: { status: "loading" },
      onRetry: noop,
      onSettled: noop,
      now: NOW,
    }),
  );
  assert.match(html, /does not show that work is happening right now/);
  assert.match(html, /Active: scheduled for checks/);
  assert.ok(!html.includes("working on it now"));
  assert.ok(!/Mai is checking/.test(html));
  // The badge carries the same description as its tooltip.
  const list = renderList(ready([fakeMonitorTask({ state: "running" })]));
  assert.match(list, /title="The runner has started on it\. This state does not show/);
});

test("an unknown state is shown as the backend sent it, never as a known one", () => {
  const view = stateView("quantum_pending");
  assert.equal(view.label, "quantum_pending");
  assert.equal(view.tone, "neutral");
  assert.match(view.meaning, /does not know/);
});

test("queued, running, completed, failed and cancelled render distinctly", () => {
  const html = renderList(
    ready(
      ["queued", "running", "completed", "failed", "cancelled"].map((state, index) =>
        fakeTask({ id: `id-${index}`, state, objective: `Task ${state}` }),
      ),
    ),
  );
  for (const label of ["Queued", "Running", "Completed", "Failed", "Cancelled"]) {
    assert.ok(html.includes(`>${label}</span>`), label);
  }
});

test("a task is shown as completed only because the backend said completed", () => {
  const running = renderList(ready([fakeTask({ state: "running" })]));
  // The filter menu lists every state; only a status badge claims one.
  assert.ok(!running.includes(">Completed</span>"));
  assert.ok(running.includes(">Running</span>"));
});

test("times: completed and cancelled use their own stamp; failed says last updated", () => {
  assert.deepEqual(endMoment(fakeTask({ state: "completed", completed_at: "2026-10-13T11:00:00Z" })), {
    label: "Completed",
    at: "2026-10-13T11:00:00Z",
  });
  assert.equal(endMoment(fakeTask({ state: "cancelled", cancelled_at: "2026-10-13T11:00:00Z" }))?.label, "Cancelled");
  assert.deepEqual(endMoment(fakeTask({ state: "failed", updated_at: "2026-10-13T12:00:00Z" })), {
    label: "Last updated",
    at: "2026-10-13T12:00:00Z",
  });
  assert.equal(endMoment(fakeTask({ state: "running" })), null);
});

// --- Screen states -----------------------------------------------------------------

test("loading, empty, error and ready each render", () => {
  assert.match(renderList({ status: "loading" }), /Loading tasks/);

  const empty = renderList(ready([]));
  assert.match(empty, /No tasks yet/);

  const emptyFiltered = renderList(ready([]), "running");
  assert.match(emptyFiltered, /No running tasks/);

  const error = renderList({ status: "error", error: { code: "network_error", status: 0, message: "Could not reach the Mai backend. Is it running?" } });
  assert.match(error, /role="alert"/);
  assert.match(error, /Tasks could not be loaded/);
  assert.match(error, /Could not reach the Mai backend/);
  assert.match(error, />Try again</);

  const list = renderList(ready([fakeTask()]));
  assert.match(list, /Watch Gmail for a reply/);
});

test("a failed refresh keeps the old list and says it may be out of date", () => {
  const html = renderList({
    status: "ready",
    data: { tasks: [fakeTask()], total: 1 },
    refreshing: false,
    refreshError: { code: "network_error", status: 0, message: "Could not reach the Mai backend. Is it running?" },
  });
  assert.match(html, /could not be refreshed/);
  assert.match(html, /may be out of date/);
  assert.match(html, /Watch Gmail for a reply/);
});

test("an authentication failure offers no useless retry loop but says what is needed", () => {
  const html = renderList({
    status: "error",
    error: { code: "authentication_cancelled", status: 401, message: "Mai needs its API token for this session. Choose “Enter API token” to continue." },
  });
  assert.match(html, /Enter API token/);
});

test("a 4xx that cannot succeed on retry shows no Try again button", () => {
  const html = renderList({ status: "error", error: { code: "not_found", status: 404, message: "That feature is not available on this server." } });
  assert.ok(!html.includes("Try again"));
});

test("when more tasks exist than can be listed, the screen says so", () => {
  const html = renderList(ready([fakeTask()], 80));
  assert.match(html, /most recent of 80/);
  assert.match(html, /Older tasks cannot be listed/);
});

test("the list is keyboard-operable: each task is a button with aria-expanded", () => {
  const closed = renderList(ready([fakeTask({ id: "abc" })]));
  assert.match(closed, /<button[^>]*aria-expanded="false"[^>]*aria-controls="task-abc"/);
  const open = renderList(ready([fakeTask({ id: "abc" })]), "all", "abc");
  assert.match(open, /aria-expanded="true"/);
  assert.match(open, /id="task-abc"/);
  assert.match(open, /detail body/);
  assert.match(closed, /<select/); // the state filter is a native control
  assert.match(closed, /<label[^>]*>[^<]*State/);
});

// --- Safe summaries ----------------------------------------------------------------

test("journal entries show only whitelisted facts", () => {
  const line = describeEvent(
    fakeEvent(5, "observation_recorded", {
      check: 2,
      result: "not_satisfied",
      observed: 3,
      execution_id: "00000000-0000-4000-8000-0000000000aa",
      body: "SECRET BODY TEXT",
      authorization: "Bearer shouldnotappear",
      note: "free text with spaces",
    }),
  );
  const text = JSON.stringify(line);
  assert.ok(!text.includes("SECRET BODY"));
  assert.ok(!text.includes("shouldnotappear"));
  assert.ok(!text.includes("free text"));
  assert.ok(!text.includes("execution_id"));
  assert.ok(line.details.includes("check: 2"));
  assert.ok(line.details.includes("observed: 3"));
});

test("free text in an allowed key is still dropped", () => {
  const line = describeEvent(fakeEvent(1, "monitoring_check_failed", { reason: "<script>alert(1)</script>" }));
  assert.deepEqual(line.details, []);
});

test("known reason codes read as sentences; unknown ones are not guessed", () => {
  assert.match(reasonText("failures_exhausted") ?? "", /three times/);
  assert.equal(reasonText("something_new"), null);
  assert.equal(reasonText(null), null);
});

test("the detail view renders the outcome as plain text, never as markup", () => {
  const html = renderToStaticMarkup(
    createElement(TaskDetailView, {
      task: fakeTask({ state: "completed", completed_at: "2026-10-13T11:00:00Z", result: "Done <b>bold</b> **x**" }),
      resource: { status: "loading" },
      onRetry: noop,
      onSettled: noop,
      now: NOW,
    }),
  );
  assert.ok(html.includes("Done &lt;b&gt;bold&lt;/b&gt; **x**"));
  assert.ok(!html.includes("<b>bold"));
  assert.match(html, /Loading details/);
});

test("the detail view never renders the owner or the plan", () => {
  const html = renderToStaticMarkup(
    createElement(TaskDetailView, {
      task: { ...fakeTask(), owner_id: "OWNER-SENTINEL", plan: { secret: "PLAN-SENTINEL" } } as never,
      resource: { status: "ready", data: { ...fakeMonitorDetail(), plan: { secret: "PLAN-SENTINEL" } } as never, refreshing: false, refreshError: null },
      onRetry: noop,
      onSettled: noop,
      now: NOW,
    }),
  );
  assert.ok(!html.includes("OWNER-SENTINEL"));
  assert.ok(!html.includes("PLAN-SENTINEL"));
});

// --- Monitors ----------------------------------------------------------------------

const readyDetail = (detail = fakeMonitorDetail()): Resource<import("../lib/types.ts").TaskDetail> => ({
  status: "ready",
  data: detail,
  refreshing: false,
  refreshError: null,
});

function renderDetail(task: ReturnType<typeof fakeTask>, resource: Resource<import("../lib/types.ts").TaskDetail>) {
  return renderToStaticMarkup(
    createElement(TaskDetailView, { task, resource, onRetry: noop, onSettled: noop, now: NOW }),
  );
}

test("the backend says whether a task is a monitor; an absent field is not 'no'", () => {
  assert.equal(isMonitor(fakeMonitorTask()), true);
  assert.equal(isMonitor(fakeTask()), false); // monitor: null
  assert.equal(reportsMonitorMetadata(fakeTask()), true);
  const older = fakeTask();
  delete (older as { monitor?: unknown }).monitor;
  assert.equal(isMonitor(older), false);
  assert.equal(reportsMonitorMetadata(older), false, "an older backend sends nothing, which is unknown");
});

test("the summary comes from the task row: what is checked, how often, how many, until when", () => {
  const summary = summariseMonitor(fakeMonitorTask({ state: "queued", deadline: "2026-10-20T10:00:00Z", check_count: 3 }));
  assert.ok(summary);
  assert.equal(summary.interval, "every 15 minutes");
  assert.match(summary.checks, /count of matching items/);
  assert.match(summary.checks, /at least/);
  assert.equal(summary.checksRun, 3);
  assert.equal(summary.active, true);
  assert.equal(summary.deadline, "2026-10-20T10:00:00Z");
  assert.equal(summary.unreadable, false);
  assert.equal(summariseMonitor(fakeTask()), null);
});

test("a count the backend did not send is unknown, not zero", () => {
  const task = fakeMonitorTask();
  delete (task as { check_count?: unknown }).check_count;
  assert.equal(summariseMonitor(task)?.checksRun, null);
  assert.match(renderDetail(task, { status: "loading" }), /Not reported by the server/);
  assert.equal(summariseMonitor(fakeMonitorTask({ check_count: 0 }))?.checksRun, 0);
});

test("a stored condition the backend could not read is said to be unreadable", () => {
  const task = fakeMonitorTask({ monitor: { kind: null, operator: null, interval_seconds: null } });
  const summary = summariseMonitor(task);
  assert.equal(summary?.unreadable, true);
  assert.match(summary?.checks ?? "", /could not be read/);
  assert.equal(summary?.interval, "unknown interval");
  assert.equal(isMonitor(task), true, "still a monitor");
});

test("the next check is the stored schedule, and only where the runtime would act on it", () => {
  const at = "2026-10-13T10:45:00Z";
  assert.deepEqual(nextCheck({ state: "queued", next_run_at: at }), { kind: "due", at });
  assert.deepEqual(nextCheck({ state: "running", next_run_at: at }), { kind: "due", at });
  assert.match((nextCheck({ state: "queued", next_run_at: null }) as { reason: string }).reason, /no check is queued/);
  // A schedule on a state the runtime does not poll is not a promise.
  for (const state of ["blocked", "paused", "awaiting_approval", "planned"]) {
    const next = nextCheck({ state: state as never, next_run_at: at });
    assert.equal(next.kind, "none", state);
  }
  for (const state of ["completed", "failed", "cancelled"]) {
    const next = nextCheck({ state: state as never, next_run_at: at });
    assert.equal(next.kind, "none", state);
    assert.match((next as { reason: string }).reason, /has ended/);
  }
});

test("the detail shows the stored time, 'scheduled time has passed' when it has, and never a guess", () => {
  const upcoming = renderDetail(fakeMonitorTask({ next_run_at: "2026-10-13T10:44:00Z" }), { status: "loading" });
  assert.match(upcoming, /<time[^>]*dateTime="2026-10-13T10:44:00Z"[^>]*>in 14 minutes<\/time>/);
  assert.match(upcoming, /claim ends/);

  assert.match(renderDetail(fakeMonitorTask({ next_run_at: "2026-10-13T10:00:00Z" }), { status: "loading" }), /scheduled time has passed/);

  // A journal full of checks and an interval do not make a schedule.
  const unscheduled = renderDetail(fakeMonitorTask({ next_run_at: null }), readyDetail());
  assert.match(unscheduled, /Not scheduled: no check is queued/);
  assert.ok(!unscheduled.includes("dateTime=\"2026-10-13T10:45:00Z\""), "the default schedule of the fixture is not shown when the row says null");
});

test("a timestamp without an offset is UTC wherever the viewer is", () => {
  // A SQLite-backed server sends "2026-10-13T10:44:00". Parsed as local time in
  // a zone ahead of UTC it would look hours in the past: "due now" for a check
  // that is 14 minutes away. The zone is set for this test only.
  const before = process.env.TZ;
  process.env.TZ = "Asia/Kolkata";
  try {
    assert.equal(parseMoment("2026-10-13T10:44:00")?.toISOString(), "2026-10-13T10:44:00.000Z");
    assert.equal(parseMoment("2026-10-13T10:44:00Z")?.toISOString(), "2026-10-13T10:44:00.000Z");
    assert.equal(parseMoment("2026-10-13T16:14:00+05:30")?.toISOString(), "2026-10-13T10:44:00.000Z");
    const html = renderDetail(fakeMonitorTask({ next_run_at: "2026-10-13T10:44:00" }), { status: "loading" });
    assert.match(html, />in 14 minutes</);
    assert.ok(!html.includes("scheduled time has passed"));
    const past = renderDetail(fakeMonitorTask({ next_run_at: "2026-10-13T10:10:00" }), { status: "loading" });
    assert.match(past, /scheduled time has passed/);
  } finally {
    if (before === undefined) delete process.env.TZ;
    else process.env.TZ = before;
  }
});

test("the journal supplies the last check and the counts, and nothing else does", () => {
  const checks = summariseChecks(fakeMonitorDetail().events);
  assert.deepEqual(checks, { last: { at: "2026-10-13T10:15:00Z", outcome: "Condition not met yet" }, finished: 1, failed: 0 });

  const failing = summariseChecks([
    fakeEvent(1, "observation_recorded", { check: 1, result: "not_satisfied" }, "2026-10-13T10:00:00Z"),
    fakeEvent(2, "monitoring_check_failed", { check: 2, reason: "provider_timeout" }, "2026-10-13T11:00:00Z"),
  ]);
  assert.equal(failing.failed, 1);
  assert.equal(failing.finished, 2);
  assert.match(failing.last?.outcome ?? "", /Check failed.*timed out/);
  assert.equal(summariseChecks([]).last, null);
});

test("whether a monitor is still watching follows only the task's own state", () => {
  const expected: Record<string, RegExp> = {
    queued: /^Active: scheduled for checks/,
    running: /^Active: scheduled for checks/,
    paused: /Not checking/,
    blocked: /Not checking right now \(blocked\)/,
    completed: /condition was met/,
    failed: /no longer checking/,
    cancelled: /cancelled and is no longer checking/,
  };
  for (const [state, pattern] of Object.entries(expected)) {
    assert.match(watchingText(fakeMonitorTask({ state })), pattern, state);
  }
});

test("the journal part of the detail shows loading, error with retry, and the last check", () => {
  const task = fakeMonitorTask();
  assert.match(renderDetail(task, { status: "loading" }), /Loading the last check/);
  const error = renderDetail(task, { status: "error", error: { code: "network_error", status: 0, message: "Could not reach the Mai backend. Is it running?" } });
  assert.match(error, /The last check could not be loaded/);
  assert.match(error, />Try again</);
  // The facts from the task row are still there while the journal is unavailable.
  assert.match(error, /every 15 minutes/);
  const ready = renderDetail(task, readyDetail());
  assert.match(ready, /Last check:/);
  assert.match(ready, /Condition not met yet/);
  assert.match(ready, /Finished checks: <\/span>1</);
});

test("a stop control appears for a running monitor and not for a finished one", () => {
  const render = (state: string) => renderDetail(fakeMonitorTask({ state }), { status: "loading" });
  assert.match(render("queued"), />Stop monitoring</);
  assert.match(render("blocked"), />Stop monitoring</);
  assert.ok(!render("completed").includes("Stop monitoring"));
  assert.ok(!render("cancelled").includes("Stop monitoring"));
  // An ordinary task has no monitor section at all.
  assert.ok(!renderDetail(fakeTask({ state: "queued" }), { status: "loading" }).includes("Stop monitoring"));
});

// --- Formatting --------------------------------------------------------------------

test("relative and interval formatting", () => {
  assert.equal(formatRelative("2026-10-13T10:29:30Z", { now: NOW, locale: "en" }), "just now");
  assert.equal(formatRelative("2026-10-13T10:00:00Z", { now: NOW, locale: "en" }), "30 minutes ago");
  assert.equal(formatRelative("2026-10-13T14:30:00Z", { now: NOW, locale: "en" }), "in 4 hours");
  assert.equal(formatRelative("not a date", { now: NOW }), "unknown");
  assert.equal(formatRelative(null, { now: NOW }), "unknown");
  assert.equal(formatInterval(300), "every 5 minutes");
  assert.equal(formatInterval(3600), "every hour");
  assert.equal(formatInterval(172_800), "every 2 days");
  assert.equal(formatInterval("x"), "unknown interval");
});
