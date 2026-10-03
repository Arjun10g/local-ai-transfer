import { spawn } from 'node:child_process';
import { basename, win32 as pathWin32 } from 'node:path';
import { lstat, realpath, stat } from 'node:fs/promises';
import { applyOperatorGrantPolicy } from '../../providers/operator-tool-policy.mjs';
import { ProviderToolError, digest, exactObject, boundedInteger, boundedString, checkAborted, failureResult, result } from '../../providers/provider-common.mjs';
import { validateRelativePath } from './workspace-policy.mjs';
import { windowsSystem32Path } from './system-tools.mjs';

const MAX_ACTIONS = 32;
const MAX_ARGS = 32;
const MAX_PARAM_NAME = 64;
const MAX_PARAM_VALUE = 8192;
const MAX_OUTPUT = 32768;
const MAX_TIMEOUT = 110000;
const CAP_PREFIX = 'local.process:';
const absoluteLocalPath = value => typeof value === 'string' && value.length >= 1 && value.length <= 1024 && !/[\u0000-\u001f\u007f]/u.test(value) && (/^\//u.test(value) || /^[A-Za-z]:[\\/]/u.test(value)) && !/^\/\//u.test(value) && !/^\\\\/u.test(value);
const safeId = /^[A-Za-z0-9_.-]{1,64}$/u;
const placeholder = /^\{([A-Za-z0-9_.-]{1,64})\}$/u;
const forbiddenExecutables = new Set(['powershell', 'powershell_ise', 'pwsh', 'cmd', 'wscript', 'cscript', 'mshta', 'bash', 'sh', 'zsh', 'fish', 'python', 'python3', 'node', 'deno', 'ruby', 'perl', 'rundll32', 'regsvr32', 'msiexec', 'installutil']);
const forbiddenIds = new Set(['__proto__', 'constructor', 'prototype']);
const executableAllowed = value => { if (!absoluteLocalPath(value)) return false; const normalized = value.replaceAll('\\', '/').toLowerCase(); const name = basename(normalized); const stem = name.replace(/\.exe$/u, ''); return !forbiddenExecutables.has(stem) && !/\.(?:bat|cmd|ps1|vbs|vbe|js|jse|wsf|wsh|hta)$/u.test(name); };

export const processDefinition = Object.freeze({
  name: 'process.run_allowlisted', version: '0.1.0', description: 'Run one fixed operator-configured local action with bounded output. The executable is an unsandboxed operator trust boundary and may access the network.', risk_tier: 'T3', side_effect: 'process_execution', network: true, data_egress: 'operator_configured', requires_confirmation: true, timeout_ms: MAX_TIMEOUT + 10000, output_limit: MAX_OUTPUT * 2,
  parameters: { type: 'object', additionalProperties: false, required: ['action_id'], properties: { action_id: { type: 'string', minLength: 1, maxLength: 64 }, parameters: { type: 'object', additionalProperties: true } } },
  input_schema: { type: 'object', additionalProperties: false, required: ['action_id'], properties: { action_id: { type: 'string', minLength: 1, maxLength: 64 }, parameters: { type: 'object', additionalProperties: true } } }
});

function modelParameterSchema(spec) {
  if (spec.type === 'string') return { type: 'string', maxLength: spec.max_length, ...(spec.enum ? { enum: spec.enum } : {}) };
  if (spec.type === 'integer') return { type: 'integer', minimum: spec.argv_kind ? Math.max(spec.minimum ?? 0, 0) : (spec.minimum ?? Number.MIN_SAFE_INTEGER), maximum: spec.maximum ?? Number.MAX_SAFE_INTEGER };
  return { type: 'boolean' };
}

function modelSchema(actions) {
  const entries = Object.values(actions);
  const allProperties = Object.create(null);
  for (const action of entries) for (const [name, spec] of Object.entries(action.parameters)) allProperties[name] ??= modelParameterSchema(spec);
  const variants = entries.map(action => {
    const required = action.args.map(arg => arg.match(placeholder)?.[1]).filter(Boolean);
    if (action.stdin_parameter) required.push(action.stdin_parameter);
    const parameters = { type: 'object', additionalProperties: false, properties: Object.fromEntries(Object.entries(action.parameters).map(([name, spec]) => [name, modelParameterSchema(spec)])), ...(required.length ? { required: [...new Set(required)] } : {}) };
    return { type: 'object', additionalProperties: false, required: ['action_id', ...(required.length ? ['parameters'] : [])], properties: { action_id: { enum: [action.id] }, parameters } };
  });
  return { type: 'object', additionalProperties: false, required: ['action_id'], properties: { action_id: entries.length ? { type: 'string', enum: entries.map(action => action.id) } : { type: 'string', minLength: 1, maxLength: 64 }, parameters: { type: 'object', additionalProperties: false, properties: allProperties } }, ...(variants.length ? { oneOf: variants } : {}) };
}

function validateParameterSpec(name, spec) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !['string', 'integer', 'boolean'].includes(spec.type)) throw new TypeError(`invalid process parameter: ${name}`);
  exactObject(spec, ['type', 'max_length', 'minimum', 'maximum', 'enum', 'argv_kind']);
  if (spec.type !== 'string' && (spec.max_length !== undefined || spec.enum !== undefined)) throw new TypeError(`invalid process scalar parameter: ${name}`);
  if (spec.type === 'string' && (!Number.isInteger(spec.max_length) || spec.max_length < 1 || spec.max_length > MAX_PARAM_VALUE)) throw new TypeError(`invalid process string parameter: ${name}`);
  if (spec.type === 'string' && spec.enum !== undefined && (!Array.isArray(spec.enum) || spec.enum.length < 1 || spec.enum.length > 32 || spec.enum.some(value => typeof value !== 'string' || value.length < 1 || value.length > spec.max_length))) throw new TypeError(`invalid process enum parameter: ${name}`);
  if (spec.argv_kind !== undefined && !['identifier', 'relative_path', 'enum', 'scalar'].includes(spec.argv_kind)) throw new TypeError(`invalid process argv parameter: ${name}`);
  if (spec.type === 'string' && spec.argv_kind === 'scalar' || spec.type !== 'string' && spec.argv_kind && spec.argv_kind !== 'scalar') throw new TypeError(`invalid process argv type: ${name}`);
  if (spec.argv_kind === 'enum' && !Array.isArray(spec.enum)) throw new TypeError(`process enum parameter requires enum: ${name}`);
  if (spec.type === 'integer' && (spec.minimum !== undefined && !Number.isSafeInteger(spec.minimum) || spec.maximum !== undefined && !Number.isSafeInteger(spec.maximum) || spec.minimum !== undefined && spec.maximum !== undefined && spec.minimum > spec.maximum)) throw new TypeError(`invalid process integer parameter: ${name}`);
}

