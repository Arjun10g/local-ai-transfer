// The visible conversation as plain data, kept for reload-continuity in
// this TAB's sessionStorage only: it dies with the tab and Reset clears it.
// Conversation content never goes to localStorage.  The host listens on a
// random port per launch, so localStorage would be a fresh origin each run:
// saved copies could never be restored or deleted by a later run, yet would
// stay in the browser profile, readable by whatever local server next gets
// that port.  The project rule is also to store metadata, not prompts or
// responses (AGENTS.md).  Pure apart from the injected storage object, so
// node tests can exercise it with a fake.
import { revealInvisible } from './markdown.js';

export const TRANSCRIPT_KEY = 'bmo.tab_transcript.v1';
// Keys earlier builds wrote to localStorage; purged (best effort) at load.
export const LEGACY_LOCAL_KEYS = Object.freeze(['bmo.transcript.v1', 'bmo.ui.remember_transcript', 'bmo.ui.tools_off', 'bmo.ui.show_deep']);
export const MAX_ITEMS = 300;
const MAX_TEXT = 65536;
const MAX_STORED_CHARS = 1_500_000;
const ROLES = new Set(['user', 'assistant', 'tool', 'note', 'error']);
const SHORT = 256;

const bounded = (value, limit) => typeof value === 'string' ? value.slice(0, limit) : '';

/** Validate one item; returns a clean copy or null.  Storage is untrusted. */
export function normalizeItem(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !ROLES.has(value.role)) return null;
  const item = { role: value.role, text: bounded(value.text, MAX_TEXT), at: Number.isSafeInteger(value.at) && value.at > 0 ? value.at : 0 };
  for (const key of ['name', 'status', 'code', 'action']) if (typeof value[key] === 'string' && value[key]) item[key] = bounded(value[key], SHORT);
  if (typeof value.thinking === 'string' && value.thinking) item.thinking = bounded(value.thinking, MAX_TEXT);
  return item;
}

export function normalizeTranscript(value) {
  const items = Array.isArray(value?.items) ? value.items : [];
  return items.map(normalizeItem).filter(Boolean).slice(-MAX_ITEMS);
}

/** Never throws: private windows, disabled storage, or junk all mean "empty". */
export function loadTranscript(storage) {
  try { const raw = storage?.getItem(TRANSCRIPT_KEY); return raw ? normalizeTranscript(JSON.parse(raw)) : []; } catch { return []; }
}

/** Returns false when the browser refused to store it (quota, disabled). */
export function saveTranscript(storage, items) {
  try {
    let kept = items.map(normalizeItem).filter(Boolean).slice(-MAX_ITEMS);
    let raw = JSON.stringify({ version: 1, items: kept });
    // Drop the oldest messages rather than failing outright on a long session.
    while (raw.length > MAX_STORED_CHARS && kept.length > 1) { kept = kept.slice(Math.ceil(kept.length / 4)); raw = JSON.stringify({ version: 1, items: kept }); }
    storage.setItem(TRANSCRIPT_KEY, raw); return true;
  } catch { return false; }
}

export function clearTranscript(storage) { try { storage?.removeItem(TRANSCRIPT_KEY); return true; } catch { return false; } }

/** Delete what earlier builds left in localStorage; never throws. Returns how many keys were removed. */
export function purgeLegacyLocalStorage(storage) {
  let removed = 0;
  for (const key of LEGACY_LOCAL_KEYS) { try { if (storage?.getItem(key) !== null && storage?.getItem(key) !== undefined) { storage.removeItem(key); removed += 1; } } catch { /* storage blocked: nothing to purge */ } }
  return removed;
}

const quote = text => text.split('\n').map(line => `> ${line}`).join('\n');
const longestRun = (text, char) => { let longest = 0; let run = 0; for (const c of text) { run = c === char ? run + 1 : 0; if (run > longest) longest = run; } return longest; };
// A code span no renderer can break out of (fence longer than any run inside).
function codeSpan(text) { const value = revealInvisible(String(text)).replace(/\n/g, ' '); const ticks = '`'.repeat(longestRun(value, '`') + 1); const pad = value.startsWith('`') || value.endsWith('`') ? ' ' : ''; return `${ticks}${pad}${value}${pad}${ticks}`; }
// Message text is untrusted Markdown: a link or image in it (inline,
// reference-style, autolink, or raw <img>) would be FETCHED or made clickable
// by VS Code, Obsidian, Typora, or GitHub.  Each message therefore goes into a
// fenced block longer than any backtick run inside, so every renderer shows
// it as literal text and nothing in it can link, load, or end the fence.
function fenced(text) { const value = revealInvisible(text); const ticks = '`'.repeat(Math.max(3, longestRun(value, '`') + 1)); return `${ticks}text\n${value}\n${ticks}`; }

/** Export as Markdown that is inert in any renderer. */
export function transcriptToMarkdown(items, { exportedAt = new Date() } = {}) {
  const lines = ['# BMO conversation', '', `Exported ${exportedAt.toISOString()} from this tab. Messages are shown as literal text, so links and images in them are not loaded.`, ''];
  for (const raw of items) {
    const item = normalizeItem(raw); if (!item) continue;
    if (item.role === 'user') lines.push(`**You${item.status === 'not_sent' ? ' (not sent)' : ''}:**`, '', fenced(item.text), '');
    else if (item.role === 'assistant') lines.push('**BMO:**', '', item.text ? fenced(item.text) : '_(no text)_', '');
    else if (item.role === 'tool') lines.push(`> Tool ${codeSpan(item.name ?? 'tool')}: ${codeSpan(item.status ?? 'unknown')}${item.code ? ` (${codeSpan(item.code)})` : ''}`, '');
    else if (item.role === 'error') lines.push(quote(`Error: ${item.text}${item.action ? ` ${item.action}` : ''}${item.code ? ` (code ${codeSpan(item.code)})` : ''}`), '');
    else lines.push(quote(item.text), '');
  }
  return `${lines.join('\n').trimEnd()}\n`;
}
