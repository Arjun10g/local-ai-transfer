import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, resolve, relative, isAbsolute, sep } from 'node:path';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { sseFrame } from '../agent/assistant-events.mjs';
import { mergeConfig } from '../agent/config.mjs';

const UI_ROOT = join(import.meta.dirname, '..', '..', 'ui');
const ASSETS = new Map([['/', ['index.html', 'text/html; charset=utf-8']], ['/index.html', ['index.html', 'text/html; charset=utf-8']], ['/app.js', ['app.js', 'text/javascript; charset=utf-8']], ['/styles.css', ['styles.css', 'text/css; charset=utf-8']]]);
const LOCAL_HOST = /^(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$/i;
const OPAQUE_ID = /^[A-Za-z0-9_-]{8,96}$/;

/** Platform-neutral containment check; avoids assuming `/` on Windows. */
export function isWithinDirectory(root, target) {
  const rootPath = resolve(root); const targetPath = resolve(target); const rel = relative(rootPath, targetPath);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

function json(res, status, value) { const body = JSON.stringify(value); res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body), 'cache-control': 'no-store', ...securityHeaders() }); res.end(body); }
function securityHeaders() { return { 'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'", 'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer', 'permissions-policy': 'camera=(), microphone=(), geolocation=()' }; }
function tokenMatch(actual, expected) { if (typeof actual !== 'string' || actual.length !== expected.length) return false; return timingSafeEqual(Buffer.from(actual), Buffer.from(expected)); }
function jsonContentType(req) { return /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|utf8))?$/i.test(req.headers['content-type'] ?? ''); }
function exactBody(input, allowed, required = []) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw Object.assign(new Error('invalid_request_body'), { code: 'invalid_request_body' });
  if (Object.keys(input).some(key => !allowed.includes(key)) || required.some(key => !Object.hasOwn(input, key))) throw Object.assign(new Error('invalid_request_body'), { code: 'invalid_request_body' });
  return input;
}
async function body(req, maxBytes, timeoutMs) {
  const declared = Number(req.headers['content-length']);
  if (Number.isSafeInteger(declared) && declared > maxBytes) throw Object.assign(new Error('body_too_large'), { code: 'body_too_large' });
  let total = 0; const chunks = []; const timeout = () => req.destroy(Object.assign(new Error('request_timeout'), { code: 'request_timeout' }));
  req.setTimeout(timeoutMs, timeout);
  try {
    for await (const chunk of req) { total += chunk.length; if (total > maxBytes) throw Object.assign(new Error('body_too_large'), { code: 'body_too_large' }); chunks.push(chunk); }
    if (!total) return {};
    try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw Object.assign(new Error('invalid_json'), { code: 'invalid_json' }); }
  } finally { req.setTimeout(0); }
}

