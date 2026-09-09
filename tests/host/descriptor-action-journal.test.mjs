import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { chmod, link, mkdtemp, open, readFile, rm, truncate, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createActionBinding, DescriptorActionJournal } from '../../host/agent/action-journal.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { createMicrosoftGraphTools, MicrosoftGraphProvider } from '../../host/providers/microsoft-graph.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { createHostComposition } from '../../lae-host.mjs';

const REQUEST = 'request_descriptor01';
const CALL = 'call_descriptor001';
const SECRET = 'descriptor-secret-must-not-persist';
const WAL_HEADER = Buffer.from('{"format":"lae-action-journal-wal","version":2}\n');
const FRAME_HEADER_BYTES = 145;

function encodedFrame(payloadText) {
  const payload = Buffer.from(payloadText, 'utf8'); const length = payload.length.toString(16).padStart(8, '0'); const payloadDigest = createHash('sha256').update(payload).digest('hex');
  const unsignedHeader = `@LAE2:${length}:${payloadDigest}:`; const headerDigest = createHash('sha256').update(unsignedHeader, 'ascii').digest('hex');
  return Buffer.concat([Buffer.from(`${unsignedHeader}${headerDigest}:`, 'ascii'), payload, Buffer.from(':COMMIT\n', 'ascii')]);
}

function frameOffsets(raw) {
  const offsets = []; let offset = WAL_HEADER.length;
  while (offset < raw.length) { const length = Number.parseInt(raw.subarray(offset + 6, offset + 14).toString('ascii'), 16); offsets.push({ offset, length }); offset += FRAME_HEADER_BYTES + length + 8; }
  assert.equal(offset, raw.length); return offsets;
}

async function wal(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'lae-descriptor-journal-'));
  const path = join(directory, 'journal.wal');
  const handle = await open(path, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR, 0o600);
  let current = null;
  t.after(async () => { await current?.close().catch(() => {}); await handle.close().catch(() => {}); await rm(directory, { recursive: true, force: true }); });
  let tick = 0; let id = 0;
  const openJournal = async extra => {
    current = await DescriptorActionJournal.open({ fd: handle.fd, now: () => new Date(Date.UTC(2026, 8, 9, 0, 0, tick++)).toISOString(), idFactory: () => `act_${(++id).toString(16).padStart(32, '0')}`, ...options, ...extra });
    return current;
  };
  return { path, handle, openJournal, current: () => current };
}

function journalInput(overrides = {}) {
  const value = { requestId: REQUEST, callId: CALL, toolName: 'mail.create_draft', riskTier: 'T2', sideEffect: 'create_draft', arguments: { to: ['a@example.com'], subject: 'subject', body: SECRET }, preview: { subject: 'subject' }, ...overrides };
  const binding = createActionBinding({ requestId: value.requestId, callId: value.callId, toolName: value.toolName, arguments: value.arguments, preview: value.preview });
  return { requestId: value.requestId, callId: value.callId, toolName: value.toolName, riskTier: value.riskTier, sideEffect: value.sideEffect, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest };
}

async function dispatch(journal, overrides = {}) {
  const receipt = await journal.prepare(journalInput(overrides));
  await journal.authorize(receipt.operation_id, 'user_confirmation');
  await journal.dispatch(receipt.operation_id);
  return receipt;
}

function graphEngine(args = { to: ['a@example.com'], subject: 'subject', body: SECRET }) {
  return { async *generate({ messages }) {
    if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: CALL, name: 'mail.create_draft', arguments: args }) }; return; }
    yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' };
  } };
}

async function runGraph({ journal, tool, args, requestId = REQUEST }) {
  const events = [];
  const controller = new ConversationController({ engine: graphEngine(args), actionJournal: journal, toolRegistry: { 'mail.create_draft': tool }, confirmationTimeoutMs: 1000 });
  const pending = controller.runTurn({ sessionId: 'session_descriptor01', requestId, message: 'create draft', onEvent: event => {
    events.push(event);
    if (event.event === 'tool.confirmation_required') queueMicrotask(() => controller.confirm(event.data.confirmation_id, true, { requestId, callId: CALL }));
  } });
  return { result: await pending, events };
}

