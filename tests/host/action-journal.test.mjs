import test from 'node:test';
import assert from 'node:assert/strict';
import { appendFile, chmod, lstat, mkdtemp, readFile, realpath, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ActionJournal, ACTION_JOURNAL_LIMITS, createActionBinding } from '../../host/agent/action-journal.mjs';
import { ConversationController, requiresDurableAction } from '../../host/agent/controller.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { HostServer } from '../../host/server/host-server.mjs';

const SECRET = 'do-not-persist-super-secret-body';
const REQUEST_ID = 'request_action01';
const CALL_ID = 'call_action0001';

async function directory(t) {
  const raw = await mkdtemp(join(tmpdir(), 'lae-action-journal-'));
  const path = await realpath(raw);
  await chmod(path, 0o700);
  t.after(async () => { await import('node:fs/promises').then(fs => fs.rm(path, { recursive: true, force: true })); });
  return path;
}

function deterministicOptions(path, extra = {}) {
  let id = 0; let tick = 0;
  return { directory: path, idFactory: () => `act_${(++id).toString(16).padStart(32, '0')}`, now: () => new Date(Date.UTC(2026, 0, 1, 0, 0, tick++)).toISOString(), ...extra };
}

function input(overrides = {}) {
  const value = { requestId: REQUEST_ID, callId: CALL_ID, toolName: 'fs.write_new', riskTier: 'T2', sideEffect: 'create', arguments: { path: 'note.txt', content: SECRET }, preview: { path: 'note.txt', content_preview: SECRET }, ...overrides };
  const binding = createActionBinding({ requestId: value.requestId, callId: value.callId, toolName: value.toolName, arguments: value.arguments, preview: value.preview });
  return { requestId: value.requestId, callId: value.callId, toolName: value.toolName, riskTier: value.riskTier, sideEffect: value.sideEffect, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest };
}

async function prepared(journal, overrides) { return journal.prepare(input(overrides)); }

function mutationEngine(onTools) {
  return {
    async *generate({ messages, tools }) {
      onTools?.(tools);
      if (!messages.some(message => message.role === 'tool')) {
        yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: CALL_ID, name: 'fs.write_new', arguments: { path: 'note.txt', content: SECRET } }) };
        return;
      }
      yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' };
    }
  };
}

function mutationTool(execute) {
  return {
    name: 'fs.write_new', risk_tier: 'T2', side_effect: 'create', requires_confirmation: true, timeout_ms: 1000,
    parameters: { type: 'object', additionalProperties: false, required: ['path', 'content'], properties: { path: { type: 'string' }, content: { type: 'string' } } },
    preview: async call => ({ path: call.arguments.path, content_preview: call.arguments.content }), execute
  };
}

async function runMutation({ journal, execute, approved = true, onTools }) {
  let confirmation;
  const events = [];
  const controller = new ConversationController({ engine: mutationEngine(onTools), actionJournal: journal, confirmationTimeoutMs: 1000, toolRegistry: { 'fs.write_new': mutationTool(execute) } });
  const turn = controller.runTurn({ sessionId: 'session_action01', requestId: REQUEST_ID, message: 'write note', onEvent: event => {
    events.push(event);
    if (event.event === 'tool.confirmation_required') { confirmation = event; queueMicrotask(() => controller.confirm(event.data.confirmation_id, approved, { requestId: REQUEST_ID, callId: CALL_ID })); }
  } });
  return { result: await turn, events, confirmation };
}

