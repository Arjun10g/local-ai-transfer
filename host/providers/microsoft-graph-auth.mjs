import { ProviderToolError, checkAborted, digest } from './provider-common.mjs';

const LOGIN_ORIGIN = 'https://login.microsoftonline.com';
const GRAPH_ORIGIN = 'https://graph.microsoft.com';
const DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code';
const MAX_AUTH_BODY_BYTES = 256 * 1024;
const MAX_AUTH_RETRIES = 2;
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;
const GRAPH_PATH = /^\/v1\.0\/(?:me(?:\/mailFolders\/[^/]+\/messages|\/messages(?:\/[^/]+(?:\/send)?)?|\/chats)?|chats\/[^/]+\/messages)$/u;
const GRAPH_SCOPES = new Set(['User.Read', 'Mail.Read', 'Mail.ReadWrite', 'Mail.Send', 'Chat.Read', 'Chat.ReadWrite', 'ChatMessage.Send']);
const AUTH_HEADERS = new Set(['accept', 'authorization', 'content-type', 'prefer', 'idempotency-key']);

const safeTenant = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$/u.test(value) && !value.includes('..') && !/[.-]$/u.test(value);
const safeClientId = value => typeof value === 'string' && GUID.test(value);
const scopeSatisfies = (granted, required) => granted.has(required) || required === 'Mail.Read' && granted.has('Mail.ReadWrite') || required === 'Chat.Read' && granted.has('Chat.ReadWrite') || required === 'ChatMessage.Send' && granted.has('Chat.ReadWrite');
const safeScopes = value => Array.isArray(value) && value.length > 0 && value.length <= GRAPH_SCOPES.size && new Set(value).size === value.length && value.includes('User.Read') && value.every(scope => typeof scope === 'string' && GRAPH_SCOPES.has(scope));
const waitDefault = (milliseconds, signal) => new Promise((resolve, reject) => { let settled = false; const finish = (error) => { if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); error ? reject(error) : resolve(); }; const abort = () => finish(new ProviderToolError('provider_cancelled')); const timer = setTimeout(() => finish(), milliseconds); if (signal?.aborted) finish(new ProviderToolError('provider_cancelled')); else signal?.addEventListener('abort', abort, { once: true }); });
const raceAbort = (promise, signal) => { if (!signal) return promise; checkAborted(signal); return new Promise((resolve, reject) => { const abort = () => { signal.removeEventListener('abort', abort); reject(new ProviderToolError('provider_cancelled')); }; signal.addEventListener('abort', abort, { once: true }); promise.then(value => { signal.removeEventListener('abort', abort); resolve(value); }, error => { signal.removeEventListener('abort', abort); reject(error); }); }); };

async function readBoundedJson(response, maxBytes = MAX_AUTH_BODY_BYTES) {
  if (!response?.body) return { value: {}, bytes: 0 };
  const chunks = []; let bytes = 0;
  for await (const chunk of response.body) { const buffer = Buffer.from(chunk); bytes += buffer.byteLength; if (bytes > maxBytes) throw new ProviderToolError('provider_response_too_large'); chunks.push(buffer); }
  if (!chunks.length) return { value: {}, bytes: 0 };
  try { const value = JSON.parse(Buffer.concat(chunks).toString('utf8')); return { value: value && typeof value === 'object' && !Array.isArray(value) ? value : {}, bytes }; } catch { throw new ProviderToolError('provider_invalid_response'); }
}

const retryAfter = headers => { const raw = headers?.get?.('retry-after') ?? headers?.['retry-after']; const seconds = Number(raw); return Number.isFinite(seconds) && seconds >= 0 && seconds <= 30 ? seconds * 1000 : 500; };

