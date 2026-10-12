/**
 * The shared token-entry flow in lib/api.ts, driven through the real client.
 *
 * Each case loads a fresh copy of the module (its own entry and lock state)
 * against a fake backend that follows the 6M.4 contract, a fake
 * sessionStorage and a fake dialog. Every token here is a fake sentinel.
 *
 * Run: npm test   (Node's built-in runner; Node strips the types itself)
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

type Api = typeof import("../lib/api.ts");
type Reason = import("../lib/api.ts").TokenRequestReason;

const VALID = "fake-valid-token-0001";
const WRONG = "fake-wrong-token-0002";
const NEWER = "fake-newer-token-0003";
const KEY = "mai.apiToken";
const API = "http://localhost:8000";

let loads = 0;
async function freshApi(): Promise<Api> {
  loads += 1;
  return (await import(`../lib/api.ts?case=${loads}`)) as Api;
}

class FakeStorage {
  readonly items = new Map<string, string>();
  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.items.set(key, String(value));
  }
  removeItem(key: string): void {
    this.items.delete(key);
  }
}

type Sent = { url: string; auth: string | null };
type Reply = { status: number; code?: string; body?: unknown };
type Backend = (sent: Sent) => Reply | Promise<Reply>;

/** The 6M.4 contract: no or malformed token, a wrong one, the right one. */
function contract(valid = VALID): Backend {
  return ({ auth }) => {
    const token = auth?.startsWith("Bearer ") ? auth.slice(7) : null;
    if (!token || /\s/.test(token)) return { status: 401, code: "authentication_required" };
    if (token !== valid) return { status: 401, code: "invalid_credentials" };
    return { status: 200, body: { items: [], total: 0 } };
  };
}

type Env = {
  session: FakeStorage;
  sent: Sent[];
  logged: string[];
  storageTouches: string[];
};

const originalFetch = globalThis.fetch;
const consoleMethods = ["log", "info", "warn", "error", "debug"] as const;
const originalConsole = consoleMethods.map((name) => console[name]);

