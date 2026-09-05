import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  NATIVE_ACTION_JOURNAL_CLIENT_LIMITS,
  NativeActionJournalClient,
  PRODUCTION_NATIVE_ACTION_JOURNAL_CLIENT_AVAILABLE,
} from '../../host/agent/native-action-journal-client.mjs';
import {
  ACTION_JOURNAL_LIMITS,
  encodeEnvelope,
} from '../../host/agent/action-journal-protocol.mjs';
import {
  InMemoryJournalBlockDevice,
  formatJournalContainer,
} from '../reference/action-journal-container-model.mjs';
import { ActionJournalHelperReference } from '../reference/action-journal-helper-model.mjs';

const KEY = Buffer.from('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'hex');
const NONCE = '00112233445566778899aabbccddeeff';
const NOW = 1_700_000_000_000;
const DIGEST = Object.freeze({
  a: 'a'.repeat(64), b: 'b'.repeat(64), c: 'c'.repeat(64), d: 'd'.repeat(64),
});
const CLIENT = Object.freeze({
  creation_time: '133713371337', image_file_id: '0123456789abcdef0123456789abcdef',
  image_path: 'C:\\Approved\\node.exe', image_volume_serial: '0123456789abcdef',
  pid: 4242, session_id: 7, user_sid: 'S-1-5-21-1000',
});
const ISSUER = Object.freeze({
  creation_time: '133713371300', image_file_id: '1'.repeat(64),
  image_volume_serial: '1122334455667788', parent_pid: 4000,
  pipe_server_pid: 4000, session_id: 7, trust_anchor_digest: '2'.repeat(64),
  user_sid: 'S-1-5-21-1000',
});

function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function device() {
  const value = new InMemoryJournalBlockDevice();
  await formatJournalContainer(value, { containerId: Buffer.alloc(32, 0x42) });
  return value;
}

async function helperFor(blockDevice) {
  const helper = new ActionJournalHelperReference({
    device: blockDevice ?? await device(), key: KEY, nonce: NONCE,
    expectedClient: CLIENT, expectedIssuer: ISSUER, now: () => NOW,
  });
  await helper.start({ issuer: ISSUER });
  const response = helper._response.bind(helper);
  helper._response = (request, options = {}) => request.method === 'health'
    ? response(request, {
      body: {
        platform_available: true,
        production_enabled: false,
        recovery_count: helper.recoveryCount,
        status: 'ready',
      },
    })
    : response(request, options);
  return helper;
}

class HelperDuplex {
  constructor(helper, plan) {
    this.helper = helper;
    this.plan = plan;
    this.chunks = [];
    this.closed = false;
  }

  async write(frame, { signal } = {}) {
    this.plan.writeCalls = (this.plan.writeCalls ?? 0) + 1;
    if (this.closed || signal?.aborted) throw new Error('closed');
    if (this.plan.hangWrite) {
      await this.plan.hangWrite.promise;
      if (signal?.aborted) throw new Error('cancelled');
    }
    const request = JSON.parse(Buffer.from(frame).subarray(4).toString('utf8'));
    const dropResponse = this.plan.dropNext === true;
    this.plan.dropNext = false;
    const response = await this.helper.receive(frame, { dropResponse });
    if (response === null) return;
    let bytes = Buffer.from(response);
    if (this.plan.tamperNextDetail && request.method === 'detail') {
      this.plan.tamperNextDetail = false;
      const envelope = JSON.parse(bytes.subarray(4).toString('utf8'));
      const wrong = 'f'.repeat(64);
      envelope.body.events.at(-1).receipt_digest = wrong;
      envelope.body.receipt.receipt_digest = wrong;
      bytes = encodeEnvelope(envelope, KEY);
    }
    if (this.plan.splitNext && bytes.length > 8) {
      this.plan.splitNext = false;
      const middle = Math.floor(bytes.length / 2);
      this.chunks.push(bytes.subarray(0, middle).slice(), bytes.subarray(middle).slice());
    } else this.chunks.push(bytes);
  }

  async read({ signal } = {}) {
    if (this.plan.hangRead) {
      const pending = this.plan.hangRead;
      this.plan.hangRead = null;
      return pending.promise;
    }
    if (this.plan.oversizeNext) {
      this.plan.oversizeNext = false;
      return new Uint8Array(NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_response_bytes + 1);
    }
    if (this.closed || signal?.aborted || this.chunks.length === 0) return null;
    return this.chunks.shift();
  }

  async close({ signal } = {}) {
    this.plan.closeCalls = (this.plan.closeCalls ?? 0) + 1;
    this.plan.closeSignals ??= [];
    this.plan.closeSignals.push(signal);
    if (this.plan.hangClose) {
      const pending = this.plan.hangClose;
      this.plan.hangClose = null;
      await pending.promise;
    }
    if (signal?.aborted && this.plan.rejectAbortedClose) throw new Error('cancelled');
    if (this.plan.throwClose) throw new Error('untrusted-close-error');
    if (this.closed) return;
    this.closed = true;
    if (this.helper.client !== null) this.helper.disconnect();
    this.chunks.length = 0;
  }
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function factoryFor(helper, plan = {}) {
  let connections = 0;
  const factory = async () => {
    connections++;
    helper.responseSequence = 0;
    helper.connect(CLIENT);
    const key = new Uint8Array(KEY);
    plan.sourceKeys ??= [];
    plan.sourceKeys.push(key);
    return { key, nonce: NONCE, transport: new HelperDuplex(helper, plan) };
  };
  return { factory, count: () => connections, plan };
}

function requestIds() {
  let value = 0;
  return () => `req_${(++value).toString(16).padStart(32, '0')}`;
}

async function openClient(helper, plan = {}, options = {}) {
  const connection = factoryFor(helper, plan);
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: connection.factory,
    now: () => NOW,
    requestIdFactory: requestIds(),
    ...options,
  });
  return { client, connection };
}

