"use client";

/**
 * Recent tasks and background activity.
 *
 * Everything shown is a backend record: the state is the backend's state, a
 * time is a stored time, and nothing is "completed" until the backend says
 * so. The list is a snapshot refreshed on demand and, while something is
 * still in progress, on a slow timer that pauses with the tab.
 */

import { useState } from "react";

import { tasks } from "@/lib/api";
import { detailKey, endMoment, stateView, summariseMonitor, TASK_STATES } from "@/lib/tasks";
import { usePolling, type TickResult } from "@/lib/polling";
import { type Resource, useResource } from "@/lib/resource";
import type { Task, TaskDetail, TaskList, TaskState } from "@/lib/types";

import { CancelControl } from "./CancelControl";
import { EventList, LastCheckView, MonitorFacts, ReasonLine, StateBadge, StepList, TaskTimes, Time } from "./TaskParts";
import { Empty, ErrorNotice, Loading, RefreshButton, SectionHeading } from "./ui";

export type Filter = TaskState | "all";
export const LIST_LIMIT = 50;
const POLL_MS = 30_000;

export interface TaskListViewProps {
  resource: Resource<TaskList>;
  filter: Filter;
  onFilter: (filter: Filter) => void;
  onRetry: () => void;
  onRefresh: () => void;
  openId: string | null;
  onToggle: (id: string) => void;
  renderDetail: (task: Task) => React.ReactNode;
  now?: Date;
}

