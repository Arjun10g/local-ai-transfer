import { createHmac, timingSafeEqual } from 'node:crypto';
import { TextDecoder } from 'node:util';
import { parseStrictJson } from './tool-envelope.mjs';

/**
 * Source-only wire contract for a future foreground native journal helper.
 * Nothing in the production host imports this module. The future transport,
 * durable store, launcher, and trust anchor remain deliberately absent.
 */
export const ACTION_JOURNAL_PROTOCOL = 'lae.action-journal.v0.1.0';
export const ACTION_JOURNAL_VERSION = 1;
export const PRODUCTION_ACTION_JOURNAL_PROTOCOL_AVAILABLE = false;

export const ACTION_JOURNAL_LIMITS = Object.freeze({
  max_frame_bytes: 64 * 1024,
  max_payload_bytes: 64 * 1024 - 4,
  max_buffered_frames: 8,
  max_session_frames: 1024,
  max_records: 1024,
  max_string_bytes: 2048,
  max_object_fields: 24,
  max_array_items: 128,
  max_summary_records: 128,
  max_detail_events: 32,
  max_events_per_operation: 32,
  max_deadline_span_ms: 10 * 60 * 1000,
  max_clock_skew_ms: 30 * 1000,
  max_unix_ms: 4102444800000,
});

export const ACTION_JOURNAL_METHODS = Object.freeze([
  'health', 'prepare', 'authorize', 'dispatch', 'acknowledge',
  'begin_reconciliation', 'complete', 'cancel', 'fail_definitive',
  'mark_unknown', 'summary', 'detail',
]);
export const ACTION_JOURNAL_STATES = Object.freeze([
  'prepared', 'authorized', 'dispatching', 'acknowledged', 'reconciling',
  'completed', 'cancelled', 'failed_definitive', 'unknown_manual',
]);
export const ACTION_JOURNAL_AUTHORIZATIONS = Object.freeze([
  'policy', 'user_confirmation', 'operator_grant',
]);
export const ACTION_JOURNAL_RISK_TIERS = Object.freeze(['T0', 'T1', 'T2', 'T3', 'T4']);
export const ACTION_JOURNAL_SIDE_EFFECTS = Object.freeze([
  'browser_activation', 'browser_close', 'browser_input', 'browser_navigation',
  'browser_read', 'cloud_inference', 'create', 'external_navigation', 'launch',
  'none', 'process_execution', 'read_sensitive', 'replace', 'write_sensitive',
]);
export const ACTION_JOURNAL_RESOLUTIONS = Object.freeze([
  'user_denied', 'request_cancelled', 'pre_dispatch_failure',
  'dispatch_ambiguous', 'provider_acknowledged', 'completed',
  'startup_recovery', 'manual_completed', 'manual_failed_definitive',
]);
export const ACTION_JOURNAL_RECONCILIATION_REASONS = Object.freeze([
  'lost_ack', 'provider_timeout', 'transport_closed', 'startup_recovery',
  'postcondition_pending',
]);
export const ACTION_JOURNAL_ERROR_RETRYABILITY = Object.freeze({
  invalid_frame: false,
  invalid_utf8: false,
  invalid_json: false,
  duplicate_key: false,
  noncanonical_json: false,
  unknown_field: false,
  invalid_request: false,
  invalid_mac: false,
  replay: false,
  sequence_out_of_order: false,
  nonce_mismatch: false,
  deadline_expired: false,
  queue_full: true,
  not_found: false,
  invalid_transition: false,
  commit_non_cancellable: false,
  platform_unavailable: false,
  record_limit_exceeded: false,
  internal: false,
});
export const ACTION_JOURNAL_ERRORS = Object.freeze(Object.keys(ACTION_JOURNAL_ERROR_RETRYABILITY));