function prepareInput(overrides = {}) {
  return {
    requestId: 'request_00000001', callId: 'call_0000000001',
    toolName: 'coding.copilot_ask', riskTier: 'T3', sideEffect: 'cloud_inference',
    argumentsDigest: DIGEST.a, previewDigest: DIGEST.b, operationDigest: DIGEST.c,
    ...overrides,
  };
}

test('slice is immutable false, test-only, absent from host, and contract is honest', async () => {
  assert.equal(PRODUCTION_NATIVE_ACTION_JOURNAL_CLIENT_AVAILABLE, false);
  assert.throws(() => new NativeActionJournalClient({ connectionFactory() {} }), /test-only/u);
  const contract = JSON.parse(await readFile(new URL('../../contracts/action-journal-client/v0.1.0.json', import.meta.url), 'utf8'));
  for (const field of ['production_available', 'host_import_added', 'native_transport_added', 'pipe_discovery_added', 'process_launch_added', 'package_added', 'availability_changed']) {
    assert.equal(contract[field], false);
  }
  assert.equal(contract.cleanup_claim.immutable_js_strings_or_prior_factory_copies_erased, false);
  assert.equal(contract.limits.max_detail_events, ACTION_JOURNAL_LIMITS.max_detail_events);
  assert.equal(contract.limits.max_detail_events, 16);
  assert.equal(contract.limits.max_events_per_operation, ACTION_JOURNAL_LIMITS.max_events_per_operation);
  assert.equal(contract.limits.max_events_per_operation, 16);
  const host = await readFile(new URL('../../lae-host.mjs', import.meta.url), 'utf8');
  assert.equal(host.includes('native-action-journal-client'), false);
});

