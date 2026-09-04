/** Strict v0.1.0 tool envelope/result validation. No JSON-schema runtime dependency. */
export const TOOL_ENVELOPE_VERSION = '0.1.0';
export const MAX_ENVELOPE_BYTES = 32 * 1024;
const ID = /^[A-Za-z0-9_-]{1,96}$/;
const NAME = /^[a-z][a-z0-9_.-]{1,95}$/;

export class EnvelopeError extends Error {
  constructor(code, message) { super(message); this.name = 'EnvelopeError'; this.code = code; }
}

class JsonReader {
  constructor(text, limits = {}) {
    this.text = text;
    this.i = 0;
    this.maxDepth = limits.maxDepth ?? 8;
    this.maxString = limits.maxString ?? 4096;
    this.maxArray = limits.maxArray ?? 32;
    this.maxObject = limits.maxObject ?? 32;
  }
  fail(message) { throw new EnvelopeError('invalid_json', message); }
  ws() { while (/\s/.test(this.text[this.i] ?? '')) this.i++; }
  value(depth = 0) {
    if (depth > this.maxDepth) throw new EnvelopeError('json_depth_exceeded', 'JSON nesting exceeds limit');
    this.ws();
    const c = this.text[this.i];
    if (c === '{') return this.object(depth + 1);
    if (c === '[') return this.array(depth + 1);
    if (c === '"') return this.string();
    if (c === '-' || /[0-9]/.test(c ?? '')) return this.number();
    for (const [literal, val] of [['true', true], ['false', false], ['null', null]]) {
      if (this.text.startsWith(literal, this.i)) { this.i += literal.length; return val; }
    }
    this.fail(`unexpected token at byte ${this.i}`);
  }
  string() {
    const start = this.i++;
    let out = '';
    while (this.i < this.text.length) {
      const c = this.text[this.i++];
      if (c === '"') { if (out.length > this.maxString) throw new EnvelopeError('json_string_too_large', 'JSON string exceeds limit'); return out; }
      if (c === '\\') {
        const e = this.text[this.i++];
        if (!e) this.fail('unterminated escape');
        if (e === 'u') {
          const hex = this.text.slice(this.i, this.i + 4);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) this.fail('invalid unicode escape');
          out += String.fromCharCode(Number.parseInt(hex, 16)); this.i += 4;
        } else {
          const map = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' };
          if (!(e in map)) this.fail('invalid escape');
          out += map[e];
        }
      } else {
        if (c < ' ') this.fail('control character in string');
        out += c;
      }
      if (out.length > this.maxString) throw new EnvelopeError('json_string_too_large', 'JSON string exceeds limit');
    }
    this.i = start; this.fail('unterminated string');
  }
  number() {
    const m = this.text.slice(this.i).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/);
    if (!m) this.fail('invalid number');
    this.i += m[0].length; const n = Number(m[0]);
    if (!Number.isFinite(n)) this.fail('non-finite number');
    return n;
  }
  object(depth) {
    this.i++; const out = {}; const keys = new Set(); this.ws();
    if (this.text[this.i] === '}') { this.i++; return out; }
    while (this.i < this.text.length) {
      this.ws(); if (this.text[this.i] !== '"') this.fail('object key must be a string');
      const key = this.string();
      if (keys.has(key)) throw new EnvelopeError('duplicate_json_key', `duplicate key: ${key}`);
      keys.add(key); if (keys.size > this.maxObject) throw new EnvelopeError('json_object_too_large', 'JSON object exceeds limit');
      this.ws(); if (this.text[this.i++] !== ':') this.fail('missing object colon');
      out[key] = this.value(depth); this.ws();
      const c = this.text[this.i++]; if (c === '}') return out;
      if (c !== ',') this.fail('missing object comma');
    }
    this.fail('unterminated object');
  }
  array(depth) {
    this.i++; const out = []; this.ws();
    if (this.text[this.i] === ']') { this.i++; return out; }
    while (this.i < this.text.length) {
      if (out.length >= this.maxArray) throw new EnvelopeError('json_array_too_large', 'JSON array exceeds limit');
      out.push(this.value(depth)); this.ws(); const c = this.text[this.i++];
      if (c === ']') return out; if (c !== ',') this.fail('missing array comma');
    }
    this.fail('unterminated array');
  }
}

export function parseStrictJson(text, limits) {
  if (typeof text !== 'string') throw new EnvelopeError('invalid_json', 'JSON input must be text');
  if (Buffer.byteLength(text, 'utf8') > (limits?.maxBytes ?? MAX_ENVELOPE_BYTES)) throw new EnvelopeError('json_too_large', 'JSON exceeds byte limit');
  const reader = new JsonReader(text, limits); const value = reader.value(); reader.ws();
  if (reader.i !== text.length) throw new EnvelopeError('invalid_json', 'trailing JSON content');
  return value;
}

function exactObject(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new EnvelopeError('invalid_envelope', 'envelope must be an object');
  for (const key of Object.keys(value)) if (!fields.includes(key)) throw new EnvelopeError('unknown_field', `unknown field: ${key}`);
  for (const key of fields) if (!(key in value)) throw new EnvelopeError('missing_field', `missing field: ${key}`);
}

export function validateToolCall(value) {
  exactObject(value, ['id', 'name', 'arguments']);
  if (typeof value.id !== 'string' || !ID.test(value.id)) throw new EnvelopeError('invalid_call_id', 'invalid tool call id');
  if (typeof value.name !== 'string' || !NAME.test(value.name)) throw new EnvelopeError('invalid_tool_name', 'invalid tool name');
  if (!value.arguments || typeof value.arguments !== 'object' || Array.isArray(value.arguments)) throw new EnvelopeError('invalid_arguments', 'arguments must be an object');
  if (Object.keys(value.arguments).length > 32) throw new EnvelopeError('arguments_too_large', 'too many argument fields');
  return { id: value.id, name: value.name, arguments: structuredClone(value.arguments) };
}

export function parseToolCall(text, limits) { return validateToolCall(parseStrictJson(text, limits)); }

export function validateToolResult(value) {
  exactObject(value, ['id', 'name', 'status', 'content', 'metadata']);
  if (typeof value.id !== 'string' || !ID.test(value.id)) throw new EnvelopeError('invalid_call_id', 'invalid result id');
  if (typeof value.name !== 'string' || !NAME.test(value.name)) throw new EnvelopeError('invalid_tool_name', 'invalid result tool name');
  if (!['ok', 'denied', 'cancelled', 'failed'].includes(value.status)) throw new EnvelopeError('invalid_result_status', 'invalid result status');
  if (!Array.isArray(value.content) || value.content.length > 16) throw new EnvelopeError('invalid_result_content', 'invalid result content');
  for (const item of value.content) {
    exactObject(item, ['type', 'text']);
    if (item.type !== 'text' || typeof item.text !== 'string' || item.text.length > 65536) throw new EnvelopeError('invalid_result_content', 'invalid text result');
  }
  exactObject(value.metadata, ['truncated', 'duration_ms']);
  if (typeof value.metadata.truncated !== 'boolean' || !Number.isInteger(value.metadata.duration_ms) || value.metadata.duration_ms < 0) throw new EnvelopeError('invalid_result_metadata', 'invalid result metadata');
  return structuredClone(value);
}

export function makeToolResult({ id, name, status = 'ok', text = '', truncated = false, durationMs = 0 }) {
  return validateToolResult({ id, name, status, content: [{ type: 'text', text: String(text).slice(0, 65536) }], metadata: { truncated: Boolean(truncated), duration_ms: Math.max(0, Math.trunc(durationMs)) } });
}
