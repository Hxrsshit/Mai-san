"use client";

/**
 * Mai's monitors: what each one watches, what it last found, and whether it
 * is still running, with a way to stop one.
 *
 * Built from the task list alone (`lib/monitors.ts`): monitor metadata, the
 * schedule and the check count come on each task. The journal (last check) is
 * read only when a person opens a monitor. A monitor is shown as stopped only
 * after the backend's own state says so.
 */

import { useState } from "react";

import { tasks } from "@/lib/api";
import { loadMonitors, type MonitorEntry, type MonitorsResult } from "@/lib/monitors";
import { usePolling, type TickResult } from "@/lib/polling";
import { type Resource, useResource } from "@/lib/resource";
import { detailKey } from "@/lib/tasks";
import type { Task } from "@/lib/types";

import { CancelControl } from "./CancelControl";
import { LastCheckView, MonitorFacts, ReasonLine, StateBadge } from "./TaskParts";
import { Empty, ErrorNotice, Loading, RefreshButton, SectionHeading } from "./ui";

const POLL_MS = 60_000;

/** Loads the journal for one monitor. Mounted only when a person opens it. */
function LastCheckPanel({ taskId, now }: { taskId: string; now?: Date }) {
  const { state, reload } = useResource(() => tasks.get(taskId));
  return <LastCheckView resource={state} onRetry={() => void reload()} now={now} />;
}

/**
 * The journal of one monitor, shown only while it is open.
 *
 * Closed, it renders nothing, so nothing is fetched. Open, the loader is keyed
 * by the task's version (`detailKey`), the same pattern the Tasks tab uses:
 * when a list refresh brings a newer `updated_at` the journal is read again,
 * and while the task is unchanged it is not. No timer of its own, and no
 * hooks here so the keying can be checked without rendering.
 */
export function MonitorJournal({ task, open, now }: { task: Task; open: boolean; now?: Date }) {
  if (!open) return null;
  return (
    <div id={`journal-${task.id}`}>
      <LastCheckPanel key={detailKey(task)} taskId={task.id} now={now} />
    </div>
  );
}

function MonitorCard({
  entry,
  onSettled,
  now,
}: {
  entry: MonitorEntry;
  onSettled: () => void;
  now?: Date;
}) {
  const { task, summary } = entry;
  const [open, setOpen] = useState(false);
  return (
    <li className="space-y-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/60 p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 break-words text-sm font-medium">{task.objective}</h3>
        <StateBadge state={task.state} />
      </div>
      <MonitorFacts task={task} summary={summary} now={now} />
      <ReasonLine task={task} />
      {task.result ? <p className="whitespace-pre-wrap break-words text-xs leading-5">{task.result}</p> : null}
      {summary.active ? <CancelControl taskId={task.id} onSettled={onSettled} /> : null}
      <div className="space-y-2">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`journal-${task.id}`}
          onClick={() => setOpen((current) => !current)}
          className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)]"
        >
          {open ? "Hide last check and journal" : "Show last check and journal"}
        </button>
        <MonitorJournal task={task} open={open} now={now} />
      </div>
    </li>
  );
}

export interface MonitorsViewProps {
  resource: Resource<MonitorsResult>;
  onRetry: () => void;
  onRefresh: () => void;
  now?: Date;
}

export function MonitorsViewBody({ resource, onRetry, onRefresh, now }: MonitorsViewProps) {
  const refreshing = resource.status === "ready" && resource.refreshing;
  return (
    <section aria-labelledby="monitors-heading" className="space-y-5">
      <SectionHeading id="monitors-heading" action={<RefreshButton onClick={onRefresh} busy={refreshing} />}>
        MONITORS
      </SectionHeading>

      {resource.status === "loading" ? <Loading label="Loading monitors…" /> : null}
      {resource.status === "error" ? (
        <ErrorNotice error={resource.error} title="Monitors could not be loaded." onRetry={onRetry} />
      ) : null}

      {resource.status === "ready" ? (
        <>
          {resource.refreshError ? (
            <ErrorNotice error={resource.refreshError} title="Monitors could not be refreshed." onRetry={onRetry} stale />
          ) : null}
          {resource.data.unreported > 0 ? (
            <p role="status" className="text-xs text-[var(--color-warn)]">
              This server did not say which of {resource.data.unreported}{" "}
              {resource.data.unreported === 1 ? "task is" : "tasks are"} monitors, so{" "}
              {resource.data.unreported === 1 ? "it is" : "they are"} not listed below. The backend needs to be
              updated to report monitor details.
            </p>
          ) : null}

          <div className="space-y-2">
            <h3 className="text-xs font-medium text-[var(--color-muted)]">Watching now</h3>
            {resource.data.active.length === 0 ? (
              <Empty title="No active monitors">
                Ask Mai in a conversation to watch your Gmail for a message; once you confirm, it appears here.
              </Empty>
            ) : (
              <ul className="space-y-3">
                {resource.data.active.map((entry) => (
                  <MonitorCard key={entry.task.id} entry={entry} onSettled={onRetry} now={now} />
                ))}
              </ul>
            )}
          </div>

          {resource.data.ended.length > 0 ? (
            <div className="space-y-2">
              <h3 className="text-xs font-medium text-[var(--color-muted)]">Recently ended</h3>
              <ul className="space-y-3">
                {resource.data.ended.map((entry) => (
                  <MonitorCard key={entry.task.id} entry={entry} onSettled={onRetry} now={now} />
                ))}
              </ul>
            </div>
          ) : null}

          {resource.data.truncated ? (
            <p className="text-xs text-[var(--color-muted)]">
              Only the most recent tasks were examined, so older monitors may not be listed.
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export function MonitorsView() {
  const { state, reload } = useResource(() => loadMonitors(tasks));

  const watching = state.status === "ready" && state.data.active.length > 0;
  usePolling(
    async (): Promise<TickResult> => ((await reload()) ? "ok" : "failed"),
    POLL_MS,
    watching,
  );

  return <MonitorsViewBody resource={state} onRetry={() => void reload()} onRefresh={() => void reload()} />;
}
