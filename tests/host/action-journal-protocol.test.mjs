import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ACTION_JOURNAL_EVENT_ACTIONS,
  ACTION_JOURNAL_LIMITS,
  ACTION_JOURNAL_METHODS,
  ACTION_JOURNAL_PROTOCOL,
  ACTION_JOURNAL_ERRORS,
  ACTION_JOURNAL_ERROR_RETRYABILITY,
  ACTION_JOURNAL_STATES,
  ActionJournalFrameDecoder,
  ActionJournalProtocolReceiver,
  PRODUCTION_ACTION_JOURNAL_PROTOCOL_AVAILABLE,
  buildRequest,
  buildResponse,
  canonicalJson,
  decodeFrame,
  encodeEnvelope,
  journalEventDigest,
  macForEnvelope,
  protocolError,
  redactedReceipt,
  startupRecovery,
  transitionState,
} from '../../host/agent/action-journal-protocol.mjs';

const KEY = Buffer.from('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'hex');
const NONCE = '00112233445566778899aabbccddeeff';
const NOW = 1_700_000_000_000;
const OPERATION = 'act_fedcba9876543210fedcba9876543210';
const DIGEST = Object.freeze({
  a: 'a'.repeat(64), b: 'b'.repeat(64), c: 'c'.repeat(64),
  d: 'd'.repeat(64), e: 'e'.repeat(64), f: 'f'.repeat(64),
});

function requestId(index) {
  return `req_${index.toString(16).padStart(32, '0')}`;
}

function bodyFor(method) {
  switch (method) {
    case 'health': return {};
    case 'prepare': return {
      arguments_digest: DIGEST.a,
      call_ref: DIGEST.b,
      operation_digest: DIGEST.c,
      preview_digest: DIGEST.d,
      request_ref: DIGEST.e,
      risk_tier: 'T3',
      side_effect: 'browser_activation',
      tool_name: 'browser.activate_control',
    };
    case 'authorize': return { authorization_kind: 'user_confirmation' };
    case 'dispatch': return {};
    case 'acknowledge': return { provider_receipt_digest: DIGEST.f };
    case 'begin_reconciliation': return { reason: 'lost_ack' };
    case 'complete': return { receipt_digest: DIGEST.e, resolution: 'completed' };
    case 'cancel': return { resolution: 'request_cancelled' };
    case 'fail_definitive': return { resolution: 'pre_dispatch_failure' };
    case 'mark_unknown': return { resolution: 'dispatch_ambiguous' };
    case 'summary': return { cursor: null, include_terminal: false, limit: 4 };
    case 'detail': return { after_event_digest: null, after_sequence: null, limit: 4 };
    default: throw new Error(`missing test body: ${method}`);
  }
}

function requestFor(method, sequence = 0, index = sequence + 1, overrides = {}) {
  const operationId = ['health', 'prepare', 'summary'].includes(method) ? null : OPERATION;
  return buildRequest({
    requestId: requestId(index),
    sequence,
    nonce: NONCE,
    issuedAtMs: NOW,
    deadlineAtMs: NOW + 5_000,
    method,
    operationId,
    body: bodyFor(method),
    ...overrides,
  });
}

function receiptFor(state, sequence = 0, operationId = OPERATION, overrides = {}) {
  const defaults = {
    prepared: { authorizationKind: null, resolution: null },
    authorized: { authorizationKind: 'user_confirmation', resolution: null },
    dispatching: { authorizationKind: 'user_confirmation', resolution: null },
    acknowledged: { authorizationKind: 'user_confirmation', resolution: 'provider_acknowledged' },
    reconciling: { authorizationKind: 'user_confirmation', resolution: null },
    completed: { authorizationKind: 'user_confirmation', resolution: 'completed' },
    cancelled: { authorizationKind: null, resolution: 'request_cancelled' },
    failed_definitive: { authorizationKind: null, resolution: 'pre_dispatch_failure' },
    unknown_manual: { authorizationKind: 'user_confirmation', resolution: 'dispatch_ambiguous' },
  }[state];
  return redactedReceipt({
    operationId,
    state,
    sequence,
    receiptDigest: DIGEST.f,
    ...defaults,
    ...overrides,
  });
}

function eventFor(state, sequence = 0, overrides = {}) {
  const defaultAction = {
    prepared: 'prepare',
    authorized: 'authorize',
    dispatching: 'dispatch',
    acknowledged: 'acknowledge',
    reconciling: 'begin_reconciliation',
    completed: 'complete',
    cancelled: 'cancel',
    failed_definitive: 'fail_definitive',
    unknown_manual: 'mark_unknown',
  }[state];
  const { action = defaultAction, ...receiptOverrides } = overrides;
  const receipt = receiptFor(state, sequence, OPERATION, receiptOverrides);
  return {
    action,
    authorization_kind: receipt.authorization_kind,
    receipt_digest: receipt.receipt_digest,
    resolution: receipt.resolution,
    sequence: receipt.sequence,
    state: receipt.state,
  };
}

function detailBodyAfter(event, limit) {
  return {
    after_event_digest: event === null ? null : journalEventDigest(event),
    after_sequence: event === null ? null : event.sequence,
    limit,
  };
}

function successResponseFor(request, sequence = 0, overrides = {}) {
  let operationId = request.operation_id;
  let state = null;
  let body;
  switch (request.method) {
    case 'health':
      body = { platform_available: false, production_enabled: false, recovery_count: 0, status: 'unavailable' };
      break;
    case 'summary':
      body = { next_cursor: null, records: [receiptFor('prepared')], truncated: false };
      break;
    case 'detail': {
      const receipt = receiptFor('prepared');
      state = 'prepared';
      body = {
        events: [eventFor('prepared')],
        next_sequence: null,
        predecessor: null,
        receipt,
        truncated: false,
      };
      break;
    }
    default: {
      const states = {
        prepare: 'prepared', authorize: 'authorized', dispatch: 'dispatching',
        acknowledge: 'acknowledged', begin_reconciliation: 'reconciling',
        complete: 'completed', cancel: 'cancelled',
        fail_definitive: 'failed_definitive', mark_unknown: 'unknown_manual',
      };
      state = states[request.method];
      if (request.method === 'prepare') operationId = OPERATION;
      body = { receipt: receiptFor(state, 0, operationId) };
    }
  }
  return buildResponse({
    requestId: request.request_id,
    sequence,
    nonce: NONCE,
    method: request.method,
    operationId,
    state,
    body,
    ...overrides,
  });
}

function errorCode(fn, code) {
  assert.throws(fn, error => error?.name === 'ActionJournalProtocolError' && error.code === code);
}

function signedPayload(value) {
  const unsigned = { ...value };
  delete unsigned.mac;
  const signed = { ...unsigned, mac: macForEnvelope(unsigned, KEY) };
  return { signed, text: canonicalJson(signed) };
}

function frameForText(text) {
  const payload = Buffer.from(text, 'utf8');
  const frame = Buffer.alloc(payload.length + 4);
  frame.writeUInt32BE(payload.length, 0);
  payload.copy(frame, 4);
  return frame;
}

function decodeRequest(frame, sequence = 0, seenRequestIds = new Set(), nowMs = NOW) {
  return decodeFrame(frame, KEY, {
    expectedKind: 'request',
    expectedNonce: NONCE,
    expectedSequence: sequence,
    seenRequestIds,
    nowMs,
  });
}

test('slice is explicit source-only metadata and every method has an exact valid request', () => {
  assert.equal(PRODUCTION_ACTION_JOURNAL_PROTOCOL_AVAILABLE, false);
  assert.equal(ACTION_JOURNAL_PROTOCOL, 'lae.action-journal.v0.1.0');
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'request', nonce: NONCE, now: () => NOW });
  ACTION_JOURNAL_METHODS.forEach((method, sequence) => {
    const value = receiver.receive(encodeEnvelope(requestFor(method, sequence), KEY));
    assert.equal(value.method, method);
    assert.equal(value.sequence, sequence);
  });
});

