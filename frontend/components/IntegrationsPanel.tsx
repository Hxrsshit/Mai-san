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

import { useCallback, useEffect, useState } from "react";

import {
  beginIntegrationConnect,
  disconnectIntegration,
  getIntegrationStatus,
} from "@/lib/api";
import type { IntegrationStatus } from "@/lib/types";

type Key = "google" | "gmail";

const LABELS: Record<Key, string> = {
  google: "Google Calendar",
  gmail: "Gmail",
};

/** What each backend state means, in words the user can act on. */
const STATE_TEXT: Record<string, string> = {
  available: "Connected",
  authentication_required: "Not connected",
  not_configured: "Not configured on this server",
  disabled: "Disabled",
};

function describe(status: IntegrationStatus | null): string {
  if (!status) return "Checking…";
  return STATE_TEXT[status.state] ?? status.state;
}

export function IntegrationsPanel() {
  const [statuses, setStatuses] = useState<Record<Key, IntegrationStatus | null>>({
    google: null,
    gmail: null,
  });
  const [disclosure, setDisclosure] = useState<{ key: Key; text: string; url: string } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Key | null>(null);

  const refresh = useCallback(async () => {
    for (const key of ["google", "gmail"] as Key[]) {
      try {
        const status = await getIntegrationStatus(key);
        setStatuses((previous) => ({ ...previous, [key]: status }));
      } catch {
        // A status that cannot be read is left as "Checking…" rather than
        // rendered as disconnected: claiming a definite state Mai could not
        // establish is the failure this whole layer exists to avoid.
        setStatuses((previous) => ({ ...previous, [key]: null }));
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function connect(key: Key) {
    setBusy(key);
    setError(null);
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
      await disconnectIntegration(key);
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

      <ul className="space-y-2">
        {(["google", "gmail"] as Key[]).map((key) => {
          const status = statuses[key];
          const connected = status?.connected ?? false;
          const configurable = status?.state !== "not_configured";
          return (
            <li key={key} className="flex items-center justify-between gap-2 text-xs">
              <span className="flex min-w-0 flex-col">
                <span className="truncate">{LABELS[key]}</span>
                <span
                  className={
                    connected
                      ? "text-green-600 dark:text-green-400"
                      : "text-[var(--color-muted)]"
                  }
                >
                  {describe(status)}
                </span>
              </span>
              {configurable ? (
                <button
                  type="button"
                  disabled={busy === key}
                  onClick={() => (connected ? disconnect(key) : connect(key))}
                  className="shrink-0 rounded border border-[var(--color-border)] px-2 py-1
                             hover:bg-[var(--color-hover)] disabled:opacity-50"
                >
                  {connected ? "Disconnect" : "Connect"}
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>

      {error ? (
        <p className="mt-2 text-xs text-red-500" role="alert">
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
            After granting access, come back and press Connect again to refresh
            the status.
          </p>
        </div>
      ) : null}
    </div>
  );
}
