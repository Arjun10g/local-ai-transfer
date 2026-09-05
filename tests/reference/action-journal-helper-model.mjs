import { createHash, createHmac } from 'node:crypto';

import {
  ACTION_JOURNAL_LIMITS,
  ActionJournalProtocolReceiver,
  buildResponse,
  canonicalJson,
  encodeEnvelope,
  journalEventDigest,
  protocolError,
  redactedReceipt,
  startupRecovery,
  transitionState,
} from '../../host/agent/action-journal-protocol.mjs';
import {
  ActionJournalContainerReference,
  InMemoryJournalBlockDevice,
} from './action-journal-container-model.mjs';

export const ACTION_JOURNAL_HELPER_REFERENCE_ONLY = true;
export const ACTION_JOURNAL_HELPER_PRODUCTION_AVAILABLE = false;

const TERMINAL = new Set(['completed', 'cancelled', 'failed_definitive']);
const DIGEST = /^[a-f0-9]{64}$/u;
const IDENTITY_FIELDS = Object.freeze([
  'creation_time', 'image_file_id', 'image_path', 'image_volume_serial',
  'pid', 'session_id', 'user_sid',
]);

export class ActionJournalHelperModelError extends Error {
  constructor(code) { super(code); this.name = 'ActionJournalHelperModelError'; this.code = code; }
}

function fail(code) { throw new ActionJournalHelperModelError(code); }
function clone(value) { return structuredClone(value); }
function sha256(...parts) {
  const hash = createHash('sha256');
  for (const part of parts) hash.update(part);
  return hash.digest('hex');
}
function exactIdentity(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail('client_identity_mismatch');
  if (Object.keys(value).sort().join('\0') !== IDENTITY_FIELDS.join('\0')) fail('client_identity_mismatch');
  if (!Number.isSafeInteger(value.pid) || value.pid < 1 || value.pid > 0xffffffff) fail('client_identity_mismatch');
  if (!Number.isSafeInteger(value.session_id) || value.session_id < 0 || value.session_id > 0xffffffff) fail('client_identity_mismatch');
  if (typeof value.creation_time !== 'string' || !/^[0-9]{1,20}$/u.test(value.creation_time)) fail('client_identity_mismatch');
  if (typeof value.user_sid !== 'string' || !/^S-1-[0-9-]{3,180}$/u.test(value.user_sid)) fail('client_identity_mismatch');
  if (typeof value.image_path !== 'string' || !/^[A-Z]:\\[^\u0000-\u001f\u007f:]{1,1024}$/u.test(value.image_path)) fail('client_identity_mismatch');
  if (typeof value.image_volume_serial !== 'string' || !/^[a-f0-9]{16}$/u.test(value.image_volume_serial)) fail('client_identity_mismatch');
  if (typeof value.image_file_id !== 'string' || !/^[a-f0-9]{32}$/u.test(value.image_file_id)) fail('client_identity_mismatch');
  return Object.freeze(clone(value));
}
function sameIdentity(left, right) {
  return IDENTITY_FIELDS.every(field => left[field] === right[field]);
}
function keyBytes(value) {
  if (!(value instanceof Uint8Array) || value.byteLength !== 32) fail('bootstrap_invalid');
  return Buffer.from(value);
}
function nonceValue(value) {
  if (typeof value !== 'string' || !/^[a-f0-9]{32}$/u.test(value)) fail('bootstrap_invalid');
  return value;
}
function operationIdFrom(containerId, operationDigest) {
  if (typeof operationDigest !== 'string' || !DIGEST.test(operationDigest)) fail('invalid_transition');
  return `act_${createHmac('sha256', containerId).update(`operation\0${operationDigest}`, 'utf8').digest('hex').slice(0, 32)}`;
}
function bindingDigest(body) {
  return sha256('lae.action-journal.helper.v0.1.0\0prepare-binding\0', canonicalJson(body));
}
function nextReceiptDigest(operationId, previous, method, body, state, authorization, resolution) {
  return sha256(
    'lae.action-journal.helper.v0.1.0\0receipt\0', operationId, '\0',
    previous?.receipt_digest ?? '', '\0', method, '\0', canonicalJson(body), '\0',
    state, '\0', authorization ?? '', '\0', resolution ?? '',
  );
}
function receiptFrom(operationId, event) {
  return redactedReceipt({
    operationId,
    state: event.state,
    sequence: event.sequence,
    receiptDigest: event.receipt_digest,
    authorizationKind: event.authorization_kind,
    resolution: event.resolution,
  });
}
function eventFor(operationId, previous, action, body, state, authorization, resolution) {
  return Object.freeze({
    action,
    authorization_kind: authorization,
    receipt_digest: action === 'prepare'
      ? bindingDigest(body)
      : nextReceiptDigest(operationId, previous, action, body, state, authorization, resolution),
    resolution,
    sequence: previous === null ? 0 : previous.sequence + 1,
    state,
  });
}