function validateActions(actions) {
  if (!actions || typeof actions !== 'object' || Array.isArray(actions) || Object.keys(actions).length > MAX_ACTIONS) throw new TypeError('invalid process actions');
  const output = Object.create(null);
  for (const [id, action] of Object.entries(actions)) {
    if (!safeId.test(id) || forbiddenIds.has(id) || !action || typeof action !== 'object' || Array.isArray(action)) throw new TypeError('invalid process action');
    exactObject(action, ['executable', 'args', 'workspace_id', 'cwd', 'parameters', 'stdin_parameter', 'timeout_ms', 'max_stdout_bytes', 'max_stderr_bytes'], ['executable', 'args', 'workspace_id', 'cwd']);
    if (!executableAllowed(action.executable)) throw new TypeError(`process action ${id} executable is not permitted`);
    if (!Array.isArray(action.args) || action.args.length > MAX_ARGS || action.args.some(arg => typeof arg !== 'string' || arg.length > 1024 || /[\u0000\r\n\u007f]/u.test(arg))) throw new TypeError(`process action ${id} args invalid`);
    if (typeof action.workspace_id !== 'string' || !safeId.test(action.workspace_id)) throw new TypeError(`process action ${id} workspace invalid`);
    if (typeof action.cwd !== 'string' || action.cwd.length > 1024 || action.cwd.startsWith('/') || action.cwd.startsWith('\\') || action.cwd.includes('\0')) throw new TypeError(`process action ${id} cwd invalid`);
    const parameters = action.parameters ?? {};
    if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters) || Object.keys(parameters).length > 16) throw new TypeError(`process action ${id} parameters invalid`);
  for (const [name, spec] of Object.entries(parameters)) { if (!/^[A-Za-z0-9_.-]{1,64}$/u.test(name) || forbiddenIds.has(name)) throw new TypeError(`process action ${id} parameter name invalid`); validateParameterSpec(name, spec); }
  for (const arg of action.args) { const match = arg.match(placeholder); if (match && (!Object.hasOwn(parameters, match[1]) || !parameters[match[1]].argv_kind)) throw new TypeError(`process action ${id} references an undeclared or unconstrained parameter`); }
  if (action.stdin_parameter !== undefined && (typeof action.stdin_parameter !== 'string' || !Object.hasOwn(parameters, action.stdin_parameter) || parameters[action.stdin_parameter].type !== 'string' || parameters[action.stdin_parameter].max_length > MAX_PARAM_VALUE)) throw new TypeError(`process action ${id} stdin parameter invalid`);
  for (const name of Object.keys(parameters)) if (name !== action.stdin_parameter && !action.args.some(arg => arg === `{${name}}`)) throw new TypeError(`process action ${id} declares an unused parameter`);
    const timeoutMs = action.timeout_ms ?? 30000; if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > MAX_TIMEOUT) throw new TypeError(`process action ${id} timeout invalid`);
    const maxStdout = action.max_stdout_bytes ?? MAX_OUTPUT; const maxStderr = action.max_stderr_bytes ?? MAX_OUTPUT; if (![maxStdout, maxStderr].every(value => Number.isInteger(value) && value >= 1 && value <= MAX_OUTPUT)) throw new TypeError(`process action ${id} output bound invalid`);
    output[id] = Object.freeze({ id, executable: action.executable, args: [...action.args], workspace_id: action.workspace_id, cwd: action.cwd, parameters: structuredClone(parameters), stdin_parameter: action.stdin_parameter, timeout_ms: timeoutMs, max_stdout_bytes: maxStdout, max_stderr_bytes: maxStderr });
  }
  return Object.freeze(output);
}

