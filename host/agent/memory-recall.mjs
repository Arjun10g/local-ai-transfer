// Conversation recall: a deterministic archive of what left the context
// window, searched by keyword when the user asks about it again.
//
// The memory note (memory-note.mjs) asks the model to summarise dropped turns,
// which costs an extra engine call (minutes on a CPU) and loses whatever the
// summary leaves out.  Recall costs no engine call at all: every message the
// host removes from the window is split into short, self-contained entries, and
// at the start of each turn the entries that best match the user's new message
// (BM25 over the entry words) are put back in front of it as a small block of
// quoted lines.  Exact values -- names, numbers, file paths, values inside tool
// results, the later of two corrections -- come back verbatim because nothing
// rewrote them.
//
// Limits, stated plainly: it is lexical.  A question that shares no words with
// the thing it asks about ("who was that person I mentioned?") finds nothing,
// and the block is capped, so many matching facts crowd each other out.  The
// optional note covers the gist; recall covers the exact value.
//
// Everything here is pure.  The controller owns when entries are added and
// when the block is built.  scripts/test/memory_eval.py mirrors the string
// handling below; tests/model/test_memory_recall_parity.py and
// tests/host/memory-recall.test.mjs share tests/model/memory_recall_vectors.json.
import { maskCredentialText } from './argument-summary.mjs';
import { isElidedToolResult, utf8Bytes } from './context-budget.mjs';
import { MEMORY_PROMPTS, collapseSpaces, cutUtf8, isRecallMessage, stripMarkup } from './memory-note.mjs';

export const RECALL_DEFAULTS = Object.freeze({
  // Total archived text (2 MiB: a few hundred turns of everything).  Oldest
  // entries go first beyond it.
  archiveBytes: 2097152,
  // One entry (a sentence, or a slice of one tool result).
  entryBytes: 320,
  // The block put in front of the user's message.  ~300 tokens: a modest cost
  // in an 8,192-token window, paid only on turns that matched something.
  recallBytes: 1280,
  recallEntries: 8,
});
export const RECALL_LABEL = MEMORY_PROMPTS.recall_label;
const MAX_ENTRIES = 24000;
// One pasted file or log must not push the rest of the conversation out.
const MAX_ENTRIES_PER_MESSAGE = 40;
const MAX_LEAVES = 80;
const MIN_ENTRY_CHARS = 8;
const TRUNCATION_MARKER = ' [...]';

export function recallOptions(input = {}) {
  const o = { ...RECALL_DEFAULTS, ...(input ?? {}) };
  const int = (key, min, max) => { if (!Number.isInteger(o[key]) || o[key] < min || o[key] > max) throw new TypeError(`memory.${key} must be an integer in ${min}..${max}`); };
  int('archiveBytes', 4096, 4194304); int('entryBytes', 64, 2048); int('recallBytes', 128, 8192); int('recallEntries', 1, 32);
  if (o.entryBytes > o.recallBytes) throw new TypeError('memory.entryBytes must not exceed memory.recallBytes');
  return Object.freeze(o);
}

// ---- words ------------------------------------------------------------------
// Letters and digits only, so `invoice_total`, `auth-gateway-7790.json` and
// `R-412` all break into the words a person would type.  The parity test pins
// this against Python's `[^\W_]+`.
const WORD = /[\p{L}\p{N}]+/gu;
const STOP = new Set(['a', 'an', 'the', 'is', 'was', 'were', 'are', 'be', 'been', 'am', 'what', 'which', 'who', 'whom', 'whose', 'when', 'where', 'how', 'why', 'do', 'does', 'did', 'for', 'of', 'to', 'in', 'on', 'at', 'by', 'from', 'with', 'about', 'as', 'and', 'or', 'but', 'if', 'then', 'than', 'that', 'this', 'these', 'those', 'it', 'its', 'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she', 'they', 'them', 'there', 'here', 'please', 'tell', 'remind', 'earlier', 'before', 'previously', 'said', 'told', 'mentioned', 'again', 'now', 'can', 'could', 'would', 'will', 'just', 'so', 'not', 'no', 'yes', 'ok', 'okay', 'thanks', 'thank', 'reply', 'only', 'one', 'any', 'all', 'me']);
// Plural and `-ies` endings only: enough for "decision"/"decisions" without a
// stemmer whose mistakes would be hard to reason about.
export function stem(word) {
  if (word.length > 4 && word.endsWith('ies')) return `${word.slice(0, -3)}y`;
  if (word.length > 3 && word.endsWith('s') && !word.endsWith('ss') && !word.endsWith('us') && !word.endsWith('is')) return word.slice(0, -1);
  return word;
}
export function words(text) {
  const out = [];
  for (const match of String(text ?? '').toLowerCase().matchAll(WORD)) {
    const word = match[0];
    if (STOP.has(word)) continue;
    out.push(stem(word));
  }
  return out;
}

