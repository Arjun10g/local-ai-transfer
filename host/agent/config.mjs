export const CONFIG_VERSION = '0.1.0';
const PROVIDERS = ['disabled', 'approved_http_search', 'approved_http_fetch', 'browser_open'];
const MODES = ['fixture', 'native'];
const own = (o, key) => Object.prototype.hasOwnProperty.call(o, key);
// [config key, min, max, environment override] for NativeEngineClient's
// generation options; the ranges are the client's own validation ranges.
export const ENGINE_GENERATION_LIMITS = Object.freeze([
  ['first_token_timeout_ms', 1000, 3600000, 'LAE_ENGINE_FIRST_TOKEN_TIMEOUT_MS'],
  ['idle_timeout_ms', 1000, 1800000, 'LAE_ENGINE_IDLE_TIMEOUT_MS'],
  ['total_timeout_ms', 1000, 7200000, 'LAE_ENGINE_TOTAL_TIMEOUT_MS'],
  ['max_tokens', 1, 2048, 'LAE_ENGINE_MAX_TOKENS'],
]);
function object(value, name) { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`); return value; }
// An application is launched by absolute local path only.  A bare name such
// as `ms-teams.exe` would be resolved by CreateProcess, which searches the
// current directory before PATH, so a binary planted there would run instead.
// UNC and `//` paths are refused because they reach the network.
const absoluteLocalExecutable = value => typeof value === 'string' && value.length >= 2 && value.length <= 1024 && !/[\u0000-\u001f\u007f]/u.test(value) && (/^\/(?!\/)/u.test(value) || /^[A-Za-z]:[\\/]/u.test(value));
// [config key, controller option, min, max]; the ranges match
// memoryOptions() in memory-note.mjs, which validates again.
const MEMORY_CONFIG_KEYS = Object.freeze([
  ['mode', 'mode'], ['note_tokens', 'noteTokens', 32, 1024], ['note_bytes', 'noteBytes', 128, 4096], ['max_input_bytes', 'maxInputBytes', 512, 32768],
  ['per_message_bytes', 'perMessageBytes', 128, 8192], ['backlog_bytes', 'backlogBytes', 0, 262144], ['timeout_ms', 'timeoutMs', 1000, 3600000],
  ['wait_ms', 'waitMs', 0, 600000], ['plan_headroom_tokens', 'planHeadroomTokens', 0, 8192], ['plan_target_percent', 'planTargetPercent', 20, 60],
  ['archive_bytes', 'archiveBytes', 4096, 4194304], ['entry_bytes', 'entryBytes', 64, 2048], ['recall_bytes', 'recallBytes', 128, 8192], ['recall_entries', 'recallEntries', 1, 32],
]);
// The ConversationController `memory` option for a validated config.
export function memoryOptionsFromConfig(config = {}) {
  const m = config?.memory ?? {};
  return Object.fromEntries(MEMORY_CONFIG_KEYS.filter(([key]) => own(m, key)).map(([key, option]) => [option, m[key]]));
}
function keys(value, allowed, name) { for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new Error(`${name} has unknown key: ${key}`); }
// Delegation (host/delegate): [config key, min, max, default].  Off unless
// `enabled` is true or LAE_DELEGATE_ENABLED=1; the defaults are the job
// bounds the delegate bridge is documented against.
export const DELEGATE_LIMITS = Object.freeze([
  // How long a job waits for the operator's Approve before it expires.
  ['approval_timeout_ms', 5000, 600000, 120000],
  // Hard wall-clock cap on one job's generation, tools included.
  ['max_runtime_ms', 10000, 3600000, 600000],
  // Cap on the job's total generated tokens across all its model calls.
  ['max_output_tokens', 64, 16384, 2048],
  // Tool calls one job may make; each is a further model call on a CPU.
  ['max_tool_calls', 0, 8, 4],
]);
/** The delegate service's options for a validated config and environment. */
export function delegateOptionsFromConfig(config = {}, env = {}) {
  const raw = env.LAE_DELEGATE_ENABLED;
  if (raw !== undefined && raw !== '0' && raw !== '1') throw new Error('invalid LAE_DELEGATE_ENABLED; expected 0 or 1');
  const d = config?.delegate ?? {};
  const options = { enabled: d.enabled === true || raw === '1' };
  for (const [key, , , fallback] of DELEGATE_LIMITS) options[key] = own(d, key) ? d[key] : fallback;
  return options;
}

export function validateConfig(input = {}) {
  object(input, 'config');
  keys(input, ['version', 'host', 'engine', 'workspace_roots', 'applications', 'process_actions', 'network', 'providers', 'memory', 'delegate'], 'config');
  if (own(input, 'version') && input.version !== CONFIG_VERSION) throw new Error('unsupported config version');
  if (own(input, 'host')) {
    const h = object(input.host, 'host'); keys(h, ['bind', 'max_body_bytes', 'request_timeout_ms', 'max_connections', 'max_header_bytes', 'max_header_count', 'confirmation_timeout_ms'], 'host');
    if (own(h, 'bind') && h.bind !== '127.0.0.1') throw new Error('host.bind must be 127.0.0.1');
    if (own(h, 'max_body_bytes') && (!Number.isInteger(h.max_body_bytes) || h.max_body_bytes < 1024 || h.max_body_bytes > 1048576)) throw new Error('host.max_body_bytes out of range');
    if (own(h, 'request_timeout_ms') && (!Number.isInteger(h.request_timeout_ms) || h.request_timeout_ms < 100 || h.request_timeout_ms > 120000)) throw new Error('host.request_timeout_ms out of range');
    if (own(h, 'max_connections') && (!Number.isInteger(h.max_connections) || h.max_connections < 1 || h.max_connections > 256)) throw new Error('host.max_connections out of range');
    if (own(h, 'max_header_bytes') && (!Number.isInteger(h.max_header_bytes) || h.max_header_bytes < 1024 || h.max_header_bytes > 65536)) throw new Error('host.max_header_bytes out of range');
    if (own(h, 'max_header_count') && (!Number.isInteger(h.max_header_count) || h.max_header_count < 8 || h.max_header_count > 256)) throw new Error('host.max_header_count out of range');
    // How long a tool confirmation card waits for the user. Long enough to
    // narrate a demo; bounded so an abandoned card cannot hold the single
    // generation slot indefinitely.
    if (own(h, 'confirmation_timeout_ms') && (!Number.isInteger(h.confirmation_timeout_ms) || h.confirmation_timeout_ms < 5000 || h.confirmation_timeout_ms > 600000)) throw new Error('host.confirmation_timeout_ms out of range');
  }
  if (own(input, 'engine')) {
    const e = object(input.engine, 'engine'); keys(e, ['mode', 'endpoint', 'model', 'backend', 'request_timeout_ms', 'first_token_timeout_ms', 'idle_timeout_ms', 'total_timeout_ms', 'max_tokens'], 'engine');
    if (own(e, 'mode') && !MODES.includes(e.mode)) throw new Error('engine.mode unsupported');
    if (own(e, 'endpoint') && (typeof e.endpoint !== 'string' || e.endpoint.length > 512)) throw new Error('engine.endpoint invalid');
    if (own(e, 'model') && (typeof e.model !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(e.model))) throw new Error('engine.model invalid');
    if (own(e, 'backend') && (typeof e.backend !== 'string' || !/^[A-Za-z0-9._/-]{1,128}$/.test(e.backend))) throw new Error('engine.backend invalid');
    if (own(e, 'request_timeout_ms') && (!Number.isInteger(e.request_timeout_ms) || e.request_timeout_ms < 1000 || e.request_timeout_ms > 120000)) throw new Error('engine.request_timeout_ms out of range');
    // Generation deadlines and the answer cap, with the native engine client's
    // own bounds (a CPU prefill can take many minutes before the first token).
    for (const [key, min, max] of ENGINE_GENERATION_LIMITS) if (own(e, key) && (!Number.isInteger(e[key]) || e[key] < min || e[key] > max)) throw new Error(`engine.${key} out of range`);
  }
  // Opt-in conversation memory (host/agent/memory-note.mjs).  Absent means
  // 'off': the summary costs one extra engine call per compaction, which is
  // minutes on a laptop CPU.
  if (own(input, 'memory')) {
    const m = object(input.memory, 'memory'); keys(m, MEMORY_CONFIG_KEYS.map(([key]) => key), 'memory');
    if (own(m, 'mode') && !['off', 'recall', 'summary'].includes(m.mode)) throw new Error('memory.mode must be off, recall or summary');
    for (const [key, , min, max] of MEMORY_CONFIG_KEYS.slice(1)) if (own(m, key) && (!Number.isInteger(m[key]) || m[key] < min || m[key] > max)) throw new Error(`memory.${key} out of range`);
    if (Number.isInteger(m.per_message_bytes) && m.per_message_bytes > (m.max_input_bytes ?? 6144)) throw new Error('memory.per_message_bytes out of range');
  }
  if (own(input, 'workspace_roots')) {
    if (!Array.isArray(input.workspace_roots) || input.workspace_roots.length > 16 || input.workspace_roots.some(x => (typeof x === 'string' && (x.length < 1 || x.length > 1024)) || (x && typeof x === 'object' && !Array.isArray(x) && (Object.keys(x).some(key => !['id', 'path', 'read', 'write', 'delegate'].includes(key)) || typeof x.id !== 'string' || x.id.length < 1 || x.id.length > 64 || typeof x.path !== 'string' || x.path.length < 1 || x.path.length > 1024 || (x.read !== undefined && typeof x.read !== 'boolean') || (x.write !== undefined && typeof x.write !== 'boolean') || (x.delegate !== undefined && typeof x.delegate !== 'boolean'))) || (typeof x !== 'string' && (!x || typeof x !== 'object' || Array.isArray(x))))) throw new Error('workspace_roots invalid');
    // A folder lent to delegated jobs is read by a model whose answer leaves
    // the laptop, so it must be readable, and its id must be one the job's
    // scope can name.
    for (const x of input.workspace_roots) if (x && typeof x === 'object' && x.delegate === true && (x.read === false || !/^[A-Za-z0-9_.-]{1,64}$/u.test(x.id))) throw new Error('workspace_roots delegate folder must be readable with a simple id');
  }
  if (own(input, 'delegate')) {
    const d = object(input.delegate, 'delegate'); keys(d, ['enabled', ...DELEGATE_LIMITS.map(([key]) => key)], 'delegate');
    if (own(d, 'enabled') && typeof d.enabled !== 'boolean') throw new Error('delegate.enabled invalid');
    for (const [key, min, max] of DELEGATE_LIMITS) if (own(d, key) && (!Number.isInteger(d[key]) || d[key] < min || d[key] > max)) throw new Error(`delegate.${key} out of range`);
  }
  if (own(input, 'applications')) {
    const applications = object(input.applications, 'applications'); if (Object.keys(applications).length > 16) throw new Error('applications limit exceeded');
    for (const [id, value] of Object.entries(applications)) {
      if (!/^[A-Za-z0-9_.-]{1,64}$/.test(id)) throw new Error('applications id invalid');
      const app = object(value, `applications.${id}`); keys(app, ['executable_id', 'executable', 'args'], `applications.${id}`);
      if (own(app, 'executable_id') && (typeof app.executable_id !== 'string' || !/^[A-Za-z0-9_.-]{1,64}$/.test(app.executable_id))) throw new Error(`applications.${id}.executable_id invalid`);
      if (typeof app.executable !== 'string' || app.executable.length < 1 || app.executable.length > 1024 || /[\u0000-\u001f\u007f]/u.test(app.executable)) throw new Error(`applications.${id}.executable invalid`);
      if (!absoluteLocalExecutable(app.executable)) throw new Error(`applications.${id}.executable must be an absolute local path (C:\\...\\app.exe or /path/to/app); bare names and UNC paths are refused`);
      if (!Array.isArray(app.args) || app.args.length > 16 || app.args.some(arg => typeof arg !== 'string' || arg.length > 1024 || /[\u0000\r\n\u007f]/u.test(arg))) throw new Error(`applications.${id}.args invalid`);
    }
  }
  if (own(input, 'process_actions')) {
    const process = object(input.process_actions, 'process_actions'); keys(process, ['enabled', 'actions'], 'process_actions'); if (own(process, 'enabled') && typeof process.enabled !== 'boolean') throw new Error('process_actions.enabled invalid'); const actions = process.actions ?? {}; if (!actions || typeof actions !== 'object' || Array.isArray(actions) || Object.keys(actions).length > 32) throw new Error('process_actions.actions invalid');
    const forbiddenExecutables = new Set(['powershell', 'powershell_ise', 'pwsh', 'cmd', 'wscript', 'cscript', 'mshta', 'bash', 'sh', 'zsh', 'fish', 'python', 'python3', 'node', 'deno', 'ruby', 'perl', 'rundll32', 'regsvr32', 'msiexec', 'installutil']); const localPath = value => { if (!(typeof value === 'string' && value.length >= 1 && value.length <= 1024 && !/[\u0000-\u001f\u007f]/u.test(value) && (/^\//u.test(value) || /^[A-Za-z]:[\\/]/u.test(value)) && !/^\/\//u.test(value) && !/^\\\\/u.test(value))) return false; const name = value.replaceAll('\\', '/').split('/').at(-1).toLowerCase(); const stem = name.replace(/\.exe$/u, ''); return !forbiddenExecutables.has(stem) && !/\.(?:bat|cmd|ps1|vbs|vbe|js|jse|wsf|wsh|hta)$/u.test(name); }; for (const id of Object.keys(actions)) if (['__proto__', 'constructor', 'prototype'].includes(id)) throw new Error('process action invalid');
    for (const action of Object.values(actions)) { const parameters = action && typeof action === 'object' && !Array.isArray(action) ? action.parameters : null; if (parameters && typeof parameters === 'object' && !Array.isArray(parameters) && Object.keys(parameters).some(name => ['__proto__', 'constructor', 'prototype'].includes(name))) throw new Error('process action parameter invalid'); }
    for (const action of Object.values(actions)) { const parameters = action && typeof action === 'object' && !Array.isArray(action) ? action.parameters : null; if (parameters && typeof parameters === 'object' && !Array.isArray(parameters)) for (const spec of Object.values(parameters)) if (spec && typeof spec === 'object' && ((spec.type === 'string' && spec.argv_kind === 'scalar') || (spec.type !== 'string' && spec.argv_kind !== undefined && spec.argv_kind !== 'scalar'))) throw new Error('process action argv kind invalid'); }
    for (const action of Object.values(actions)) { const parameters = action && typeof action === 'object' && !Array.isArray(action) ? action.parameters : null; if (Array.isArray(action?.args) && parameters && typeof parameters === 'object' && !Array.isArray(parameters)) { const referenced = new Set(action.args.flatMap(arg => typeof arg === 'string' ? [...arg.matchAll(/^\{([A-Za-z0-9_.-]{1,64})\}$/gu)].map(match => match[1]) : [])); for (const name of Object.keys(parameters)) if (name !== action.stdin_parameter && !referenced.has(name)) throw new Error('process action parameter unused'); } }
    for (const action of Object.values(actions)) if (action && typeof action === 'object' && action.timeout_ms !== undefined && Number.isInteger(action.timeout_ms) && action.timeout_ms > 110000) throw new Error('process action timeout invalid');
    for (const [id, action] of Object.entries(actions)) { if (!/^[A-Za-z0-9_.-]{1,64}$/u.test(id) || !action || typeof action !== 'object' || Array.isArray(action)) throw new Error('process action invalid'); keys(action, ['executable', 'args', 'workspace_id', 'cwd', 'parameters', 'stdin_parameter', 'timeout_ms', 'max_stdout_bytes', 'max_stderr_bytes'], `process_actions.actions.${id}`); if (!localPath(action.executable)) throw new Error('process action executable invalid'); if (!Array.isArray(action.args) || action.args.length > 32 || action.args.some(arg => typeof arg !== 'string' || arg.length > 1024 || /[\u0000\r\n\u007f]/u.test(arg))) throw new Error('process action args invalid'); if (typeof action.workspace_id !== 'string' || !/^[A-Za-z0-9_.-]{1,64}$/u.test(action.workspace_id)) throw new Error('process action workspace invalid'); if (typeof action.cwd !== 'string' || action.cwd.length > 1024 || action.cwd.startsWith('/') || action.cwd.startsWith('\\') || action.cwd.includes('\0')) throw new Error('process action cwd invalid'); const parameters = action.parameters ?? {}; if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters) || Object.keys(parameters).length > 16) throw new Error('process action parameters invalid'); for (const [name, spec] of Object.entries(parameters)) { if (!/^[A-Za-z0-9_.-]{1,64}$/u.test(name) || !spec || typeof spec !== 'object' || Array.isArray(spec) || !['string', 'integer', 'boolean'].includes(spec.type)) throw new Error('process action parameter invalid'); keys(spec, ['type', 'max_length', 'minimum', 'maximum', 'enum', 'argv_kind'], `process action parameter ${name}`); if (spec.type === 'string' && (!Number.isInteger(spec.max_length) || spec.max_length < 1 || spec.max_length > 8192)) throw new Error('process action string parameter invalid'); if (spec.type === 'string' && spec.enum !== undefined && (!Array.isArray(spec.enum) || spec.enum.length < 1 || spec.enum.length > 32 || spec.enum.some(value => typeof value !== 'string' || value.length < 1 || value.length > spec.max_length))) throw new Error('process action enum invalid'); if (spec.argv_kind !== undefined && !['identifier', 'relative_path', 'enum', 'scalar'].includes(spec.argv_kind)) throw new Error('process action argv kind invalid'); if (spec.argv_kind === 'enum' && !Array.isArray(spec.enum)) throw new Error('process action argv enum invalid'); if (spec.type === 'integer' && ((spec.minimum !== undefined && !Number.isSafeInteger(spec.minimum)) || (spec.maximum !== undefined && !Number.isSafeInteger(spec.maximum)) || spec.minimum !== undefined && spec.maximum !== undefined && spec.minimum > spec.maximum)) throw new Error('process action integer parameter invalid'); } if (action.stdin_parameter !== undefined && (typeof action.stdin_parameter !== 'string' || !Object.hasOwn(parameters, action.stdin_parameter) || parameters[action.stdin_parameter].type !== 'string' || parameters[action.stdin_parameter].argv_kind)) throw new Error('process action stdin parameter invalid'); for (const arg of action.args) { const match = arg.match(/^\{([A-Za-z0-9_.-]{1,64})\}$/u); if (match && (!Object.hasOwn(parameters, match[1]) || !parameters[match[1]].argv_kind)) throw new Error('process action placeholder invalid'); } for (const [key, fallback] of [['timeout_ms', 30000], ['max_stdout_bytes', 32768], ['max_stderr_bytes', 32768]]) { const value = action[key] ?? fallback; const limits = key === 'timeout_ms' ? [100, 120000] : [1, 32768]; if (!Number.isInteger(value) || value < limits[0] || value > limits[1]) throw new Error(`process action ${key} invalid`); } }
  }
  if (own(input, 'network')) {
    const n = object(input.network, 'network'); keys(n, ['provider'], 'network');
    if (own(n, 'provider') && !PROVIDERS.includes(n.provider)) throw new Error('network.provider unsupported');
  }
  if (own(input, 'providers')) {
    const p = object(input.providers, 'providers'); keys(p, ['microsoft_graph', 'copilot', 'browser_actions'], 'providers');
    if (own(p, 'microsoft_graph')) { const graph = object(p.microsoft_graph, 'providers.microsoft_graph'); keys(graph, ['enabled', 'permission_profile', 'account_fingerprint', 'scope', 'tenant', 'client_id', 'scopes'], 'providers.microsoft_graph'); if (own(graph, 'enabled') && typeof graph.enabled !== 'boolean') throw new Error('providers.microsoft_graph.enabled invalid'); if (own(graph, 'permission_profile') && !['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access'].includes(graph.permission_profile)) throw new Error('providers.microsoft_graph.permission_profile invalid'); if (graph.permission_profile === 'full_access' && (typeof graph.account_fingerprint !== 'string' || !graph.account_fingerprint || graph.account_fingerprint === 'unknown')) throw new Error('providers.microsoft_graph.account_fingerprint required for full_access'); if (own(graph, 'account_fingerprint') && (typeof graph.account_fingerprint !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(graph.account_fingerprint))) throw new Error('providers.microsoft_graph.account_fingerprint invalid'); if (own(graph, 'scope') && (typeof graph.scope !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(graph.scope))) throw new Error('providers.microsoft_graph.scope invalid'); if (own(graph, 'tenant') && (typeof graph.tenant !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$/.test(graph.tenant) || graph.tenant.includes('..') || /[.-]$/.test(graph.tenant))) throw new Error('providers.microsoft_graph.tenant invalid'); if (own(graph, 'client_id') && (typeof graph.client_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(graph.client_id))) throw new Error('providers.microsoft_graph.client_id invalid'); if (own(graph, 'scopes') && (!Array.isArray(graph.scopes) || graph.scopes.length < 1 || graph.scopes.length > 9 || graph.scopes.some(scope => !['User.Read', 'Mail.Read', 'Mail.ReadWrite', 'Mail.Send', 'Chat.Read', 'Chat.ReadWrite', 'ChatMessage.Send', 'Channel.ReadBasic.All', 'ChannelMessage.Read.All'].includes(scope)) || new Set(graph.scopes).size !== graph.scopes.length || !graph.scopes.includes('User.Read'))) throw new Error('providers.microsoft_graph.scopes invalid'); }
    if (own(p, 'copilot')) { const copilot = object(p.copilot, 'providers.copilot'); keys(copilot, ['enabled', 'executable', 'allowlist', 'version'], 'providers.copilot'); const localPath = value => typeof value === 'string' && value.length >= 1 && value.length <= 1024 && !/[\u0000-\u001f\u007f]/u.test(value) && (/^\//u.test(value) || /^[A-Za-z]:[\\/]/u.test(value)) && !/^\/\//u.test(value) && !/^\\\\/u.test(value); if (own(copilot, 'enabled') && typeof copilot.enabled !== 'boolean') throw new Error('providers.copilot.enabled invalid'); if (own(copilot, 'executable') && !localPath(copilot.executable)) throw new Error('providers.copilot.executable invalid'); if (own(copilot, 'allowlist') && (!Array.isArray(copilot.allowlist) || copilot.allowlist.length > 16 || copilot.allowlist.some(value => !localPath(value)))) throw new Error('providers.copilot.allowlist invalid'); if (own(copilot, 'version') && (typeof copilot.version !== 'string' || !/^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/u.test(copilot.version))) throw new Error('providers.copilot.version invalid'); }
    if (own(p, 'browser_actions')) { const browser = object(p.browser_actions, 'providers.browser_actions'); keys(browser, ['enabled', 'executable', 'allowlist', 'safe_actions', 'action_origins'], 'providers.browser_actions'); const localPath = value => typeof value === 'string' && value.length >= 1 && value.length <= 1024 && !/[\u0000-\u001f\u007f]/u.test(value) && (/^\//u.test(value) || /^[A-Za-z]:[\\/]/u.test(value)) && !/^\/\//u.test(value) && !/^\\\\/u.test(value); if (own(browser, 'enabled') && typeof browser.enabled !== 'boolean') throw new Error('providers.browser_actions.enabled invalid'); if (own(browser, 'executable') && !localPath(browser.executable)) throw new Error('providers.browser_actions.executable invalid'); if (own(browser, 'allowlist') && (!Array.isArray(browser.allowlist) || browser.allowlist.length > 16 || browser.allowlist.some(value => !localPath(value)))) throw new Error('providers.browser_actions.allowlist invalid'); if (own(browser, 'safe_actions') && browser.safe_actions !== false && browser.safe_actions !== true) throw new Error('providers.browser_actions.safe_actions invalid'); if (browser.safe_actions === true && (!Array.isArray(browser.action_origins) || browser.action_origins.length < 1 || browser.action_origins.length > 16)) throw new Error('providers.browser_actions.action_origins required'); if (own(browser, 'action_origins') && (!Array.isArray(browser.action_origins) || browser.action_origins.length > 16 || browser.action_origins.some(value => typeof value !== 'string' || value.length < 1 || value.length > 512 || !/^https:\/\/[^/?#]+\/?$/u.test(value)))) throw new Error('providers.browser_actions.action_origins invalid'); }
  }
  return structuredClone(input);
}

export const DEFAULT_CONFIG = Object.freeze({ version: CONFIG_VERSION, host: { bind: '127.0.0.1', max_body_bytes: 65536, request_timeout_ms: 30000, max_connections: 32, max_header_bytes: 16384, max_header_count: 64, confirmation_timeout_ms: 120000 }, engine: { mode: 'fixture' }, workspace_roots: [], applications: {}, process_actions: { enabled: false, actions: {} }, network: { provider: 'disabled' }, providers: {} });

export function mergeConfig(input = {}) { const checked = validateConfig(input); return validateConfig({ ...DEFAULT_CONFIG, ...checked, host: { ...DEFAULT_CONFIG.host, ...(checked.host ?? {}) }, engine: { ...DEFAULT_CONFIG.engine, ...(checked.engine ?? {}) }, applications: { ...DEFAULT_CONFIG.applications, ...(checked.applications ?? {}) }, process_actions: { ...DEFAULT_CONFIG.process_actions, ...(checked.process_actions ?? {}), actions: { ...DEFAULT_CONFIG.process_actions.actions, ...(checked.process_actions?.actions ?? {}) } }, network: { ...DEFAULT_CONFIG.network, ...(checked.network ?? {}) }, providers: { ...DEFAULT_CONFIG.providers, ...(checked.providers ?? {}) } }); }
