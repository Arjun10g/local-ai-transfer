// Test helpers for the MCP bridge (not a test file itself): an in-process fake BMO host that
// implements the delegate routes on 127.0.0.1, plus line-oriented MCP clients that behave
// like VS Code (legacy initialize handshake) and like Copilot CLI (stateless 2026-07-28).

import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { PassThrough } from 'node:stream';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startStdioBridge } from '../../host/mcp/stdio.mjs';

export const TEST_KEY = 'test-delegate-key-0123456789abcdef';
export const MODERN_META = Object.freeze({
  'io.modelcontextprotocol/protocolVersion': '2026-07-28',
  'io.modelcontextprotocol/clientCapabilities': {},
  'io.modelcontextprotocol/clientInfo': { name: 'github-copilot-cli', version: '1.0.91' },
});
export const FAST_BUDGETS = Object.freeze({ askMs: 1_300, slackMs: 200, statusDefaultWait: 0, requestMs: 600, healthMs: 400, cancelMs: 300 });
export const FAST_LIMITS = Object.freeze({ maxInFlight: 4, callDeadlineMs: 2_500, progressIntervalMs: 100, maxBatch: 16, shutdownGraceMs: 600 });

/**
 * Fake host matching the BMO host's delegate contract (host/delegate/service.mjs, as reported
 * by the host agent): ids `job_` + 24 base64url chars, phase names, error-body codes, strict
 * request validation (Origin -> 403, Host pinned, JSON content type on POST, exact body keys,
 * cancel body `{}`), queue cap 3 counting jobs awaiting approval, `retry_after_s` on 409/429.
 * Jobs complete `completeAfterMs` after creation unless a test overrides behaviour via `hooks`
 * (per-route functions returning true when they handled the request) or `state`.
 */
export const HOST_PHASES = Object.freeze(['waiting_for_operator', 'waiting_for_engine', 'waiting_for_operator_chat', 'starting', 'generating', 'reading_files', 'using_tool']);
const PHASE_BY_STATUS = { awaiting_approval: 'waiting_for_operator', queued: 'waiting_for_engine', running: 'generating' };
const TERMINAL = ['completed', 'failed', 'cancelled', 'denied', 'expired'];
const codePoints = text => [...text].length;

