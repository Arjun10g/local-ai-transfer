import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, resolve, relative, isAbsolute, sep } from 'node:path';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { sseFrame } from '../agent/assistant-events.mjs';
import { mergeConfig } from '../agent/config.mjs';
import { parseStrictJson } from '../agent/tool-envelope.mjs';
import { readProviderAuthStatus } from '../providers/microsoft-graph.mjs';

const UI_ROOT = join(import.meta.dirname, '..', '..', 'ui');
const ASSETS = new Map([['/', ['index.html', 'text/html; charset=utf-8']], ['/index.html', ['index.html', 'text/html; charset=utf-8']], ['/app.js', ['app.js', 'text/javascript; charset=utf-8']], ['/styles.css', ['styles.css', 'text/css; charset=utf-8']]]);
const LOCAL_HOST = /^(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$/i;
const OPAQUE_ID = /^[A-Za-z0-9_-]{8,96}$/;
const BOOTSTRAP_NONCE = /^[A-Za-z0-9_-]{43}$/;
const BOOTSTRAP_TTL_MS = 60_000;
const AUTH_STATES = new Set(['disabled', 'unconfigured', 'idle', 'requesting_device_code', 'awaiting_user', 'authenticated', 'checking_account', 'expired', 'failed', 'offline', 'unauthorized']);
const AUTH_USER_CODE = /^(?=.{1,128}$)[^\s\p{Cc}\p{Cf}]+$/u;
const AUTH_VERIFICATION_URIS = new Set(['https://microsoft.com/devicelogin', 'https://www.microsoft.com/devicelogin', 'https://login.microsoftonline.com/common/oauth2/deviceauth']);

/** Platform-neutral containment check; avoids assuming `/` on Windows. */
export function isWithinDirectory(root, target) {
  const rootPath = resolve(root); const targetPath = resolve(target); const rel = relative(rootPath, targetPath);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

function json(res, status, value) { const body = JSON.stringify(value); res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body), 'cache-control': 'no-store', ...securityHeaders() }); res.end(body); }
function securityHeaders() { return { 'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'", 'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer', 'permissions-policy': 'camera=(), microphone=(), geolocation=()' }; }
function authStatusProjection(value) {
  try {
    const issued = readProviderAuthStatus(value);
    if (!issued) return { state: 'unavailable', prompt: null, account_verified: false };
    const state = AUTH_STATES.has(issued.state) ? issued.state : 'unavailable';
    const rawPrompt = issued.prompt;
    let prompt = null;
    if (rawPrompt && typeof rawPrompt === 'object' && !Array.isArray(rawPrompt) && Object.getPrototypeOf(rawPrompt) === Object.prototype && typeof rawPrompt.userCode === 'string' && AUTH_USER_CODE.test(rawPrompt.userCode) && typeof rawPrompt.verificationUri === 'string' && rawPrompt.verificationUri.length <= 256) {
      try { const verification = new URL(rawPrompt.verificationUri); if (!verification.username && !verification.password && !verification.search && !verification.hash && AUTH_VERIFICATION_URIS.has(verification.toString())) prompt = { userCode: rawPrompt.userCode, verificationUri: verification.toString() }; } catch {}
    }
    return { state, prompt, account_verified: state === 'authenticated' && issued.accountVerified === true };
  } catch { return { state: 'unavailable', prompt: null, account_verified: false }; }
}
function resolveAuthControl(providerAuth) { try { return providerAuth?.()?.microsoft_graph ?? null; } catch { return null; } }
function safeAuthStatus(control) { try { return authStatusProjection(control?.status?.()); } catch { return authStatusProjection(null); } }
function tokenMatch(actual, expected) { if (typeof actual !== 'string' || typeof expected !== 'string') return false; const actualBytes = Buffer.from(actual, 'utf8'); const expectedBytes = Buffer.from(expected, 'utf8'); if (actualBytes.length !== expectedBytes.length) return false; return timingSafeEqual(actualBytes, expectedBytes); }
function jsonContentType(req) { return /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|utf8))?$/i.test(req.headers['content-type'] ?? ''); }
function exactBody(input, allowed, required = []) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw Object.assign(new Error('invalid_request_body'), { code: 'invalid_request_body' });
  if (Object.keys(input).some(key => !allowed.includes(key)) || required.some(key => !Object.hasOwn(input, key))) throw Object.assign(new Error('invalid_request_body'), { code: 'invalid_request_body' });
  return input;
}
function errorStatus(code) {
  if (code === 'body_too_large') return 413;
  if (code === 'request_timeout') return 408;
  if (code === 'action_reconciliation_unavailable') return 501;
  if (code === 'action_journal_not_found') return 404;
  if (code === 'action_journal_invalid_request' || code === 'invalid_json' || code?.startsWith('invalid_') || code === 'unsupported_content_type') return 400;
  if (code?.startsWith('action_journal_')) return 409;
  return 500;
}
async function body(req, maxBytes, timeoutMs) {
  const declared = Number(req.headers['content-length']);
  if (Number.isSafeInteger(declared) && declared > maxBytes) throw Object.assign(new Error('body_too_large'), { code: 'body_too_large' });
  let total = 0; const chunks = []; const timeout = () => req.destroy(Object.assign(new Error('request_timeout'), { code: 'request_timeout' }));
  req.setTimeout(timeoutMs, timeout);
  try {
    for await (const chunk of req) { total += chunk.length; if (total > maxBytes) throw Object.assign(new Error('body_too_large'), { code: 'body_too_large' }); chunks.push(chunk); }
    if (!total) return {};
    try { return parseStrictJson(Buffer.concat(chunks).toString('utf8'), { maxBytes, maxDepth: 16, maxString: 8192, maxArray: 64, maxObject: 64 }); } catch { throw Object.assign(new Error('invalid_json'), { code: 'invalid_json' }); }
  } finally { req.setTimeout(0); }
}

