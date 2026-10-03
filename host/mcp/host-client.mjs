// Loopback client from the MCP bridge to the already-running BMO host.
//
// WHY a separate, minimal client: the bridge holds exactly one secret (the delegate key) and
// talks to exactly one peer (127.0.0.1:<port from host.json>). Every rule that keeps that
// secret from leaking lives here: fixed loopback address, no redirects, no proxy agent, no
// Origin header, bounded response size, bounded time, and error messages that never echo
// socket errors (Node puts "127.0.0.1:<port>" in them).

import http from 'node:http';
import { constants as fsConstants } from 'node:fs';
import { open, lstat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

export const JOB_ID_PATTERN = '^[A-Za-z0-9_-]{8,64}$';
export const JOB_ID_RE = new RegExp(JOB_ID_PATTERN);
export const JOB_STATUSES = Object.freeze(['queued', 'awaiting_approval', 'running', 'completed', 'failed', 'cancelled', 'denied', 'expired']);
export const TERMINAL_STATUSES = Object.freeze(new Set(['completed', 'failed', 'cancelled', 'denied', 'expired']));
export const HOST_FILE_MAX_BYTES = 1024;
export const MAX_RESPONSE_BYTES = 256 * 1024;
export const MAX_ANSWER_CHARS = 8000;

/** Errors the tool layer turns into isError results. `code` is a fixed identifier; `message` is static text. */
export class HostError extends Error {
  constructor(code, message, extra = {}) { super(message); this.name = 'HostError'; this.code = code; Object.assign(this, extra); }
}

// host.json exists only while the host runs WITH delegation enabled, so a missing or stale
// file means either case; the message names both and the one command that fixes both.
export const NOT_RUNNING_MESSAGE = 'BMO is not running on this computer, or delegation is turned off. Start it with Start-BMO.ps1 -Mode app -EnableDelegation, then retry.';
export const SHOW_KEY_HINT = 'Show the current key on the laptop with Start-BMO.ps1 -ShowDelegateKey';

/** Per-user BMO state directory; `BMO_STATE_DIR` overrides it (absolute paths only). */
export function defaultStateDir({ platform = process.platform, env = process.env, homedir = os.homedir() } = {}) {
  const p = platform === 'win32' ? path.win32 : path.posix;
  if (typeof env.BMO_STATE_DIR === 'string' && p.isAbsolute(env.BMO_STATE_DIR)) return env.BMO_STATE_DIR;
  if (platform === 'win32') return p.join(env.LOCALAPPDATA && p.isAbsolute(env.LOCALAPPDATA) ? env.LOCALAPPDATA : p.join(homedir, 'AppData', 'Local'), 'BMO');
  if (platform === 'darwin') return p.join(homedir, 'Library', 'Application Support', 'BMO');
  const xdg = env.XDG_STATE_HOME && p.isAbsolute(env.XDG_STATE_HOME) ? env.XDG_STATE_HOME : p.join(homedir, '.local', 'state');
  return p.join(xdg, 'bmo');
}

/** `process.kill(pid, 0)` probes existence. EPERM means another user's process: not our host. */
export function pidIsAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

/**
 * Read and strictly validate host.json. Returns `{ ok: true, port, pid }` or `{ ok: false, reason }`.
 * `reason` is an internal identifier (missing | insecure | invalid | stale) for logs only;
 * callers always show the caller the same NOT_RUNNING_MESSAGE so nothing about the file leaks.
 */
export async function readHostInfo({ stateDir = defaultStateDir(), platform = process.platform, getuid = process.getuid?.bind(process), isAlive = pidIsAlive } = {}) {
  const file = (platform === 'win32' ? path.win32 : path.posix).join(stateDir, 'host.json');
  let handle;
  try {
    // A symlink could point the bridge at an attacker-chosen port file; refuse to follow it.
    const linkInfo = await lstat(file);
    if (linkInfo.isSymbolicLink()) return { ok: false, reason: 'insecure' };
    const noFollow = platform === 'win32' ? 0 : (fsConstants.O_NOFOLLOW ?? 0);
    handle = await open(file, fsConstants.O_RDONLY | noFollow);
    const info = await handle.stat();
    if (!info.isFile()) return { ok: false, reason: 'insecure' };
    if (info.size > HOST_FILE_MAX_BYTES) return { ok: false, reason: 'invalid' };
    // POSIX: a file another user owns, or one others can rewrite, could redirect our bearer
    // key to a port they control. Windows ACLs are not visible to Node built-ins; there the
    // per-user %LOCALAPPDATA% location is the protection (documented in docs/copilot/README.md).
    if (platform !== 'win32') {
      if (typeof getuid === 'function' && info.uid !== getuid()) return { ok: false, reason: 'insecure' };
      if ((info.mode & 0o022) !== 0) return { ok: false, reason: 'insecure' };
    }
    const buffer = Buffer.alloc(HOST_FILE_MAX_BYTES + 1);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
    if (bytesRead > HOST_FILE_MAX_BYTES) return { ok: false, reason: 'invalid' };
    const parsed = parseHostJson(buffer.subarray(0, bytesRead).toString('utf8'));
    if (!parsed) return { ok: false, reason: 'invalid' };
    if (!isAlive(parsed.pid)) return { ok: false, reason: 'stale' };
    return { ok: true, port: parsed.port, pid: parsed.pid };
  } catch (error) {
    return { ok: false, reason: error?.code === 'ENOENT' || error?.code === 'ENOTDIR' ? 'missing' : 'insecure' };
  } finally {
    await handle?.close().catch(() => {});
  }
}

const HOST_KEYS = ['version', 'port', 'pid', 'started_at'];
export function parseHostJson(text) {
  let value;
  try { value = JSON.parse(text.replace(/^﻿/, '')); } catch { return null; }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== HOST_KEYS.length || !HOST_KEYS.every(key => keys.includes(key))) return null;
  if (value.version !== 1) return null;
  // Ports below 1024 are privileged on POSIX and never chosen by the host's ephemeral bind.
  if (!Number.isInteger(value.port) || value.port < 1024 || value.port > 65535) return null;
  if (!Number.isInteger(value.pid) || value.pid <= 0 || value.pid > 0x7fffffff) return null;
  if (typeof value.started_at !== 'string' || value.started_at.length > 64 || !Number.isFinite(Date.parse(value.started_at))) return null;
  return { port: value.port, pid: value.pid };
}

