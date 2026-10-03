import { randomUUID } from 'node:crypto';
import { CONTEXT_DEFAULTS, utf8Bytes } from '../agent/context-budget.mjs';

export const FIXTURE_ENGINE_VERSION = 'fixture-0.1.0';
const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(Object.assign(new Error('cancelled'), { code: 'cancelled' }));
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(timer); reject(Object.assign(new Error('cancelled'), { code: 'cancelled' })); }, { once: true });
});

/**
 * Replaceable fixture implementation of the future S1 engine boundary. It emits
 * text chunks or a deliberately split, structured tool-call payload.
 */
export class FixtureEngineClient {
  constructor({ chunkSize = 8, delayMs = 1 } = {}) { this.chunkSize = chunkSize; this.delayMs = delayMs; this.cancelled = new Set(); }
  async health() { return { ready: true, engine: FIXTURE_ENGINE_VERSION, backend: 'fixture', model: 'synthetic' }; }
  cancel(requestId) { this.cancelled.add(requestId); }
  async *generate({ requestId = randomUUID().replaceAll('-', ''), messages = [], mode = 'normal', signal }) {
    const isCancelled = () => signal?.aborted || this.cancelled.has(requestId);
    const check = () => { if (isCancelled()) throw Object.assign(new Error('generation cancelled'), { code: 'cancelled' }); };
    const latest = messages.at(-1);
    const needsTime = latest?.role === 'user' && /\b(time|clock|date|timezone)\b/i.test(latest.content ?? '');
    const hasTimeResult = messages.some(m => m.role === 'tool' && m.name === 'time.now');
    if (needsTime && !hasTimeResult) {
      const call = JSON.stringify({ id: `call_${requestId.slice(0, 12)}`, name: 'time.now', arguments: { format: 'local' } });
      for (let i = 0; i < call.length; i += this.chunkSize) { check(); await sleep(this.delayMs, signal); yield { kind: 'tool_call_chunk', text: call.slice(i, i + this.chunkSize) }; }
      return;
    }
    const answer = hasTimeResult
      ? `The fixture clock reports: ${String(messages.findLast(m => m.role === 'tool' && m.name === 'time.now')?.content ?? 'time unavailable')}.`
      : mode === 'deep' ? 'Fixture deep mode completed a bounded local answer.' : 'Fixture answer: local inference is running with no network provider.';
    for (let i = 0; i < answer.length; i += this.chunkSize) { check(); await sleep(this.delayMs, signal); yield { kind: 'text_delta', text: answer.slice(i, i + this.chunkSize) }; }
    // The fixture cannot tokenize. It used to report the message COUNT as
    // `prompt_tokens`; the controller learns its bytes-per-token ratio from
    // that field, so the fixture now omits it exactly as the native client
    // does when the engine sends no usage, and estimates only the answer.
    yield { kind: 'done', finish_reason: 'stop', usage: { completion_tokens: Math.ceil(utf8Bytes(answer) / CONTEXT_DEFAULTS.bytesPerToken) } };
  }
  async shutdown() { this.cancelled.clear(); }
}