test('journal persists a bounded hash chain with private permissions and digest-only receipts', async t => {
  assert.equal(ACTION_JOURNAL_LIMITS.max_event_bytes, 64 * 1024); assert.equal(ACTION_JOURNAL_LIMITS.max_active, 256);
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); assert.deepEqual(journal.health(), { state: 'ready', error: null });
  const receipt = await prepared(journal); await journal.authorize(receipt.operation_id, 'user_confirmation'); await journal.dispatch(receipt.operation_id); await journal.acknowledge(receipt.operation_id); await journal.complete(receipt.operation_id);
  const detail = await journal.detail(receipt.operation_id);
  assert.deepEqual(detail.events.map(event => event.state), ['prepared', 'authorized', 'dispatching', 'acknowledged', 'completed']);
  assert.match(detail.arguments_digest, /^[a-f0-9]{64}$/); assert.match(detail.preview_digest, /^[a-f0-9]{64}$/); assert.match(detail.operation_digest, /^[a-f0-9]{64}$/);
  const filename = join(path, `${receipt.operation_id}.jsonl`); const raw = await readFile(filename, 'utf8');
  for (const secret of [SECRET, REQUEST_ID, CALL_ID, 'note.txt']) assert.equal(raw.includes(secret), false, secret);
  for (const line of raw.trimEnd().split('\n')) assert.ok(Buffer.byteLength(line, 'utf8') <= ACTION_JOURNAL_LIMITS.max_event_bytes);
  if (process.platform !== 'win32') { assert.equal((await lstat(path)).mode & 0o777, 0o700); assert.equal((await lstat(filename)).mode & 0o777, 0o600); }
  const reopened = await ActionJournal.open({ directory: path }); assert.deepEqual(reopened.health(), { state: 'ready', error: null }); assert.equal((await reopened.detail(receipt.operation_id)).receipt_hash, detail.receipt_hash);
});

test('Windows and untrusted directory paths fail closed without creating or chmodding targets', async t => {
  const parent = await directory(t); const absent = join(parent, 'not-created');
  const windows = await ActionJournal.open({ directory: 'C:\\private\\journal', platform: 'win32' });
  assert.deepEqual(windows.health(), { state: 'blocked', error: 'action_journal_platform_unavailable' });
  const absentJournal = await ActionJournal.open({ directory: absent }); assert.equal(absentJournal.health().state, 'blocked');
  await assert.rejects(lstat(absent), { code: 'ENOENT' });
  const target = join(parent, 'target'); await import('node:fs/promises').then(fs => fs.mkdir(target, { mode: 0o755 }));
  const link = join(parent, 'link'); await symlink(target, link);
  const linked = await ActionJournal.open({ directory: link }); assert.deepEqual(linked.health(), { state: 'blocked', error: 'action_journal_permissions_invalid' });
  assert.equal((await lstat(target)).mode & 0o777, 0o755, 'opening a symlink does not chmod its target');
});

test('startup never manufactures completion and recovery preserves ambiguity', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path));
  const acknowledged = await prepared(journal); await journal.authorize(acknowledged.operation_id, 'user_confirmation'); await journal.dispatch(acknowledged.operation_id); await journal.acknowledge(acknowledged.operation_id);
  const second = await prepared(journal, { requestId: 'request_action02', callId: 'call_action0002', arguments: { path: 'second.txt', content: SECRET }, preview: { path: 'second.txt' } }); await journal.authorize(second.operation_id, 'user_confirmation'); await journal.dispatch(second.operation_id); await journal.beginReconciliation(second.operation_id);
  const third = await prepared(journal, { requestId: 'request_action03', callId: 'call_action0003', arguments: { path: 'third.txt', content: SECRET }, preview: { path: 'third.txt' } }); await journal.authorize(third.operation_id, 'user_confirmation'); await journal.dispatch(third.operation_id);
  const reopened = await ActionJournal.open({ directory: path });
  assert.equal((await reopened.detail(acknowledged.operation_id)).state, 'acknowledged');
  assert.equal((await reopened.detail(second.operation_id)).state, 'reconciling');
  assert.equal((await reopened.detail(third.operation_id)).state, 'unknown_manual');
});

