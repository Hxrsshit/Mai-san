/**
 * What an integration's status response proves, and no more.
 *
 * The backend's `connected` means "a stored credential covers the required
 * scopes". It is a local file read: nothing asked Google whether the grant
 * still works. So a present credential is shown as exactly that -- never as a
 * green "connected" -- and the other states Mai cannot observe (recently
 * verified, expired or revoked by Google) are not shown unless the backend
 * itself reports them.
 */

import { isUnreachable } from "./errors";
import type { IntegrationStatus } from "./types";

export type StatusTone = "ok" | "info" | "warn" | "error" | "unknown";

export interface IntegrationView {
  label: string;
  tone: StatusTone;
  detail: string;
  canConnect: boolean;
  canDisconnect: boolean;
  /** A failed read can be retried; a settled answer cannot. */
  canRetry: boolean;
}

/** What the last attempt to read the status produced. */
export type StatusResult =
  | { kind: "loading" }
  | { kind: "ok"; status: IntegrationStatus }
  | { kind: "error"; error: unknown };

function scopeName(scope: string): string {
  const tail = scope.split("/").pop() ?? scope;
  return tail || scope;
}

const NONE = { canConnect: false, canDisconnect: false, canRetry: false };

export function classifyIntegration(result: StatusResult): IntegrationView {
  if (result.kind === "loading") {
    return { ...NONE, label: "Checking…", tone: "unknown", detail: "Asking the Mai backend." };
  }

  if (result.kind === "error") {
    const error = result.error as { code?: string; status?: number } | null;
    const retry = { ...NONE, canRetry: true };
    if (isUnreachable(result.error)) {
      return {
        ...retry,
        label: "Status unknown",
        tone: "unknown",
        detail: "Could not reach the Mai backend, so this integration's state is not known.",
      };
    }
    if (error?.code === "authentication_cancelled") {
      return {
        ...retry,
        label: "Status unknown",
        tone: "unknown",
        detail: "Mai's API token is needed before the status can be read.",
      };
    }
    if (typeof error?.status === "number" && error.status >= 500) {
      return {
        ...retry,
        label: "Temporarily unavailable",
        tone: "warn",
        detail: "The backend failed while reading this status. Try again shortly.",
      };
    }
    return {
      ...retry,
      label: "Status unknown",
      tone: "unknown",
      detail: "The backend did not return a usable status.",
    };
  }

  const { status } = result;
  const granted = status.granted_scopes ?? [];
  const required = status.required_scopes ?? [];
  const missing = required.filter((scope) => !granted.includes(scope));

  switch (status.state) {
    case "not_configured":
      return {
        ...NONE,
        label: "Not configured",
        tone: "unknown",
        detail: "This server has no OAuth client for it, so it cannot be connected from here.",
      };
    case "disabled":
      return {
        ...NONE,
        label: "Disabled",
        tone: "unknown",
        detail: "The server operator switched this integration off.",
      };
    case "authentication_required":
      if (granted.length > 0 && missing.length > 0) {
        return {
          ...NONE,
          canConnect: true,
          label: "Permissions incomplete",
          tone: "warn",
          detail: `Access was granted, but not everything Mai needs (missing: ${missing
            .map(scopeName)
            .join(", ")}). Connect again to grant it.`,
        };
      }
      return {
        ...NONE,
        canConnect: true,
        label: "Not connected",
        tone: "unknown",
        detail: "No account is connected.",
      };
    case "expired":
      return {
        ...NONE,
        canConnect: true,
        canDisconnect: true,
        label: "Authentication expired",
        tone: "error",
        detail: "The stored access has expired. Connect again to renew it.",
      };
    case "unavailable":
      return {
        ...NONE,
        canRetry: true,
        label: "Temporarily unavailable",
        tone: "warn",
        detail: "The backend reports this integration as failing right now.",
      };
    case "configured":
      return {
        ...NONE,
        canConnect: true,
        label: "Configured, not verified",
        tone: "info",
        detail: "Set up on the server, but no working connection has been established.",
      };
    case "available": {
      if (!status.connected) {
        return {
          ...NONE,
          canRetry: true,
          label: "Status unknown",
          tone: "unknown",
          detail: "The backend reports it as available but not connected.",
        };
      }
      if (missing.length > 0) {
        return {
          ...NONE,
          canConnect: true,
          canDisconnect: true,
          label: "Permissions incomplete",
          tone: "warn",
          detail: `Missing: ${missing.map(scopeName).join(", ")}. Connect again to grant it.`,
        };
      }
      return {
        ...NONE,
        canDisconnect: true,
        label: "Credentials present, not verified",
        tone: "info",
        detail:
          `Mai holds stored access (${granted.map(scopeName).join(", ") || "scopes not listed"}). ` +
          "It has not been checked with the provider; if it was revoked, the next real use will fail and say so.",
      };
    }
    default:
      return {
        ...NONE,
        canRetry: true,
        label: "Status unknown",
        tone: "unknown",
        detail: `The backend reported a state this page does not know (${status.state}).`,
      };
  }
}

/** What to tell the person after a disconnect, from the backend's own answer. */
export function disconnectMessage(result: { disconnected: boolean; revoked_remotely: boolean }): string {
  if (!result.disconnected) return "Mai could not remove the stored access. It is still connected.";
  return result.revoked_remotely
    ? "Disconnected, and the provider confirmed the access was withdrawn."
    : "Disconnected here. The provider did not confirm the withdrawal; you can revoke Mai's access in your provider account settings.";
}
