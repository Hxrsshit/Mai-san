"use client";

/**
 * The language-model provider's status, from the backend's own health report.
 *
 * What it can show is limited to what `/health` proves: the outcome of the
 * provider's last *real* request (never a probe, so checking costs no tokens),
 * and the provider and model only when the backend reports them. There is no
 * switching control: the provider is chosen in the server configuration and
 * Mai never changes it on its own.
 */

import { useEffect, useRef, useState } from "react";

import { getHealth } from "@/lib/api";
import { classifyProvider, type ProviderResult, type ProviderView } from "@/lib/providerStatus";
import { useResource } from "@/lib/resource";
import type { Health } from "@/lib/types";

import { Badge, fromStatusTone } from "./ui";

export interface ProviderStatusViewProps {
  view: ProviderView;
  database?: { healthy: boolean } | null;
  open: boolean;
  onToggle: () => void;
  onRecheck: () => void;
  checking: boolean;
}

export function ProviderStatusView({ view, database, open, onToggle, onRecheck, checking }: ProviderStatusViewProps) {
  return (
    <div className="relative">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls="provider-status-panel"
        className="flex items-center gap-2 rounded-lg px-2 py-1 text-xs hover:bg-[var(--color-hover)]"
      >
        <span className="text-[var(--color-muted)]">Model</span>
        <Badge tone={fromStatusTone(view.tone)}>{view.label}</Badge>
      </button>

      {open ? (
        <div
          id="provider-status-panel"
          role="region"
          aria-label="Language model status"
          className="absolute right-0 z-30 mt-2 w-80 max-w-[calc(100vw-2rem)] space-y-3 rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-elevated)] p-4 text-xs shadow-2xl"
        >
          <p className="leading-5">{view.meaning}</p>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-[var(--color-muted)]">Provider</dt>
            <dd>{view.provider ?? "Not reported by the backend"}</dd>
            <dt className="text-[var(--color-muted)]">Model</dt>
            <dd>{view.model ?? "Not reported by the backend"}</dd>
            {database ? (
              <>
                <dt className="text-[var(--color-muted)]">Database</dt>
                <dd>{database.healthy ? "Reachable" : "Not reachable"}</dd>
              </>
            ) : null}
          </dl>
          {view.backendDetail ? (
            <p className="leading-5 text-[var(--color-muted)]">
              Backend report: {view.backendDetail}
            </p>
          ) : null}
          <p className="leading-5 text-[var(--color-muted)]">
            The provider is chosen in the server’s configuration and cannot be changed here. If a request to it fails,
            that message is not answered and nothing is sent to a different provider.
          </p>
          <button
            type="button"
            onClick={onRecheck}
            disabled={checking}
            className="rounded-lg border border-[var(--color-border)] px-3 py-1 hover:bg-[var(--color-hover)] disabled:opacity-50"
          >
            {checking ? "Checking…" : "Re-read status"}
          </button>
          <p className="text-[11px] text-[var(--color-muted)]">
            Re-reading asks the backend what it last observed. It does not send anything to the model.
          </p>
        </div>
      ) : null}
    </div>
  );
}

/** `refreshKey` changes after each chat turn, when the observed outcome may have changed. */
export function ProviderStatus({ refreshKey }: { refreshKey: number }) {
  const { state, reload } = useResource<Health>(getHealth);
  const [open, setOpen] = useState(false);
  const first = useRef(true);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    void reload();
  }, [refreshKey, reload]);

  const result: ProviderResult =
    state.status === "loading"
      ? { kind: "loading" }
      : state.status === "error"
        ? { kind: "error", error: state.error }
        : { kind: "ok", health: state.data };

  // A refresh that failed after an earlier success must not keep claiming the old state.
  const view =
    state.status === "ready" && state.refreshError
      ? classifyProvider({ kind: "error", error: state.refreshError })
      : classifyProvider(result);

  return (
    <ProviderStatusView
      view={view}
      database={state.status === "ready" && !state.refreshError ? state.data.database : null}
      open={open}
      onToggle={() => setOpen((current) => !current)}
      onRecheck={() => void reload()}
      checking={state.status === "ready" && state.refreshing}
    />
  );
}