const REQUEST_FIELDS = Object.freeze([
  'body', 'deadline_at_ms', 'issued_at_ms', 'kind', 'mac', 'method', 'nonce',
  'operation_id', 'request_id', 'sequence', 'version',
]);
const RESPONSE_FIELDS = Object.freeze([
  'body', 'error', 'kind', 'mac', 'method', 'nonce', 'operation_id',
  'request_id', 'sequence', 'state', 'version',
]);
const REQUEST_ID = /^req_[a-f0-9]{32}$/u;
const OPERATION_ID = /^act_[a-f0-9]{32}$/u;
const NONCE = /^[a-f0-9]{32}$/u;
const DIGEST = /^[a-f0-9]{64}$/u;
const TOOL_NAME = /^[a-z][a-z0-9_.-]{1,95}$/u;
const METHODS = new Set(ACTION_JOURNAL_METHODS);
const STATES = new Set(ACTION_JOURNAL_STATES);
const AUTHORIZATIONS = new Set(ACTION_JOURNAL_AUTHORIZATIONS);
const RISKS = new Set(ACTION_JOURNAL_RISK_TIERS);
const SIDE_EFFECTS = new Set(ACTION_JOURNAL_SIDE_EFFECTS);
const RESOLUTIONS = new Set(ACTION_JOURNAL_RESOLUTIONS);
const RECONCILIATION_REASONS = new Set(ACTION_JOURNAL_RECONCILIATION_REASONS);
const ERRORS = new Set(ACTION_JOURNAL_ERRORS);
const UTF8_FATAL = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });

const TRANSITIONS = Object.freeze({
  authorize: Object.freeze({ from: ['prepared'], to: 'authorized' }),
  dispatch: Object.freeze({ from: ['authorized'], to: 'dispatching' }),
  acknowledge: Object.freeze({ from: ['dispatching'], to: 'acknowledged' }),
  begin_reconciliation: Object.freeze({ from: ['dispatching', 'acknowledged'], to: 'reconciling' }),
  complete: Object.freeze({ from: ['acknowledged', 'reconciling', 'unknown_manual'], to: 'completed' }),
  cancel: Object.freeze({ from: ['prepared', 'authorized'], to: 'cancelled' }),
  fail_definitive: Object.freeze({ from: ['prepared', 'authorized', 'reconciling', 'unknown_manual'], to: 'failed_definitive' }),
  mark_unknown: Object.freeze({ from: ['dispatching', 'acknowledged', 'reconciling'], to: 'unknown_manual' }),
});
const SUCCESS_STATES = Object.freeze({
  prepare: 'prepared', authorize: 'authorized', dispatch: 'dispatching',
  acknowledge: 'acknowledged', begin_reconciliation: 'reconciling',
  complete: 'completed', cancel: 'cancelled',
  fail_definitive: 'failed_definitive', mark_unknown: 'unknown_manual',
});

export class ActionJournalProtocolError extends Error {
  constructor(code) {
    super(code);
    this.name = 'ActionJournalProtocolError';
    this.code = code;
  }
}

function fail(code) { throw new ActionJournalProtocolError(code); }
function plainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === null || prototype === Object.prototype;
}
function exactKeys(value, allowed) {
  if (!plainObject(value)) fail('invalid_request');
  if (Object.keys(value).length !== allowed.length) {
    const unknown = Object.keys(value).some(key => !allowed.includes(key));
    fail(unknown ? 'unknown_field' : 'invalid_request');
  }
  for (const key of allowed) if (!Object.hasOwn(value, key)) fail('invalid_request');
}
function hasUnpairedSurrogate(value) {
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return true;
      index++;
    } else if (code >= 0xdc00 && code <= 0xdfff) return true;
  }
  return false;
}
function boundedString(value, { pattern, bytes = ACTION_JOURNAL_LIMITS.max_string_bytes, empty = false } = {}) {
  if (
    typeof value !== 'string'
    || (!empty && value.length === 0)
    || hasUnpairedSurrogate(value)
    || /[\u0000-\u001f\u007f]/u.test(value)
    || Buffer.byteLength(value, 'utf8') > bytes
    || (pattern && !pattern.test(value))
  ) fail('invalid_request');
  return value;
}
function integer(value, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) fail('invalid_request');
  return value;
}
function requestId(value) { return boundedString(value, { pattern: REQUEST_ID, bytes: 36 }); }
function operationId(value) { return boundedString(value, { pattern: OPERATION_ID, bytes: 36 }); }
function nonce(value) { return boundedString(value, { pattern: NONCE, bytes: 32 }); }
function digest(value) { return boundedString(value, { pattern: DIGEST, bytes: 64 }); }
function nullable(value, validator) { if (value !== null) validator(value); return value; }
function enumValue(value, values) { if (!values.has(value)) fail('invalid_request'); return value; }
function keyBytes(key) {
  if (!(key instanceof Uint8Array) || key.byteLength !== 32) fail('invalid_request');
  return Buffer.from(key);
}

