import { spawn as nodeSpawn } from 'node:child_process';
import { lstat, realpath, stat } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { tmpdir } from 'node:os';
import { ProviderToolError, boundedArray, boundedString, checkAborted, digest, exactObject, failureResult, result } from './provider-common.mjs';
import { parseStrictJson } from '../agent/tool-envelope.mjs';
import { isTrustedWorkspaceContextReader } from './copilot-context.mjs';

const MAX_OUTPUT = 65536;
const MAX_ATTEMPTS = 256;
const MAX_FRAME_BYTES = 65536;
const MAX_CWD_BYTES = 1024;
const VERSION = /^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/u;
const WORKSPACE_ID = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/u;
const ACP_PERMISSION_KINDS = new Set(['allow_once', 'allow_always', 'reject_once', 'reject_always']);
const PATH = (value, name = 'context_path') => boundedString(value, name, { min: 1, max: 1024, identifier: true }).replaceAll('\\', '/');
const logicalWorkspacePath = workspaceId => `/lae/workspaces/${workspaceId}`;
const validate = input => { const args = exactObject(input, ['prompt', 'workspace_id', 'context_paths'], ['prompt', 'workspace_id', 'context_paths']); boundedString(args.prompt, 'prompt', { min: 1, max: 8192 }); boundedString(args.workspace_id, 'workspace_id', { min: 1, max: 64, identifier: true }); if (!WORKSPACE_ID.test(args.workspace_id)) throw new ProviderToolError('invalid_tool_arguments', 'workspace_id must be an opaque lowercase identifier'); boundedArray(args.context_paths, 'context_paths', { max: 8 }); const contextPaths = args.context_paths.map((value, index) => PATH(value, `context_paths[${index}]`)); if (contextPaths.some(value => value.startsWith('/') || /^[A-Za-z]:\//u.test(value) || value.split('/').some(part => !part || part === '.' || part === '..'))) throw new ProviderToolError('invalid_tool_arguments', 'context paths must be relative and contained'); return { ...structuredClone(args), context_paths: contextPaths }; };

const COPILOT_PARAMETERS = { type: 'object', additionalProperties: false, required: ['prompt', 'workspace_id', 'context_paths'], properties: { prompt: { type: 'string', minLength: 1, maxLength: 8192 }, workspace_id: { type: 'string', minLength: 1, maxLength: 64, pattern: '^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$' }, context_paths: { type: 'array', maxItems: 8, items: { type: 'string', minLength: 1, maxLength: 1024 } } } };
export const copilotDefinition = Object.freeze({ name: 'coding.copilot_ask', version: '0.1.0', description: 'Ask GitHub Copilot for a bounded prompt-only answer with explicitly selected context.', risk_tier: 'T3', side_effect: 'cloud_inference', network: true, data_egress: 'prompt_and_selected_files', requires_confirmation: true, timeout_ms: 30000, output_limit: MAX_OUTPUT, parameters: COPILOT_PARAMETERS, input_schema: COPILOT_PARAMETERS });

function exactKeys(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ProviderToolError('provider_failed');
  const allowed = new Set([...required, ...optional]);
  if (Object.keys(value).some(key => !allowed.has(key)) || required.some(key => !Object.hasOwn(value, key))) throw new ProviderToolError('provider_failed');
  return value;
}

function boundedFrameString(value, max = 2048) {
  if (typeof value !== 'string' || value.length < 1 || value.length > max || /[\u0000-\u001f\u007f]/u.test(value) || Buffer.byteLength(value, 'utf8') > max * 4) throw new ProviderToolError('provider_failed');
  return value;
}

function validateAvailableCommand(command) {
  exactKeys(command, ['name'], ['description']);
  boundedFrameString(command.name, 256);
  if (command.description !== undefined) boundedFrameString(command.description);
}

function validatePermissionOption(option) {
  exactKeys(option, ['optionId', 'name', 'kind'], ['description']);
  boundedFrameString(option.optionId, 256); boundedFrameString(option.name, 512); boundedFrameString(option.kind, 128);
  if (!ACP_PERMISSION_KINDS.has(option.kind)) throw new ProviderToolError('provider_failed');
  if (option.description !== undefined) boundedFrameString(option.description);
}

function validateFrameShape(message) {
  if (!message || message.jsonrpc !== '2.0') throw new ProviderToolError('provider_failed');
  if (typeof message.method === 'string') {
    const notification = message.method === 'session/update' && !Object.hasOwn(message, 'id');
    if (!notification && (!Number.isInteger(message.id) || message.id < 1 || message.id > 0x7fffffff)) throw new ProviderToolError('provider_failed');
    exactKeys(message, notification ? ['jsonrpc', 'method', 'params'] : ['jsonrpc', 'id', 'method', 'params']);
    if (message.method === 'session/update') {
      const params = exactKeys(message.params, ['sessionId', 'update']);
      if (typeof params.sessionId !== 'string' || params.sessionId.length < 1 || params.sessionId.length > 128 || /[\u0000-\u001f\u007f]/u.test(params.sessionId)) throw new ProviderToolError('provider_failed');
      const update = exactKeys(params.update, ['sessionUpdate'], ['content', 'availableCommands']);
      if (update.sessionUpdate === 'agent_message_chunk') {
        if (Object.keys(update).length !== 2) throw new ProviderToolError('provider_failed');
        const content = exactKeys(update.content, ['type', 'text']);
        if (content.type !== 'text' || typeof content.text !== 'string' || Buffer.byteLength(content.text, 'utf8') > MAX_OUTPUT) throw new ProviderToolError('provider_failed');
      } else if (update.sessionUpdate === 'available_commands_update') {
        if (Object.keys(update).length !== 2) throw new ProviderToolError('provider_failed');
        const commands = exactKeys(update, ['sessionUpdate', 'availableCommands']);
        if (!Array.isArray(commands.availableCommands) || commands.availableCommands.length > 128) throw new ProviderToolError('provider_failed');
        commands.availableCommands.forEach(validateAvailableCommand);
      } else throw new ProviderToolError('provider_failed');
    } else if (message.method === 'session/request_permission') {
      // ACP permission requests are intentionally answered cancelled. Keep the
      // accepted shape narrow so a server cannot smuggle arbitrary authority.
      const params = exactKeys(message.params, [], ['sessionId', 'toolCall', 'options']);
      if (params.sessionId !== undefined && (typeof params.sessionId !== 'string' || params.sessionId.length < 1 || params.sessionId.length > 128 || /[\u0000-\u001f\u007f]/u.test(params.sessionId))) throw new ProviderToolError('provider_failed');
      if (params.toolCall !== undefined) { const toolCall = exactKeys(params.toolCall, [], ['toolCallId', 'title', 'description', 'kind']); for (const key of ['toolCallId', 'title', 'description', 'kind']) if (toolCall[key] !== undefined && (typeof toolCall[key] !== 'string' || toolCall[key].length > 2048 || /[\u0000-\u001f\u007f]/u.test(toolCall[key]))) throw new ProviderToolError('provider_failed'); }
      if (params.options !== undefined) { if (!Array.isArray(params.options) || params.options.length > 16) throw new ProviderToolError('provider_failed'); params.options.forEach(validatePermissionOption); }
    } else throw new ProviderToolError('provider_failed');
    return message;
  }
  if (!Number.isInteger(message.id) || message.id < 1 || message.id > 0x7fffffff) throw new ProviderToolError('provider_failed');
  if (Object.hasOwn(message, 'error')) { exactKeys(message, ['jsonrpc', 'id', 'error']); const error = exactKeys(message.error, ['code'], ['message']); if (!Number.isInteger(error.code) || error.code < -32768 || error.code > 32768 || error.message !== undefined && (typeof error.message !== 'string' || error.message.length > 2048 || /[\u0000-\u001f\u007f]/u.test(error.message))) throw new ProviderToolError('provider_failed'); }
  else { exactKeys(message, ['jsonrpc', 'id', 'result']); if (!message.result || typeof message.result !== 'object' || Array.isArray(message.result)) throw new ProviderToolError('provider_failed'); }
  return message;
}

/** Parse one ACP NDJSON frame without accepting duplicate, unknown, or
 * oversized JSON. Exported only for contract-level mocked tests. */
export function parseCopilotAcpFrame(line) {
  if (typeof line !== 'string' || !line || Buffer.byteLength(line, 'utf8') > MAX_FRAME_BYTES) throw new ProviderToolError('provider_response_too_large');
  let parsed;
  try { parsed = parseStrictJson(line, { maxBytes: MAX_FRAME_BYTES, maxDepth: 8, maxString: 16384, maxArray: 32, maxObject: 32 }); } catch { throw new ProviderToolError('provider_failed'); }
  return validateFrameShape(parsed);
}

function sameIdentity(left, right) {
  return Boolean(left && right && left.canonicalPath === right.canonicalPath && left.dev === right.dev && left.ino === right.ino && left.size === right.size && left.mtimeMs === right.mtimeMs && left.nlink === right.nlink && left.uid === right.uid && left.mode === right.mode && left.brokerIssued === right.brokerIssued && left.workspace_id === right.workspace_id);
}

function workspaceIdentity(identity, workspaceId) {
  return identity?.brokerIssued === true && identity.workspace_id === workspaceId;
}

async function filesystemIdentity(path, { directory = false } = {}) {
  if (typeof path !== 'string' || !isAbsolute(path) || Buffer.byteLength(path, 'utf8') > MAX_CWD_BYTES) throw new ProviderToolError('copilot_policy_denied');
  const before = await lstat(path);
  if (before.isSymbolicLink()) throw new ProviderToolError('copilot_policy_denied');
  const canonicalPath = await realpath(path);
  const current = await stat(canonicalPath);
  if ((directory ? !current.isDirectory() : !current.isFile()) || !directory && current.nlink !== 1) throw new ProviderToolError('copilot_policy_denied');
  if (directory && (typeof current.uid !== 'number' || typeof process.getuid !== 'function' || current.uid !== process.getuid() || (current.mode & 0o777) !== 0o700)) throw new ProviderToolError('copilot_policy_denied');
  // Path-based stat cannot hold an identity through spawn. A future broker
  // may issue brokerIssued=true; this fallback is intentionally non-authorizing.
  return Object.freeze({ canonicalPath, dev: current.dev, ino: current.ino, size: current.size, mtimeMs: current.mtimeMs, nlink: current.nlink, uid: current.uid, mode: current.mode & 0o777, brokerIssued: false, workspace_id: null });
}

function validateBinding(binding) {
  if (!binding || typeof binding !== 'object' || Array.isArray(binding) || Object.keys(binding).length !== 4 || typeof binding.operation_id !== 'string' || typeof binding.operation_digest !== 'string' || typeof binding.arguments_digest !== 'string' || typeof binding.preview_digest !== 'string' || !/^[a-f0-9]{64}$/u.test(binding.operation_digest) || !/^[a-f0-9]{64}$/u.test(binding.arguments_digest) || !/^[a-f0-9]{64}$/u.test(binding.preview_digest)) throw new ProviderToolError('provider_permission_insufficient');
  return binding;
}

// The issuer is intentionally module-private. Controllers can observe and
// transfer an attestation already issued by this provider, but generic tools
// cannot manufacture the WeakMap identity or its exact journal binding.
const copilotAttestations = new WeakMap();
function copilotPayloadDigest(value) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.content)) return null;
  const content = value.content.map(item => {
    if (!item || typeof item !== 'object' || item.type !== 'text' || typeof item.text !== 'string') return null;
    return { type: 'text', text: item.text };
  });
  if (content.some(item => item === null)) return null;
  return digest({ status: value.status, id: value.id, name: value.name, content });
}
export const copilotSafeCompletionDigest = copilotPayloadDigest;
function issueCopilotAttestation(value, { call, binding }) {
  if (!binding || value?.status !== 'ok') return value;
  const safePayloadDigest = copilotPayloadDigest(value);
  if (!safePayloadDigest) return value;
  copilotAttestations.set(value, Object.freeze({
    provider: 'github_copilot', call_id: call.id, tool_name: call.name,
    operation_id: binding.operation_id, operation_digest: binding.operation_digest,
    arguments_digest: binding.arguments_digest, preview_digest: binding.preview_digest,
    safe_payload_digest: safePayloadDigest,
  }));
  return value;
}
export const readCopilotAttestation = value => value && typeof value === 'object' ? copilotAttestations.get(value) ?? null : null;
export const transferCopilotAttestation = (source, target) => { const attestation = readCopilotAttestation(source); if (attestation && target && typeof target === 'object') copilotAttestations.set(target, attestation); return target; };

