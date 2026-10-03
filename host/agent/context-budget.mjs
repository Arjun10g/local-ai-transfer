// Token-budgeted history for the engine's fixed context window.
//
// The engine refuses a prompt whose tokens plus `max_tokens` exceed its
// context (8,192 by default), and the host cannot tokenize, so everything here
// is a conservative byte-based estimate that a per-session observation of the
// engine's real `usage.prompt_tokens` can tighten.  Pure functions only: the
// controller owns the session and decides when to persist a compacted history.

export const CONTEXT_DEFAULTS = Object.freeze({
  contextTokens: 8192,
  // Fallback only: the controller reserves the engine client's `maxTokens`
  // (NativeEngineClient default 1024) when the engine exposes it.
  maxOutputTokens: 1024,
  // UTF-8 bytes rather than UTF-16 chars: a CJK character is 3 bytes and about
  // one token, so counting bytes stays conservative where chars/3 would not.
  bytesPerToken: 3.0,
  minBytesPerToken: 1.0,
  maxBytesPerToken: 4.0,
  // Observed ratios are discounted by this much before being trusted.
  learnSafety: 0.1,
  // Applied after an engine overflow the estimate did not predict.
  overflowPenalty: 0.75,
  // `<|im_start|>role\n ... <|im_end|>\n` plus the tool-response wrapper.
  perMessageTokens: 8,
  // App-owned policy system message (~620 bytes) + the pinned template's
  // tools preamble (~1,040 bytes) + generation prompt and think scaffold.
  fixedOverheadTokens: 480,
  noToolsOverheadTokens: 32,
  marginRatio: 0.05,
  minMarginTokens: 128,
  // Compact to this fraction of the budget, not merely under it, so the next
  // several prompts are strict extensions of the compacted one instead of
  // every turn re-trimming.  Lower keeps less history.
  lowWaterRatio: 0.6,
  // A new user turn already re-prefills the whole prompt (the template strips
  // the previous answer's think scaffold, so it is not a strict extension),
  // which makes turn start the free moment to compact.  Compacting there
  // early leaves the tool loop, whose continuations the engine does reuse,
  // room to grow without a mid-loop compaction.
  turnStartTriggerRatio: 0.75,
  turnStartTargetRatio: 0.5,
  // Results this small cost less than the marker that would replace them.
  maskMinBytes: 256,
});

export class ContextBudgetError extends Error {
  constructor(details = {}) {
    super('This message and its tool results are too large for the model\'s context window. Shorten the message or reset the conversation.');
    this.name = 'ContextBudgetError'; this.code = 'context_overflow'; this.details = Object.freeze({ ...details });
  }
}

const encoder = new TextEncoder();
export const utf8Bytes = value => (typeof value === 'string' ? encoder.encode(value).byteLength : 0);

// Exactly the fields the engine renders; role is part of the per-message cost.
export const messageBytes = message => utf8Bytes(message.content) + utf8Bytes(message.name) + utf8Bytes(message.tool_call_id);
export const toolsBytes = tools => (Array.isArray(tools) && tools.length ? utf8Bytes(JSON.stringify(tools)) : 0);

function options(overrides) { return { ...CONTEXT_DEFAULTS, ...(overrides ?? {}) }; }
const clampRatio = (ratio, o) => Math.min(o.maxBytesPerToken, Math.max(o.minBytesPerToken, ratio));

export function messageTokens(message, bytesPerToken, perMessageTokens = CONTEXT_DEFAULTS.perMessageTokens) {
  return perMessageTokens + Math.ceil(messageBytes(message) / bytesPerToken);
}
export function messagesTokens(messages, bytesPerToken, perMessageTokens = CONTEXT_DEFAULTS.perMessageTokens) {
  let total = 0; for (const message of messages) total += messageTokens(message, bytesPerToken, perMessageTokens); return total;
}