function canonicalize(value, depth, counter) {
  if (depth > 16 || ++counter.nodes > 4096) fail('invalid_request');
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || Object.is(value, -0)) fail('invalid_request');
    return JSON.stringify(value);
  }
  if (typeof value === 'string') {
    boundedString(value, { bytes: ACTION_JOURNAL_LIMITS.max_string_bytes, empty: true });
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    if (value.length > ACTION_JOURNAL_LIMITS.max_array_items) fail('invalid_request');
    return `[${value.map(item => canonicalize(item, depth + 1, counter)).join(',')}]`;
  }
  if (!plainObject(value)) fail('invalid_request');
  const keys = Object.keys(value).sort();
  if (keys.length > ACTION_JOURNAL_LIMITS.max_object_fields) fail('invalid_request');
  return `{${keys.map(key => {
    boundedString(key, { bytes: 128, empty: true });
    return `${JSON.stringify(key)}:${canonicalize(value[key], depth + 1, counter)}`;
  }).join(',')}}`;
}

/** RFC-8259 JSON subset with sorted keys, safe integers, and no whitespace. */
export function canonicalJson(value) {
  const encoded = canonicalize(value, 0, { nodes: 0 });
  if (Buffer.byteLength(encoded, 'utf8') > ACTION_JOURNAL_LIMITS.max_payload_bytes) fail('invalid_request');
  return encoded;
}

function validatePrepareBody(body) {
  exactKeys(body, [
    'arguments_digest', 'call_ref', 'operation_digest', 'preview_digest',
    'request_ref', 'risk_tier', 'side_effect', 'tool_name',
  ]);
  for (const field of ['arguments_digest', 'call_ref', 'operation_digest', 'preview_digest', 'request_ref']) digest(body[field]);
  boundedString(body.tool_name, { pattern: TOOL_NAME, bytes: 96 });
  enumValue(body.risk_tier, RISKS);
  enumValue(body.side_effect, SIDE_EFFECTS);
}

function validateRequestBody(method, body, operation) {
  if (!plainObject(body)) fail('invalid_request');
  if (['health', 'prepare', 'summary'].includes(method) && operation !== null) fail('invalid_request');
  if (!['health', 'prepare', 'summary'].includes(method) && operation === null) fail('invalid_request');
  switch (method) {
    case 'health': exactKeys(body, []); break;
    case 'prepare': validatePrepareBody(body); break;
    case 'authorize':
      exactKeys(body, ['authorization_kind']);
      enumValue(body.authorization_kind, AUTHORIZATIONS);
      break;
    case 'dispatch': exactKeys(body, []); break;
    case 'acknowledge':
      exactKeys(body, ['provider_receipt_digest']);
      digest(body.provider_receipt_digest);
      break;
    case 'begin_reconciliation':
      exactKeys(body, ['reason']);
      enumValue(body.reason, RECONCILIATION_REASONS);
      break;
    case 'complete':
      exactKeys(body, ['receipt_digest', 'resolution']);
      digest(body.receipt_digest);
      if (!['completed', 'manual_completed'].includes(body.resolution)) fail('invalid_request');
      break;
    case 'cancel':
      exactKeys(body, ['resolution']);
      if (!['user_denied', 'request_cancelled'].includes(body.resolution)) fail('invalid_request');
      break;
    case 'fail_definitive':
      exactKeys(body, ['resolution']);
      if (!['pre_dispatch_failure', 'manual_failed_definitive'].includes(body.resolution)) fail('invalid_request');
      break;
    case 'mark_unknown':
      exactKeys(body, ['resolution']);
      if (body.resolution !== 'dispatch_ambiguous') fail('invalid_request');
      break;
    case 'summary':
      exactKeys(body, ['cursor', 'include_terminal', 'limit']);
      nullable(body.cursor, operationId);
      if (typeof body.include_terminal !== 'boolean') fail('invalid_request');
      integer(body.limit, 1, ACTION_JOURNAL_LIMITS.max_summary_records);
      break;
    case 'detail':
      exactKeys(body, ['after_sequence', 'limit']);
      nullable(body.after_sequence, value => integer(value, 0, ACTION_JOURNAL_LIMITS.max_events_per_operation - 1));
      integer(body.limit, 1, ACTION_JOURNAL_LIMITS.max_detail_events);
      break;
    default: fail('invalid_request');
  }
}