/**
 * Platform-neutral helper model. It uses only the merged protocol and block
 * device reference models and is never imported by production code.
 */
export class ActionJournalHelperReference {
  constructor({ device, key, nonce, expectedClient, now = () => Date.now() } = {}) {
    if (!(device instanceof InMemoryJournalBlockDevice) || typeof now !== 'function') fail('bootstrap_invalid');
    this.device = device;
    this.key = keyBytes(key);
    this.nonce = nonceValue(nonce);
    this.expectedClient = exactIdentity(expectedClient);
    this.now = now;
    this.store = null;
    this.client = null;
    this.requestReceiver = null;
    this.responseSequence = 0;
    this.commitInProgress = false;
    this.closed = false;
    this.recoveryCount = 0;
  }

  async start({ onBoundary } = {}) {
    if (this.closed || this.store !== null) fail('bootstrap_invalid');
    this.store = ActionJournalContainerReference.open(this.device);
    const initial = this.store.summary();
    for (const summary of initial.records) {
      const detail = this.store.detail(summary.operation_id);
      const previous = detail.events.at(-1);
      const recovery = startupRecovery(previous.state);
      if (recovery.state === previous.state) continue;
      const event = eventFor(
        summary.operation_id,
        previous,
        'startup_recovery',
        {},
        recovery.state,
        previous.authorization_kind,
        recovery.resolution,
      );
      this.commitInProgress = true;
      try { await this.store.append(summary.operation_id, event, { onBoundary }); }
      finally { this.commitInProgress = false; }
      this.recoveryCount++;
    }
    return Object.freeze({ production_enabled: false, recovery_count: this.recoveryCount, status: 'unavailable' });
  }

  connect(actualClient) {
    if (this.closed || this.store === null || this.client !== null) fail('pipe_connect_failed');
    const identity = exactIdentity(actualClient);
    if (!sameIdentity(identity, this.expectedClient)) fail('client_identity_mismatch');
    this.client = identity;
    this.requestReceiver = new ActionJournalProtocolReceiver({
      key: this.key,
      kind: 'request',
      nonce: this.nonce,
      now: this.now,
    });
    this.responseSequence = 0;
  }

  disconnect() {
    this.client = null;
    this.requestReceiver = null;
  }

  close() {
    this.disconnect();
    this.key.fill(0);
    this.nonce = '0'.repeat(32);
    this.closed = true;
  }

  _response(request, { operationId = request.operation_id, state = null, body = {}, error = null } = {}) {
    const envelope = buildResponse({
      requestId: request.request_id,
      sequence: this.responseSequence++,
      nonce: this.nonce,
      method: request.method,
      operationId,
      state,
      body,
      error,
    });
    return encodeEnvelope(envelope, this.key);
  }

  _error(request, code, operationId = request.operation_id) {
    return this._response(request, { operationId, error: protocolError(code) });
  }

  _detail(operationId) {
    try { return this.store.detail(operationId); }
    catch (error) { if (error?.code === 'container_not_found') return null; throw error; }
  }

  _summary(request) {
    const all = this.store.summary().records
      .map(item => this.store.detail(item.operation_id))
      .map(detail => receiptFrom(detail.operation_id, detail.events.at(-1)))
      .filter(receipt => request.body.include_terminal || !TERMINAL.has(receipt.state))
      .filter(receipt => request.body.cursor === null || receipt.operation_id > request.body.cursor)
      .sort((left, right) => left.operation_id.localeCompare(right.operation_id));
    const records = all.slice(0, request.body.limit);
    const truncated = records.length < all.length;
    return this._response(request, {
      body: { next_cursor: truncated ? records.at(-1).operation_id : null, records, truncated },
    });
  }