export class HostServer {
  constructor({ controller, engine, config = {}, providers, providerAuth, providerShutdown, operatorGrants, actionJournal, localCapabilities } = {}) {
    if (!controller) throw new TypeError('controller is required');
    const controllerJournal = controller.actionJournal;
    if (controllerJournal !== undefined && actionJournal !== undefined && controllerJournal !== actionJournal) throw new TypeError('controller and host action journals must be identical');
    this.controller = controller; this.engine = engine; this.config = mergeConfig(config); this.providers = providers; this.providerAuth = providerAuth; this.providerShutdown = providerShutdown; this.operatorGrants = operatorGrants; this.localCapabilities = localCapabilities; this.actionJournal = actionJournal ?? controllerJournal; this.actionJournalBound = this.actionJournal !== undefined && this.controller.actionJournal === this.actionJournal; this.token = randomBytes(32).toString('base64url'); this.bootstrapNonce = randomBytes(32).toString('base64url'); this.bootstrapExpiresAt = 0; this.bootstrapUsed = false; this.server = null; this.port = null; this.authFailures = new Map(); this.closePromise = null;
  }
  async listen(port = 0) {
    if (this.server) return this.address();
    this.server = http.createServer({ maxHeaderSize: this.config.host.max_header_bytes }, (req, res) => { void this.handle(req, res).catch(error => this.fail(res, error)); });
    // Node enforces this socket cap; the controller separately enforces one active generation.
    this.server.maxConnections = this.config.host.max_connections;
    this.server.maxHeadersCount = this.config.host.max_header_count;
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(port, '127.0.0.1', resolve); });
    this.port = this.server.address().port; this.bootstrapExpiresAt = Date.now() + BOOTSTRAP_TTL_MS; return this.address();
  }
  address() { const url = `http://127.0.0.1:${this.port}`; return { host: '127.0.0.1', port: this.port, token: this.token, url, bootstrap_url: this.bootstrapNonce ? `${url}/#bootstrap=${encodeURIComponent(this.bootstrapNonce)}` : null }; }
  async close() {
    if (this.closePromise) return this.closePromise;
    this.closePromise = (async () => {
      this.operatorGrants?.revokeAll?.(); this.controller.cancelActive?.();
      try {
        await this.providerShutdown?.();
        if (this.server) {
          const server = this.server;
          await new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); });
          this.server = null;
        }
      } finally { await this.engine?.shutdown?.(); }
    })();
    return this.closePromise;
  }
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
  async bootstrap(req, res) {
    const expectedOrigin = `http://127.0.0.1:${this.port}`;
    const expectedHost = `127.0.0.1:${this.port}`;
    if (req.headers.host !== expectedHost || req.headers.origin !== expectedOrigin || req.headers.referer !== undefined || req.headers['sec-fetch-site'] !== 'same-origin') return json(res, 403, { error: 'forbidden' });
    if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' });
    const input = exactBody(await body(req, Math.min(this.config.host.max_body_bytes, 1024), this.config.host.request_timeout_ms), ['nonce'], ['nonce']);
    if (this.bootstrapUsed || Date.now() > this.bootstrapExpiresAt || !this.bootstrapNonce) return json(res, 410, { error: 'bootstrap_unavailable' });
    if (typeof input.nonce !== 'string' || !BOOTSTRAP_NONCE.test(input.nonce) || !tokenMatch(input.nonce, this.bootstrapNonce)) { this.recordAuthFailure(req); return json(res, this.authLimited(req) ? 429 : 401, { error: this.authLimited(req) ? 'auth_rate_limited' : 'invalid_bootstrap' }); }
    this.bootstrapUsed = true; this.bootstrapNonce = null; this.clearAuthFailure(req);
    return json(res, 200, { token: this.token });
  }
  fail(res, error) { if (res.destroyed || res.writableEnded) return; if (res.headersSent) { res.end(); return; } const code = error?.code ?? 'request_failed'; json(res, errorStatus(code), { error: code }); }
  async handle(req, res) {
    res.setHeader('x-content-type-options', 'nosniff');
    if (!this.allowedRequest(req)) return json(res, 403, { error: 'forbidden' });
    const requestUrl = new URL(req.url ?? '/', 'http://127.0.0.1');
    const path = requestUrl.pathname;
    if (req.method === 'GET' && path === '/healthz') return json(res, 200, { ok: true });
    if (req.method === 'GET' && ASSETS.has(path) && !requestUrl.search) return this.asset(path, res);
    if (req.method === 'POST' && path === '/bootstrap' && !requestUrl.search) return this.bootstrap(req, res);
    if (!this.authorized(req)) { this.recordAuthFailure(req); return json(res, this.authLimited(req) ? 429 : 401, { error: this.authLimited(req) ? 'auth_rate_limited' : 'unauthorized' }); }
    this.clearAuthFailure(req);
    if (req.method === 'GET' && path === '/api/status') return this.status(res);
    if (req.method === 'GET' && path === '/api/provider-auth/microsoft_graph') return this.providerAuthStatus(res);
    if (req.method === 'GET' && path === '/api/operator-grants') return json(res, 200, { capabilities: this.operatorGrants?.list?.() ?? [] });
    try {
      if (req.method === 'GET' && path === '/api/action-journal') {
        if (!this.actionJournal) return json(res, 409, { error: 'action_journal_unavailable' });
        const keys = [...requestUrl.searchParams.keys()]; if (keys.some(key => !['limit', 'state'].includes(key)) || new Set(keys).size !== keys.length) return json(res, 400, { error: 'action_journal_invalid_request' });
        const rawLimit = requestUrl.searchParams.get('limit'); const state = requestUrl.searchParams.get('state') ?? undefined; const limit = rawLimit === null ? 100 : Number(rawLimit);
        return json(res, 200, await this.actionJournal.summary({ limit, state }));
      }
      const actionDetail = path.match(/^\/api\/action-journal\/(act_[a-f0-9]{32})$/u);
      if (req.method === 'GET' && actionDetail && !requestUrl.search) {
        if (!this.actionJournal) return json(res, 409, { error: 'action_journal_unavailable' });
        return json(res, 200, await this.actionJournal.detail(actionDetail[1]));
      }
      const actionResolution = path.match(/^\/api\/action-journal\/(act_[a-f0-9]{32})\/resolve$/u);
      if (req.method === 'POST' && actionResolution && !requestUrl.search) {
        if (!this.actionJournal) return json(res, 409, { error: 'action_journal_unavailable' }); if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' });
        const input = exactBody(await body(req, Math.min(this.config.host.max_body_bytes, 1024), this.config.host.request_timeout_ms), ['resolution'], ['resolution']);
        if (!['completed', 'failed_definitive'].includes(input.resolution)) return json(res, 400, { error: 'action_journal_invalid_request' });
        return json(res, 200, { resolved: true, receipt: await this.actionJournal.resolve(actionResolution[1], input.resolution) });
      }
      const actionReconcile = path.match(/^\/api\/action-journal\/(act_[a-f0-9]{32})\/reconcile$/u);
      if (req.method === 'POST' && actionReconcile && !requestUrl.search) {
        if (!this.actionJournal) return json(res, 409, { error: 'action_journal_unavailable' }); if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); exactBody(await body(req, Math.min(this.config.host.max_body_bytes, 1024), this.config.host.request_timeout_ms), []);
        return json(res, 501, { error: 'action_reconciliation_unavailable' });
      }
      if (req.method === 'POST' && path === '/api/sessions') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['session_id', 'reset']); if (input.session_id !== undefined && (typeof input.session_id !== 'string' || !OPAQUE_ID.test(input.session_id))) return json(res, 400, { error: 'invalid_request_body' }); if (input.reset !== undefined && typeof input.reset !== 'boolean') return json(res, 400, { error: 'invalid_request_body' }); const session = this.controller.createSession(input.session_id); if (input.reset) this.controller.resetSession(session.id); return json(res, 201, { session_id: session.id, state: this.controller.state(session.id) }); }
      if (req.method === 'POST' && path === '/api/chat') return await this.chat(req, res);
      const authAction = path.match(/^\/api\/provider-auth\/microsoft_graph\/(start|cancel|clear)$/);
      if (req.method === 'POST' && authAction) { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), []); const control = resolveAuthControl(this.providerAuth); let configured = false; try { configured = control?.configured === true; } catch {} if (!configured) return json(res, 409, { error: 'provider_unconfigured' }); if (authAction[1] === 'start') { void Promise.resolve().then(() => control.start()).catch(() => {}); return json(res, 202, { accepted: true, status: safeAuthStatus(control) }); } let accepted = true; try { if (authAction[1] === 'cancel') control.cancel(); else control.clear(); } catch { accepted = false; } return json(res, accepted ? 200 : 503, { accepted, status: safeAuthStatus(control) }); }
      if (req.method === 'POST' && path === '/api/cancel') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['request_id'], ['request_id']); if (typeof input.request_id !== 'string' || !OPAQUE_ID.test(input.request_id)) return json(res, 400, { error: 'invalid_request_id' }); const cancelled = this.controller.cancel(input.request_id); return json(res, cancelled ? 200 : 404, { cancelled }); }
      if (req.method === 'POST' && path === '/api/operator-grants/revoke-all') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), []); this.controller.cancelActive?.(); return json(res, 200, this.operatorGrants?.revokeAll?.() ?? { revoked: 0 }); }
      const grant = path.match(/^\/api\/operator-grants\/([A-Za-z0-9_.:-]{1,256})$/);
      if (req.method === 'POST' && grant) {
        if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' });
        const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['granted', 'duration_ms'], ['granted']);
        if (typeof input.granted !== 'boolean' || (input.granted && (!Number.isInteger(input.duration_ms) || input.duration_ms < 60000 || input.duration_ms > 28800000)) || (!input.granted && input.duration_ms !== undefined)) return json(res, 400, { error: 'invalid_request_body' });
        const value = input.granted ? this.operatorGrants?.grant?.(grant[1], input.duration_ms) : this.operatorGrants?.revoke?.(grant[1]);
        return value ? json(res, 200, value) : json(res, 404, { error: 'unknown_capability' });
      }
      const confirmation = path.match(/^\/api\/tool-confirmations\/([A-Za-z0-9_-]{8,96})$/);
      if (req.method === 'POST' && confirmation) { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); const input = exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), ['approved', 'request_id', 'call_id'], ['approved', 'request_id', 'call_id']); if (typeof input.approved !== 'boolean' || typeof input.request_id !== 'string' || !OPAQUE_ID.test(input.request_id) || typeof input.call_id !== 'string' || !OPAQUE_ID.test(input.call_id)) return json(res, 400, { error: 'invalid_request_body' }); const accepted = this.controller.confirm(confirmation[1], input.approved, { requestId: input.request_id, callId: input.call_id }); return json(res, accepted ? 200 : 404, { accepted }); }
      if (req.method === 'POST' && path === '/api/shutdown') { if (!jsonContentType(req)) return json(res, 415, { error: 'unsupported_content_type' }); exactBody(await body(req, this.config.host.max_body_bytes, this.config.host.request_timeout_ms), []); json(res, 200, { shutting_down: true }); setImmediate(() => this.close()); return; }
      return json(res, 404, { error: 'not_found' });
    } catch (error) { if (res.headersSent) return this.fail(res, error); const code = error.code ?? (error instanceof TypeError ? 'invalid_request_body' : 'request_failed'); return json(res, errorStatus(code), { error: code }); }
  }
  providerAuthStatus(res) { const control = resolveAuthControl(this.providerAuth); return json(res, 200, { microsoft_graph: safeAuthStatus(control) }); }
  async asset(path, res) {
    const [file, type] = ASSETS.get(path); const candidate = resolve(join(UI_ROOT, file));
    if (!isWithinDirectory(UI_ROOT, candidate)) return json(res, 404, { error: 'not_found' });
    try { const content = await readFile(candidate, 'utf8'); res.writeHead(200, { 'content-type': type, 'cache-control': 'no-store', ...securityHeaders() }); res.end(content); } catch { json(res, 404, { error: 'not_found' }); }
  }
  async status(res) { let engine = { ready: false, backend: 'unknown' }; try { engine = await this.engine?.health?.() ?? engine; } catch { /* generic status only */ } const providers = typeof this.providers === 'function' ? this.providers() : this.providers ?? {}; const journal = this.actionJournal?.health?.() ?? { state: 'unavailable', error: 'action_journal_unavailable' }; const bound = this.actionJournalBound; const journalError = this.actionJournal === undefined ? journal.error : bound ? journal.error ?? null : 'action_journal_controller_mismatch'; json(res, 200, { host: { bind: '127.0.0.1', port: this.port }, engine, network: { provider: this.config.network.provider, enabled: this.config.network.provider !== 'disabled' }, providers, local_capabilities: this.localCapabilities ?? { basis: 'unavailable', platform: 'unknown', tools: {} }, action_journal: { state: journal.state, error: journalError, bound_to_controller: bound, durable_action_dispatch: bound && journal.state === 'ready' }, operator_grants: { available: this.operatorGrants?.list?.().length ?? 0, active: this.operatorGrants?.list?.().filter(value => value.granted).length ?? 0 }, limits: { max_body_bytes: this.config.host.max_body_bytes, max_connections: this.config.host.max_connections } }); }
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
