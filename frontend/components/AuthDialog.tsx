"use client";

/**
 * The one place the person enters Mai's API token.
 *
 * `lib/api.ts` asks for the token through the provider registered here, and
 * shares that one request among every API call that needs it, so however
 * many calls fail at once this dialog opens once. It replaces
 * `window.prompt`, which some embedded browsers dismiss without showing.
 *
 * What is typed lives in this component's input only until the dialog
 * closes; it is handed to `lib/api.ts`, which keeps it in sessionStorage.
 * Nothing here logs it, puts it in a URL or keeps a second copy.
 *
 * Cancelling stores nothing and erases nothing. Mai then stays locked -- no
 * dialog reopens by itself -- until the person chooses "Enter API token".
 */

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  type TokenRequestReason,
  setTokenProvider,
  signIn,
  subscribeAuthLock,
} from "@/lib/api";

type Pending = {
  reason: TokenRequestReason;
  resolve: (token: string | null) => void;
};

const REASONS: Record<TokenRequestReason, string> = {
  required: "This Mai server requires its API token before it will answer.",
  rejected: "That API token was not accepted. Check it and enter it again.",
};

export function AuthDialog() {
  const [pending, setPending] = useState<Pending | null>(null);
  const [locked, setLocked] = useState(false);
  const [value, setValue] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const unregister = setTokenProvider(
      (reason) =>
        new Promise<string | null>((resolve) => {
          const entry = { reason, resolve };
          pendingRef.current = entry;
          setPending(entry);
        }),
    );
    return () => {
      unregister();
      // Never leave a request waiting on a dialog that no longer exists.
      pendingRef.current?.resolve(null);
      pendingRef.current = null;
    };
  }, []);

  useEffect(() => subscribeAuthLock(setLocked), []);

  useEffect(() => {
    if (pending) inputRef.current?.focus();
  }, [pending]);

  function finish(token: string | null) {
    const entry = pendingRef.current;
    pendingRef.current = null;
    setPending(null);
    setValue("");
    setHint(null);
    entry?.resolve(token);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const token = value.trim();
    if (!token) {
      setHint("Enter the token to continue, or choose Cancel.");
      return;
    }
    if (/\s/.test(token)) {
      setHint("An API token contains no spaces. Paste only the token.");
      return;
    }
    finish(token);
  }

  async function unlock() {
    // Everything that failed while locked loads again with the new token;
    // sessionStorage keeps it across the reload.
    if (await signIn()) window.location.reload();
  }

  return (
    <>
      {locked && !pending && (
        <div
          role="status"
          className="fixed inset-x-0 bottom-24 z-40 mx-auto flex w-fit max-w-[calc(100%-2rem)] items-center gap-3
                     rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-elevated)] px-4 py-2.5 text-sm shadow-lg"
        >
          <span>Mai is locked: its API token is needed for this session.</span>
          <button
            type="button"
            onClick={() => void unlock()}
            className="shrink-0 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-white"
          >
            Enter API token
          </button>
        </div>
      )}

      {pending && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
          <form
            role="dialog"
            aria-modal="true"
            aria-labelledby="mai-auth-title"
            aria-describedby="mai-auth-reason"
            onSubmit={submit}
            onKeyDown={(event) => {
              if (event.key === "Escape") finish(null);
            }}
            className="mai-surface w-full max-w-md rounded-2xl border border-[var(--color-border-strong)]
                       bg-[var(--color-elevated)] p-6 shadow-2xl"
          >
            <h2 id="mai-auth-title" className="mb-2 text-base font-medium">
              Enter your Mai API token
            </h2>
            <p
              id="mai-auth-reason"
              role={pending.reason === "rejected" ? "alert" : undefined}
              className={
                pending.reason === "rejected"
                  ? "mb-3 text-sm text-[var(--color-error)]"
                  : "mb-3 text-sm text-[var(--color-secondary)]"
              }
            >
              {REASONS[pending.reason]}
            </p>
            <p className="mb-4 text-xs text-[var(--color-muted)]">
              It is kept only for this browser session and sent only to the Mai
              backend.
            </p>
            <input
              ref={inputRef}
              type="password"
              aria-label="API token"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              value={value}
              onChange={(event) => {
                setValue(event.target.value);
                setHint(null);
              }}
              className="w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2.5
                         text-sm outline-none focus:border-[var(--color-accent)]"
            />
            {hint && (
              <p role="alert" className="mt-2 text-xs text-[var(--color-error)]">
                {hint}
              </p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => finish(null)}
                className="rounded-lg border border-[var(--color-border)] px-4 py-2 text-sm hover:bg-[var(--color-hover)]"
              >
                Cancel
              </button>
              <button
                type="submit"
                className="rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm text-white"
              >
                Continue
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
