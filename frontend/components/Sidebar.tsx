"use client";

import { HistoryImportPanel } from "@/components/HistoryImportPanel";
import { IntegrationsPanel } from "@/components/IntegrationsPanel";
import type { Conversation } from "@/lib/types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  disabled: boolean;
}


export function Sidebar({
  conversations,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  disabled,
}: Props) {
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
                <button
                  type="button"
                  onClick={() => onSelect(conversation.id)}
                  aria-current={conversation.id === activeId ? "true" : undefined}
                  className={[
                    "w-full truncate rounded-xl py-2.5 pl-3 pr-8 text-left text-sm",
                    conversation.id === activeId
                      ? "bg-[var(--color-elevated)] font-medium shadow-sm ring-1 ring-[var(--color-border)]"
                      : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-ink)]",
                  ].join(" ")}
                >
                  {conversation.title}
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(conversation.id)}
                  aria-label={`Delete ${conversation.title}`}
                  className="absolute right-1.5 top-1/2 hidden -translate-y-1/2
                             rounded px-1.5 py-0.5 text-xs text-[var(--color-muted)]
                             hover:text-red-500 group-hover:block"
                >
                  ✕
                </button>
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