test('controller-shaped transitions prove exact helper receipts and redact session material', async t => {
  const helper = await helperFor();
  const plan = { splitNext: true };
  const { client } = await openClient(helper, plan);
  t.after(() => client.close());
  assert.equal(plan.sourceKeys[0].byteLength, 0, 'factory source key view must be detached');
  const prepared = await client.prepare(prepareInput());
  const authorized = await client.authorize(prepared.operation_id, 'user_confirmation');
  const dispatched = await client.dispatch(prepared.operation_id);
  const acknowledged = await client.acknowledge(prepared.operation_id);
  const completed = await client.complete(prepared.operation_id);
  assert.deepEqual([prepared.state, authorized.state, dispatched.state, acknowledged.state, completed.state],
    ['prepared', 'authorized', 'dispatching', 'acknowledged', 'completed']);
  const detail = await client.detail(prepared.operation_id);
  assert.deepEqual(detail.events.map(event => event.action), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
  const output = JSON.stringify({ health: client.health(), prepared, detail });
  for (const secret of [KEY.toString('hex'), NONCE, CLIENT.image_path, CLIENT.user_sid, 'request_00000001', 'call_0000000001']) {
    assert.equal(output.includes(secret), false);
  }
});

test('lost acknowledgement accepts only the exact attempted event digest and never replays', async t => {
  const helper = await helperFor();
  const plan = {};
  const { client, connection } = await openClient(helper, plan);
  t.after(() => client.close());
  const prepared = await client.prepare(prepareInput());
  await client.authorize(prepared.operation_id, 'user_confirmation');
  plan.dropNext = true;
  const dispatched = await client.dispatch(prepared.operation_id);
  assert.equal(dispatched.state, 'dispatching');
  assert.equal(connection.count(), 2);
  assert.equal(helper.store.detail(prepared.operation_id).events.filter(event => event.action === 'dispatch').length, 1);
});

test('protocol-valid terminal state with a different durable event digest cannot forge lost ACK proof', async () => {
  const helper = await helperFor();
  const plan = {};
  const { client } = await openClient(helper, plan);
  const prepared = await client.prepare(prepareInput());
  plan.dropNext = true;
  plan.tamperNextDetail = true;
  await assert.rejects(client.cancel(prepared.operation_id), error => error.code === 'action_journal_commit_unknown');
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_commit_unknown' });
  await client.close();
});

test('lost prepare acknowledgement permits one exact deterministic retry', async t => {
  const helper = await helperFor();
  const plan = {};
  const { client, connection } = await openClient(helper, plan);
  t.after(() => client.close());
  plan.dropNext = true;
  const receipt = await client.prepare(prepareInput());
  assert.equal(receipt.state, 'prepared');
  assert.equal(connection.count(), 2);
  assert.equal(helper.store.detail(receipt.operation_id).events.length, 1);
});

test('same tool and arguments cannot gain a second active authority through new request/call IDs', async t => {
  const helper = await helperFor();
  const { client } = await openClient(helper);
  t.after(() => client.close());
  const first = await client.prepare(prepareInput());
  await assert.rejects(client.prepare(prepareInput({
    requestId: 'request_00000002', callId: 'call_0000000002',
    previewDigest: DIGEST.d, operationDigest: DIGEST.d,
  })), error => error.code === 'action_journal_duplicate_active');
  assert.equal(helper.store.summary().records.length, 1);
  await client.cancel(first.operation_id);
  const second = await client.prepare(prepareInput({
    requestId: 'request_00000003', callId: 'call_0000000003', operationDigest: DIGEST.d,
  }));
  assert.notEqual(second.operation_id, first.operation_id);
});

test('hostile identifier objects are rejected without coercion before helper mutation', async t => {
  const helper = await helperFor();
  const { client } = await openClient(helper);
  t.after(() => client.close());
  let coerced = false;
  const hostile = { toString() { coerced = true; throw new Error('untrusted coercion'); } };
  await assert.rejects(client.prepare(prepareInput({ toolName: hostile })), error => error.code === 'action_journal_invalid_request');
  assert.equal(coerced, false);
  assert.equal(helper.store.summary().records.length, 0);
});

test('startup with an unreconstructable active binding blocks every new prepare', async () => {
  const helper = await helperFor();
  const { client: first } = await openClient(helper);
  await first.prepare(prepareInput());
  await first.close();
  const { client: second } = await openClient(helper);
  assert.deepEqual(second.health(), { state: 'blocked', error: 'action_journal_recovery_incomplete' });
  await assert.rejects(second.prepare(prepareInput({ operationDigest: DIGEST.d })), error => error.code === 'action_journal_recovery_incomplete');
  await second.close();
});

test('unknown startup recovery stays blocked until explicit manual resolution', async t => {
  const blockDevice = await device();
  const firstHelper = await helperFor(blockDevice);
  const { client: first } = await openClient(firstHelper);
  const prepared = await first.prepare(prepareInput());
  await first.authorize(prepared.operation_id, 'user_confirmation');
  await first.dispatch(prepared.operation_id);
  await first.close();
  firstHelper.close();
  const secondHelper = await helperFor(blockDevice);
  const { client } = await openClient(secondHelper);
  t.after(() => client.close());
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_recovery_required' });
  const resolved = await client.resolve(prepared.operation_id, 'failed_definitive');
  assert.equal(resolved.resolution, 'manual_failed_definitive');
  assert.deepEqual(client.health(), { state: 'ready', error: null });
});

test('oversize chunk is rejected before decoder accumulation', async () => {
  const helper = await helperFor();
  const plan = {};
  const { client } = await openClient(helper, plan);
  plan.oversizeNext = true;
  await assert.rejects(client.summary(), error => error.code === 'action_journal_transport_unavailable');
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_transport_unavailable' });
  await client.close();
});

test('a timed-out uncooperative read cannot mutate after a returned failure', async () => {
  const helper = await helperFor();
  const plan = {};
  const { client } = await openClient(helper, plan, { deadlineMs: 1500, settlementGraceMs: 5 });
  const pendingRead = deferred();
  plan.hangRead = pendingRead;
  let returned = false;
  const query = client.summary().finally(() => { returned = true; });
  while (plan.hangRead !== null) await delay(1);
  await delay(1800);
  assert.equal(returned, false);
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_transport_settlement_unproven' });
  pendingRead.resolve(null);
  await assert.rejects(query, error => error.code === 'action_journal_transport_settlement_unproven');
  await client.close();
});

test('a late factory result is closed and settled before open returns', async () => {
  const helper = await helperFor();
  const gate = deferred();
  const sourceKey = new Uint8Array(KEY);
  let returned = false;
  const opening = NativeActionJournalClient.open({
    testOnly: true,
    deadlineMs: 20,
    settlementGraceMs: 5,
    now: () => NOW,
    requestIdFactory: requestIds(),
    connectionFactory: async () => {
      await gate.promise;
      helper.responseSequence = 0;
      helper.connect(CLIENT);
      return { key: sourceKey, nonce: NONCE, transport: new HelperDuplex(helper, {}) };
    },
  }).then(client => { returned = true; return client; });
  await delay(30);
  assert.equal(returned, false);
  gate.resolve();
  const client = await opening;
  assert.equal(helper.client, null);
  assert.deepEqual(sourceKey, new Uint8Array(32), 'timed-out untransferred source key is zeroed');
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_transport_unavailable' });
  await client.close();
});

test('a late write cannot mutate or reconnect until the write settles', async t => {
  const helper = await helperFor();
  const plan = {};
  const { client, connection } = await openClient(helper, plan, { deadlineMs: 1500, settlementGraceMs: 5 });
  t.after(() => client.close());
  const pendingWrite = deferred();
  plan.hangWrite = pendingWrite;
  let returned = false;
  const preparing = client.prepare(prepareInput()).finally(() => { returned = true; });
  await delay(1600);
  assert.equal(returned, false);
  assert.equal(connection.count(), 1);
  assert.equal(helper.store.summary().records.length, 0);
  pendingWrite.resolve();
  await assert.rejects(preparing, error => error.code === 'action_journal_transport_settlement_unproven');
  assert.equal(connection.count(), 1);
  assert.equal(helper.store.summary().records.length, 0);
});

test('close awaits an uncooperative injected close before returning and zeroing ownership', async () => {
  const helper = await helperFor();
  const plan = {};
  const { client } = await openClient(helper, plan, { deadlineMs: 1500, settlementGraceMs: 5 });
  const pendingClose = deferred();
  plan.hangClose = pendingClose;
  let returned = false;
  const close = client.close().then(() => { returned = true; });
  await delay(1510);
  assert.equal(returned, false);
  assert.equal(plan.closeSignals[0].aborted, true, 'cleanup timeout aborts the one close call');
  pendingClose.resolve();
  await close;
  assert.equal(returned, true);
  assert.equal(plan.closeCalls, 1);
});

test('close is a monotonic cleanup transaction after the operation deadline and consults no injected clock', async () => {
  const helper = await helperFor();
  const plan = {};
  let clockCalls = 0;
  const connection = factoryFor(helper, plan);
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: connection.factory,
    deadlineMs: 100,
    settlementGraceMs: 5,
    now: () => { clockCalls++; return NOW; },
    requestIdFactory: requestIds(),
  });
  const beforeClose = clockCalls;
  await delay(150);
  const closing = client.close();
  await closing;
  assert.equal(clockCalls, beforeClose, 'close uses monotonic timing only');
  assert.equal(plan.closeCalls, 1);
  assert.equal(helper.client, null);
  assert.equal(client.close(), closing, 'repeated close shares the same promise');
});