function spawnProcess(executable, args, options) { return nodeSpawn(executable, args, options); }

function putBounded(map, key, value, max) { if (map.size >= max && !map.has(key)) map.delete(map.keys().next().value); map.set(key, value); }

function minimalEnvironment(environment = {}) {
  const env = {};
  if (typeof environment?.PATH === 'string') env.PATH = environment.PATH;
  if (typeof environment?.SystemRoot === 'string') env.SystemRoot = environment.SystemRoot;
  if (typeof environment?.WINDIR === 'string') env.WINDIR = environment.WINDIR;
  if (typeof environment?.COPILOT_GH_HOST === 'string') env.COPILOT_GH_HOST = environment.COPILOT_GH_HOST;
  for (const value of Object.values(env)) if (value.length > 4096 || /[\u0000-\u001f\u007f]/u.test(value)) throw new ProviderToolError('copilot_policy_denied');
  return env;
}

function waitForClose(child, timeoutMs) {
  if ((child.exitCode !== null && child.exitCode !== undefined) || (child.signalCode !== null && child.signalCode !== undefined)) return Promise.resolve(true);
  return new Promise(resolve => {
    let settled = false;
    const finish = value => { if (settled) return; settled = true; clearTimeout(timer); child.removeListener?.('close', onClose); resolve(value); };
    const onClose = () => finish(true);
    const timer = setTimeout(() => finish(false), timeoutMs);
    timer.unref?.();
    child.once?.('close', onClose);
  });
}