test('every method has a request-correlated bounded successful response', () => {
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  ACTION_JOURNAL_METHODS.forEach((method, sequence) => {
    const request = requestFor(method, sequence, sequence + 40);
    receiver.expectResponse(request);
    const response = receiver.receive(encodeEnvelope(successResponseFor(request, sequence), KEY));
    assert.equal(response.method, method);
    assert.equal(response.request_id, request.request_id);
  });
});

test('response receiver refuses unsolicited, wrong, late, and reordered responses without poisoning the expected request', () => {
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  const request = requestFor('authorize', 0, 70);
  const correct = successResponseFor(request, 0);
  errorCode(() => receiver.receive(encodeEnvelope(correct, KEY)), 'invalid_request');
  receiver.expectResponse(request);
  errorCode(() => receiver.receive(encodeEnvelope({ ...correct, request_id: requestId(71) }, KEY)), 'invalid_request');
  errorCode(() => receiver.receive(encodeEnvelope({ ...correct, method: 'dispatch', body: { receipt: receiptFor('dispatching') }, state: 'dispatching' }, KEY)), 'invalid_request');
  assert.equal(receiver.receive(encodeEnvelope(correct, KEY)).state, 'authorized');

  const late = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW + 5_001 });
  late.expectResponse(request);
  errorCode(() => late.receive(encodeEnvelope(correct, KEY)), 'deadline_expired');
});

test('prepare is the only success allowed to introduce an operation id', () => {
  const request = requestFor('prepare', 0, 72);
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  receiver.expectResponse(request);
  const malformed = successResponseFor(request, 0, { operationId: null, body: { receipt: receiptFor('prepared', 0, OPERATION) } });
  errorCode(() => receiver.receive(encodeEnvelope(malformed, KEY)), 'invalid_request');
  assert.equal(receiver.receive(encodeEnvelope(successResponseFor(request, 0), KEY)).operation_id, OPERATION);
});