// A private agent: the global agent may be replaced or proxied (Node 24's NODE_USE_ENV_PROXY),
// and the bearer key must never travel through a proxy. keepAlive is off so EOF shutdown
// leaves no pooled sockets behind.
const loopbackAgent = new http.Agent({ keepAlive: false, maxSockets: 8 });

export class HostClient {
  #key;
  constructor({ key, discover = () => readHostInfo(), maxResponseBytes = MAX_RESPONSE_BYTES, request = http.request, agent = loopbackAgent } = {}) {
    this.#key = typeof key === 'string' ? key : '';
    this.discover = discover;
    this.maxResponseBytes = maxResponseBytes;
    this.requestImpl = request;
    this.agent = agent;
  }

  hasKey() { return this.#key.length > 0; }

  async startJob(body, options) {
    const response = await this.#call('POST', '/api/delegate/jobs', body, options);
    if (response.status < 200 || response.status > 299) throw mapStatus(response);
    return parseJob(response.body, { start: true });
  }

  async getJob(jobId, waitSeconds, options) {
    assertJobId(jobId);
    const wait = Math.max(0, Math.min(15, Math.floor(waitSeconds)));
    const response = await this.#call('GET', `/api/delegate/jobs/${encodeURIComponent(jobId)}?wait=${wait}`, undefined, options);
    if (response.status !== 200) throw mapStatus(response);
    return parseJob(response.body);
  }

  async cancelJob(jobId, options) {
    assertJobId(jobId);
    const response = await this.#call('POST', `/api/delegate/jobs/${encodeURIComponent(jobId)}/cancel`, {}, options);
    if (response.status !== 200) throw mapStatus(response);
    return parseJob(response.body);
  }

  async health(options) {
    const response = await this.#call('GET', '/api/delegate/health', undefined, options);
    if (response.status !== 200) throw mapStatus(response);
    return parseHealth(response.body);
  }

  async #call(method, pathAndQuery, body, { timeoutMs = 5000, signal } = {}) {
    if (!this.hasKey()) throw new HostError('no_key', 'The BMO delegate key is not configured: set BMO_DELEGATE_KEY in this MCP server\'s env (VS Code: a password input).');
    if (signal?.aborted) throw new HostError('aborted', 'The request was cancelled.');
    const info = await this.discover();
    if (!info?.ok) throw new HostError('bmo_not_running', NOT_RUNNING_MESSAGE, { reason: info?.reason ?? 'missing' });
    const payload = body === undefined ? undefined : Buffer.from(JSON.stringify(body), 'utf8');
    // Header set is fixed. No Origin: the host treats any Origin as a browser and the bridge is
    // not one. Host is pinned to the literal loopback authority the host validates against.
    const headers = { Host: `127.0.0.1:${info.port}`, Accept: 'application/json', Authorization: `Bearer ${this.#key}` };
    if (payload) { headers['Content-Type'] = 'application/json'; headers['Content-Length'] = String(payload.length); }
    return await new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error, value) => {
        if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', onAbort);
        if (error) reject(error); else resolve(value);
      };
      const request = this.requestImpl({ host: '127.0.0.1', family: 4, port: info.port, method, path: pathAndQuery, headers, agent: this.agent, setHost: false }, response => {
        const status = response.statusCode ?? 0;
        if (status >= 300 && status <= 399) { response.resume(); request.destroy(); finish(new HostError('host_protocol_error', 'The BMO host answered with a redirect, which the bridge refuses to follow.')); return; }
        const chunks = []; let size = 0;
        response.on('data', chunk => {
          size += chunk.length;
          if (size > this.maxResponseBytes) { request.destroy(); finish(new HostError('host_protocol_error', 'The BMO host response was too large.')); return; }
          chunks.push(chunk);
        });
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let parsed = null;
          if (text.length) { try { parsed = JSON.parse(text); } catch { finish(new HostError('host_protocol_error', 'The BMO host returned malformed JSON.')); return; } }
          finish(null, { status, body: parsed });
        });
        response.on('error', () => finish(new HostError('host_unreachable', 'The connection to BMO was interrupted.')));
        response.on('aborted', () => finish(new HostError('host_unreachable', 'The connection to BMO was interrupted.')));
      });
      const timer = setTimeout(() => { request.destroy(); finish(new HostError('host_timeout', 'BMO did not answer in time.')); }, Math.max(1, timeoutMs));
      const onAbort = () => { request.destroy(); finish(new HostError('aborted', 'The request was cancelled.')); };
      signal?.addEventListener('abort', onAbort, { once: true });
      request.on('error', error => finish(error?.code === 'ECONNREFUSED' ? new HostError('bmo_not_running', NOT_RUNNING_MESSAGE, { reason: 'refused' }) : new HostError('host_unreachable', 'The BMO host could not be reached.')));
      request.end(payload);
    });
  }
}

