export const CONFIG_VERSION = '0.1.0';
const PROVIDERS = ['disabled', 'approved_http_search', 'approved_http_fetch', 'browser_open'];
const MODES = ['fixture', 'native'];
const own = (o, key) => Object.prototype.hasOwnProperty.call(o, key);
function object(value, name) { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`); return value; }
function keys(value, allowed, name) { for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new Error(`${name} has unknown key: ${key}`); }

export function validateConfig(input = {}) {
  object(input, 'config');
  keys(input, ['version', 'host', 'engine', 'workspace_roots', 'applications', 'network', 'providers'], 'config');
  if (own(input, 'version') && input.version !== CONFIG_VERSION) throw new Error('unsupported config version');
  if (own(input, 'host')) {
    const h = object(input.host, 'host'); keys(h, ['bind', 'max_body_bytes', 'request_timeout_ms', 'max_connections', 'max_header_bytes', 'max_header_count'], 'host');
    if (own(h, 'bind') && h.bind !== '127.0.0.1') throw new Error('host.bind must be 127.0.0.1');
    if (own(h, 'max_body_bytes') && (!Number.isInteger(h.max_body_bytes) || h.max_body_bytes < 1024 || h.max_body_bytes > 1048576)) throw new Error('host.max_body_bytes out of range');
    if (own(h, 'request_timeout_ms') && (!Number.isInteger(h.request_timeout_ms) || h.request_timeout_ms < 100 || h.request_timeout_ms > 120000)) throw new Error('host.request_timeout_ms out of range');
    if (own(h, 'max_connections') && (!Number.isInteger(h.max_connections) || h.max_connections < 1 || h.max_connections > 256)) throw new Error('host.max_connections out of range');
    if (own(h, 'max_header_bytes') && (!Number.isInteger(h.max_header_bytes) || h.max_header_bytes < 1024 || h.max_header_bytes > 65536)) throw new Error('host.max_header_bytes out of range');
    if (own(h, 'max_header_count') && (!Number.isInteger(h.max_header_count) || h.max_header_count < 8 || h.max_header_count > 256)) throw new Error('host.max_header_count out of range');
  }
  if (own(input, 'engine')) {
    const e = object(input.engine, 'engine'); keys(e, ['mode', 'endpoint', 'model', 'backend', 'request_timeout_ms'], 'engine');
    if (own(e, 'mode') && !MODES.includes(e.mode)) throw new Error('engine.mode unsupported');
    if (own(e, 'endpoint') && (typeof e.endpoint !== 'string' || e.endpoint.length > 512)) throw new Error('engine.endpoint invalid');
    if (own(e, 'model') && (typeof e.model !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(e.model))) throw new Error('engine.model invalid');
    if (own(e, 'backend') && (typeof e.backend !== 'string' || !/^[A-Za-z0-9._/-]{1,128}$/.test(e.backend))) throw new Error('engine.backend invalid');
    if (own(e, 'request_timeout_ms') && (!Number.isInteger(e.request_timeout_ms) || e.request_timeout_ms < 1000 || e.request_timeout_ms > 120000)) throw new Error('engine.request_timeout_ms out of range');
  }
  if (own(input, 'workspace_roots')) {
    if (!Array.isArray(input.workspace_roots) || input.workspace_roots.length > 16 || input.workspace_roots.some(x => (typeof x === 'string' && (x.length < 1 || x.length > 1024)) || (x && typeof x === 'object' && !Array.isArray(x) && (Object.keys(x).some(key => !['id', 'path', 'read', 'write'].includes(key)) || typeof x.id !== 'string' || x.id.length < 1 || x.id.length > 64 || typeof x.path !== 'string' || x.path.length < 1 || x.path.length > 1024 || (x.read !== undefined && typeof x.read !== 'boolean') || (x.write !== undefined && typeof x.write !== 'boolean'))) || (typeof x !== 'string' && (!x || typeof x !== 'object' || Array.isArray(x))))) throw new Error('workspace_roots invalid');
  }
  if (own(input, 'applications')) {
    const applications = object(input.applications, 'applications'); if (Object.keys(applications).length > 16) throw new Error('applications limit exceeded');
    for (const [id, value] of Object.entries(applications)) {
      if (!/^[A-Za-z0-9_.-]{1,64}$/.test(id)) throw new Error('applications id invalid');
      const app = object(value, `applications.${id}`); keys(app, ['executable_id', 'executable', 'args'], `applications.${id}`);
      if (own(app, 'executable_id') && (typeof app.executable_id !== 'string' || !/^[A-Za-z0-9_.-]{1,64}$/.test(app.executable_id))) throw new Error(`applications.${id}.executable_id invalid`);
      if (typeof app.executable !== 'string' || app.executable.length < 1 || app.executable.length > 1024 || /[\u0000-\u001f\u007f]/u.test(app.executable)) throw new Error(`applications.${id}.executable invalid`);
      if (!Array.isArray(app.args) || app.args.length > 16 || app.args.some(arg => typeof arg !== 'string' || arg.length > 1024 || /[\u0000\r\n\u007f]/u.test(arg))) throw new Error(`applications.${id}.args invalid`);
    }
  }
  if (own(input, 'network')) {
    const n = object(input.network, 'network'); keys(n, ['provider'], 'network');
    if (own(n, 'provider') && !PROVIDERS.includes(n.provider)) throw new Error('network.provider unsupported');
  }
  if (own(input, 'providers')) {
    const p = object(input.providers, 'providers'); keys(p, ['microsoft_graph', 'copilot'], 'providers');
    if (own(p, 'microsoft_graph')) { const graph = object(p.microsoft_graph, 'providers.microsoft_graph'); keys(graph, ['enabled', 'permission_profile', 'account_fingerprint', 'scope'], 'providers.microsoft_graph'); if (own(graph, 'enabled') && typeof graph.enabled !== 'boolean') throw new Error('providers.microsoft_graph.enabled invalid'); if (own(graph, 'permission_profile') && !['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access'].includes(graph.permission_profile)) throw new Error('providers.microsoft_graph.permission_profile invalid'); if (own(graph, 'account_fingerprint') && (typeof graph.account_fingerprint !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(graph.account_fingerprint))) throw new Error('providers.microsoft_graph.account_fingerprint invalid'); if (own(graph, 'scope') && (typeof graph.scope !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(graph.scope))) throw new Error('providers.microsoft_graph.scope invalid'); }
    if (own(p, 'copilot')) { const copilot = object(p.copilot, 'providers.copilot'); keys(copilot, ['enabled', 'executable', 'allowlist', 'version'], 'providers.copilot'); if (own(copilot, 'enabled') && typeof copilot.enabled !== 'boolean') throw new Error('providers.copilot.enabled invalid'); if (own(copilot, 'executable') && (typeof copilot.executable !== 'string' || copilot.executable.length < 1 || copilot.executable.length > 1024 || !/^(?:[A-Za-z]:[\\/]|\/)/u.test(copilot.executable) || /[\u0000-\u001f\u007f]/u.test(copilot.executable))) throw new Error('providers.copilot.executable invalid'); if (own(copilot, 'allowlist') && (!Array.isArray(copilot.allowlist) || copilot.allowlist.length > 16 || copilot.allowlist.some(value => typeof value !== 'string' || value.length < 1 || value.length > 1024 || !/^(?:[A-Za-z]:[\\/]|\/)/u.test(value) || /[\u0000-\u001f\u007f]/u.test(value)))) throw new Error('providers.copilot.allowlist invalid'); if (own(copilot, 'version') && (typeof copilot.version !== 'string' || !/^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/u.test(copilot.version))) throw new Error('providers.copilot.version invalid'); }
  }
  return structuredClone(input);
}

export const DEFAULT_CONFIG = Object.freeze({ version: CONFIG_VERSION, host: { bind: '127.0.0.1', max_body_bytes: 65536, request_timeout_ms: 30000, max_connections: 32, max_header_bytes: 16384, max_header_count: 64 }, engine: { mode: 'fixture' }, workspace_roots: [], applications: {}, network: { provider: 'disabled' }, providers: {} });

export function mergeConfig(input = {}) { const checked = validateConfig(input); return validateConfig({ ...DEFAULT_CONFIG, ...checked, host: { ...DEFAULT_CONFIG.host, ...(checked.host ?? {}) }, engine: { ...DEFAULT_CONFIG.engine, ...(checked.engine ?? {}) }, applications: { ...DEFAULT_CONFIG.applications, ...(checked.applications ?? {}) }, network: { ...DEFAULT_CONFIG.network, ...(checked.network ?? {}) }, providers: { ...DEFAULT_CONFIG.providers, ...(checked.providers ?? {}) } }); }