test('descriptor WAL persists only digest-bound events and reopens from the same authority', async t => {
  const store = await wal(t); const journal = await store.openJournal();
  assert.deepEqual(journal.health(), { state: 'ready', error: null });
  const receipt = await dispatch(journal); await journal.acknowledge(receipt.operation_id); await journal.complete(receipt.operation_id);
  const first = await journal.detail(receipt.operation_id); assert.deepEqual(first.events.map(event => event.state), ['prepared', 'authorized', 'dispatching', 'acknowledged', 'completed']);
  const raw = await readFile(store.path, 'utf8'); assert.match(raw, /^\{"format":"lae-action-journal-wal","version":2\}\n/u);
  for (const forbidden of [SECRET, REQUEST, CALL, 'a@example.com']) assert.equal(raw.includes(forbidden), false, forbidden);
  await journal.close(); const reopened = await store.openJournal();
  assert.deepEqual(reopened.health(), { state: 'ready', error: null }); assert.equal((await reopened.detail(receipt.operation_id)).receipt_hash, first.receipt_hash);
});

test('descriptor access probes reject read-only and write-only handles without changing bytes', async t => {
  if (process.platform === 'win32') return t.skip('POSIX descriptor access-mode probe');
  for (const [name, flags] of [['read-only', constants.O_RDONLY], ['write-only', constants.O_WRONLY]]) await t.test(name, async t => {
    const directory = await mkdtemp(join(tmpdir(), 'lae-descriptor-access-')); const path = join(directory, 'journal.wal');
    const original = Buffer.from(`unchanged-${name}`, 'utf8'); await writeFile(path, original, { mode: 0o600 });
    const handle = await open(path, flags); t.after(async () => { await handle.close().catch(() => {}); await rm(directory, { recursive: true, force: true }); });
    const journal = await DescriptorActionJournal.open({ fd: handle.fd });
    assert.deepEqual(journal.health(), { state: 'blocked', error: 'action_journal_permissions_invalid' });
    assert.deepEqual(await readFile(path), original); await journal.close(); assert.deepEqual(await readFile(path), original);
  });
});

test('only an exact incomplete final frame is truncated; complete or noncanonical corruption blocks', async t => {
  for (const damage of ['partial-magic', 'partial-length', 'partial-checksum', 'partial-header-digest', 'partial-payload', 'partial-commit', 'complete-checksum', 'committed-length-mismatch', 'noncanonical']) await t.test(damage, async t => {
    const store = await wal(t); const journal = await store.openJournal(); await journal.prepare(journalInput()); await journal.close();
    const raw = await readFile(store.path); const headerLength = WAL_HEADER.length; const payloadLength = frameOffsets(raw)[0].length;
    if (damage === 'partial-magic') await truncate(store.path, headerLength + 3);
    else if (damage === 'partial-length') await truncate(store.path, headerLength + 10);
    else if (damage === 'partial-checksum') await truncate(store.path, headerLength + 40);
    else if (damage === 'partial-header-digest') await truncate(store.path, headerLength + 100);
    else if (damage === 'partial-payload') await truncate(store.path, headerLength + FRAME_HEADER_BYTES + Math.floor(payloadLength / 2));
    else if (damage === 'partial-commit') await truncate(store.path, raw.length - 3);
    else if (damage === 'complete-checksum') { raw[headerLength + FRAME_HEADER_BYTES] ^= 1; await writeFile(store.path, raw); }
    else if (damage === 'committed-length-mismatch') {
      const payload = raw.subarray(headerLength + FRAME_HEADER_BYTES, headerLength + FRAME_HEADER_BYTES + payloadLength); const length = (payloadLength + 1).toString(16).padStart(8, '0'); const payloadDigest = createHash('sha256').update(payload).digest('hex');
      const unsignedHeader = `@LAE2:${length}:${payloadDigest}:`; const headerDigest = createHash('sha256').update(unsignedHeader, 'ascii').digest('hex');
      await writeFile(store.path, Buffer.concat([raw.subarray(0, headerLength), Buffer.from(`${unsignedHeader}${headerDigest}:`), payload, Buffer.from(':COMMIT\n')]));
    }
    else {
      const payload = raw.subarray(headerLength + FRAME_HEADER_BYTES, headerLength + FRAME_HEADER_BYTES + payloadLength).toString('utf8').replace(',"operation_id"', ', "operation_id"');
      assert.notEqual(payload.length, payloadLength);
      await writeFile(store.path, Buffer.concat([raw.subarray(0, headerLength), encodedFrame(payload)]));
    }
    const reopened = await store.openJournal();
    if (damage.startsWith('partial-')) {
      assert.deepEqual(reopened.health(), { state: 'ready', error: null }); assert.equal((await reopened.summary()).records.length, 0); assert.equal((await readFile(store.path)).length, headerLength);
    } else assert.deepEqual(reopened.health(), { state: 'blocked', error: 'action_journal_corrupt' });
  });
});

