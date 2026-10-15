/**
 * A small, safe Markdown parser for assistant replies.
 *
 * It produces a data tree and nothing else. The renderer turns the tree into
 * React elements, so every string reaches the page as a text node and React
 * escapes it: there is no HTML string, no raw-HTML sink and no dependency.
 *
 * What is deliberately not supported, because it is where untrusted text
 * becomes dangerous or expensive:
 *   - Raw HTML is ordinary text. `<script>` is shown, never run.
 *   - Images are never loaded. An image is shown as its alt text: a remote
 *     image is fetched automatically, so an address could carry data out.
 *   - Links are allowed only for http, https and mailto. `javascript:`, `data:`
 *     and relative addresses become plain text.
 *   - Tables, footnotes and reference links are plain text.
 *
 * Work is bounded: nesting depth and the distance searched for a closing
 * delimiter are capped, so hostile input cannot make parsing quadratic.
 */

export type Inline =
  | { type: "text"; text: string }
  | { type: "code"; text: string }
  | { type: "strong"; children: Inline[] }
  | { type: "em"; children: Inline[] }
  | { type: "link"; href: string; children: Inline[] }
  | { type: "br" };

export type Block =
  | { type: "paragraph"; children: Inline[] }
  | { type: "heading"; level: number; children: Inline[] }
  | { type: "code"; lang: string; text: string }
  | { type: "list"; ordered: boolean; start: number; items: Block[][] }
  | { type: "quote"; children: Block[] }
  | { type: "rule" };

/** Longer text is shown as plain text: bounded work for any input. */
export const MAX_MARKDOWN_CHARS = 40_000;
const MAX_BLOCK_DEPTH = 6;
const MAX_INLINE_DEPTH = 4;
/** How far ahead a closing delimiter is searched for. */
const MAX_SPAN = 1_000;
const ALLOWED_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:", "mailto:"]);

/** The address if it is safe to link to, otherwise null. */
export function safeHref(raw: string): string | null {
  // Control characters and spaces are removed before parsing, the way a browser
  // would: `java\tscript:` must not slip past a scheme check.
  const cleaned = raw.replace(/[\u0000- \u007f-\u009f]/g, "");
  if (!cleaned || cleaned.length > 2_000) return null;
  try {
    const url = new URL(cleaned);
    if (!ALLOWED_PROTOCOLS.has(url.protocol)) return null;
    if (url.protocol !== "mailto:" && !url.hostname) return null;
    return url.href;
  } catch {
    return null;
  }
}

// --- Inline ----------------------------------------------------------------------

const ESCAPABLE = "\\`*_{}[]()#+-.!~|>";
const isWord = (char: string | undefined) => char !== undefined && /[A-Za-z0-9]/.test(char);
const isSpace = (char: string | undefined) => char === undefined || /\s/.test(char);

function pushText(out: Inline[], text: string): void {
  if (!text) return;
  const last = out[out.length - 1];
  if (last && last.type === "text") last.text += text;
  else out.push({ type: "text", text });
}

/** Index of the next unescaped `marker` within the span window, or -1. */
function findClose(text: string, from: number, marker: string): number {
  const limit = Math.min(text.length, from + MAX_SPAN);
  for (let i = from; i < limit; i += 1) {
    if (text[i] === "\\") {
      i += 1;
    } else if (text.startsWith(marker, i)) {
      return i;
    }
  }
  return -1;
}

function parseInlineAt(text: string, depth: number): Inline[] {
  const out: Inline[] = [];
  let i = 0;
  while (i < text.length) {
    const char = text[i];

    if (char === "\\" && i + 1 < text.length) {
      if (ESCAPABLE.includes(text[i + 1])) {
        pushText(out, text[i + 1]);
        i += 2;
      } else {
        pushText(out, char);
        i += 1;
      }
      continue;
    }

    if (char === "\n") {
      out.push({ type: "br" });
      i += 1;
      continue;
    }

    if (char === "`") {
      let run = 1;
      while (text[i + run] === "`") run += 1;
      const fence = "`".repeat(run);
      const end = text.indexOf(fence, i + run);
      if (end !== -1 && end - i <= MAX_SPAN) {
        let code = text.slice(i + run, end);
        if (code.length > 1 && code.startsWith(" ") && code.endsWith(" ")) code = code.slice(1, -1);
        out.push({ type: "code", text: code });
        i = end + run;
        continue;
      }
      pushText(out, fence);
      i += run;
      continue;
    }

    // Images are text. Handled before links so `![a](b)` is not read as a link.
    if (char === "!" && text[i + 1] === "[") {
      const close = findClose(text, i + 2, "](");
      const end = close === -1 ? -1 : findClose(text, close + 2, ")");
      if (close !== -1 && end !== -1) {
        pushText(out, `[image: ${text.slice(i + 2, close)}]`);
        i = end + 1;
        continue;
      }
    }

    if (char === "[" && depth < MAX_INLINE_DEPTH) {
      const close = findClose(text, i + 1, "](");
      const end = close === -1 ? -1 : findClose(text, close + 2, ")");
      if (close !== -1 && end !== -1) {
        const label = text.slice(i + 1, close);
        // An optional title after the address is dropped.
        const target = text.slice(close + 2, end).trim().split(/\s+/)[0] ?? "";
        const href = safeHref(target.replace(/^<|>$/g, ""));
        const children = parseInlineAt(label, MAX_INLINE_DEPTH);
        if (href) out.push({ type: "link", href, children });
        else for (const child of children) out.push(child);
        i = end + 1;
        continue;
      }
    }

    if ((char === "h" || char === "H") && !isWord(text[i - 1])) {
      const match = /^https?:\/\/[^\s<>]+/i.exec(text.slice(i, i + 2_100));
      if (match) {
        // Trailing punctuation belongs to the sentence, not the address.
        const bare = match[0].replace(/[.,;:!?)\]'"]+$/, "");
        const href = safeHref(bare);
        if (href) {
          out.push({ type: "link", href, children: [{ type: "text", text: bare }] });
          i += bare.length;
          continue;
        }
      }
    }

    if ((char === "*" || char === "_") && depth < MAX_INLINE_DEPTH) {
      const double = text[i + 1] === char;
      const marker = double ? char + char : char;
      const after = text[i + marker.length];
      const openOk =
        !isSpace(after) && (char === "*" || !isWord(text[i - 1])) && after !== char;
      if (openOk) {
        const close = findCloser(text, i + marker.length, marker);
        if (close !== -1) {
          const inner = parseInlineAt(text.slice(i + marker.length, close), depth + 1);
          out.push({ type: double ? "strong" : "em", children: inner });
          i = close + marker.length;
          continue;
        }
      }
    }

    pushText(out, char);
    i += 1;
  }
  return out;
}

