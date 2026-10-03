// Text hygiene and a tiny JSON Schema subset validator for the MCP bridge.
//
// WHY this module exists: the MCP caller (a cloud planner steered by repository files,
// issues and web pages) is untrusted, and the host's answers come from a small local model
// that may quote untrusted local files. Everything that crosses the bridge in either
// direction passes through one of the functions below so the rules live in one place.

// Bidi overrides/isolates and invisible formatters let an attacker hide or reorder
// instructions that a human approving the job on the laptop would never see. Unicode TAG
// characters (U+E0000..U+E007F) are the "ASCII smuggling" channel for invisible prompt
// injection. ZWJ/ZWNJ (U+200C/U+200D) are kept: emoji sequences and Persian/Indic text need them.
const BIDI_AND_INVISIBLE = /[؜​‎‏‪-‮⁠-⁤⁦-⁩﻿]|[\u{E0000}-\u{E007F}]/gu;
// C0 controls except TAB and LF (CR is normalised first), DEL and the C1 block.
const CONTROLS = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F]/g;

/** Normalise free text from either side: well-formed UTF-16, LF line ends, no controls/bidi/tags. */
export function cleanText(value) {
  return String(value).toWellFormed().replace(/\r\n?/g, '\n').replace(CONTROLS, '').replace(BIDI_AND_INVISIBLE, '');
}

/** Reduce host-supplied identifiers (phase, engine, error code) to a short, inert label. */
export function label(value, max = 64) {
  if (typeof value !== 'string') return '';
  return value.replace(/[^A-Za-z0-9 _.:()-]/g, '').replace(/\s+/g, ' ').trim().slice(0, max);
}

// Well-known credential shapes. Matching is deliberately narrow: generic "long random
// string" redaction would mangle hashes and IDs the operator legitimately asked about.
const TOKEN_SHAPES = [
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/g,
  /\bgithub_pat_[A-Za-z0-9_]{20,}\b/g,
  /\bsk-[A-Za-z0-9_-]{20,}\b/g,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
];
// The host's loopback port is an internal detail; reflecting it helps a local attacker.
const LOOPBACK_ENDPOINT = /\b(?:127\.0\.0\.1|localhost|\[::1\]):\d{1,5}\b/gi;
// Absolute paths reveal usernames and folder layout. Only error text is path-scrubbed:
// answers may legitimately cite files from operator-flagged folders.
const ABSOLUTE_PATH = /(?:\b[A-Za-z]:[\\/]|\\\\[^\s\\]+\\)[^\s"'<>|]*|(?<![\w.~])\/(?:Users|home|var|tmp|private|etc|opt|root|mnt|Volumes|usr|srv)\/[^\s"'<>]*/g;

/**
 * Prepare host- or model-originated text for the caller.
 * `secrets` (the delegate key) are removed verbatim before any shape-based redaction so a
 * key that does not look like a known token shape is still never reflected.
 */
export function scrubOutbound(value, { secrets = [], paths = false } = {}) {
  let text = cleanText(value);
  for (const secret of secrets) if (typeof secret === 'string' && secret.length >= 8) text = text.split(secret).join('[redacted]');
  for (const shape of TOKEN_SHAPES) text = text.replace(shape, '[redacted]');
  text = text.replace(LOOPBACK_ENDPOINT, '[bmo-host]');
  if (paths) text = text.replace(ABSOLUTE_PATH, '[path]');
  return text;
}

/** Truncate by code points so a surrogate pair is never split. */
export function truncateCodePoints(text, max) {
  const points = [...text];
  return points.length <= max ? { text, truncated: false } : { text: points.slice(0, max).join(''), truncated: true };
}

export function utf8Bytes(value) { return Buffer.byteLength(value, 'utf8'); }

/**
 * Validate `value` against the JSON Schema subset this bridge uses (type, enum, properties,
 * required, additionalProperties:false, min/maxLength, pattern, minimum/maximum).
 * Returns null when valid, else a short message naming the offending argument.
 *
 * WHY hand-rolled: zero runtime dependencies is a fixed project rule, and using the very
 * schema we advertise for validation keeps the advertised contract and the enforced one
 * identical. String bounds are applied conservatively: maxLength counts UTF-16 units (>= code
 * points) and minLength counts code points (<= units), so anything accepted here is also
 * accepted by a standards-conforming validator and by a host that counts `.length`.
 */
export function validateSchema(schema, value, where = 'value') {
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : null;
  if (types && !types.some(type => matchesType(type, value))) return `${where} must be ${types.join(' or ')}`;
  if (schema.enum && !schema.enum.some(option => option === value)) return `${where} must be one of: ${schema.enum.join(', ')}`;
  if (typeof value === 'string') {
    if (schema.maxLength !== undefined && value.length > schema.maxLength) return `${where} is longer than ${schema.maxLength} characters`;
    if (schema.minLength !== undefined && [...value].length < schema.minLength) return `${where} is shorter than ${schema.minLength} characters`;
    if (schema.pattern !== undefined && !new RegExp(schema.pattern, 'u').test(value)) return `${where} has an invalid format`;
  }
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) return `${where} must be >= ${schema.minimum}`;
    if (schema.maximum !== undefined && value > schema.maximum) return `${where} must be <= ${schema.maximum}`;
  }
  if (matchesType('object', value)) {
    for (const name of schema.required ?? []) if (!Object.hasOwn(value, name)) return `${name} is required`;
    for (const [name, child] of Object.entries(value)) {
      const childSchema = schema.properties?.[name];
      if (!childSchema) { if (schema.additionalProperties === false) return `unknown argument: ${label(name, 40) || '(unnamed)'}`; continue; }
      const problem = validateSchema(childSchema, child, name);
      if (problem) return problem;
    }
  }
  return null;
}

function matchesType(type, value) {
  switch (type) {
    case 'object': return value !== null && typeof value === 'object' && !Array.isArray(value);
    case 'array': return Array.isArray(value);
    case 'string': return typeof value === 'string';
    case 'integer': return Number.isInteger(value);
    case 'number': return typeof value === 'number' && Number.isFinite(value);
    case 'boolean': return typeof value === 'boolean';
    case 'null': return value === null;
    default: return false;
  }
}

export function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) { Object.freeze(value); for (const child of Object.values(value)) deepFreeze(child); }
  return value;
}
