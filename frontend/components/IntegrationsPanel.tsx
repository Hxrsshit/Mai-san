"use client";

/**
 * Connection state for Mai's Google integrations.
 *
 * Calendar and Gmail are shown separately because they *are* separate: each
 * holds its own OAuth grant with its own scope, and connecting one connects
 * nothing else. A single "Google" switch would misrepresent what the user is
 * agreeing to.
 *
 * The browser is not part of the credential path. It asks the backend for a
 * consent URL, shows the disclosure, and opens the URL in a new tab; Google
 * redirects back to the *backend*, which exchanges the code and stores the
 * token in its own credential store. No token, code or secret ever reaches
 * this component, and nothing here writes to localStorage or sessionStorage.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  beginIntegrationConnect,
  disconnectIntegration,
  getIntegrationStatus,
} from "@/lib/api";
import {
  classifyIntegration,
  disconnectMessage,
  type IntegrationView,
  type StatusResult,
  type StatusTone,
} from "@/lib/integrationStatus";

type Key = "google" | "gmail";

const LABELS: Record<Key, string> = {
  google: "Google Calendar",
  gmail: "Gmail",
};

/**
 * Only a state the backend actually observed is ever shown in green. A stored
 * credential is shown in blue as "present, not verified" (see
 * `lib/integrationStatus.ts`): nothing here has asked the provider.
 */
const TONE_CLASS: Record<StatusTone, string> = {
  ok: "text-[var(--color-success)]",
  info: "text-[var(--color-secondary)]",
  warn: "text-[var(--color-warn)]",
  error: "text-[var(--color-error)]",
  unknown: "text-[var(--color-muted)]",
};

/** A status read is a cheap local lookup, but still not repeated more than this often. */
const MIN_REFRESH_GAP_MS = 5_000;

export interface IntegrationRowProps {
  name: string;
  view: IntegrationView;
  busy: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  onRetry: () => void;
}

function RowButton({ label, aria, busy, onClick }: { label: string; aria: string; busy: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      disabled={busy}
      onClick={onClick}
      aria-label={aria}
      className="rounded border border-[var(--color-border)] px-2 py-1 hover:bg-[var(--color-hover)] disabled:opacity-50"
    >
      {label}
    </button>
  );
}

export function IntegrationRow({ name, view, busy, onConnect, onDisconnect, onRetry }: IntegrationRowProps) {
  return (
    <li className="flex items-start justify-between gap-2 text-xs">
      <span className="flex min-w-0 flex-col">
        <span className="truncate">{name}</span>
        <span className={TONE_CLASS[view.tone]}>{view.label}</span>
        <span className="text-[11px] leading-4 text-[var(--color-muted)]">{view.detail}</span>
      </span>
      <span className="flex shrink-0 flex-col gap-1">
        {view.canRetry ? (
          <RowButton label="Retry" aria={`Check ${name} again`} busy={busy} onClick={onRetry} />
        ) : null}
        {view.canConnect ? (
          <RowButton label="Connect" aria={`Connect ${name}`} busy={busy} onClick={onConnect} />
        ) : null}
        {view.canDisconnect ? (
          <RowButton label="Disconnect" aria={`Disconnect ${name}`} busy={busy} onClick={onDisconnect} />
        ) : null}
      </span>
    </li>
  );
}

export function IntegrationsPanel() {
  const [statuses, setStatuses] = useState<Record<Key, StatusResult>>({
    google: { kind: "loading" },
    gmail: { kind: "loading" },
  });
  const [notice, setNotice] = useState<string | null>(null);
  const lastRefresh = useRef(0);
  const [disclosure, setDisclosure] = useState<{ key: Key; text: string; url: string } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Key | null>(null);

  const refresh = useCallback(async () => {
    lastRefresh.current = Date.now();
    // Both are read together. A failure is kept as a failure, with its cause:
    // it is never turned into "Checking…" or into a state Mai did not observe.
    await Promise.all(
      (["google", "gmail"] as Key[]).map(async (key) => {
        let result: StatusResult;
        try {
          result = { kind: "ok", status: await getIntegrationStatus(key) };
        } catch (error) {
          result = { kind: "error", error };
        }
        setStatuses((previous) => ({ ...previous, [key]: result }));
      }),
    );
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Authorization finishes in another tab. Coming back re-reads the (local,
  // cheap) status once, instead of asking the person to press Connect again.
  useEffect(() => {
    const onFocus = () => {
      if (Date.now() - lastRefresh.current >= MIN_REFRESH_GAP_MS) void refresh();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  async function connect(key: Key) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      const start = await beginIntegrationConnect(key);
      // Shown before anything is opened. The disclosure is the point of the
      // step, so it is not skipped past.
      setDisclosure({ key, text: start.disclosure, url: start.authorization_url });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start authorization.");
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(key: Key) {
    setBusy(key);
    setError(null);
    try {
      const result = await disconnectIntegration(key);
      setNotice(disconnectMessage(result));
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not disconnect.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="border-t border-[var(--color-border)] px-4 py-3">
      <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
        Integrations
      </h2>

      <ul className="space-y-3">
        {(["google", "gmail"] as Key[]).map((key) => (
          <IntegrationRow
            key={key}
            name={LABELS[key]}
            view={classifyIntegration(statuses[key])}
            busy={busy === key}
            onConnect={() => void connect(key)}
            onDisconnect={() => void disconnect(key)}
            onRetry={() => void refresh()}
          />
        ))}
      </ul>

      {notice ? (
        <p className="mt-2 text-xs text-[var(--color-muted)]" role="status">
          {notice}
        </p>
      ) : null}

      {error ? (
        <p className="mt-2 text-xs text-[var(--color-error)]" role="alert">
          {error}
        </p>
      ) : null}

      {disclosure ? (
        <div
          role="dialog"
          aria-label={`Connect ${LABELS[disclosure.key]}`}
          className="mt-3 rounded border border-[var(--color-border)] p-3 text-xs"
        >
          <p className="mb-2 whitespace-pre-line">{disclosure.text}</p>
          <div className="flex gap-2">
            <a
              href={disclosure.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={() => setDisclosure(null)}
              className="rounded bg-[var(--color-accent)] px-2 py-1 text-white"
            >
              Continue to Google
            </a>
            <button
              type="button"
              onClick={() => setDisclosure(null)}
              className="rounded border border-[var(--color-border)] px-2 py-1"
            >
              Cancel
            </button>
          </div>
          <p className="mt-2 text-[var(--color-muted)]">
            After granting access, return to this tab and the status refreshes.
          </p>
        </div>
      ) : null}
    </div>
  );
}