test('concurrent close callers share one exactly-once cleanup transaction', async () => {
  const helper = await helperFor();
  const plan = {};
  const { client } = await openClient(helper, plan);
  const first = client.close();
  const second = client.close();
  assert.equal(first, second);
  await first;
  assert.equal(plan.closeCalls, 1);
  assert.equal(helper.client, null);
});

test('close rejection retains poisoned ownership and blocks reuse or reconnect', async () => {
  const helper = await helperFor();
  const plan = { throwClose: true };
  const connection = factoryFor(helper, plan);
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: connection.factory,
    now: () => NOW,
    requestIdFactory: requestIds(),
  });
  const first = client.close();
  await assert.rejects(first, error => error.code === 'action_journal_close_unproven');
  assert.equal(client.close(), first);
  assert.equal(plan.closeCalls, 1, 'rejected close is never retried');
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_close_unproven' });
  await assert.rejects(client.prepare(prepareInput()), error => error.code === 'action_journal_unavailable');
  assert.equal(connection.count(), 1);
  assert.notEqual(helper.client, null, 'poisoned ownership remains attached for manual recovery');
});

test('close request cancels active lost-ACK recovery before reconnect or a second action', async () => {
  const helper = await helperFor();
  const plan = {};
  const { client, connection } = await openClient(helper, plan);
  const pendingRead = deferred();
  plan.dropNext = true;
  plan.hangRead = pendingRead;
  const preparing = client.prepare(prepareInput());
  while (plan.hangRead !== null) await delay(1);
  const closing = client.close();
  await assert.rejects(client.summary(), error => error.code === 'action_journal_unavailable');
  assert.equal(connection.count(), 1, 'close request prevents recovery reconnect');
  assert.equal(plan.writeCalls, 3, 'only startup queries and the one attempted prepare were written');
  pendingRead.resolve(null);
  await assert.rejects(preparing, error => error.code === 'action_journal_close_requested');
  await closing;
  assert.equal(connection.count(), 1);
  assert.equal(plan.writeCalls, 3, 'no I/O occurs after close owns settlement');
});