function assertJobId(jobId) {
  if (typeof jobId !== 'string' || !JOB_ID_RE.test(jobId)) throw new HostError('invalid_job_id', 'job_id is not a valid BMO job id.');
}

// Fallback by HTTP status, used when the body carries no code we recognise.
const STATUS_ERRORS = {
  400: ['host_rejected', 'BMO rejected the request as invalid.'],
  401: ['unauthorized', `BMO rejected the delegate key. Update BMO_DELEGATE_KEY in the MCP server config. ${SHOW_KEY_HINT}.`],
  403: ['forbidden', 'BMO refused this request.'],
  404: ['unknown_job', 'Unknown or expired job_id. Start a new job with bmo_ask.'],
  409: ['queue_full', 'BMO\'s queue is full. Wait, or cancel jobs you no longer need.'],
  413: ['too_large', 'The task or context is too large for BMO.'],
  429: ['rate_limited', 'Too many delegated jobs. Wait before starting another.'],
};

// The host's documented error codes (body `{ error: <code> }`). Each maps to a fixed, plain,
// secret-free sentence: host text is never passed through for these.
const HOST_ERRORS = {
  invalid_request_body: ['host_rejected', 'BMO rejected the request as invalid.'],
  invalid_json: ['host_rejected', 'BMO rejected the request as invalid.'],
  task_too_large: ['task_too_large', 'The task is too long for BMO. Shorten it.'],
  context_too_large: ['context_too_large', 'The context is too long for BMO. Send less text.'],
  job_too_large: ['job_too_large', 'The task plus context is too large for BMO\'s model. Send less text.'],
  body_too_large: ['too_large', 'The request was too large for BMO.'],
  queue_full: ['queue_full', 'BMO already holds 3 jobs (queued or awaiting approval). Wait, or cancel jobs you no longer need.'],
  rate_limited: ['rate_limited', 'Too many delegated jobs started recently. Wait before starting another.'],
  auth_rate_limited: ['auth_rate_limited', `BMO is refusing requests after repeated wrong delegate keys. Wait a minute, then check the key. ${SHOW_KEY_HINT}.`],
  job_not_found: ['unknown_job', 'Unknown or expired job_id. Start a new job with bmo_ask.'],
};

