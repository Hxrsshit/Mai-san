"use client";

/**
 * Stop a task, in two steps, without saying it stopped until the backend did.
 *
 * "Stop" asks for confirmation (the focus lands on the safe choice, Keep).
 * While the request is in flight the control is disabled and says so. The
 * outcome text comes from `cancelTask`, which only reports "stopped" for a
 * backend reply that names the task as cancelled.
 */

import { useRef, useState } from "react";

import { tasks } from "@/lib/api";
import { type CancelOutcome, cancelTask } from "@/lib/cancel";

export type Phase = "idle" | "confirming" | "stopping";

export interface CancelControlViewProps {
  phase: Phase;
  outcome: CancelOutcome | null;
  label: string;
  onAsk: () => void;
  onConfirm: () => void;
  onKeep: () => void;
}

export function CancelControlView({ phase, outcome, label, onAsk, onConfirm, onKeep }: CancelControlViewProps) {
  const message =
    outcome === null
      ? null
      : outcome.kind === "cancelled"
        ? "Stopped. The server confirmed the task is cancelled."
        : outcome.message;
  const isProblem = outcome !== null && outcome.kind !== "cancelled";

  return (
    <div className="space-y-2">
      {phase === "confirming" ? (
        <div role="group" aria-label={`Confirm: ${label}`} className="flex flex-wrap items-center gap-2 text-xs">
          <span>This stops it for good. It cannot be resumed.</span>
          <button
            type="button"
            onClick={onKeep}
            autoFocus
            className="rounded-lg border border-[var(--color-border-strong)] px-3 py-1 hover:bg-[var(--color-hover)]"
          >
            Keep it running
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-lg border border-[color:rgba(255,107,124,.5)] px-3 py-1 text-[var(--color-error)] hover:bg-[color:rgba(255,107,124,.1)]"
          >
            Yes, stop it
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={onAsk}
          disabled={phase === "stopping" || outcome?.kind === "cancelled"}
          className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)] disabled:opacity-60"
        >
          {phase === "stopping" ? "Stopping…" : label}
        </button>
      )}
      {message ? (
        <p role={isProblem ? "alert" : "status"} className={`text-xs ${isProblem ? "text-[var(--color-error)]" : ""}`}>
          {message}
        </p>
      ) : null}
    </div>
  );
}

export function CancelControl({
  taskId,
  label = "Stop monitoring",
  onSettled,
}: {
  taskId: string;
  label?: string;
  /** Called after any answer, so the screen re-reads the task's real state. */
  onSettled: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [outcome, setOutcome] = useState<CancelOutcome | null>(null);
  // Claimed before the first await so a double click cannot send two requests.
  const inFlight = useRef(false);

  async function confirm() {
    if (inFlight.current) return;
    inFlight.current = true;
    setPhase("stopping");
    setOutcome(null);
    try {
      setOutcome(await cancelTask(tasks, taskId));
    } finally {
      inFlight.current = false;
      setPhase("idle");
      onSettled();
    }
  }

  return (
    <CancelControlView
      phase={phase}
      outcome={outcome}
      label={label}
      onAsk={() => {
        setOutcome(null);
        setPhase("confirming");
      }}
      onConfirm={() => void confirm()}
      onKeep={() => setPhase("idle")}
    />
  );
}