test('close routes a fulfilled reconnect factory result that arrives after close request', async () => {
  const helper = await helperFor();
  const plan = {};
  let calls = 0;
  const gate = deferred();
  const sourceKey = new Uint8Array(KEY);
  const factory = async () => {
    calls++;
    if (calls > 1) {
      await gate.promise;
      helper.responseSequence = 0;
      helper.connect(CLIENT);
      return { key: sourceKey, nonce: NONCE, transport: new HelperDuplex(helper, plan) };
    }
    helper.responseSequence = 0;
    helper.connect(CLIENT);
    return { key: new Uint8Array(KEY), nonce: NONCE, transport: new HelperDuplex(helper, plan) };
  };
  const client = await NativeActionJournalClient.open({
    testOnly: true, connectionFactory: factory, now: () => NOW, requestIdFactory: requestIds(),
  });
  plan.dropNext = true;
  const pendingRead = deferred();
  plan.hangRead = pendingRead;
  const preparing = client.prepare(prepareInput());
  while (plan.hangRead !== null) await delay(1);
  pendingRead.resolve(null);
  while (calls < 2) await delay(1);
  const closing = client.close();
  await assert.rejects(client.summary(), error => error.code === 'action_journal_unavailable');
  gate.resolve();
  await assert.rejects(preparing, error => error.code === 'action_journal_close_requested');
  await closing;
  assert.deepEqual(sourceKey, new Uint8Array(32), 'fulfilled raw reconnect is closed and zeroed after close routing');
  assert.equal(calls, 2);
});

