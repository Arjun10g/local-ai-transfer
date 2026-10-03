// Size limits a tool result must respect before it enters the history.
//
// The engine refuses any single message whose content exceeds 32,768 BYTES
// (native/server/chat_request.cpp `kMaxString`, measured on the UTF-8 string),
// while a tool result envelope may legally carry 65,536 characters and a raw
// tool may return far more.  A result that large would also be the newest
// message of the turn, which the context budget never elides, so one big file
// read could fail the whole turn.  Each result is therefore cut to a share of
// what is left of the prompt budget as it arrives, with a marker the model can
// read, instead of failing later at the engine.
import { CONTEXT_DEFAULTS, historyBudget, messagesTokens, utf8Bytes } from './context-budget.mjs';

export const ENGINE_MAX_MESSAGE_BYTES = 32768;
// A quarter of the remaining budget leaves room for the model's next step and
// for further tool calls in the same loop (each later result gets a quarter of
// a smaller remainder), without compacting mid-loop.
export const TOOL_RESULT_BUDGET_SHARE = 0.25;
// Below this the model cannot tell what the tool returned at all; if the
// window really is that full, the context budget decides what else to drop.
export const MIN_TOOL_RESULT_BYTES = 512;
// Room for the marker itself, so the capped content stays within the cap.
const MARKER_RESERVE_BYTES = 96;
export const TRUNCATION_MARKER = /\n\[Output truncated: \d+ of \d+ bytes shown\.\]$/u;
export const truncationMarker = (shownBytes, totalBytes) => `\n[Output truncated: ${shownBytes} of ${totalBytes} bytes shown.]`;

// Byte cap for the next tool result: TOOL_RESULT_BUDGET_SHARE of the prompt
// budget left after the LATEST TURN (its user message, the tool steps so far,
// and any message about to be appended), never above the engine's per-message
// cap.  Older turns do not count: the budget elides and drops them as needed,
// but it can never shrink the latest turn, so that is the only content a
// fresh result competes with.  Each result taking a quarter of what is left
// keeps the whole tool loop inside the budget.
export function toolResultByteCap({ history = [], pending = [], tools = [], bytesPerToken, contextTokens = CONTEXT_DEFAULTS.contextTokens, maxOutputTokens = CONTEXT_DEFAULTS.maxOutputTokens } = {}) {
  const budget = historyBudget({ tools, bytesPerToken, contextTokens, maxOutputTokens });
  const latest = history.findLastIndex(message => message?.role === 'user');
  const turn = latest === -1 ? history : history.slice(latest);
  const used = messagesTokens(turn, budget.bytesPerToken) + messagesTokens(pending, budget.bytesPerToken);
  const remaining = Math.max(0, budget.budgetTokens - used);
  const share = Math.floor((remaining * TOOL_RESULT_BUDGET_SHARE - CONTEXT_DEFAULTS.perMessageTokens) * budget.bytesPerToken);
  return Math.min(ENGINE_MAX_MESSAGE_BYTES, Math.max(MIN_TOOL_RESULT_BYTES, share));
}

// Keeps the head of `text` in at most `maxBytes` UTF-8 bytes, marker
// included.  The cut backs off to a character boundary so the kept text never
// ends in a broken multi-byte sequence.  Head only: no host tool has a
// head-and-tail convention to reuse (the `_bounded_command_tail` helpers are
// the Python remote-eval receipts, a different boundary).
export function truncateUtf8(text, maxBytes) {
  const totalBytes = utf8Bytes(text);
  if (totalBytes <= maxBytes) return { text, truncated: false, shownBytes: totalBytes, totalBytes };
  const bytes = Buffer.from(text, 'utf8');
  let cut = Math.max(0, maxBytes - MARKER_RESERVE_BYTES);
  while (cut > 0 && (bytes[cut] & 0xc0) === 0x80) cut -= 1;
  return { text: bytes.subarray(0, cut).toString('utf8') + truncationMarker(cut, totalBytes), truncated: true, shownBytes: cut, totalBytes };
}