// Names and identifiers in a question: a word that is capitalised or has a
// digit, other than the question's first word.  They are what the user is
// asking ABOUT ("Orion", "7790"), so a line must contain one to be returned,
// and a question about a name the archive has never seen finds nothing instead
// of the nearest unrelated lines.
export function anchorWords(text) {
  const out = [];
  [...String(text ?? '').matchAll(WORD)].forEach((match, index) => {
    const raw = match[0]; const lower = raw.toLowerCase();
    if (STOP.has(lower)) return;
    if ((index > 0 && raw.length > 1 && /^\p{Lu}/u.test(raw)) || /\p{N}/u.test(raw)) out.push(stem(lower));
  });
  return [...new Set(out)];
}

// ---- turning messages into entries -------------------------------------------
// Sentence ends: `.`, `!`, `?` followed by space and a capital, digit or quote
// (so `40088.38` and `v1.2 is` stay whole).  Line breaks are handled earlier.
const SENTENCE_BREAK = /(?<=[.!?])\s+(?=[A-Z0-9"'(\[])/u;
const NAME_KEYS = new Set(['path', 'file', 'filename', 'name', 'id', 'url', 'title', 'key', 'workspace_id']);

function clean(text) {
  return collapseSpaces(maskCredentialText(stripMarkup(text)).text).trim();
}
// Prose is split at line breaks BEFORE spaces are collapsed (a list or a
// pasted block is one line per item), then each line into sentences.
function proseLines(text, maxBytes) {
  return stripMarkup(text).split(/[\n\r]+/u).flatMap(line => sentences(clean(line), maxBytes));
}
function bound(text, maxBytes) {
  return utf8Bytes(text) > maxBytes ? cutUtf8(text, maxBytes - utf8Bytes(TRUNCATION_MARKER)).trimEnd() + TRUNCATION_MARKER : text;
}
// Sentences, with a sentence over the bound kept as consecutive slices so a
// long paragraph stays searchable instead of being cut to its first words.
function sentences(text, maxBytes) {
  const out = [];
  for (const part of text.split(SENTENCE_BREAK)) {
    let rest = part.trim();
    while (rest.length >= MIN_ENTRY_CHARS) {
      if (utf8Bytes(rest) <= maxBytes) { out.push(rest); break; }
      let head = cutUtf8(rest, maxBytes); const space = head.lastIndexOf(' ');
      if (!head) break;
      if (space > head.length / 2) head = head.slice(0, space);
      out.push(head.trim()); rest = rest.slice(head.length).trim();
    }
  }
  return out;
}

function leaves(value, prefix, out, depth = 0) {
  if (out.length >= MAX_LEAVES) return;
  if (value === null || value === undefined) return;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') { out.push([prefix, String(value)]); return; }
  if (depth >= 6 || typeof value !== 'object') return;
  const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item]) : Object.entries(value);
  for (const [key, item] of entries) leaves(item, prefix ? `${prefix}.${key}` : key, out, depth + 1);
}

// A tool result that is JSON becomes `key=value` slices that each repeat the
// result's identity (its path or name), so any slice found on its own still
// says which file it came from.  Anything else is split like prose.
function toolEntries(message, entryBytes) {
  const name = clean(message.name ?? 'tool');
  const text = message.content.trim();
  let parsed;
  if (text.startsWith('{') || text.startsWith('[')) { try { parsed = JSON.parse(text); } catch { parsed = undefined; } }
  const label = `Tool result (${name})`;
  if (parsed === undefined || parsed === null || typeof parsed !== 'object') return proseLines(text, entryBytes - utf8Bytes(label) - 2).map(line => `${label}: ${line}`);
  const flat = []; leaves(parsed, '', flat);
  const identity = flat.find(([key]) => NAME_KEYS.has(key.split('.').at(-1)?.toLowerCase()));
  const head = clean(`${label}${identity ? ` ${identity[1]}` : ''}:`);
  const room = Math.max(32, entryBytes - utf8Bytes(head) - 1);
  const out = []; let current = '';
  for (const [key, value] of flat) {
    if (identity && key === identity[0]) continue;
    const pair = clean(`${key}=${value}`); if (!pair) continue;
    const piece = bound(pair, room);
    if (current && utf8Bytes(`${current}; ${piece}`) > room) { out.push(`${head} ${current}`); current = ''; }
    current = current ? `${current}; ${piece}` : piece;
  }
  if (current) out.push(`${head} ${current}`);
  return out;
}

const CALL_NAME = /<function=([^<>\n]{1,96})>/;
const CALL_PARAM = /<parameter=([^<>\n]{1,96})>\s*([\s\S]*?)\s*<\/parameter>/g;
function callEntry(message, entryBytes) {
  const name = CALL_NAME.exec(message.content)?.[1];
  if (!name) return null;
  const params = [...message.content.matchAll(CALL_PARAM)].map(match => `${match[1]}=${match[2]}`).join('; ');
  return bound(clean(`Assistant called ${name}${params ? ` with ${params}` : ''}`), entryBytes);
}

