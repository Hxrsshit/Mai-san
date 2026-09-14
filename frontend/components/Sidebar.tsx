"use client";

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
      className="flex w-64 shrink-0 flex-col border-r border-[var(--color-border)]
                 bg-[var(--color-surface)]"
    >
      <div className="p-3">
        <button
          type="button"
          onClick={onCreate}
          disabled={disabled}
          className="w-full rounded-lg border border-[var(--color-border)]
                     bg-[var(--color-canvas)] px-3 py-2 text-sm font-medium
                     transition-colors enabled:hover:border-[var(--color-accent)]
                     disabled:opacity-50"
        >
          + New conversation
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 pb-3">
        {conversations.length === 0 ? (
          <p className="px-2 py-4 text-sm text-[var(--color-muted)]">
            No conversations yet.
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
                    "w-full truncate rounded-lg py-2 pl-3 pr-8 text-left text-sm",
                    conversation.id === activeId
                      ? "bg-[var(--color-canvas)] font-medium"
                      : "hover:bg-[var(--color-canvas)]/60",
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

      <IntegrationsPanel />

      <div className="border-t border-[var(--color-border)] px-4 py-3">
        <p className="text-xs text-[var(--color-muted)]">Mai · Stage 1</p>
      </div>
    </aside>
  );
}
