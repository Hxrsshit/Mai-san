"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { MessageInput } from "@/components/MessageInput";
import { MessageList } from "@/components/MessageList";
import { Sidebar } from "@/components/Sidebar";
import { ApiError, api } from "@/lib/api";
import type { Conversation, Message } from "@/lib/types";

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Mirrors activeId so an in-flight request can tell whether the user has
  // since switched conversations. State captured in the closure would be stale.
  const activeConversationRef = useRef<string | null>(null);
  activeConversationRef.current = activeId;

  const reportError = useCallback((caught: unknown) => {
    setError(
      caught instanceof ApiError
        ? caught.message
        : "Something went wrong. Please try again.",
    );
  }, []);

  const refreshConversations = useCallback(async () => {
    try {
      const { items } = await api.listConversations();
      setConversations(items);
      return items;
    } catch (caught) {
      reportError(caught);
      return [];
    }
  }, [reportError]);

  // Load the conversation list once on mount.
  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  const selectConversation = useCallback(
    async (id: string) => {
      setActiveId(id);
      setError(null);
      try {
        const conversation = await api.getConversation(id);
        setMessages(conversation.messages);
      } catch (caught) {
        reportError(caught);
        setMessages([]);
      }
    },
    [reportError],
  );

  async function createConversation() {
    setError(null);
    try {
      const conversation = await api.createConversation();
      setConversations((current) => [conversation, ...current]);
      setActiveId(conversation.id);
      setMessages([]);
    } catch (caught) {
      reportError(caught);
    }
  }

  async function deleteConversation(id: string) {
    setError(null);
    try {
      await api.deleteConversation(id);
      setConversations((current) => current.filter((item) => item.id !== id));
      if (id === activeId) {
        setActiveId(null);
        setMessages([]);
      }
    } catch (caught) {
      reportError(caught);
    }
  }

  async function sendMessage(content: string) {
    // Claim the send slot BEFORE any await. Doing this after the
    // createConversation() call left a window in which a second Enter press
    // passed the disabled check and created a duplicate conversation.
    if (isSending) return;
    setIsSending(true);
    setError(null);

    // Create a conversation on the fly if the user just started typing.
    let conversationId = activeId;
    if (!conversationId) {
      try {
        const conversation = await api.createConversation();
        conversationId = conversation.id;
        setConversations((current) => [conversation, ...current]);
        setActiveId(conversation.id);
      } catch (caught) {
        reportError(caught);
        setIsSending(false);
        return;
      }
    }

    // Narrowing across try/catch is not reliable; make it explicit.
    if (!conversationId) {
      setIsSending(false);
      return;
    }

    // Show the user's message immediately; it is replaced by the stored row.
    const pending: Message = {
      id: `pending-${Date.now()}`,
      conversation_id: conversationId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, pending]);

    try {
      const result = await api.sendMessage(conversationId, content);
      // The user may have switched conversations while this was in flight;
      // applying the reply then would show it under the wrong conversation.
      if (activeConversationRef.current === conversationId) {
        setMessages((current) => [
          ...current.filter((message) => message.id !== pending.id),
          result.user_message,
          result.assistant_message,
        ]);
      }
      // The reply may have auto-titled the conversation and changed its order.
      await refreshConversations();
    } catch (caught) {
      // The turn was rolled back server-side, so drop the optimistic message.
      if (activeConversationRef.current === conversationId) {
        setMessages((current) =>
          current.filter((message) => message.id !== pending.id),
        );
        reportError(caught);
      }
    } finally {
      setIsSending(false);
    }
  }

  return (
    <div className="flex h-full">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={selectConversation}
        onCreate={createConversation}
        onDelete={deleteConversation}
        disabled={isSending}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="border-b border-[var(--color-border)] px-6 py-3">
          <h1 className="text-sm font-semibold">
            {conversations.find((item) => item.id === activeId)?.title ?? "Mai"}
          </h1>
        </header>

        {error && (
          <div
            role="alert"
            className="flex items-start justify-between gap-4 border-b
                       border-red-500/30 bg-red-500/10 px-6 py-3 text-sm text-red-500"
          >
            <span>{error}</span>
            <button
              type="button"
              onClick={() => setError(null)}
              aria-label="Dismiss error"
              className="shrink-0 opacity-70 hover:opacity-100"
            >
              ✕
            </button>
          </div>
        )}

        <MessageList messages={messages} isLoading={isSending} />
        <MessageInput onSend={sendMessage} disabled={isSending} />
      </main>
    </div>
  );
}
