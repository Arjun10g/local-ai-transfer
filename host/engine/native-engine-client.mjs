import { setTimeout as delay } from 'node:timers/promises';
import { ToolCallStreamDecoder } from '../agent/tool-envelope.mjs';

const LOOPBACK = '127.0.0.1';
const REQUEST_ID = /^[A-Za-z0-9_-]{1,96}$/;
const MAX_REQUEST_JSON_BYTES = 64 * 1024;
const MAX_RESPONSE_JSON_BYTES = 4 * 1024 * 1024;
const MAX_SSE_BYTES = 4 * 1024 * 1024;
const MAX_SSE_EVENTS = 4096;
const MAX_SSE_LINE = 256 * 1024;
// The engine writes the SSE head (and with it X-Request-Id) before prefill
// starts, so a cancel that races the head only has to wait milliseconds for
// the id that lets it reach the engine. Past this the engine is not answering
// and closing the socket is all that is left.
const HEAD_GRACE_MS = 2000;

export class NativeEngineError extends Error {
  constructor(code, message, status = 0) { super(message); this.name = 'NativeEngineError'; this.code = code; this.status = status; }
}

function endpointUrl(endpoint) {
  if (typeof endpoint !== 'string' || endpoint.length < 1 || endpoint.length > 512) throw new NativeEngineError('invalid_engine_endpoint', 'engine endpoint is invalid');
  let url; try { url = new URL(endpoint); } catch { throw new NativeEngineError('invalid_engine_endpoint', 'engine endpoint is not a URL'); }
  if (url.protocol !== 'http:' || url.hostname !== LOOPBACK || url.username || url.password || url.pathname !== '/' || url.search || url.hash || !url.port) throw new NativeEngineError('invalid_engine_endpoint', 'engine endpoint must be an authenticated numeric loopback HTTP URL');
  const port = Number(url.port); if (!Number.isInteger(port) || port < 1 || port > 65535) throw new NativeEngineError('invalid_engine_endpoint', 'engine endpoint port is invalid');
  return url.origin;
}

async function readJson(response) {
  if (!response.body) throw new NativeEngineError('engine_empty_response', 'native JSON response had no body');
  const chunks = []; let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.byteLength ?? 0;
    if (bytes > MAX_RESPONSE_JSON_BYTES) throw new NativeEngineError('engine_response_too_large', 'native JSON response exceeded the size limit');
    chunks.push(chunk);
  }
  const text = new TextDecoder().decode(Buffer.concat(chunks.map(chunk => Buffer.from(chunk))));
  try { return JSON.parse(text); } catch { return {}; }
}

// `onFrame` sees every data frame (progress for the stall timer); `onEnd` runs
// once the engine has said it is finished, so no cancel needs to be sent.
async function* sseEvents(response, signal, { onFrame, onEnd } = {}) {
  if (!response.body) throw new NativeEngineError('engine_empty_stream', 'engine returned no response stream');
  const decoder = new TextDecoder(); const encoder = new TextEncoder(); let buffer = ''; let totalBytes = 0; let events = 0;
  for await (const chunk of response.body) {
    if (signal?.aborted) throw Object.assign(new NativeEngineError('cancelled', 'native generation cancelled'), { code: 'cancelled' });
    totalBytes += chunk.byteLength ?? 0;
    if (totalBytes > MAX_SSE_BYTES) throw new NativeEngineError('engine_stream_too_large', 'native SSE response exceeded the size limit');
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split(/\r?\n/); buffer = lines.pop() ?? '';
    if (encoder.encode(buffer).byteLength > MAX_SSE_LINE) throw new NativeEngineError('engine_stream_line_too_large', 'native SSE line exceeded the size limit');
    for (const line of lines) {
      if (encoder.encode(line).byteLength > MAX_SSE_LINE) throw new NativeEngineError('engine_stream_line_too_large', 'native SSE line exceeded the size limit');
      if (!line.startsWith('data: ')) continue;
      const data = line.slice(6); if (data === '[DONE]') { onEnd?.(); return; }
      if (++events > MAX_SSE_EVENTS) throw new NativeEngineError('engine_stream_event_limit', 'native SSE event limit exceeded');
      let value; try { value = JSON.parse(data); } catch { throw new NativeEngineError('invalid_engine_stream', 'engine emitted malformed SSE JSON'); }
      onFrame?.();
      if (value.error) { onEnd?.(); throw new NativeEngineError(value.error.code ?? 'engine_error', 'engine returned an error event'); }
      yield value;
    }
  }
  buffer += decoder.decode();
  if (encoder.encode(buffer).byteLength > MAX_SSE_LINE || buffer.trim()) throw new NativeEngineError('invalid_engine_stream', 'engine stream ended mid-frame');
}

