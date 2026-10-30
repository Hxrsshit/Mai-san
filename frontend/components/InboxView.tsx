"use client";

/**
 * The inbox: monitoring alerts and reminders.
 *
 * Only what the backend recorded is shown. An alert carries no text (the
 * backend sends none), so each one is described from its kind and linked to
 * its task. A reminder "fired" entry means the in-app record was written; it
 * does not prove anyone saw it, which is what "Mark seen" is for. Read and
 * seen states change only after the backend accepted the change.
 */

import { useState } from "react";

import { reminders, taskNotifications, tasks } from "@/lib/api";
import { describeError } from "@/lib/errors";
import { formatAbsolute } from "@/lib/format";
import { appendPage, deliveryText, kindText, markAlertRead, markReminderSeen } from "@/lib/inbox";
import { type Resource, useResource } from "@/lib/resource";
import type {
  Reminder,
  ReminderList,
  ReminderNotification,
  ReminderNotificationList,
  TaskNotification,
  TaskNotificationList,
} from "@/lib/types";

import { StateBadge, Time } from "./TaskParts";
import { Badge, Empty, ErrorNotice, Loading, RefreshButton, SectionHeading } from "./ui";

export const PAGE_SIZE = 20;

// --- Monitoring alerts -----------------------------------------------------------

function AlertTask({ taskId }: { taskId: string }) {
  const { state, reload } = useResource(() => tasks.get(taskId));
  if (state.status === "loading") return <Loading label="Loading the task…" />;
  if (state.status === "error") {
    return (
      <ErrorNotice
        error={state.error}
        title="The task could not be loaded."
        onRetry={() => void reload()}
      />
    );
  }
  const task = state.data;
  return (
    <div className="space-y-1 text-xs">
      <p className="break-words">{task.objective}</p>
      <p className="flex items-center gap-2 text-[var(--color-muted)]">
        Task state now: <StateBadge state={task.state} />
      </p>
    </div>
  );
}

export interface AlertsSectionProps {
  resource: Resource<TaskNotificationList>;
  unreadOnly: boolean;
  onUnreadOnly: (value: boolean) => void;
  onRetry: () => void;
  onRefresh: () => void;
  onMore: () => void;
  loadingMore: boolean;
  moreError: unknown | null;
  onMarkRead: (notification: TaskNotification) => void;
  /** Ids with a mark-read in flight, and ids whose last attempt failed. */
  marking: ReadonlySet<string>;
  markErrors: Readonly<Record<string, string>>;
  now?: Date;
}

