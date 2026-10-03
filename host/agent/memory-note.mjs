// Opt-in conversation memory: a short, bounded note that summarises turns the
// context window can no longer hold.
//
// Without it the host keeps a conversation inside the window only by eliding
// old tool results and dropping whole oldest turns (context-budget.mjs), so a
// fact said in a dropped turn is simply gone.  With `memory.mode: 'summary'`
// the content that is about to leave the window is summarised by ONE extra
// engine call into a note that is pinned at the start of every later prompt.
//
// Everything here is a pure function.  The controller owns WHEN the note is
// built (between turns, so the user's next turn does not wait on it) and the
// session state; this module owns WHAT the note may contain and how big it may
// get.  The prompt text lives in memory-prompts.json, which the Python
// evaluator (scripts/test/memory_eval.py) reads too, so the prompt that is
// measured is the prompt that ships.  Any change to the string handling here
// must be mirrored there; tests/model/test_memory_eval.py checks the two
// render identically.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { CONTEXT_DEFAULTS, fitHistory, droppableHeadLength, isElidedToolResult, utf8Bytes } from './context-budget.mjs';
import { maskCredentialText } from './argument-summary.mjs';

export const MEMORY_PROMPTS_URL = new URL('./memory-prompts.json', import.meta.url);
const PROMPTS_BYTES = readFileSync(MEMORY_PROMPTS_URL);
// The digest of the exact bytes on disk: the evaluator records the same
// digest in its receipts, so a measured prompt can be matched to a build.
export const MEMORY_PROMPTS_SHA256 = createHash('sha256').update(PROMPTS_BYTES).digest('hex');
export const MEMORY_PROMPTS = deepFreeze(validatePrompts(JSON.parse(PROMPTS_BYTES.toString('utf8'))));

function deepFreeze(value) { if (value && typeof value === 'object') { for (const item of Object.values(value)) deepFreeze(item); Object.freeze(value); } return value; }
function validatePrompts(prompts) {
  const strings = ['version', 'system', 'user_template', 'output_instruction', 'empty_note', 'note_label', 'recall_label', 'note_message_template'];
  for (const key of strings) if (typeof prompts?.[key] !== 'string' || !prompts[key]) throw new TypeError(`memory-prompts.json: ${key} must be a non-empty string`);
  for (const key of ['user', 'assistant', 'assistant_tool_call', 'tool', 'line_template', 'separator', 'truncation_marker']) if (typeof prompts.excerpt?.[key] !== 'string') throw new TypeError(`memory-prompts.json: excerpt.${key} must be a string`);
  if (!(Number.isInteger(prompts.bytes_per_word) && prompts.bytes_per_word >= 3 && prompts.bytes_per_word <= 16)) throw new TypeError('memory-prompts.json: bytes_per_word must be an integer in 3..16');
  for (const placeholder of ['{note}', '{excerpt}', '{output_instruction}']) if (!prompts.user_template.includes(placeholder)) throw new TypeError(`memory-prompts.json: user_template lacks ${placeholder}`);
  return prompts;
}

// off: plain dropping.  recall: dropped turns are archived and searched by
// keyword when the user asks about them again (memory-recall.mjs; no engine
// call).  summary: recall plus the model-written note below.
export const MEMORY_MODES = Object.freeze(['off', 'recall', 'summary']);
// Recall is the default: it adds no engine call, only a small bounded block
// on turns that matched something.  The note stays opt-in ('summary'): on a
// laptop CPU its extra call costs minutes of prefill and decode.
export const MEMORY_DEFAULTS = Object.freeze({
  mode: 'recall',
  // ~170 words: room for a few dozen short facts, small enough to be a
  // modest fixed cost in every later prompt of an 8,192-token window.  (The
  // first real-model measurement, with 256 tokens / 768 bytes, truncated five
  // of six notes; see docs/research/CONTEXT_MEMORY_EVALUATION.md.)
  noteTokens: 512,
  noteBytes: 1536,
  // What one summarisation call may read.  Bounded so the extra call's
  // prefill on a CPU stays in the low minutes, whatever was dropped.
  maxInputBytes: 6144,
  perMessageBytes: 1536,
  // Dropped content waiting for the next summarisation (a turn that compacted
  // before a note was ready).  Oldest is discarded first beyond this.
  backlogBytes: 16384,
  timeoutMs: 600000,
  // How long a new turn waits for an unfinished summary before cancelling it
  // and falling back to plain dropping.  0: the user never waits for it.
  waitMs: 0,
  // Plan the summary when the history is within this many tokens of the
  // turn-start trigger, so it is ready when the next turn starts instead of
  // being needed synchronously.
  planHeadroomTokens: 512,
  // A planned compaction cuts the history to this share of the budget (plain
  // turn-start compaction cuts to 50%).  Lower because the note keeps the
  // facts of what is cut, and because every compaction costs a summary call
  // AND a full re-prefill on a CPU, so fewer, larger ones are cheaper.
  planTargetPercent: 40,
});