// Tokens available to the history once the response, the rendered tool
// definitions, the fixed template overhead and a safety margin are reserved.
export function historyBudget({ tools = [], bytesPerToken, ...overrides } = {}) {
  const o = options(overrides); const ratio = clampRatio(bytesPerToken ?? o.bytesPerToken, o);
  const margin = Math.max(o.minMarginTokens, Math.ceil(o.contextTokens * o.marginRatio));
  const overhead = tools.length ? o.fixedOverheadTokens : o.noToolsOverheadTokens;
  const toolTokens = Math.ceil(toolsBytes(tools) / ratio);
  return { budgetTokens: o.contextTokens - o.maxOutputTokens - margin - overhead - toolTokens, toolTokens, overheadTokens: overhead, marginTokens: margin, bytesPerToken: ratio };
}

export function estimatePromptTokens({ messages, tools = [], bytesPerToken, ...overrides } = {}) {
  const o = options(overrides); const ratio = clampRatio(bytesPerToken ?? o.bytesPerToken, o);
  return messagesTokens(messages, ratio, o.perMessageTokens) + Math.ceil(toolsBytes(tools) / ratio) + (tools.length ? o.fixedOverheadTokens : o.noToolsOverheadTokens);
}

// Derive a bytes-per-token ratio from the engine's real prompt size.  Returns
// null for an observation that cannot be a tokenizer measurement (the fixture
// engine reports a message count; the native stream reports nothing), so a
// fake number can never loosen the estimate.
export function observedBytesPerToken({ messages, tools = [], promptTokens, ...overrides } = {}) {
  const o = options(overrides);
  if (!Number.isInteger(promptTokens) || promptTokens < 1) return null;
  let bytes = toolsBytes(tools); for (const message of messages) bytes += messageBytes(message);
  const structural = o.perMessageTokens * messages.length + (tools.length ? o.fixedOverheadTokens : o.noToolsOverheadTokens);
  const contentTokens = promptTokens - structural;
  if (contentTokens < 16 || bytes < 64) return null;
  const ratio = bytes / contentTokens;
  return ratio >= 0.5 && ratio <= 8 ? ratio : null;
}

// Learned ratio: discounted by `learnSafety` and clamped, so even a perfect
// observation leaves headroom for the next turn's different content mix.
// Asymmetric on purpose: evidence of denser tokenization is always taken, but
// the ratio is relaxed only from a prompt large enough that its content, not
// the guessed fixed overhead, dominates the count.  On a small prompt that
// guess swamps the arithmetic and would otherwise loosen the budget.
export function learnBytesPerToken({ observed, current, promptTokens, ...overrides } = {}) {
  const o = options(overrides); if (observed == null) return null;
  const candidate = clampRatio(observed * (1 - o.learnSafety), o);
  if (candidate <= (current ?? o.bytesPerToken)) return candidate;
  return promptTokens * 4 >= o.contextTokens ? candidate : null;
}
export function penalizeBytesPerToken(current, overrides) {
  const o = options(overrides); return clampRatio((current ?? o.bytesPerToken) * o.overflowPenalty, o);
}

// The engine reports "context limit exceeded" as an SSE `invalid_request`
// error event (status 0 through the native client), which it uses for nothing
// else on the streaming path.  A plain HTTP 400 `invalid_request` is also what
// a schema failure returns, so it counts only when the prompt was large enough
// for overflow to be the plausible cause.
export function isContextOverflowError(error, { estimatedTokens = 0, contextTokens = CONTEXT_DEFAULTS.contextTokens } = {}) {
  if (!error || typeof error !== 'object') return false;
  if (error.code === 'context_overflow' || error.code === 'context_limit_exceeded') return true;
  if (error.code !== 'invalid_request') return false;
  if (!error.status) return true;
  return error.status === 400 && estimatedTokens * 2 >= contextTokens;
}