export async function startFakeHost({ key = TEST_KEY, completeAfterMs = 50, answer = 'fake answer', initialStatus = 'queued', queueCap = 3, hooks = {} } = {}) {
  const requests = [];
  const jobs = new Map();
  const sockets = new Set();
  const state = { completeAfterMs, answer, initialStatus, finalStatus: 'completed', finalError: null, phase: null, queueCap };
  const view = job => {
    const done = Date.now() - job.created >= job.completeAfterMs && job.status !== 'cancelled';
    const status = job.status === 'cancelled' ? 'cancelled' : done ? job.finalStatus : job.initialStatus;
    const body = { job_id: job.id, status, elapsed_s: (Date.now() - job.created) / 1000, truncated: false };
    if (!TERMINAL.includes(status)) body.phase = state.phase ?? PHASE_BY_STATUS[status];
    if (status === 'completed') body.answer = job.answer;
    if (['failed', 'denied', 'expired'].includes(status)) body.error = job.finalError ?? { code: status, message: `${status} by fake host` };
    return body;
  };
  const pending = () => [...jobs.values()].filter(job => ['awaiting_approval', 'queued'].includes(view(job).status)).length;
  const exactKeys = (value, allowed, required) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => allowed.includes(k)) && required.every(k => Object.hasOwn(value, k));
  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', async () => {
      let body = null; let badJson = false;
      try { body = raw ? JSON.parse(raw) : null; } catch { badJson = true; }
      const entry = { method: req.method, url: req.url, headers: { ...req.headers }, body, closed: false, status: null };
      requests.push(entry);
      let gone = false;
      res.on('close', () => { gone = true; entry.closed = !res.writableFinished; });
      const send = (status, value) => { entry.status = status; if (!res.writableEnded && !res.destroyed) { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); } };
      if (req.headers.origin !== undefined) return send(403, { error: 'forbidden' });
      if (req.headers.host !== `127.0.0.1:${port}` && req.headers.host !== `localhost:${port}`) return send(403, { error: 'forbidden' });
      const bearer = /^Bearer ([A-Za-z0-9_-]{1,128})$/u.exec(req.headers.authorization ?? '');
      if (!bearer || bearer[1] !== key) return send(401, { error: 'unauthorized' });
      if (state.forced) return send(state.forced.status, state.forced.body);
      const url = new URL(req.url, 'http://127.0.0.1');
      const match = url.pathname.match(/^\/api\/delegate\/jobs\/([^/]+)(\/cancel)?$/);
      for (const [name, hook] of Object.entries(hooks)) {
        if (name === 'any' || (name === 'start' && req.method === 'POST' && url.pathname === '/api/delegate/jobs') || (name === 'get' && req.method === 'GET' && match && !match[2]) || (name === 'cancel' && match?.[2]) || (name === 'health' && url.pathname === '/api/delegate/health')) {
          if (await hook({ req, res, url, body, send, jobs, state, entry })) return;
        }
      }
      if (req.method === 'POST' && !/^application\/json\b/.test(req.headers['content-type'] ?? '')) return send(415, { error: 'unsupported_content_type' });
      if (req.method === 'POST' && badJson) return send(400, { error: 'invalid_json' });
      if (req.method === 'POST' && url.pathname === '/api/delegate/jobs' && !url.search) {
        if (!exactKeys(body, ['task', 'context', 'allow_files', 'caller'], ['task', 'caller']) || !exactKeys(body.caller, ['client', 'name'], ['client', 'name'])) return send(400, { error: 'invalid_request_body' });
        for (const value of [body.caller.client, body.caller.name]) if (typeof value !== 'string' || !value.trim() || codePoints(value) > 64) return send(400, { error: 'invalid_request_body' });
        if (typeof body.task !== 'string' || (body.context !== undefined && typeof body.context !== 'string') || (body.allow_files !== undefined && typeof body.allow_files !== 'boolean')) return send(400, { error: 'invalid_request_body' });
        if (codePoints(body.task) > 4000) return send(413, { error: 'task_too_large' });
        if (body.context !== undefined && codePoints(body.context) > 16000) return send(413, { error: 'context_too_large' });
        if (!body.task.trim()) return send(400, { error: 'invalid_request_body' });
        if (pending() >= state.queueCap) return send(409, { error: 'queue_full', retry_after_s: 30 });
        const id = `job_${randomBytes(18).toString('base64url')}`;
        jobs.set(id, { id, created: Date.now(), completeAfterMs: state.completeAfterMs, answer: state.answer, status: null, initialStatus: state.initialStatus, finalStatus: state.finalStatus, finalError: state.finalError, body });
        return send(202, { job_id: id, status: state.initialStatus });
      }
      if (req.method === 'GET' && url.pathname === '/api/delegate/health' && !url.search) return send(200, { ready: true, engine: 'ready', queue: pending(), busy: false, approval: 'per_job' });
      if (match && req.method === 'POST' && match[2] && !url.search) {
        if (!exactKeys(body, [], [])) return send(400, { error: 'invalid_request_body' });
        const job = jobs.get(match[1]);
        if (!job) return send(404, { error: 'job_not_found' });
        if (!TERMINAL.includes(view(job).status)) job.status = 'cancelled';
        return send(200, { job_id: job.id, status: view(job).status });
      }
      if (match && req.method === 'GET' && !match[2]) {
        const keys = [...url.searchParams.keys()];
        const rawWait = url.searchParams.get('wait');
        if (keys.some(k => k !== 'wait') || keys.length > 1 || (rawWait !== null && !/^(?:[0-9]|1[0-5])$/u.test(rawWait))) return send(400, { error: 'invalid_request_body' });
        const job = jobs.get(match[1]);
        if (!job) return send(404, { error: 'job_not_found' });
        const until = Date.now() + Number(rawWait ?? 0) * 1000;
        while (Date.now() < until && !TERMINAL.includes(view(job).status) && !gone) await new Promise(resolve => setTimeout(resolve, 10));
        return send(200, view(job));
      }
      send(404, { error: 'not_found' });
    });
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    port, requests, jobs, state,
    discover: async () => ({ ok: true, port, pid: process.pid }),
    close: async () => { for (const socket of sockets) socket.destroy(); await new Promise(resolve => server.close(resolve)); },
  };
}

/** Write a host.json the bridge will accept (owned by us, mode 0600, live pid). */
export async function makeStateDir(t, { port, pid = process.pid, extra = {}, mode = 0o600, raw } = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'bmo-mcp-state-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'host.json');
  await writeFile(file, raw ?? JSON.stringify({ version: 1, port, pid, started_at: new Date().toISOString(), ...extra }));
  await chmod(file, mode);
  return dir;
}