test('receiver processing reentrant close cannot return authenticated query data', async () => {
  const helper = await helperFor();
  const plan = {};
  let client;
  let armed = false;
  let closePromise;
  const now = () => {
    if (armed && !closePromise) closePromise = client.close();
    return NOW;
  };
  client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: factoryFor(helper, plan).factory,
    now,
    requestIdFactory: requestIds(),
  });
  armed = true;
  await assert.rejects(client.summary(), error => error.code === 'action_journal_close_requested');
  await closePromise;
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_unavailable' });
});

test('rejecting malformed factory close retains poisoned transport and standalone key', async () => {
  const sourceKey = new Uint8Array(KEY);
  let closeCalls = 0;
  const malformed = {
    key: sourceKey,
    nonce: NONCE,
    extra: true,
    transport: {
      write() {},
      read() {},
      close() { closeCalls++; throw new Error('close-rejection-secret'); },
    },
  };
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: async () => malformed,
    now: () => NOW,
    requestIdFactory: requestIds(),
  });
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_close_unproven' });
  assert.equal(closeCalls, 1, 'malformed transport close is attempted exactly once');
  assert.deepEqual(sourceKey, new Uint8Array(KEY), 'key is retained while close is unproven');
  await assert.rejects(client.close(), error => error.code === 'action_journal_close_unproven');
  assert.equal(closeCalls, 1, 'poisoned malformed transport is never retried');
});

test('a pre-transport clock failure is finite and cannot trigger mutation recovery', async t => {
  const helper = await helperFor();
  const connection = factoryFor(helper);
  let hostile = false;
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: connection.factory,
    now: () => {
      if (hostile) throw new Error('clock-secret');
      return NOW;
    },
    requestIdFactory: requestIds(),
  });
  t.after(() => client.close());
  assert.deepEqual(client.health(), { state: 'ready', error: null });
  hostile = true;
  await assert.rejects(client.prepare(prepareInput()), error => {
    assert.equal(error.code, 'action_journal_prewrite_invalid');
    assert.equal(error.message, 'action_journal_prewrite_invalid');
    return true;
  });
  assert.equal(connection.count(), 1, 'pre-write clock errors do not reconnect/recover');
  assert.equal(helper.store.summary().records.length, 0);
});

test('a pre-transport request-id failure is finite and does not become ambiguous', async t => {
  const helper = await helperFor();
  const connection = factoryFor(helper);
  let hostile = false;
  const goodIds = requestIds();
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: connection.factory,
    now: () => NOW,
    requestIdFactory: () => {
      if (hostile) throw new Error('request-id-secret');
      return goodIds();
    },
  });
  t.after(() => client.close());
  assert.deepEqual(client.health(), { state: 'ready', error: null });
  hostile = true;
  await assert.rejects(client.prepare(prepareInput()), error => error.code === 'action_journal_prewrite_invalid');
  assert.equal(connection.count(), 1);
  assert.equal(helper.store.summary().records.length, 0);
});

test('accessor and nested proxy factory results are rejected without invoking accessors or exposing keys', async () => {
  const sourceKey = new Uint8Array(KEY);
  let keyAccessorCalled = false;
  const accessorResult = {};
  Object.defineProperty(accessorResult, 'key', {
    enumerable: true,
    get() { keyAccessorCalled = true; throw new Error('key-secret'); },
  });
  Object.defineProperty(accessorResult, 'nonce', { enumerable: true, value: NONCE });
  Object.defineProperty(accessorResult, 'transport', {
    enumerable: true,
    get() { throw new Error('transport-secret'); },
  });
  const accessorClient = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: async () => accessorResult,
    now: () => NOW,
    requestIdFactory: requestIds(),
  });
  assert.equal(keyAccessorCalled, false);
  assert.deepEqual(accessorClient.health(), { state: 'blocked', error: 'action_journal_transport_unavailable' });
  await accessorClient.close();

  const nestedKey = new Uint8Array(KEY);
  const nestedTransport = { write() {}, read() {}, close() {} };
  const proxiedTransport = new Proxy(nestedTransport, {
    getOwnPropertyDescriptor() { throw new Error('nested-descriptor-secret'); },
    getPrototypeOf() { throw new Error('nested-prototype-secret'); },
  });
  const proxied = { key: nestedKey, nonce: NONCE, transport: proxiedTransport };
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: async () => proxied,
    now: () => NOW,
    requestIdFactory: requestIds(),
  });
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_transport_unavailable' });
  assert.deepEqual(nestedKey, new Uint8Array(32), 'safely accessible key is zeroed after nested trap');
  await client.close();
});