// A turn is a user message and everything up to the next user message.
// Anything before the first user message is a headless group of orphans.
export function groupTurns(messages) {
  const turns = []; let start = 0;
  for (let i = 1; i <= messages.length; i++) {
    if (i === messages.length || messages[i].role === 'user') { turns.push({ start, end: i, headless: messages[start].role !== 'user' }); start = i; }
  }
  return messages.length ? turns : [];
}

// Length of the oldest group that may be dropped: never the latest turn, which
// holds the newest user message and any in-flight tool loop.
export function droppableHeadLength(messages) {
  const next = messages.findIndex((message, index) => index > 0 && message.role === 'user');
  return next === -1 ? 0 : next;
}

const ELIDED = /^\[Earlier tool result elided to fit the context window: \d+ bytes removed\.\]$/;
export const isElidedToolResult = message => message?.role === 'tool' && typeof message.content === 'string' && ELIDED.test(message.content);
// Keeps the role, tool name and call id so the call/result pair stays intact
// and the model can still see which tool ran.
export function elideToolResult(message) {
  return { ...message, content: `[Earlier tool result elided to fit the context window: ${utf8Bytes(message.content)} bytes removed.]` };
}

// Returns the history to send.  While the estimate is at most `triggerTokens`
// (default: the budget) it returns the input array itself; otherwise, in
// order: (a) old tool results are replaced by an elision marker, oldest first,
// never in the latest turn; (b) whole oldest turns are dropped.  Both stop at
// `targetTokens` (low-water mark).  If the latest turn alone exceeds
// `budgetTokens`, throws ContextBudgetError.  The result is a fixed point:
// fitting it again with the same arguments changes nothing.
export function fitHistory({ messages, budgetTokens, triggerTokens, targetTokens, bytesPerToken = CONTEXT_DEFAULTS.bytesPerToken, perMessageTokens = CONTEXT_DEFAULTS.perMessageTokens, maskMinBytes = CONTEXT_DEFAULTS.maskMinBytes }) {
  if (!Array.isArray(messages)) throw new TypeError('messages must be an array');
  if (!Number.isFinite(budgetTokens)) throw new TypeError('budgetTokens is required');
  const target = Math.min(budgetTokens, targetTokens ?? Math.floor(budgetTokens * CONTEXT_DEFAULTS.lowWaterRatio));
  const trigger = Math.max(target, Math.min(budgetTokens, triggerTokens ?? budgetTokens));
  const cost = message => messageTokens(message, bytesPerToken, perMessageTokens);
  const stats = { masked: 0, maskedBytes: 0, droppedTurns: 0, droppedMessages: 0 };
  let out = messages; let tokens = messagesTokens(out, bytesPerToken, perMessageTokens);
  // A headless prefix can only be legacy damage, but the engine rejects a
  // history whose leading messages answer nothing, so it always goes.
  const firstUser = out.findIndex(message => message.role === 'user');
  if (firstUser > 0) { for (const message of out.slice(0, firstUser)) tokens -= cost(message); stats.droppedMessages += firstUser; out = out.slice(firstUser); }
  if (tokens > trigger) {
    const latest = out.findLastIndex(message => message.role === 'user');
    for (let i = 0; i < latest && tokens > target; i++) {
      const message = out[i];
      if (message.role !== 'tool' || isElidedToolResult(message) || utf8Bytes(message.content) <= maskMinBytes) continue;
      if (out === messages) out = messages.slice();
      const elided = elideToolResult(message); tokens += cost(elided) - cost(message);
      stats.masked += 1; stats.maskedBytes += utf8Bytes(message.content); out[i] = elided;
    }
    while (tokens > target) {
      const length = droppableHeadLength(out); if (!length) break;
      for (let i = 0; i < length; i++) tokens -= cost(out[i]);
      out = out.slice(length); stats.droppedTurns += 1; stats.droppedMessages += length;
    }
  }
  if (tokens > budgetTokens) throw new ContextBudgetError({ estimated_tokens: tokens, budget_tokens: budgetTokens });
  return { messages: out, changed: out !== messages, tokens, ...stats };
}
