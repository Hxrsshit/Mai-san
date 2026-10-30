/** Shared test helpers: a fake browser session, a fake backend, a fake dialog. */

export class FakeStorage {
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

export type Sent = { url: string; method: string; body: string | null; auth: string | null };
export type Reply = { status: number; body?: unknown; code?: string; message?: string };
export type Backend = (sent: Sent) => Reply | Promise<Reply>;

const originalFetch = globalThis.fetch;

/** Route every `fetch` to `backend`, with a fake sessionStorage on `window`. */
export function installBackend(backend: Backend, session = new FakeStorage()) {
  const sent: Sent[] = [];
  (globalThis as unknown as { window: unknown }).window = { sessionStorage: session };
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const record: Sent = {
      url: String(input),
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? init.body : null,
      auth: headers.Authorization ?? null,
    };
    sent.push(record);
    const reply = await backend(record);
    const body =
      reply.body ??
      (reply.code ? { error: { code: reply.code, message: reply.message ?? "fake" } } : null);
    return new Response(reply.status === 204 ? null : JSON.stringify(body), {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  return { sent, session };
}

export function uninstallBackend(): void {
  globalThis.fetch = originalFetch;
  delete (globalThis as unknown as { window?: unknown }).window;
}

let loads = 0;
/** A fresh copy of the API module: its own token-entry and lock state. */
export async function freshApi(): Promise<typeof import("../../lib/api.ts")> {
  loads += 1;
  return (await import(`../../lib/api.ts?harness=${loads}`)) as typeof import("../../lib/api.ts");
}

/** A synthetic fixture task. */
export function fakeTask(overrides: Record<string, unknown> = {}) {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    conversation_id: null,
    objective: "Watch Gmail for a reply from the dentist",
    state: "queued",
    priority: "medium",
    deadline: null,
    current_step: null,
    error_code: null,
    result: null,
    failure_count: 0,
    completed_at: null,
    cancelled_at: null,
    created_at: "2026-10-13T10:00:00Z",
    updated_at: "2026-10-13T10:05:00Z",
    step_count: 1,
    monitor: null,
    next_run_at: null,
    check_count: 0,
    ...overrides,
  } as import("../../lib/types.ts").Task;
}

/** A task as the backend reports a monitor on `TaskRead`. */
export function fakeMonitorTask(overrides: Record<string, unknown> = {}) {
  return fakeTask({
    monitor: { kind: "count", operator: "gte", interval_seconds: 900 },
    next_run_at: "2026-10-13T10:45:00Z",
    check_count: 2,
    ...overrides,
  });
}

export function fakeEvent(
  sequence: number,
  event_type: string,
  metadata: Record<string, unknown> = {},
  occurred_at = "2026-10-13T10:00:00Z",
) {
  return {
    id: `00000000-0000-4000-9000-${String(sequence).padStart(12, "0")}`,
    event_type,
    actor: "system",
    sequence,
    occurred_at,
    metadata,
  } as import("../../lib/types.ts").TaskEvent;
}

export function fakeMonitorDetail(
  overrides: Record<string, unknown> = {},
  events: import("../../lib/types.ts").TaskEvent[] = [
    fakeEvent(1, "task_created"),
    fakeEvent(2, "monitoring_configured", { kind: "count", operator: "gte", interval_seconds: 900 }),
    fakeEvent(3, "observation_recorded", { check: 1, result: "not_satisfied", observed: 0 }, "2026-10-13T10:15:00Z"),
  ],
) {
  return {
    ...fakeMonitorTask(overrides),
    steps: [],
    events,
  } as import("../../lib/types.ts").TaskDetail;
}