/** Newline-delimited JSON-RPC client over a pair of streams. Records every raw stdout line. */
export class LineClient {
  constructor(toServer, fromServer) {
    this.toServer = toServer; this.lines = []; this.messages = []; this.waiters = []; this.nextId = 1; this.buffer = '';
    fromServer.setEncoding?.('utf8');
    fromServer.on('data', chunk => {
      this.buffer += chunk;
      let index;
      while ((index = this.buffer.indexOf('\n')) !== -1) {
        const line = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + 1);
        this.lines.push(line);
        let message; try { message = JSON.parse(line); } catch { message = { unparseable: line }; }
        this.messages.push(message);
        for (const waiter of [...this.waiters]) if (waiter.match(message)) { this.waiters.splice(this.waiters.indexOf(waiter), 1); waiter.resolve(message); }
      }
    });
  }
  sendRaw(text) { this.toServer.write(text); }
  send(message) { this.sendRaw(`${JSON.stringify(message)}\n`); }
  notify(method, params) { this.send(params === undefined ? { jsonrpc: '2.0', method } : { jsonrpc: '2.0', method, params }); }
  waitFor(match, timeoutMs = 10_000) {
    const found = this.messages.find(match);
    if (found) return Promise.resolve(found);
    return new Promise((resolve, reject) => {
      const waiter = { match, resolve: value => { clearTimeout(timer); resolve(value); } };
      const timer = setTimeout(() => { this.waiters.splice(this.waiters.indexOf(waiter), 1); reject(new Error('timed out waiting for message')); }, timeoutMs);
      this.waiters.push(waiter);
    });
  }
  request(method, params, { id = this.nextId++, timeoutMs } = {}) {
    const message = { jsonrpc: '2.0', id, method };
    if (params !== undefined) message.params = params;
    this.send(message);
    return this.waitFor(reply => reply.id === id && (Object.hasOwn(reply, 'result') || Object.hasOwn(reply, 'error')), timeoutMs);
  }
  responsesFor(id) { return this.messages.filter(message => message.id === id); }
}

/** VS Code 1.140-style client: legacy initialize at 2025-11-25, then plain requests. */
export class VsCodeLikeClient extends LineClient {
  async handshake(protocolVersion = '2025-11-25') {
    const reply = await this.request('initialize', { protocolVersion, capabilities: { roots: { listChanged: true }, sampling: {}, elicitation: {} }, clientInfo: { name: 'Visual Studio Code', version: '1.140.0' } });
    this.notify('notifications/initialized');
    return reply;
  }
  listTools() { return this.request('tools/list', {}); }
  callTool(name, args, { progressToken, id } = {}) {
    const params = { name, arguments: args };
    if (progressToken !== undefined) params._meta = { progressToken };
    return this.request('tools/call', params, { id });
  }
}

/** Copilot CLI 1.0.81+-style client: stateless, `_meta` on every request, discover probe first. */
export class CopilotCliLikeClient extends LineClient {
  meta(extra = {}) { return { ...MODERN_META, ...extra }; }
  discover() { return this.request('server/discover', { _meta: this.meta() }); }
  listTools() { return this.request('tools/list', { _meta: this.meta() }); }
  callTool(name, args, { progressToken, id } = {}) {
    return this.request('tools/call', { name, arguments: args, _meta: this.meta(progressToken === undefined ? {} : { progressToken }) }, { id });
  }
}

/** Run the real stdio bridge in-process over PassThrough streams against `host`. */
export function startBridge(t, { host, ClientClass = LineClient, key = TEST_KEY, budgets = FAST_BUDGETS, limits = FAST_LIMITS, limiter, discover } = {}) {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  const stderr = [];
  let resolveExit;
  const exited = new Promise(resolve => { resolveExit = resolve; });
  const env = key === null ? {} : { BMO_DELEGATE_KEY: key }; // null = variable absent
  const bridge = startStdioBridge({ stdin, stdout, writeErr: text => stderr.push(text), env, exit: code => resolveExit(code), discover: discover ?? host?.discover ?? (async () => ({ ok: false, reason: 'missing' })), budgets, limits, limiter, logLevel: 'debug' });
  const client = new ClientClass(stdin, stdout);
  t.after(async () => { stdin.end(); await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 1500))]); });
  return { client, bridge, stdin, stdout, env, exited, stderr: () => stderr.join('') };
}

export const structured = reply => reply.result.structuredContent;
export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
