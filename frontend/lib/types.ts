/** Shapes returned by the Mai backend. Mirrors the Pydantic schemas. */

export type MessageRole = "user" | "assistant" | "system";

export interface Message {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface ConversationList {
  items: Conversation[];
  total: number;
}

export interface ChatResponse {
  conversation_id: string;
  user_message: Message;
  assistant_message: Message;
}

/** The error envelope every failing endpoint returns. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id: string | null;
  };
}

/**
 * One integration's connection state, as the backend reports it.
 *
 * Carries no credential and cannot: the backend's `ConnectionRead` has no
 * token field, so there is nothing here for the browser to store even by
 * mistake. Scope names are public URLs.
 */
export interface IntegrationStatus {
  integration: string;
  state: string;
  connected: boolean;
  granted_scopes: string[];
  required_scopes: string[];
}

/** The consent URL and the disclosure shown before the user follows it. */
export interface AuthorizationStart {
  authorization_url: string;
  disclosure: string;
}

/**
 * Stage 5C: an export file available to import.
 *
 * Metadata only. There is deliberately no content field and no path: the
 * import directory is server configuration, and archived conversation text
 * never crosses to the browser.
 */
export interface ImportSource {
  filename: string;
  size_bytes: number;
  modified_at: string;
}

export interface ImportSourceList {
  sources: ImportSource[];
  total: number;
}

/**
 * One import run.
 *
 * `redactions` is a count of credential shapes masked on the way in -- never
 * the values. `already_imported` is the idempotency answer: the same file
 * imported twice returns the first run and does nothing.
 */
export interface ImportRun {
  id: string;
  source_filename: string;
  source_fingerprint: string;
  source_bytes: number;
  import_format: string;
  status: string;
  conversations_imported: number;
  messages_imported: number;
  conversations_skipped: number;
  messages_skipped: number;
  redactions: number;
  memories_derived: number;
  error_code: string | null;
  already_imported: boolean;
  created_at: string;
  updated_at: string;
}

export interface ImportRunList {
  runs: ImportRun[];
  total: number;
}

/**
 * Tasks, background activity and monitoring (Stages 6A, 6G, 6N).
 *
 * These mirror the backend's `TaskRead` / `TaskDetail`. `owner_id`, `budget`,
 * `spent` and the stored plan are deliberately not typed here: the UI has no
 * use for them and a field that is not typed cannot be rendered by accident.
 */
export type TaskState =
  | "proposed"
  | "planned"
  | "awaiting_approval"
  | "queued"
  | "running"
  | "paused"
  | "blocked"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  id: string;
  conversation_id: string | null;
  objective: string;
  state: TaskState;
  priority: string;
  deadline: string | null;
  current_step: string | null;
  error_code: string | null;
  result: string | null;
  failure_count: number;
  completed_at: string | null;
  cancelled_at: string | null;
  created_at: string;
  updated_at: string;
  step_count: number;
  /**
   * Monitoring metadata, read from the task row by the backend.
   *
   * `null` means the task is not a monitor. `undefined` (field absent) means
   * this backend does not report monitor metadata at all, which is a different
   * fact and is never treated as "not a monitor".
   */
  monitor?: MonitorMeta | null;
  /** The persisted schedule, unmodified; null means "not scheduled". */
  next_run_at?: string | null;
  /** How many checks have run (committed with their outcome, finished or failed). */
  check_count?: number;
}

/** The shape of a monitor's condition. Every field is null if the stored spec cannot be read. */
export interface MonitorMeta {
  kind: string | null;
  operator: string | null;
  interval_seconds: number | null;
}

export interface TaskList {
  tasks: Task[];
  total: number;
}

export interface TaskStep {
  id: string;
  step_key: string;
  sequence: number;
  title: string;
  state: "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";
  started_at: string | null;
  completed_at: string | null;
}

/** One line of the append-only task journal. */
export interface TaskEvent {
  id: string;
  event_type: string;
  actor: string;
  sequence: number;
  occurred_at: string;
  metadata: Record<string, unknown>;
}

export interface TaskDetail extends Task {
  steps: TaskStep[];
  events: TaskEvent[];
}

/** Stage 6M.2. References and times only: the backend sends no text. */
export interface DeliveryState {
  adapter: string;
  status: "sending" | "delivered" | "failed";
  attempts: number;
  last_attempt_at: string;
  delivered_at: string | null;
  retryable: boolean;
}

export interface TaskNotification {
  id: string;
  task_id: string;
  kind: "condition_met" | "monitoring_failed";
  check_number: number;
  created_at: string;
  read_at: string | null;
  deliveries: DeliveryState[];
}

export interface TaskNotificationList {
  notifications: TaskNotification[];
  next_before: string | null;
}

/** Stage 5F.1 reminders. The list holds scheduled reminders only. */
export interface Reminder {
  id: string;
  text: string;
  state: "scheduled" | "completed" | "cancelled" | "failed";
  recurrence: "once" | "daily" | "weekly";
  next_run_at: string;
  timezone_name: string;
  fire_count: number;
  created_at: string;
}

export interface ReminderList {
  reminders: Reminder[];
  total: number;
}

/** One fired reminder the person has not yet marked seen. */
export interface ReminderNotification {
  id: string;
  reminder_id: string;
  text: string;
  due_at: string;
  delivered_at: string;
}

export interface ReminderNotificationList {
  notifications: ReminderNotification[];
  total: number;
}

/**
 * `GET /health`. `reachability` is observed from the provider's last real
 * request and may be absent from an older backend. `provider` and `model` are
 * optional because the backend does not report them yet (see the audit); the
 * UI shows them only when present.
 */
export interface HealthComponent {
  healthy: boolean;
  detail?: string | null;
  reachability?: "unverified" | "reachable" | "failing" | null;
  provider?: string | null;
  model?: string | null;
}

export interface Health {
  status: string;
  database: HealthComponent;
  llm: HealthComponent;
}