/** A closing `*`/`_` run: not after a space, and not inside a word for `_`. */
function findCloser(text: string, from: number, marker: string): number {
  const char = marker[0];
  let at = from;
  while (true) {
    const found = findClose(text, at, marker);
    if (found === -1) return -1;
    const before = text[found - 1];
    const next = text[found + marker.length];
    const ok =
      found > from &&
      !isSpace(before) &&
      (char === "*" || !isWord(next)) &&
      // Leave `**` to the double-marker search when matching a single marker.
      !(marker.length === 1 && next === char);
    if (ok) return found;
    at = found + marker.length;
    if (at - from > MAX_SPAN) return -1;
  }
}

export function parseInline(text: string): Inline[] {
  return parseInlineAt(text, 0);
}

// --- Blocks ----------------------------------------------------------------------

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([A-Za-z0-9_+.#-]{0,30})[^`]*$/;
const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const BULLET = /^( {0,3})([-*+])[ \t]+(.*)$/;
const ORDERED = /^( {0,3})(\d{1,9})[.)][ \t]+(.*)$/;
const QUOTE = /^ {0,3}>[ ]?(.*)$/;

function startsBlock(line: string): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    BULLET.test(line) ||
    ORDERED.test(line) ||
    QUOTE.test(line)
  );
}

function parseBlocksAt(lines: string[], depth: number): Block[] {
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1];
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // the closing fence, or the end of input for an unclosed one
      blocks.push({ type: "code", lang: fence[2] ?? "", text: body.join("\n") });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length,
        children: parseInline(heading[2]),
      });
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ type: "rule" });
      i += 1;
      continue;
    }

    if (QUOTE.test(line) && depth < MAX_BLOCK_DEPTH) {
      const inner: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        inner.push((QUOTE.exec(lines[i]) as RegExpExecArray)[1]);
        i += 1;
      }
      blocks.push({ type: "quote", children: parseBlocksAt(inner, depth + 1) });
      continue;
    }

    const bullet = BULLET.exec(line);
    const ordered = bullet ? null : ORDERED.exec(line);
    if ((bullet || ordered) && depth < MAX_BLOCK_DEPTH) {
      const isOrdered = ordered !== null;
      const itemPattern = isOrdered ? ORDERED : BULLET;
      const start = ordered ? Number(ordered[2]) : 1;
      const items: Block[][] = [];

      while (i < lines.length) {
        const item = itemPattern.exec(lines[i]);
        if (!item) break;
        const markerWidth = item[0].length - item[3].length;
        const content: string[] = [item[3]];
        i += 1;
        // Continuation: indented lines belong to this item, and so does a
        // blank line when an indented line follows it.
        while (i < lines.length) {
          const next = lines[i];
          if (next.trim() && /^ {2,}\S/.test(next)) {
            content.push(next.slice(Math.min(markerWidth, next.length - next.trimStart().length)));
            i += 1;
          } else if (!next.trim() && i + 1 < lines.length && /^ {2,}\S/.test(lines[i + 1])) {
            content.push("");
            i += 1;
          } else if (next.trim() && !startsBlock(next)) {
            // A lazy continuation of the item's first paragraph.
            content.push(next);
            i += 1;
          } else {
            break;
          }
        }
        items.push(parseBlocksAt(content, depth + 1));
        if (i < lines.length && !lines[i].trim() && itemPattern.test(lines[i + 1] ?? "")) i += 1;
      }
      blocks.push({ type: "list", ordered: isOrdered, start, items });
      continue;
    }

    // A paragraph runs until a blank line or the start of another block.
    const para: string[] = [line.trim()];
    i += 1;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) {
      para.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ type: "paragraph", children: parseInline(para.join("\n")) });
  }

  return blocks;
}

export function parseMarkdown(source: string): Block[] {
  const normalised = source.replace(/\r\n?/g, "\n");
  if (normalised.length > MAX_MARKDOWN_CHARS) {
    const children: Inline[] = [];
    normalised.split("\n").forEach((line, index) => {
      if (index > 0) children.push({ type: "br" });
      if (line) children.push({ type: "text", text: line });
    });
    return [{ type: "paragraph", children }];
  }
  return parseBlocksAt(normalised.split("\n"), 0);
}