test('a root proxy get trap cannot cause a post-inspection nonce reread', async t => {
  const helper = await helperFor();
  let nonceReads = 0;
  const connectionFactory = async () => {
    helper.responseSequence = 0;
    helper.connect(CLIENT);
    const target = {
      key: new Uint8Array(KEY),
      nonce: NONCE,
      transport: new HelperDuplex(helper, {}),
    };
    return new Proxy(target, {
      get(object, property, receiver) {
        if (property === 'nonce') {
          nonceReads++;
          throw new Error('nonce-reread-secret');
        }
        return Reflect.get(object, property, receiver);
      },
    });
  };
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory,
    now: () => NOW,
    requestIdFactory: requestIds(),
  });
  t.after(() => client.close());
  assert.equal(nonceReads, 0);
  assert.equal(client.health().state, 'ready');
});

test('disconnect closes its detached connection without consulting a hostile clock', async () => {
  const helper = await helperFor();
  let hostile = false;
  const connection = factoryFor(helper);
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: connection.factory,
    now: () => {
      if (hostile) throw new Error('disconnect-clock-secret');
      return NOW;
    },
    requestIdFactory: requestIds(),
  });
  hostile = true;
  await client.close();
  assert.equal(helper.client, null, 'detached transport is closed despite clock failure');
});

test('malformed factory result retains standalone key while close is unproven', async () => {
  const sourceKey = new Uint8Array(KEY);
  const malformed = { key: sourceKey, nonce: NONCE, extra: true, transport: { write() {}, read() {}, close() {
    throw new Error('close-secret');
  } } };
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: async () => malformed,
    now: () => NOW,
    requestIdFactory: requestIds(),
  });
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_close_unproven' });
  assert.deepEqual(sourceKey, new Uint8Array(KEY));
  await assert.rejects(client.close(), error => error.code === 'action_journal_close_unproven');
});

test('clock failure after factory fulfillment still closes the raw transport and zeroes its key', async () => {
  const sourceKey = new Uint8Array(KEY);
  let clockCalls = 0;
  let closeCalls = 0;
  const transport = {
    write() {},
    read() {},
    close() { closeCalls++; },
  };
  const client = await NativeActionJournalClient.open({
    testOnly: true,
    connectionFactory: async () => ({ key: sourceKey, nonce: NONCE, transport }),
    now: () => {
      clockCalls++;
      if (clockCalls >= 3) throw new Error('late-clock-secret');
      return NOW;
    },
    requestIdFactory: requestIds(),
  });
  assert.deepEqual(client.health(), { state: 'blocked', error: 'action_journal_transport_unavailable' });
  assert.ok(clockCalls >= 3);
  assert.equal(closeCalls, 1);
  assert.deepEqual(sourceKey, new Uint8Array(32));
  await client.close();
});

test('source orders chunk bounds before decoder copy and contains no production discovery', async () => {
  const source = await readFile(new URL('../../host/agent/native-action-journal-client.mjs', import.meta.url), 'utf8');
  const guard = source.indexOf('chunk.byteLength > NATIVE_ACTION_JOURNAL_CLIENT_LIMITS.max_response_bytes - responseBytes');
  const copy = source.indexOf('this.#decoder.push(chunk)', guard);
  assert.ok(guard > 0 && copy > guard);
  for (const forbidden of ['CreateNamedPipe', 'child_process', 'spawn(', 'execFile(', 'PRODUCTION_NATIVE_ACTION_JOURNAL_CLIENT_AVAILABLE = true']) {
    assert.equal(source.includes(forbidden), false);
  }
});