  _detailPage(request) {
    const detail = this._detail(request.operation_id);
    if (detail === null) return this._error(request, 'not_found');
    const { after_sequence: afterSequence, after_event_digest: afterDigest, limit } = request.body;
    const predecessor = afterSequence === null ? null : detail.events[afterSequence];
    if (
      afterSequence !== null
      && (predecessor === undefined || journalEventDigest(predecessor) !== afterDigest)
    ) return this._error(request, 'invalid_transition');
    const first = afterSequence === null ? 0 : afterSequence + 1;
    const events = detail.events.slice(first, first + limit).map(clone);
    const final = detail.events.at(-1);
    const truncated = events.length > 0 && events.at(-1).sequence < final.sequence;
    return this._response(request, {
      operationId: detail.operation_id,
      state: final.state,
      body: {
        events,
        next_sequence: truncated ? events.at(-1).sequence : null,
        predecessor: predecessor === null ? null : clone(predecessor),
        receipt: receiptFrom(detail.operation_id, final),
        truncated,
      },
    });
  }

  async _mutation(request, { signal, onBoundary } = {}) {
    if (signal?.aborted) return this._error(request, 'deadline_expired');
    let operationId = request.operation_id;
    let detail = null;
    let previous = null;
    let state;
    let authorization = null;
    let resolution = null;
    if (request.method === 'prepare') {
      operationId = operationIdFrom(this.store.containerId, request.body.operation_digest);
      detail = this._detail(operationId);
      if (detail !== null) {
        const first = detail.events[0];
        if (detail.events.length === 1 && first.state === 'prepared' && first.receipt_digest === bindingDigest(request.body)) {
          return this._response(request, { operationId, state: first.state, body: { receipt: receiptFrom(operationId, first) } });
        }
        return this._error(request, 'invalid_transition', null);
      }
      state = 'prepared';
    } else {
      detail = this._detail(operationId);
      if (detail === null) return this._error(request, 'not_found');
      previous = detail.events.at(-1);
      authorization = previous.authorization_kind;
      if (request.method === 'authorize') authorization = request.body.authorization_kind;
      if (request.method === 'acknowledge') resolution = 'provider_acknowledged';
      if (['complete', 'cancel', 'fail_definitive', 'mark_unknown'].includes(request.method)) resolution = request.body.resolution;
      try { state = transitionState(previous.state, request.method, resolution); }
      catch { return this._error(request, 'invalid_transition'); }
    }
    const event = eventFor(operationId, previous, request.method, request.body, state, authorization, resolution);
    this.commitInProgress = true;
    try { await this.store.append(operationId, event, { onBoundary }); }
    catch (error) {
      this.disconnect();
      throw error;
    } finally { this.commitInProgress = false; }
    if (signal?.aborted) return this._error(request, 'commit_non_cancellable');
    return this._response(request, { operationId, state, body: { receipt: receiptFrom(operationId, event) } });
  }

  async receive(frame, { signal, onBoundary, dropResponse = false } = {}) {
    if (this.closed || this.client === null || this.requestReceiver === null) fail('transport_closed');
    let request;
    try { request = this.requestReceiver.receive(frame); }
    catch (error) { this.disconnect(); throw error; }
    let response;
    if (request.method === 'health') {
      response = this._response(request, { body: { platform_available: false, production_enabled: false, recovery_count: this.recoveryCount, status: 'unavailable' } });
    } else if (request.method === 'summary') response = this._summary(request);
    else if (request.method === 'detail') response = this._detailPage(request);
    else response = await this._mutation(request, { signal, onBoundary });
    if (dropResponse) { this.disconnect(); return null; }
    return response;
  }
}

export const ACTION_JOURNAL_HELPER_LIMITS = Object.freeze({
  max_frame_bytes: ACTION_JOURNAL_LIMITS.max_frame_bytes,
  max_buffered_frames: ACTION_JOURNAL_LIMITS.max_buffered_frames,
  max_session_frames: ACTION_JOURNAL_LIMITS.max_session_frames,
  max_pending_requests: ACTION_JOURNAL_LIMITS.max_buffered_frames,
  bootstrap_bytes: 65536,
  io_deadline_ms: 15000,
});
