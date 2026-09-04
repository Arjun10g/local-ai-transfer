import { spawn as nodeSpawn } from 'node:child_process';
import { ProviderToolError, boundedArray, boundedString, checkAborted, digest, exactObject, failureResult, result } from './provider-common.mjs';

const MAX_OUTPUT = 65536;
const MAX_ATTEMPTS = 256;
const VERSION = /^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/u;
const PATH = (value, name = 'context_path') => boundedString(value, name, { min: 1, max: 1024, identifier: true }).replaceAll('\\', '/');
const validate = input => { const args = exactObject(input, ['prompt', 'workspace_id', 'context_paths'], ['prompt', 'workspace_id', 'context_paths']); boundedString(args.prompt, 'prompt', { min: 1, max: 8192 }); boundedString(args.workspace_id, 'workspace_id', { min: 1, max: 64, identifier: true }); boundedArray(args.context_paths, 'context_paths', { max: 8 }); const contextPaths = args.context_paths.map((value, index) => PATH(value, `context_paths[${index}]`)); if (contextPaths.some(value => value.startsWith('/') || /^[A-Za-z]:\//u.test(value) || value.split('/').some(part => !part || part === '.' || part === '..'))) throw new ProviderToolError('invalid_tool_arguments', 'context paths must be relative and contained'); return { ...structuredClone(args), context_paths: contextPaths }; };

const COPILOT_PARAMETERS = { type: 'object', additionalProperties: false, required: ['prompt', 'workspace_id', 'context_paths'], properties: { prompt: { type: 'string', minLength: 1, maxLength: 8192 }, workspace_id: { type: 'string', minLength: 1, maxLength: 64 }, context_paths: { type: 'array', maxItems: 8, items: { type: 'string', minLength: 1, maxLength: 1024 } } } };
export const copilotDefinition = Object.freeze({ name: 'coding.copilot_ask', version: '0.1.0', description: 'Ask GitHub Copilot for a bounded prompt-only answer with explicitly selected context.', risk_tier: 'T3', side_effect: 'cloud_inference', network: true, data_egress: 'prompt_and_selected_files', requires_confirmation: true, timeout_ms: 30000, output_limit: MAX_OUTPUT, parameters: COPILOT_PARAMETERS, input_schema: COPILOT_PARAMETERS });

function spawnProcess(executable, args, options) { return nodeSpawn(executable, args, options); }

function putBounded(map, key, value, max) { if (map.size >= max && !map.has(key)) map.delete(map.keys().next().value); map.set(key, value); }

function minimalEnvironment(environment = process.env) {
  const env = {};
  if (typeof environment?.PATH === 'string') env.PATH = environment.PATH;
  if (typeof environment?.SystemRoot === 'string') env.SystemRoot = environment.SystemRoot;
  if (typeof environment?.COPILOT_HOME === 'string') env.COPILOT_HOME = environment.COPILOT_HOME;
  if (typeof environment?.COPILOT_GH_HOST === 'string') env.COPILOT_GH_HOST = environment.COPILOT_GH_HOST;
  return env;
}

function waitForClose(child, timeoutMs) {
  if ((child.exitCode !== null && child.exitCode !== undefined) || (child.signalCode !== null && child.signalCode !== undefined)) return Promise.resolve();
  return new Promise(resolve => {
    let settled = false;
    const finish = () => { if (settled) return; settled = true; clearTimeout(timer); child.removeListener?.('close', finish); resolve(); };
    const timer = setTimeout(finish, timeoutMs);
    timer.unref?.();
    child.once?.('close', finish);
  });
}

export async function killCopilotProcessTree(child, { platform = process.platform, spawn = nodeSpawn, graceMs = 1000 } = {}) {
  if (!child || !Number.isInteger(child.pid) || child.pid <= 0 || (child.exitCode !== null && child.exitCode !== undefined) || (child.signalCode !== null && child.signalCode !== undefined)) return;
  if (platform === 'win32') {
    let killer;
    try { killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: 'ignore' }); } catch { try { child.kill(); } catch {} }
    if (killer) await waitForClose(killer, Math.min(5000, Math.max(100, graceMs)));
    await waitForClose(child, Math.min(5000, Math.max(100, graceMs)));
    return;
  }
  try { process.kill(-child.pid, 'SIGTERM'); } catch { try { child.kill('SIGTERM'); } catch {} }
  await waitForClose(child, Math.min(5000, Math.max(100, graceMs)));
  // A detached leader can exit while a descendant keeps the process group
  // alive, so always issue the bounded final group kill after the grace wait.
  try { process.kill(-child.pid, 'SIGKILL'); } catch { if (child.exitCode === null && child.signalCode === null) { try { child.kill('SIGKILL'); } catch {} } }
  await waitForClose(child, Math.min(5000, Math.max(100, graceMs)));
}

export function createCopilotVersionCheck({ expectedVersion, spawn = spawnProcess, environment = process.env, timeoutMs = 5000 } = {}) {
  if (typeof expectedVersion !== 'string' || !VERSION.test(expectedVersion)) throw new TypeError('Copilot expected version must be an exact semantic version');
  const normalized = expectedVersion.startsWith('v') ? expectedVersion.slice(1) : expectedVersion;
  const boundedTimeout = Math.min(15000, Math.max(100, timeoutMs));
  return async (executable, signal) => {
    checkAborted(signal);
    let child;
    try { child = spawn(executable, ['--no-auto-update', '--no-color', 'version'], { shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'], env: minimalEnvironment(environment) }); } catch { return false; }
    const matched = await new Promise(resolve => {
      let settled = false; let output = ''; let oversized = false;
      const finish = value => { if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); resolve(value); };
      const abort = () => { try { child.kill(); } catch {} finish(false); };
      const timer = setTimeout(() => { try { child.kill(); } catch {} finish(false); }, boundedTimeout);
      timer.unref?.();
      child.stdout?.on('data', chunk => { const bytes = Buffer.from(chunk); if (Buffer.byteLength(output, 'utf8') + bytes.length > 4096) { oversized = true; try { child.kill(); } catch {} return; } output += bytes.toString('utf8'); });
      child.once('error', () => finish(false));
      child.once('close', code => {
        const escaped = normalized.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
        finish(code === 0 && !oversized && new RegExp(`(?:^|[^0-9A-Za-z])v?${escaped}(?:$|[^0-9A-Za-z])`, 'u').test(output));
      });
      signal?.addEventListener('abort', abort, { once: true });
    });
    checkAborted(signal);
    return matched;
  };
}

export class CopilotCliProvider {
  constructor({ enabled = false, executable, allowlist = [], version = 'unverified', versionCheck, readContext, protocol = 'legacy_stdin', spawn = spawnProcess, killProcess, timeoutMs = 30000, maxOutput = MAX_OUTPUT, environment = process.env, platform = process.platform } = {}) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 120000) throw new TypeError('invalid Copilot timeout');
    if (!Number.isInteger(maxOutput) || maxOutput < 1024 || maxOutput > MAX_OUTPUT) throw new TypeError('invalid Copilot output limit');
    if (!['acp', 'legacy_stdin'].includes(protocol)) throw new TypeError('invalid Copilot protocol');
    this.enabled = enabled === true; this.executable = executable; this.allowlist = new Set(allowlist); this.version = version; this.versionCheck = versionCheck; this.readContext = readContext; this.protocol = protocol; this.spawn = spawn; this.killProcess = killProcess ?? (child => killCopilotProcessTree(child, { platform, spawn })); this.timeoutMs = timeoutMs; this.maxOutput = maxOutput; this.environment = environment; this.platform = platform; this.proposals = new Map(); this.attempts = new Map();
  }
  state() { if (!this.enabled) return 'disabled'; if (typeof this.executable !== 'string' || !this.executable || !this.versionCheck) return 'unconfigured'; if (!this.allowlist.has(this.executable)) return 'unconfigured'; return 'ready'; }
  validate(input) { return validate(input); }
  async preview(call) { const args = validate(call.arguments); const context = await this.context(args); const contextBytes = Buffer.byteLength(context.text, 'utf8'); const promptBytes = Buffer.byteLength(args.prompt, 'utf8'); const separatorBytes = context.text ? Buffer.byteLength('\n\nSelected context:\n', 'utf8') : 0; const bytes = promptBytes + separatorBytes + contextBytes; if (bytes > 65536) throw new ProviderToolError('invalid_tool_arguments', 'prompt and selected context exceed egress bound'); const proposal = { digest: digest(args), name: call.name, workspace_id: args.workspace_id, context_digest: digest({ text: context.text, files: context.files }), context_bytes: contextBytes, egress_bytes: bytes }; putBounded(this.proposals, call.id, proposal, 128); return { provider: 'github_copilot', destination: 'GitHub Copilot cloud', action: 'prompt_only', workspace_id: args.workspace_id, context_paths: args.context_paths, context_files: context.files, egress_bytes: bytes, data_categories: ['prompt', ...(args.context_paths.length ? ['selected_workspace_files'] : [])], disclosure: 'Selected prompt/context is sent to GitHub Copilot; output is untrusted.' }; }
  async execute(call) {
    let args; try { args = validate(call.arguments); const saved = this.proposals.get(call.id); if (!saved || saved.name !== call.name || saved.digest !== digest(args)) throw new ProviderToolError('copilot_policy_denied'); if (!call.authorization || typeof call.authorization !== 'object' || Array.isArray(call.authorization) || call.authorization.kind !== 'user_confirmation' || Object.keys(call.authorization).length !== 1) throw new ProviderToolError('provider_permission_insufficient'); if (!this.enabled) throw new ProviderToolError('copilot_cli_unavailable'); if (!this.executable || !this.versionCheck || !this.allowlist.has(this.executable)) throw new ProviderToolError('copilot_policy_denied'); if (!await this.versionCheck(this.executable, call.signal)) throw new ProviderToolError('copilot_policy_denied'); const context = await this.context(args); const contextBytes = Buffer.byteLength(context.text, 'utf8'); if (digest({ text: context.text, files: context.files }) !== saved.context_digest || contextBytes !== saved.context_bytes) throw new ProviderToolError('copilot_policy_denied', 'selected context changed after preview'); const prompt = context.text ? `${args.prompt}\n\nSelected context:\n${context.text}` : args.prompt; if (Buffer.byteLength(prompt, 'utf8') !== saved.egress_bytes || Buffer.byteLength(prompt, 'utf8') > 65536) throw new ProviderToolError('copilot_policy_denied'); const attemptKey = `${call.id}:${digest(args)}`; if (this.attempts.has(attemptKey)) return this.attempts.get(attemptKey); putBounded(this.attempts, attemptKey, failureResult(call, new ProviderToolError('provider_request_already_attempted')), MAX_ATTEMPTS); let output; try { output = await this.run(call, args, prompt); } catch (error) { output = failureResult(call, error); } this.attempts.set(attemptKey, output); return output; } catch (error) { if (error?.code === 'invalid_tool_arguments') throw error; return failureResult(call, error); }
  }
  async context(args) {
    if (!args.context_paths.length) return { text: '', files: [] };
    if (typeof this.readContext !== 'function') throw new ProviderToolError('copilot_policy_denied');
    const value = await this.readContext(args.workspace_id, args.context_paths); const details = typeof value === 'string' ? { text: value, files: [] } : value; if (!details || typeof details.text !== 'string' || !Array.isArray(details.files) || Buffer.byteLength(details.text, 'utf8') > 57344) throw new ProviderToolError('provider_response_too_large'); return details;
  }
  async run(call, args, prompt) {
    if (this.protocol === 'acp') return this.runAcp(call, prompt);
    checkAborted(call.signal); const started = Date.now(); const child = this.spawn(this.executable, ['-s', '--no-auto-update', '--no-color', '--no-custom-instructions', '--no-experimental', '--no-remote', '--no-remote-export', '--no-ask-user', '--disable-builtin-mcps', '--disallow-temp-dir', '--log-level=none', '--available-tools='], { shell: false, windowsHide: true, detached: true, stdio: ['pipe', 'pipe', 'pipe'], env: this.safeEnvironment() }); let output = ''; let truncated = false; const outputDecoder = new TextDecoder();
    const collect = (current, chunk, decoder, final = false) => { const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), 'utf8'); const text = decoder.decode(bytes, { stream: !final }); const room = this.maxOutput - Buffer.byteLength(current, 'utf8'); if (room <= 0) { truncated = true; return current; } const textBytes = Buffer.from(text, 'utf8'); if (textBytes.length > room) { truncated = true; let bounded = new TextDecoder().decode(textBytes.subarray(0, room)); while (bounded.endsWith('\uFFFD')) bounded = bounded.slice(0, -1); return current + bounded; } return current + text; };
    const stop = async () => { try { await this.killProcess(child); } catch {} };
    let outputOverflow = false;
    return await new Promise((resolve) => {
      let settled = false; let forcedCode = null; let killBackstop; const finish = value => { if (!settled) { settled = true; clearTimeout(timer); clearTimeout(killBackstop); call.signal?.removeEventListener('abort', abort); resolve(value); } }; const stopAndFinish = code => { if (forcedCode) return; forcedCode = code; killBackstop = setTimeout(() => finish(failureResult(call, new ProviderToolError(code))), 6000); killBackstop.unref?.(); void stop().finally(() => finish(failureResult(call, new ProviderToolError(code)))); }; const abort = () => stopAndFinish('provider_cancelled'); const timer = setTimeout(() => stopAndFinish('provider_timeout'), this.timeoutMs); timer.unref?.();
      child.stdout?.on('data', chunk => { const before = Buffer.byteLength(output, 'utf8'); output = collect(output, chunk, outputDecoder); if (Buffer.byteLength(output, 'utf8') >= this.maxOutput && Buffer.byteLength(chunk) + before > this.maxOutput && !outputOverflow) { outputOverflow = true; stopAndFinish('provider_response_too_large'); } }); child.stderr?.on('data', () => {}); child.once('error', error => finish(failureResult(call, new ProviderToolError(error.code === 'ENOENT' ? 'copilot_cli_unavailable' : 'provider_failed')))); child.once('close', code => { if (forcedCode) return; output = collect(output, Buffer.alloc(0), outputDecoder, true); const exitClass = code === 0 ? 'ok' : code === null ? 'cancelled' : 'failed'; finish(result(call, code === 0 ? 'ok' : 'failed', { provider: 'github_copilot', state: 'ready', stdout: output, exit_class: exitClass, truncated, duration_ms: Date.now() - started, cli_version: this.version, egress_bytes: Buffer.byteLength(prompt, 'utf8'), idempotency: 'new' })); }); call.signal?.addEventListener('abort', abort, { once: true }); child.stdin?.once?.('error', () => stopAndFinish('provider_failed')); child.stdin?.end(prompt, 'utf8');
    });
  }
  async runAcp(call, prompt) {
    checkAborted(call.signal); const started = Date.now(); const child = this.spawn(this.executable, ['--acp', '--stdio', '--no-auto-update', '--no-color', '--no-custom-instructions', '--no-experimental', '--no-remote', '--no-remote-export', '--disable-builtin-mcps', '--available-tools='], { shell: false, windowsHide: true, detached: true, stdio: ['pipe', 'pipe', 'pipe'], env: this.safeEnvironment() }); let output = ''; let lineBuffer = ''; let nextId = 1; const pending = new Map(); let sessionId; let stdoutBytes = 0; let overflow = false; const decoder = new TextDecoder();
    const request = message => new Promise((resolve, reject) => { if (pending.size >= 8) { reject(new ProviderToolError('provider_response_too_large')); return; } const id = nextId++; pending.set(id, { resolve, reject }); try { child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, ...message })}\n`); } catch { pending.delete(id); reject(new ProviderToolError('provider_failed')); } });
    return await new Promise(resolve => { let settled = false; let stopping = false; const finish = value => { if (!settled) { settled = true; clearTimeout(timer); call.signal?.removeEventListener('abort', abort); resolve(value); } }; const stop = async code => { if (stopping) return; stopping = true; try { await this.killProcess(child); } catch {} finish(failureResult(call, new ProviderToolError(code))); }; const abort = () => { void stop('provider_cancelled'); }; const timer = setTimeout(() => { void stop('provider_timeout'); }, this.timeoutMs); timer.unref?.(); const protocolFail = () => { void stop('provider_failed'); }; const handle = line => { if (Buffer.byteLength(line, 'utf8') > 262144) { overflow = true; void stop('provider_response_too_large'); return; } let message; try { message = JSON.parse(line); } catch { protocolFail(); return; } if (!message || message.jsonrpc !== '2.0') return protocolFail(); if (message.method === 'session/update') { const update = message.params?.update; const text = update?.content?.text ?? update?.content?.text?.value ?? (update?.sessionUpdate === 'agent_message_chunk' ? update?.content?.text : ''); if (typeof text === 'string') { const room = this.maxOutput - Buffer.byteLength(output, 'utf8'); if (Buffer.byteLength(text, 'utf8') > room) { overflow = true; void stop('provider_response_too_large'); } else output += text; } return; } if (message.method === 'session/request_permission') { if (!Number.isInteger(message.id)) return protocolFail(); try { child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { outcome: 'cancelled' } })}\n`); } catch { protocolFail(); } return; } if (message.method) return protocolFail(); if (!Number.isInteger(message.id) || !pending.has(message.id)) return protocolFail(); const item = pending.get(message.id); pending.delete(message.id); if (message.error) item.reject(new ProviderToolError('provider_failed')); else item.resolve(message.result ?? {}); };
      child.stdout?.on('data', chunk => { stdoutBytes += Buffer.byteLength(chunk); if (stdoutBytes > 524288) { overflow = true; void stop('provider_response_too_large'); return; } lineBuffer += decoder.decode(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk), { stream: true }); let split; while ((split = lineBuffer.indexOf('\n')) >= 0) { const line = lineBuffer.slice(0, split).replace(/\r$/u, ''); lineBuffer = lineBuffer.slice(split + 1); handle(line); } }); child.stdout?.on('end', () => { lineBuffer += decoder.decode(); if (lineBuffer) handle(lineBuffer); }); child.stderr?.on('data', () => {}); child.once('error', error => finish(failureResult(call, new ProviderToolError(error.code === 'ENOENT' ? 'copilot_cli_unavailable' : 'provider_failed')))); child.once('close', code => { if (settled || stopping) return; if (pending.size || code !== 0) return finish(failureResult(call, new ProviderToolError('provider_failed'))); finish(result(call, 'ok', { provider: 'github_copilot', state: 'ready', stdout: output, exit_class: 'ok', truncated: false, duration_ms: Date.now() - started, cli_version: this.version, egress_bytes: Buffer.byteLength(prompt, 'utf8'), idempotency: 'new' })); }); call.signal?.addEventListener('abort', abort, { once: true }); (async () => { try { const init = await request({ method: 'initialize', params: { protocolVersion: 1, clientInfo: { name: 'local-assistant-engine', version: '0.1.0' }, capabilities: {} } }); if (!init || typeof init !== 'object') throw new ProviderToolError('provider_failed'); const opened = await request({ method: 'session/new', params: { mcpServers: [] } }); sessionId = opened?.sessionId; if (typeof sessionId !== 'string' || sessionId.length > 128) throw new ProviderToolError('provider_failed'); const prompted = await request({ method: 'session/prompt', params: { sessionId, prompt: [{ type: 'text', text: prompt }] } }); if (!prompted || typeof prompted !== 'object') throw new ProviderToolError('provider_failed'); try { child.stdin.end(); } catch {} } catch (error) { protocolFail(); } })(); });
  }
  safeEnvironment() { return minimalEnvironment(this.environment); }
}

export function createCopilotTool(options = {}) { const provider = options instanceof CopilotCliProvider ? options : new CopilotCliProvider(options); return { ...copilotDefinition, preview: call => provider.preview(call), execute: call => provider.execute(call) }; }