export async function killCopilotProcessTree(child, { platform = process.platform, spawn = nodeSpawn, graceMs = 1000 } = {}) {
  if (!child || !Number.isInteger(child.pid) || child.pid <= 0 || (child.exitCode !== null && child.exitCode !== undefined) || (child.signalCode !== null && child.signalCode !== undefined)) return;
  if (platform === 'win32') {
    let killer;
    try { killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: 'ignore' }); } catch { try { child.kill(); } catch {} }
    if (killer && !await waitForClose(killer, Math.min(5000, Math.max(100, graceMs)))) throw new ProviderToolError('provider_cleanup_unknown');
    if (!await waitForClose(child, Math.min(5000, Math.max(100, graceMs)))) throw new ProviderToolError('provider_cleanup_unknown');
    return;
  }
  try { process.kill(-child.pid, 'SIGTERM'); } catch { try { child.kill('SIGTERM'); } catch {} }
  await waitForClose(child, Math.min(5000, Math.max(100, graceMs)));
  // A detached leader can exit while a descendant keeps the process group
  // alive, so always issue the bounded final group kill after the grace wait.
  try { process.kill(-child.pid, 'SIGKILL'); } catch { if (child.exitCode === null && child.signalCode === null) { try { child.kill('SIGKILL'); } catch {} } }
  if (!await waitForClose(child, Math.min(5000, Math.max(100, graceMs)))) throw new ProviderToolError('provider_cleanup_unknown');
}

