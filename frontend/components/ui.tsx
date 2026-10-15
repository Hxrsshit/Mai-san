/** Small shared pieces: a status badge, loading, error and empty states. */

import type { ReactNode } from "react";

import { describeError } from "@/lib/errors";
import type { StatusTone } from "@/lib/integrationStatus";
import type { Tone } from "@/lib/tasks";

export type BadgeTone = "success" | "info" | "warn" | "error" | "neutral";

const BADGE: Record<BadgeTone, { text: string; dot: string }> = {
  success: { text: "text-[var(--color-success)]", dot: "bg-[var(--color-success)]" },
  info: { text: "text-[var(--color-secondary)]", dot: "bg-[var(--color-secondary)]" },
  warn: { text: "text-[var(--color-warn)]", dot: "bg-[var(--color-warn)]" },
  error: { text: "text-[var(--color-error)]", dot: "bg-[var(--color-error)]" },
  neutral: { text: "text-[var(--color-muted)]", dot: "bg-[var(--color-muted)]" },
};

export function fromTaskTone(tone: Tone): BadgeTone {
  return (
    { active: "info", waiting: "warn", success: "success", error: "error", neutral: "neutral" } as const
  )[tone];
}

export function fromStatusTone(tone: StatusTone): BadgeTone {
  return (
    { ok: "success", info: "info", warn: "warn", error: "error", unknown: "neutral" } as const
  )[tone];
}

/** The state is always spelled out; colour only reinforces it. */
export function Badge({ tone, children, title }: { tone: BadgeTone; children: ReactNode; title?: string }) {
  const style = BADGE[tone];
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-[var(--color-border)] px-2 py-0.5 text-[11px] font-medium ${style.text}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {children}
    </span>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p role="status" className="px-1 py-6 text-sm text-[var(--color-muted)]">
      {label}
    </p>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--color-border)] px-4 py-8 text-center">
      <p className="text-sm font-medium">{title}</p>
      {children ? <p className="mt-1 text-xs leading-5 text-[var(--color-muted)]">{children}</p> : null}
    </div>
  );
}

/** A failure the person can read, with a way to try again when trying again can help. */
export function ErrorNotice({
  error,
  title,
  onRetry,
  stale,
}: {
  error: unknown;
  title: string;
  onRetry?: () => void;
  /** The screen still shows older data. */
  stale?: boolean;
}) {
  const view = describeError(error);
  return (
    <div
      role="alert"
      className="rounded-xl border border-[color:rgba(255,107,124,.35)] bg-[color:rgba(255,107,124,.08)] px-4 py-3 text-sm"
    >
      <p className="font-medium text-[var(--color-error)]">{title}</p>
      <p className="mt-1 text-xs leading-5 text-[var(--color-ink)]">{view.message}</p>
      {stale ? (
        <p className="mt-1 text-xs text-[var(--color-muted)]">What is shown below may be out of date.</p>
      ) : null}
      {onRetry && view.retryable ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 rounded-lg border border-[var(--color-border-strong)] px-3 py-1 text-xs hover:bg-[var(--color-hover)]"
        >
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function SectionHeading({ id, children, action }: { id: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-3 flex items-center justify-between gap-3">
      <h2 id={id} className="text-xs font-medium tracking-[0.16em] text-[var(--color-muted)]">
        {children}
      </h2>
      {action}
    </div>
  );
}

export function RefreshButton({ onClick, busy, label = "Refresh" }: { onClick: () => void; busy: boolean; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      aria-label={busy ? "Refreshing" : label}
      className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)] disabled:opacity-50"
    >
      {busy ? "Refreshing…" : label}
    </button>
  );
}
