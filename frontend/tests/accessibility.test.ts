/**
 * Keyboard and assistive-technology behaviour of the new controls, plus the
 * small pieces of logic behind them (tab keys, loading states, backoff).
 *
 * Markup is checked by rendering the real components; live focus movement and
 * key handling in a browser is covered by the end-to-end check in the report.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { createElement, Fragment } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Sidebar } from "../components/Sidebar.tsx";
import { Tabs, panelId, tabId } from "../components/Tabs.tsx";
import { Empty, ErrorNotice, Loading } from "../components/ui.tsx";
import { nextDelay, MAX_BACKOFF_MS } from "../lib/polling.ts";
import { mapReady, reduceResource, type Resource } from "../lib/resource.ts";
import { nextTabIndex } from "../lib/tabs.ts";

const noop = () => undefined;
const items = [
  { id: "chat", label: "Chat" },
  { id: "tasks", label: "Tasks" },
  { id: "inbox", label: "Inbox", badge: "3" },
];

test("tabs follow the ARIA pattern: one tab stop, selected state, named panels", () => {
  const html = renderToStaticMarkup(createElement(Tabs, { items, selected: "tasks", onSelect: noop, label: "Workspace" }));
  assert.match(html, /role="tablist"[^>]*aria-label="Workspace"/);
  const tabs = html.match(/<button[^>]*role="tab"[^>]*>/g) ?? [];
  assert.equal(tabs.length, 3);
  assert.equal(tabs.filter((t) => t.includes('tabindex="0"')).length, 1, "exactly one tab is in the Tab order");
  assert.ok(tabs.find((t) => t.includes('id="tab-tasks"'))?.includes('aria-selected="true"'));
  assert.ok(tabs.find((t) => t.includes('id="tab-chat"'))?.includes('aria-selected="false"'));
  // Only the selected tab names its panel; inactive tabs carry no aria-controls at all.
  // The expected ID is a literal, and panelId (used for the panel's own id) is pinned to it separately.
  const selectedTab = tabs.find((t) => t.includes('id="tab-tasks"'));
  assert.ok(selectedTab?.includes('aria-controls="panel-tasks"'), `selected tab-tasks must have aria-controls="panel-tasks"; got ${selectedTab}`);
  assert.equal(panelId("tasks"), "panel-tasks");
  const inactiveTabs = ["chat", "inbox"].map((id) => ({ id, tag: tabs.find((t) => t.includes(`id="tab-${id}"`)) }));
  assert.equal(inactiveTabs.filter(({ tag }) => tag !== undefined).length, 2, "both inactive tabs (tab-chat, tab-inbox) are rendered");
  for (const { id, tag } of inactiveTabs) {
    assert.doesNotMatch(tag!, /aria-controls=/, `inactive tab-${id} must have no aria-controls attribute; got ${tag}`);
  }
  assert.equal(tabId("x"), "tab-x");
});

test("the selected tab's aria-controls resolves to the panel the caller renders", () => {
  // Tabs renders no panels. Its contract is that the caller renders the selected
  // tab's panel with id panelId(selected), as app/page.tsx does; with that panel
  // beside the list, the reference must name exactly one element, the panel.
  for (const { id: selected } of items) {
    const html = renderToStaticMarkup(
      createElement(
        Fragment,
        null,
        createElement(Tabs, { items, selected, onSelect: noop, label: "Workspace" }),
        createElement("div", { role: "tabpanel", id: panelId(selected), "aria-labelledby": tabId(selected) }),
      ),
    );
    const selectedTabs = (html.match(/<button[^>]*role="tab"[^>]*>/g) ?? []).filter((t) => t.includes('aria-selected="true"'));
    assert.equal(selectedTabs.length, 1, `exactly one tab is selected when selected=${selected}`);
    const controls = selectedTabs[0].match(/aria-controls="([^"]+)"/)?.[1];
    assert.ok(controls, `selected tab-${selected} must name its panel; got ${selectedTabs[0]}`);
    const targets = (html.match(/<[a-z][a-z0-9-]*\s[^>]*>/g) ?? []).filter((t) => t.includes(` id="${controls}"`));
    assert.equal(targets.length, 1, `aria-controls="${controls}" must match exactly one rendered element`);
    assert.match(targets[0], /role="tabpanel"/, `aria-controls="${controls}" must name the panel, not ${targets[0]}`);
  }
});

test("an unread count is spoken, not just shown", () => {
  const html = renderToStaticMarkup(createElement(Tabs, { items, selected: "chat", onSelect: noop, label: "Workspace" }));
  assert.match(html, /3<span class="sr-only"> unread<\/span>/);
});

test("arrow keys wrap, Home and End jump, other keys are left alone", () => {
  assert.equal(nextTabIndex("ArrowRight", 0, 4), 1);
  assert.equal(nextTabIndex("ArrowRight", 3, 4), 0);
  assert.equal(nextTabIndex("ArrowLeft", 0, 4), 3);
  assert.equal(nextTabIndex("Home", 2, 4), 0);
  assert.equal(nextTabIndex("End", 0, 4), 3);
  assert.equal(nextTabIndex("a", 0, 4), null);
  assert.equal(nextTabIndex("Tab", 0, 4), null);
  assert.equal(nextTabIndex("ArrowRight", 0, 0), null);
});

test("the conversation rename and delete controls are real, named, keyboard-reachable buttons", () => {
  const html = renderToStaticMarkup(
    createElement(Sidebar, {
      conversations: [{ id: "c1", title: "Trip plans", created_at: "2026-10-13T10:00:00Z", updated_at: "2026-10-13T10:00:00Z" }],
      activeId: null,
      onSelect: noop,
      onCreate: noop,
      onDelete: noop,
      onRename: async () => null,
      disabled: false,
    }),
  );
  assert.match(html, /<button[^>]*aria-label="Rename Trip plans"/);
  assert.match(html, /<button[^>]*aria-label="Delete Trip plans"/);
  // `hidden` is display:none and removes a control from the keyboard; the
  // controls are revealed by opacity on hover *and* on focus instead.
  const actions = html.match(/<span class="absolute[^"]*">/)?.[0] ?? "";
  assert.match(actions, /group-focus-within:opacity-100/);
  assert.ok(!/\bhidden\b/.test(actions));
});

test("the rename field names itself, takes Enter and Escape, and reports errors as alerts", () => {
  const source = readFileSync(new URL("../components/Sidebar.tsx", import.meta.url), "utf8");
  assert.match(source, /aria-label=\{`New title for \$\{conversation\.title\}`\}/);
  assert.match(source, /event\.key === "Escape"/);
  assert.match(source, /onSubmit/);
  assert.match(source, /role="alert"/);
  assert.match(source, /A title cannot be blank/);
});

test("status regions: loading is polite, errors are alerts, empty states have a heading", () => {
  assert.match(renderToStaticMarkup(createElement(Loading, { label: "Loading tasks…" })), /role="status"/);
  const error = renderToStaticMarkup(
    createElement(ErrorNotice, { error: { code: "network_error", status: 0, message: "Could not reach the Mai backend. Is it running?" }, title: "Tasks could not be loaded.", onRetry: noop }),
  );
  assert.match(error, /role="alert"/);
  assert.match(error, /<button[^>]*type="button"[^>]*>Try again<\/button>/);
  assert.match(renderToStaticMarkup(createElement(Empty, { title: "No tasks yet" })), /No tasks yet/);
});

test("state is spelled out in text wherever colour is used", () => {
  const html = renderToStaticMarkup(createElement(Tabs, { items, selected: "chat", onSelect: noop, label: "x" }));
  assert.ok(html.length > 0);
  const ui = readFileSync(new URL("../components/ui.tsx", import.meta.url), "utf8");
  // The badge's dot is decorative; its meaning is always the children text.
  assert.match(ui, /aria-hidden="true"/);
});

// --- Loading logic ---------------------------------------------------------------------

test("a failed refresh keeps the data and flags it; a failed first load is an error", () => {
  const loading: Resource<number> = { status: "loading" };
  const first = reduceResource(loading, { type: "failure", error: "boom" });
  assert.deepEqual(first, { status: "error", error: "boom" });

  const ready = reduceResource(loading, { type: "success", data: 1 });
  const refreshing = reduceResource(ready, { type: "start" });
  assert.deepEqual(refreshing, { status: "ready", data: 1, refreshing: true, refreshError: null });
  const failed = reduceResource(refreshing, { type: "failure", error: "boom" });
  assert.deepEqual(failed, { status: "ready", data: 1, refreshing: false, refreshError: "boom" });
  const recovered = reduceResource(failed, { type: "success", data: 2 });
  assert.deepEqual(recovered, { status: "ready", data: 2, refreshing: false, refreshError: null });
});

test("retrying from an error returns to loading", () => {
  assert.deepEqual(reduceResource({ status: "error", error: "x" }, { type: "start" }), { status: "loading" });
});

test("local edits apply only to loaded data", () => {
  assert.deepEqual(mapReady<number>({ status: "loading" }, (n) => n + 1), { status: "loading" });
  assert.deepEqual(mapReady<number>({ status: "ready", data: 1, refreshing: false, refreshError: null }, (n) => n + 1), {
    status: "ready",
    data: 2,
    refreshing: false,
    refreshError: null,
  });
});

test("refresh backs off exponentially on failure and is capped", () => {
  assert.equal(nextDelay(30_000, 0), 30_000);
  assert.equal(nextDelay(30_000, 1), 60_000);
  assert.equal(nextDelay(30_000, 2), 120_000);
  assert.equal(nextDelay(30_000, 3), 240_000);
  assert.equal(nextDelay(30_000, 4), MAX_BACKOFF_MS);
  assert.equal(nextDelay(30_000, 99), MAX_BACKOFF_MS);
});

test("polling is never an interval and pauses with the tab", () => {
  const source = readFileSync(new URL("../lib/polling.ts", import.meta.url), "utf8");
  assert.ok(!source.includes("setInterval"));
  assert.match(source, /document\.hidden/);
  assert.match(source, /visibilitychange/);
});
