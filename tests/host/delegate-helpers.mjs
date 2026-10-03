// Shared offline fixtures for the delegate tests: a scriptable engine and the
// real HostServer + DelegateService wired the way lae-host.mjs wires them.
import http from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ConversationController } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { DelegateService, DELEGATE_GRANT_BINDING } from '../../host/delegate/service.mjs';
import { createDelegateToolRegistry } from '../../host/delegate/tools.mjs';
import { readDelegateKey } from '../../host/delegate/state.mjs';
import { OperatorGrantControl, OperatorGrantStore } from '../../host/providers/operator-grants.mjs';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const cancelled = () => Object.assign(new Error('cancelled'), { code: 'cancelled' });

/**
 * One generation at a time, like the real engine.  `plan` is consumed per
 * generate() call: {text} answers, {call} emits a tool call, {hold} streams
 * nothing until aborted (or until `release()`), {busy} refuses, {chunks}
 * streams many deltas.  A step may be a function of the request.
 */
export class ScriptEngine {
  constructor() { this.calls = []; this.cancels = []; this.deleted = []; this.plan = []; this.maxTokens = 256; this.active = null; this.holds = []; this.refused = 0; this.lingerMs = 0; this.busyUntil = 0; }
  async health() { return { ready: true, engine: 'script', backend: 'script', model: 'script' }; }
  cancel(id) { this.cancels.push(id); }
  async deleteSession(id) { this.deleted.push(id); }
  // Like /readyz: ready while BUSY, with the lifecycle saying which.
  async ready() { return { ready: true, lifecycle: this.active || Date.now() < this.busyUntil ? 'BUSY' : 'READY' }; }
  releaseHolds() { for (const release of this.holds.splice(0)) release(); }
  async *generate(args) {
    // A second generation while one runs is refused, as the engine does;
    // `refused` lets a test prove the host never even tried.
    if (this.active || Date.now() < this.busyUntil) { this.refused += 1; throw Object.assign(new Error('busy'), { code: 'busy' }); }
    this.calls.push({ ...args, toolNames: (args.tools ?? []).map(tool => tool.function.name) });
    let step = this.plan.shift() ?? { text: 'ok' };
    if (typeof step === 'function') step = step(args);
    if (step.busy) throw Object.assign(new Error('busy'), { code: 'busy' });
    this.active = args.requestId;
    try {
      if (step.hold) {
        await new Promise((resolve, reject) => {
          if (args.signal?.aborted) return reject(cancelled());
          this.holds.push(resolve);
          // A real engine stays BUSY a moment after a cancel (next abort check).
          args.signal?.addEventListener('abort', () => { this.busyUntil = Date.now() + this.lingerMs; reject(cancelled()); }, { once: true });
        });
      }
      if (step.call) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: `call_${this.calls.length}x`, name: step.call.name, arguments: step.call.arguments ?? {} }) }; return; }
      for (const piece of step.chunks ?? [step.text ?? 'ok']) { if (args.signal?.aborted) throw cancelled(); await delay(1); yield { kind: 'text_delta', text: piece }; }
      yield { kind: 'done', finish_reason: 'stop', usage: step.usage ?? {} };
    } finally { this.active = null; }
  }
}

export async function tempDir(t, prefix = 'bmo-delegate-') {
  const dir = await mkdtemp(join(tmpdir(), prefix));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
}

export async function setup(t, { options = {}, workspaceRoots = [], engine = new ScriptEngine(), userController, extraBindings = [], platform } = {}) {
  const root = await tempDir(t); const stateDir = join(root, 'state');
  const controller = userController ?? new ConversationController({ engine });
  const store = new OperatorGrantStore();
  const grants = new OperatorGrantControl({ store, bindings: [DELEGATE_GRANT_BINDING, { capability: 'local.clipboard', provider: 'local_clipboard', accountFingerprint: 'local_host', scope: 'clipboard', label: 'Write the local clipboard' }, ...extraBindings] });
  const tools = createDelegateToolRegistry({ workspaceRoots, platform });
  const delegate = new DelegateService({ engine, userController: controller, toolRegistry: tools.registry, workspaceIds: tools.workspaceIds, stateDir, grantControl: grants, options: { approval_timeout_ms: 60000, ...options }, platform });
  const host = new HostServer({ controller, engine, delegate, operatorGrants: grants });
  const address = await host.listen(0);
  t.after(() => host.close());
  const key = await readDelegateKey(stateDir);
  const responses = [];
  const call = async (method, path, { body, headers = {}, auth = key, raw } = {}) => {
    const payload = raw !== undefined ? Buffer.from(raw) : body === undefined ? undefined : Buffer.from(JSON.stringify(body));
    const finalHeaders = { host: `127.0.0.1:${address.port}`, ...(auth ? { authorization: `Bearer ${auth}` } : {}), ...(payload ? { 'content-type': 'application/json', 'content-length': String(payload.length) } : {}), ...headers };
    for (const [name, value] of Object.entries(finalHeaders)) if (value === null) delete finalHeaders[name];
    return new Promise((resolve, reject) => {
      const request = http.request({ host: '127.0.0.1', port: address.port, method, path, headers: finalHeaders, setHost: false, agent: false }, response => {
        const chunks = []; response.on('data', chunk => chunks.push(chunk));
        response.on('end', () => { const text = Buffer.concat(chunks).toString('utf8'); let json = null; try { json = JSON.parse(text); } catch {} const out = { status: response.statusCode, headers: response.headers, text, json }; responses.push(out); resolve(out); });
      });
      request.on('error', reject); if (payload) request.write(payload); request.end();
    });
  };
  const ui = (method, path, body) => call(method, path, { body, auth: address.token });
  const submit = (extra = {}) => call('POST', '/api/delegate/jobs', { body: { task: 'Summarise the note.', caller: { client: 'vscode', name: 'copilot' }, ...extra } });
  const poll = (id, wait = 0) => call('GET', `/api/delegate/jobs/${id}?wait=${wait}`);
  const until = async (id, predicate, { timeoutMs = 5000 } = {}) => {
    const end = Date.now() + timeoutMs; let last;
    while (Date.now() < end) { last = await poll(id, 1); if (predicate(last.json)) return last.json; }
    throw new Error(`job ${id} never reached the expected state; last ${JSON.stringify(last?.json)}`);
  };
  return { root, stateDir, engine, controller, store, grants, tools, delegate, host, address, key, call, ui, submit, poll, until, responses };
}

export { delay };