test('malformed, truncated, and hash-tampered journals block subsequent actions', async t => {
  for (const damage of ['malformed', 'truncated', 'hash', 'over-limit']) {
    await t.test(damage, async t => {
      const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); const receipt = await prepared(journal); const filename = join(path, `${receipt.operation_id}.jsonl`);
      if (damage === 'malformed') await appendFile(filename, '{\n');
      else if (damage === 'truncated') { const raw = await readFile(filename, 'utf8'); await import('node:fs/promises').then(fs => fs.writeFile(filename, raw.slice(0, -2), { mode: 0o600 })); }
      else if (damage === 'hash') { const raw = await readFile(filename, 'utf8'); await import('node:fs/promises').then(fs => fs.writeFile(filename, raw.replace(/"hash":"[a-f0-9]{64}"/, `"hash":"${'f'.repeat(64)}"`), { mode: 0o600 })); }
      else await appendFile(filename, `${'x'.repeat(ACTION_JOURNAL_LIMITS.max_event_bytes + 1)}\n`);
      const reopened = await ActionJournal.open({ directory: path }); assert.equal(reopened.health().state, 'blocked'); await assert.rejects(prepared(reopened), error => error.code === reopened.health().error);
    });
  }
});

test('record symlink substitution is rejected before target mutation', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); const receipt = await prepared(journal); const filename = join(path, `${receipt.operation_id}.jsonl`); const target = join(path, 'unrelated');
  await import('node:fs/promises').then(async fs => { await fs.writeFile(target, 'unchanged', { mode: 0o600 }); await fs.unlink(filename); await fs.symlink(target, filename); });
  await assert.rejects(journal.authorize(receipt.operation_id, 'user_confirmation'), error => ['action_journal_write_failed', 'action_journal_corrupt'].includes(error.code)); assert.equal(await readFile(target, 'utf8'), 'unchanged'); assert.equal(journal.health().state, 'blocked');
});

test('dispatch must be fsynced before execution and any post-dispatch failure becomes unknown', async t => {
  for (const phase of ['before_append', 'after_append_before_sync', 'after_fsync']) {
    await t.test(`dispatch fault ${phase}`, async t => {
      const path = await directory(t); let executions = 0;
      const journal = await ActionJournal.open(deterministicOptions(path, { fault: ({ phase: actual, state }) => { if (state === 'dispatching' && actual === phase) throw new Error('injected_crash'); } }));
      const turn = await runMutation({ journal, execute: async call => { executions++; return makeToolResult({ id: call.id, name: call.name }); } });
      assert.equal(executions, 0); assert.equal(turn.result.error, 'action_journal_write_failed');
      const reopened = await ActionJournal.open({ directory: path }); const summary = await reopened.summary();
      assert.ok(summary.records.every(record => ['cancelled', 'unknown_manual'].includes(record.state))); assert.equal((await readFile(join(path, (await import('node:fs/promises').then(fs => fs.readdir(path))).find(name => name.endsWith('.jsonl'))), 'utf8')).includes(SECRET), false);
    });
  }
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); let executions = 0;
  const turn = await runMutation({ journal, execute: async () => { executions++; throw Object.assign(new Error('provider lost'), { code: 'provider_timeout' }); } });
  assert.equal(executions, 1); assert.equal(turn.result.error, 'provider_timeout'); assert.equal((await journal.summary()).records[0].state, 'unknown_manual');
});

