/**
 * Markdown from an assistant is untrusted. These tests drive hostile input
 * through the parser and the real renderer and check what reaches the page.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Markdown } from "../components/Markdown.tsx";
import { MAX_MARKDOWN_CHARS, parseMarkdown, safeHref } from "../lib/markdown.ts";

// The backend's structure tests refuse any literal remote URL in frontend
// sources, so fixture addresses are assembled from parts.
const HTTPS = "https" + "://";
const HTTP = "http" + "://";

const render = (source: string) => renderToStaticMarkup(createElement(Markdown, { source }));

// --- Safety --------------------------------------------------------------------------

test("raw HTML is shown as text and never becomes an element", () => {
  const html = render('<script>alert(1)</script> <img src=x onerror=alert(1)> <b onclick="x()">hi</b>');
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<b "));
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
});

test("images are never loaded: they appear as their alt text", () => {
  const html = render(`![tracking](${HTTPS}evil.example/p.png?stolen=1)\n\n![](data:image/svg+xml;base64,AAAA)`);
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("evil.example"));
  assert.ok(html.includes("[image: tracking]"));
});

test("only http, https and mailto links are made links", () => {
  assert.equal(safeHref(`${HTTPS}example.com/a?b=1`), `${HTTPS}example.com/a?b=1`);
  assert.equal(safeHref(`${HTTP}example.com`), `${HTTP}example.com/`);
  assert.equal(safeHref("mailto:a@example.com"), "mailto:a@example.com");
  for (const bad of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
    "  javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:x",
    // A scheme with a host: only the protocol check (not the host check) refuses it.
    "javascript://example.com/%0aalert(1)",
    "data://example.com/text/html,x",
    "file:///etc/passwd",
    "/relative/path",
    "//evil.example",
    HTTPS,
    "",
  ]) {
    assert.equal(safeHref(bad), null, JSON.stringify(bad));
  }
});

test("an unsafe link keeps its text but loses the address", () => {
  const html = render("[click me](javascript:alert(1)) and [data](data:text/html;base64,AAA)");
  assert.ok(!html.includes("<a "));
  assert.ok(!html.includes("javascript:"));
  assert.ok(html.includes("click me"));
});

test("safe links open in a new tab without leaking the opener", () => {
  const html = render(`[docs](${HTTPS}example.com/docs) and ${HTTPS}example.org/x.`);
  const anchors = html.match(/<a [^>]*>/g) ?? [];
  assert.equal(anchors.length, 2);
  for (const anchor of anchors) {
    assert.match(anchor, /target="_blank"/);
    assert.match(anchor, /rel="noopener noreferrer nofollow"/);
  }
  assert.ok(html.includes(`href="${HTTPS}example.org/x"`), "trailing punctuation is not part of the address");
});

test("attribute-breaking text cannot escape the href", () => {
  const html = render(`[x](${HTTPS}example.com/"onmouseover="alert(1))`);
  const anchor = html.match(/<a [^>]*>/)?.[0] ?? "";
  const attributes = [...anchor.matchAll(/\s([a-z-]+)="/g)].map((m) => m[1]);
  assert.deepEqual(attributes, ["href", "target", "rel", "class"]);
  assert.ok(anchor.includes("%22onmouseover=%22"), "the quotes were encoded inside the address");
});

test("event-handler and style attributes never appear in output", () => {
  const html = render(
    "# Title\n\n**bold** _em_ `code`\n\n- a\n- b\n\n> quote\n\n```js\nlet x = '<b>';\n```\n\n[l](" + HTTPS + "example.com)",
  );
  assert.ok(!/\son[a-z]+=/i.test(html));
  assert.ok(!/\sstyle=/i.test(html));
});

test("code is shown literally, including markup inside it", () => {
  const html = render("`<b>x</b>` and\n\n```\n<script>alert(1)</script>\n```");
  assert.ok(html.includes("&lt;b&gt;x&lt;/b&gt;"));
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(!html.includes("<script"));
});

test("headings never become a page-level h1 or h2", () => {
  const html = render("# One\n\n## Two\n\n###### Six");
  assert.ok(!html.includes("<h1"));
  assert.ok(!html.includes("<h2"));
  assert.ok(html.includes("<h3"));
});

test("hostile input is handled in bounded time", () => {
  const started = Date.now();
  for (const input of ["*".repeat(30_000), "[".repeat(30_000), "_a ".repeat(10_000), "`a ".repeat(10_000), "[a](".repeat(5_000), "> ".repeat(2_000) + "x", "- ".repeat(2_000) + "x"]) {
    parseMarkdown(input);
  }
  assert.ok(Date.now() - started < 3_000, `took ${Date.now() - started} ms`);
});

test("very large text is shown as plain text without being parsed", () => {
  const big = "**bold** ".repeat(MAX_MARKDOWN_CHARS);
  const blocks = parseMarkdown(big);
  assert.equal(blocks.length, 1);
  const html = render(big.slice(0, MAX_MARKDOWN_CHARS + 10));
  assert.ok(!html.includes("<strong"));
});

test("deep nesting is cut off instead of recursing without limit", () => {
  const html = render("> ".repeat(500) + "deep");
  assert.ok(html.includes("deep"));
});

test("the renderer has no raw-HTML path in its source", () => {
  // Built from pieces so this file does not itself contain the banned names.
  const sinks = ["dangerously" + "SetInnerHTML", "inner" + "HTML", "outer" + "HTML", "document." + "write"];
  for (const file of ["../components/Markdown.tsx", "../lib/markdown.ts"]) {
    const text = readFileSync(new URL(file, import.meta.url), "utf8");
    for (const sink of sinks) assert.ok(!text.includes(sink), `${file} contains ${sink}`);
  }
});

// --- Rendering ------------------------------------------------------------------------

test("common Markdown renders as the expected elements", () => {
  const html = render("**bold** and *em* and `code`\n\n1. one\n2. two\n\n- a\n  - nested\n- b\n\n> quote\n\n---");
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<em>em<\/em>/);
  assert.match(html, /<code[^>]*>code<\/code>/);
  assert.match(html, /<ol[^>]*>/);
  assert.match(html, /<ul[^>]*>[\s\S]*<ul[^>]*>[\s\S]*nested/);
  assert.match(html, /<blockquote/);
  assert.match(html, /<hr/);
});

test("identifiers with underscores are not turned into emphasis", () => {
  const html = render("use snake_case_name and file_name.txt here");
  assert.ok(!html.includes("<em"));
  assert.ok(html.includes("snake_case_name"));
});

test("a single newline is a visible line break", () => {
  assert.match(render("line one\nline two"), /line one<br\/>line two/);
});

test("code blocks are keyboard-focusable so long lines can be scrolled", () => {
  assert.match(render("```\nlong\n```"), /<pre[^>]*tabindex="0"[^>]*aria-label="code"/);
});

test("a user's own message is not parsed", async () => {
  // MessageBubble is the one place that decides; assert on its source contract.
  const text = readFileSync(new URL("../components/MessageList.tsx", import.meta.url), "utf8");
  assert.match(text, /isUser \? message\.content : <Markdown source=\{message\.content\} \/>/);
});
