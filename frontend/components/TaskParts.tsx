/** Pieces shared by the task list, the task detail and the monitor cards. */

import { formatAbsolute, formatRelative, parseMoment } from "@/lib/format";
import type { Resource } from "@/lib/resource";
import {
  describeEvent,
  endMoment,
  reasonText,
  stateView,
  summariseChecks,
  watchingText,
  type MonitorSummary,
} from "@/lib/tasks";
import type { Task, TaskDetail, TaskEvent, TaskStep } from "@/lib/types";

import { Badge, ErrorNotice, Loading, fromTaskTone } from "./ui";

export function Time({ iso, now }: { iso: string | null | undefined; now?: Date }) {
  if (!iso) return <span>unknown</span>;
  return (
    <time dateTime={iso} title={formatAbsolute(iso)}>
      {formatRelative(iso, { now })}
    </time>
  );
}

export function StateBadge({ state }: { state: string }) {
  const view = stateView(state);
  return (
    <Badge tone={fromTaskTone(view.tone)} title={view.meaning}>
      {view.label}
    </Badge>
  );
}

/** The one-line reason a task stopped, from the backend's reason code. */
export function ReasonLine({ task }: { task: Pick<Task, "error_code" | "state"> }) {
  if (!task.error_code) return null;
  const text = reasonText(task.error_code);
  return (
    <p className="text-xs leading-5 text-[var(--color-muted)]">
      {text ?? "The server gave a reason this page does not know."}{" "}
      <span className="font-mono text-[11px]">({task.error_code})</span>
    </p>
  );
}

export function TaskTimes({ task, now }: { task: Task; now?: Date }) {
  const end = endMoment(task);
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
      <dt className="text-[var(--color-muted)]">Created</dt>
      <dd>
        <Time iso={task.created_at} now={now} />
      </dd>
      {end ? (
        <>
          <dt className="text-[var(--color-muted)]">{end.label}</dt>
          <dd>
            <Time iso={end.at} now={now} />
          </dd>
        </>
      ) : (
        <>
          <dt className="text-[var(--color-muted)]">Last updated</dt>
          <dd>
            <Time iso={task.updated_at} now={now} />
          </dd>
        </>
      )}
      {task.deadline ? (
        <>
          <dt className="text-[var(--color-muted)]">Deadline</dt>
          <dd>
            <Time iso={task.deadline} now={now} />
          </dd>
        </>
      ) : null}
    </dl>
  );
}

export function StepList({ steps }: { steps: TaskStep[] }) {
  if (steps.length === 0) return null;
  return (
    <section aria-label="Steps">
      <h4 className="mb-1 text-xs font-medium text-[var(--color-muted)]">Steps</h4>
      <ol className="space-y-1">
        {[...steps]
          .sort((a, b) => a.sequence - b.sequence)
          .map((step) => (
            <li key={step.id} className="flex items-center justify-between gap-3 text-xs">
              <span className="min-w-0 truncate">
                {step.sequence}. {step.title}
              </span>
              <StateBadge state={step.state} />
            </li>
          ))}
      </ol>
    </section>
  );
}

/** The journal, with only whitelisted facts per line. Collapsed by default. */
export function EventList({ events, now }: { events: TaskEvent[]; now?: Date }) {
  const lines = [...events].sort((a, b) => b.sequence - a.sequence).map(describeEvent);
  if (lines.length === 0) return null;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-[var(--color-muted)]">
        Journal ({lines.length} {lines.length === 1 ? "entry" : "entries"}, newest first)
      </summary>
      <ul className="mt-2 space-y-1.5">
        {lines.map((line) => (
          <li key={line.id}>
            <span className="font-medium">{line.label}</span>{" "}
            <span className="text-[var(--color-muted)]">
              <Time iso={line.at} now={now} />
              {line.details.length ? ` · ${line.details.join(" · ")}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

/**
 * What a monitor checks, when it next checks, and whether it is still
 * watching. Everything here comes from the task row (`TaskRead`); what the
 * last check found lives in the journal and is shown by `LastCheckView`.
 */
export function MonitorFacts({
  task,
  summary,
  now,
}: {
  task: Task;
  summary: MonitorSummary;
  now?: Date;
}) {
  const next = summary.next;
  const due = next.kind === "due" ? parseMoment(next.at) : null;
  const overdue = due !== null && due.getTime() <= (now ?? new Date()).getTime();
  return (
    <section aria-label="Monitoring" className="space-y-2 rounded-xl border border-[var(--color-border)] p-3 text-xs">
      <p className="font-medium">{watchingText(task)}</p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-[var(--color-muted)]">Checks</dt>
        <dd>{summary.checks}</dd>
        <dt className="text-[var(--color-muted)]">Frequency</dt>
        <dd>{summary.interval}</dd>
        <dt className="text-[var(--color-muted)]">Next check</dt>
        <dd>
          {next.kind === "none" ? (
            next.reason
          ) : overdue ? (
            "The scheduled time has passed. This page cannot tell whether the background runner is working."
          ) : (
            <>
              <Time iso={next.at} now={now} />
              <span className="block text-[11px] text-[var(--color-muted)]">
                While a check is running this shows when its 5-minute claim ends, then the real next time.
              </span>
            </>
          )}
        </dd>
        <dt className="text-[var(--color-muted)]">Checks run</dt>
        <dd>{summary.checksRun === null ? "Not reported by the server." : summary.checksRun}</dd>
        {summary.deadline ? (
          <>
            <dt className="text-[var(--color-muted)]">Watches until</dt>
            <dd>
              <Time iso={summary.deadline} now={now} />
            </dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}

/** The last check and the journal, from the full task record. */
export function LastCheckView({
  resource,
  onRetry,
  now,
}: {
  resource: Resource<TaskDetail>;
  onRetry: () => void;
  now?: Date;
}) {
  if (resource.status === "loading") return <Loading label="Loading the last check…" />;
  if (resource.status === "error") {
    return <ErrorNotice error={resource.error} title="The last check could not be loaded." onRetry={onRetry} />;
  }
  const checks = summariseChecks(resource.data.events);
  return (
    <div className="space-y-2 text-xs">
      {resource.refreshError ? (
        <ErrorNotice error={resource.refreshError} title="Could not refresh." onRetry={onRetry} stale />
      ) : null}
      <p>
        <span className="text-[var(--color-muted)]">Last check: </span>
        {checks.last ? (
          <>
            <Time iso={checks.last.at} now={now} /> — {checks.last.outcome}
          </>
        ) : (
          "no check has finished yet."
        )}
      </p>
      <p>
        <span className="text-[var(--color-muted)]">Finished checks: </span>
        {checks.finished}
        {checks.failed ? ` (${checks.failed} failed)` : ""}
      </p>
      <EventList events={resource.data.events} now={now} />
    </div>
  );
}