test('every committed frame-header byte mutation blocks and a length flip cannot erase a dispatch tombstone', async t => {
  const store = await wal(t); const journal = await store.openJournal(); const receipt = await dispatch(journal); assert.equal((await journal.detail(receipt.operation_id)).state, 'dispatching'); await journal.close();
  const raw = await readFile(store.path); const dispatchFrame = frameOffsets(raw).at(-1);
  for (let index = 0; index < FRAME_HEADER_BYTES; index++) {
    const mutated = Buffer.from(raw); const position = dispatchFrame.offset + index; const character = String.fromCharCode(mutated[position]);
    mutated[position] = /^[0-9a-f]$/u.test(character) ? (character === '0' ? '1' : '0').charCodeAt(0) : (mutated[position] ^ 1);
    await writeFile(store.path, mutated); const blocked = await store.openJournal();
    assert.deepEqual(blocked.health(), { state: 'blocked', error: 'action_journal_corrupt' }, `header byte ${index}`);
    assert.equal((await readFile(store.path)).length, raw.length, `header byte ${index} truncated`); await blocked.close();
  }
  const exploit = Buffer.from(raw); const lengthText = exploit.subarray(dispatchFrame.offset + 6, dispatchFrame.offset + 14).toString('ascii');
  let changedLength = null;
  for (let index = 7; index >= 0 && changedLength === null; index--) {
    const candidate = `${lengthText.slice(0, index)}f${lengthText.slice(index + 1)}`; const value = Number.parseInt(candidate, 16);
    if (candidate !== lengthText && value > dispatchFrame.length && value <= 65536) changedLength = candidate;
  }
  assert.ok(changedLength); exploit.write(changedLength, dispatchFrame.offset + 6, 'ascii'); await writeFile(store.path, exploit);
  const blocked = await store.openJournal(); assert.deepEqual(blocked.health(), { state: 'blocked', error: 'action_journal_corrupt' });
  await assert.rejects(blocked.prepare(journalInput()), error => error?.code === 'action_journal_corrupt');
  assert.equal((await readFile(store.path)).length, raw.length);
});