test('denial is cancelled, missing journals block actions, and reads are not journaled', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); let executions = 0;
  const denied = await runMutation({ journal, approved: false, execute: async call => { executions++; return makeToolResult({ id: call.id, name: call.name }); } });
  assert.ok(denied.confirmation); assert.equal(executions, 0); assert.equal((await journal.summary()).records[0].state, 'cancelled');
  let advertised = []; const absent = await runMutation({ onTools: tools => { advertised = tools.map(tool => tool.function.name); }, execute: async call => { executions++; return makeToolResult({ id: call.id, name: call.name }); } });
  assert.equal(absent.result.error, 'action_journal_unavailable'); assert.equal(executions, 0); assert.equal(advertised.includes('fs.write_new'), false, 'a guaranteed-unavailable action is not advertised even if a hostile model proposes it');
  const unhealthyJournal = await ActionJournal.open({ directory: 'C:\\private\\journal', platform: 'win32' }); let unhealthyAdvertised = [];
  const unhealthy = await runMutation({ journal: unhealthyJournal, onTools: tools => { unhealthyAdvertised = tools.map(tool => tool.function.name); }, execute: async call => { executions++; return makeToolResult({ id: call.id, name: call.name }); } });
  assert.equal(unhealthy.result.error, 'action_journal_platform_unavailable'); assert.equal(unhealthyAdvertised.includes('fs.write_new'), false); assert.equal(executions, 0);
  assert.equal(requiresDurableAction({ risk_tier: 'T1', side_effect: 'launch' }), true); assert.equal(requiresDurableAction({ risk_tier: 'T1', side_effect: 'external_navigation' }), true); assert.equal(requiresDurableAction({ risk_tier: 'T1', side_effect: 'browser_navigation' }), true);
  assert.equal(requiresDurableAction({ risk_tier: 'T1', side_effect: 'browser_read' }), false); assert.equal(requiresDurableAction({ risk_tier: 'T3', side_effect: 'cloud_inference' }), true, 'confirmed Copilot egress requires a durable dispatch receipt'); assert.throws(() => requiresDurableAction({ risk_tier: 'T2', side_effect: 'mystery' }), error => error.code === 'action_journal_classification_required');
  const before = (await journal.summary()).total;
  const readController = new ConversationController({ engine: { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_read00001', name: 'test.read', arguments: {} }) }; return; } yield { kind: 'text_delta', text: 'read' }; } }, actionJournal: journal, toolRegistry: { 'test.read': { name: 'test.read', risk_tier: 'T1', side_effect: 'read_sensitive', execute: async call => makeToolResult({ id: call.id, name: call.name }) } } });
  assert.equal((await readController.runTurn({ sessionId: 'session_read0001', requestId: 'request_read0001', message: 'read' })).state, 'COMPLETED'); assert.equal((await journal.summary()).total, before);
});

test('request cancellation and pre-dispatch authorization failure become definitive terminal records', async t => {
  const cancelPath = await directory(t); const cancelJournal = await ActionJournal.open(deterministicOptions(cancelPath)); let executions = 0;
  const cancelling = new ConversationController({ engine: mutationEngine(), actionJournal: cancelJournal, confirmationTimeoutMs: 1000, toolRegistry: { 'fs.write_new': mutationTool(async call => { executions++; return makeToolResult({ id: call.id, name: call.name }); }) } });
  const cancelled = cancelling.runTurn({ sessionId: 'session_cancel01', requestId: REQUEST_ID, message: 'write', onEvent: event => { if (event.event === 'tool.confirmation_required') queueMicrotask(() => cancelling.cancel(REQUEST_ID)); } });
  assert.equal((await cancelled).state, 'CANCELLED'); assert.equal(executions, 0); const cancelDetail = await cancelJournal.detail((await cancelJournal.summary()).records[0].operation_id); assert.equal(cancelDetail.state, 'cancelled'); assert.equal(cancelDetail.events.at(-1).resolution, 'request_cancelled');

  const failurePath = await directory(t); const failureJournal = await ActionJournal.open(deterministicOptions(failurePath));
  const failingTool = { ...mutationTool(async call => { executions++; return makeToolResult({ id: call.id, name: call.name }); }), requires_confirmation: false, authorize: async () => { throw Object.assign(new Error('grant unavailable'), { code: 'authorization_unavailable' }); } };
  const failing = new ConversationController({ engine: mutationEngine(), actionJournal: failureJournal, toolRegistry: { 'fs.write_new': failingTool } });
  const failed = await failing.runTurn({ sessionId: 'session_failure1', requestId: REQUEST_ID, message: 'write' }); assert.equal(failed.error, 'authorization_unavailable'); assert.equal(executions, 0); assert.equal((await failureJournal.summary()).records[0].state, 'failed_definitive');
});

test('provider acknowledgements stay visibly unverified and duplicate active actions are blocked', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); let executions = 0; let callNumber = 0; let previewRevision = 0;
  const tool = {
    name: 'mail.create_draft', risk_tier: 'T2', side_effect: 'create_draft', network: true, requires_confirmation: true,
    parameters: { type: 'object', additionalProperties: false, required: ['subject'], properties: { subject: { type: 'string' } } },
    preview: async call => ({ subject: call.arguments.subject, proposal_revision: `revision-${++previewRevision}` }), execute: async call => { executions++; return makeToolResult({ id: call.id, name: call.name, text: '{"accepted":true}' }); }
  };
  const controller = new ConversationController({ actionJournal: journal, confirmationTimeoutMs: 1000, toolRegistry: { [tool.name]: tool }, engine: { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: `call_draft000${++callNumber}`, name: tool.name, arguments: { subject: 'same action' } }) }; return; } yield { kind: 'text_delta', text: 'pending' }; } } });
  const events = []; const first = controller.runTurn({ sessionId: 'session_draft001', requestId: 'request_draft001', message: 'draft', onEvent: event => { events.push(event); if (event.event === 'tool.confirmation_required') queueMicrotask(() => controller.confirm(event.data.confirmation_id, true, { requestId: event.request_id, callId: event.data.call.id })); } });
  assert.equal((await first).state, 'COMPLETED'); const completed = events.find(event => event.event === 'tool.completed'); assert.equal(completed.data.result.status, 'failed'); assert.equal(JSON.parse(completed.data.result.content[0].text).code, 'action_completion_unverified'); assert.equal((await journal.summary()).records[0].state, 'reconciling');
  controller.resetSession('session_draft001'); const second = controller.runTurn({ sessionId: 'session_draft001', requestId: 'request_draft002', message: 'draft again', onEvent: event => { if (event.event === 'tool.confirmation_required') queueMicrotask(() => controller.confirm(event.data.confirmation_id, true, { requestId: event.request_id, callId: event.data.call.id })); } });
  assert.equal((await second).error, 'action_journal_duplicate_active'); assert.equal(previewRevision, 2, 'nondeterministic preview revisions cannot bypass argument-bound duplicate refusal'); assert.equal(executions, 1);
});

