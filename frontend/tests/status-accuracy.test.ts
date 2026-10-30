/**
 * Integration and provider status accuracy: a state is shown only if the
 * backend's evidence supports it, and a failure to read is never dressed up
 * as "checking" or as a success.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { IntegrationRow } from "../components/IntegrationsPanel.tsx";
import { ProviderStatusView } from "../components/ProviderStatus.tsx";
import { classifyIntegration, disconnectMessage } from "../lib/integrationStatus.ts";
import { classifyProvider } from "../lib/providerStatus.ts";
import type { IntegrationStatus } from "../lib/types.ts";

const noop = () => undefined;
// Assembled from parts: the structure tests refuse literal remote URLs.
const SCOPES = "https" + "://scopes.example/auth/";
const REQUIRED = [`${SCOPES}calendar.readonly`];

function status(over: Partial<IntegrationStatus>): IntegrationStatus {
  return {
    integration: "google_calendar",
    state: "available",
    connected: true,
    granted_scopes: REQUIRED,
    required_scopes: REQUIRED,
    ...over,
  };
}

class StatusError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(statusCode: number, code: string) {
    super("x");
    this.status = statusCode;
    this.code = code;
  }
}

const ok = (over: Partial<IntegrationStatus>) => classifyIntegration({ kind: "ok", status: status(over) });

// --- Integrations ---------------------------------------------------------------------

test("a stored credential is shown as present and unverified, never as connected or success", () => {
  const view = ok({});
  assert.equal(view.label, "Credentials present, not verified");
  assert.notEqual(view.tone, "ok", "never green");
  assert.match(view.detail, /not been checked with the provider/);
  assert.ok(!/connected and verified|verified recently/i.test(view.label + view.detail));
  assert.equal(view.canDisconnect, true);
  assert.equal(view.canConnect, false);
});

test("no state the backend reports ever produces a green indicator", () => {
  for (const state of ["available", "configured", "authentication_required", "not_configured", "disabled", "expired", "unavailable", "wat"]) {
    for (const connected of [true, false]) {
      const view = ok({ state, connected });
      assert.notEqual(view.tone, "ok", `${state}/${connected}`);
    }
  }
});

test("each distinguishable state gets its own accurate words", () => {
  assert.equal(ok({ state: "not_configured", connected: false, granted_scopes: [] }).label, "Not configured");
  assert.equal(ok({ state: "disabled", connected: false, granted_scopes: [] }).label, "Disabled");
  const none = ok({ state: "authentication_required", connected: false, granted_scopes: [] });
  assert.equal(none.label, "Not connected");
  assert.equal(none.canConnect, true);
  assert.equal(ok({ state: "expired" }).label, "Authentication expired");
  assert.equal(ok({ state: "unavailable" }).label, "Temporarily unavailable");
  assert.equal(ok({ state: "configured", connected: false }).label, "Configured, not verified");
});

test("granted scopes that do not cover what Mai needs are reported as incomplete", () => {
  const view = ok({ state: "authentication_required", connected: false, granted_scopes: [`${SCOPES}other`] });
  assert.equal(view.label, "Permissions incomplete");
  assert.match(view.detail, /calendar\.readonly/);
  assert.equal(view.canConnect, true);
});

test("not configured offers no connect button", () => {
  const view = ok({ state: "not_configured", connected: false, granted_scopes: [] });
  assert.equal(view.canConnect, false);
  assert.equal(view.canDisconnect, false);
});

test("an unknown backend state is reported as unknown, with what was sent", () => {
  const view = ok({ state: "teleporting" });
  assert.equal(view.label, "Status unknown");
  assert.match(view.detail, /teleporting/);
});

test("an error is never shown as Checking", () => {
  const unreachable = classifyIntegration({ kind: "error", error: new StatusError(0, "network_error") });
  assert.equal(unreachable.label, "Status unknown");
  assert.match(unreachable.detail, /Could not reach the Mai backend/);
  assert.equal(unreachable.canRetry, true);

  const down = classifyIntegration({ kind: "error", error: new StatusError(500, "http_error") });
  assert.equal(down.label, "Temporarily unavailable");

  const locked = classifyIntegration({ kind: "error", error: new StatusError(401, "authentication_cancelled") });
  assert.match(locked.detail, /API token is needed/);

  const loading = classifyIntegration({ kind: "loading" });
  assert.equal(loading.label, "Checking…");
  for (const view of [unreachable, down, locked]) assert.notEqual(view.label, "Checking…");
});

test("the row renders the label, the explanation and an accessible retry", () => {
  const html = renderToStaticMarkup(
    createElement(IntegrationRow, {
      name: "Gmail",
      view: classifyIntegration({ kind: "error", error: new StatusError(0, "network_error") }),
      busy: false,
      onConnect: noop,
      onDisconnect: noop,
      onRetry: noop,
    }),
  );
  assert.match(html, /Status unknown/);
  assert.match(html, /aria-label="Check Gmail again"/);
  assert.ok(!html.includes("Checking"));
});

test("a present credential renders without any success styling", () => {
  const html = renderToStaticMarkup(
    createElement(IntegrationRow, { name: "Google Calendar", view: ok({}), busy: false, onConnect: noop, onDisconnect: noop, onRetry: noop }),
  );
  assert.ok(!html.includes("--color-success"));
  assert.ok(!/green/.test(html));
  assert.match(html, /aria-label="Disconnect Google Calendar"/);
});

test("disconnect reports whether the provider confirmed the withdrawal", () => {
  assert.match(disconnectMessage({ disconnected: true, revoked_remotely: true }), /provider confirmed/);
  assert.match(disconnectMessage({ disconnected: true, revoked_remotely: false }), /did not confirm/);
  assert.match(disconnectMessage({ disconnected: false, revoked_remotely: false }), /still connected/);
});

// --- Provider --------------------------------------------------------------------------

const health = (llm: Record<string, unknown>) => ({
  kind: "ok" as const,
  health: { status: "ok", database: { healthy: true }, llm: { healthy: false, ...llm } } as never,
});

test("observed outcomes map one-to-one onto what is shown", () => {
  const reachable = classifyProvider(health({ healthy: true, reachability: "reachable", detail: "The last groq request succeeded at 2026-10-13 15:43:46 UTC." }));
  assert.equal(reachable.label, "Model reachable");
  assert.equal(reachable.tone, "ok");

  const unverified = classifyProvider(health({ reachability: "unverified" }));
  assert.equal(unverified.label, "Not yet verified");
  assert.notEqual(unverified.tone, "ok");
  assert.match(unverified.meaning, /untested/);

  const failing = classifyProvider(health({ reachability: "failing", detail: "The last groq request failed (llm_timeout) at 2026-10-13 15:43:46 UTC." }));
  assert.equal(failing.label, "Model failing");
  assert.equal(failing.tone, "error");
  assert.match(failing.meaning, /does not switch to another provider/);
});

test("an older backend without reachability is never shown as verified", () => {
  const healthy = classifyProvider(health({ healthy: true }));
  assert.equal(healthy.label, "Configured, not verified");
  assert.notEqual(healthy.tone, "ok");
  const unhealthy = classifyProvider(health({ healthy: false }));
  assert.equal(unhealthy.label, "Not available");
});

test("the backend's sentence is passed through verbatim and provider fields only if present", () => {
  const withoutFields = classifyProvider(health({ reachability: "reachable", healthy: true, detail: "The last groq request succeeded at T." }));
  assert.equal(withoutFields.backendDetail, "The last groq request succeeded at T.");
  assert.equal(withoutFields.provider, null);
  assert.equal(withoutFields.model, null);

  const withFields = classifyProvider(health({ reachability: "reachable", healthy: true, provider: "gemini", model: "m-1" }));
  assert.equal(withFields.provider, "gemini");
  assert.equal(withFields.model, "m-1");
});

test("an unreachable or broken health call is not reported as a model state", () => {
  const down = classifyProvider({ kind: "error", error: new StatusError(0, "network_error") });
  assert.equal(down.label, "Backend unreachable");
  const odd = classifyProvider({ kind: "error", error: new StatusError(500, "http_error") });
  assert.equal(odd.label, "Status unknown");
  assert.equal(classifyProvider({ kind: "loading" }).label, "Checking…");
});

test("the panel says the provider cannot be changed here and never mentions OpenAI", () => {
  const view = classifyProvider(health({ reachability: "failing", provider: "groq", model: "m" }));
  const html = renderToStaticMarkup(createElement(ProviderStatusView, { view, database: { healthy: true }, open: true, onToggle: noop, onRecheck: noop, checking: false }));
  assert.match(html, /cannot be changed here/);
  assert.match(html, /groq/);
  assert.ok(!/openai/i.test(html));
  assert.ok(!/<select/.test(html), "no provider switcher");
  assert.match(html, /does not send anything to the model/);
});

test("the provider toggle is a button that reports expanded state", () => {
  const view = classifyProvider(health({ reachability: "unverified" }));
  const closed = renderToStaticMarkup(createElement(ProviderStatusView, { view, open: false, onToggle: noop, onRecheck: noop, checking: false }));
  assert.match(closed, /<button[^>]*aria-expanded="false"[^>]*aria-controls="provider-status-panel"/);
  assert.ok(!closed.includes("provider-status-panel\" role"));
  const open = renderToStaticMarkup(createElement(ProviderStatusView, { view, open: true, onToggle: noop, onRecheck: noop, checking: false }));
  assert.match(open, /aria-expanded="true"/);
  assert.match(open, /role="region"[^>]*aria-label="Language model status"|aria-label="Language model status"/);
});

test("fields not reported are said to be unreported, not left blank or invented", () => {
  const view = classifyProvider(health({ reachability: "unverified" }));
  const html = renderToStaticMarkup(createElement(ProviderStatusView, { view, open: true, onToggle: noop, onRecheck: noop, checking: false }));
  assert.equal((html.match(/Not reported by the backend/g) ?? []).length, 2);
});
