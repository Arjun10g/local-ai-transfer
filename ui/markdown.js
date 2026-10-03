// A deliberately small Markdown subset for assistant output.  Model text is
// untrusted, so this module never produces HTML strings: `parseMarkdown`
// returns a plain AST and `renderMarkdown` turns it into DOM nodes through
// createElement/createTextNode only.  Raw HTML in the source is shown as
// literal text, and links are rendered as text (label plus URL) rather than
// anchors, so no javascript:/data: URL can ever become clickable.
//
// Every scan is linear in the input: delimiter searches cache their last
// answer (searches only move forward), backtick runs are paired through a
// precomputed "next run of the same length" table, and no regex with nested
// or adjacent unbounded quantifiers is applied to whole lines.

export const MAX_MARKDOWN_CHARS = 262144;
const MAX_DEPTH = 4;
const CODE_OPEN = '';
const CODE_CLOSE = '';
const PLACEHOLDER = /(\d+)/g;
const LIST_ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])(?:[ \t]+|$)/;
const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const PUNCTUATION = /[!-/:-@[-`{-~]/;
const ALNUM = /[\p{L}\p{N}]/u;

// Invisible format characters (bidi overrides such as U+202E, zero-width
// spaces/joiners, BOM, soft hyphen) and stray controls can make code DISPLAY
// one command while Copy pastes another.  In code they are replaced by a
// visible marker such as ‹U+202E›, for both display and the Copy payload.
const INVISIBLE = /[\p{Cf}\u00AD\u034F\u115F\u1160\u17B4\u17B5\u3164\uFFA0\u2028\u2029]|[\0-\x08\x0B-\x1F\x7F-\x9F]/gu;
export function revealInvisible(text) {
  return typeof text === 'string' ? text.replace(INVISIBLE, c => `‹U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}›`) : '';
}

const isBlank = line => line.trim() === '';
const leadingSpaces = line => { let n = 0; while (n < line.length && line[n] === ' ') n++; return n; };

function fenceOpen(line) {
  const indent = leadingSpaces(line); if (indent > 3) return null;
  const marker = line[indent]; if (marker !== '`' && marker !== '~') return null;
  let end = indent; while (end < line.length && line[end] === marker) end++;
  if (end - indent < 3) return null;
  const info = line.slice(end).trim();
  // CommonMark: a backtick fence's info string may not contain backticks
  // (otherwise ```inline``` on one line would swallow the rest of the reply).
  if (marker === '`' && info.includes('`')) return null;
  return { marker, length: end - indent, lang: (info.split(/\s/)[0] ?? '').slice(0, 32) };
}
function fenceCloses(line, fence) {
  const indent = leadingSpaces(line); if (indent > 3) return false;
  let end = indent; while (end < line.length && line[end] === fence.marker) end++;
  return end - indent >= fence.length && line.slice(end).trim() === '';
}
function heading(line) {
  const indent = leadingSpaces(line); if (indent > 3) return null;
  let level = 0; while (indent + level < line.length && line[indent + level] === '#') level++;
  if (level < 1 || level > 6) return null;
  const after = line[indent + level];
  if (after !== undefined && after !== ' ' && after !== '\t') return null;
  let text = line.slice(indent + level).trim();
  // Optional closing sequence ("## Title ##"), stripped without a regex.
  let cut = text.length; while (cut > 0 && text[cut - 1] === '#') cut--;
  if (cut === 0) text = ''; else if (cut < text.length && (text[cut - 1] === ' ' || text[cut - 1] === '\t')) text = text.slice(0, cut).trimEnd();
  return { level, text };
}
function listItem(line) {
  const match = LIST_ITEM.exec(line); if (!match) return null;
  const ordered = /\d/.test(match[2]);
  return { indent: match[1].length, ordered, start: ordered ? Number.parseInt(match[2], 10) : 1, contentOffset: match[0].length, content: line.slice(match[0].length) };
}
const quoteLine = line => { const indent = leadingSpaces(line); return indent <= 3 && line[indent] === '>' ? line.slice(indent + (line[indent + 1] === ' ' ? 2 : 1)) : null; };
const startsBlock = line => fenceOpen(line) !== null || heading(line) !== null || HR.test(line) || listItem(line) !== null || quoteLine(line) !== null;

/** Parse Markdown source into a block AST. Never throws for string input. */
export function parseMarkdown(source) {
  const text = typeof source === 'string' ? source : '';
  // Oversized input is shown verbatim rather than parsed.
  if (text.length > MAX_MARKDOWN_CHARS) return [{ type: 'paragraph', children: [{ type: 'text', text }] }];
  const lines = text.replace(/\r\n?/g, '\n').replace(/[]/g, '�').split('\n');
  return parseBlocks(lines, 0);
}

