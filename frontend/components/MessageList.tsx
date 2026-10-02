"use client";

import { useEffect, useRef } from "react";
import type { Message } from "@/lib/types";

interface Props {
  messages: Message[];
  isLoading: boolean;
}

export function MessageList({ messages, isLoading }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, isLoading]);

  if (messages.length === 0 && !isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <div className="max-w-md text-center">
          <img src="/branding/mai-mark.svg" alt="" className="mai-logo-pulse mai-mark mx-auto mb-6 h-24 w-28" />
          <p className="mai-wordmark mb-3">MAI</p>
          <h2 className="mb-2 text-xl font-medium">Your personal AI, present.</h2>
          <p className="text-sm leading-6 text-[var(--color-muted)]">Start a conversation and Mai will help you think, remember, and move forward.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-5 py-8 sm:px-8">
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {isLoading && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";

  return (
    <div className={isUser ? "flex justify-end" : "flex justify-start"}>
      <div
        className={[
          "max-w-[82%] rounded-2xl px-4 py-3 text-[15px] leading-relaxed shadow-sm",
          "whitespace-pre-wrap break-words",
          isUser
            ? "bg-[var(--color-accent)] text-white shadow-[0_8px_22px_rgba(100,88,255,.22)]"
            : "mai-surface border border-[var(--color-border)] text-[var(--color-ink)]",
        ].join(" ")}
      >
        {message.content}
      </div>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex justify-start" aria-live="polite" aria-label="Mai is typing">
      <div className="mai-surface flex gap-1.5 rounded-2xl border border-[var(--color-border)] px-4 py-3.5">
        {[0, 150, 300].map((delay) => (
          <span
            key={delay}
            className="mai-dot h-1.5 w-1.5 rounded-full bg-[#9e93ff]"
            style={{ animationDelay: `${delay}ms` }}
          />
        ))}
      </div>
    </div>
  );
}
