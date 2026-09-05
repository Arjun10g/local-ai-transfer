import { setTimeout as delay } from 'node:timers/promises';
import { ToolCallStreamDecoder } from '../agent/tool-envelope.mjs';

const LOOPBACK = '127.0.0.1';
const REQUEST_ID = /^[A-Za-z0-9_-]{1,96}$/;
const MAX_REQUEST_JSON_BYTES = 64 * 1024;
const MAX_RESPONSE_JSON_BYTES = 4 * 1024 * 1024;
const MAX_SSE_BYTES = 4 * 1024 * 1024;
const MAX_SSE_EVENTS = 4096;
const MAX_SSE_LINE = 256 * 1024;

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

async function* sseEvents(response, signal) {
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
      const data = line.slice(6); if (data === '[DONE]') return;
      if (++events > MAX_SSE_EVENTS) throw new NativeEngineError('engine_stream_event_limit', 'native SSE event limit exceeded');
      let value; try { value = JSON.parse(data); } catch { throw new NativeEngineError('invalid_engine_stream', 'engine emitted malformed SSE JSON'); }
      if (value.error) throw new NativeEngineError(value.error.code ?? 'engine_error', 'engine returned an error event');
      yield value;
    }
  }
  buffer += decoder.decode();
  if (encoder.encode(buffer).byteLength > MAX_SSE_LINE || buffer.trim()) throw new NativeEngineError('invalid_engine_stream', 'engine stream ended mid-frame');
}

export class NativeEngineClient {
  constructor({ endpoint, token, model, backend, timeoutMs = 120000, maxTokens = 256 } = {}) {
    this.baseUrl = endpointUrl(endpoint); if (typeof token !== 'string' || token.length < 16 || token.length > 512) throw new NativeEngineError('invalid_engine_token', 'engine bearer token is invalid');
    if (typeof model !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(model)) throw new NativeEngineError('invalid_engine_model', 'native model identity is required');
    if (typeof backend !== 'string' || !/^[A-Za-z0-9._/-]{1,128}$/.test(backend)) throw new NativeEngineError('invalid_engine_backend', 'native backend identity is required');
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 120000) throw new NativeEngineError('invalid_engine_timeout', 'native engine timeout must be 1000-120000ms');
    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 256) throw new NativeEngineError('invalid_engine_max_tokens', 'native maxTokens must be 1-256');
    this.token = token; this.model = model; this.backend = backend; this.timeoutMs = timeoutMs; this.maxTokens = maxTokens; this.sessions = new Map(); this.active = new Map(); this.closed = false;
  }
  headers(extra = {}) { return { authorization: `Bearer ${this.token}`, ...extra }; }
  async request(path, options = {}, { signal, timeoutMs = this.timeoutMs } = {}) {
    if (this.closed) throw new NativeEngineError('engine_client_closed', 'native engine client is closed');
    if (typeof options.body === 'string' && new TextEncoder().encode(options.body).byteLength > MAX_REQUEST_JSON_BYTES)
      throw new NativeEngineError('engine_request_too_large', 'native JSON request exceeded the size limit');
    const timeoutSignal = AbortSignal.timeout(timeoutMs);
    const requestSignal = signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal;
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
  async ready() { const response = await this.request('/readyz'); const data = await readJson(response); return { ready: data.ready === true, lifecycle: data.lifecycle ?? 'UNKNOWN' }; }
  async waitReady({ timeoutMs = this.timeoutMs, intervalMs = 25 } = {}) { const deadline = Date.now() + timeoutMs; while (Date.now() < deadline) { try { const status = await this.ready(); if (status.ready) return status; } catch (error) { if (!(error instanceof NativeEngineError) || !['engine_timeout', 'http_503', 'not_ready'].includes(error.code)) throw error; } await delay(intervalMs); } throw new NativeEngineError('engine_not_ready', 'native engine readiness timed out'); }
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
    const active = this.active.get(requestId); if (!active) return false; active.cancelled = true; if (active.nativeRequestId) void this.postCancel(active.nativeRequestId); active.abort.abort(); return true;
  }
  async postCancel(nativeRequestId) { if (!REQUEST_ID.test(nativeRequestId)) return false; try { const response = await this.request(`/v1/cancel/${encodeURIComponent(nativeRequestId)}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, { timeoutMs: 2000 }); const result = await readJson(response); return result.cancelled === true; } catch { return false; } }
  async *generate({ requestId, sessionId, messages = [], mode = 'normal', tools = [], signal }) {
    if (!REQUEST_ID.test(requestId)) throw new NativeEngineError('invalid_request_id', 'host request id is invalid');
    if (!Array.isArray(messages) || messages.length < 1 || messages.length > 64) throw new NativeEngineError('invalid_messages', 'native message history is invalid');
    if (!Array.isArray(tools) || tools.length > 32) throw new NativeEngineError('invalid_tools', 'native tool definitions are invalid');
    if (!['normal', 'deep'].includes(mode)) throw new NativeEngineError('invalid_mode', 'native mode is invalid');
    const localAbort = new AbortController(); const relay = () => { localAbort.abort(); }; let timedOut = false;
    signal?.addEventListener('abort', relay, { once: true }); const active = { abort: localAbort, nativeRequestId: null, cancelled: false }; this.active.set(requestId, active);
    const generationTimer = setTimeout(() => { timedOut = true; localAbort.abort(); if (active.nativeRequestId) void this.postCancel(active.nativeRequestId); }, this.timeoutMs);
    try {
      const nativeSessionId = await this.ensureSession(sessionId ?? 'ses_native_default', localAbort.signal);
      const payload = { model: this.model, session_id: nativeSessionId, messages, tools, stream: true, max_tokens: this.maxTokens, mode };
      const response = await this.request('/v1/chat/completions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) }, { signal: localAbort.signal });
      active.nativeRequestId = response.headers.get('x-request-id'); if (active.cancelled && active.nativeRequestId) void this.postCancel(active.nativeRequestId);
      const decoder = new ToolCallStreamDecoder();
      for await (const chunk of sseEvents(response, localAbort.signal)) {
        const choice = chunk.choices?.[0]; const delta = choice?.delta?.content;
        if (typeof delta === 'string' && delta) for (const event of decoder.push(delta)) yield event;
        const finish = choice?.finish_reason; if (finish) { if (finish === 'cancelled' || active.cancelled) throw Object.assign(new NativeEngineError('cancelled', 'native generation cancelled'), { code: 'cancelled' }); yield { kind: 'done', finish_reason: finish, usage: { completion_tokens: 0 } }; }
      }
      for (const event of decoder.finish()) yield event;
    } catch (error) {
      if (timedOut || error?.name === 'TimeoutError' || (error?.name === 'AbortError' && !active.cancelled && !signal?.aborted)) throw Object.assign(new NativeEngineError('engine_timeout', 'native generation timed out'), { code: 'engine_timeout' });
      if (active.cancelled || signal?.aborted || error?.code === 'cancelled') throw Object.assign(new NativeEngineError('cancelled', 'native generation cancelled'), { code: 'cancelled' });
      throw error;
    } finally { clearTimeout(generationTimer); signal?.removeEventListener('abort', relay); this.active.delete(requestId); }
  }
  async shutdown() { for (const active of this.active.values()) active.abort.abort(); this.active.clear(); const sessions = [...this.sessions.keys()]; await Promise.allSettled(sessions.map(id => this.deleteSession(id))); this.closed = true; }
}
