export const CONFIG_VERSION = '0.1.0';
const PROVIDERS = ['disabled', 'approved_http_search', 'approved_http_fetch', 'browser_open'];
const MODES = ['fixture', 'native'];
const own = (o, key) => Object.prototype.hasOwnProperty.call(o, key);
function object(value, name) { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`); return value; }
function keys(value, allowed, name) { for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new Error(`${name} has unknown key: ${key}`); }

export function validateConfig(input = {}) {
  object(input, 'config');
  keys(input, ['version', 'host', 'engine', 'workspace_roots', 'network'], 'config');
  if (own(input, 'version') && input.version !== CONFIG_VERSION) throw new Error('unsupported config version');
  if (own(input, 'host')) {
    const h = object(input.host, 'host'); keys(h, ['bind', 'max_body_bytes', 'request_timeout_ms', 'max_connections'], 'host');
    if (own(h, 'bind') && h.bind !== '127.0.0.1') throw new Error('host.bind must be 127.0.0.1');
    if (own(h, 'max_body_bytes') && (!Number.isInteger(h.max_body_bytes) || h.max_body_bytes < 1024 || h.max_body_bytes > 1048576)) throw new Error('host.max_body_bytes out of range');
    if (own(h, 'request_timeout_ms') && (!Number.isInteger(h.request_timeout_ms) || h.request_timeout_ms < 100 || h.request_timeout_ms > 120000)) throw new Error('host.request_timeout_ms out of range');
    if (own(h, 'max_connections') && (!Number.isInteger(h.max_connections) || h.max_connections < 1 || h.max_connections > 256)) throw new Error('host.max_connections out of range');
  }
  if (own(input, 'engine')) {
    const e = object(input.engine, 'engine'); keys(e, ['mode', 'endpoint'], 'engine');
    if (own(e, 'mode') && !MODES.includes(e.mode)) throw new Error('engine.mode unsupported');
    if (own(e, 'endpoint') && (typeof e.endpoint !== 'string' || e.endpoint.length > 512)) throw new Error('engine.endpoint invalid');
  }
  if (own(input, 'workspace_roots')) {
    if (!Array.isArray(input.workspace_roots) || input.workspace_roots.length > 16 || input.workspace_roots.some(x => (typeof x === 'string' && (x.length < 1 || x.length > 1024)) || (x && typeof x === 'object' && !Array.isArray(x) && (typeof x.id !== 'string' || x.id.length < 1 || x.id.length > 64 || typeof x.path !== 'string' || x.path.length < 1 || x.path.length > 1024 || (x.read !== undefined && typeof x.read !== 'boolean') || (x.write !== undefined && typeof x.write !== 'boolean'))) || (typeof x !== 'string' && (!x || typeof x !== 'object' || Array.isArray(x))))) throw new Error('workspace_roots invalid');
  }
  if (own(input, 'network')) {
    const n = object(input.network, 'network'); keys(n, ['provider'], 'network');
    if (own(n, 'provider') && !PROVIDERS.includes(n.provider)) throw new Error('network.provider unsupported');
  }
  return structuredClone(input);
}

export const DEFAULT_CONFIG = Object.freeze({ version: CONFIG_VERSION, host: { bind: '127.0.0.1', max_body_bytes: 65536, request_timeout_ms: 30000, max_connections: 32 }, engine: { mode: 'fixture' }, workspace_roots: [], network: { provider: 'disabled' } });

export function mergeConfig(input = {}) { const checked = validateConfig(input); return validateConfig({ ...DEFAULT_CONFIG, ...checked, host: { ...DEFAULT_CONFIG.host, ...(checked.host ?? {}) }, engine: { ...DEFAULT_CONFIG.engine, ...(checked.engine ?? {}) }, network: { ...DEFAULT_CONFIG.network, ...(checked.network ?? {}) } }); }