function validateReceipt(value) {
  exactKeys(value, [
    'authorization_kind', 'operation_id', 'receipt_digest', 'recovery_required',
    'redacted', 'resolution', 'sequence', 'state',
  ]);
  if (value.redacted !== true || typeof value.recovery_required !== 'boolean') fail('invalid_request');
  operationId(value.operation_id);
  digest(value.receipt_digest);
  enumValue(value.state, STATES);
  integer(value.sequence, 0, ACTION_JOURNAL_LIMITS.max_events_per_operation - 1);
  nullable(value.authorization_kind, candidate => enumValue(candidate, AUTHORIZATIONS));
  nullable(value.resolution, candidate => enumValue(candidate, RESOLUTIONS));
  const needsRecovery = ['reconciling', 'unknown_manual'].includes(value.state);
  if (value.recovery_required !== needsRecovery) fail('invalid_request');
  const authorizationRequired = ['authorized', 'dispatching', 'acknowledged', 'reconciling', 'completed', 'unknown_manual'].includes(value.state);
  if (authorizationRequired && value.authorization_kind === null) fail('invalid_request');
  const allowedResolutions = {
    prepared: [null], authorized: [null], dispatching: [null],
    acknowledged: ['provider_acknowledged'], reconciling: [null],
    completed: ['completed', 'manual_completed'],
    cancelled: ['user_denied', 'request_cancelled', 'startup_recovery'],
    failed_definitive: ['pre_dispatch_failure', 'manual_failed_definitive'],
    unknown_manual: ['dispatch_ambiguous'],
  };
  if (!allowedResolutions[value.state].includes(value.resolution)) fail('invalid_request');
  return value;
}

function validateError(value) {
  if (value === null) return;
  exactKeys(value, ['code', 'retryable']);
  enumValue(value.code, ERRORS);
  if (typeof value.retryable !== 'boolean' || value.retryable !== ACTION_JOURNAL_ERROR_RETRYABILITY[value.code]) fail('invalid_request');
}

function validateEvent(value) {
  exactKeys(value, ['authorization_kind', 'receipt_digest', 'resolution', 'sequence', 'state']);
  integer(value.sequence, 0, ACTION_JOURNAL_LIMITS.max_events_per_operation - 1);
  enumValue(value.state, STATES);
  digest(value.receipt_digest);
  nullable(value.authorization_kind, candidate => enumValue(candidate, AUTHORIZATIONS));
  nullable(value.resolution, candidate => enumValue(candidate, RESOLUTIONS));
  validateReceipt({
    ...value,
    operation_id: 'act_00000000000000000000000000000000',
    recovery_required: ['reconciling', 'unknown_manual'].includes(value.state),
    redacted: true,
  });
  return value;
}

