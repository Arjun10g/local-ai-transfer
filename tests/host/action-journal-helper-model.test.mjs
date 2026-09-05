import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ActionJournalProtocolReceiver,
  buildRequest,
  decodeFrame,
  encodeEnvelope,
  journalEventDigest,
} from '../../host/agent/action-journal-protocol.mjs';
import {
  ACTION_JOURNAL_CONTAINER_BOUNDARIES,
  ActionJournalContainerReference,
  InMemoryJournalBlockDevice,
  formatJournalContainer,
} from '../reference/action-journal-container-model.mjs';
import {
  ACTION_JOURNAL_HELPER_PRODUCTION_AVAILABLE,
  ActionJournalHelperReference,
  classifyBoundedStorageCompletion,
  classifyCancelledPipeSettlement,
  modelParsedBootstrapSecretLifetime,
  validatePersistedAuthority,
} from '../reference/action-journal-helper-model.mjs';

const KEY = Buffer.from('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'hex');
const NEXT_KEY = Buffer.from('202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f', 'hex');
const NONCE = '00112233445566778899aabbccddeeff';
const NEXT_NONCE = '102132435465768798a9bacbdcedfe0f';
const NOW = 1_700_000_000_000;
const CONTAINER_ID = Buffer.alloc(32, 0x42);
const DIGEST = Object.freeze({
  a: 'a'.repeat(64), b: 'b'.repeat(64), c: 'c'.repeat(64),
  d: 'd'.repeat(64), e: 'e'.repeat(64), f: 'f'.repeat(64),
});
const CLIENT = Object.freeze({
  creation_time: '133713371337',
  image_file_id: '0123456789abcdef0123456789abcdef',
  image_path: 'C:\\Approved\\node.exe',
  image_volume_serial: '0123456789abcdef',
  pid: 4242,
  session_id: 7,
  user_sid: 'S-1-5-21-1000',
});
const ISSUER = Object.freeze({
  creation_time: '133713371300',
  image_file_id: '1'.repeat(64),
  image_volume_serial: '1122334455667788',
  parent_pid: 4000,
  pipe_server_pid: 4000,
  session_id: 7,
  trust_anchor_digest: '2'.repeat(64),
  user_sid: 'S-1-5-21-1000',
});

function body(method, overrides = {}) {
  const values = {
    health: {},
    prepare: { arguments_digest: DIGEST.a, call_ref: DIGEST.b, operation_digest: DIGEST.c, preview_digest: DIGEST.d, request_ref: DIGEST.e, risk_tier: 'T3', side_effect: 'cloud_inference', tool_name: 'coding.copilot_ask' },
    authorize: { authorization_kind: 'user_confirmation' },
    dispatch: {},
    acknowledge: { provider_receipt_digest: DIGEST.f },
    begin_reconciliation: { reason: 'lost_ack' },
    complete: { receipt_digest: DIGEST.e, resolution: 'completed' },
    cancel: { resolution: 'request_cancelled' },
    fail_definitive: { resolution: 'pre_dispatch_failure' },
    mark_unknown: { resolution: 'dispatch_ambiguous' },
    summary: { cursor: null, include_terminal: true, limit: 8 },
    detail: { after_event_digest: null, after_sequence: null, limit: 8 },
  }[method];
  return { ...values, ...overrides };
}

async function formattedDevice() {
  const device = new InMemoryJournalBlockDevice();
  await formatJournalContainer(device, { containerId: CONTAINER_ID });
  return device;
}

async function helperFor(device, options = {}) {
  const selectedDevice = device ?? await formattedDevice();
  const helper = new ActionJournalHelperReference({ device: selectedDevice, key: options.key ?? KEY, nonce: options.nonce ?? NONCE, expectedClient: CLIENT, expectedIssuer: ISSUER, now: options.now ?? (() => NOW) });
  await helper.start({ issuer: ISSUER, ...(options.start ?? {}) });
  helper.connect(CLIENT);
  return helper;
}

class ClientSession {
  constructor(helper, key = KEY, nonce = NONCE) {
    this.helper = helper; this.key = key; this.nonce = nonce; this.requestSequence = 0; this.requestIndex = 1;
    this.responses = new ActionJournalProtocolReceiver({ key, kind: 'response', nonce, now: () => NOW });
  }
  request(method, operationId = null, bodyOverrides = {}, requestOverrides = {}) {
    return buildRequest({
      requestId: `req_${(this.requestIndex++).toString(16).padStart(32, '0')}`,
      sequence: this.requestSequence++, nonce: this.nonce, issuedAtMs: NOW,
      deadlineAtMs: NOW + 5000, method, operationId,
      body: body(method, bodyOverrides), ...requestOverrides,
    });
  }
  async exchange(request, options = {}) {
    this.responses.expectResponse(request);
    const frame = await this.helper.receive(encodeEnvelope(request, this.key), options);
    if (frame === null) return null;
    return this.responses.receive(frame);
  }
}

test('helper slice is explicitly inert and machine contract has no activation path', async () => {
  assert.equal(ACTION_JOURNAL_HELPER_PRODUCTION_AVAILABLE, false);
  const contract = JSON.parse(await readFile(new URL('../../contracts/action-journal-helper/v0.1.0.json', import.meta.url), 'utf8'));
  for (const field of ['production_available', 'native_target_registered', 'node_integration_added', 'cmake_added', 'package_added', 'activation_permitted']) assert.equal(contract[field], false);
  assert.equal(contract.pipe.instances, 1);
  assert.match(contract.bootstrap.secret_policy, /never argv, environment, disk/);
});

test('helper refuses any client PID SID session creation-time path or image identity mismatch', async () => {
  for (const [field, value] of Object.entries({ pid: 4243, user_sid: 'S-1-5-21-1001', session_id: 8, creation_time: '1', image_path: 'C:\\Other\\node.exe', image_volume_serial: 'f'.repeat(16), image_file_id: 'f'.repeat(32) })) {
    const helper = new ActionJournalHelperReference({ device: await formattedDevice(), key: KEY, nonce: NONCE, expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => NOW });
    await helper.start({ issuer: ISSUER });
    assert.throws(() => helper.connect({ ...CLIENT, [field]: value }), error => error.code === 'client_identity_mismatch');
  }
});

test('bootstrap issuer is OS-derived and exact; forged parent, pipe server, or trust anchor is refused', async () => {
  for (const [field, value] of Object.entries({ parent_pid: 4001, pipe_server_pid: 4001, trust_anchor_digest: 'f'.repeat(64), image_file_id: 'e'.repeat(64), user_sid: 'S-1-5-21-1001' })) {
    const helper = new ActionJournalHelperReference({
      device: await formattedDevice(), key: KEY, nonce: NONCE,
      expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => NOW,
    });
    await assert.rejects(
      helper.start({ issuer: { ...ISSUER, [field]: value } }),
      error => error.code === 'bootstrap_issuer_untrusted',
    );
    assert.equal(helper.store, null);
  }
  const helper = new ActionJournalHelperReference({
    device: await formattedDevice(), key: KEY, nonce: NONCE,
    expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => NOW,
  });
  await assert.rejects(
    helper.start({ issuer: { ...ISSUER, pipe_server_pid: ISSUER.parent_pid + 1 } }),
    error => error.code === 'bootstrap_issuer_untrusted',
  );
});

test('startup scan has an exact deadline and cancellation probe before recovery access', async () => {
  let now = NOW;
  const device = await formattedDevice();
  const create = () => new ActionJournalHelperReference({
    device, key: KEY, nonce: NONCE, expectedClient: CLIENT,
    expectedIssuer: ISSUER, now: () => now,
  });
  const timed = create();
  await assert.rejects(timed.start({
    issuer: ISSUER,
    deadlineAtMs: NOW + 1,
    onStorageIo() { now = NOW + 1; },
  }), error => error.code === 'io_timeout');
  assert.equal(timed.store, null);
  now = NOW;
  const cancelled = create();
  const controller = new AbortController();
  await assert.rejects(cancelled.start({
    issuer: ISSUER,
    signal: controller.signal,
    onStorageIo() { controller.abort(); },
  }), error => error.code === 'io_cancel_failed');
  assert.equal(cancelled.store, null);
});

test('successful storage completion is rejected at deadline or after cancellation', () => {
  assert.equal(classifyBoundedStorageCompletion({
    success: true, cancelled: false, nowMs: NOW + 9, deadlineAtMs: NOW + 10,
  }), 'ok');
  assert.equal(classifyBoundedStorageCompletion({
    success: true, cancelled: false, nowMs: NOW + 10, deadlineAtMs: NOW + 10,
  }), 'io_timeout');
  assert.equal(classifyBoundedStorageCompletion({
    success: true, cancelled: true, nowMs: NOW + 9, deadlineAtMs: NOW + 10,
  }), 'cancelled');
});

test('startup probes the whole bank scan and final boundary for deadline and supervisor death', async () => {
  for (const mode of ['deadline', 'supervisor']) {
    let now = NOW;
    const controller = new AbortController();
    const helper = new ActionJournalHelperReference({
      device: await formattedDevice(), key: KEY, nonce: NONCE,
      expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => now,
    });
    await assert.rejects(helper.start({
      issuer: ISSUER,
      deadlineAtMs: NOW + 10,
      signal: controller.signal,
      onStorageIo(phase, scanIndex) {
        if (phase === 'during_startup_scan' && scanIndex === 2047) {
          if (mode === 'deadline') now = NOW + 10;
          else controller.abort();
        }
      },
    }), error => error.code === (mode === 'deadline' ? 'io_timeout' : 'io_cancel_failed'));
    assert.equal(helper.store, null);
  }
});

test('parsed bootstrap secret copies are wiped on invalid and successful paths', () => {
  for (const valid of [false, true]) {
    const state = modelParsedBootstrapSecretLifetime({ valid });
    assert.equal(state.accepted, valid);
    assert.deepEqual(state.parsedKey, new Uint8Array(32));
    assert.deepEqual(state.parsedNonce, new Uint8Array(16));
    assert.equal(state.outputKey.some(Boolean), valid);
    assert.equal(state.outputNonce.some(Boolean), valid);
  }
});

test('cancellation settlement classifies raced completion by actual settled bytes', () => {
  assert.equal(classifyCancelledPipeSettlement({
    settled: true, cancelStarted: false, cancelError: 'not_found',
    completionSucceeded: true, completionBytes: 12, completionError: null,
    timedOut: true,
  }), 'io_cancel_failed');
  assert.equal(classifyCancelledPipeSettlement({
    settled: true, cancelStarted: true, cancelError: null,
    completionSucceeded: false, completionBytes: 0,
    completionError: 'operation_aborted', timedOut: true,
  }), 'io_timeout');
});

test('startup recovery rechecks its shared deadline before each durable append', async () => {
  const device = await formattedDevice();
  let helper = await helperFor(device); let client = new ClientSession(helper);
  await client.exchange(client.request('prepare'));
  helper.close();
  let now = NOW;
  helper = new ActionJournalHelperReference({
    device, key: NEXT_KEY, nonce: NEXT_NONCE, expectedClient: CLIENT,
    expectedIssuer: ISSUER, now: () => now,
  });
  await assert.rejects(helper.start({
    issuer: ISSUER,
    deadlineAtMs: NOW + 1,
    onStorageIo(phase) {
      if (phase === 'before_startup_recovery_append') now = NOW + 1;
    },
  }), error => error.code === 'io_timeout');
  assert.equal(helper.recoveryCount, 0);
});

test('persisted authorization and resolution values are finite and unknown authority is rejected', () => {
  const base = { authorization_kind: null, resolution: null };
  assert.equal(validatePersistedAuthority(base), true);
  assert.equal(validatePersistedAuthority({ ...base, authorization_kind: 'user_confirmation' }), true);
  assert.equal(validatePersistedAuthority({ ...base, resolution: 'dispatch_ambiguous' }), true);
  for (const event of [
    { ...base, authorization_kind: '' },
    { ...base, authorization_kind: 'forged_admin' },
    { ...base, resolution: '' },
    { ...base, resolution: 'provider_said_ok' },
    { ...base, authorization_kind: 1 },
  ]) assert.equal(validatePersistedAuthority(event), false);
});

test('request at the exact deadline is rejected before mutation', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const request = client.request('prepare', null, {}, {
    issuedAtMs: NOW - 1,
    deadlineAtMs: NOW,
  });
  await assert.rejects(
    helper.receive(encodeEnvelope(request, KEY)),
    error => error.code === 'deadline_expired',
  );
  assert.equal(helper.store.summary().total, 0);
});