// Option names are the controller's camelCase; config.mjs maps snake_case.
export function memoryOptions(input = {}) {
  const o = { ...MEMORY_DEFAULTS, ...(input ?? {}) };
  if (!MEMORY_MODES.includes(o.mode)) throw new TypeError('memory.mode must be off, recall or summary');
  const int = (key, min, max) => { if (!Number.isInteger(o[key]) || o[key] < min || o[key] > max) throw new TypeError(`memory.${key} must be an integer in ${min}..${max}`); };
  int('noteTokens', 32, 1024); int('noteBytes', 128, 4096); int('maxInputBytes', 512, 32768); int('perMessageBytes', 128, 8192);
  int('backlogBytes', 0, 262144); int('timeoutMs', 1000, 3600000); int('waitMs', 0, 600000); int('planHeadroomTokens', 0, 8192); int('planTargetPercent', 20, 60);
  if (o.perMessageBytes > o.maxInputBytes) throw new TypeError('memory.perMessageBytes must not exceed memory.maxInputBytes');
  return Object.freeze(o);
}

// One pass: a value that itself contains `{note}` is never re-expanded, so
// untrusted excerpt text cannot pull the note (or anything else) into itself.
export function fillTemplate(template, values) {
  return template.replace(/\{([a-z_]+)\}/g, (whole, key) => (Object.hasOwn(values, key) ? String(values[key]) : whole));
}

// ---- sanitising untrusted and model-written text ----------------------------
// Control and invisible format characters (bidi overrides, zero-width
// joiners) are removed first, so a zero-width space inside a tag cannot hide it from
// the patterns below.  Newline and tab survive.
const INVISIBLE = /[\p{Cc}\p{Cf}]/gu;
// `<|im_start|>`, `<|endoftext|>` and any other `<|...|>` control token.
const CONTROL_TOKEN = /<\|[^<>|]{0,64}\|>/g;
// The chat template's own markup.  A tag cut off at the end of the text
// (`...<tool_call`) counts too.
const MARKUP_TAG = /<[ \t\n\r\f\v]*\/?[ \t\n\r\f\v]*(?:tool_call|tool_response|tools|think|function|parameter)\b[^<>]{0,256}(?:>|$)/gi;
const CONTROL_PIECE = /<\||\|>|\b(?:im_start|im_end|endoftext)\b/gi;
// Explicit whitespace set, identical in the Python evaluator: JavaScript's
// `\s` and Python's `\s` disagree on a few code points.
const SPACE_CODES = [0x20, 0x09, 0x0a, 0x0d, 0x0c, 0x0b, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000];
const SPACES = new Set(SPACE_CODES.map(code => String.fromCharCode(code)));
export function collapseSpaces(text) {
  let out = ''; let run = false;
  for (const ch of text) { if (SPACES.has(ch)) { if (!run) out += ' '; run = true; } else { out += ch; run = false; } }
  return out;
}

// Removes everything that could read as chat-template structure.  Looped to a
// fixed point so removing one tag cannot assemble another
// (`<tool_<think>call>` -> `<tool_call>`).  The engine also defuses these
// tokens, but the host must never STORE them: the note is replayed into every
// later prompt.
export function stripMarkup(text) {
  let out = String(text ?? '').replace(INVISIBLE, ch => (ch === '\n' || ch === '\t' ? ch : ''));
  for (let i = 0; i < 16; i++) {
    const next = out.replace(CONTROL_TOKEN, '').replace(MARKUP_TAG, '').replace(CONTROL_PIECE, '');
    if (next === out) break;
    out = next;
  }
  return out;
}

// Hidden reasoning is not a note.  Only the text after the LAST `</think>`
// is the answer (a template can open the think block in the prompt, so the
// output may hold only its end), and an unclosed `<think>` runs to the end of
// the output.  Removing the whole block, not just its tags, keeps the model's
// scratch work out of every later prompt.
export function dropReasoning(text) {
  let out = String(text ?? '');
  // Regex positions, not toLowerCase() offsets: lower-casing can change a
  // string's length and misplace the cut.
  const closes = [...out.matchAll(/<\/think>/gi)];
  if (closes.length) { const last = closes.at(-1); out = out.slice(last.index + last[0].length); }
  const open = /<think>/i.exec(out);
  return open ? out.slice(0, open.index) : out;
}