async function boundedCleanup(provider, child, wasClosed = () => false) {
  let timer;
  try {
    await Promise.race([
      Promise.resolve().then(() => provider.killProcess(child)),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new ProviderToolError('provider_cleanup_unknown')), 7000);
        timer.unref?.();
      }),
    ]);
  } finally { clearTimeout(timer); }
  if (!provider.testOnly && !wasClosed() && child.exitCode === null && child.exitCode === undefined && child.signalCode === null && child.signalCode === undefined) throw new ProviderToolError('provider_cleanup_unknown');
}

export function createCopilotVersionCheck({ expectedVersion, spawn = spawnProcess, environment = {}, timeoutMs = 5000, platform = process.platform, requireCwd = false } = {}) {
  if (typeof expectedVersion !== 'string' || !VERSION.test(expectedVersion)) throw new TypeError('Copilot expected version must be an exact semantic version');
  const normalized = expectedVersion.startsWith('v') ? expectedVersion.slice(1) : expectedVersion;
  const boundedTimeout = Math.min(15000, Math.max(100, timeoutMs));
  return async (executable, signal, cwd) => {
    checkAborted(signal);
    if (requireCwd && (typeof cwd !== 'string' || !isAbsolute(cwd))) return false;
    if (platform === 'win32' && !environment?.SystemRoot && !environment?.WINDIR) return false;
    let child;
    try { child = spawn(executable, ['--no-auto-update', '--no-color', 'version'], { ...(cwd ? { cwd } : {}), shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'], env: minimalEnvironment(environment) }); } catch { return false; }
    const matched = await new Promise(resolve => {
      let settled = false; let stopping = false; let output = ''; let oversized = false;
      const finish = value => { if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); resolve(value); };
      const stop = async () => { if (stopping || settled) return; stopping = true; try { await killCopilotProcessTree(child, { platform, spawn, graceMs: 250 }); } catch {} finish(false); };
      const abort = () => { void stop(); };
      const timer = setTimeout(() => { void stop(); }, boundedTimeout);
      timer.unref?.();
      child.stdout?.on('data', chunk => { const bytes = Buffer.from(chunk); if (Buffer.byteLength(output, 'utf8') + bytes.length > 4096) { oversized = true; void stop(); return; } output += bytes.toString('utf8'); });
      child.once('error', () => { void stop(); });
      child.once('close', code => {
        if (stopping) return finish(false);
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
  constructor({ enabled = false, executable, allowlist = [], version = 'unverified', versionCheck, readContext, protocol = 'acp', cwd, spawn = spawnProcess, killProcess, timeoutMs = 30000, maxOutput = MAX_OUTPUT, environment = {}, platform = process.platform, testOnly = false, executableIdentity, cwdIdentity } = {}) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 120000) throw new TypeError('invalid Copilot timeout');
    if (!Number.isInteger(maxOutput) || maxOutput < 1024 || maxOutput > MAX_OUTPUT) throw new TypeError('invalid Copilot output limit');
    const isTest = testOnly === true;
    const selectedCwd = cwd ?? (isTest ? tmpdir() : undefined);
    if (!['acp', 'legacy_stdin'].includes(protocol) || selectedCwd !== undefined && (typeof selectedCwd !== 'string' || !isAbsolute(selectedCwd) || Buffer.byteLength(selectedCwd, 'utf8') > MAX_CWD_BYTES) || (protocol === 'legacy_stdin' && !isTest) || (readContext !== undefined && !isTest && !isTrustedWorkspaceContextReader(readContext)) || (!isTest && enabled === true && typeof executable !== 'string')) throw new TypeError('invalid Copilot protocol/cwd/test gate');
    if (typeof executable === 'string' && (!isAbsolute(executable) || executable.length > 1024) || allowlist.some(item => typeof item !== 'string' || !isAbsolute(item) || item.length > 1024)) throw new TypeError('invalid Copilot executable path');
    if (executableIdentity !== undefined && !isTest) throw new TypeError('executable identity injection is test-only');
    if (cwdIdentity !== undefined && !isTest) throw new TypeError('cwd identity injection is test-only');
    this.enabled = enabled === true; Object.defineProperty(this, 'testOnly', { value: isTest, enumerable: false }); this.executable = executable; this.allowlist = new Set(allowlist); this.version = version; this.versionCheck = versionCheck; this.readContext = readContext; this.protocol = protocol; this.cwd = selectedCwd; this.spawn = spawn; this.killProcess = killProcess ?? (child => killCopilotProcessTree(child, { platform, spawn })); this.timeoutMs = timeoutMs; this.maxOutput = maxOutput; this.environment = environment; this.platform = platform; this.executableIdentity = executableIdentity ?? (isTest ? async value => ({ canonicalPath: value }) : value => filesystemIdentity(value)); this.cwdIdentity = cwdIdentity ?? (isTest ? async value => ({ canonicalPath: value }) : value => filesystemIdentity(value, { directory: true })); this.proposals = new Map(); this.attempts = new Map(); this.probeIdentity = null;
  }
  state() { if (!this.enabled) return 'disabled'; if (!this.testOnly) return 'unconfigured'; if (!this.cwd || typeof this.executable !== 'string' || !this.executable || !this.versionCheck) return 'unconfigured'; if (!this.allowlist.has(this.executable)) return 'unconfigured'; return 'ready'; }
  validate(input) { return validate(input); }
  async preview(call) { const args = validate(call.arguments); const context = await this.context(args); const contextBytes = Buffer.byteLength(context.text, 'utf8'); const promptBytes = Buffer.byteLength(args.prompt, 'utf8'); const separatorBytes = context.text ? Buffer.byteLength('\n\nSelected context:\n', 'utf8') : 0; const bytes = promptBytes + separatorBytes + contextBytes; if (bytes > 65536) throw new ProviderToolError('invalid_tool_arguments', 'prompt and selected context exceed egress bound'); let executableIdentity = null; let reviewedCwdIdentity = null; try { executableIdentity = await this.executableIdentity(this.executable); } catch { if (this.testOnly) executableIdentity = { canonicalPath: this.executable }; } try { reviewedCwdIdentity = await this.cwdIdentity(this.cwd); } catch { if (this.testOnly) reviewedCwdIdentity = { canonicalPath: this.cwd }; } const proposal = { digest: digest(args), name: call.name, workspace_id: args.workspace_id, context_digest: digest({ text: context.text, files: context.files }), context_bytes: contextBytes, egress_bytes: bytes, executable_identity: executableIdentity, cwd_identity: reviewedCwdIdentity }; putBounded(this.proposals, call.id, proposal, 128); return { provider: 'github_copilot', destination: 'GitHub Copilot cloud', action: 'prompt_only', workspace_id: args.workspace_id, context_paths: args.context_paths, context_files: context.files, egress_bytes: bytes, data_categories: ['prompt', ...(args.context_paths.length ? ['selected_workspace_files'] : [])], disclosure: 'Selected prompt/context is sent to GitHub Copilot; output is untrusted.' }; }
  async execute(call) {
    let args; try { args = validate(call.arguments); const saved = this.proposals.get(call.id); if (!saved || saved.name !== call.name || saved.digest !== digest(args) || !saved.executable_identity || !saved.cwd_identity) throw new ProviderToolError('copilot_policy_denied'); if (!call.authorization || typeof call.authorization !== 'object' || Array.isArray(call.authorization) || call.authorization.kind !== 'user_confirmation' || Object.keys(call.authorization).length !== 1) throw new ProviderToolError('provider_permission_insufficient'); if (!this.enabled) throw new ProviderToolError('copilot_cli_unavailable'); if (!this.executable || !this.versionCheck || !this.allowlist.has(this.executable)) throw new ProviderToolError('copilot_policy_denied'); const binding = call.internal?.journal_binding; if (!this.testOnly) validateBinding(binding); const probeBefore = await this.executableIdentity(this.executable); if (!sameIdentity(probeBefore, saved.executable_identity) || !this.testOnly && (!workspaceIdentity(probeBefore, args.workspace_id))) throw new ProviderToolError('copilot_policy_denied'); const cwdBefore = await this.cwdIdentity(this.cwd, args.workspace_id); if (!sameIdentity(cwdBefore, saved.cwd_identity) || !this.testOnly && (!workspaceIdentity(cwdBefore, args.workspace_id))) throw new ProviderToolError('copilot_policy_denied'); if (!await this.versionCheck(probeBefore.canonicalPath, call.signal, this.cwd)) throw new ProviderToolError('copilot_policy_denied'); const probeAfter = await this.executableIdentity(this.executable); if (!sameIdentity(probeBefore, probeAfter) || !this.testOnly && (!workspaceIdentity(probeAfter, args.workspace_id))) throw new ProviderToolError('copilot_policy_denied'); const context = await this.context(args); const contextBytes = Buffer.byteLength(context.text, 'utf8'); if (digest({ text: context.text, files: context.files }) !== saved.context_digest || contextBytes !== saved.context_bytes) throw new ProviderToolError('copilot_policy_denied', 'selected context changed after preview'); const prompt = context.text ? `${args.prompt}\n\nSelected context:\n${context.text}` : args.prompt; if (Buffer.byteLength(prompt, 'utf8') !== saved.egress_bytes || Buffer.byteLength(prompt, 'utf8') > 65536) throw new ProviderToolError('copilot_policy_denied'); const attemptKey = `${binding?.operation_digest ?? `${call.id}:${digest(args)}`}`; if (this.attempts.has(attemptKey)) return this.attempts.get(attemptKey); putBounded(this.attempts, attemptKey, failureResult(call, new ProviderToolError('provider_request_already_attempted')), MAX_ATTEMPTS); let output; try { output = await this.run(call, args, prompt, probeAfter.canonicalPath, binding); } catch (error) { output = failureResult(call, error); } this.attempts.set(attemptKey, output); return output; } catch (error) { if (error?.code === 'invalid_tool_arguments') throw error; return failureResult(call, error); }
  }
  async context(args) {
    if (!args.context_paths.length) return { text: '', files: [] };
    if (typeof this.readContext !== 'function') throw new ProviderToolError('copilot_policy_denied');
    const value = await this.readContext(args.workspace_id, args.context_paths); const details = typeof value === 'string' ? { text: value, files: [] } : value; if (!details || typeof details.text !== 'string' || !Array.isArray(details.files) || Buffer.byteLength(details.text, 'utf8') > 57344) throw new ProviderToolError('provider_response_too_large'); return details;
  }
  async run(call, args, prompt, executable = this.executable, binding = null) {
    if (this.protocol === 'acp') return this.runAcp(call, prompt, executable, binding);
    checkAborted(call.signal); const started = Date.now(); const child = this.spawn(executable, ['-s', '--no-auto-update', '--no-color', '--no-custom-instructions', '--no-experimental', '--no-remote', '--no-remote-export', '--no-ask-user', '--disable-builtin-mcps', '--disallow-temp-dir', '--log-level=none', '--available-tools='], { cwd: this.cwd, shell: false, windowsHide: true, detached: true, stdio: ['pipe', 'pipe', 'pipe'], env: this.safeEnvironment() }); let output = ''; let truncated = false; const outputDecoder = new TextDecoder();
    const collect = (current, chunk, decoder, final = false) => { const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), 'utf8'); const text = decoder.decode(bytes, { stream: !final }); const room = this.maxOutput - Buffer.byteLength(current, 'utf8'); if (room <= 0) { truncated = true; return current; } const textBytes = Buffer.from(text, 'utf8'); if (textBytes.length > room) { truncated = true; let bounded = new TextDecoder().decode(textBytes.subarray(0, room)); while (bounded.endsWith('\uFFFD')) bounded = bounded.slice(0, -1); return current + bounded; } return current + text; };
    const stop = async () => { try { await this.killProcess(child); } catch {} };
    let outputOverflow = false;
    return await new Promise((resolve) => {
      let settled = false; let forcedCode = null; let killBackstop; const finish = value => { if (!settled) { settled = true; clearTimeout(timer); clearTimeout(killBackstop); call.signal?.removeEventListener('abort', abort); resolve(value); } }; const stopAndFinish = code => { if (forcedCode) return; forcedCode = code; killBackstop = setTimeout(() => finish(failureResult(call, new ProviderToolError(code))), 6000); killBackstop.unref?.(); void stop().finally(() => finish(failureResult(call, new ProviderToolError(code)))); }; const abort = () => stopAndFinish('provider_cancelled'); const timer = setTimeout(() => stopAndFinish('provider_timeout'), this.timeoutMs); timer.unref?.();
      child.stdout?.on('data', chunk => { const before = Buffer.byteLength(output, 'utf8'); output = collect(output, chunk, outputDecoder); if (Buffer.byteLength(output, 'utf8') >= this.maxOutput && Buffer.byteLength(chunk) + before > this.maxOutput && !outputOverflow) { outputOverflow = true; stopAndFinish('provider_response_too_large'); } }); child.stderr?.on('data', () => {}); child.once('error', error => stopAndFinish(error.code === 'ENOENT' ? 'copilot_cli_unavailable' : 'provider_failed')); child.once('close', code => { if (forcedCode) return; output = collect(output, Buffer.alloc(0), outputDecoder, true); const exitClass = code === 0 ? 'ok' : code === null ? 'cancelled' : 'failed'; const outputResult = result(call, code === 0 ? 'ok' : 'failed', { provider: 'github_copilot', state: 'ready', stdout: output, exit_class: exitClass, truncated, duration_ms: Date.now() - started, cli_version: this.version, egress_bytes: Buffer.byteLength(prompt, 'utf8'), idempotency: 'new' }); finish(issueCopilotAttestation(outputResult, { call, binding })); }); call.signal?.addEventListener('abort', abort, { once: true }); child.stdin?.once?.('error', () => stopAndFinish('provider_failed')); child.stdin?.end(prompt, 'utf8');
    });
  }
 
  safeEnvironment() { return minimalEnvironment(this.environment); }
}

export function createCopilotTool(options = {}) { const provider = options instanceof CopilotCliProvider ? options : new CopilotCliProvider(options); return { ...copilotDefinition, preview: call => provider.preview(call), execute: call => provider.execute(call) }; }

// Production ACP dispatch uses this strict implementation; its only workspace
// path sent to ACP is a logical ID. Legacy stdin remains test-only in run().
async function hardenedRunAcp(call, prompt, executable = this.executable, binding = null) {
  checkAborted(call.signal);
  const env = this.safeEnvironment();
  if (this.platform === 'win32' && !env.SystemRoot && !env.WINDIR) return failureResult(call, new ProviderToolError('copilot_policy_denied'));
  let child;
  try { child = this.spawn(executable, ['--acp', '--stdio', '--no-auto-update', '--no-color', '--no-custom-instructions', '--no-experimental', '--no-remote', '--no-remote-export', '--disable-builtin-mcps', '--available-tools='], { cwd: this.cwd, shell: false, windowsHide: true, detached: true, stdio: ['pipe', 'pipe', 'pipe'], env }); } catch { return failureResult(call, new ProviderToolError('copilot_cli_unavailable')); }
  const started = Date.now(); const pending = new Map(); const serverRequestIds = new Set(); let nextId = 1; let phase = 'boot'; let sessionId = null; let output = ''; let lineBuffer = ''; let stdoutBytes = 0; let promptDone = false; let settled = false; let stopping = false; let observedClose = false; let timer;
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const finish = value => { if (settled) return; settled = true; clearTimeout(timer); call.signal?.removeEventListener('abort', abort); returnValue(value); };
  let returnValue;
  const completed = new Promise(resolve => { returnValue = resolve; });
  const stop = async code => { if (stopping || settled) return; stopping = true; const error = new ProviderToolError(code); for (const item of pending.values()) item.reject(error); pending.clear(); let cleanupFailed = false; try { await boundedCleanup(this, child, () => observedClose); } catch { cleanupFailed = true; } finish(failureResult(call, cleanupFailed ? new ProviderToolError('provider_failed') : error)); };
  const abort = () => { void stop('provider_cancelled'); };
  const request = message => new Promise((resolve, reject) => { if (pending.size >= 8 || phase === 'complete') { reject(new ProviderToolError('provider_failed')); return; } const id = nextId++; pending.set(id, { method: message.method, resolve, reject }); try { child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, ...message })}\n`); } catch { pending.delete(id); reject(new ProviderToolError('provider_failed')); void stop('provider_failed'); } });
  const handle = line => {
    let message; try { message = parseCopilotAcpFrame(line); } catch (error) { void stop(error.code === 'provider_response_too_large' ? error.code : 'provider_failed'); return; }
    if (message.method === 'session/update') {
      if (phase !== 'prompting' || message.params.sessionId !== sessionId) { void stop('provider_failed'); return; }
      if (message.params.update.sessionUpdate === 'agent_message_chunk') { const text = message.params.update.content.text; const bytes = Buffer.byteLength(text, 'utf8'); if (Buffer.byteLength(output, 'utf8') + bytes > this.maxOutput) { void stop('provider_response_too_large'); return; } output += text; }
      return;
    }
    if (message.method === 'session/request_permission') { if (phase !== 'prompting' || message.params.sessionId !== sessionId || serverRequestIds.has(message.id)) { void stop('provider_failed'); return; } if (serverRequestIds.size >= 8) { void stop('provider_response_too_large'); return; } serverRequestIds.add(message.id); try { child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { outcome: { outcome: 'cancelled' } } })}\n`); } catch { void stop('provider_failed'); } return; }
    const item = pending.get(message.id); if (!item) { void stop('provider_failed'); return; } pending.delete(message.id);
    if (message.error) { item.reject(new ProviderToolError('provider_failed')); return; }
    try {
      if (item.method === 'initialize') { exactKeys(message.result, ['protocolVersion'], ['agentCapabilities', 'authMethods', 'agentInfo']); if (message.result.protocolVersion !== 1 || phase !== 'boot') throw new ProviderToolError('provider_failed'); phase = 'initialized'; }
      else if (item.method === 'session/new') { exactKeys(message.result, ['sessionId']); if (phase !== 'initialized' || typeof message.result.sessionId !== 'string' || message.result.sessionId.length < 1 || message.result.sessionId.length > 128 || /[\u0000-\u001f\u007f]/u.test(message.result.sessionId)) throw new ProviderToolError('provider_failed'); sessionId = message.result.sessionId; phase = 'session_open'; }
      else if (item.method === 'session/prompt') { exactKeys(message.result, ['stopReason']); if (message.result.stopReason !== 'end_turn' || phase !== 'prompting') throw new ProviderToolError('provider_failed'); promptDone = true; phase = 'complete'; }
      else throw new ProviderToolError('provider_failed');
      item.resolve(message.result);
    } catch { item.reject(new ProviderToolError('provider_failed')); void stop('provider_failed'); }
  };
  child.stdout?.on('data', chunk => { if (settled || stopping) return; const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk); stdoutBytes += bytes.length; if (stdoutBytes > 524288) { void stop('provider_response_too_large'); return; } try { lineBuffer += decoder.decode(bytes, { stream: true }); } catch { void stop('provider_failed'); return; } let split; while ((split = lineBuffer.indexOf('\n')) >= 0) { const line = lineBuffer.slice(0, split).replace(/\r$/u, ''); lineBuffer = lineBuffer.slice(split + 1); if (Buffer.byteLength(line, 'utf8') > MAX_FRAME_BYTES) { void stop('provider_response_too_large'); return; } handle(line); } if (Buffer.byteLength(lineBuffer, 'utf8') > MAX_FRAME_BYTES) void stop('provider_response_too_large'); });
  child.stdout?.on('end', () => { try { lineBuffer += decoder.decode(); } catch { void stop('provider_failed'); return; } if (lineBuffer) handle(lineBuffer); });
  child.stderr?.on('data', () => {}); child.once('error', error => { void stop(error.code === 'ENOENT' ? 'copilot_cli_unavailable' : 'provider_failed'); });
  child.once('close', code => { observedClose = true; if (settled || stopping) return; if (!promptDone || pending.size || code !== 0) { void stop('provider_failed'); return; } void (async () => { try { await boundedCleanup(this, child, () => observedClose); const outputResult = result(call, 'ok', { provider: 'github_copilot', state: 'ready', stdout: output, exit_class: 'ok', truncated: false, duration_ms: Date.now() - started, cli_version: this.version, egress_bytes: Buffer.byteLength(prompt, 'utf8'), idempotency: 'new' }); finish(issueCopilotAttestation(outputResult, { call, binding })); } catch { finish(failureResult(call, new ProviderToolError('provider_failed'))); } })(); });
  timer = setTimeout(() => { void stop('provider_timeout'); }, this.timeoutMs); timer.unref?.(); call.signal?.addEventListener('abort', abort, { once: true }); child.stdin?.once?.('error', () => { void stop('provider_failed'); });
  (async () => { try { await request({ method: 'initialize', params: { protocolVersion: 1, clientCapabilities: {}, clientInfo: { name: 'local-assistant-engine', version: '0.1.0' } } }); await request({ method: 'session/new', params: { cwd: logicalWorkspacePath(call.arguments.workspace_id), mcpServers: [] } }); phase = 'prompting'; await request({ method: 'session/prompt', params: { sessionId, prompt: [{ type: 'text', text: prompt }] } }); try { child.stdin.end(); } catch {} } catch { void stop('provider_failed'); } })();
  return completed;
}

CopilotCliProvider.prototype.runAcp = hardenedRunAcp;