test('confirmed Copilot egress receives the same durable pre-dispatch receipt without mutation authority', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); let executions = 0; const events = [];
  const tool = {
    name: 'coding.copilot_ask', risk_tier: 'T3', side_effect: 'cloud_inference', network: true, requires_confirmation: true,
    parameters: { type: 'object', additionalProperties: false, required: ['prompt'], properties: { prompt: { type: 'string' } } },
    preview: async call => ({ destination: 'GitHub Copilot cloud', bytes: Buffer.byteLength(call.arguments.prompt) }), execute: async call => { executions++; return makeToolResult({ id: call.id, name: call.name, text: 'synthetic answer' }); }
  };
  const controller = new ConversationController({ actionJournal: journal, confirmationTimeoutMs: 1000, toolRegistry: { [tool.name]: tool }, engine: { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_copilot01', name: tool.name, arguments: { prompt: SECRET } }) }; return; } yield { kind: 'text_delta', text: 'done' }; } } });
  const result = controller.runTurn({ sessionId: 'session_copilot1', requestId: 'request_copilot1', message: 'ask', onEvent: event => { events.push(event); if (event.event === 'tool.confirmation_required') queueMicrotask(() => controller.confirm(event.data.confirmation_id, true, { requestId: event.request_id, callId: event.data.call.id })); } });
  assert.equal((await result).state, 'COMPLETED'); assert.equal(executions, 1); const summary = await journal.summary(); assert.equal(summary.records[0].side_effect, 'cloud_inference'); assert.equal(summary.records[0].state, 'completed'); assert.equal(JSON.stringify(summary).includes(SECRET), false); assert.equal((await readFile(join(path, `${summary.records[0].operation_id}.jsonl`), 'utf8')).includes(SECRET), false); assert.equal(events.find(event => event.event === 'tool.completed').data.result.status, 'ok');
});

test('active bounds and terminal pruning keep lifetime use bounded', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path, { maxActive: 1, maxRecords: 2, maxTerminalRecords: 1 }));
  const secondInput = { requestId: 'request_action02', callId: 'call_action0002', arguments: { path: 'second.txt', content: SECRET }, preview: { path: 'second.txt' } };
  const first = await prepared(journal); await assert.rejects(prepared(journal), error => error.code === 'action_journal_duplicate_active'); await assert.rejects(prepared(journal, secondInput), error => error.code === 'action_journal_limit_exceeded');
  await journal.cancel(first.operation_id); const second = await prepared(journal, secondInput); await journal.cancel(second.operation_id);
  const summary = await journal.summary(); assert.equal(summary.total, 1); assert.equal(summary.active, 0); assert.equal(summary.records[0].operation_id, second.operation_id); await assert.rejects(journal.detail(first.operation_id), error => error.code === 'action_journal_not_found');
});