test('one client only and close zeroes session material and refuses reconnect', async () => {
  const helper = await helperFor();
  assert.throws(() => helper.connect(CLIENT), error => error.code === 'pipe_connect_failed');
  helper.close();
  assert.equal(helper.key.equals(Buffer.alloc(32)), true);
  assert.throws(() => helper.connect(CLIENT), error => error.code === 'pipe_connect_failed');
});

test('health stays unavailable and exposes no bootstrap or identity data', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const response = await client.exchange(client.request('health'));
  assert.deepEqual(response.body, { platform_available: false, production_enabled: false, recovery_count: 0, status: 'unavailable' });
  const serialized = JSON.stringify(response.body);
  for (const forbidden of [KEY.toString('hex'), NONCE, CLIENT.user_sid, CLIENT.image_path]) assert.equal(serialized.includes(forbidden), false);
});

test('prepare is deterministic and exact duplicate reuses only its still-prepared binding', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const firstRequest = client.request('prepare'); const first = await client.exchange(firstRequest);
  const second = await client.exchange(client.request('prepare'));
  assert.equal(second.operation_id, first.operation_id);
  assert.equal(second.body.receipt.receipt_digest, first.body.receipt.receipt_digest);
  const changed = await client.exchange(client.request('prepare', null, { preview_digest: DIGEST.f }));
  assert.equal(changed.error.code, 'invalid_transition');
  assert.equal(changed.operation_id, null);
});