export class MicrosoftGraphHttpsTransport {
  constructor({ fetchImpl = globalThis.fetch, requestTimeoutMs = 10000, sleep = waitDefault } = {}) {
    if (typeof fetchImpl !== 'function') throw new TypeError('HTTPS fetch implementation is required');
    if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 100 || requestTimeoutMs > 120000) throw new TypeError('invalid HTTPS request timeout');
    if (typeof sleep !== 'function') throw new TypeError('HTTPS sleep implementation is required');
    this.fetchImpl = fetchImpl; this.requestTimeoutMs = requestTimeoutMs; this.sleep = sleep;
  }
  async request({ origin, method, path, query = {}, headers = {}, body, signal }) {
    if (![GRAPH_ORIGIN, LOGIN_ORIGIN].includes(origin) || typeof method !== 'string' || !/^(GET|POST|PATCH)$/u.test(method) || typeof path !== 'string' || path.length < 1 || path.length > 2048 || path.includes('//') || path.includes('..') || path.includes('?') || path.includes('#') || /[\u0000-\u001f\u007f]/u.test(path)) throw new ProviderToolError('provider_destination_rejected');
    if (origin === GRAPH_ORIGIN && !GRAPH_PATH.test(path)) throw new ProviderToolError('provider_destination_rejected');
    if (origin === LOGIN_ORIGIN && !/^\/[A-Za-z0-9][A-Za-z0-9.-]{0,127}\/oauth2\/v2\.0\/(?:devicecode|token)$/u.test(path)) throw new ProviderToolError('provider_destination_rejected');
    if (origin === LOGIN_ORIGIN && method !== 'POST') throw new ProviderToolError('provider_destination_rejected');
    if (!query || typeof query !== 'object' || Array.isArray(query) || Object.entries(query).some(([key, value]) => !/^[A-Za-z0-9_$.-]{1,64}$/u.test(key) || (typeof value === 'number' && !Number.isFinite(value)) || !['string', 'number', 'boolean'].includes(typeof value))) throw new ProviderToolError('provider_destination_rejected');
    const url = new URL(path, origin); for (const [key, value] of Object.entries(query)) url.searchParams.set(key, String(value));
    if (!headers || typeof headers !== 'object' || Array.isArray(headers) || Object.keys(headers).length > AUTH_HEADERS.size) throw new ProviderToolError('provider_destination_rejected');
    const requestHeaders = { accept: 'application/json' };
    for (const [name, value] of Object.entries(headers)) { const normalized = name.toLowerCase(); if (!AUTH_HEADERS.has(normalized) || Object.hasOwn(requestHeaders, normalized) || typeof value !== 'string' || value.length > 8192 || /[\u0000-\u001f\u007f]/u.test(value)) throw new ProviderToolError('provider_destination_rejected'); requestHeaders[normalized] = value; }
    let requestBody = body;
    if (body !== undefined && body !== null && typeof body === 'object') { if (Array.isArray(body)) throw new ProviderToolError('provider_destination_rejected'); requestHeaders['content-type'] ??= 'application/json'; requestBody = JSON.stringify(body); }
    if (typeof requestBody === 'string' && Buffer.byteLength(requestBody, 'utf8') > MAX_AUTH_BODY_BYTES) throw new ProviderToolError('provider_response_too_large');
    const canRetry = (origin === GRAPH_ORIGIN && method === 'GET') || (origin === LOGIN_ORIGIN && path.endsWith('/token'));
    let attempts = 0;
    while (true) {
      checkAborted(signal); const timeout = AbortSignal.timeout(this.requestTimeoutMs); const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
      let response;
      try { response = await this.fetchImpl(url, { method, headers: requestHeaders, body: requestBody, redirect: 'error', signal: combined }); }
      catch (error) { if (signal?.aborted) throw new ProviderToolError('provider_cancelled'); if (error?.name === 'TimeoutError' || error?.name === 'AbortError') throw new ProviderToolError('provider_timeout'); throw new ProviderToolError('provider_offline'); }
      if (!response || !Number.isInteger(response.status) || response.status < 100 || response.status > 599) throw new ProviderToolError('provider_invalid_response');
      const rawLength = response.headers?.get?.('content-length'); if (rawLength !== undefined && rawLength !== null && (!/^\d+$/u.test(String(rawLength)) || Number(rawLength) > MAX_AUTH_BODY_BYTES)) throw new ProviderToolError('provider_response_too_large');
      if (canRetry && [429, 503].includes(response.status) && attempts++ < MAX_AUTH_RETRIES) { await readBoundedJson(response); await this.sleep(retryAfter(response.headers), signal); continue; }
      const parsed = await readBoundedJson(response);
      const contentType = response.headers?.get?.('content-type') ?? '';
      const emptySuccess = parsed.bytes === 0 && [202, 204].includes(response.status);
      if (!emptySuccess && !/^(?:application\/json|application\/[^;]+\+json)(?:;|$)/iu.test(contentType)) throw new ProviderToolError('provider_invalid_response');
      return { status: response.status, headers: response.headers, body: parsed.value };
    }
  }
}