test('restart cancels pre-dispatch work, tombstones dispatched work, and never manufactures completion', async t => {
  const store = await wal(t); const journal = await store.openJournal();
  const prepared = await journal.prepare(journalInput()); await journal.authorize(prepared.operation_id, 'user_confirmation');
  const sent = await dispatch(journal, { requestId: 'request_descriptor02', callId: 'call_descriptor002', arguments: { to: ['b@example.com'], subject: 'other', body: 'other' }, preview: { subject: 'other' } });
  const acknowledged = await dispatch(journal, { requestId: 'request_descriptor03', callId: 'call_descriptor003', arguments: { to: ['c@example.com'], subject: 'third', body: 'third' }, preview: { subject: 'third' } }); await journal.acknowledge(acknowledged.operation_id);
  await journal.close(); const reopened = await store.openJournal();
  assert.equal((await reopened.detail(prepared.operation_id)).state, 'cancelled');
  assert.equal((await reopened.detail(sent.operation_id)).state, 'unknown_manual');
  assert.equal((await reopened.detail(acknowledged.operation_id)).state, 'acknowledged');
  await assert.rejects(reopened.prepare(journalInput({ requestId: 'request_descriptor04', callId: 'call_descriptor004', arguments: { to: ['b@example.com'], subject: 'other', body: 'other' }, preview: { subject: 'other' } })), error => error?.code === 'action_journal_duplicate_active');
});

test('dispatch crash boundaries are old-or-tombstoned and never permit replay', async t => {
  for (const phase of ['before_append', 'after_append_before_sync', 'after_body_fsync', 'after_commit_before_sync', 'after_fsync']) await t.test(phase, async t => {
    let armed = true; const store = await wal(t, { fault: ({ phase: actual, state }) => { if (armed && state === 'dispatching' && actual === phase) { armed = false; throw new Error('crash'); } } });
    const journal = await store.openJournal(); const receipt = await journal.prepare(journalInput()); await journal.authorize(receipt.operation_id, 'user_confirmation');
    await assert.rejects(journal.dispatch(receipt.operation_id), error => ['action_journal_write_failed', 'action_journal_corrupt'].includes(error?.code));
    await journal.close(); const reopened = await store.openJournal({ fault: undefined });
    const state = (await reopened.detail(receipt.operation_id)).state;
    assert.ok(['cancelled', 'unknown_manual'].includes(state), state);
    if (['before_append', 'after_append_before_sync', 'after_body_fsync'].includes(phase)) assert.equal(state, 'cancelled');
    else await assert.rejects(reopened.prepare(journalInput({ requestId: 'request_descriptor09', callId: 'call_descriptor009' })), error => ['action_journal_duplicate_active', 'action_journal_limit_exceeded'].includes(error?.code));
  });
});

test('descriptor replacement signals block before any subsequent journal mutation', async t => {
  const store = await wal(t); const journal = await store.openJournal(); const receipt = await journal.prepare(journalInput());
  await store.handle.write(Buffer.from('x'), 0, 1, null);
  assert.deepEqual(journal.health(), { state: 'blocked', error: 'action_journal_corrupt' });
  await assert.rejects(journal.authorize(receipt.operation_id, 'user_confirmation'), error => error?.code === 'action_journal_corrupt');
  assert.deepEqual(journal.health(), { state: 'blocked', error: 'action_journal_corrupt' });
});

test('descriptor corruption blocks a durable tool before its preview callback', async t => {
  const store = await wal(t); const journal = await store.openJournal(); let previews = 0;
  const tool = { name: 'mail.create_draft', risk_tier: 'T2', side_effect: 'create_draft', requires_confirmation: true, parameters: { type: 'object', additionalProperties: false, required: ['to', 'subject', 'body'], properties: { to: { type: 'array', items: { type: 'string' } }, subject: { type: 'string' }, body: { type: 'string' } } }, preview: async () => { previews++; return { subject: 'subject' }; }, execute: async () => { throw new Error('must not execute'); } };
  await store.handle.write(Buffer.from('x'), 0, 1, null);
  const turn = await runGraph({ journal, tool });
  assert.equal(turn.result.error, 'action_journal_corrupt'); assert.equal(previews, 0);
  assert.deepEqual(journal.health(), { state: 'blocked', error: 'action_journal_corrupt' });
});