function validateCall(input, actions) {
  const args = exactObject(input, ['action_id', 'parameters'], ['action_id']); boundedString(args.action_id, 'action_id', { min: 1, max: 64, identifier: true }); const action = actions[args.action_id]; if (!action) throw new ProviderToolError('invalid_tool_arguments', 'action_id is not configured'); const values = args.parameters ?? {}; exactObject(values, Object.keys(action.parameters));
  for (const arg of action.args) { const match = arg.match(placeholder); if (match && !Object.hasOwn(values, match[1])) throw new ProviderToolError('invalid_tool_arguments', `missing parameter: ${match[1]}`); }
  for (const [name, spec] of Object.entries(action.parameters)) { if (!Object.hasOwn(values, name)) { if (action.stdin_parameter === name || action.args.some(arg => arg === `{${name}}`)) throw new ProviderToolError('invalid_tool_arguments', `missing parameter: ${name}`); continue; } const value = values[name]; if (spec.type === 'string') { boundedString(value, name, { min: spec.argv_kind ? 1 : 0, max: spec.max_length }); if (spec.argv_kind === 'identifier' && !/^[A-Za-z0-9_.-]{1,64}$/u.test(value)) throw new ProviderToolError('invalid_tool_arguments', `${name} must be a safe identifier`); if (spec.argv_kind === 'relative_path') { try { validateRelativePath(value); } catch { throw new ProviderToolError('invalid_tool_arguments', `${name} must be workspace-relative`); } } if (spec.argv_kind && value.startsWith('-')) throw new ProviderToolError('invalid_tool_arguments', `${name} cannot begin with an option prefix`); if (spec.enum && !spec.enum.includes(value)) throw new ProviderToolError('invalid_tool_arguments', `${name} is not an allowed value`); } else if (spec.type === 'integer') { boundedInteger(value, name, spec.minimum ?? Number.MIN_SAFE_INTEGER, spec.maximum ?? Number.MAX_SAFE_INTEGER); if (spec.argv_kind && String(value).startsWith('-')) throw new ProviderToolError('invalid_tool_arguments', `${name} cannot begin with an option prefix`); } else if (typeof value !== 'boolean') throw new ProviderToolError('invalid_tool_arguments', `${name} must be boolean`); }
  return { action, args: { action_id: args.action_id, parameters: structuredClone(values) } };
}

