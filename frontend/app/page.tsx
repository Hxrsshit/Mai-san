"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { InboxView } from "@/components/InboxView";
import { MessageInput } from "@/components/MessageInput";
import { MessageList } from "@/components/MessageList";
import { MonitorsView } from "@/components/MonitorsView";
import { ProviderStatus } from "@/components/ProviderStatus";
import { Sidebar } from "@/components/Sidebar";
import { TaskActivity } from "@/components/TaskActivity";
import { Tabs, panelId, tabId } from "@/components/Tabs";
import { ApiError, api } from "@/lib/api";
import { useInboxBadge } from "@/lib/useInboxBadge";
import type { Conversation, Message } from "@/lib/types";

type View = "chat" | "tasks" | "monitors" | "inbox";

const VIEW_TITLES: Record<Exclude<View, "chat">, string> = {
  tasks: "Tasks and background activity",
  monitors: "Monitors",
  inbox: "Alerts and reminders",
};

export default function ChatPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("chat");
  // Bumped after each chat turn: the provider's observed outcome may have changed.
  const [providerCheck, setProviderCheck] = useState(0);
  const inbox = useInboxBadge(true);
  const refreshBadge = inbox.refresh;
  const firstView = useRef(true);
  // Marking things read happens in the Inbox; re-count when the person moves on.
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    void refreshBadge();
  }, [view, refreshBadge]);

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

  /** Resolves with an error message, or null once the backend returned the new title. */
  async function renameConversation(id: string, title: string): Promise<string | null> {
    try {
      const renamed = await api.renameConversation(id, title);
      setConversations((current) =>
        current.map((item) => (item.id === id ? { ...item, title: renamed.title } : item)),
      );
      return null;
    } catch (caught) {
      return caught instanceof ApiError
        ? caught.message
        : "The conversation could not be renamed. Please try again.";
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
      setProviderCheck((count) => count + 1);
    }
  }

  return (
    <div className="flex h-full overflow-hidden">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={selectConversation}
        onCreate={createConversation}
        onDelete={deleteConversation}
        onRename={renameConversation}
        disabled={isSending}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-canvas)]/65 px-5 py-4 backdrop-blur-xl sm:px-7">
          <div className="min-w-0">
            <p className="mb-1 text-[10px] font-medium tracking-[0.2em] text-[var(--color-muted)]">
              {view === "chat" ? "CONVERSATION" : "ACTIVITY"}
            </p>
            <h1 className="truncate text-sm font-medium">
              {view === "chat"
                ? (conversations.find((item) => item.id === activeId)?.title ?? "A space to think")
                : VIEW_TITLES[view]}
            </h1>
          </div>
          <ProviderStatus refreshKey={providerCheck} />
        </header>

        <div className="border-b border-[var(--color-border)] px-5 py-2 sm:px-7">
          <Tabs
            label="Workspace"
            selected={view}
            onSelect={(id) => setView(id as View)}
            items={[
              { id: "chat", label: "Chat" },
              { id: "tasks", label: "Tasks" },
              { id: "monitors", label: "Monitors" },
              { id: "inbox", label: "Inbox", badge: inbox.badge },
            ]}
          />
        </div>

        {view === "chat" ? (
          <div
            role="tabpanel"
            id={panelId("chat")}
            aria-labelledby={tabId("chat")}
            className="flex min-h-0 flex-1 flex-col"
          >
            {error && (
              <div
                role="alert"
                className="flex items-start justify-between gap-4 border-b
                           border-[color:rgba(255,107,124,.35)] bg-[color:rgba(255,107,124,.1)] px-6 py-3 text-sm text-[var(--color-error)]"
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
          </div>
        ) : (
          <div
            role="tabpanel"
            id={panelId(view)}
            aria-labelledby={tabId(view)}
            className="min-h-0 flex-1 overflow-y-auto"
          >
            <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8">
              {view === "tasks" ? <TaskActivity /> : null}
              {view === "monitors" ? <MonitorsView /> : null}
              {view === "inbox" ? <InboxView onChanged={() => void refreshBadge()} /> : null}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
