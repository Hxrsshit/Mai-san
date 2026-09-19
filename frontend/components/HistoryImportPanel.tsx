"use client";

/**
 * Stage 5C: importing ChatGPT history.
 *
 * There is no file picker here, and its absence is the design rather than an
 * omission. Uploading would mean multipart form parsing on the backend, which
 * would add a dependency and put Starlette's form parser on a reachable path
 * — the thing that currently makes its form-parsing advisory inapplicable to
 * this deployment. It would also push a multi-hundred-megabyte export through
 * the browser and the request path for no benefit.
 *
 * So the user drops the export into the server's import folder, and this
 * panel lists what is there and asks the backend to import one by name. No
 * archived conversation text ever reaches the browser: the list is filenames
 * and sizes, and a run is counts.
 *
 * Nothing here writes to localStorage or sessionStorage.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError, listImportRuns, listImportSources, startImport } from "@/lib/api";
import type { ImportRun, ImportSource } from "@/lib/types";

/** What each backend status means, in words the user can act on. */
const STATUS_TEXT: Record<string, string> = {
  pending: "Queued",
  parsing: "Reading the export…",
  extracting: "Learning from it…",
  completed: "Imported",
  partial: "Imported, up to the configured limit",
  failed: "Failed",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function describe(run: ImportRun): string {
  if (run.already_imported) return "Already imported — nothing to do";
  if (run.status === "failed") return STATUS_TEXT.failed;
  return STATUS_TEXT[run.status] ?? run.status;
}

export function HistoryImportPanel() {
  const [sources, setSources] = useState<ImportSource[] | null>(null);
  const [runs, setRuns] = useState<ImportRun[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [available, history] = await Promise.all([
        listImportSources(),
        listImportRuns(),
      ]);
      setSources(available.sources);
      setRuns(history.runs);
      setUnavailable(false);
    } catch (caught) {
      // A 404 means the feature is switched off on this server, which is a
      // configuration state rather than an error to shout about.
      if (caught instanceof ApiError && caught.status === 404) {
        setUnavailable(true);
        return;
      }
      setError(caught instanceof ApiError ? caught.message : "Could not load imports.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runImport = useCallback(
    async (filename: string) => {
      setBusy(filename);
      setError(null);
      setNotice(null);
      try {
        const run = await startImport(filename);
        // Without this the idempotent case looks like nothing happened: the
        // list refreshes to the same single entry and the click appears to
        // have been ignored. `already_imported` is the run's answer, and the
        // stored record cannot carry it -- it is a fact about this request.
        setNotice(
          run.already_imported
            ? `${filename} was already imported — nothing to do.`
            : `${filename} imported: ${run.conversations_imported} conversations, ${run.memories_derived} remembered.`,
        );
        await refresh();
      } catch (caught) {
        setError(
          caught instanceof ApiError ? caught.message : "The import could not start.",
        );
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  if (unavailable) return null;

  return (
    <section className="border-t border-[var(--color-border)] px-4 py-3 text-xs">
      <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
        ChatGPT history
      </h2>

      {sources === null && <p className="text-[var(--color-muted)]">Checking…</p>}

      {sources !== null && sources.length === 0 && (
        <p className="text-[var(--color-muted)]">
          No exports found. Put a ChatGPT export .zip in Mai&apos;s import folder on
          the server, then reload.
        </p>
      )}

      <ul className="space-y-1">
        {sources?.map((source) => (
          <li key={source.filename} className="flex items-center justify-between gap-2">
            <span className="truncate" title={source.filename}>
              {source.filename}
              <span className="ml-1 text-[var(--color-muted)]">{formatSize(source.size_bytes)}</span>
            </span>
            <button
              type="button"
              onClick={() => void runImport(source.filename)}
              disabled={busy !== null}
              className="shrink-0 rounded border border-[var(--color-border)] px-2 py-1
                         hover:bg-[var(--color-surface)] disabled:opacity-40"
            >
              {busy === source.filename ? "Importing…" : "Import"}
            </button>
          </li>
        ))}
      </ul>

      {error && (
        <p className="mt-2 text-red-500" role="alert">
          {error}
        </p>
      )}

      {notice && !error && (
        <p className="mt-2 text-[var(--color-muted)]" role="status">
          {notice}
        </p>
      )}

      {runs.length > 0 && (
        <div className="mt-3 space-y-1">
          <h3 className="text-[var(--color-muted)]">Previous imports</h3>
          {runs.map((run) => (
            <div key={run.id} className="text-[var(--color-muted)]">
              <span className="text-[var(--color-ink)]">{run.source_filename}</span>
              {" — "}
              {describe(run)}
              {run.status !== "failed" && (
                <>
                  {" · "}
                  {run.conversations_imported} conversations,{" "}
                  {run.memories_derived} remembered
                  {run.redactions > 0 && (
                    /* A count, never the values: the archive stores the masked
                       form and the secrets themselves were never persisted. */
                    <> · {run.redactions} credentials masked</>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