test('unknown and missing fields, invalid state receipts, unsafe strings, floats, and negative zero fail closed', () => {
  errorCode(() => encodeEnvelope({ ...requestFor('health'), surprise: true }, KEY), 'unknown_field');
  const missing = requestFor('health');
  delete missing.body;
  errorCode(() => encodeEnvelope(missing, KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('prepare', 0, 1, { body: { ...bodyFor('prepare'), tool_name: 'bad\nname' } }), KEY), 'invalid_request');
  errorCode(() => canonicalJson({ unsafe: 1.5 }), 'invalid_request');
  errorCode(() => canonicalJson({ unsafe: -0 }), 'invalid_request');
  errorCode(() => canonicalJson({ unsafe: 'x'.repeat(ACTION_JOURNAL_LIMITS.max_string_bytes + 1) }), 'invalid_request');
  errorCode(() => redactedReceipt({ operationId: OPERATION, state: 'completed', sequence: 1, receiptDigest: DIGEST.f, authorizationKind: null, resolution: 'completed' }), 'invalid_request');
  errorCode(() => redactedReceipt({ operationId: OPERATION, state: 'prepared', sequence: 0, receiptDigest: DIGEST.f, authorizationKind: 'policy', resolution: null }), 'invalid_request');
});

test('identifier, digest, enum, nonce, and finite error schemas are exact', () => {
  errorCode(() => encodeEnvelope(requestFor('health', 0, 1, { requestId: 'request-1' }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('dispatch', 0, 2, { operationId: 'act_not_hex' }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('health', 0, 3, { nonce: 'A'.repeat(32) }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('prepare', 0, 4, { body: { ...bodyFor('prepare'), risk_tier: 'ADMIN' } }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('prepare', 0, 5, { body: { ...bodyFor('prepare'), arguments_digest: 'a'.repeat(63) } }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('authorize', 0, 6, { body: { authorization_kind: 'model' } }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('detail', 0, 7, { body: { after_event_digest: null, after_sequence: 1, limit: 1 } }), KEY), 'invalid_request');
  errorCode(() => encodeEnvelope(requestFor('detail', 0, 8, { body: { after_event_digest: DIGEST.a, after_sequence: null, limit: 1 } }), KEY), 'invalid_request');
  assert.deepEqual(ACTION_JOURNAL_ERRORS, Object.keys(ACTION_JOURNAL_ERROR_RETRYABILITY));
  assert.deepEqual(protocolError('queue_full'), { code: 'queue_full', retryable: true });
  errorCode(() => protocolError('provider_said_something'), 'invalid_request');
});

test('duplicate JSON keys are distinguished and noncanonical key order is rejected', () => {
  const { signed, text } = signedPayload(requestFor('health', 0, 73));
  const duplicate = text.replace(/\}$/, ',"version":1}');
  errorCode(() => decodeRequest(frameForText(duplicate)), 'duplicate_key');
  const reverseOrder = JSON.stringify(Object.fromEntries(Object.keys(signed).sort().reverse().map(key => [key, signed[key]])));
  errorCode(() => decodeRequest(frameForText(reverseOrder)), 'noncanonical_json');
});

test('strict UTF-8, frame lengths, partial frames, and bounded incremental buffering fail closed', () => {
  const invalidUtf8 = Buffer.from([0, 0, 0, 2, 0xc3, 0x28]);
  errorCode(() => decodeRequest(invalidUtf8), 'invalid_utf8');
  errorCode(() => decodeRequest(Buffer.from([0, 0, 0, 1, 0x7b])), 'invalid_frame');
  const oversizedPrefix = Buffer.alloc(4);
  oversizedPrefix.writeUInt32BE(ACTION_JOURNAL_LIMITS.max_payload_bytes + 1);
  errorCode(() => decodeRequest(oversizedPrefix), 'invalid_frame');

  const frame = encodeEnvelope(requestFor('health', 0, 74), KEY);
  const decoder = new ActionJournalFrameDecoder();
  assert.deepEqual(decoder.push(frame.subarray(0, 5)), []);
  errorCode(() => decoder.finish(), 'invalid_frame');
  const decoder2 = new ActionJournalFrameDecoder();
  assert.deepEqual(decoder2.push(frame.subarray(0, 5)), []);
  assert.equal(decoder2.push(frame.subarray(5)).length, 1);
  decoder2.finish();
  errorCode(() => new ActionJournalFrameDecoder().push(Buffer.alloc(ACTION_JOURNAL_LIMITS.max_frame_bytes * ACTION_JOURNAL_LIMITS.max_buffered_frames + 1)), 'queue_full');
  const tooManyDecoder = new ActionJournalFrameDecoder();
  errorCode(() => tooManyDecoder.push(Buffer.concat(Array.from({ length: ACTION_JOURNAL_LIMITS.max_buffered_frames + 1 }, () => frame))), 'record_limit_exceeded');
});

test('validly authenticated unknown fields and malformed MACs are rejected', () => {
  const request = { ...requestFor('health', 0, 75), extension: false };
  const { text } = signedPayload(request);
  errorCode(() => decodeRequest(frameForText(text)), 'unknown_field');
  const frame = encodeEnvelope(requestFor('health', 0, 76), KEY);
  const parsed = JSON.parse(frame.subarray(4).toString('utf8'));
  parsed.mac = '0'.repeat(64);
  errorCode(() => decodeRequest(frameForText(canonicalJson(parsed))), 'invalid_mac');
  parsed.mac = 'z'.repeat(64);
  errorCode(() => decodeRequest(frameForText(canonicalJson(parsed))), 'invalid_request');
});

test('nonce, sequence, request-id replay, and bounded session records are enforced', () => {
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'request', nonce: NONCE, now: () => NOW });
  const first = requestFor('health', 0, 77);
  const firstFrame = encodeEnvelope(first, KEY);
  receiver.receive(firstFrame);
  errorCode(() => receiver.receive(firstFrame), 'replay');
  const duplicateIdAtNextSequence = requestFor('health', 1, 77);
  errorCode(() => receiver.receive(encodeEnvelope(duplicateIdAtNextSequence, KEY)), 'replay');

  const futureReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'request', nonce: NONCE, now: () => NOW });
  errorCode(() => futureReceiver.receive(encodeEnvelope(requestFor('health', 1, 78), KEY)), 'sequence_out_of_order');
  errorCode(() => decodeRequest(encodeEnvelope(requestFor('health', 0, 79, { nonce: 'f'.repeat(32) }), KEY)), 'nonce_mismatch');

  const seen = new Set(Array.from({ length: ACTION_JOURNAL_LIMITS.max_session_frames }, (_, index) => requestId(index + 100)));
  errorCode(() => decodeRequest(encodeEnvelope(requestFor('health', 0, 2000), KEY), 0, seen), 'record_limit_exceeded');
});

test('absolute deadline and future issue-time checks happen on authenticated requests', () => {
  errorCode(() => decodeRequest(encodeEnvelope(requestFor('health', 0, 80), KEY), 0, new Set(), NOW + 5_001), 'deadline_expired');
  const future = requestFor('health', 0, 81, { issuedAtMs: NOW + ACTION_JOURNAL_LIMITS.max_clock_skew_ms + 1, deadlineAtMs: NOW + ACTION_JOURNAL_LIMITS.max_clock_skew_ms + 2 });
  errorCode(() => decodeRequest(encodeEnvelope(future, KEY)), 'invalid_request');
  const tooLong = requestFor('health', 0, 82, { deadlineAtMs: NOW + ACTION_JOURNAL_LIMITS.max_deadline_span_ms + 1 });
  errorCode(() => encodeEnvelope(tooLong, KEY), 'invalid_request');
});

test('summary and detail query bounds, ordering, and pagination are response-bound', () => {
  const summary = requestFor('summary', 0, 83, { body: { cursor: null, include_terminal: true, limit: 1 } });
  const summaryReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  summaryReceiver.expectResponse(summary);
  const tooMany = successResponseFor(summary, 0, {
    body: { next_cursor: null, records: [receiptFor('prepared'), receiptFor('prepared', 0, 'act_ffffffffffffffffffffffffffffffff')], truncated: false },
  });
  errorCode(() => summaryReceiver.receive(encodeEnvelope(tooMany, KEY)), 'invalid_request');

  const detail = requestFor('detail', 0, 84, { body: detailBodyAfter(eventFor('authorized', 3), 2) });
  const detailReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  detailReceiver.expectResponse(detail);
  const wrongFirst = successResponseFor(detail, 0);
  errorCode(() => detailReceiver.receive(encodeEnvelope(wrongFirst, KEY)), 'invalid_request');
});

test('summary response honors the exact include-terminal filter', () => {
  const excluded = requestFor('summary', 0, 90, {
    body: { cursor: null, include_terminal: false, limit: 2 },
  });
  for (const state of ['completed', 'cancelled', 'failed_definitive']) {
    const rejectingReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
    rejectingReceiver.expectResponse(excluded);
    const terminal = successResponseFor(excluded, 0, {
      body: { next_cursor: null, records: [receiptFor(state)], truncated: false },
    });
    errorCode(() => rejectingReceiver.receive(encodeEnvelope(terminal, KEY)), 'invalid_request');
  }
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  receiver.expectResponse(excluded);
  assert.equal(receiver.receive(encodeEnvelope(successResponseFor(excluded, 0), KEY)).body.records[0].state, 'prepared');

  const included = requestFor('summary', 0, 91, {
    body: { cursor: null, include_terminal: true, limit: 1 },
  });
  const includedReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  includedReceiver.expectResponse(included);
  const accepted = successResponseFor(included, 0, {
    body: { next_cursor: null, records: [receiptFor('failed_definitive')], truncated: false },
  });
  assert.equal(includedReceiver.receive(encodeEnvelope(accepted, KEY)).body.records[0].state, 'failed_definitive');
});

test('detail pagination is complete and contiguous through the authenticated receipt high-watermark', () => {
  const request = requestFor('detail', 0, 92, {
    body: detailBodyAfter(eventFor('authorized', 3), 2),
  });
  const receipt = receiptFor('reconciling', 6);
  const page = buildResponse({
    requestId: request.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: receipt.state,
    body: {
      events: [eventFor('dispatching', 4), eventFor('acknowledged', 5)],
      next_sequence: 5,
      predecessor: eventFor('authorized', 3),
      receipt,
      truncated: true,
    },
  });
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  receiver.expectResponse(request);
  assert.equal(receiver.receive(encodeEnvelope(page, KEY)).body.receipt.sequence, 6);

  const finalRequest = requestFor('detail', 0, 93, {
    body: detailBodyAfter(eventFor('acknowledged', 5), 2),
  });
  const finalReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  finalReceiver.expectResponse(finalRequest);
  const finalPage = buildResponse({
    requestId: finalRequest.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: receipt.state,
    body: { events: [eventFor('reconciling', 6)], next_sequence: null, predecessor: eventFor('acknowledged', 5), receipt, truncated: false },
  });
  assert.equal(finalReceiver.receive(encodeEnvelope(finalPage, KEY)).body.truncated, false);

  const currentRequest = requestFor('detail', 0, 94, {
    body: detailBodyAfter(eventFor('reconciling', 6), 2),
  });
  const currentReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  currentReceiver.expectResponse(currentRequest);
  const currentPage = buildResponse({
    requestId: currentRequest.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: receipt.state,
    body: { events: [], next_sequence: null, predecessor: eventFor('reconciling', 6), receipt, truncated: false },
  });
  assert.deepEqual(currentReceiver.receive(encodeEnvelope(currentPage, KEY)).body.events, []);
});

test('detail validates initial history and exact transitions across page boundaries', () => {
  const receipt = receiptFor('reconciling', 3);
  const firstRequest = requestFor('detail', 0, 97, {
    body: detailBodyAfter(null, 2),
  });
  const firstResponse = buildResponse({
    requestId: firstRequest.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: receipt.state,
    body: {
      events: [eventFor('prepared', 0), eventFor('authorized', 1)],
      next_sequence: 1,
      predecessor: null,
      receipt,
      truncated: true,
    },
  });
  const firstReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  firstReceiver.expectResponse(firstRequest);
  const firstPage = firstReceiver.receive(encodeEnvelope(firstResponse, KEY));
  assert.equal(firstPage.body.events.at(-1).state, 'authorized');

  const nextRequest = requestFor('detail', 0, 98, {
    body: detailBodyAfter(firstPage.body.events.at(-1), 2),
  });
  const nextResponse = buildResponse({
    requestId: nextRequest.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: receipt.state,
    body: {
      events: [eventFor('dispatching', 2), eventFor('reconciling', 3)],
      next_sequence: null,
      predecessor: firstPage.body.events.at(-1),
      receipt,
      truncated: false,
    },
  });
  const nextReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  nextReceiver.expectResponse(nextRequest);
  assert.equal(nextReceiver.receive(encodeEnvelope(nextResponse, KEY)).body.events.at(-1).state, 'reconciling');

  const recoveryRequest = requestFor('detail', 0, 99, {
    body: detailBodyAfter(eventFor('authorized', 1), 1),
  });
  const recoveryReceipt = receiptFor('cancelled', 2, OPERATION, {
    authorizationKind: 'user_confirmation',
    resolution: 'startup_recovery',
  });
  const recoveryResponse = buildResponse({
    requestId: recoveryRequest.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: recoveryReceipt.state,
    body: {
      events: [eventFor('cancelled', 2, { action: 'startup_recovery', authorizationKind: 'user_confirmation', resolution: 'startup_recovery' })],
      next_sequence: null,
      predecessor: eventFor('authorized', 1),
      receipt: recoveryReceipt,
      truncated: false,
    },
  });
  const recoveryReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  recoveryReceiver.expectResponse(recoveryRequest);
  assert.equal(recoveryReceiver.receive(encodeEnvelope(recoveryResponse, KEY)).body.receipt.resolution, 'startup_recovery');
});

test('detail rejects invalid initial, terminal, action, resolution, and authorization transitions', () => {
  const cases = [
    {
      after: null,
      events: [eventFor('authorized', 0)],
      predecessor: null,
      receipt: receiptFor('authorized', 0),
    },
    {
      after: 4,
      events: [eventFor('prepared', 5)],
      predecessor: eventFor('completed', 4),
      receipt: receiptFor('prepared', 5),
    },
    {
      after: 1,
      events: [eventFor('acknowledged', 2)],
      predecessor: eventFor('authorized', 1),
      receipt: receiptFor('acknowledged', 2),
    },
    {
      after: 4,
      events: [eventFor('completed', 5, { resolution: 'completed' })],
      predecessor: eventFor('unknown_manual', 4),
      receipt: receiptFor('completed', 5, OPERATION, { resolution: 'completed' }),
    },
    {
      after: 1,
      events: [eventFor('dispatching', 2, { authorizationKind: 'operator_grant' })],
      predecessor: eventFor('authorized', 1),
      receipt: receiptFor('dispatching', 2, OPERATION, { authorizationKind: 'operator_grant' }),
    },
    {
      after: 1,
      requestPredecessor: eventFor('authorized', 1),
      events: [eventFor('dispatching', 2, { receiptDigest: DIGEST.e })],
      predecessor: eventFor('authorized', 1, { receiptDigest: DIGEST.e }),
      receipt: receiptFor('dispatching', 2, OPERATION, { receiptDigest: DIGEST.e }),
    },
  ];
  for (const [index, value] of cases.entries()) {
    const requestPredecessor = Object.hasOwn(value, 'requestPredecessor')
      ? value.requestPredecessor
      : value.predecessor;
    const request = requestFor('detail', 0, 100 + index, {
      body: detailBodyAfter(requestPredecessor, 1),
    });
    assert.equal(request.body.after_sequence, value.after);
    const response = buildResponse({
      requestId: request.request_id,
      sequence: 0,
      nonce: NONCE,
      method: 'detail',
      operationId: OPERATION,
      state: value.receipt.state,
      body: {
        events: value.events,
        next_sequence: null,
        predecessor: value.predecessor,
        receipt: value.receipt,
        truncated: false,
      },
    });
    const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
    receiver.expectResponse(request);
    errorCode(() => receiver.receive(encodeEnvelope(response, KEY)), 'invalid_request');
  }
});

test('detail rejects hidden, short, gapped, or receipt-divergent event pages', () => {
  const request = requestFor('detail', 0, 95, {
    body: detailBodyAfter(eventFor('authorized', 3), 2),
  });
  const receipt = receiptFor('reconciling', 5);
  const invalidBodies = [
    // A hidden final event cannot be described as a complete page.
    { events: [eventFor('dispatching', 4)], next_sequence: null, predecessor: eventFor('authorized', 3), receipt, truncated: false },
    // A truncated page must fill the requested bound while more events remain.
    { events: [eventFor('dispatching', 4)], next_sequence: 4, predecessor: eventFor('authorized', 3), receipt: receiptFor('reconciling', 6), truncated: true },
    // The page must begin exactly after the request cursor.
    { events: [eventFor('reconciling', 5)], next_sequence: null, predecessor: eventFor('authorized', 3), receipt, truncated: false },
    // Completion must match every exposed receipt projection field.
    { events: [eventFor('dispatching', 4), eventFor('reconciling', 5, { receiptDigest: DIGEST.e })], next_sequence: null, predecessor: eventFor('authorized', 3), receipt, truncated: false },
  ];
  for (const body of invalidBodies) {
    const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
    receiver.expectResponse(request);
    const response = buildResponse({
      requestId: request.request_id,
      sequence: 0,
      nonce: NONCE,
      method: 'detail',
      operationId: OPERATION,
      state: body.receipt.state,
      body,
    });
    errorCode(() => receiver.receive(encodeEnvelope(response, KEY)), 'invalid_request');
  }

  const ahead = requestFor('detail', 0, 96, {
    body: detailBodyAfter(eventFor('reconciling', 6), 2),
  });
  const aheadReceipt = receiptFor('reconciling', 5);
  const aheadResponse = buildResponse({
    requestId: ahead.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'detail',
    operationId: OPERATION,
    state: aheadReceipt.state,
    body: { events: [], next_sequence: null, predecessor: eventFor('reconciling', 6), receipt: aheadReceipt, truncated: false },
  });
  const aheadReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  aheadReceiver.expectResponse(ahead);
  errorCode(() => aheadReceiver.receive(encodeEnvelope(aheadResponse, KEY)), 'invalid_request');
});

test('checked-in adversarial vectors cover response correlation and manual resolution', async () => {
  const vectors = JSON.parse(await readFile(new URL('../../contracts/action-journal/v0.1.0-vectors.json', import.meta.url), 'utf8'));
  assert.deepEqual(
    vectors.adversarial_response_vectors.map(vector => [vector.name, vector.expected_error]),
    [
      ['summary_terminal_under_nonterminal_filter', 'invalid_request'],
      ['detail_hidden_final_event', 'invalid_request'],
      ['detail_underfilled_truncated_page', 'invalid_request'],
      ['detail_completion_receipt_mismatch', 'invalid_request'],
      ['detail_cursor_beyond_high_watermark', 'invalid_request'],
      ['unknown_manual_ordinary_complete_resolution', 'invalid_transition'],
      ['unknown_manual_pre_dispatch_failure_resolution', 'invalid_transition'],
      ['detail_nonprepared_initial_event', 'invalid_request'],
      ['detail_completed_to_prepared_transition', 'invalid_request'],
      ['detail_wrong_action_at_boundary', 'invalid_request'],
      ['detail_predecessor_digest_mismatch', 'invalid_request'],
    ],
  );
  for (const vector of vectors.adversarial_response_vectors) {
    if (vector.request_method === 'summary') {
      assert.deepEqual(Object.keys(vector.request_body).sort(), ['cursor', 'include_terminal', 'limit']);
    } else if (vector.request_method === 'detail') {
      assert.deepEqual(Object.keys(vector.request_body).sort(), ['after_event_digest', 'after_sequence', 'limit']);
    }
  }
  assert.equal(
    vectors.adversarial_response_vectors.find(vector => vector.name === 'detail_hidden_final_event').request_body.after_event_digest,
    journalEventDigest(eventFor('authorized', 3)),
  );
  assert.equal(
    vectors.adversarial_response_vectors.find(vector => vector.name === 'detail_completed_to_prepared_transition').request_body.after_event_digest,
    journalEventDigest(eventFor('completed', 4)),
  );
  assert.deepEqual(
    vectors.event_digest_vectors.map(({ event, canonical_event: canonicalEvent, digest_hex: digestHex }) => ({
      canonical: canonicalJson(event) === canonicalEvent,
      digest: journalEventDigest(event) === digestHex,
    })),
    Array.from({ length: 4 }, () => ({ canonical: true, digest: true })),
  );
  assert.deepEqual(
    vectors.valid_detail_page_vectors.map(vector => ({
      name: vector.name,
      request: vector.request_body,
      predecessor: vector.response_projection.predecessor_event_digest,
      sequences: vector.response_projection.event_sequences,
      truncated: vector.response_projection.truncated,
    })),
    [
      {
        name: 'initial_page_to_authorized',
        request: detailBodyAfter(null, 2),
        predecessor: null,
        sequences: [0, 1],
        truncated: true,
      },
      {
        name: 'continued_page_to_high_watermark',
        request: detailBodyAfter(eventFor('authorized', 1), 2),
        predecessor: journalEventDigest(eventFor('authorized', 1)),
        sequences: [2, 3],
        truncated: false,
      },
    ],
  );
});

test('receipt relation is exact and raw provider fields can never enter a response', () => {
  const request = requestFor('acknowledge', 0, 85);
  const wrongResolution = successResponseFor(request, 0);
  wrongResolution.body = { receipt: { ...wrongResolution.body.receipt, resolution: 'completed', state: 'completed' } };
  wrongResolution.state = 'completed';
  errorCode(() => encodeEnvelope(wrongResolution, KEY), 'invalid_request');
  const raw = successResponseFor(request, 0);
  raw.body.provider_payload = { token: 'secret' };
  errorCode(() => encodeEnvelope(raw, KEY), 'unknown_field');
  const receipt = receiptFor('unknown_manual');
  assert.equal(receipt.recovery_required, true);
  assert.equal(JSON.stringify(receipt).includes('secret'), false);
  assert.deepEqual(Object.keys(receipt).sort(), [
    'authorization_kind', 'operation_id', 'receipt_digest', 'recovery_required',
    'redacted', 'resolution', 'sequence', 'state',
  ]);
});

test('transition graph rejects out-of-order and terminal mutations', () => {
  assert.equal(transitionState(null, 'prepare'), 'prepared');
  assert.equal(transitionState('prepared', 'authorize'), 'authorized');
  assert.equal(transitionState('authorized', 'dispatch'), 'dispatching');
  assert.equal(transitionState('dispatching', 'acknowledge', 'provider_acknowledged'), 'acknowledged');
  assert.equal(transitionState('dispatching', 'begin_reconciliation'), 'reconciling');
  assert.equal(transitionState('reconciling', 'mark_unknown', 'dispatch_ambiguous'), 'unknown_manual');
  assert.equal(transitionState('prepared', 'cancel', 'user_denied'), 'cancelled');
  assert.equal(transitionState('acknowledged', 'complete', 'completed'), 'completed');
  assert.equal(transitionState('reconciling', 'fail_definitive', 'pre_dispatch_failure'), 'failed_definitive');
  assert.equal(transitionState('unknown_manual', 'complete', 'manual_completed'), 'completed');
  assert.equal(transitionState('unknown_manual', 'fail_definitive', 'manual_failed_definitive'), 'failed_definitive');
  errorCode(() => transitionState('unknown_manual', 'complete', 'completed'), 'invalid_transition');
  errorCode(() => transitionState('unknown_manual', 'fail_definitive', 'pre_dispatch_failure'), 'invalid_transition');
  errorCode(() => transitionState('acknowledged', 'complete', 'manual_completed'), 'invalid_transition');
  errorCode(() => transitionState('prepared', 'fail_definitive', 'manual_failed_definitive'), 'invalid_transition');
  errorCode(() => transitionState('prepared', 'cancel'), 'invalid_transition');
  errorCode(() => transitionState('reconciling', 'mark_unknown'), 'invalid_transition');
  errorCode(() => transitionState('prepared', 'dispatch'), 'invalid_transition');
  errorCode(() => transitionState('completed', 'cancel'), 'invalid_transition');
});

test('lost dispatch acknowledgement requires a fresh authenticated detail session, never replay', () => {
  const firstSession = new ActionJournalProtocolReceiver({ key: KEY, kind: 'request', nonce: NONCE, now: () => NOW });
  const dispatch = requestFor('dispatch', 0, 86);
  firstSession.receive(encodeEnvelope(dispatch, KEY));
  errorCode(() => firstSession.receive(encodeEnvelope(dispatch, KEY)), 'replay');

  const recoveryNonce = '11223344556677889900aabbccddeeff';
  const detail = requestFor('detail', 0, 87, { nonce: recoveryNonce });
  const recoverySession = new ActionJournalProtocolReceiver({ key: KEY, kind: 'request', nonce: recoveryNonce, now: () => NOW });
  assert.equal(recoverySession.receive(encodeEnvelope(detail, KEY)).method, 'detail');
});

test('noncancellable completion returns a fixed nonretryable error and requires detail', () => {
  const cancel = requestFor('cancel', 0, 88);
  const response = buildResponse({
    requestId: cancel.request_id,
    sequence: 0,
    nonce: NONCE,
    method: 'cancel',
    operationId: OPERATION,
    state: null,
    body: {},
    error: protocolError('commit_non_cancellable'),
  });
  const receiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  receiver.expectResponse(cancel);
  const decoded = receiver.receive(encodeEnvelope(response, KEY));
  assert.deepEqual(decoded.error, { code: 'commit_non_cancellable', retryable: false });
  errorCode(() => encodeEnvelope({ ...response, state: 'completed' }, KEY), 'invalid_request');
});

test('startup recovery never guesses an external effect', () => {
  assert.deepEqual(startupRecovery('prepared'), { state: 'cancelled', resolution: 'startup_recovery' });
  assert.deepEqual(startupRecovery('authorized'), { state: 'cancelled', resolution: 'startup_recovery' });
  for (const state of ['dispatching', 'acknowledged', 'reconciling']) {
    assert.deepEqual(startupRecovery(state), { state: 'unknown_manual', resolution: 'dispatch_ambiguous' });
  }
  for (const state of ['completed', 'cancelled', 'failed_definitive', 'unknown_manual']) {
    assert.deepEqual(startupRecovery(state), { state, resolution: null });
  }
});

test('deterministic cross-language frames match the checked-in synthetic vectors', async () => {
  const vectors = JSON.parse(await readFile(new URL('../../contracts/action-journal/v0.1.0-vectors.json', import.meta.url), 'utf8'));
  assert.equal(vectors.synthetic_only, true);
  assert.equal(vectors.key_hex, KEY.toString('hex'));
  for (const vector of vectors.vectors) {
    const envelope = vector.envelope;
    assert.equal(canonicalJson(Object.fromEntries(Object.entries(envelope).filter(([key]) => key !== 'mac'))), vector.canonical_without_mac);
    assert.equal(envelope.mac, vector.mac_hex);
    assert.equal(encodeEnvelope(envelope, KEY).toString('hex'), vector.frame_hex);
  }
  const requestVector = vectors.vectors.find(vector => vector.direction === 'request');
  const responseVector = vectors.vectors.find(vector => vector.direction === 'response');
  const requestReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'request', nonce: NONCE, now: () => NOW });
  assert.equal(requestReceiver.receive(Buffer.from(requestVector.frame_hex, 'hex')).method, 'health');
  const responseReceiver = new ActionJournalProtocolReceiver({ key: KEY, kind: 'response', nonce: NONCE, now: () => NOW });
  responseReceiver.expectResponse(requestVector.envelope);
  assert.equal(responseReceiver.receive(Buffer.from(responseVector.frame_hex, 'hex')).body.production_enabled, false);
});

