// Screened stderr logger for the MCP bridge.
//
// WHY so restrictive: stdout belongs to the MCP client (anything else corrupts the stream),
// and stderr is captured verbatim into VS Code's "Show Output" panel and Copilot CLI logs,
// which users paste into bug reports. AGENTS.md requires metadata-only logging, so this
// logger cannot carry free text at all: event names and string fields must be short
// identifiers, and every line is scrubbed for configured secrets as a last line of defence.

const IDENT = /^[A-Za-z0-9_.:/-]{0,64}$/;
const LEVELS = { off: 0, error: 1, info: 2, debug: 3 };

export function createLogger({ write = text => process.stderr.write(text), level = 'info', secrets = [], now = () => new Date() } = {}) {
  const threshold = LEVELS[level] ?? LEVELS.info;
  const emit = (severity, event, fields = {}) => {
    if (LEVELS[severity] > threshold) return;
    const parts = [`lae-mcp ${now().toISOString()} ${severity} ${IDENT.test(event) ? event : 'event'}`];
    for (const [key, value] of Object.entries(fields)) {
      if (!IDENT.test(key)) continue;
      parts.push(`${key}=${screen(value)}`);
    }
    let line = parts.join(' ');
    for (const secret of secrets) if (typeof secret === 'string' && secret.length >= 8) line = line.split(secret).join('[redacted]');
    try { write(`${line}\n`); } catch { /* a closed stderr must never take the bridge down */ }
  };
  return { error: (event, fields) => emit('error', event, fields), info: (event, fields) => emit('info', event, fields), debug: (event, fields) => emit('debug', event, fields) };
}

function screen(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? String(Math.round(value * 1000) / 1000) : 'NaN';
  if (typeof value === 'boolean') return String(value);
  if (typeof value === 'string' && IDENT.test(value)) return value || '""';
  return '[omitted]';
}

export const silentLogger = Object.freeze({ error() {}, info() {}, debug() {} });