// The searchable lines of one message, oldest first.  Pure: the same message
// always gives the same lines.
export function entriesFromMessage(message, { entryBytes = RECALL_DEFAULTS.entryBytes } = {}) {
  if (!message || typeof message.content !== 'string' || isElidedToolResult(message) || isRecallMessage(message)) return [];
  if (message.role === 'tool') return toolEntries(message, entryBytes);
  if (message.role === 'assistant' && /<tool_call>|<function=/i.test(message.content)) { const line = callEntry(message, entryBytes); return line ? [line] : []; }
  if (message.role !== 'user' && message.role !== 'assistant') return [];
  const label = message.role === 'user' ? 'User' : 'Assistant';
  return proseLines(message.content, entryBytes - utf8Bytes(label) - 2).map(line => `${label}: ${line}`);
}

// ---- the archive ------------------------------------------------------------
const CORRECTION = /\b(?:correction|corrected|actually|instead|no longer|changed|updated|update|now)\b/i;
const K1 = 1.2; const B = 0.75;
// A line must match something this informative (BM25 idf, summed over matched
// words) to be shown, so "thanks!" retrieves nothing.
const MIN_SCORE = 1.0;
const RELATIVE_CUTOFF = 0.35;

export class RecallArchive {
  #entries = [];
  #bytes = 0;
  #seq = 0;
  constructor(options = {}) { this.options = recallOptions(options); }
  get size() { return this.#entries.length; }
  get bytes() { return this.#bytes; }
  add(messages) {
    let added = 0;
    for (const message of messages ?? []) {
      for (const text of entriesFromMessage(message, this.options).slice(0, MAX_ENTRIES_PER_MESSAGE)) {
        const tokens = words(text); if (!tokens.length) continue;
        const bytes = utf8Bytes(text);
        this.#entries.push({ seq: this.#seq++, text, tokens, bytes }); this.#bytes += bytes; added += 1;
      }
    }
    while (this.#entries.length > MAX_ENTRIES || this.#bytes > this.options.archiveBytes) { const old = this.#entries.shift(); this.#bytes -= old.bytes; }
    return added;
  }
  clear() { this.#entries = []; this.#bytes = 0; }
  // The best-matching lines for `queries` ([{text, weight}], newest first),
  // within the byte and count bounds, oldest first.
  search(queries) {
    const entries = this.#entries; const n = entries.length;
    if (!n) return [];
    const weights = new Map();
    for (const { text, weight } of queries) for (const word of new Set(words(text))) weights.set(word, Math.max(weights.get(word) ?? 0, weight));
    if (!weights.size) return [];
    const df = new Map();
    for (const entry of entries) for (const word of new Set(entry.tokens)) if (weights.has(word)) df.set(word, (df.get(word) ?? 0) + 1);
    if (!df.size) return [];
    const anchors = anchorWords(queries[0]?.text); const present = anchors.filter(word => df.has(word));
    if (anchors.length && !present.length) return [];
    const average = entries.reduce((sum, entry) => sum + entry.tokens.length, 0) / n;
    const scored = [];
    for (const entry of entries) {
      const tf = new Map();
      for (const word of entry.tokens) if (weights.has(word)) tf.set(word, (tf.get(word) ?? 0) + 1);
      if (!tf.size || (present.length && !present.some(word => tf.has(word)))) continue;
      let score = 0;
      for (const [word, count] of tf) {
        const idf = Math.log(1 + (n - df.get(word) + 0.5) / (df.get(word) + 0.5));
        score += weights.get(word) * idf * (count * (K1 + 1)) / (count + K1 * (1 - B + B * entry.tokens.length / average));
      }
      if (CORRECTION.test(entry.text)) score *= 1.1;
      scored.push({ entry, score });
    }
    if (!scored.length) return [];
    scored.sort((a, b) => b.score - a.score || b.entry.seq - a.entry.seq);
    const top = scored[0].score;
    // With an anchor, the lines that contain it are the candidates whatever
    // their idf (twenty similar lines about one project all match).
    if (!present.length && top < MIN_SCORE) return [];
    const picked = []; let bytes = utf8Bytes(RECALL_LABEL) + 1;
    for (const { entry, score } of scored) {
      if (picked.length >= this.options.recallEntries || score < top * RELATIVE_CUTOFF || (!present.length && score < MIN_SCORE * 0.5)) break;
      const cost = utf8Bytes(entry.text) + 3;
      if (bytes + cost > this.options.recallBytes) continue;
      picked.push(entry); bytes += cost;
    }
    return picked.sort((a, b) => a.seq - b.seq).map(entry => entry.text);
  }
}

// The block that goes in front of the user's message, or null.
export function recallMessage(lines) {
  if (!lines?.length) return null;
  return { role: 'user', content: `${RECALL_LABEL}\n${lines.map(line => `- ${line}`).join('\n')}` };
}
// The user's message, plus the one before it at half weight so a short
// follow-up ("and the second one?") still finds its subject.
export function recallQueries(message, previousUserMessage) {
  const queries = [{ text: String(message ?? ''), weight: 1 }];
  if (previousUserMessage) queries.push({ text: String(previousUserMessage), weight: 0.5 });
  return queries;
}