function parseBlocks(lines, depth) {
  const blocks = []; let paragraph = []; let i = 0;
  const flush = () => { if (paragraph.length) { blocks.push({ type: 'paragraph', children: parseInline(paragraph.join('\n'), depth) }); paragraph = []; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = fenceOpen(line);
    if (fence) {
      flush(); const body = []; let closed = false; i++;
      while (i < lines.length) { if (fenceCloses(lines[i], fence)) { closed = true; i++; break; } body.push(lines[i]); i++; }
      blocks.push({ type: 'code_block', lang: revealInvisible(fence.lang), text: revealInvisible(body.join('\n')), closed });
      continue;
    }
    if (isBlank(line)) { flush(); i++; continue; }
    if (HR.test(line)) { flush(); blocks.push({ type: 'hr' }); i++; continue; }
    const head = heading(line);
    if (head) { flush(); blocks.push({ type: 'heading', level: head.level, children: parseInline(head.text, depth) }); i++; continue; }
    if (quoteLine(line) !== null) {
      flush(); const inner = [];
      while (i < lines.length && !isBlank(lines[i])) { const quoted = quoteLine(lines[i]); if (quoted === null && startsBlock(lines[i])) break; inner.push(quoted ?? lines[i]); i++; }
      blocks.push(depth < MAX_DEPTH ? { type: 'blockquote', children: parseBlocks(inner, depth + 1) } : { type: 'paragraph', children: parseInline(inner.join('\n'), depth) });
      continue;
    }
    const item = listItem(line);
    if (item) { flush(); i = parseList(lines, i, item, depth, blocks); continue; }
    paragraph.push(line); i++;
  }
  flush();
  return blocks;
}

function parseList(lines, i, first, depth, blocks) {
  const list = { type: 'list', ordered: first.ordered, start: first.start, items: [] };
  let current = null;
  const finishItem = () => {
    if (!current) return;
    list.items.push(depth < MAX_DEPTH ? parseBlocks(current, depth + 1) : [{ type: 'paragraph', children: parseInline(current.join('\n'), depth) }]);
    current = null;
  };
  let offset = first.contentOffset;
  while (i < lines.length) {
    const line = lines[i];
    const item = listItem(line);
    if (item && item.ordered === list.ordered && item.indent <= first.indent + 1) { finishItem(); current = [item.content]; offset = item.contentOffset; i++; continue; }
    if (item && current === null) break;
    if (isBlank(line)) {
      // A blank line continues the list only if more of it follows.
      let next = i + 1; while (next < lines.length && isBlank(lines[next])) next++;
      if (next >= lines.length) break;
      const following = listItem(lines[next]);
      const continues = (following && following.ordered === list.ordered && following.indent <= first.indent + 1) || leadingSpaces(lines[next]) >= offset;
      if (!continues) break;
      current?.push(''); i++; continue;
    }
    const indent = leadingSpaces(line);
    if (indent >= 2 || (indent >= offset)) { current.push(line.slice(Math.min(indent, offset))); i++; continue; }
    if (item || startsBlock(line)) break;
    // Lazy continuation: an unindented line right after item text.
    if (current.length && current[current.length - 1] !== '') { current.push(line); i++; continue; }
    break;
  }
  finishItem();
  blocks.push(list);
  return i;
}

// ---- inline ---------------------------------------------------------------

function parseInline(text, depth) {
  // Code spans are lifted out first (their contents are never interpreted),
  // and replaced by private-use placeholders so emphasis can still wrap them:
  // "**`npm test`**" must be bold code, not literal asterisks.
  const codes = [];
  const runs = [];
  for (let i = 0; i < text.length;) {
    if (text[i] !== '`') { i++; continue; }
    let j = i; while (j < text.length && text[j] === '`') j++;
    runs.push([i, j - i]); i = j;
  }
  const next = new Array(runs.length).fill(-1); const lastByLength = new Map();
  for (let r = runs.length - 1; r >= 0; r--) { const length = runs[r][1]; next[r] = lastByLength.get(length) ?? -1; lastByLength.set(length, r); }
  let lifted = ''; let cursor = 0;
  for (let r = 0; r < runs.length;) {
    const close = next[r]; if (close === -1) { r++; continue; }
    const [start, length] = runs[r];
    let code = text.slice(start + length, runs[close][0]).replace(/\n/g, ' ');
    if (code.length > 2 && code[0] === ' ' && code[code.length - 1] === ' ' && code.trim() !== '') code = code.slice(1, -1);
    lifted += text.slice(cursor, start) + CODE_OPEN + codes.length + CODE_CLOSE; codes.push(code);
    cursor = runs[close][0] + length; r = close + 1;
  }
  lifted += text.slice(cursor);
  return parseSpans(lifted, depth, codes);
}

// Forward-only search cache: the first match at or after `from` is reused
// for any later `from` up to it, which keeps repeated failed searches linear.
function finder(text, accept) {
  let cachedFrom = -1; let cached = -2;
  return (needle, from) => {
    if (cached !== -2 && from >= cachedFrom && (cached === -1 || cached >= from)) return cached;
    let at = text.indexOf(needle, from);
    while (at !== -1 && !accept(at)) at = text.indexOf(needle, at + 1);
    cachedFrom = from; cached = at; return at;
  };
}

function emitText(nodes, value, codes) {
  if (!value) return;
  PLACEHOLDER.lastIndex = 0; let cursor = 0; let match;
  while ((match = PLACEHOLDER.exec(value)) !== null) {
    if (match.index > cursor) pushText(nodes, value.slice(cursor, match.index));
    nodes.push({ type: 'code', text: revealInvisible(codes[Number(match[1])] ?? '') });
    cursor = match.index + match[0].length;
  }
  if (cursor < value.length) pushText(nodes, value.slice(cursor));
}
function pushText(nodes, value) {
  // Soft line breaks inside a paragraph are kept as explicit breaks: chat
  // answers use single newlines intentionally (addresses, short lines).
  const parts = value.split('\n');
  parts.forEach((part, index) => {
    if (index > 0) nodes.push({ type: 'break' });
    if (!part) return;
    const last = nodes[nodes.length - 1];
    if (last?.type === 'text') last.text += part; else nodes.push({ type: 'text', text: part });
  });
}

function parseSpans(text, depth, codes) {
  const nodes = [];
  const space = at => at < 0 || at >= text.length || /\s/.test(text[at]);
  const word = at => at >= 0 && at < text.length && ALNUM.test(text[at]);
  const finders = {
    '**': finder(text, at => !space(at - 1)),
    '__': finder(text, at => !space(at - 1) && !word(at + 2)),
    '*': finder(text, at => !space(at - 1) && text[at - 1] !== '*' && text[at + 1] !== '*'),
    '_': finder(text, at => !space(at - 1) && text[at - 1] !== '_' && text[at + 1] !== '_' && !word(at + 1)),
    ']': finder(text, () => true),
    ')': finder(text, () => true),
  };
  const special = /[*_[\\]/g;
  let plain = ''; let i = 0;
  const flushPlain = () => { emitText(nodes, plain, codes); plain = ''; };
  const inner = value => depth < MAX_DEPTH ? parseSpans(value, depth + 1, codes) : (() => { const out = []; emitText(out, value, codes); return out; })();
  while (i < text.length) {
    special.lastIndex = i; const found = special.exec(text);
    if (!found) { plain += text.slice(i); break; }
    plain += text.slice(i, found.index); i = found.index;
    const c = text[i];
    if (c === '\\') {
      if (i + 1 < text.length && PUNCTUATION.test(text[i + 1])) { plain += text[i + 1]; i += 2; } else { plain += c; i++; }
      continue;
    }
    if (c === '[') {
      const close = finders[']'](']', i + 1);
      if (close !== -1 && close - i <= 1000 && text[close + 1] === '(') {
        let end = finders[')'](')', close + 2);
        // Allow one level of balanced parentheses inside the URL (wiki links).
        if (end !== -1 && text.slice(close + 2, end).includes('(')) { const next = finders[')'](')', end + 1); if (next !== -1 && next - close <= 2050) end = next; }
        const url = end === -1 ? '' : text.slice(close + 2, end);
        if (end !== -1 && url.length > 0 && url.length <= 2048 && !/[\s]/.test(url)) {
          flushPlain(); nodes.push({ type: 'link', url, children: inner(text.slice(i + 1, close)) }); i = end + 1; continue;
        }
      }
      plain += c; i++; continue;
    }
    // Emphasis: '*' or '_', single or doubled.
    const double = text[i + 1] === c;
    const delimiter = double ? c + c : c;
    // Intraword single delimiters stay literal: snake_case, 2*3*4.
    const opensWord = (c === '_' || !double) && word(i - 1);
    if (!opensWord && !space(i + delimiter.length)) {
      const close = finders[delimiter](delimiter, i + delimiter.length + 1);
      if (close !== -1) {
        flushPlain(); nodes.push({ type: double ? 'strong' : 'em', children: inner(text.slice(i + delimiter.length, close)) });
        i = close + delimiter.length; continue;
      }
    }
    plain += delimiter; i += delimiter.length;
  }
  flushPlain();
  return nodes;
}

// ---- thinking -------------------------------------------------------------

/**
 * Split model reasoning out of the visible answer.  Qwen "thinking" output
 * arrives as <think>…</think>; when the chat template opens the block in the
 * prompt, the reply contains only the closing tag.  `open` is true while a
 * block is still streaming.
 */
export function separateThinking(source) {
  const text = typeof source === 'string' ? source : '';
  const thinking = []; let answer = ''; let rest = text; let open = false;
  const firstOpen = rest.indexOf('<think>'); const firstClose = rest.indexOf('</think>');
  if (firstClose !== -1 && (firstOpen === -1 || firstClose < firstOpen)) { thinking.push(rest.slice(0, firstClose)); rest = rest.slice(firstClose + 8); }
  for (;;) {
    const start = rest.indexOf('<think>');
    if (start === -1) { answer += rest; break; }
    answer += rest.slice(0, start);
    const after = rest.slice(start + 7); const end = after.indexOf('</think>');
    if (end === -1) { thinking.push(after); open = true; break; }
    thinking.push(after.slice(0, end)); rest = after.slice(end + 8);
  }
  return { answer: answer.replace(/^\s*\n/, ''), thinking: thinking.map(part => part.trim()).filter(Boolean).join('\n\n'), open };
}

// ---- DOM rendering ----------------------------------------------------------

/**
 * Render Markdown into a DocumentFragment.  `doc` is the Document to build
 * with (injected so tests can pass a fake); `onCopy(button, text)` is wired to
 * each code block's Copy button.
 */
export function renderMarkdown(source, { document: doc = globalThis.document, onCopy } = {}) {
  const fragment = doc.createDocumentFragment();
  for (const block of parseMarkdown(source)) fragment.append(renderBlock(block, doc, onCopy));
  return fragment;
}

function element(doc, tag, className) { const node = doc.createElement(tag); if (className) node.className = className; return node; }

function renderBlock(block, doc, onCopy) {
  switch (block.type) {
    case 'paragraph': { const p = element(doc, 'p'); appendInline(p, block.children, doc); return p; }
    case 'heading': { const h = element(doc, `h${Math.min(6, block.level + 2)}`, 'md-heading'); appendInline(h, block.children, doc); return h; }
    case 'hr': return element(doc, 'hr');
    case 'blockquote': { const quote = element(doc, 'blockquote'); for (const child of block.children) quote.append(renderBlock(child, doc, onCopy)); return quote; }
    case 'list': {
      const list = element(doc, block.ordered ? 'ol' : 'ul');
      if (block.ordered && block.start !== 1 && Number.isSafeInteger(block.start)) list.setAttribute('start', String(block.start));
      for (const item of block.items) {
        const li = element(doc, 'li');
        // Tight items (one paragraph) render inline so bullets stay compact.
        if (item.length === 1 && item[0].type === 'paragraph') appendInline(li, item[0].children, doc);
        else for (const child of item) li.append(renderBlock(child, doc, onCopy));
        list.append(li);
      }
      return list;
    }
    case 'code_block': {
      const wrapper = element(doc, 'div', 'code-block'); const head = element(doc, 'div', 'code-head');
      const label = element(doc, 'span', 'code-lang'); label.textContent = block.lang || 'code';
      const copy = element(doc, 'button', 'copy-button'); copy.type = 'button'; copy.textContent = 'Copy';
      if (typeof onCopy === 'function') copy.addEventListener('click', () => onCopy(copy, block.text));
      head.append(label, copy);
      const pre = element(doc, 'pre'); const code = element(doc, 'code'); code.textContent = block.text; pre.append(code);
      wrapper.append(head, pre); return wrapper;
    }
    default: { const p = element(doc, 'p'); p.textContent = ''; return p; }
  }
}

function appendInline(parent, nodes, doc) {
  for (const node of nodes) {
    if (node.type === 'text') parent.append(doc.createTextNode(node.text));
    else if (node.type === 'break') parent.append(element(doc, 'br'));
    else if (node.type === 'code') { const code = element(doc, 'code', 'inline-code'); code.textContent = node.text; parent.append(code); }
    else if (node.type === 'strong' || node.type === 'em') { const wrap = element(doc, node.type); appendInline(wrap, node.children, doc); parent.append(wrap); }
    else if (node.type === 'link') {
      // Shown as text on purpose: model-chosen URLs are never clickable.
      const span = element(doc, 'span', 'md-link'); appendInline(span, node.children, doc);
      const url = element(doc, 'span', 'md-link-url'); url.textContent = ` (${node.url})`;
      span.append(url); parent.append(span);
    }
  }
}