const integerIn = (value, min, max) => Number.isInteger(value) && value >= min && value <= max;
const tokenCount = value => integerIn(value, 0, 1 << 24);
// The final SSE frame carries the engine's real token counts; the controller
// learns its bytes-per-token estimate from `prompt_tokens` and reports both
// fields in `message.completed.usage`. Only counts the engine actually sent
// are passed on: an engine that omits them (or sends garbage) still yields a
// usable `done`, with the missing field left missing. A host-side tally of
// stream frames would be the host's guess, not the engine's count, and
// anything downstream would read it as a real measurement.
function engineUsage(usage) {
  const out = {};
  for (const key of ['prompt_tokens', 'completion_tokens']) if (tokenCount(usage?.[key])) out[key] = usage[key];
  return out;
}

export class NativeEngineClient {
  // `timeoutMs` bounds each short JSON call (sessions, readiness, cancel).
  // A generation is bounded separately because on a laptop CPU a long prompt
  // can prefill for many minutes before the first token while a stuck decode
  // must still be noticed quickly:
  //   firstTokenTimeoutMs - request start to the first SSE data frame;
  //   idleTimeoutMs       - longest gap between data frames after that;
  //   totalTimeoutMs      - hard ceiling on the whole generation.
  // The earliest applicable deadline wins.
  constructor({ endpoint, token, model, backend, timeoutMs = 120000, maxTokens = 1024, firstTokenTimeoutMs = 900000, idleTimeoutMs = 120000, totalTimeoutMs = 1800000 } = {}) {
    this.baseUrl = endpointUrl(endpoint); if (typeof token !== 'string' || token.length < 16 || token.length > 512) throw new NativeEngineError('invalid_engine_token', 'engine bearer token is invalid');
    if (typeof model !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(model)) throw new NativeEngineError('invalid_engine_model', 'native model identity is required');
    if (typeof backend !== 'string' || !/^[A-Za-z0-9._/-]{1,128}$/.test(backend)) throw new NativeEngineError('invalid_engine_backend', 'native backend identity is required');
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 120000) throw new NativeEngineError('invalid_engine_timeout', 'native engine timeout must be 1000-120000ms');
    // Matches the engine's own max_tokens bound (chat_request.cpp).
    if (!integerIn(maxTokens, 1, 2048)) throw new NativeEngineError('invalid_engine_max_tokens', 'native maxTokens must be 1-2048');
    if (!integerIn(firstTokenTimeoutMs, 1000, 3600000)) throw new NativeEngineError('invalid_engine_timeout', 'native first-token timeout must be 1000-3600000ms');
    if (!integerIn(idleTimeoutMs, 1000, 1800000)) throw new NativeEngineError('invalid_engine_timeout', 'native idle timeout must be 1000-1800000ms');
    if (!integerIn(totalTimeoutMs, 1000, 7200000)) throw new NativeEngineError('invalid_engine_timeout', 'native total timeout must be 1000-7200000ms');
    this.token = token; this.model = model; this.backend = backend; this.timeoutMs = timeoutMs; this.maxTokens = maxTokens; this.firstTokenTimeoutMs = firstTokenTimeoutMs; this.idleTimeoutMs = idleTimeoutMs; this.totalTimeoutMs = totalTimeoutMs; this.sessions = new Map(); this.active = new Map(); this.closed = false;
  }
  headers(extra = {}) {
    // Never allow a caller-provided case variant to replace the launch token.
    const safeExtra = extra && typeof extra === 'object'
      ? Object.fromEntries(Object.entries(extra).filter(([name]) => name.toLowerCase() !== 'authorization'))
      : {};
    return { ...safeExtra, authorization: `Bearer ${this.token}` };
  }
  async request(path, options = {}, { signal, timeoutMs = this.timeoutMs } = {}) {
    if (this.closed) throw new NativeEngineError('engine_client_closed', 'native engine client is closed');
    if (typeof options.body === 'string' && new TextEncoder().encode(options.body).byteLength > MAX_REQUEST_JSON_BYTES)
      throw new NativeEngineError('engine_request_too_large', 'native JSON request exceeded the size limit');
    // `timeoutMs: null` is for the streaming generation, whose deadlines are
    // owned by generate(): a fetch timeout would also cut the response body.
    const timeoutSignal = timeoutMs === null ? null : AbortSignal.timeout(timeoutMs);
    const requestSignal = signal && timeoutSignal ? AbortSignal.any([signal, timeoutSignal]) : signal ?? timeoutSignal;
    try {
      const response = await fetch(`${this.baseUrl}${path}`, { ...options, signal: requestSignal, headers: this.headers(options.headers) });
      if (!response.ok) { const payload = await readJson(response); throw new NativeEngineError(payload.error?.code ?? `http_${response.status}`, 'native engine request failed', response.status); }
      return response;
    } catch (error) {
      if (error?.name === 'AbortError' || error?.name === 'TimeoutError') throw Object.assign(new NativeEngineError(signal?.aborted ? 'cancelled' : 'engine_timeout', signal?.aborted ? 'native request cancelled' : 'native request timed out'), { code: signal?.aborted ? 'cancelled' : 'engine_timeout' });
      throw error;
    }
  }
  async health() {
    const response = await this.request('/healthz', {}, { timeoutMs: 10000 }); const data = await readJson(response);
    const build = await this.buildInfo();
    const backendMatches = this.backend === 'cpu'
      ? typeof build.backend === 'string' && build.backend.endsWith('/cpu')
      : build.backend === this.backend || (typeof build.backend === 'string' && build.backend.startsWith(`${this.backend}/`));
    if (build.model !== this.model || !backendMatches) throw new NativeEngineError('engine_identity_mismatch', 'native engine identity does not match configured model/backend');
    return { ready: data.lifecycle === 'READY' || data.lifecycle === 'BUSY', engine: build.engine_version ?? 'native-0.1.0', backend: build.backend ?? this.backend, model: build.model ?? this.model, lifecycle: data.lifecycle ?? 'UNKNOWN' };
  }
  async buildInfo() { const response = await this.request('/build-info', {}, { timeoutMs: 10000 }); return readJson(response); }
  async ready({ timeoutMs = this.timeoutMs, signal } = {}) { const response = await this.request('/readyz', {}, { timeoutMs, signal }); const data = await readJson(response); return { ready: data.ready === true, lifecycle: data.lifecycle ?? 'UNKNOWN' }; }
  async waitReady({ timeoutMs = this.timeoutMs, intervalMs = 25, signal } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const remaining = Math.max(1, deadline - Date.now());
      try {
        const status = await this.ready({ timeoutMs: Math.min(this.timeoutMs, remaining), signal });
        if (status.ready) return status;
      } catch (error) {
        if (!(error instanceof NativeEngineError) || !['engine_timeout', 'http_503', 'not_ready'].includes(error.code)) throw error;
      }
      await delay(Math.min(intervalMs, Math.max(1, deadline - Date.now())), undefined, { signal });
    }
    throw new NativeEngineError('engine_not_ready', 'native engine readiness timed out');
  }
  async ensureSession(hostSessionId, signal) {
    if (!REQUEST_ID.test(hostSessionId)) throw new NativeEngineError('invalid_session_id', 'host session id is invalid');
    const existing = this.sessions.get(hostSessionId); if (existing) return existing;
    const response = await this.request('/v1/sessions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, { signal }); const data = await readJson(response);
    if (typeof data.id !== 'string' || !REQUEST_ID.test(data.id)) throw new NativeEngineError('invalid_engine_response', 'engine returned invalid session id');
    this.sessions.set(hostSessionId, data.id); return data.id;
  }
  async deleteSession(hostSessionId) { const nativeId = this.sessions.get(hostSessionId); if (!nativeId) return false; try { await this.request(`/v1/sessions/${encodeURIComponent(nativeId)}`, { method: 'DELETE' }); } finally { this.sessions.delete(hostSessionId); } return true; }
  async resetSession(hostSessionId) { return this.deleteSession(hostSessionId); }
  cancel(requestId) {
    const active = this.active.get(requestId); if (!active) return false; active.cancelled = true; this.#halt(active); return true;
  }
  // Stop reading AND make the engine stop. Closing the socket alone is not
  // enough: the engine only notices a dead socket on its next token write, so
  // during prefill it would stay BUSY for minutes. POST /v1/cancel/<id> sets
  // the cancellation flag that llama.cpp's abort callback polls mid-prefill.
  #halt(active) {
    active.abort.abort();
    if (active.fetchAbort && !active.fetchAbort.signal.aborted) {
      if (active.response) active.fetchAbort.abort();
      else if (!active.headGrace) { active.headGrace = setTimeout(() => active.fetchAbort.abort(), HEAD_GRACE_MS); active.headGrace.unref?.(); }
    }
    if (active.nativeRequestId && !active.engineFinished && !active.cancelPosted) { active.cancelPosted = true; void this.postCancel(active.nativeRequestId); }
  }
  async postCancel(nativeRequestId) { if (!REQUEST_ID.test(nativeRequestId)) return false; try { const response = await this.request(`/v1/cancel/${encodeURIComponent(nativeRequestId)}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, { timeoutMs: 2000 }); const result = await readJson(response); return result.cancelled === true; } catch { return false; } }
  // Waits for the SSE head without letting a cancel wait on it: the fetch
  // itself keeps running (up to HEAD_GRACE_MS) so the request id still arrives
  // and the cancel can be delivered to the engine.
  #openStream(active, payload) {
    const fetchAbort = new AbortController(); active.fetchAbort = fetchAbort; active.response = null; active.nativeRequestId = null; active.engineFinished = false; active.cancelPosted = false;
    const opened = this.request('/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) }, { signal: fetchAbort.signal, timeoutMs: null }).then(response => {
      active.response = response; active.nativeRequestId = response.headers.get('x-request-id'); clearTimeout(active.headGrace); active.headGrace = null;
      if (active.abort.signal.aborted) this.#halt(active);
      return response;
    });
    opened.catch(() => {});
    return new Promise((resolve, reject) => {
      const stopped = () => reject(Object.assign(new NativeEngineError('cancelled', 'native generation cancelled'), { code: 'cancelled' }));
      if (active.abort.signal.aborted) { stopped(); return; }
      active.abort.signal.addEventListener('abort', stopped, { once: true });
      opened.then(response => { active.abort.signal.removeEventListener('abort', stopped); resolve(response); }, error => { active.abort.signal.removeEventListener('abort', stopped); reject(error); });
    });
  }
  async *generate({ requestId, sessionId, messages = [], mode = 'normal', tools = [], signal }) {
    if (!REQUEST_ID.test(requestId)) throw new NativeEngineError('invalid_request_id', 'host request id is invalid');
    if (!Array.isArray(messages) || messages.length < 1 || messages.length > 64) throw new NativeEngineError('invalid_messages', 'native message history is invalid');
    if (!Array.isArray(tools) || tools.length > 32) throw new NativeEngineError('invalid_tools', 'native tool definitions are invalid');
    if (!['normal', 'deep'].includes(mode)) throw new NativeEngineError('invalid_mode', 'native mode is invalid');
    const hostSessionId = sessionId ?? 'ses_native_default';
    const localAbort = new AbortController(); const active = { abort: localAbort, nativeRequestId: null, cancelled: false, fetchAbort: null, response: null, headGrace: null, engineFinished: false, cancelPosted: false };
    this.active.set(requestId, active);
    // An abort from the caller's signal is a cancel like any other and must
    // reach the engine, not just drop the socket.
    const relay = () => { active.cancelled = true; this.#halt(active); };
    signal?.addEventListener('abort', relay, { once: true });
    const deadline = Date.now() + this.totalTimeoutMs; let timer = null; let timeoutReason = null;
    const arm = (ms, reason) => {
      clearTimeout(timer); const remaining = deadline - Date.now(); const [wait, why] = remaining <= ms ? [remaining, 'total'] : [ms, reason];
      timer = setTimeout(() => { timeoutReason = why; this.#halt(active); }, Math.max(0, wait));
    };
    arm(this.firstTokenTimeoutMs, 'first_token');
    let completed = false;
    try {
      if (signal?.aborted) relay();
      for (let attempt = 0; ; attempt++) {
        const nativeSessionId = await this.ensureSession(hostSessionId, localAbort.signal);
        const payload = { model: this.model, session_id: nativeSessionId, messages, tools, stream: true, max_tokens: this.maxTokens, mode };
        let emitted = false;
        try {
          const response = await this.#openStream(active, payload);
          const decoder = new ToolCallStreamDecoder({ stream: true, reasoning: mode === 'deep' });
          const frames = sseEvents(response, localAbort.signal, { onFrame: () => arm(this.idleTimeoutMs, 'stalled'), onEnd: () => { active.engineFinished = true; } });
          for await (const chunk of frames) {
            const choice = chunk.choices?.[0]; const delta = choice?.delta?.content;
            if (typeof delta === 'string' && delta) { for (const event of decoder.push(delta)) { emitted = true; yield event; } }
            const finish = choice?.finish_reason;
            if (finish) {
              active.engineFinished = true;
              if (finish === 'cancelled' || active.cancelled) throw Object.assign(new NativeEngineError('cancelled', 'native generation cancelled'), { code: 'cancelled' });
              // Flush held text before `done` so a consumer sees the whole
              // answer first. finish_reason 'length' means the max_tokens cap
              // cut the answer; it is passed through for a "continue" offer.
              for (const event of decoder.finish()) { emitted = true; yield event; }
              emitted = true; yield { kind: 'done', finish_reason: finish, usage: engineUsage(chunk.usage) };
            }
          }
          for (const event of decoder.finish()) yield event;
          completed = true; return;
        } catch (error) {
          // The engine keeps a few sessions and evicts the oldest, so a cached
          // native id can go stale. The host resends the whole history every
          // turn; a fresh session loses only prefix reuse. Retry once, and
          // only before anything reached the caller, so nothing is repeated.
          if (attempt === 0 && !emitted && error?.code === 'not_found' && !localAbort.signal.aborted) {
            if (this.sessions.get(hostSessionId) === nativeSessionId) this.sessions.delete(hostSessionId);
            continue;
          }
          throw error;
        }
      }
    } catch (error) {
      if (timeoutReason) throw Object.assign(new NativeEngineError('engine_timeout', `native generation timed out (${timeoutReason})`), { code: 'engine_timeout', reason: timeoutReason });
      if (active.cancelled || signal?.aborted || error?.code === 'cancelled') throw Object.assign(new NativeEngineError('cancelled', 'native generation cancelled'), { code: 'cancelled' });
      if (error?.name === 'TimeoutError' || error?.name === 'AbortError') throw Object.assign(new NativeEngineError('engine_timeout', 'native generation timed out'), { code: 'engine_timeout' });
      throw error;
    } finally {
      clearTimeout(timer); signal?.removeEventListener('abort', relay);
      // A consumer that stopped early (a refused tool call, a thrown error)
      // leaves the engine generating unless it is told to stop.
      if (!completed) this.#halt(active);
      this.active.delete(requestId);
    }
  }
  async shutdown() { for (const active of this.active.values()) { active.cancelled = true; this.#halt(active); } this.active.clear(); const sessions = [...this.sessions.keys()]; await Promise.allSettled(sessions.map(id => this.deleteSession(id))); this.closed = true; }
}
