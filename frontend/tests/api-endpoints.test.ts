/**
 * The new endpoint wrappers, driven through the real client: what they send,
 * how failures are worded, and that authentication behaves exactly as it does
 * for the existing calls (shared dialog, retry, no token in a URL).
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { describeError } from "../lib/errors.ts";
import { freshApi, installBackend, uninstallBackend } from "./support/harness.ts";

const VALID = "fake-valid-token-0001";
const WRONG = "fake-wrong-token-0002";
const KEY = "mai.apiToken";
const ID = "00000000-0000-4000-8000-000000000001";

afterEach(uninstallBackend);

async function withToken(reply: (url: string) => { status: number; body?: unknown; code?: string; message?: string }) {
  const { FakeStorage } = await import("./support/harness.ts");
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  const env = installBackend(({ url }) => reply(url), session);
  return { env, api: await freshApi() };
}

test("tasks.list sends each state filter and the page bound", async () => {
  const { env, api } = await withToken(() => ({ status: 200, body: { tasks: [], total: 0 } }));
  await api.tasks.list(["running", "queued"], 50);
  assert.equal(env.sent[0].url, "http://localhost:8000/api/tasks?limit=50&state=running&state=queued");
  assert.equal(env.sent[0].method, "GET");
  assert.equal(env.sent[0].auth, `Bearer ${VALID}`);
});

test("tasks.cancel is a POST with exactly an empty JSON object", async () => {
  const { env, api } = await withToken(() => ({ status: 200, body: { id: ID, state: "cancelled" } }));
  await api.tasks.cancel(ID);
  assert.equal(env.sent[0].url, `http://localhost:8000/api/tasks/${ID}/cancel`);
  assert.equal(env.sent[0].method, "POST");
  assert.equal(env.sent[0].body, "{}");
});

test("notification list, read-back and mark-read use the documented routes", async () => {
  const { env, api } = await withToken((url) =>
    url.endsWith("/read") ? { status: 204 } : { status: 200, body: { notifications: [], next_before: null } },
  );
  await api.taskNotifications.list({ before: ID, unreadOnly: true, limit: 20 });
  const marked = await api.taskNotifications.markRead(ID);
  assert.equal(
    env.sent[0].url,
    `http://localhost:8000/api/task-notifications?limit=20&before=${ID}&unread_only=true`,
  );
  assert.equal(env.sent[1].url, `http://localhost:8000/api/task-notifications/${ID}/read`);
  assert.equal(env.sent[1].method, "POST");
  assert.equal(env.sent[1].body, "{}");
  assert.equal(marked, undefined);
});

test("reminder routes and conversation rename", async () => {
  const { env, api } = await withToken((url) =>
    url.includes("/read") ? { status: 204 } : { status: 200, body: { id: ID, title: "New", reminders: [], notifications: [], total: 0 } },
  );
  await api.reminders.scheduled(10);
  await api.reminders.fired(10);
  await api.reminders.markSeen(ID);
  await api.api.renameConversation(ID, "New");
  assert.deepEqual(
    env.sent.map((s) => `${s.method} ${s.url.replace("http://localhost:8000", "")}`),
    [
      "GET /api/reminders?limit=10",
      "GET /api/reminders/notifications?limit=10",
      `POST /api/reminders/notifications/${ID}/read`,
      `PATCH /api/conversations/${ID}`,
    ],
  );
  assert.equal(env.sent[3].body, JSON.stringify({ title: "New" }));
});

test("no new call puts the token in a URL", async () => {
  const { env, api } = await withToken(() => ({ status: 200, body: { tasks: [], total: 0, notifications: [], next_before: null, reminders: [] } }));
  await api.tasks.list();
  await api.taskNotifications.list();
  await api.reminders.scheduled();
  for (const sent of env.sent) assert.ok(!sent.url.includes(VALID), sent.url);
});

// --- Error wording --------------------------------------------------------------

async function failure(reply: { status: number; code: string; message: string }, call: (api: Awaited<ReturnType<typeof freshApi>>) => Promise<unknown>) {
  const { api } = await withToken(() => reply);
  try {
    await call(api);
  } catch (caught) {
    return caught as { code: string; message: string; status: number };
  }
  throw new Error("expected a failure");
}

test("an HTTP exception reason is worded for a person, not shown as a code", async () => {
  const stopped = await failure({ status: 409, code: "http_error", message: "task_is_terminal" }, (a) => a.tasks.cancel(ID));
  assert.equal(stopped.code, "http_error");
  assert.equal(stopped.status, 409);
  assert.match(stopped.message, /already finished/);
  assert.ok(!stopped.message.includes("task_is_terminal"));

  const missing = await failure({ status: 404, code: "not_found", message: "notification_not_found" }, (a) => a.taskNotifications.get(ID));
  assert.match(missing.message, /no longer exists/);

  const notSaved = await failure({ status: 500, code: "http_error", message: "task_not_cancelled" }, (a) => a.tasks.cancel(ID));
  assert.match(notSaved.message, /may still be running/);
});

test("a route that is not mounted reads as an unavailable feature", async () => {
  const error = await failure({ status: 404, code: "not_found", message: "Not Found" }, (a) => a.reminders.scheduled());
  assert.match(error.message, /not available on this server/);
});

test("an unknown reason falls back to the status, not to raw text", async () => {
  const error = await failure({ status: 409, code: "http_error", message: "some_internal_reason" }, (a) => a.tasks.list());
  assert.equal(error.message, "Request failed (409).");
});

test("provider errors name no particular provider", async () => {
  for (const code of ["llm_not_configured", "llm_auth_error"]) {
    const error = await failure({ status: 502, code, message: "x" }, (a) => a.api.listConversations());
    assert.ok(!/groq/i.test(error.message), error.message);
    assert.match(error.message, /backend configuration/);
  }
});

test("a validation failure keeps the backend's own explanation", async () => {
  const error = await failure({ status: 422, code: "validation_error", message: "title: Title cannot be blank." }, (a) => a.api.renameConversation(ID, " "));
  assert.equal(error.code, "validation_error");
  // Written for a UI by the backend (api/errors.py); passed through unchanged.
  assert.equal(error.message, "title: Title cannot be blank.");
});

test("a network failure is reported as unreachable and is retryable", async () => {
  const { FakeStorage } = await import("./support/harness.ts");
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  installBackend(() => {
    throw new TypeError("fetch failed");
  }, session);
  const api = await freshApi();
  let caught: unknown;
  try {
    await api.tasks.list();
  } catch (error) {
    caught = error;
  }
  const view = describeError(caught);
  assert.equal((caught as { code: string }).code, "network_error");
  assert.equal(view.retryable, true);
  assert.equal(view.needsSignIn, false);
});

// --- Authentication -------------------------------------------------------------

test("an expired token on a new endpoint re-asks once, then the call succeeds", async () => {
  const { FakeStorage } = await import("./support/harness.ts");
  const session = new FakeStorage();
  session.setItem(KEY, WRONG);
  const env = installBackend(({ auth }) => {
    if (auth === `Bearer ${VALID}`) return { status: 200, body: { tasks: [], total: 0 } };
    return { status: 401, code: "invalid_credentials" };
  }, session);
  const api = await freshApi();
  const reasons: string[] = [];
  api.setTokenProvider(async (reason) => {
    reasons.push(reason);
    return VALID;
  });

  const result = await api.tasks.list();

  assert.deepEqual(result, { tasks: [], total: 0 });
  assert.deepEqual(reasons, ["rejected"]);
  assert.equal(session.getItem(KEY), VALID);
  assert.equal(env.sent.length, 2);
});

test("a cancelled token entry fails new calls clearly and keeps the person informed", async () => {
  const env = installBackend(() => ({ status: 401, code: "authentication_required" }));
  const api = await freshApi();
  api.setTokenProvider(async () => null);

  let caught: unknown;
  try {
    await api.taskNotifications.list();
  } catch (error) {
    caught = error;
  }
  const view = describeError(caught);
  assert.equal((caught as { code: string }).code, "authentication_cancelled");
  assert.equal(view.needsSignIn, true);
  assert.ok(env.sent.length >= 1);
});