test('authorize dispatch acknowledge and complete are exact durable transitions', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const prepared = await client.exchange(client.request('prepare')); const operation = prepared.operation_id;
  for (const [method, expected] of [['authorize', 'authorized'], ['dispatch', 'dispatching'], ['acknowledge', 'acknowledged'], ['complete', 'completed']]) {
    const response = await client.exchange(client.request(method, operation));
    assert.equal(response.state, expected);
  }
  const detail = helper.store.detail(operation);
  assert.deepEqual(detail.events.map(event => event.action), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('lost dispatch acknowledgement never permits dispatch replay and detail exposes exact committed state', async () => {
  const device = await formattedDevice(); const helper = await helperFor(device); const client = new ClientSession(helper);
  const prepared = await client.exchange(client.request('prepare')); const operation = prepared.operation_id;
  await client.exchange(client.request('authorize', operation));
  assert.equal(await client.exchange(client.request('dispatch', operation), { dropResponse: true }), null);
  helper.connect(CLIENT); const next = new ClientSession(helper);
  const replay = await next.exchange(next.request('dispatch', operation));
  assert.equal(replay.error.code, 'invalid_transition');
  const detail = await next.exchange(next.request('detail', operation));
  assert.equal(detail.body.receipt.state, 'dispatching');
  assert.deepEqual(detail.body.events.map(event => event.action), ['prepare', 'authorize', 'dispatch']);
});

test('startup recovery never guesses an external effect', async () => {
  const device = await formattedDevice(); let helper = await helperFor(device); let client = new ClientSession(helper);
  const prepared = await client.exchange(client.request('prepare')); const operation = prepared.operation_id;
  await client.exchange(client.request('authorize', operation));
  await client.exchange(client.request('dispatch', operation), { dropResponse: true });
  helper.close();
  helper = new ActionJournalHelperReference({ device, key: NEXT_KEY, nonce: NEXT_NONCE, expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => NOW });
  const status = await helper.start({ issuer: ISSUER }); helper.connect(CLIENT); client = new ClientSession(helper, NEXT_KEY, NEXT_NONCE);
  assert.equal(status.recovery_count, 1);
  const detail = await client.exchange(client.request('detail', operation));
  assert.equal(detail.body.receipt.state, 'unknown_manual');
  assert.equal(detail.body.receipt.recovery_required, true);
  assert.equal(detail.body.events.at(-1).action, 'startup_recovery');
});

test('prepared and authorized startup recovery cancel without dispatch', async () => {
  for (const authorize of [false, true]) {
    const device = await formattedDevice(); let helper = await helperFor(device); let client = new ClientSession(helper);
    const prepared = await client.exchange(client.request('prepare')); const operation = prepared.operation_id;
    if (authorize) await client.exchange(client.request('authorize', operation));
    helper.close(); helper = new ActionJournalHelperReference({ device, key: NEXT_KEY, nonce: NEXT_NONCE, expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => NOW });
    await helper.start({ issuer: ISSUER }); helper.connect(CLIENT); client = new ClientSession(helper, NEXT_KEY, NEXT_NONCE);
    const detail = await client.exchange(client.request('detail', operation));
    assert.equal(detail.body.receipt.state, 'cancelled'); assert.equal(detail.body.receipt.resolution, 'startup_recovery');
  }
});

test('cancellation before commit performs no mutation', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper); const controller = new AbortController(); controller.abort();
  const response = await client.exchange(client.request('prepare'), { signal: controller.signal });
  assert.equal(response.error.code, 'deadline_expired');
  assert.equal(helper.store.summary().total, 0);
});

