import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, normalize } from 'node:path';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { sseFrame } from '../agent/assistant-events.mjs';
import { mergeConfig } from '../agent/config.mjs';

const UI_ROOT = join(import.meta.dirname, '..', '..', 'ui');
const ASSETS = new Map([['/', ['index.html', 'text/html; charset=utf-8']], ['/index.html', ['index.html', 'text/html; charset=utf-8']], ['/app.js', ['app.js', 'text/javascript; charset=utf-8']], ['/styles.css', ['styles.css', 'text/css; charset=utf-8']]]);
const LOCAL_HOST = /^(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$/i;

function json(res, status, value) { const body = JSON.stringify(value); res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body), 'cache-control': 'no-store', ...securityHeaders() }); res.end(body); }
function securityHeaders() { return { 'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'", 'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer', 'permissions-policy': 'camera=(), microphone=(), geolocation=()' }; }
function tokenMatch(actual, expected) { if (typeof actual !== 'string' || actual.length !== expected.length) return false; return timingSafeEqual(Buffer.from(actual), Buffer.from(expected)); }
async function body(req, maxBytes, timeoutMs) {
  let total = 0; const chunks = []; req.setTimeout(timeoutMs, () => req.destroy(Object.assign(new Error('request_timeout'), { code: 'request_timeout' })));
  for await (const chunk of req) { total += chunk.length; if (total > maxBytes) throw Object.assign(new Error('body_too_large'), { code: 'body_too_large' }); chunks.push(chunk); }
  if (!total) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw Object.assign(new Error('invalid_json'), { code: 'invalid_json' }); }
}

export class HostServer {
  constructor({ controller, engine, config = {} } = {}) {
    if (!controller) throw new TypeError('controller is required');
    this.controller = controller; this.engine = engine; this.config = mergeConfig(config); this.token = randomBytes(32).toString('base64url'); this.server = null; this.port = null;
  }
  async listen(port = 0) {
    if (this.server) return this.address();
    this.server = http.createServer((req, res) => this.handle(req, res));
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(port, '127.0.0.1', resolve); });
    this.port = this.server.address().port; return this.address();
  }
  address() { return { host: '127.0.0.1', port: this.port, token: this.token, url: `http://127.0.0.1:${this.port}` }; }
  async close() { if (!this.server) return; await new Promise(resolve => this.server.close(() => resolve())); this.server = null; await this.engine?.shutdown?.(); }
  allowedRequest(req) {
    if (req.socket.remoteAddress && !['127.0.0.1', '::ffff:127.0.0.1', '::1'].includes(req.socket.remoteAddress)) return false;
    if (!LOCAL_HOST.test(req.headers.host ?? '')) return false;
    const origin = req.headers.origin;
    if (origin && !/^https?:\/\/(?:127\.0\.0\.1|localhost):\d{1,5}$/i.test(origin)) return false;
    return true;
  }
  authorized(req) { const match = /^Bearer\s+(.+)$/i.exec(req.headers.authorization ?? ''); return Boolean(match && tokenMatch(match[1], this.token)); }
  async handle(req, res) {
    res.setHeader('x-content-type-options', 'nosniff');
    if (!this.allowedRequest(req)) return json(res, 403, { error: 'forbidden' });
    const path = new URL(req.url ?? '/', 'http://127.0.0.1').pathname;
    if (req.method === 'GET' && path === '/healthz') return json(res, 200, { ok: true });
    if (!this.authorized(req)) return json(res, 401, { error: 'unauthorized' });
    if (req.method === 'GET' && ASSETS.has(path)) return this.asset(path, res);
    if (req.method === 'GET' && path === '/api/status') return this.status(res);
    try {
      if (req.method === 'POST' && path === '/api/sessions') { const input = await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms); const session = this.controller.createSession(input.session_id); if (input.reset) this.controller.resetSession(session.id); return json(res, 201, { session_id: session.id, state: this.controller.state(session.id) }); }
      if (req.method === 'POST' && path === '/api/chat') return this.chat(req, res);
      if (req.method === 'POST' && path === '/api/cancel') { const input = await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms); const cancelled = this.controller.cancel(input.request_id); return json(res, cancelled ? 200 : 404, { cancelled }); }
      const confirmation = path.match(/^\/api\/tool-confirmations\/([A-Za-z0-9_-]{8,96})$/);
      if (req.method === 'POST' && confirmation) { const input = await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms); const accepted = this.controller.confirm(confirmation[1], input.approved); return json(res, accepted ? 200 : 404, { accepted }); }
      if (req.method === 'POST' && path === '/api/shutdown') { json(res, 200, { shutting_down: true }); setImmediate(() => this.close()); return; }
      return json(res, 404, { error: 'not_found' });
    } catch (error) { return json(res, error.code === 'body_too_large' ? 413 : 400, { error: error.code ?? 'bad_request' }); }
  }
  async asset(path, res) {
    const [file, type] = ASSETS.get(path); const candidate = normalize(join(UI_ROOT, file));
    if (!candidate.startsWith(`${UI_ROOT}/`)) return json(res, 404, { error: 'not_found' });
    try { let content = await readFile(candidate, 'utf8'); if (file === 'index.html') content = content.replaceAll('__LAE_BOOTSTRAP__', this.token); res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store', ...securityHeaders() }); res.end(content); } catch { json(res, 404, { error: 'not_found' }); }
  }
  async status(res) { let engine = { ready: false, backend: 'unknown' }; try { engine = await this.engine?.health?.() ?? engine; } catch { /* generic status only */ } json(res, 200, { host: { bind: '127.0.0.1', port: this.port }, engine, network: { provider: this.config.network.provider, enabled: this.config.network.provider !== 'disabled' }, limits: { max_body_bytes: this.config.host.max_body_bytes } }); }
  async chat(req, res) {
    const input = await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms);
    if (typeof input.session_id !== 'string' || typeof input.message !== 'string') return json(res, 400, { error: 'invalid_chat_request' });
    const abort = new AbortController(); let finished = false;
    res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-store', connection: 'keep-alive', ...securityHeaders() });
    res.on('close', () => { if (!finished) abort.abort(); });
    await this.controller.runTurn({ sessionId: input.session_id, message: input.message, mode: input.mode ?? 'normal', requestId: input.request_id, signal: abort.signal, onEvent: event => { if (!res.destroyed) res.write(sseFrame(event)); } });
    finished = true; res.end();
  }
}