test('operator endpoints are authenticated, bounded, assertion-only, and never replay actions', async t => {
  const path = await directory(t); const journal = await ActionJournal.open(deterministicOptions(path)); const receipt = await prepared(journal); await journal.authorize(receipt.operation_id, 'user_confirmation'); await journal.dispatch(receipt.operation_id); await journal.markUnknown(receipt.operation_id);
  let providerExecutions = 0;
  const host = new HostServer({ controller: { cancelActive() { return false; } }, actionJournal: journal, providerShutdown: async () => { providerExecutions++; } });
  const address = await host.listen(0); t.after(() => host.close()); const headers = { authorization: `Bearer ${address.token}` };
  assert.equal((await fetch(`${address.url}/api/action-journal`)).status, 401);
  const summaryResponse = await fetch(`${address.url}/api/action-journal?limit=1`, { headers }); assert.equal(summaryResponse.status, 200); const summary = await summaryResponse.json(); assert.equal(summary.records[0].state, 'unknown_manual'); assert.equal(JSON.stringify(summary).includes(SECRET), false);
  assert.equal((await fetch(`${address.url}/api/action-journal?limit=1&limit=2`, { headers })).status, 400);
  const detailResponse = await fetch(`${address.url}/api/action-journal/${receipt.operation_id}`, { headers }); assert.equal(detailResponse.status, 200); assert.equal((await detailResponse.json()).events.at(-1).state, 'unknown_manual');
  const reconcileResponse = await fetch(`${address.url}/api/action-journal/${receipt.operation_id}/reconcile`, { method: 'POST', headers: { ...headers, 'content-type': 'application/json' }, body: '{}' }); assert.equal(reconcileResponse.status, 501); assert.deepEqual(await reconcileResponse.json(), { error: 'action_reconciliation_unavailable' });
  assert.equal((await fetch(`${address.url}/api/action-journal/act_${'f'.repeat(32)}`, { headers })).status, 404);
  assert.equal((await fetch(`${address.url}/api/action-journal/${receipt.operation_id}/resolve`, { method: 'POST', headers: { ...headers, 'content-type': 'application/json' }, body: '{"resolution":"maybe"}' })).status, 400);
  const resolveResponse = await fetch(`${address.url}/api/action-journal/${receipt.operation_id}/resolve`, { method: 'POST', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify({ resolution: 'completed' }) }); assert.equal(resolveResponse.status, 200); assert.equal((await resolveResponse.json()).receipt.state, 'completed'); assert.equal(providerExecutions, 0, 'resolution only appends the operator assertion and does not contact or replay a provider');
  const unavailableHost = new HostServer({ controller: { cancelActive() { return false; } } }); const unavailableAddress = await unavailableHost.listen(0); t.after(() => unavailableHost.close()); assert.equal((await fetch(`${unavailableAddress.url}/api/action-journal`, { headers: { authorization: `Bearer ${unavailableAddress.token}` } })).status, 409);
});
