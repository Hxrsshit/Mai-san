/**
 * Typed client for the Mai backend.
 *
 * The base URL is configurable so the same build works against a local
 * backend, a container, or a deployed environment.
 */

import type {
  ChatResponse,
  Conversation,
  ConversationDetail,
  ConversationList,
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

export { API_URL };