test('cancellation during a commit cannot rewind it and requires detail', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper); const controller = new AbortController();
  const request = client.request('prepare');
  const response = await client.exchange(request, { signal: controller.signal, onBoundary(phase) { if (phase === 'after_commit_write') controller.abort(); } });
  assert.equal(response.error.code, 'commit_non_cancellable');
  const summary = helper.store.summary(); assert.equal(summary.total, 1); assert.equal(summary.records[0].state, 'prepared');
});

test('crash at every store write and flush boundary never fabricates a response', async () => {
  for (const boundary of ACTION_JOURNAL_CONTAINER_BOUNDARIES) {
    const device = await formattedDevice(); const helper = await helperFor(device); const client = new ClientSession(helper);
    const request = client.request('prepare');
    await assert.rejects(client.exchange(request, { onBoundary(phase) { if (phase === boundary) { device.crash(); throw new Error('simulated crash'); } } }));
    assert.equal(helper.client, null);
    try {
      const reopened = ActionJournalContainerReference.open(device);
      assert.ok([0, 1].includes(reopened.summary().total));
    } catch (error) {
      assert.ok(['container_corrupt_bank', 'container_conflicting_authority'].includes(error.code));
    }
  }
});

test('wrong MAC nonce sequence and replay close the session before mutation', async () => {
  for (const kind of ['mac', 'nonce', 'sequence', 'replay']) {
    const helper = await helperFor(); const request = new ClientSession(helper).request('prepare');
    let frame = encodeEnvelope(request, KEY);
    if (kind === 'mac') { frame = Buffer.from(frame); frame[frame.length - 3] ^= 1; }
    if (kind === 'nonce') frame = encodeEnvelope({ ...request, nonce: NEXT_NONCE }, KEY);
    if (kind === 'sequence') frame = encodeEnvelope({ ...request, sequence: 1 }, KEY);
    if (kind === 'replay') { await helper.receive(frame); frame = encodeEnvelope(request, KEY); }
    await assert.rejects(helper.receive(frame));
    assert.equal(helper.store.summary().total, kind === 'replay' ? 1 : 0);
  }
});

