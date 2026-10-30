/**
 * Renders the safe Markdown tree from `lib/markdown.ts`.
 *
 * Every string below becomes a React text node, which React escapes. There is
 * no raw-HTML path in this file, and links open in a new tab with
 * `noopener noreferrer nofollow`. Only assistant text goes through here.
 */

import type { ReactNode } from "react";

import { type Block, type Inline, parseMarkdown } from "@/lib/markdown";

function renderInline(nodes: Inline[]): ReactNode[] {
  return nodes.map((node, index) => {
    switch (node.type) {
      case "text":
        return node.text;
      case "br":
        return <br key={index} />;
      case "code":
        return (
          <code key={index} className="rounded bg-[var(--color-hover)] px-1 py-0.5 font-mono text-[0.9em]">
            {node.text}
          </code>
        );
      case "strong":
        return <strong key={index}>{renderInline(node.children)}</strong>;
      case "em":
        return <em key={index}>{renderInline(node.children)}</em>;
      case "link":
        return (
          <a
            key={index}
            href={node.href}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="break-all text-[var(--color-secondary)] underline underline-offset-2"
          >
            {renderInline(node.children)}
          </a>
        );
    }
  });
}

const HEADING_CLASS = "mt-1 font-semibold leading-snug";

function renderBlocks(blocks: Block[]): ReactNode[] {
  return blocks.map((block, index) => {
    switch (block.type) {
      case "paragraph":
        return <p key={index}>{renderInline(block.children)}</p>;
      case "heading": {
        // Page headings stay unique: a reply's `#` becomes an h3, never an h1.
        const Tag = `h${Math.min(block.level + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
        return (
          <Tag key={index} className={HEADING_CLASS}>
            {renderInline(block.children)}
          </Tag>
        );
      }
      case "code":
        return (
          <pre
            key={index}
            tabIndex={0}
            aria-label={block.lang ? `${block.lang} code` : "code"}
            className="overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-canvas)] p-3 font-mono text-[13px] leading-5"
          >
            <code>{block.text}</code>
          </pre>
        );
      case "list": {
        const List = block.ordered ? "ol" : "ul";
        return (
          <List
            key={index}
            start={block.ordered ? block.start : undefined}
            className={`${block.ordered ? "list-decimal" : "list-disc"} space-y-1 pl-5`}
          >
            {block.items.map((item, itemIndex) => (
              <li key={itemIndex} className="space-y-1">
                {renderBlocks(item)}
              </li>
            ))}
          </List>
        );
      }
      case "quote":
        return (
          <blockquote key={index} className="space-y-2 border-l-2 border-[var(--color-border-strong)] pl-3 text-[var(--color-muted)]">
            {renderBlocks(block.children)}
          </blockquote>
        );
      case "rule":
        return <hr key={index} className="border-[var(--color-border)]" />;
    }
  });
}

export function Markdown({ source }: { source: string }) {
  return <div className="space-y-3 break-words">{renderBlocks(parseMarkdown(source))}</div>;
}