function renderArgs(action, parameters) { return action.args.map(arg => { const match = arg.match(placeholder); if (!match) return arg; if (!Object.hasOwn(parameters, match[1])) throw new ProviderToolError('invalid_tool_arguments', `missing parameter: ${match[1]}`); return String(parameters[match[1]]); }); }
function isFile(info) { return Boolean(info && (typeof info.isFile === 'function' ? info.isFile() : info.isFile === true)); }
function isSymlink(info) { return Boolean(info && (typeof info.isSymbolicLink === 'function' ? info.isSymbolicLink() : info.isSymbolicLink === true)); }
async function resolveRealExecutable(path, { platform, resolveExecutable = realpath, statExecutable = lstat, statResolvedExecutable = stat } = {}) { try { const original = await statExecutable(path); if (isSymlink(original)) throw new ProviderToolError('provider_executable_invalid'); const canonical = await resolveExecutable(path); const resolved = await statResolvedExecutable(canonical); if (!isFile(resolved) || (platform !== 'win32' && typeof resolved.mode === 'number' && (resolved.mode & 0o111) === 0)) throw new ProviderToolError('provider_executable_invalid'); return canonical; } catch (error) { if (error instanceof ProviderToolError) throw error; throw new ProviderToolError('provider_executable_invalid'); } }
function validateAuthorization(authorization) { if (!authorization || typeof authorization !== 'object' || Array.isArray(authorization) || typeof authorization.kind !== 'string') throw new ProviderToolError('provider_permission_insufficient'); if (authorization.kind === 'user_confirmation') { if (Object.keys(authorization).length !== 1) throw new ProviderToolError('provider_permission_insufficient'); return authorization; } if (authorization.kind === 'operator_grant') { if (Object.keys(authorization).length !== 2 || typeof authorization.generation !== 'string' || !/^[a-f0-9]{32}$/u.test(authorization.generation)) throw new ProviderToolError('provider_permission_insufficient'); return authorization; } throw new ProviderToolError('provider_permission_insufficient'); }
function collect(target, chunk, max, final = false) { const bytes = Buffer.from(chunk); const room = max - target.bytes; const accepted = room > 0 ? bytes.subarray(0, room) : Buffer.alloc(0); const text = target.decoder.decode(accepted, { stream: !final }); return { ...target, text: target.text + text, bytes: target.bytes + accepted.length, overflow: target.overflow || accepted.length < bytes.length }; }

