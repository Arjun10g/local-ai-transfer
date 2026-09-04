import { createHash } from 'node:crypto';
import { makeToolResult } from '../agent/tool-envelope.mjs';

export class ProviderToolError extends Error {
  constructor(code, message = code) { super(message); this.name = 'ProviderToolError'; this.code = code; }
}

export const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

export function exactObject(value, allowed, required = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ProviderToolError('invalid_tool_arguments', 'arguments must be an object');
  for (const key of Object.keys(value)) if (!allowed.includes(key)) throw new ProviderToolError('invalid_tool_arguments', `unknown argument: ${key}`);
  for (const key of required) if (!own(value, key)) throw new ProviderToolError('invalid_tool_arguments', `missing argument: ${key}`);
  return value;
}

export function boundedString(value, name, { min = 0, max = 1024, identifier = false } = {}) {
  if (typeof value !== 'string' || value.length < min || value.length > max || /[\u0000-\u001f\u007f]/u.test(value)) throw new ProviderToolError('invalid_tool_arguments', `${name} must be a bounded string`);
  if (identifier && !value.trim()) throw new ProviderToolError('invalid_tool_arguments', `${name} must not be blank`);
  return value;
}

export function boundedInteger(value, name, min, max) {
  if (!Number.isInteger(value) || value < min || value > max) throw new ProviderToolError('invalid_tool_arguments', `${name} is out of range`);
  return value;
}

export function boundedBoolean(value, name) {
  if (typeof value !== 'boolean') throw new ProviderToolError('invalid_tool_arguments', `${name} must be boolean`);
  return value;
}

export function boundedArray(value, name, { max = 20, min = 0, item } = {}) {
  if (!Array.isArray(value) || value.length < min || value.length > max) throw new ProviderToolError('invalid_tool_arguments', `${name} has invalid length`);
  if (item) value.forEach((entry, index) => item(entry, `${name}[${index}]`));
  return value;
}

export function digest(value) { return createHash('sha256').update(JSON.stringify(value)).digest('hex'); }

export function checkAborted(signal) { if (signal?.aborted) throw new ProviderToolError('provider_cancelled', 'provider operation cancelled'); }

export function result(call, status, payload, { truncated = false } = {}) {
  const text = JSON.stringify(payload);
  if (Buffer.byteLength(text, 'utf8') > 65536) throw new ProviderToolError('provider_response_too_large');
  return makeToolResult({ id: call.id, name: call.name, status, text, truncated });
}

export function failureResult(call, error) {
  const code = error?.code ?? 'provider_failed';
  return result(call, 'failed', { code });
}

export function jsonResponse(response) {
  if (!response || typeof response !== 'object' || Array.isArray(response)) throw new ProviderToolError('provider_invalid_response');
  if (!Number.isInteger(response.status)) throw new ProviderToolError('provider_invalid_response');
  if (response.status === 204 || response.body === undefined || response.body === null) return {};
  if (typeof response.body !== 'object' || Array.isArray(response.body)) throw new ProviderToolError('provider_invalid_response');
  return response.body;
}

export function providerError(status) {
  if (status === 401) return 'provider_unauthorized';
  if (status === 403) return 'provider_permission_insufficient';
  if (status === 408) return 'provider_timeout';
  if (status === 429) return 'provider_rate_limited';
  if (status >= 500) return 'provider_failed';
  return 'provider_invalid_response';
}

export function normalizeText(value, maxBytes) {
  if (typeof value !== 'string') return { text: '', truncated: false };
  const decode = text => text.replace(/&(?:amp|lt|gt|quot|apos|nbsp);|&#(?:x[0-9a-f]+|[0-9]+);/giu, entity => {
    const named = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'", '&nbsp;': ' ' };
    if (named[entity.toLowerCase()]) return named[entity.toLowerCase()]; const hex = entity.match(/^&#x([0-9a-f]+);$/iu); const decimal = entity.match(/^&#([0-9]+);$/u); const code = hex ? Number.parseInt(hex[1], 16) : decimal ? Number.parseInt(decimal[1], 10) : NaN; return Number.isSafeInteger(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : ' ';
  });
  const decoded = decode(value);
  const normalized = decoded.replace(/<!--[\s\S]*?-->/gu, ' ').replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/giu, ' ').replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>/giu, ' ').replace(/<[^>]*>/gu, ' ').replace(/[ \t\r\f]+/gu, ' ').replace(/\n{3,}/gu, '\n\n').trim();
  const bytes = Buffer.from(normalized, 'utf8');
  if (bytes.length <= maxBytes) return { text: normalized, truncated: false };
  let text = new TextDecoder().decode(bytes.subarray(0, maxBytes)); while (text && text.endsWith('\uFFFD')) text = text.slice(0, -1); return { text, truncated: true };
}

export function safeArray(value, max = 256) { return Array.isArray(value) ? value.slice(0, max) : []; }