export class MicrosoftDeviceCodeCredential {
  constructor({ tenant, clientId, scopes, transport, now = () => Date.now(), sleep = waitDefault, requestTimeoutMs = 10000, onUserCode } = {}) {
    if (!safeTenant(tenant) || !safeClientId(clientId) || !safeScopes(scopes)) throw new TypeError('explicit Microsoft tenant, client ID, and scopes are required');
    if (!transport || typeof transport.request !== 'function') throw new TypeError('Microsoft auth transport is required');
    this.tenant = tenant; this.clientId = clientId; this.scopes = [...scopes]; this.transport = transport; this.now = now; this.sleep = sleep; this.requestTimeoutMs = requestTimeoutMs; this.onUserCode = onUserCode; this.cached = null; this.inFlight = null; this.authAbort = null; this.authState = 'idle'; this.authPrompt = null; this.authEnabled = false;
  }
  async getAccessToken(signal) {
    checkAborted(signal); if (this.cached && this.cached.expiresAt > this.now() + 60000) return this.cached.value; if (!this.authEnabled) throw new ProviderToolError('provider_unauthorized', 'explicit authentication is required');
    if (!this.inFlight) { this.authAbort = new AbortController(); this.inFlight = this.authenticate(this.authAbort.signal).catch(error => { if (error?.code !== 'provider_cancelled') { this.authEnabled = false; this.authPrompt = null; if (this.authState !== 'expired') this.authState = 'failed'; } throw error; }).finally(() => { this.inFlight = null; this.authAbort = null; }); }
    return raceAbort(this.inFlight, signal);
  }
  start(signal) { this.authEnabled = true; return this.getAccessToken(signal); }
  async authenticate(signal) {
    this.authState = 'requesting_device_code'; this.authPrompt = null;
    const device = await this.transport.request({ origin: LOGIN_ORIGIN, method: 'POST', path: `/${this.tenant}/oauth2/v2.0/devicecode`, headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ client_id: this.clientId, scope: this.scopes.join(' ') }).toString(), signal });
    let verification;
    try { verification = new URL(device?.body?.verification_uri); } catch { verification = null; }
    if (typeof device?.status !== 'number' || device.status < 200 || device.status >= 300 || typeof device.body?.device_code !== 'string' || device.body.device_code.length < 1 || device.body.device_code.length > 4096 || /[\u0000-\u001f\u007f]/u.test(device.body.device_code) || typeof device.body?.user_code !== 'string' || device.body.user_code.length < 1 || device.body.user_code.length > 128 || /[\u0000-\u001f\u007f]/u.test(device.body.user_code) || !verification || verification.protocol !== 'https:' || !['microsoft.com', 'www.microsoft.com', 'login.microsoftonline.com'].includes(verification.hostname) || verification.username || verification.password || verification.hash || verification.search) throw new ProviderToolError(device?.status === 429 ? 'provider_rate_limited' : 'provider_unauthorized');
    const expiresAt = this.now() + Math.min(Math.max(Number.isInteger(device.body.expires_in) ? device.body.expires_in : 900, 1), 900) * 1000; this.authPrompt = { userCode: device.body.user_code, verificationUri: verification.toString(), expiresAt }; this.authState = 'awaiting_user'; this.onUserCode?.({ userCode: this.authPrompt.userCode, verificationUri: this.authPrompt.verificationUri }); let interval = Math.min(Math.max(Number.isInteger(device.body.interval) ? device.body.interval : 5, 5), 60);
    while (this.now() < expiresAt) {
      await this.sleep(interval * 1000, signal); checkAborted(signal);
      const response = await this.transport.request({ origin: LOGIN_ORIGIN, method: 'POST', path: `/${this.tenant}/oauth2/v2.0/token`, headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ grant_type: DEVICE_GRANT, client_id: this.clientId, device_code: device.body.device_code }).toString(), signal });
      if (typeof response?.status === 'number' && response.status >= 200 && response.status < 300 && typeof response.body?.access_token === 'string' && response.body.access_token.length > 0 && Buffer.byteLength(response.body.access_token, 'utf8') <= 4096 && !/[\u0000-\u001f\u007f]/u.test(response.body.access_token)) {
        const returnedScope = response.body.scope; if (returnedScope !== undefined && (typeof returnedScope !== 'string' || returnedScope.length > 4096 || /[\u0000-\u001f\u007f]/u.test(returnedScope) || !this.scopes.every(scope => scopeSatisfies(new Set(returnedScope.split(/\s+/u)), scope)))) throw new ProviderToolError('provider_unauthorized');
        const expiresIn = Number.isInteger(response.body.expires_in) ? Math.min(Math.max(response.body.expires_in, 1), 86400) : 3600; this.cached = { value: response.body.access_token, expiresAt: this.now() + expiresIn * 1000 }; this.authEnabled = false; this.authState = 'authenticated'; this.authPrompt = null; return response.body.access_token;
      }
      const code = response.body?.error;
      if (code === 'authorization_pending') continue;
      if (code === 'slow_down') { interval = Math.min(interval + 5, 60); continue; }
      if (code === 'authorization_declined' || code === 'expired_token' || response.status === 400) throw new ProviderToolError('provider_unauthorized');
      if (response.status === 429) { await this.sleep(retryAfter(response.headers), signal); continue; }
      throw new ProviderToolError('provider_unauthorized');
    }
    this.authState = 'expired'; throw new ProviderToolError('provider_unauthorized');
  }
  authStatus() { return { state: this.authState, prompt: this.authPrompt ? { ...this.authPrompt } : null }; }
  cancel() { this.authEnabled = false; this.authAbort?.abort(); this.authPrompt = null; this.authState = 'idle'; }
  clear() { this.cached = null; this.cancel(); }
}

export { GRAPH_ORIGIN, LOGIN_ORIGIN, GRAPH_SCOPES, safeClientId, safeScopes, safeTenant };