export function TaskListView({
  resource,
  filter,
  onFilter,
  onRetry,
  onRefresh,
  openId,
  onToggle,
  renderDetail,
  now,
}: TaskListViewProps) {
  const refreshing = resource.status === "ready" && resource.refreshing;
  return (
    <section aria-labelledby="tasks-heading" className="space-y-4">
      <SectionHeading
        id="tasks-heading"
        action={
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
              State
              <select
                value={filter}
                onChange={(event) => onFilter(event.target.value as Filter)}
                className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-xs text-[var(--color-ink)]"
              >
                <option value="all">All</option>
                {TASK_STATES.map((state) => (
                  <option key={state} value={state}>
                    {stateView(state).label}
                  </option>
                ))}
              </select>
            </label>
            <RefreshButton onClick={onRefresh} busy={refreshing} />
          </div>
        }
      >
        TASKS AND BACKGROUND ACTIVITY
      </SectionHeading>

      {resource.status === "loading" ? <Loading label="Loading tasks…" /> : null}

      {resource.status === "error" ? (
        <ErrorNotice error={resource.error} title="Tasks could not be loaded." onRetry={onRetry} />
      ) : null}

      {resource.status === "ready" ? (
        <>
          {resource.refreshError ? (
            <ErrorNotice error={resource.refreshError} title="Tasks could not be refreshed." onRetry={onRetry} stale />
          ) : null}
          {resource.data.tasks.length === 0 ? (
            filter === "all" ? (
              <Empty title="No tasks yet">
                Tasks appear here when Mai starts work for you, such as confirmed Gmail monitoring.
              </Empty>
            ) : (
              <Empty title={`No ${stateView(filter).label.toLowerCase()} tasks`}>
                Choose another state, or All, to see the rest.
              </Empty>
            )
          ) : (
            <>
              <ul className="space-y-2">
                {resource.data.tasks.map((task) => {
                  const open = openId === task.id;
                  const end = endMoment(task);
                  return (
                    <li key={task.id} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/60">
                      <button
                        type="button"
                        aria-expanded={open}
                        aria-controls={`task-${task.id}`}
                        onClick={() => onToggle(task.id)}
                        className="flex w-full items-start justify-between gap-3 rounded-xl px-4 py-3 text-left hover:bg-[var(--color-hover)]"
                      >
                        <span className="min-w-0">
                          <span className="line-clamp-2 break-words text-sm">{task.objective}</span>
                          <span className="mt-1 block text-xs text-[var(--color-muted)]">
                            Created <Time iso={task.created_at} now={now} />
                            {end ? (
                              <>
                                {" · "}
                                {end.label} <Time iso={end.at} now={now} />
                              </>
                            ) : null}
                          </span>
                        </span>
                        <StateBadge state={task.state} />
                      </button>
                      {open ? (
                        <div id={`task-${task.id}`} className="border-t border-[var(--color-border)] px-4 py-3">
                          {renderDetail(task)}
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
              <p className="text-xs text-[var(--color-muted)]">
                {resource.data.total > resource.data.tasks.length
                  ? `Showing the ${resource.data.tasks.length} most recent of ${resource.data.total}. Older tasks cannot be listed.`
                  : `${resource.data.tasks.length} ${resource.data.tasks.length === 1 ? "task" : "tasks"}.`}
              </p>
            </>
          )}
        </>
      ) : null}
    </section>
  );
}

export interface TaskDetailViewProps {
  task: Task;
  resource: Resource<TaskDetail>;
  onRetry: () => void;
  /** Re-read the task after a stop attempt. */
  onSettled: () => void;
  now?: Date;
}

export function TaskDetailView({ task, resource, onRetry, onSettled, now }: TaskDetailViewProps) {
  const view = stateView(task.state);
  // From the task row: the backend says whether this is a monitor and what it checks.
  const monitor = summariseMonitor(task);
  return (
    <div className="space-y-3 text-sm">
      <p className="text-xs text-[var(--color-muted)]">{view.meaning}</p>
      <TaskTimes task={task} now={now} />
      <ReasonLine task={task} />
      {task.result ? (
        <div>
          <h4 className="mb-1 text-xs font-medium text-[var(--color-muted)]">Outcome</h4>
          <p className="whitespace-pre-wrap break-words text-xs leading-5">{task.result}</p>
        </div>
      ) : null}

      {monitor ? (
        <>
          <MonitorFacts task={task} summary={monitor} now={now} />
          {monitor.active ? <CancelControl taskId={task.id} onSettled={onSettled} /> : null}
          <LastCheckView resource={resource} onRetry={onRetry} now={now} />
        </>
      ) : (
        <>
          {resource.status === "loading" ? <Loading label="Loading details…" /> : null}
          {resource.status === "error" ? (
            <ErrorNotice error={resource.error} title="Details could not be loaded." onRetry={onRetry} />
          ) : null}
          {resource.status === "ready" ? (
            <>
              {resource.refreshError ? (
                <ErrorNotice error={resource.refreshError} title="Details could not be refreshed." onRetry={onRetry} stale />
              ) : null}
              <StepList steps={resource.data.steps} />
              <EventList events={resource.data.events} now={now} />
            </>
          ) : null}
        </>
      )}
    </div>
  );
}

function TaskDetailPanel({ task, onChanged }: { task: Task; onChanged: () => void }) {
  const { state, reload } = useResource(() => tasks.get(task.id));
  return (
    <TaskDetailView
      task={task}
      resource={state}
      onRetry={() => void reload()}
      onSettled={() => {
        void reload();
        onChanged();
      }}
    />
  );
}

function TaskListLoader({ filter, onFilter }: { filter: Filter; onFilter: (filter: Filter) => void }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const { state, reload } = useResource(() =>
    tasks.list(filter === "all" ? undefined : [filter], LIST_LIMIT),
  );

  // Refresh slowly, and only while something on screen can still change.
  const inProgress =
    state.status === "ready" &&
    state.data.tasks.some((task) => !["completed", "failed", "cancelled"].includes(task.state));
  usePolling(
    async (): Promise<TickResult> => {
      const ok = await reload();
      return ok ? "ok" : "failed";
    },
    POLL_MS,
    inProgress,
  );

  return (
    <TaskListView
      resource={state}
      filter={filter}
      onFilter={onFilter}
      onRetry={() => void reload()}
      onRefresh={() => void reload()}
      openId={openId}
      onToggle={(id) => setOpenId((current) => (current === id ? null : id))}
      renderDetail={(task) => <TaskDetailPanel key={detailKey(task)} task={task} onChanged={() => void reload()} />}
    />
  );
}

export function TaskActivity() {
  const [filter, setFilter] = useState<Filter>("all");
  // Keyed by the filter: changing it is a fresh load, never a stale list.
  return <TaskListLoader key={filter} filter={filter} onFilter={setFilter} />;
}
