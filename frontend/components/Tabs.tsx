"use client";

/**
 * A tab list: one tab in the Tab order, arrow keys move between tabs, and
 * every tab names the panel it controls.
 */

import { type KeyboardEvent, useRef } from "react";

import { nextTabIndex } from "@/lib/tabs";

export interface TabItem {
  id: string;
  label: string;
  /** A count or marker after the label, such as unread items. */
  badge?: string | null;
}

export function tabId(id: string): string {
  return `tab-${id}`;
}

export function panelId(id: string): string {
  return `panel-${id}`;
}

export function Tabs({
  items,
  selected,
  onSelect,
  label,
}: {
  items: TabItem[];
  selected: string;
  onSelect: (id: string) => void;
  label: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  function onKeyDown(event: KeyboardEvent, index: number) {
    const target = nextTabIndex(event.key, index, items.length);
    if (target === null) return;
    event.preventDefault();
    onSelect(items[target].id);
    refs.current[target]?.focus();
  }

  return (
    <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto">
      {items.map((item, index) => {
        const active = item.id === selected;
        return (
          <button
            key={item.id}
            ref={(element) => {
              refs.current[index] = element;
            }}
            type="button"
            role="tab"
            id={tabId(item.id)}
            aria-selected={active}
            aria-controls={panelId(item.id)}
            tabIndex={active ? 0 : -1}
            onClick={() => onSelect(item.id)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={[
              "flex items-center gap-2 whitespace-nowrap rounded-lg px-3 py-1.5 text-xs",
              active
                ? "bg-[var(--color-elevated)] font-medium ring-1 ring-[var(--color-border-strong)]"
                : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-ink)]",
            ].join(" ")}
          >
            {item.label}
            {item.badge ? (
              <span className="rounded-full bg-[var(--color-accent)] px-1.5 text-[10px] text-white">
                {item.badge}
                <span className="sr-only"> unread</span>
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