function install(backend: Backend, session = new FakeStorage()): Env {
  const env: Env = { session, sent: [], logged: [], storageTouches: [] };
  const trap = new Proxy(
    {},
    {
      get(_target, property) {
        env.storageTouches.push(String(property));
        return () => null;
      },
    },
  );
  (globalThis as unknown as { window: unknown }).window = {
    sessionStorage: session,
    localStorage: trap,
    prompt: () => {
      throw new Error("window.prompt must not be used");
    },
  };
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const sent = { url: String(input), auth: headers.Authorization ?? null };
    env.sent.push(sent);
    const reply = await backend(sent);
    const body =
      reply.body ?? (reply.code ? { error: { code: reply.code, message: "fake" } } : null);
    return new Response(reply.status === 204 ? null : JSON.stringify(body), {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  consoleMethods.forEach((name) => {
    console[name] = (...args: unknown[]) => {
      env.logged.push(args.map(String).join(" "));
    };
  });
  return env;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  consoleMethods.forEach((name, index) => {
    console[name] = originalConsole[index];
  });
  delete (globalThis as unknown as { window?: unknown }).window;
});

/** A dialog that answers from a script; records each reason it was shown for. */
function dialog(api: Api, answers: Array<string | null>, delayMs = 5) {
  const reasons: Reason[] = [];
  api.setTokenProvider(async (reason) => {
    reasons.push(reason);
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    return answers.length ? answers.shift()! : null;
  });
  return reasons;
}

/** Five startup calls, issued together, as the page does on load. */
function startup(api: Api) {
  return [
    api.api.listConversations(),
    api.getIntegrationStatus("google"),
    api.getIntegrationStatus("gmail"),
    api.listImportSources(),
    api.listImportRuns(),
  ];
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

async function rejection(promise: Promise<unknown>): Promise<{ code: string; message: string }> {
  try {
    await promise;
  } catch (caught) {
    return caught as { code: string; message: string };
  }
  throw new Error("expected a rejection");
}

// --- shared acquisition ---------------------------------------------------

test("concurrent startup requests open one dialog and all succeed", async () => {
  const env = install(contract());
  const api = await freshApi();
  const reasons = dialog(api, [VALID]);

  const results = await Promise.all(startup(api));

  assert.equal(results.length, 5);
  assert.deepEqual(reasons, ["required"]);
  assert.equal(env.session.getItem(KEY), VALID);
  assert.equal(env.sent.length, 10); // each: one unauthenticated try, one retry
  assert.equal(env.sent.filter((s) => s.auth === `Bearer ${VALID}`).length, 5);
});

test("a successful response asks for nothing", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  const env = install(contract(), session);
  const api = await freshApi();
  const reasons = dialog(api, []);

  await Promise.all(startup(api));

  assert.deepEqual(reasons, []);
  assert.equal(env.sent.length, 5);
});

// --- cancellation and empty input -----------------------------------------

test("cancelling rejects every waiting request once and does not reopen", async () => {
  const env = install(contract());
  const api = await freshApi();
  const reasons = dialog(api, [null]);
  const lockStates: boolean[] = [];
  api.subscribeAuthLock((locked) => lockStates.push(locked));

  const outcomes = await Promise.allSettled(startup(api));

  assert.deepEqual(reasons, ["required"]);
  for (const outcome of outcomes) {
    assert.equal(outcome.status, "rejected");
    assert.equal((outcome as PromiseRejectedResult).reason.code, "authentication_cancelled");
  }
  assert.deepEqual(lockStates, [false, true]);

  // Later calls fail clearly without a dialog: no prompt loop.
  const later = await rejection(api.api.listConversations());
  assert.equal(later.code, "authentication_cancelled");
  assert.match(later.message, /Enter API token/);
  assert.deepEqual(reasons, ["required"]);
  assert.equal(env.session.getItem(KEY), null);
});

test("cancelling keeps a token that was already stored", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  // The backend reports this request as unauthenticated (say, a token-less
  // retry path); the stored token is not proven wrong and must survive.
  install(() => ({ status: 401, code: "authentication_required" }), session);
  const api = await freshApi();
  dialog(api, [null]);

  const error = await rejection(api.api.listConversations());

  assert.equal(error.code, "authentication_cancelled");
  assert.equal(session.getItem(KEY), VALID);
});

test("an empty or blank entry stores nothing and erases nothing", async () => {
  for (const blank of ["", "   ", "\n"]) {
    const session = new FakeStorage();
    session.setItem(KEY, NEWER);
    install(() => ({ status: 401, code: "authentication_required" }), session);
    const api = await freshApi();
    dialog(api, [blank]);

    const error = await rejection(api.api.listConversations());

    assert.equal(error.code, "authentication_cancelled");
    assert.equal(session.getItem(KEY), NEWER);
  }
});

test("an entry containing whitespace is refused, never sent", async () => {
  const env = install(contract());
  const api = await freshApi();
  dialog(api, [`${VALID} extra`]);

  const error = await rejection(api.api.listConversations());

  assert.equal(error.code, "authentication_cancelled");
  assert.equal(env.session.getItem(KEY), null);
  assert.equal(env.sent.length, 1);
});

test("signIn after a cancel lifts the lock and asks again", async () => {
  const env = install(contract());
  const api = await freshApi();
  const reasons = dialog(api, [null, VALID]);

  await rejection(api.api.listConversations());
  assert.equal(await api.signIn(), true);
  await api.api.listConversations();

  assert.deepEqual(reasons, ["required", "required"]);
  assert.equal(env.session.getItem(KEY), VALID);
});

test("no registered dialog is a clean cancel, not a crash or a prompt", async () => {
  install(contract());
  const api = await freshApi();

  const error = await rejection(api.api.listConversations());

  assert.equal(error.code, "authentication_cancelled");
});

// --- rejected, missing and unconfigured credentials ----------------------