// The note as it may be stored: markup stripped, credential-shaped values
// masked (the summariser read untrusted content and may repeat a secret),
// tidy lines, and bounded.
export function sanitizeNote(text, { maxBytes } = {}) {
  const lines = stripMarkup(dropReasoning(text)).split('\n').map(line => line.replace(/[ \t]+$/u, ''));
  let out = lines.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  out = maskCredentialText(out).text;
  return maxBytes === undefined ? out : boundLines(out, maxBytes);
}

// Whole lines while they fit, so a bound never leaves half a fact; a single
// over-long first line is cut at a character boundary.
export function boundLines(text, maxBytes) {
  if (utf8Bytes(text) <= maxBytes) return text;
  const kept = []; let bytes = 0;
  for (const line of text.split('\n')) {
    const cost = utf8Bytes(line) + (kept.length ? 1 : 0);
    if (bytes + cost > maxBytes) break;
    kept.push(line); bytes += cost;
  }
  return kept.length ? kept.join('\n').trimEnd() : cutUtf8(text.split('\n')[0], maxBytes).trimEnd();
}

export function cutUtf8(text, maxBytes) {
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.byteLength <= maxBytes) return text;
  let cut = Math.max(0, maxBytes);
  while (cut > 0 && (bytes[cut] & 0xc0) === 0x80) cut -= 1;
  return bytes.subarray(0, cut).toString('utf8');
}

// Both the byte bound and the token budget, at the host's conservative
// default ratio, whichever is smaller.
export function noteByteLimit({ noteTokens, noteBytes }) {
  return Math.min(noteBytes, Math.floor(noteTokens * CONTEXT_DEFAULTS.bytesPerToken));
}

// ---- the summarisation request -------------------------------------------
const TOOL_CALL_HINT = /<tool_call>|<function=/i;
function isToolCallText(message) {
  if (message.role !== 'assistant' || typeof message.content !== 'string') return false;
  if (TOOL_CALL_HINT.test(message.content)) return true;
  const trimmed = message.content.trim();
  if (!trimmed.startsWith('{')) return false;
  try { const value = JSON.parse(trimmed); return Boolean(value) && typeof value === 'object' && typeof value.name === 'string' && 'arguments' in value; } catch { return false; }
}

// One excerpt line per message.  A tool call keeps its function and
// parameter names as words before the markup is stripped, so the summariser
// still sees what was called with what.  Whitespace is collapsed: pasted JSON
// indentation would otherwise cost CPU prefill for nothing.
export function excerptLine(message, { perMessageBytes = MEMORY_DEFAULTS.perMessageBytes, mask = true } = {}) {
  const prepared = prepareExcerpt(message, { mask });
  return prepared ? renderExcerpt(prepared, perMessageBytes) : null;
}
// The screened, one-line form of a message, before any length bound.
function prepareExcerpt(message, { mask }) {
  if (!message || typeof message.content !== 'string' || isElidedToolResult(message) || isRecallMessage(message)) return null;
  const labels = MEMORY_PROMPTS.excerpt;
  const call = isToolCallText(message);
  const role = message.role === 'tool' ? fillTemplate(labels.tool, { name: stripMarkup(message.name ?? 'tool') }) : message.role === 'assistant' ? (call ? labels.assistant_tool_call : labels.assistant) : message.role === 'user' ? labels.user : null;
  if (role === null) return null;
  let content = call ? message.content.replace(/<(?:function|parameter)=([^<>\n]{1,96})>/g, ' $1: ') : message.content;
  content = stripMarkup(content);
  if (mask) content = maskCredentialText(content).text;
  // The excerpt is fenced by `<<<` / `>>>` in the prompt; untrusted text
  // must not be able to close the fence and speak outside it.
  content = collapseSpaces(content).replaceAll('<<<', '< < <').replaceAll('>>>', '> > >').trim();
  return content ? { role, content } : null;
}
function renderExcerpt({ role, content }, maxBytes) {
  const marker = MEMORY_PROMPTS.excerpt.truncation_marker;
  const text = utf8Bytes(content) > maxBytes ? cutUtf8(content, maxBytes - utf8Bytes(marker)).trimEnd() + marker : content;
  return fillTemplate(MEMORY_PROMPTS.excerpt.line_template, { role, content: text });
}

// Below this a message says too little to be worth a line.
const MIN_LINE_BYTES = 96;
const joinedBytes = (lines, separator) => lines.reduce((sum, line) => sum + utf8Bytes(line), 0) + Math.max(0, lines.length - 1) * utf8Bytes(separator);