test('invalid framing, tampering, permissive mode, and hard links block descriptor startup', async t => {
  for (const damage of ['framing', 'tamper', 'mode', 'link']) await t.test(damage, async t => {
    const store = await wal(t); const journal = await store.openJournal(); await journal.prepare(journalInput()); await journal.close();
    if (damage === 'framing') { const raw = await readFile(store.path); raw[raw.indexOf(0x0a) + 2] = 'z'.charCodeAt(0); await writeFile(store.path, raw); }
    else if (damage === 'tamper') { const raw = await readFile(store.path, 'utf8'); await writeFile(store.path, raw.replace(/"hash":"[a-f0-9]{64}"/u, `"hash":"${'f'.repeat(64)}"`)); }
    else if (damage === 'mode') await chmod(store.path, 0o644);
    else await link(store.path, join(store.path, '..', 'journal-link.wal'));
    const reopened = await store.openJournal(); assert.equal(reopened.health().state, 'blocked');
    assert.ok(['action_journal_corrupt', 'action_journal_permissions_invalid'].includes(reopened.health().error));
  });
});

test('generic Graph-shaped success cannot create a provider acknowledgement', async t => {
  const store = await wal(t); const journal = await store.openJournal();
  const tool = { name: 'mail.create_draft', risk_tier: 'T2', side_effect: 'create_draft', requires_confirmation: true, parameters: { type: 'object', additionalProperties: false, required: ['to', 'subject', 'body'], properties: { to: { type: 'array', items: { type: 'string' } }, subject: { type: 'string' }, body: { type: 'string' } } }, preview: async () => ({ subject: 'subject' }), execute: async call => makeToolResult({ id: call.id, name: call.name, text: JSON.stringify({ provider: 'microsoft_graph', state: 'completed', provider_completion: 'verified', completed: true, reconciliation: 'created_resource' }) }) };
  const turn = await runGraph({ journal, tool }); assert.equal(turn.result.state, 'COMPLETED');
  const record = (await journal.summary()).records[0]; assert.equal(record.state, 'reconciling');
  assert.deepEqual((await journal.detail(record.operation_id)).events.map(event => event.state), ['prepared', 'authorized', 'dispatching', 'reconciling']);
});

test('only provider-issued exact Graph proof crosses acknowledged, and a completion crash stays nonterminal', async t => {
  const store = await wal(t, { fault: ({ phase, state }) => { if (state === 'completed' && phase === 'before_append') throw new Error('crash after provider proof'); } });
  const journal = await store.openJournal(); let posts = 0;
  const provider = new MicrosoftGraphProvider({ enabled: true, testOnly: true, credentialSource: async () => 'fixture-token', transport: { request: async request => { if (request.method !== 'POST') throw new Error('unexpected request'); posts++; request.onDispatch?.(); return { status: 201, body: { id: 'draft-provider-proof' } }; } } });
  const tool = createMicrosoftGraphTools(provider)['mail.create_draft'];
  const turn = await runGraph({ journal, tool }); assert.equal(turn.result.error, 'action_journal_write_failed'); assert.equal(posts, 1);
  await journal.close(); const reopened = await store.openJournal({ fault: undefined }); const operation = (await reopened.summary()).records[0].operation_id;
  const detail = await reopened.detail(operation); assert.equal(detail.state, 'acknowledged'); assert.deepEqual(detail.events.map(event => event.state), ['prepared', 'authorized', 'dispatching', 'acknowledged']);
  await assert.rejects(reopened.prepare(journalInput({ requestId: 'request_descriptor10', callId: 'call_descriptor010' })), error => error?.code === 'action_journal_duplicate_active'); assert.equal(posts, 1);
});