async function waitClosed(child, timeoutMs = 5000) { if (!child || !Number.isInteger(child.pid)) return; if (child.exitCode !== null && child.exitCode !== undefined) return; await new Promise(resolve => { let timer = setTimeout(resolve, timeoutMs); const done = () => { clearTimeout(timer); resolve(); }; child.once?.('close', done); child.once?.('exit', done); }); }
async function boundedOperation(operation, timeoutMs) { await new Promise(resolve => { let finished = false; const timer = setTimeout(() => { if (!finished) { finished = true; resolve(); } }, timeoutMs); Promise.resolve().then(operation).catch(() => {}).finally(() => { if (!finished) { finished = true; clearTimeout(timer); resolve(); } }); }); }
// taskkill gets a minimal environment (SystemRoot, windir) too: it needs none of the tokens the host holds.
// taskkill.exe is spawned by its System32 path from a validated SystemRoot:
// by bare name, CreateProcess would search the current directory first.
export async function terminateProcessTree(child, killProcess, platform, taskkillSpawn = spawn, environment = process.env) {
  if (!child) return;
  try {
    if (killProcess) await boundedOperation(() => killProcess(child), 5000);
    else if (platform === 'win32' && Number.isInteger(child.pid)) await new Promise(resolve => { let killer; try { const taskkillPath = windowsSystem32Path('taskkill.exe', environment); const systemRoot = pathWin32.dirname(pathWin32.dirname(taskkillPath)); killer = taskkillSpawn(taskkillPath, ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: 'ignore', env: { SystemRoot: systemRoot, windir: systemRoot } }); } catch { child.kill?.(); resolve(); return; } const timer = setTimeout(() => { clearTimeout(timer); child.kill?.(); resolve(); }, 5000); killer.once('close', () => { clearTimeout(timer); resolve(); }); killer.once('error', () => { clearTimeout(timer); child.kill?.(); resolve(); }); });
    else if (Number.isInteger(child.pid)) { try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill?.('SIGTERM'); } await waitClosed(child, 1000); try { process.kill(-child.pid, 'SIGKILL'); } catch { /* group is gone */ } }
    else child.kill?.('SIGTERM');
  } catch { /* termination is bounded and cleanup continues */ }
  await waitClosed(child, 5000);
}

