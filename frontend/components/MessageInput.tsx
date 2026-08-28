"use client";

import { useRef, useState } from "react";

interface Props {
  onSend: (content: string) => void;
  disabled: boolean;
}

export function MessageInput({ onSend, disabled }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const canSend = value.trim().length > 0 && !disabled;

  function submit() {
    if (!canSend) return;
    onSend(value.trim());
    setValue("");
    // Reset the auto-grown height after sending.
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter inserts a newline.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function handleChange(event: React.ChangeEvent<HTMLTextAreaElement>) {
    setValue(event.target.value);
    const element = event.target;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
  }

  return (
    <div className="border-t border-[var(--color-border)] bg-[var(--color-canvas)] p-4">
      <div className="mx-auto flex w-full max-w-3xl items-end gap-2">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
          placeholder={disabled ? "Mai is responding…" : "Message Mai…"}
          aria-label="Message Mai"
          className="flex-1 resize-none rounded-xl border border-[var(--color-border)]
                     bg-[var(--color-surface)] px-4 py-3 text-[15px] outline-none
                     placeholder:text-[var(--color-muted)]
                     focus:border-[var(--color-accent)] disabled:opacity-60"
        />
        <button
          type="button"
          onClick={submit}
          disabled={!canSend}
          className="rounded-xl bg-[var(--color-accent)] px-5 py-3 text-[15px]
                     font-medium text-white transition-opacity
                     enabled:hover:opacity-90 disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </div>
  );
}