function validateResponseBody(envelope) {
  const { body, error, method, operation_id: operation, state } = envelope;
  if (!plainObject(body)) fail('invalid_request');
  if (error !== null) {
    exactKeys(body, []);
    if (state !== null) fail('invalid_request');
    if (['health', 'summary', 'prepare'].includes(method)) {
      if (operation !== null) fail('invalid_request');
    } else if (operation === null) fail('invalid_request');
    return;
  }
  if (method === 'health') {
    if (operation !== null || state !== null) fail('invalid_request');
    exactKeys(body, ['platform_available', 'production_enabled', 'recovery_count', 'status']);
    if (
      typeof body.platform_available !== 'boolean'
      || body.production_enabled !== false
      || !['ready', 'recovering', 'unavailable'].includes(body.status)
    ) fail('invalid_request');
    integer(body.recovery_count, 0, ACTION_JOURNAL_LIMITS.max_records);
    return;
  }
  if (method === 'summary') {
    if (operation !== null || state !== null) fail('invalid_request');
    exactKeys(body, ['next_cursor', 'records', 'truncated']);
    nullable(body.next_cursor, operationId);
    if (!Array.isArray(body.records) || body.records.length > ACTION_JOURNAL_LIMITS.max_summary_records || typeof body.truncated !== 'boolean') fail('invalid_request');
    for (const receipt of body.records) validateReceipt(receipt);
    if (body.truncated !== (body.next_cursor !== null)) fail('invalid_request');
    return;
  }
  if (operation === null) fail('invalid_request');
  if (method === 'detail') {
    exactKeys(body, ['events', 'next_sequence', 'receipt', 'truncated']);
    if (!Array.isArray(body.events) || body.events.length > ACTION_JOURNAL_LIMITS.max_detail_events || typeof body.truncated !== 'boolean') fail('invalid_request');
    nullable(body.next_sequence, value => integer(value, 0, ACTION_JOURNAL_LIMITS.max_events_per_operation - 1));
    if (body.truncated !== (body.next_sequence !== null)) fail('invalid_request');
    const receipt = validateReceipt(body.receipt);
    if (receipt.operation_id !== operation || state !== receipt.state) fail('invalid_request');
    let previous = null;
    for (const event of body.events) {
      validateEvent(event);
      if (previous !== null && event.sequence !== previous + 1) fail('invalid_request');
      previous = event.sequence;
    }
    if (body.events.length > 0 && body.events.at(-1).state !== receipt.state) fail('invalid_request');
    return;
  }
  exactKeys(body, ['receipt']);
  const receipt = validateReceipt(body.receipt);
  if (receipt.operation_id !== operation || state !== receipt.state || SUCCESS_STATES[method] !== state) fail('invalid_request');
}

function validateReceiptForRequest(receipt, request) {
  const { body, method } = request;
  switch (method) {
    case 'prepare':
      if (receipt.authorization_kind !== null || receipt.resolution !== null) fail('invalid_request');
      break;
    case 'authorize':
      if (receipt.authorization_kind !== body.authorization_kind || receipt.resolution !== null) fail('invalid_request');
      break;
    case 'dispatch':
      if (receipt.authorization_kind === null || receipt.resolution !== null) fail('invalid_request');
      break;
    case 'acknowledge':
      if (receipt.authorization_kind === null || receipt.resolution !== 'provider_acknowledged') fail('invalid_request');
      break;
    case 'begin_reconciliation':
      if (receipt.authorization_kind === null || receipt.resolution !== null) fail('invalid_request');
      break;
    case 'complete':
    case 'cancel':
    case 'fail_definitive':
    case 'mark_unknown':
      if (receipt.resolution !== body.resolution) fail('invalid_request');
      break;
    default:
      break;
  }
}

function validateResponseAgainstRequest(response, request, nowMs) {
  if (!plainObject(request) || request.kind !== 'request') fail('invalid_request');
  validateEnvelope(request, 'request');
  if (response.request_id !== request.request_id || response.method !== request.method) fail('invalid_request');
  if (nowMs > request.deadline_at_ms) fail('deadline_expired');

  if (request.method === 'prepare') {
    if (response.error === null) {
      if (response.operation_id === null) fail('invalid_request');
    } else if (response.operation_id !== null) fail('invalid_request');
  } else if (response.operation_id !== request.operation_id) fail('invalid_request');

  if (response.error !== null) return;
  if (Object.hasOwn(response.body, 'receipt')) validateReceiptForRequest(response.body.receipt, request);

  if (request.method === 'summary') {
    if (response.body.records.length > request.body.limit) fail('invalid_request');
    let previous = request.body.cursor;
    for (const receipt of response.body.records) {
      if (previous !== null && receipt.operation_id <= previous) fail('invalid_request');
      previous = receipt.operation_id;
    }
    if (response.body.truncated) {
      if (response.body.records.length === 0 || response.body.next_cursor !== response.body.records.at(-1).operation_id) fail('invalid_request');
    }
  }
  if (request.method === 'detail') {
    if (response.body.events.length > request.body.limit) fail('invalid_request');
    const firstExpected = request.body.after_sequence === null ? 0 : request.body.after_sequence + 1;
    if (response.body.events.length > 0 && response.body.events[0].sequence !== firstExpected) fail('invalid_request');
    if (response.body.truncated) {
      if (response.body.events.length === 0 || response.body.next_sequence !== response.body.events.at(-1).sequence) fail('invalid_request');
    }
  }
}

