"use client";

import { useEffect, useRef, useState } from "react";

import { HistoryImportPanel } from "@/components/HistoryImportPanel";
import { IntegrationsPanel } from "@/components/IntegrationsPanel";
import type { Conversation } from "@/lib/types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  /** Resolves with an error message, or null once the backend confirmed the new title. */
  onRename: (id: string, title: string) => Promise<string | null>;
  disabled: boolean;
}

/**
 * The title field for one conversation. Enter saves, Escape cancels. The list
 * only changes after the backend accepted the new title, so a failed rename
 * leaves the old title on screen with the reason beside it.
 */
function RenameField({
  conversation,
  onRename,
  onDone,
}: {
  conversation: Conversation;
  onRename: (id: string, title: string) => Promise<string | null>;
  onDone: () => void;
}) {
  const [value, setValue] = useState(conversation.title);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const claimed = useRef(false);

  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, []);

  async function save() {
    if (claimed.current) return;
    const title = value.trim();
    if (!title) {
      setError("A title cannot be blank.");
      return;
    }
    if (title === conversation.title) {
      onDone();
      return;
    }
    claimed.current = true;
    setSaving(true);
    setError(null);
    const failure = await onRename(conversation.id, title);
    claimed.current = false;
    setSaving(false);
    if (failure) setError(failure);
    else onDone();
  }

  return (
    <form
      className="px-1"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <input
        ref={input}
        value={value}
        disabled={saving}
        maxLength={200}
        aria-label={`New title for ${conversation.title}`}
        aria-invalid={error ? true : undefined}
        onChange={(event) => {
          setValue(event.target.value);
          setError(null);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onDone();
          }
        }}
        className="w-full rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-sm outline-none"
      />
      <p className="mt-1 text-[11px] text-[var(--color-muted)]">
        {saving ? "Saving…" : "Enter to save, Escape to cancel."}
      </p>
      {error ? (
        <p role="alert" className="mt-1 text-[11px] text-[var(--color-error)]">
          {error}
        </p>
      ) : null}
    </form>
  );
}


export function Sidebar({
  conversations,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  onRename,
  disabled,
}: Props) {
  const [renamingId, setRenamingId] = useState<string | null>(null);
  return (
    <aside
      className="mai-sidebar flex w-72 shrink-0 flex-col border-r border-[var(--color-border)]
                 bg-[var(--color-surface)]/80 backdrop-blur-xl"
    >
      <div className="border-b border-[var(--color-border)] px-4 py-5">
        <div className="mb-6 flex items-center gap-3">
          <img src="/branding/mai-mark.svg" alt="MAI" className="mai-mark h-9 w-10" />
          <div className="mai-sidebar-copy leading-none">
            <p className="mai-wordmark">MAI</p>
            <p className="mt-1.5 text-[10px] tracking-[0.2em] text-[var(--color-muted)]">PERSONAL AI</p>
          </div>
        </div>
        <button
          type="button"
          onClick={onCreate}
          disabled={disabled}
          className="group flex w-full items-center justify-center gap-2 rounded-xl border border-[var(--color-border)]
                     bg-[var(--color-elevated)] px-3 py-2.5 text-sm font-medium shadow-sm
                     enabled:hover:border-[var(--color-border-strong)] enabled:hover:shadow-[var(--glow-mai)] disabled:opacity-50"
        >
          <span className="text-lg font-light leading-none text-[#a89dff]">+</span><span className="mai-new-label">New conversation</span>
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-4">
        <p className="mai-sidebar-copy mb-2 px-2 text-[10px] font-medium tracking-[0.18em] text-[var(--color-muted)]">CONVERSATIONS</p>
        {conversations.length === 0 ? (
          <p className="px-2 py-4 text-sm text-[var(--color-muted)]">
            No conversations.
          </p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {conversations.map((conversation) => (
            <li key={conversation.id} className="group relative">
              {renamingId === conversation.id ? (
                <RenameField
                  conversation={conversation}
                  onRename={onRename}
                  onDone={() => setRenamingId(null)}
                />
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => onSelect(conversation.id)}
                    aria-current={conversation.id === activeId ? "true" : undefined}
                    className={[
                      "w-full truncate rounded-xl py-2.5 pl-3 pr-14 text-left text-sm",
                      conversation.id === activeId
                        ? "bg-[var(--color-elevated)] font-medium shadow-sm ring-1 ring-[var(--color-border)]"
                        : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-ink)]",
                    ].join(" ")}
                  >
                    {conversation.title}
                  </button>
                  {/* Opacity, not display: a hidden control cannot be reached by keyboard. */}
                  <span className="absolute right-1.5 top-1/2 flex -translate-y-1/2 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100">
                    <button
                      type="button"
                      onClick={() => setRenamingId(conversation.id)}
                      aria-label={`Rename ${conversation.title}`}
                      className="rounded px-1.5 py-0.5 text-xs text-[var(--color-muted)] hover:text-[var(--color-ink)]"
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete(conversation.id)}
                      aria-label={`Delete ${conversation.title}`}
                      className="rounded px-1.5 py-0.5 text-xs text-[var(--color-muted)] hover:text-red-500"
                    >
                      ✕
                    </button>
                  </span>
                </>
              )}
            </li>
            ))}
          </ul>
        )}
      </nav>

      <div className="mai-sidebar-panels"><IntegrationsPanel /><HistoryImportPanel /></div>

      <div className="border-t border-[var(--color-border)] px-4 py-4">
        <p className="mai-sidebar-copy text-[10px] tracking-[0.16em] text-[var(--color-muted)]">MAI · PERSONAL ENVIRONMENT</p>
      </div>
    </aside>
  );
}