export function AlertsSection(props: AlertsSectionProps) {
  const { resource, unreadOnly } = props;
  const [openId, setOpenId] = useState<string | null>(null);
  const refreshing = resource.status === "ready" && resource.refreshing;

  return (
    <section aria-labelledby="alerts-heading" className="space-y-3">
      <SectionHeading
        id="alerts-heading"
        action={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
              <input
                type="checkbox"
                checked={unreadOnly}
                onChange={(event) => props.onUnreadOnly(event.target.checked)}
              />
              Unread only
            </label>
            <RefreshButton onClick={props.onRefresh} busy={refreshing} />
          </div>
        }
      >
        MONITORING ALERTS
      </SectionHeading>

      {resource.status === "loading" ? <Loading label="Loading alerts…" /> : null}
      {resource.status === "error" ? (
        <ErrorNotice error={resource.error} title="Alerts could not be loaded." onRetry={props.onRetry} />
      ) : null}

      {resource.status === "ready" ? (
        <>
          {resource.refreshError ? (
            <ErrorNotice error={resource.refreshError} title="Alerts could not be refreshed." onRetry={props.onRetry} stale />
          ) : null}
          {resource.data.notifications.length === 0 ? (
            unreadOnly ? (
              <Empty title="No unread alerts">Untick “Unread only” to see ones you have already read.</Empty>
            ) : (
              <Empty title="No alerts yet">
                Mai records an alert when a monitor’s condition is met or when a monitor gives up after repeated failures.
              </Empty>
            )
          ) : (
            <ul className="space-y-2">
              {resource.data.notifications.map((alert) => {
                const text = kindText(alert.kind);
                const unread = alert.read_at === null;
                const open = openId === alert.id;
                const busy = props.marking.has(alert.id);
                const failure = props.markErrors[alert.id];
                return (
                  <li
                    key={alert.id}
                    className="space-y-2 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/60 px-4 py-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className={`text-sm ${unread ? "font-semibold" : ""}`}>{text.title}</p>
                        <p className="mt-0.5 text-xs text-[var(--color-muted)]">
                          Check {alert.check_number} · recorded <Time iso={alert.created_at} now={props.now} />
                        </p>
                      </div>
                      <Badge tone={unread ? "info" : "neutral"}>{unread ? "Unread" : "Read"}</Badge>
                    </div>
                    <p className="text-xs leading-5 text-[var(--color-muted)]">{text.meaning}</p>
                    {alert.deliveries.length > 0 ? (
                      <ul aria-label="Delivery" className="text-xs text-[var(--color-muted)]">
                        {alert.deliveries.map((delivery) => (
                          <li key={delivery.adapter}>{deliveryText(delivery)}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-[var(--color-muted)]">Not sent to any outside channel.</p>
                    )}
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        aria-expanded={open}
                        onClick={() => setOpenId(open ? null : alert.id)}
                        className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)]"
                      >
                        {open ? "Hide task" : "Show task"}
                      </button>
                      {unread ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => props.onMarkRead(alert)}
                          className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)] disabled:opacity-50"
                        >
                          {busy ? "Marking…" : "Mark as read"}
                        </button>
                      ) : (
                        <span className="text-xs text-[var(--color-muted)]">
                          Read <Time iso={alert.read_at} now={props.now} />
                        </span>
                      )}
                    </div>
                    {failure ? (
                      <p role="alert" className="text-xs text-[var(--color-error)]">
                        {failure}
                      </p>
                    ) : null}
                    {open ? <AlertTask taskId={alert.task_id} /> : null}
                  </li>
                );
              })}
            </ul>
          )}
          {resource.data.next_before ? (
            <div className="space-y-2">
              {props.moreError ? (
                <ErrorNotice error={props.moreError} title="Older alerts could not be loaded." onRetry={props.onMore} />
              ) : null}
              <button
                type="button"
                disabled={props.loadingMore}
                onClick={props.onMore}
                className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)] disabled:opacity-50"
              >
                {props.loadingMore ? "Loading…" : "Load older alerts"}
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function AlertsLoader({
  unreadOnly,
  onUnreadOnly,
  onChanged,
}: {
  unreadOnly: boolean;
  onUnreadOnly: (v: boolean) => void;
  onChanged: () => void;
}) {
  const { state, reload, update } = useResource(() =>
    taskNotifications.list({ unreadOnly, limit: PAGE_SIZE }),
  );
  const [marking, setMarking] = useState<ReadonlySet<string>>(new Set());
  const [markErrors, setMarkErrors] = useState<Record<string, string>>({});
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown | null>(null);

  async function more() {
    if (state.status !== "ready" || !state.data.next_before || loadingMore) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await taskNotifications.list({
        before: state.data.next_before,
        unreadOnly,
        limit: PAGE_SIZE,
      });
      update((data) => ({
        notifications: appendPage(data.notifications, page.notifications),
        next_before: page.next_before,
      }));
    } catch (error) {
      setMoreError(error);
    } finally {
      setLoadingMore(false);
    }
  }

  async function markRead(alert: TaskNotification) {
    if (marking.has(alert.id)) return;
    setMarking((current) => new Set(current).add(alert.id));
    setMarkErrors(({ [alert.id]: _dropped, ...rest }) => rest);
    const outcome = await markAlertRead(taskNotifications, alert);
    setMarking((current) => {
      const next = new Set(current);
      next.delete(alert.id);
      return next;
    });
    if (outcome.kind === "read") {
      update((data) => ({
        ...data,
        notifications: data.notifications.map((n) => (n.id === alert.id ? outcome.notification : n)),
      }));
      onChanged();
    } else if (outcome.kind === "gone") {
      update((data) => ({ ...data, notifications: data.notifications.filter((n) => n.id !== alert.id) }));
      setMarkErrors((current) => ({ ...current, [alert.id]: "That alert no longer exists." }));
    } else {
      setMarkErrors((current) => ({
        ...current,
        [alert.id]: `Not marked as read: ${describeError(outcome.error).message}`,
      }));
    }
  }

  return (
    <AlertsSection
      resource={state}
      unreadOnly={unreadOnly}
      onUnreadOnly={onUnreadOnly}
      onRetry={() => void reload()}
      onRefresh={() => void reload()}
      onMore={() => void more()}
      loadingMore={loadingMore}
      moreError={moreError}
      onMarkRead={(alert) => void markRead(alert)}
      marking={marking}
      markErrors={markErrors}
    />
  );
}