function validateEnvelope(value, expectedKind) {
  if (!plainObject(value)) fail('invalid_request');
  const kind = value.kind;
  if (kind !== 'request' && kind !== 'response') fail('invalid_request');
  if (expectedKind !== undefined && kind !== expectedKind) fail('invalid_request');
  exactKeys(value, kind === 'request' ? REQUEST_FIELDS : RESPONSE_FIELDS);
  if (value.version !== ACTION_JOURNAL_VERSION) fail('invalid_request');
  requestId(value.request_id);
  nonce(value.nonce);
  integer(value.sequence, 0, 0x7fffffff);
  enumValue(value.method, METHODS);
  nullable(value.operation_id, operationId);
  boundedString(value.mac, { pattern: DIGEST, bytes: 64 });
  if (kind === 'request') {
    integer(value.issued_at_ms, 0, ACTION_JOURNAL_LIMITS.max_unix_ms);
    integer(value.deadline_at_ms, 1, ACTION_JOURNAL_LIMITS.max_unix_ms);
    if (
      value.deadline_at_ms <= value.issued_at_ms
      || value.deadline_at_ms - value.issued_at_ms > ACTION_JOURNAL_LIMITS.max_deadline_span_ms
    ) fail('invalid_request');
    validateRequestBody(value.method, value.body, value.operation_id);
  } else {
    nullable(value.state, candidate => enumValue(candidate, STATES));
    validateError(value.error);
    validateResponseBody(value);
  }
  return value;
}

function withoutMac(value) {
  const copy = Object.create(null);
  for (const key of Object.keys(value)) if (key !== 'mac') copy[key] = value[key];
  return copy;
}

export function macForEnvelope(value, key) {
  const bytes = keyBytes(key);
  return createHmac('sha256', bytes)
    .update(`${ACTION_JOURNAL_PROTOCOL}\0${canonicalJson(withoutMac(value))}`, 'utf8')
    .digest('hex');
}

function verifyMac(value, key) {
  const expected = Buffer.from(macForEnvelope(value, key), 'ascii');
  const actual = Buffer.from(value.mac, 'ascii');
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) fail('invalid_mac');
}

export function encodeEnvelope(envelope, key) {
  if (!plainObject(envelope)) fail('invalid_request');
  const unsigned = withoutMac(envelope);
  const signed = Object.assign(Object.create(null), unsigned, { mac: macForEnvelope(unsigned, key) });
  validateEnvelope(signed);
  const payload = Buffer.from(canonicalJson(signed), 'utf8');
  if (payload.length < 2 || payload.length > ACTION_JOURNAL_LIMITS.max_payload_bytes) fail('invalid_frame');
  const frame = Buffer.allocUnsafe(payload.length + 4);
  frame.writeUInt32BE(payload.length, 0);
  payload.copy(frame, 4);
  return frame;
}