export class HostServer {
  constructor({ controller, engine, config = {}, providers } = {}) {
    if (!controller) throw new TypeError('controller is required');
    this.controller = controller; this.engine = engine; this.config = mergeConfig(config); this.providers = providers; this.token = randomBytes(32).toString('base64url'); this.server = null; this.port = null; this.authFailures = new Map();
  }
  async listen(port = 0) {
    if (this.server) return this.address();
    this.server = http.createServer({ maxHeaderSize: this.config.host.max_header_bytes }, (req, res) => { void this.handle(req, res).catch(error => this.fail(res, error)); });
    // Node enforces this socket cap; the controller separately enforces one active generation.
    this.server.maxConnections = this.config.host.max_connections;
    this.server.maxHeadersCount = this.config.host.max_header_count;
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(port, '127.0.0.1', resolve); });
    this.port = this.server.address().port; return this.address();
  }
  address() { return { host: '127.0.0.1', port: this.port, token: this.token, url: `http://127.0.0.1:${this.port}` }; }
  async close() { if (!this.server) return; await new Promise(resolve => this.server.close(() => resolve())); this.server = null; await this.engine?.shutdown?.(); }
  allowedRequest(req) {
    if (req.socket.remoteAddress && !['127.0.0.1', '::ffff:127.0.0.1'].includes(req.socket.remoteAddress)) return false;
    if (!LOCAL_HOST.test(req.headers.host ?? '')) return false;
    const origin = req.headers.origin;
    if (origin && !/^https?:\/\/(?:127\.0\.0\.1|localhost):\d{1,5}$/i.test(origin)) return false;
    return true;
  }
  authorized(req) { const match = /^Bearer\s+(.+)$/i.exec(req.headers.authorization ?? ''); return Boolean(match && tokenMatch(match[1], this.token)); }
  authLimited(req) { const key = req.socket.remoteAddress ?? 'unknown'; const now = Date.now(); const previous = this.authFailures.get(key); if (!previous || previous.until <= now) return false; return previous.count >= 20; }
  recordAuthFailure(req) { const key = req.socket.remoteAddress ?? 'unknown'; const now = Date.now(); const item = this.authFailures.get(key); const next = !item || item.until <= now ? { count: 1, until: now + 60000 } : { count: item.count + 1, until: item.until }; this.authFailures.set(key, next); if (this.authFailures.size > 1024) { const oldest = [...this.authFailures.entries()].sort((a, b) => a[1].until - b[1].until)[0]; if (oldest) this.authFailures.delete(oldest[0]); } }
  clearAuthFailure(req) { this.authFailures.delete(req.socket.remoteAddress ?? 'unknown'); }
  fail(res, error) { if (res.destroyed || res.writableEnded) return; if (res.headersSent) { res.end(); return; } const code = error?.code ?? 'request_failed'; const status = code === 'body_too_large' ? 413 : code === 'request_timeout' ? 408 : 500; json(res, status, { error: code }); }
  async handle(req, res) {
    res.setHeader('x-content-type-options', 'nosniff');
    if (!this.allowedRequest(req)) return json(res, 403, { error: 'forbidden' });
    const path = new URL(req.url ?? '/', 'http://127.0.0.1').pathname;
    if (req.method === 'GET' && path === '/healthz') return json(res, 200, { ok: true });
    if (!this.authorized(req)) { this.recordAuthFailure(req); return json(res, this.authLimited(req) ? 429 : 401, { error: this.authLimited(req) ? 'auth_rate_limited' : 'unauthorized' }); }
    this.clearAuthFailure(req);
    if (req.method === 'GET' && ASSETS.has(path)) return this.asset(path, res);
    if (req.method === 'GET' && path === '/api/status') return this.status(res);
    try {
      if (req.method === 'POST' && path === '/api/sessions') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['session_id', 'reset']); if (input.session_id !== undefined && (typeof input.session_id !== 'string' || !OPAQUE_ID.test(input.session_id))) return json(res, 400, { error: 'invalid_request_body' }); if (input.reset !== undefined && typeof input.reset !== 'boolean') return json(res, 400, { error: 'invalid_request_body' }); const session = this.controller.createSession(input.session_id); if (input.reset) this.controller.resetSession(session.id); return json(res, 201, { session_id: session.id, state: this.controller.state(session.id) }); }
      if (req.method === 'POST' && path === '/api/chat') return await this.chat(req, res);
      if (req.method === 'POST' && path === '/api/cancel') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['request_id'], ['request_id']); if (typeof input.request_id !== 'string' || !OPAQUE_ID.test(input.request_id)) return json(res, 400, { error: 'invalid_request_id' }); const cancelled = this.controller.cancel(input.request_id); return json(res, cancelled ? 200 : 404, { cancelled }); }
      const confirmation = path.match(/^\/api\/tool-confirmations\/([A-Za-z0-9_-]{8,96})$/);
      if (req.method === 'POST' && confirmation) { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['approved', 'request_id', 'call_id'], ['approved', 'request_id', 'call_id']); if (typeof input.approved !== 'boolean' || typeof input.request_id !== 'string' || !OPAQUE_ID.test(input.request_id) || typeof input.call_id !== 'string' || !OPAQUE_ID.test(input.call_id)) return json(res, 400, { error: 'invalid_request_body' }); const accepted = this.controller.confirm(confirmation[1], input.approved, { requestId: input.request_id, callId: input.call_id }); return json(res, accepted ? 200 : 404, { accepted }); }
      if (req.method === 'POST' && path === '/api/shutdown') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), []); json(res, 200, { shutting_down: true }); setImmediate(() => this.close()); return; }
      return json(res, 404, { error: 'not_found' });
    } catch (error) { if (res.headersSent) return this.fail(res, error); const code = error.code ?? 'request_failed'; const status = code === 'body_too_large' ? 413 : code === 'request_timeout' ? 408 : (code.startsWith('invalid_') || code === 'unsupported_content_type') ? 400 : 500; return json(res, status, { error: code }); }
  }
  async asset(path, res) {
    const [file, type] = ASSETS.get(path); const candidate = resolve(join(UI_ROOT, file));
    if (!isWithinDirectory(UI_ROOT, candidate)) return json(res, 404, { error: 'not_found' });
    try { let content = await readFile(candidate, 'utf8'); if (file === 'index.html') content = content.replaceAll('__LAE_BOOTSTRAP__', this.token); res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store', ...securityHeaders() }); res.end(content); } catch { json(res, 404, { error: 'not_found' }); }
  }
  async status(res) { let engine = { ready: false, backend: 'unknown' }; try { engine = await this.engine?.health?.() ?? engine; } catch { /* generic status only */ } const providers = typeof this.providers === 'function' ? this.providers() : this.providers ?? {}; json(res, 200, { host: { bind: '127.0.0.1', port: this.port }, engine, network: { provider: this.config.network.provider, enabled: this.config.network.provider !== 'disabled' }, providers, limits: { max_body_bytes: this.config.host.max_body_bytes, max_connections: this.config.host.max_connections } }); }
  async chat(req, res) {
    if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' });
    const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['session_id', 'message', 'mode', 'request_id'], ['session_id', 'message', 'request_id']);
    if (typeof input.session_id !== 'string' || !OPAQUE_ID.test(input.session_id) || typeof input.message !== 'string' || typeof input.request_id !== 'string' || !OPAQUE_ID.test(input.request_id) || (input.mode !== undefined && typeof input.mode !== 'string')) return json(res, 400, { error: !OPAQUE_ID.test(input.request_id ?? '') ? 'invalid_request_id' : 'invalid_chat_request' });
    const abort = new AbortController(); let finished = false; let headersSent = false; const pending = [];
    const aborted = () => abort.abort(); req.on('aborted', aborted);
    const startStream = () => { if (headersSent || res.destroyed) return; headersSent = true; res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-store', connection: 'keep-alive', ...securityHeaders() }); for (const frame of pending.splice(0)) res.write(frame); };
    const closed = () => { if (!finished) abort.abort(); }; res.on('close', closed);
    try { await this.controller.runTurn({ sessionId: input.session_id, message: input.message, mode: input.mode ?? 'normal', requestId: input.request_id, signal: abort.signal, onEvent: event => { if (res.destroyed) return; const frame = sseFrame(event); if (!headersSent) { pending.push(frame); startStream(); } else res.write(frame); } }); finished = true; if (!headersSent) startStream(); if (!res.destroyed) res.end(); }
    finally { req.off('aborted', aborted); res.off('close', closed); }
  }
}
