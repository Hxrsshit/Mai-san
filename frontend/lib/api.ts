/**
 * Typed client for the Mai backend.
 *
 * The base URL is configurable so the same build works against a local
 * backend, a container, or a deployed environment.
 */

import type {
  AuthorizationStart,
  ChatResponse,
  Conversation,
  ConversationDetail,
  ConversationList,
  ImportRun,
  ImportRunList,
  ImportSourceList,
  IntegrationStatus,
} from "./types";

const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

/** An error carrying the backend's error code, so the UI can react to it. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

/** Messages shown to the user for backend error codes we expect to see. */
const FRIENDLY_MESSAGES: Record<string, string> = {
  llm_timeout: "Mai took too long to respond. Please try again.",
  llm_rate_limited: "Mai is handling too many requests right now. Try again shortly.",
  llm_not_configured:
    "Mai has no API key configured. Set GROQ_API_KEY on the backend.",
  llm_auth_error: "The configured API key was rejected. Check GROQ_API_KEY.",
  llm_error: "Mai could not reach the language model. Please try again.",
  llm_invalid_response: "Mai returned an unusable response. Please try again.",
  database_error: "The database is unavailable. Please try again shortly.",
  conversation_not_found: "That conversation no longer exists.",
  // Stage 5C import refusals, in words that say what to do next.
  source_not_found: "That file is no longer in the import folder.",
  invalid_filename: "That file name cannot be used.",
  outside_import_directory: "That file is outside the import folder.",
  unsupported_format: "That is not a ChatGPT export. Use the .zip, or conversations.json.",
  zip_missing_conversations: "That .zip has no conversations.json inside it.",
  malformed_json: "That export could not be read — the JSON is damaged.",
  not_utf8: "That export is not UTF-8 text.",
  source_too_large: "That file is larger than the configured import limit.",
  uncompressed_too_large: "That archive expands to more than the configured limit.",
  history_import_disabled: "History import is switched off on this server.",
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch {
    // fetch only rejects on network-level failures.
    throw new ApiError(
      "Could not reach the Mai backend. Is it running?",
      "network_error",
      0,
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    const code = body?.error?.code ?? "unknown_error";
    const message =
      FRIENDLY_MESSAGES[code] ??
      body?.error?.message ??
      `Request failed (${response.status}).`;
    throw new ApiError(message, code, response.status);
  }

  return body as T;
}

export const api = {
  listConversations: () => request<ConversationList>("/api/conversations"),

  createConversation: (title?: string) =>
    request<Conversation>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null }),
    }),

  getConversation: (id: string) =>
    request<ConversationDetail>(`/api/conversations/${id}`),

  deleteConversation: (id: string) =>
    request<void>(`/api/conversations/${id}`, { method: "DELETE" }),

  sendMessage: (id: string, content: string) =>
    request<ChatResponse>(`/api/conversations/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
};

/**
 * Stage 5C: history import.
 *
 * There is no upload function here, and that is deliberate rather than
 * missing. The backend imports from a directory it already has, so the
 * browser never carries a multi-hundred-megabyte export and the server never
 * needs multipart form parsing. The UI lists what is on the server and asks
 * it to import one by name.
 */
export async function listImportSources(): Promise<ImportSourceList> {
  return request<ImportSourceList>("/api/history/sources");
}

export async function startImport(filename: string): Promise<ImportRun> {
  return request<ImportRun>("/api/history/imports", {
    method: "POST",
    body: JSON.stringify({ filename }),
  });
}

export async function listImportRuns(): Promise<ImportRunList> {
  return request<ImportRunList>("/api/history/imports");
}

export { API_URL };


/**
 * Integration status and connection.
 *
 * Every call goes to the Mai backend. The browser never talks to Google, never
 * holds a token, and never sees one: the OAuth code is exchanged server-side
 * and the credential is written to the backend's own store. What crosses to
 * the browser is a state string, a boolean and the public scope names.
 *
 * Calendar and Gmail are deliberately separate endpoints. Connecting one says
 * nothing about the other, and the UI reflects that rather than presenting a
 * single "Google" switch.
 */
export async function getIntegrationStatus(
  integration: "google" | "gmail",
): Promise<IntegrationStatus> {
  return request<IntegrationStatus>(`/api/integrations/${integration}/status`);
}

export async function beginIntegrationConnect(
  integration: "google" | "gmail",
): Promise<AuthorizationStart> {
  return request<AuthorizationStart>(`/api/integrations/${integration}/connect`, {
    method: "POST",
  });
}

export async function disconnectIntegration(
  integration: "google" | "gmail",
): Promise<{ disconnected: boolean; revoked_remotely: boolean }> {
  return request(`/api/integrations/${integration}/disconnect`, {
    method: "POST",
  });
}