test('machine-readable contract stays exactly synchronized with protocol metadata', async () => {
  const contract = JSON.parse(await readFile(new URL('../../contracts/action-journal/v0.1.0.json', import.meta.url), 'utf8'));
  assert.equal(contract.schema_version, 'lae.action-journal-contract.v1');
  assert.equal(contract.production_available, false);
  assert.equal(contract.runtime_dependency_added, false);
  assert.equal(contract.transport_selected, false);
  assert.deepEqual(contract.methods, ACTION_JOURNAL_METHODS);
  assert.deepEqual(contract.event_actions, ACTION_JOURNAL_EVENT_ACTIONS);
  assert.deepEqual(contract.states, ACTION_JOURNAL_STATES);
  assert.deepEqual(contract.error_retryability, ACTION_JOURNAL_ERROR_RETRYABILITY);
  assert.equal(contract.limits.max_frame_bytes, ACTION_JOURNAL_LIMITS.max_frame_bytes);
  assert.equal(contract.limits.max_payload_bytes, ACTION_JOURNAL_LIMITS.max_payload_bytes);
});

test('HMAC construction is independently reproducible from the published formula', () => {
  const request = requestFor('health', 0, 89);
  const unsigned = { ...request };
  delete unsigned.mac;
  const expected = createHmac('sha256', KEY)
    .update(`${ACTION_JOURNAL_PROTOCOL}\0${canonicalJson(unsigned)}`, 'utf8')
    .digest('hex');
  assert.equal(macForEnvelope(request, KEY), expected);
});