test('detail pagination is contiguous and predecessor-digest bound', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const prepared = await client.exchange(client.request('prepare')); const operation = prepared.operation_id;
  await client.exchange(client.request('authorize', operation)); await client.exchange(client.request('dispatch', operation));
  const first = await client.exchange(client.request('detail', operation, { limit: 2 }));
  assert.equal(first.body.truncated, true); assert.deepEqual(first.body.events.map(event => event.sequence), [0, 1]);
  const predecessor = first.body.events.at(-1);
  const second = await client.exchange(client.request('detail', operation, { after_sequence: 1, after_event_digest: journalEventDigest(predecessor), limit: 2 }));
  assert.equal(second.body.predecessor.sequence, 1); assert.deepEqual(second.body.events.map(event => event.sequence), [2]); assert.equal(second.body.truncated, false);
});

test('receipts and stored events contain only redacted finite evidence', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const request = client.request('prepare'); const response = await client.exchange(request);
  const detail = helper.store.detail(response.operation_id);
  const serialized = JSON.stringify({ response: response.body, summary: helper.store.summary(), events: detail.events });
  for (const value of [request.body.tool_name, request.body.call_ref, request.body.arguments_digest, CLIENT.user_sid, CLIENT.image_path, KEY.toString('hex'), NONCE]) assert.equal(serialized.includes(value), false);
});

test('response correlation remains bound to the exact request', async () => {
  const helper = await helperFor(); const client = new ClientSession(helper);
  const request = client.request('health');
  const frame = await helper.receive(encodeEnvelope(request, KEY));
  const wrong = { ...request, request_id: 'req_' + 'f'.repeat(32) };
  assert.throws(() => decodeFrame(frame, KEY, { expectedKind: 'response', expectedNonce: NONCE, expectedSequence: 0, seenRequestIds: new Set(), expectedResponse: wrong, nowMs: NOW }));
});