// --- Reminders -------------------------------------------------------------------

const RECURRENCE: Record<string, string> = { once: "Once", daily: "Every day", weekly: "Every week" };

export interface RemindersSectionProps {
  scheduled: Resource<ReminderList>;
  fired: Resource<ReminderNotificationList>;
  onRetryScheduled: () => void;
  onRetryFired: () => void;
  onRefresh: () => void;
  seen: ReadonlySet<string>;
  marking: ReadonlySet<string>;
  markErrors: Readonly<Record<string, string>>;
  onMarkSeen: (notification: ReminderNotification) => void;
  now?: Date;
}

function unavailable(error: unknown): boolean {
  // The reminders router is only mounted when reminders are switched on.
  return (error as { status?: number })?.status === 404;
}

export function RemindersSection(props: RemindersSectionProps) {
  const refreshing =
    (props.scheduled.status === "ready" && props.scheduled.refreshing) ||
    (props.fired.status === "ready" && props.fired.refreshing);

  return (
    <section aria-labelledby="reminders-heading" className="space-y-4">
      <SectionHeading id="reminders-heading" action={<RefreshButton onClick={props.onRefresh} busy={refreshing} />}>
        REMINDERS
      </SectionHeading>

      <div className="space-y-2">
        <h3 className="text-xs font-medium text-[var(--color-muted)]">Fired, not yet marked seen</h3>
        {props.fired.status === "loading" ? <Loading label="Loading fired reminders…" /> : null}
        {props.fired.status === "error" ? (
          unavailable(props.fired.error) ? (
            <Empty title="Reminders are not available on this server" />
          ) : (
            <ErrorNotice error={props.fired.error} title="Fired reminders could not be loaded." onRetry={props.onRetryFired} />
          )
        ) : null}
        {props.fired.status === "ready" ? (
          <>
            {props.fired.refreshError ? (
              <ErrorNotice error={props.fired.refreshError} title="Fired reminders could not be refreshed." onRetry={props.onRetryFired} stale />
            ) : null}
            {props.fired.data.notifications.length === 0 ? (
              <Empty title="Nothing waiting">No fired reminders are waiting to be marked seen.</Empty>
            ) : (
              <ul className="space-y-2">
                {props.fired.data.notifications.map((item) => {
                  const isSeen = props.seen.has(item.id);
                  const busy = props.marking.has(item.id);
                  const failure = props.markErrors[item.id];
                  return (
                    <li key={item.id} className="space-y-1 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/60 px-4 py-3">
                      <div className="flex items-start justify-between gap-3">
                        <p className={`break-words text-sm ${isSeen ? "" : "font-semibold"}`}>{item.text}</p>
                        <Badge tone={isSeen ? "neutral" : "info"}>{isSeen ? "Seen" : "Fired"}</Badge>
                      </div>
                      <p className="text-xs text-[var(--color-muted)]" title={formatAbsolute(item.due_at)}>
                        Was due <Time iso={item.due_at} now={props.now} />; recorded here{" "}
                        <Time iso={item.delivered_at} now={props.now} />. This is an in-app record, not proof it was seen.
                      </p>
                      {!isSeen ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => props.onMarkSeen(item)}
                          className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs hover:bg-[var(--color-hover)] disabled:opacity-50"
                        >
                          {busy ? "Marking…" : "Mark as seen"}
                        </button>
                      ) : null}
                      {failure ? (
                        <p role="alert" className="text-xs text-[var(--color-error)]">
                          {failure}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
            {props.fired.data.total > props.fired.data.notifications.length ? (
              <p className="text-xs text-[var(--color-muted)]">
                Showing {props.fired.data.notifications.length} of {props.fired.data.total} unseen.
              </p>
            ) : null}
          </>
        ) : null}
      </div>

      <div className="space-y-2">
        <h3 className="text-xs font-medium text-[var(--color-muted)]">Scheduled</h3>
        {props.scheduled.status === "loading" ? <Loading label="Loading scheduled reminders…" /> : null}
        {props.scheduled.status === "error" ? (
          unavailable(props.scheduled.error) ? null : (
            <ErrorNotice error={props.scheduled.error} title="Scheduled reminders could not be loaded." onRetry={props.onRetryScheduled} />
          )
        ) : null}
        {props.scheduled.status === "ready" ? (
          <>
            {props.scheduled.refreshError ? (
              <ErrorNotice error={props.scheduled.refreshError} title="Scheduled reminders could not be refreshed." onRetry={props.onRetryScheduled} stale />
            ) : null}
            {props.scheduled.data.reminders.length === 0 ? (
              <Empty title="No scheduled reminders">Ask Mai in a conversation to remind you of something.</Empty>
            ) : (
              <ul className="space-y-2">
                {props.scheduled.data.reminders.map((reminder: Reminder) => (
                  <li key={reminder.id} className="flex items-start justify-between gap-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/60 px-4 py-3">
                    <div className="min-w-0">
                      <p className="break-words text-sm">{reminder.text}</p>
                      <p className="mt-0.5 text-xs text-[var(--color-muted)]" title={formatAbsolute(reminder.next_run_at, { timeZone: reminder.timezone_name })}>
                        Next due <Time iso={reminder.next_run_at} now={props.now} /> · {RECURRENCE[reminder.recurrence] ?? reminder.recurrence} · fired {reminder.fire_count} {reminder.fire_count === 1 ? "time" : "times"}
                      </p>
                    </div>
                    <Badge tone="info">Scheduled</Badge>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}
      </div>

      <p className="text-xs leading-5 text-[var(--color-muted)]">
        The server lists only scheduled reminders and fired ones you have not marked seen. Reminders that were
        cancelled, completed or failed, and ones you already marked seen, cannot be shown.
      </p>
    </section>
  );
}

function RemindersLoader({ onChanged }: { onChanged: () => void }) {
  const scheduled = useResource(() => reminders.scheduled(PAGE_SIZE));
  const fired = useResource(() => reminders.fired(PAGE_SIZE));
  const [seen, setSeen] = useState<ReadonlySet<string>>(new Set());
  const [marking, setMarking] = useState<ReadonlySet<string>>(new Set());
  const [markErrors, setMarkErrors] = useState<Record<string, string>>({});

  async function markSeen(item: ReminderNotification) {
    if (marking.has(item.id)) return;
    setMarking((current) => new Set(current).add(item.id));
    setMarkErrors(({ [item.id]: _dropped, ...rest }) => rest);
    const outcome = await markReminderSeen(reminders, item.id);
    setMarking((current) => {
      const next = new Set(current);
      next.delete(item.id);
      return next;
    });
    if (outcome.kind === "seen") {
      setSeen((current) => new Set(current).add(item.id));
      onChanged();
    }
    else if (outcome.kind === "gone") setMarkErrors((c) => ({ ...c, [item.id]: "That reminder record no longer exists. Refresh the list." }));
    else setMarkErrors((c) => ({ ...c, [item.id]: `Not marked as seen: ${describeError(outcome.error).message}` }));
  }

  return (
    <RemindersSection
      scheduled={scheduled.state}
      fired={fired.state}
      onRetryScheduled={() => void scheduled.reload()}
      onRetryFired={() => void fired.reload()}
      onRefresh={() => {
        void scheduled.reload();
        void fired.reload();
      }}
      seen={seen}
      marking={marking}
      markErrors={markErrors}
      onMarkSeen={(item) => void markSeen(item)}
    />
  );
}

/** `onChanged` runs after the backend accepted a read or seen, so the tab's count can follow. */
export function InboxView({ onChanged = () => undefined }: { onChanged?: () => void }) {
  const [unreadOnly, setUnreadOnly] = useState(false);
  return (
    <div className="space-y-8">
      {/* Keyed by the filter: changing it is a fresh first page. */}
      <AlertsLoader key={String(unreadOnly)} unreadOnly={unreadOnly} onUnreadOnly={setUnreadOnly} onChanged={onChanged} />
      <RemindersLoader onChanged={onChanged} />
    </div>
  );
}
