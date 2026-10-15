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
  // Stage 6M.4 authentication.
  authentication_required: "Mai needs its API token for this session.",
  invalid_credentials: "That API token was not accepted.",
  authentication_cancelled:
    "Mai needs its API token for this session. Choose “Enter API token” to continue.",
  authentication_not_configured:
    "The backend has no API token configured. Set MAI_API_TOKEN_SHA256.",
};

/**
 * Stage 6M.4: the API token, entered by the person and kept only for this
 * browser session (sessionStorage). It is never part of the build, never in
 * NEXT_PUBLIC_* configuration, a cookie, localStorage or a URL, never logged,
 * and sent only to the Mai backend, as `Authorization: Bearer`.
 *
 * Asking for it is shared: however many requests fail at once, one entry is
 * open at a time and every one of them waits for its answer. A token is
 * cleared only when the backend rejected that very token and it is still the
 * stored one, so a late reply to an old request cannot erase a newer token,
 * and a cancelled or empty entry leaves storage as it was. After a cancel
 * nothing asks again on its own: requests fail with `authentication_cancelled`
 * until the person chooses to sign in (`signIn`).
 */
const TOKEN_KEY = "mai.apiToken";

/** Retries one request may make after authentication failures. */
const MAX_AUTH_RETRIES = 3;

/** Why the token is asked for: none was accepted yet, or one was rejected. */
export type TokenRequestReason = "required" | "rejected";

/**
 * Shows the token entry UI; resolves with what was entered, or null if the
 * person cancelled. Registered by `AuthDialog`.
 */
export type TokenProvider = (reason: TokenRequestReason) => Promise<string | null>;

let tokenProvider: TokenProvider | null = null;
let pendingEntry: Promise<boolean> | null = null;
let locked = false;
const lockListeners = new Set<(locked: boolean) => void>();

/** Register the token entry UI. Returns the function that unregisters it. */
export function setTokenProvider(provider: TokenProvider): () => void {
  tokenProvider = provider;
  return () => {
    if (tokenProvider === provider) tokenProvider = null;
  };
}

/** Follow whether Mai is waiting for the person to choose to sign in. */
export function subscribeAuthLock(listener: (locked: boolean) => void): () => void {
  lockListeners.add(listener);
  listener(locked);
  return () => {
    lockListeners.delete(listener);
  };
}

function setLocked(next: boolean): void {
  if (locked === next) return;
  locked = next;
  lockListeners.forEach((listener) => listener(next));
}

function storedToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function storeToken(token: string): boolean {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
    return true;
  } catch {
    return false;
  }
}

/** Forget `rejected` -- but only while it is still the stored token. */
function forgetTokenIf(rejected: string): void {
  // Through `storedToken`, the one read of the token, so storage keeps
  // exactly one call site per operation (pinned by the backend structure
  // tests). Read, compare and remove run without an await in between.
  if (storedToken() !== rejected) return;
  try {
    window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage unavailable: nothing was kept.
  }
}

async function askForToken(reason: TokenRequestReason): Promise<boolean> {
  try {
    const entered = tokenProvider ? await tokenProvider(reason) : null;
    const token = entered?.trim();
    // A token never contains whitespace; the backend refuses one that does.
    if (token && !/\s/.test(token) && storeToken(token)) {
      setLocked(false);
      return true;
    }
  } catch {
    // An entry UI that fails counts as a cancel.
  }
  setLocked(true);
  return false;
}

/** The one token entry in progress, shared by every request that needs it. */
function enterToken(reason: TokenRequestReason): Promise<boolean> {
  if (!pendingEntry) {
    const entry = askForToken(reason);
    pendingEntry = entry;
    void entry.finally(() => {
      if (pendingEntry === entry) pendingEntry = null;
    });
  }
  return pendingEntry;
}

/**
 * Ask for the token because the person chose to ("Enter API token"). Lifts
 * the lock a cancel left. True if a token was entered and kept.
 */
export function signIn(): Promise<boolean> {
  setLocked(false);
  return enterToken("required");
}

const AUTH_FAILURES = new Set(["authentication_required", "invalid_credentials"]);

function cancelled(): ApiError {
  return new ApiError(
    FRIENDLY_MESSAGES.authentication_cancelled,
    "authentication_cancelled",
    401,
  );
}

async function send(
  path: string,
  init: RequestInit | undefined,
  token: string | null,
): Promise<Response> {
  try {
    return await fetch(`${API_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...init?.headers,
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      cache: "no-store",
    });
  } catch {
    // fetch only rejects on network-level failures. The token is kept.
    throw new ApiError(
      "Could not reach the Mai backend. Is it running?",
      "network_error",
      0,
    );
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  for (let retries = 0; ; retries += 1) {
    const sent = storedToken();
    const response = await send(path, init, sent);

    if (response.status === 204) {
      return undefined as T;
    }

    const body = await response.json().catch(() => null);
    if (response.ok) {
      return body as T;
    }

    const code: string = body?.error?.code ?? "unknown_error";
    const authFailure = response.status === 401 && AUTH_FAILURES.has(code);
    // Only the backend's verdict on this very token clears it: a request
    // sent without one proves nothing about the stored token.
    if (authFailure && code === "invalid_credentials" && sent !== null) {
      forgetTokenIf(sent);
    }
    if (authFailure && retries < MAX_AUTH_RETRIES) {
      // Another request already brought a newer token: use it, ask nothing.
      const current = storedToken();
      if (current !== null && current !== sent) {
        continue;
      }
      if (locked) {
        throw cancelled();
      }
      const reason = code === "invalid_credentials" ? "rejected" : "required";
      if (await enterToken(reason)) {
        continue;
      }
      throw cancelled();
    }

    const message =
      FRIENDLY_MESSAGES[code] ??
      body?.error?.message ??
      `Request failed (${response.status}).`;
    throw new ApiError(message, code, response.status);
  }
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