function runChild(action, cwd, parameters, { executable = action.executable, spawnImpl = spawn, killProcess, taskkillSpawn = spawn, platform = process.platform, environment = {}, signal } = {}) {
  return new Promise((resolve, reject) => {
    checkAborted(signal); let child; try { child = spawnImpl(executable, renderArgs(action, parameters), { cwd, env: environment, shell: false, windowsHide: true, detached: platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'] }); } catch (error) { reject(new ProviderToolError('provider_failed')); return; }
    let stdout = { text: '', bytes: 0, overflow: false, decoder: new TextDecoder('utf-8', { fatal: false }) }; let stderr = { text: '', bytes: 0, overflow: false, decoder: new TextDecoder('utf-8', { fatal: false }) }; let settled = false; let stopping = false; let timer;
    const finish = (fn, value) => { if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); fn(value); };
    const stop = async error => { if (settled || stopping) return; stopping = true; await terminateProcessTree(child, killProcess, platform, taskkillSpawn, environment); finish(reject, error); };
    const abort = () => { void stop(new ProviderToolError('provider_cancelled')); };
    const timeout = () => { void stop(new ProviderToolError('provider_timeout')); };
    const overflow = () => { void stop(new ProviderToolError('provider_response_too_large')); };
    signal?.addEventListener('abort', abort, { once: true }); if (signal?.aborted) return abort(); timer = setTimeout(timeout, action.timeout_ms);
    child.stdout?.on('data', chunk => { stdout = collect(stdout, chunk, action.max_stdout_bytes); if (stdout.overflow) overflow(); }); child.stderr?.on('data', chunk => { stderr = collect(stderr, chunk, action.max_stderr_bytes); if (stderr.overflow) overflow(); }); child.stdin?.once?.('error', () => { void stop(new ProviderToolError('provider_failed')); }); child.once('error', () => { void stop(new ProviderToolError('provider_failed')); }); child.once('close', code => { if (settled || stopping) return; stdout = collect(stdout, Buffer.alloc(0), action.max_stdout_bytes, true); stderr = collect(stderr, Buffer.alloc(0), action.max_stderr_bytes, true); if (code !== 0) return finish(reject, new ProviderToolError('process_exit')); finish(resolve, { stdout: stdout.text, stderr: stderr.text, stdout_bytes: stdout.bytes, stderr_bytes: stderr.bytes }); });
    try { if (action.stdin_parameter) child.stdin?.end(String(parameters[action.stdin_parameter])); else child.stdin?.end(); } catch { void stop(new ProviderToolError('provider_failed')); }
  });
}

export class ProcessRunProvider {
  constructor({ enabled = false, actions = {}, workspacePolicy, grantControl, spawn: spawnImpl = spawn, killProcess, taskkillSpawn = spawn, platform = process.platform, environment = {}, resolveExecutable: resolveExecutableImpl = realpath, statExecutable: statExecutableImpl = lstat, statResolvedExecutable: statResolvedExecutableImpl = stat } = {}) { this.enabled = enabled === true; this.actions = validateActions(actions); this.workspacePolicy = workspacePolicy; this.grantControl = grantControl; this.spawn = spawnImpl; this.killProcess = killProcess; this.taskkillSpawn = taskkillSpawn; this.platform = platform; this.resolveExecutableImpl = resolveExecutableImpl; this.statExecutableImpl = statExecutableImpl; this.statResolvedExecutableImpl = statResolvedExecutableImpl; this.environment = Object.fromEntries(Object.entries(environment).filter(([key]) => ['SystemRoot', 'WINDIR'].includes(key))); if (Object.values(this.environment).some(value => typeof value !== 'string' || value.length > 1024 || /[\u0000-\u001f\u007f]/u.test(value))) throw new TypeError('invalid process environment'); this.proposals = new Map(); this.dispatches = new Map(); }
  state() { if (!this.enabled) return 'disabled'; if (!this.workspacePolicy || !Object.keys(this.actions).length) return 'unconfigured'; return 'ready'; }
  capability(id) { return `${CAP_PREFIX}${id}`; }
  validate(input) { return validateCall(input, this.actions); }
  async cwd(action) { if (!this.workspacePolicy) throw new ProviderToolError('provider_unconfigured'); try { const resolved = await this.workspacePolicy.resolve(action.workspace_id, action.cwd, { write: true, mustExist: true, allowEmpty: true }); return resolved.canonical; } catch (error) { throw new ProviderToolError(error.code ?? 'provider_failed'); } }
  remember(call, validated, cwd, executable) { if (this.proposals.size >= 128 && !this.proposals.has(call.id)) this.proposals.delete(this.proposals.keys().next().value); const grant = this.grantControl?.grantFor(this.capability(validated.action.id)); this.proposals.set(call.id, { name: call.name, digest: digest(validated.args), actionDigest: digest(validated.action), cwd, executable, grantGeneration: grant?.generation ?? null }); }
  modelSchema() { return modelSchema(this.actions); }
  async preview(call) { const validated = this.validate(call.arguments); const executable = await resolveRealExecutable(validated.action.executable, { platform: this.platform, resolveExecutable: this.resolveExecutableImpl, statExecutable: this.statExecutableImpl, statResolvedExecutable: this.statResolvedExecutableImpl }); const cwd = await this.cwd(validated.action); this.remember(call, validated, cwd, executable); return { provider: 'local_process', action_id: validated.action.id, executable, argv: renderArgs(validated.action, validated.args.parameters), cwd, network: true, data_egress: 'operator_configured', timeout_ms: validated.action.timeout_ms }; }
  confirmationRequired(call) { const validated = this.validate(call.arguments); const grant = this.grantControl?.grantFor(this.capability(validated.action.id)); const proposal = this.proposals.get(call.id); return !(grant && proposal?.grantGeneration === grant.generation); }
  async authorize(call) { const validated = this.validate(call.arguments); const grant = this.grantControl?.grantFor(this.capability(validated.action.id)); const proposal = this.proposals.get(call.id); return grant && proposal?.grantGeneration === grant.generation ? { kind: 'operator_grant', generation: grant.generation } : { kind: 'policy' }; }
  async execute(call) {
    let grantRevoked = false; let unsubscribe; let grantSignal; let grantRelay; let abortRelay;
    try {
      const validated = this.validate(call.arguments); if (this.state() === 'disabled') return failureResult(call, new ProviderToolError('provider_disabled')); if (this.state() === 'unconfigured') return failureResult(call, new ProviderToolError('provider_unconfigured')); const authorization = validateAuthorization(call.authorization); if (this.dispatches.has(call.id)) return failureResult(call, new ProviderToolError('process_already_attempted')); if (this.dispatches.size >= 256) return failureResult(call, new ProviderToolError('process_dispatch_limit'));
      const proposal = this.proposals.get(call.id); if (!proposal || proposal.name !== call.name || proposal.digest !== digest(validated.args) || proposal.actionDigest !== digest(validated.action)) return failureResult(call, new ProviderToolError('provider_permission_insufficient')); this.proposals.delete(call.id);
      const capability = this.capability(validated.action.id);
      if (authorization.kind === 'operator_grant') {
        const grant = this.grantControl?.grantFor(capability); if (!grant || grant.generation !== authorization.generation || proposal.grantGeneration !== grant.generation) return failureResult(call, new ProviderToolError('provider_permission_revoked'));
        grantSignal = new AbortController(); grantRelay = () => { grantRevoked = true; grantSignal.abort(); }; abortRelay = () => grantSignal.abort(); unsubscribe = this.grantControl.store.subscribe(capability, grantRelay); if (call.signal?.aborted) abortRelay(); else call.signal?.addEventListener('abort', abortRelay, { once: true });
      } else if (authorization.kind !== 'user_confirmation') return failureResult(call, new ProviderToolError('provider_permission_insufficient'));
      const cwd = await this.cwd(validated.action); const executable = await resolveRealExecutable(validated.action.executable, { platform: this.platform, resolveExecutable: this.resolveExecutableImpl, statExecutable: this.statExecutableImpl, statResolvedExecutable: this.statResolvedExecutableImpl }); if (cwd !== proposal.cwd || executable !== proposal.executable) return failureResult(call, new ProviderToolError('provider_permission_insufficient')); this.dispatches.set(call.id, { action_id: validated.action.id, started: Date.now() });
      const signal = grantSignal?.signal ?? call.signal; const output = await runChild(validated.action, cwd, validated.args.parameters, { executable, spawnImpl: this.spawn, killProcess: this.killProcess, taskkillSpawn: this.taskkillSpawn, platform: this.platform, environment: this.environment, signal });
      const current = this.grantControl?.grantFor(capability); if (grantRevoked || (authorization.kind === 'operator_grant' && (!current || current.generation !== authorization.generation))) throw new ProviderToolError('provider_permission_revoked');
      return result(call, 'ok', { provider: 'local_process', action_id: validated.action.id, stdout: output.stdout, stderr: '', stdout_bytes: output.stdout_bytes, stderr_bytes: output.stderr_bytes });
    } catch (error) { return failureResult(call, grantRevoked ? new ProviderToolError('provider_permission_revoked') : error); }
    finally { if (call.signal && abortRelay) call.signal.removeEventListener('abort', abortRelay); unsubscribe?.(); }
  }
}

export function createProcessRunTools(options = {}) { const provider = options instanceof ProcessRunProvider ? options : new ProcessRunProvider(options); const schema = provider.modelSchema(); const tool = { ...processDefinition, parameters: schema, input_schema: schema, confirmationRequired: call => provider.confirmationRequired(call), authorize: call => provider.authorize(call), preview: call => provider.preview(call), execute: call => provider.execute(call) }; return applyOperatorGrantPolicy(tool, { grantControl: null }); }