function mapStatus(response) {
  const hostCode = typeof response.body?.error === 'string' && Object.hasOwn(HOST_ERRORS, response.body.error) ? response.body.error : null;
  const [code, message] = (hostCode && HOST_ERRORS[hostCode]) ?? STATUS_ERRORS[response.status] ?? (response.status >= 500 ? ['host_error', 'BMO hit an internal error.'] : ['host_protocol_error', 'BMO answered unexpectedly.']);
  const extra = {};
  const retry = response.body?.retry_after_s;
  if (Number.isFinite(retry)) extra.retryAfterS = Math.max(0, Math.min(3600, Math.ceil(retry)));
  return new HostError(code, message, extra);
}

/** Validate a job object from the host. Unknown fields are ignored; required ones are strict. */
export function parseJob(body, { start = false } = {}) {
  const bad = () => new HostError('host_protocol_error', 'BMO answered with an unexpected job shape.');
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw bad();
  if (typeof body.job_id !== 'string' || !JOB_ID_RE.test(body.job_id)) throw bad();
  if (!JOB_STATUSES.includes(body.status)) throw bad();
  if (start && !['queued', 'awaiting_approval', 'running'].includes(body.status)) throw bad();
  const job = { job_id: body.job_id, status: body.status, truncated: body.truncated === true };
  if (typeof body.phase === 'string') job.phase = body.phase;
  if (Number.isFinite(body.elapsed_s) && body.elapsed_s >= 0) job.elapsed_s = body.elapsed_s;
  if (body.status === 'completed') {
    if (typeof body.answer !== 'string') throw bad();
    job.answer = body.answer;
  }
  if (body.error && typeof body.error === 'object') job.error = { code: typeof body.error.code === 'string' ? body.error.code : '', message: typeof body.error.message === 'string' ? body.error.message : '' };
  return job;
}

export function parseHealth(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.ready !== 'boolean') throw new HostError('host_protocol_error', 'BMO answered with an unexpected health shape.');
  return {
    ready: body.ready,
    engine: typeof body.engine === 'string' ? body.engine : '',
    queue: Number.isInteger(body.queue) && body.queue >= 0 ? body.queue : 0,
    busy: body.busy === true,
    approval: body.approval === 'granted' ? 'granted' : 'per_job',
  };
}
