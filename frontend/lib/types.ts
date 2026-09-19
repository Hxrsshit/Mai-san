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