export function decodeFrame(frame, key, {
  expectedKind,
  expectedNonce,
  expectedSequence,
  seenRequestIds,
  expectedResponse,
  nowMs = Date.now(),
} = {}) {
  keyBytes(key);
  if (!['request', 'response'].includes(expectedKind)) fail('invalid_request');
  nonce(expectedNonce);
  integer(expectedSequence, 0, 0x7fffffff);
  if (!(seenRequestIds instanceof Set) || seenRequestIds.size > ACTION_JOURNAL_LIMITS.max_session_frames) fail('invalid_request');
  integer(nowMs, 0, ACTION_JOURNAL_LIMITS.max_unix_ms);
  if (!Buffer.isBuffer(frame) || frame.length < 6 || frame.length > ACTION_JOURNAL_LIMITS.max_frame_bytes) fail('invalid_frame');
  const payloadLength = frame.readUInt32BE(0);
  if (payloadLength < 2 || payloadLength > ACTION_JOURNAL_LIMITS.max_payload_bytes || payloadLength !== frame.length - 4) fail('invalid_frame');
  let text;
  try { text = UTF8_FATAL.decode(frame.subarray(4)); } catch { fail('invalid_utf8'); }
  let parsed;
  try {
    parsed = parseStrictJson(text, {
      maxBytes: ACTION_JOURNAL_LIMITS.max_payload_bytes,
      maxDepth: 16,
      maxString: ACTION_JOURNAL_LIMITS.max_string_bytes,
      maxArray: ACTION_JOURNAL_LIMITS.max_array_items,
      maxObject: ACTION_JOURNAL_LIMITS.max_object_fields,
    });
  } catch (error) {
    fail(error?.code === 'duplicate_json_key' ? 'duplicate_key' : 'invalid_json');
  }
  if (!plainObject(parsed)) fail('invalid_json');
  let canonical;
  try { canonical = canonicalJson(parsed); } catch { fail('invalid_json'); }
  if (canonical !== text) fail('noncanonical_json');
  validateEnvelope(parsed, expectedKind);
  verifyMac(parsed, key);
  if (parsed.nonce !== expectedNonce) fail('nonce_mismatch');
  if (parsed.sequence !== expectedSequence) fail(parsed.sequence < expectedSequence ? 'replay' : 'sequence_out_of_order');
  if (seenRequestIds.has(parsed.request_id)) fail('replay');
  if (seenRequestIds.size >= ACTION_JOURNAL_LIMITS.max_session_frames) fail('record_limit_exceeded');
  if (parsed.kind === 'request') {
    if (parsed.issued_at_ms > nowMs + ACTION_JOURNAL_LIMITS.max_clock_skew_ms) fail('invalid_request');
    if (parsed.deadline_at_ms < nowMs) fail('deadline_expired');
  } else {
    if (expectedResponse === undefined) fail('invalid_request');
    validateResponseAgainstRequest(parsed, expectedResponse, nowMs);
  }
  seenRequestIds.add(parsed.request_id);
  return structuredClone(parsed);
}

/** Incremental framing only. Authentication and replay checks happen later. */
export class ActionJournalFrameDecoder {
  constructor() { this.buffer = Buffer.alloc(0); this.frames = 0; }
  push(chunk) {
    if (!(chunk instanceof Uint8Array)) fail('invalid_frame');
    const bytes = Buffer.from(chunk);
    const maximum = ACTION_JOURNAL_LIMITS.max_frame_bytes * ACTION_JOURNAL_LIMITS.max_buffered_frames;
    if (this.buffer.length + bytes.length > maximum) fail('queue_full');
    this.buffer = Buffer.concat([this.buffer, bytes]);
    const frames = [];
    while (this.buffer.length >= 4) {
      const payloadLength = this.buffer.readUInt32BE(0);
      if (payloadLength < 2 || payloadLength > ACTION_JOURNAL_LIMITS.max_payload_bytes) fail('invalid_frame');
      const frameLength = payloadLength + 4;
      if (this.buffer.length < frameLength) break;
      frames.push(Buffer.from(this.buffer.subarray(0, frameLength)));
      this.buffer = this.buffer.subarray(frameLength);
      this.frames++;
      if (frames.length > ACTION_JOURNAL_LIMITS.max_buffered_frames || this.frames > ACTION_JOURNAL_LIMITS.max_session_frames) fail('record_limit_exceeded');
    }
    return frames;
  }
  finish() { if (this.buffer.length !== 0) fail('invalid_frame'); }
}

