import { spawn as nodeSpawn } from 'node:child_process';
import { ProviderToolError, boundedArray, boundedString, checkAborted, digest, exactObject, failureResult, result } from './provider-common.mjs';

const MAX_OUTPUT = 65536;
const PATH = value => boundedString(value, 'context_path', { min: 1, max: 1024, identifier: true }).replaceAll('\\', '/');
const validate = input => { const args = exactObject(input, ['prompt', 'workspace_id', 'context_paths'], ['prompt', 'workspace_id', 'context_paths']); boundedString(args.prompt, 'prompt', { min: 1, max: 8192 }); boundedString(args.workspace_id, 'workspace_id', { min: 1, max: 64, identifier: true }); boundedArray(args.context_paths, 'context_paths', { max: 8, item: PATH }); if (args.context_paths.some(value => value.startsWith('/') || /^[A-Za-z]:\//u.test(value) || value.split('/').some(part => !part || part === '.' || part === '..'))) throw new ProviderToolError('invalid_tool_arguments', 'context paths must be relative and contained'); return structuredClone(args); };

export const copilotDefinition = Object.freeze({ name: 'coding.copilot_ask', version: '0.1.0', risk_tier: 'T3', side_effect: 'cloud_inference', network: true, data_egress: 'prompt_and_selected_files', requires_confirmation: true, timeout_ms: 30000, output_limit: MAX_OUTPUT, input_schema: { type: 'object', additionalProperties: false, required: ['prompt', 'workspace_id', 'context_paths'], properties: { prompt: { type: 'string', minLength: 1, maxLength: 8192 }, workspace_id: { type: 'string', minLength: 1, maxLength: 64 }, context_paths: { type: 'array', maxItems: 8, items: { type: 'string', minLength: 1, maxLength: 1024 } } } } });

function spawnProcess(executable, args, options) { return nodeSpawn(executable, args, options); }

export class CopilotCliProvider {
  constructor({ enabled = false, executable, allowlist = [], version = 'unverified', versionCheck, readContext, spawn = spawnProcess, timeoutMs = 30000, maxOutput = MAX_OUTPUT, environment = process.env } = {}) {
    this.enabled = enabled === true; this.executable = executable; this.allowlist = new Set(allowlist); this.version = version; this.versionCheck = versionCheck; this.readContext = readContext; this.spawn = spawn; this.timeoutMs = Math.min(120000, Math.max(100, timeoutMs)); this.maxOutput = Math.min(MAX_OUTPUT, Math.max(1024, maxOutput)); this.environment = environment; this.proposals = new Map();
  }
  state() { if (!this.enabled) return 'disabled'; if (typeof this.executable !== 'string' || !this.executable || !this.versionCheck) return 'unconfigured'; if (!this.allowlist.has(this.executable)) return 'unconfigured'; return 'ready'; }
  validate(input) { return validate(input); }
  async preview(call) { const args = validate(call.arguments); const contextBytes = typeof this.readContext?.estimate === 'function' ? await this.readContext.estimate(args.workspace_id, args.context_paths) : 0; const bytes = Buffer.byteLength(args.prompt, 'utf8') + (Number.isInteger(contextBytes) ? contextBytes : 0); if (bytes > 65536) throw new ProviderToolError('invalid_tool_arguments', 'prompt and selected context exceed egress bound'); const proposal = { digest: digest(args), name: call.name, workspace_id: args.workspace_id }; this.proposals.set(call.id, proposal); return { provider: 'github_copilot', destination: 'GitHub Copilot cloud', action: 'prompt_only', workspace_id: args.workspace_id, context_paths: args.context_paths, egress_bytes: bytes, data_categories: ['prompt', ...(args.context_paths.length ? ['selected_workspace_files'] : [])], disclosure: 'Selected prompt/context is sent to GitHub Copilot; output is untrusted.' }; }
  async execute(call) {
    let args; try { args = validate(call.arguments); const saved = this.proposals.get(call.id); if (!saved || saved.digest !== digest(args)) throw new ProviderToolError('copilot_policy_denied'); if (!this.enabled) throw new ProviderToolError('copilot_cli_unavailable'); if (!this.executable || !this.versionCheck || !this.allowlist.has(this.executable)) throw new ProviderToolError('copilot_policy_denied'); if (!await this.versionCheck(this.executable)) throw new ProviderToolError('copilot_policy_denied'); const context = await this.context(args); const prompt = context ? `${args.prompt}\n\nSelected context:\n${context}` : args.prompt; return await this.run(call, args, prompt); } catch (error) { if (error?.code === 'invalid_tool_arguments') throw error; return failureResult(call, error); }
  }
  async context(args) {
    if (!args.context_paths.length) return '';
    if (typeof this.readContext !== 'function') throw new ProviderToolError('copilot_policy_denied');
    const value = await this.readContext(args.workspace_id, args.context_paths); if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 57344) throw new ProviderToolError('provider_response_too_large'); return value;
  }
  async run(call, args, prompt) {
    checkAborted(call.signal); const started = Date.now(); const child = this.spawn(this.executable, ['--prompt', '-'], { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: this.safeEnvironment() }); let output = ''; let truncated = false; let stderr = '';
    const collect = (current, chunk) => { const text = chunk.toString('utf8'); const room = this.maxOutput - Buffer.byteLength(current, 'utf8'); if (room <= 0) { truncated = true; return current; } const bytes = Buffer.from(text, 'utf8'); if (bytes.length > room) { truncated = true; return current + bytes.subarray(0, room).toString('utf8'); } return current + text; };
    const stop = () => { try { child.kill('SIGTERM'); } catch {} };
    return await new Promise((resolve) => {
      let settled = false; const finish = value => { if (!settled) { settled = true; clearTimeout(timer); call.signal?.removeEventListener('abort', abort); resolve(value); } }; const abort = () => { stop(); finish(failureResult(call, new ProviderToolError('provider_cancelled'))); }; const timer = setTimeout(() => { stop(); finish(failureResult(call, new ProviderToolError('provider_timeout'))); }, this.timeoutMs);
      child.stdout?.on('data', chunk => { output = collect(output, chunk); }); child.stderr?.on('data', chunk => { stderr = collect(stderr, chunk); }); child.once('error', error => finish(failureResult(call, new ProviderToolError(error.code === 'ENOENT' ? 'copilot_cli_unavailable' : 'provider_failed')))); child.once('close', code => { const exitClass = code === 0 ? 'ok' : code === null ? 'cancelled' : 'failed'; finish(result(call, code === 0 ? 'ok' : 'failed', { provider: 'github_copilot', state: 'ready', stdout: output, exit_class: exitClass, truncated, duration_ms: Date.now() - started, cli_version: this.version, egress_bytes: Buffer.byteLength(prompt, 'utf8') })); }); call.signal?.addEventListener('abort', abort, { once: true }); child.stdin?.end(prompt, 'utf8');
    });
  }
  safeEnvironment() { const env = {}; if (typeof this.environment.PATH === 'string') env.PATH = this.environment.PATH; if (typeof this.environment.SystemRoot === 'string') env.SystemRoot = this.environment.SystemRoot; return env; }
}

export function createCopilotTool(options = {}) { const provider = options instanceof CopilotCliProvider ? options : new CopilotCliProvider(options); return { ...copilotDefinition, preview: call => provider.preview(call), execute: call => provider.execute(call) }; }