test("a rejected stored token is cleared and re-entry is offered as 'rejected'", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, WRONG);
  const env = install(contract(), session);
  const api = await freshApi();
  const reasons = dialog(api, [VALID]);

  await Promise.all(startup(api));

  assert.deepEqual(reasons, ["rejected"]);
  assert.equal(session.getItem(KEY), VALID);
  assert.equal(env.sent.filter((s) => s.auth === `Bearer ${WRONG}`).length, 5);
});

test("a wrong entry reopens the one dialog as 'rejected', then cancel stops", async () => {
  const env = install(contract());
  const api = await freshApi();
  const reasons = dialog(api, [WRONG, null]);

  const outcomes = await Promise.allSettled(startup(api));

  assert.deepEqual(reasons, ["required", "rejected"]);
  assert.ok(outcomes.every((o) => o.status === "rejected"));
  assert.equal(env.session.getItem(KEY), null); // the wrong token is not kept
});

test("a missing token does not erase a stored one", async () => {
  // A request goes out before any token exists; by the time it fails,
  // another request has stored one. It must retry with it, not erase it.
  const session = new FakeStorage();
  const gate = deferred<void>();
  const env = install(async (sent) => {
    if (!sent.auth) {
      await gate.promise;
      return { status: 401, code: "authentication_required" };
    }
    return contract()(sent);
  }, session);
  const api = await freshApi();
  const reasons = dialog(api, []);

  const pending = api.api.listConversations();
  await new Promise((resolve) => setTimeout(resolve, 0));
  session.setItem(KEY, VALID);
  gate.resolve();
  await pending;

  assert.deepEqual(reasons, []);
  assert.equal(session.getItem(KEY), VALID);
  assert.equal(env.sent.at(-1)?.auth, `Bearer ${VALID}`);
});

test("503 authentication_not_configured is reported and never prompts", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  const env = install(() => ({ status: 503, code: "authentication_not_configured" }), session);
  const api = await freshApi();
  const reasons = dialog(api, [VALID]);

  const error = await rejection(api.api.listConversations());

  assert.equal(error.code, "authentication_not_configured");
  assert.match(error.message, /no API token configured/);
  assert.deepEqual(reasons, []);
  assert.equal(env.sent.length, 1);
  assert.equal(session.getItem(KEY), VALID);
});

test("a network error keeps the token and does not prompt", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  const env = install(contract(), session);
  globalThis.fetch = (async () => {
    throw new TypeError("Failed to fetch");
  }) as typeof fetch;
  const api = await freshApi();
  const reasons = dialog(api, [NEWER]);

  const error = await rejection(api.api.listConversations());

  assert.equal(error.code, "network_error");
  assert.deepEqual(reasons, []);
  assert.equal(session.getItem(KEY), VALID);
  assert.equal(env.storageTouches.length, 0);
});

test("other HTTP errors are not treated as authentication failures", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, VALID);
  install(() => ({ status: 404, code: "conversation_not_found" }), session);
  const api = await freshApi();
  const reasons = dialog(api, []);

  const error = await rejection(api.api.getConversation("missing"));

  assert.equal(error.code, "conversation_not_found");
  assert.deepEqual(reasons, []);
  assert.equal(session.getItem(KEY), VALID);
});

// --- stale responses and replacement races --------------------------------

test("a late rejection of an old token cannot erase a newer one", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, WRONG);
  const gate = deferred<void>();
  const env = install(async (sent) => {
    if (sent.auth === `Bearer ${WRONG}`) await gate.promise;
    return contract(NEWER)(sent);
  }, session);
  const api = await freshApi();
  const reasons = dialog(api, []);

  const old = api.api.listConversations(); // sent with WRONG, reply delayed
  await new Promise((resolve) => setTimeout(resolve, 0));
  session.setItem(KEY, NEWER); // replaced meanwhile
  gate.resolve();
  await old;

  assert.equal(session.getItem(KEY), NEWER);
  assert.deepEqual(reasons, []);
  assert.equal(env.sent.at(-1)?.auth, `Bearer ${NEWER}`);
});