export class ActionJournalProtocolReceiver {
  constructor({ key, kind, nonce: sessionNonce, now = () => Date.now(), startSequence = 0 } = {}) {
    this.key = keyBytes(key);
    if (!['request', 'response'].includes(kind)) fail('invalid_request');
    this.kind = kind;
    this.nonce = nonce(sessionNonce);
    if (typeof now !== 'function') fail('invalid_request');
    this.now = now;
    this.expectedSequence = integer(startSequence, 0, 0x7fffffff);
    this.seenRequestIds = new Set();
    this.pendingResponses = [];
    this.pendingRequestIds = new Set();
  }
  expectResponse(request) {
    if (this.kind !== 'response' || this.pendingResponses.length >= ACTION_JOURNAL_LIMITS.max_buffered_frames) fail('queue_full');
    validateEnvelope(request, 'request');
    if (this.pendingRequestIds.has(request.request_id) || this.seenRequestIds.has(request.request_id)) fail('replay');
    this.pendingResponses.push(structuredClone(request));
    this.pendingRequestIds.add(request.request_id);
  }
  receive(frame) {
    const expectedResponse = this.kind === 'response' ? this.pendingResponses[0] : undefined;
    if (this.kind === 'response' && expectedResponse === undefined) fail('invalid_request');
    const value = decodeFrame(frame, this.key, {
      expectedKind: this.kind,
      expectedNonce: this.nonce,
      expectedSequence: this.expectedSequence,
      seenRequestIds: this.seenRequestIds,
      expectedResponse,
      nowMs: this.now(),
    });
    if (this.kind === 'response') {
      this.pendingResponses.shift();
      this.pendingRequestIds.delete(expectedResponse.request_id);
    }
    this.expectedSequence++;
    return value;
  }
}

export function transitionState(currentState, method) {
  if (method === 'prepare') {
    if (currentState !== null) fail('invalid_transition');
    return 'prepared';
  }
  const transition = TRANSITIONS[method];
  if (!transition || !transition.from.includes(currentState)) fail('invalid_transition');
  return transition.to;
}

export function startupRecovery(state) {
  enumValue(state, STATES);
  if (['prepared', 'authorized'].includes(state)) return Object.freeze({ state: 'cancelled', resolution: 'startup_recovery' });
  if (['dispatching', 'acknowledged', 'reconciling'].includes(state)) return Object.freeze({ state: 'unknown_manual', resolution: 'dispatch_ambiguous' });
  return Object.freeze({ state, resolution: null });
}

export function redactedReceipt({
  operationId: operation,
  state,
  sequence,
  receiptDigest,
  authorizationKind = null,
  resolution = null,
} = {}) {
  const receipt = {
    authorization_kind: authorizationKind,
    operation_id: operation,
    receipt_digest: receiptDigest,
    recovery_required: ['reconciling', 'unknown_manual'].includes(state),
    redacted: true,
    resolution,
    sequence,
    state,
  };
  validateReceipt(receipt);
  return Object.freeze(receipt);
}

export function protocolError(code) {
  enumValue(code, ERRORS);
  return Object.freeze({ code, retryable: ACTION_JOURNAL_ERROR_RETRYABILITY[code] });
}

export function buildRequest({
  requestId: request,
  sequence = 0,
  nonce: sessionNonce,
  issuedAtMs,
  deadlineAtMs,
  method,
  operationId: operation = null,
  body = Object.create(null),
} = {}) {
  return {
    body,
    deadline_at_ms: deadlineAtMs,
    issued_at_ms: issuedAtMs,
    kind: 'request',
    mac: '0'.repeat(64),
    method,
    nonce: sessionNonce,
    operation_id: operation,
    request_id: request,
    sequence,
    version: ACTION_JOURNAL_VERSION,
  };
}

export function buildResponse({
  requestId: request,
  sequence = 0,
  nonce: sessionNonce,
  method,
  operationId: operation = null,
  state = null,
  body = Object.create(null),
  error = null,
} = {}) {
  return {
    body,
    error,
    kind: 'response',
    mac: '0'.repeat(64),
    method,
    nonce: sessionNonce,
    operation_id: operation,
    request_id: request,
    sequence,
    state,
    version: ACTION_JOURNAL_VERSION,
  };
}
