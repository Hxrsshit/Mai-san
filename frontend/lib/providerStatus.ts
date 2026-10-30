/**
 * What `/health` proves about the language-model provider.
 *
 * `reachability` is *observed*: the outcome of the provider's last real
 * request. Mai never probes the model to fill this in, so "unverified" is a
 * normal state after a restart and is shown as such, not as failure. The
 * provider name and model are shown only if the backend reports them as
 * fields; they are never scraped out of its prose.
 */

import { isUnreachable } from "./errors";
import type { StatusTone } from "./integrationStatus";
import type { Health } from "./types";

export interface ProviderView {
  label: string;
  tone: StatusTone;
  /** What this means for the person's next message. */
  meaning: string;
  /** The backend's own sentence, verbatim (sanitised server-side). */
  backendDetail: string | null;
  provider: string | null;
  model: string | null;
}

export type ProviderResult =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "error"; error: unknown };

export function classifyProvider(result: ProviderResult): ProviderView {
  const empty = { backendDetail: null, provider: null, model: null };

  if (result.kind === "loading") {
    return { ...empty, label: "Checking…", tone: "unknown", meaning: "Asking the Mai backend." };
  }

  if (result.kind === "error") {
    return isUnreachable(result.error)
      ? {
          ...empty,
          label: "Backend unreachable",
          tone: "error",
          meaning: "Mai cannot answer messages until the backend is running again.",
        }
      : {
          ...empty,
          label: "Status unknown",
          tone: "unknown",
          meaning: "The backend did not return a usable health report.",
        };
  }

  const llm = result.health.llm;
  const shared = {
    backendDetail: llm.detail ?? null,
    provider: llm.provider ?? null,
    model: llm.model ?? null,
  };

  switch (llm.reachability) {
    case "reachable":
      return {
        ...shared,
        label: "Model reachable",
        tone: "ok",
        meaning: "Mai's last request to the model provider succeeded.",
      };
    case "failing":
      return {
        ...shared,
        label: "Model failing",
        tone: "error",
        meaning:
          "Mai's last request to the model provider failed. Your next message will try again; Mai does not switch to another provider on its own.",
      };
    case "unverified":
      return {
        ...shared,
        label: "Not yet verified",
        tone: "info",
        // The backend reports "unverified" both for a configured provider that has
        // not been used yet and for one with no key or model set, so this text
        // must not claim the provider is configured. The backend's own note is
        // shown beside it and says which.
        meaning:
          "No successful request to the model provider has been recorded since the backend started, so it is untested. Read the backend's note below: if it says a key or model is missing, messages will fail until that is configured.",
      };
    default:
      // An older backend: `healthy` then only means "a key is set".
      return llm.healthy
        ? {
            ...shared,
            label: "Configured, not verified",
            tone: "info",
            meaning:
              "A provider is configured, but this backend does not report whether its last request worked.",
          }
        : {
            ...shared,
            label: "Not available",
            tone: "error",
            meaning: "The backend reports the model provider as not usable. Messages will fail until it is fixed.",
          };
  }
}