test("rejections arriving while the dialog is open join it", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, WRONG);
  const first = deferred<void>();
  const second = deferred<void>();
  let wrongSeen = 0;
  install(async (sent) => {
    if (sent.auth === `Bearer ${WRONG}`) {
      wrongSeen += 1;
      await (wrongSeen === 1 ? first.promise : second.promise);
    }
    return contract()(sent);
  }, session);
  const api = await freshApi();
  const answer = deferred<string | null>();
  const reasons: Reason[] = [];
  api.setTokenProvider((reason) => {
    reasons.push(reason);
    return answer.promise;
  });

  const a = api.api.listConversations();
  const b = api.listImportRuns();
  await new Promise((resolve) => setTimeout(resolve, 0));
  first.resolve(); // a's rejection opens the dialog
  await new Promise((resolve) => setTimeout(resolve, 5));
  second.resolve(); // b's rejection arrives while it is open
  await new Promise((resolve) => setTimeout(resolve, 5));
  answer.resolve(VALID);
  await Promise.all([a, b]);

  assert.deepEqual(reasons, ["rejected"]);
  assert.equal(session.getItem(KEY), VALID);
});

test("retries are bounded even if every entered token is wrong", async () => {
  const env = install(contract());
  const api = await freshApi();
  let entries = 0;
  api.setTokenProvider(async () => `fake-wrong-${(entries += 1)}`);

  const error = await rejection(api.api.listConversations());

  assert.equal(error.code, "invalid_credentials");
  assert.ok(entries <= 3, `asked ${entries} times`);
  assert.ok(env.sent.length <= 4, `sent ${env.sent.length} times`);
  assert.equal(env.session.getItem(KEY), null); // the last rejected one too
});

test("retries are bounded when a newer token keeps appearing", async () => {
  const session = new FakeStorage();
  let n = 0;
  const env = install((sent) => {
    session.setItem(KEY, `fake-rotating-${(n += 1)}`); // always "newer", never valid
    return contract()(sent);
  }, session);
  const api = await freshApi();
  dialog(api, []);

  await rejection(api.api.listConversations());

  assert.ok(env.sent.length <= 4, `sent ${env.sent.length} times`);
});

// --- storage and leakage ---------------------------------------------------

test("the token lives in sessionStorage under the existing key, nowhere else", async () => {
  const env = install(contract());
  const api = await freshApi();
  dialog(api, [VALID]);

  await Promise.all(startup(api));

  assert.deepEqual([...env.session.items.keys()], [KEY]);
  assert.deepEqual(env.storageTouches, []); // localStorage never touched
  assert.equal((globalThis as { document?: unknown }).document, undefined);
});

test("reload keeps the session token; a new browser session asks again", async () => {
  const session = new FakeStorage();
  install(contract(), session);
  const first = await freshApi();
  dialog(first, [VALID]);
  await first.api.listConversations();

  const reloaded = await freshApi(); // same sessionStorage
  const afterReload = dialog(reloaded, []);
  await reloaded.api.listConversations();
  assert.deepEqual(afterReload, []);

  install(contract(), new FakeStorage()); // new session: empty storage
  const fresh = await freshApi();
  const newSession = dialog(fresh, [VALID]);
  await fresh.api.listConversations();
  assert.deepEqual(newSession, ["required"]);
});

test("the token never reaches a URL, a log line or an error message", async () => {
  const session = new FakeStorage();
  session.setItem(KEY, WRONG);
  const env = install(contract(), session);
  const api = await freshApi();
  dialog(api, [WRONG, VALID]);

  await Promise.all(startup(api));
  const later = install(() => ({ status: 401, code: "invalid_credentials" }), env.session);
  const again = await freshApi();
  dialog(again, [null]);
  const error = await rejection(again.api.listConversations());

  const sent = [...env.sent, ...later.sent];
  const logged = [...env.logged, ...later.logged];
  for (const token of [VALID, WRONG]) {
    assert.ok(sent.every((s) => !s.url.includes(token)));
    assert.ok(logged.every((line) => !line.includes(token)));
    assert.ok(!error.message.includes(token));
  }
  assert.ok(sent.every((s) => s.url.startsWith(`${API}/api/`)));
  assert.ok(sent.every((s) => s.auth === null || s.auth.startsWith("Bearer ")));
});