// Every message that fits, in conversation order, within `maxBytes`.  When
// the whole excerpt is too big, the per-message cap is lowered ("water
// filling") until it fits: short messages -- where a stated name, number or
// decision usually is -- stay whole and only the longest (pasted JSON, code,
// tool dumps) lose their tails.  Only if even a minimal cap does not fit are
// whole lines dropped, oldest first, and counted as omitted.
export function buildExcerpt(messages, { maxBytes = MEMORY_DEFAULTS.maxInputBytes, perMessageBytes = MEMORY_DEFAULTS.perMessageBytes, mask = true } = {}) {
  const separator = MEMORY_PROMPTS.excerpt.separator;
  const prepared = messages.map(message => prepareExcerpt(message, { mask })).filter(item => item !== null);
  const at = cap => prepared.map(item => renderExcerpt(item, cap));
  let hi = Math.min(perMessageBytes, maxBytes); let lo = Math.min(MIN_LINE_BYTES, hi);
  let lines = at(hi);
  if (joinedBytes(lines, separator) > maxBytes) {
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (joinedBytes(at(mid), separator) <= maxBytes) lo = mid; else hi = mid - 1; }
    lines = at(lo);
  }
  const kept = []; let bytes = 0;
  for (let i = lines.length - 1; i >= 0; i--) {
    const cost = utf8Bytes(lines[i]) + (kept.length ? utf8Bytes(separator) : 0);
    if (bytes + cost > maxBytes) break;
    kept.unshift(lines[i]); bytes += cost;
  }
  return { text: kept.join(separator), lines: lines.length, included: kept.length, omitted: lines.length - kept.length, bytes };
}

// The word count asked for is derived from the byte bound the host will
// enforce, so a model that obeys it is never cut: a cut would keep the head
// of the note, which is not necessarily what the model chose to keep.
export function summaryRequest({ note, excerpt, noteLimitBytes = noteByteLimit(MEMORY_DEFAULTS) }) {
  const maxWords = Math.max(16, Math.floor(noteLimitBytes / MEMORY_PROMPTS.bytes_per_word));
  const instruction = fillTemplate(MEMORY_PROMPTS.output_instruction, { max_words: maxWords });
  return [
    { role: 'system', content: MEMORY_PROMPTS.system },
    { role: 'user', content: fillTemplate(MEMORY_PROMPTS.user_template, { note: note || MEMORY_PROMPTS.empty_note, excerpt, output_instruction: instruction }) },
  ];
}

// The pinned message.  A `user` message, not `system`: its content is
// derived from untrusted tool output and web text, and the system role is the
// one place the engine puts the app's own policy.  The label tells the model
// it is an unverified summary and data, not instructions.
// The recalled-lines message (memory-recall.mjs) is stored in the history just
// before the user message it was found for, so it must never be archived or
// summarised again.
export const isRecallMessage = message => message?.role === 'user' && typeof message.content === 'string' && message.content.startsWith(MEMORY_PROMPTS.recall_label);
export function noteMessage(note) {
  return { role: 'user', content: fillTemplate(MEMORY_PROMPTS.note_message_template, { label: MEMORY_PROMPTS.note_label, note }) };
}

// ---- planning a compaction ahead of time -----------------------------------
// What the next turn-start compaction would remove, computed between turns so
// the summary can be ready before it is needed.  Returns null when nothing
// would change.  `removed` is what leaves the window: whole dropped turns and,
// for a tool result that is only elided, its ORIGINAL text (the elision
// marker carries no facts, so a fact that existed only in a tool result would
// otherwise be lost before its turn is ever dropped).
export function planCompaction({ history, budgetTokens, triggerTokens, targetTokens, bytesPerToken, maxMessages = Infinity, targetMessages = maxMessages }) {
  let fitted;
  try { fitted = fitHistory({ messages: history, budgetTokens, triggerTokens, targetTokens, bytesPerToken }); } catch { return null; }
  let messages = fitted.messages; let droppedTurns = fitted.droppedTurns;
  // The hard message bound would drop the oldest turn on a later append, one
  // turn per turn once it is reached.  Fold that into the same plan, down to
  // a low-water count, so it costs one summary now and then instead of one
  // extra engine call on every turn.
  if (messages.length > maxMessages) {
    while (messages.length > targetMessages) {
      const length = droppableHeadLength(messages); if (!length) break;
      messages = messages.slice(length); droppedTurns += 1;
    }
  }
  if (messages === history) return null;
  const { removed, dropped, masked } = removedBy(history, messages);
  if (!removed.length) return null;
  return { messages, removed, droppedMessages: dropped, droppedTurns, masked };
}

// What a compaction took out of `before` to produce `after`: the dropped
// head, then the original text of each tool result it elided.  Valid for
// fitHistory and the storage bounds, which only cut the head and replace
// results in place.
export function removedBy(before, after) {
  const dropped = before.length - after.length;
  const removed = before.slice(0, Math.max(0, dropped)); let masked = 0;
  after.forEach((message, index) => {
    const original = before[dropped + index];
    if (message !== original && isElidedToolResult(message) && original && !isElidedToolResult(original)) { removed.push(original); masked += 1; }
  });
  return { removed, dropped, masked };
}
