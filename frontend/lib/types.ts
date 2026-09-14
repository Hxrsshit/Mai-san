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