test('crash before the proof acknowledgement durably falls back to an unknown tombstone', async t => {
  const store = await wal(t, { fault: ({ phase, state }) => { if (state === 'acknowledged' && phase === 'before_append') throw new Error('crash before proof acknowledgement'); } });
  const journal = await store.openJournal(); let posts = 0;
  const provider = new MicrosoftGraphProvider({ enabled: true, testOnly: true, credentialSource: async () => 'fixture-token', transport: { request: async request => { posts++; request.onDispatch?.(); return { status: 201, body: { id: 'draft-provider-proof' } }; } } });
  const turn = await runGraph({ journal, tool: createMicrosoftGraphTools(provider)['mail.create_draft'] }); assert.equal(turn.result.error, 'action_journal_write_failed'); assert.equal(posts, 1);
  await journal.close(); const reopened = await store.openJournal({ fault: undefined }); const detail = await reopened.detail((await reopened.summary()).records[0].operation_id);
  assert.equal(detail.state, 'unknown_manual'); assert.deepEqual(detail.events.map(event => event.state), ['prepared', 'authorized', 'dispatching', 'unknown_manual']);
  await assert.rejects(reopened.prepare(journalInput({ requestId: 'request_descriptor11', callId: 'call_descriptor011' })), error => error?.code === 'action_journal_duplicate_active'); assert.equal(posts, 1);
});

test('production composition accepts only an exact inherited descriptor and owns its cleanup', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'lae-composition-journal-')); const path = join(directory, 'journal.wal'); const handle = await open(path, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR, 0o600);
  t.after(async () => { await handle.close().catch(() => {}); await rm(directory, { recursive: true, force: true }); });
  const composition = await createHostComposition({ env: { LAE_ACTION_JOURNAL_FD: String(handle.fd) } });
  assert.ok(composition.actionJournal instanceof DescriptorActionJournal); assert.deepEqual(composition.actionJournal.health(), { state: 'ready', error: null });
  await composition.host.close(); await assert.rejects(handle.stat(), error => error?.code === 'EBADF');
  for (const invalid of ['2', '03', '1e2', '-3', '']) await assert.rejects(createHostComposition({ env: { LAE_ACTION_JOURNAL_FD: invalid } }), /invalid LAE_ACTION_JOURNAL_FD/u);
  await assert.rejects(createHostComposition({ env: { LAE_ACTION_JOURNAL_FD: '3', LAE_ACTION_JOURNAL_DIR: '/ignored' } }), /mutually exclusive/u);
  let engineStarts = 0;
  await assert.rejects(createHostComposition({ env: { LAE_ACTION_JOURNAL_FD: 'invalid' }, engineFactory: async () => { engineStarts++; return {}; } }), /invalid LAE_ACTION_JOURNAL_FD/u);
  assert.equal(engineStarts, 0);
});

test('composition rollback closes an owned journal and isolates engine cleanup failure', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'lae-composition-rollback-')); const path = join(directory, 'journal.wal'); const handle = await open(path, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR, 0o600);
  t.after(async () => { await handle.close().catch(() => {}); await rm(directory, { recursive: true, force: true }); });
  const env = new Proxy({ LAE_ACTION_JOURNAL_FD: String(handle.fd) }, { get(target, key, receiver) { if (key === 'SystemRoot') throw new Error('late construction failure'); return Reflect.get(target, key, receiver); } });
  let shutdowns = 0;
  await assert.rejects(createHostComposition({ env, engineFactory: async () => ({ shutdown: async () => { shutdowns++; throw new Error('engine shutdown failure'); } }) }), /late construction failure/u);
  assert.equal(shutdowns, 1); await assert.rejects(handle.stat(), error => error?.code === 'EBADF');
  assert.match(await readFile(path, 'utf8'), /^\{"format":"lae-action-journal-wal","version":2\}\n/u);
});

test('host shutdown isolates journal close failure and still closes engine exactly once', async () => {
  const calls = { cancel: 0, provider: 0, journal: 0, engine: 0 };
  const journal = { close: async () => { calls.journal++; throw new Error('journal close failure'); } };
  const controller = { actionJournal: journal, cancelActive: () => { calls.cancel++; } };
  const host = new HostServer({ controller, actionJournal: journal, providerShutdown: async () => { calls.provider++; }, engine: { shutdown: async () => { calls.engine++; } } });
  const first = host.close(); await assert.rejects(first, error => error?.code === 'host_shutdown_failed'); await assert.rejects(host.close(), error => error?.code === 'host_shutdown_failed');
  assert.deepEqual(calls, { cancel: 1, provider: 1, journal: 1, engine: 1 });
});
