import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ActionJournal, createActionBinding } from '../../host/agent/action-journal.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { createMicrosoftGraphTools, MicrosoftGraphProvider, readGraphRestartControl } from '../../host/providers/microsoft-graph.mjs';

const CLIENT_ID = '00001111-aaaa-2222-bbbb-3333cccc4444';
const args = Object.freeze({ to: ['alice@example.com'], subject: 'restart subject', body: 'restart body' });
const preview = Object.freeze({ provider: 'microsoft_graph', action: 'create_draft' });
const engine = Object.freeze({ async *generate() {} });

async function directory(t) {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'lae-graph-restart-')));
  await chmod(path, 0o700); t.after(() => rm(path, { recursive: true, force: true })); return path;
}

async function acknowledgedDraftJournal(t, { state = 'acknowledged', arguments_ = args } = {}) {
  const path = await directory(t); let tick = 0;
  const journal = await ActionJournal.open({ directory: path, testOnly: true, idFactory: () => `act_${'1'.repeat(32)}`, now: () => new Date(Date.UTC(2026, 8, 9, 0, 0, tick++)).toISOString() });
  const binding = createActionBinding({ requestId: 'request_restart01', callId: 'call_restart0001', toolName: 'mail.create_draft', arguments: arguments_, preview });
  const receipt = await journal.prepare({ requestId: 'request_restart01', callId: 'call_restart0001', toolName: 'mail.create_draft', riskTier: 'T2', sideEffect: 'create_draft', argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest });
  await journal.authorize(receipt.operation_id, 'user_confirmation'); await journal.dispatch(receipt.operation_id); await journal.acknowledge(receipt.operation_id);
  if (state === 'reconciling') await journal.beginReconciliation(receipt.operation_id);
  return { path, binding: { operation_id: receipt.operation_id, operation_digest: binding.operationDigest, arguments_digest: binding.argumentsDigest }, journal: await ActionJournal.open({ directory: path, testOnly: true }) };
}

function graphFixture({ accountId = 'provider-account-a', drafts = [], nextLink = false, pauseList = null } = {}) {
  const calls = []; let device = 0; let token = 0; let list = 0; let gets = 0;
  const transport = { request: async request => {
    calls.push(request);
    if (request.path.endsWith('/devicecode')) { device += 1; return { status: 200, body: { device_code: 'opaque-device-code', user_code: 'SAFE-CODE', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } }; }
    if (request.path.endsWith('/token')) { token += 1; return { status: 200, body: { access_token: 'synthetic-memory-token', expires_in: 3600, scope: 'User.Read Mail.ReadWrite' } }; }
    if (request.path === '/v1.0/me') return { status: 200, body: { id: accountId } };
    if (request.path === '/v1.0/me/mailFolders/drafts/messages') { list += 1; if (pauseList) await pauseList(); return { status: 200, body: { value: drafts.map(item => ({ id: item.id })), ...(nextLink ? { '@odata.nextLink': 'opaque-next-page' } : {}) } }; }
    if (request.path.startsWith('/v1.0/me/messages/')) { gets += 1; const id = decodeURIComponent(request.path.slice('/v1.0/me/messages/'.length)); const draft = drafts.find(item => item.id === id); return draft ? { status: 200, body: draft } : { status: 404, body: {} }; }
    throw new Error('unexpected injected request');
  } };
  const provider = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId: CLIENT_ID, scopes: ['User.Read', 'Mail.ReadWrite'], transport, now: () => 0, sleep: async () => {} });
  return { provider, calls, counts: () => ({ device, token, list, gets }) };
}

const providerDraft = ({ id, marker, subject = args.subject, body = args.body } = {}) => ({
  id, subject, body: { contentType: 'Text', content: body },
  toRecipients: [{ emailAddress: { address: args.to[0] } }], ccRecipients: [],
  internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], changeKey: 'provider-change-key',
});

test('authenticated draft creation writes the restart marker with canonical account and operation binding', async () => {
  const fixture = graphFixture(); const originalRequest = fixture.provider.transport.request; let writtenMarker;
  fixture.provider.transport.request = async request => {
    if (request.method === 'POST' && request.path === '/v1.0/me/messages') { request.onDispatch?.(); writtenMarker = request.body.internetMessageHeaders[0].value; return { status: 201, body: { id: 'draft-created' } }; }
    return originalRequest(request);
  };
  await fixture.provider.startAuth(); const binding = createActionBinding({ requestId: 'request_marker01', callId: 'call_marker0001', toolName: 'mail.create_draft', arguments: args, preview });
  const tools = createMicrosoftGraphTools(fixture.provider); const call = { id: 'call_marker0001', name: 'mail.create_draft', arguments: args }; await tools['mail.create_draft'].preview(call);
  const output = await tools['mail.create_draft'].execute({ ...call, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: { operation_id: `act_${'3'.repeat(32)}`, operation_digest: binding.operationDigest, arguments_digest: binding.argumentsDigest, preview_digest: binding.previewDigest } } });
  assert.equal(output.status, 'ok'); assert.equal(writtenMarker, `act_${'3'.repeat(32)}:${binding.operationDigest}:${fixture.provider.getAccountFingerprint()}`);
});

test('restart reconciliation coalesces and completes only an acknowledged draft with fresh exact Graph proof', async t => {
  const saved = await acknowledgedDraftJournal(t); const identity = graphFixture(); await identity.provider.startAuth();
  const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${identity.provider.getAccountFingerprint()}`;
  // Re-authenticate a fresh provider instance to model process restart while
  // keeping every transport interaction injected.
  const restarted = graphFixture({ drafts: [providerDraft({ id: 'draft-provider-1', marker })] }); await restarted.provider.startAuth();
  const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(restarted.provider) });
  const [first, second] = await Promise.all([controller.reconcileRestartActions(), controller.reconcileRestartActions()]);
  assert.deepEqual(first, { state: 'completed', examined: 1, completed: 1 }); assert.deepEqual(second, first);
  assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'completed');
  assert.deepEqual(restarted.counts(), { device: 1, token: 1, list: 1, gets: 1 });
  const listRequest = restarted.calls.find(call => call.path === '/v1.0/me/mailFolders/drafts/messages');
  const getRequest = restarted.calls.find(call => call.path === '/v1.0/me/messages/draft-provider-1');
  assert.deepEqual(listRequest.query, { '$top': 20, '$select': 'id' }); assert.match(getRequest.query.$select, /internetMessageHeaders/u);
});

test('account, content, uniqueness, pagination, and private tool identity all fail closed', async t => {
  const cases = [
    ['account mismatch', ({ binding, originalAccount }) => graphFixture({ accountId: 'provider-account-b', drafts: [providerDraft({ id: 'draft-account', marker: `${binding.operation_id}:${binding.operation_digest}:${originalAccount}` })] })],
    ['content mismatch', ({ binding, currentAccount }) => graphFixture({ drafts: [providerDraft({ id: 'draft-content', marker: `${binding.operation_id}:${binding.operation_digest}:${currentAccount}`, body: 'changed body' })] })],
    ['duplicate proof', ({ binding, currentAccount }) => graphFixture({ drafts: [providerDraft({ id: 'draft-one', marker: `${binding.operation_id}:${binding.operation_digest}:${currentAccount}` }), providerDraft({ id: 'draft-two', marker: `${binding.operation_id}:${binding.operation_digest}:${currentAccount}` })] })],
    ['duplicate operation header', ({ binding, currentAccount }) => { const marker = `${binding.operation_id}:${binding.operation_digest}:${currentAccount}`; const draft = providerDraft({ id: 'draft-headers', marker }); draft.internetMessageHeaders.push({ name: 'X-LAE-OPERATION', value: marker }); return graphFixture({ drafts: [draft] }); }],
    ['truncated collection', ({ binding, currentAccount }) => graphFixture({ drafts: [providerDraft({ id: 'draft-page', marker: `${binding.operation_id}:${binding.operation_digest}:${currentAccount}` })], nextLink: true })],
  ];
  for (const [label, make] of cases) {
    const saved = await acknowledgedDraftJournal(t); const identity = graphFixture(); await identity.provider.startAuth(); const originalAccount = identity.provider.getAccountFingerprint();
    const provisional = graphFixture(); await provisional.provider.startAuth(); const currentAccount = provisional.provider.getAccountFingerprint();
    const fixture = make({ binding: saved.binding, originalAccount, currentAccount }); await fixture.provider.startAuth();
    const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
    const output = await controller.reconcileRestartActions(); assert.equal(output.completed, 0, label); assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged', label);
  }
  const saved = await acknowledgedDraftJournal(t); const real = graphFixture(); await real.provider.startAuth(); const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${real.provider.getAccountFingerprint()}`; real.provider.transport.request = graphFixture({ drafts: [providerDraft({ id: 'draft-clone', marker })] }).provider.transport.request;
  const genuine = createMicrosoftGraphTools(real.provider)['mail.create_draft']; const cloned = { ...genuine };
  const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: { 'mail.create_draft': cloned } });
  assert.deepEqual(await controller.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0 });
  assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged');
});

test('reconciling and startup-ambiguous dispatch states remain manual with zero provider callbacks', async t => {
  const reconciling = await acknowledgedDraftJournal(t, { state: 'reconciling' });
  const path = await directory(t); const journal = await ActionJournal.open({ directory: path, testOnly: true, idFactory: () => `act_${'2'.repeat(32)}` });
  const binding = createActionBinding({ requestId: 'request_restart02', callId: 'call_restart0002', toolName: 'mail.create_draft', arguments: args, preview });
  const dispatching = await journal.prepare({ requestId: 'request_restart02', callId: 'call_restart0002', toolName: 'mail.create_draft', riskTier: 'T2', sideEffect: 'create_draft', argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest }); await journal.authorize(dispatching.operation_id, 'user_confirmation'); await journal.dispatch(dispatching.operation_id);
  const reopenedDispatch = await ActionJournal.open({ directory: path, testOnly: true }); assert.equal((await reopenedDispatch.detail(dispatching.operation_id)).state, 'unknown_manual');
  for (const [current, id] of [[reconciling.journal, reconciling.binding.operation_id], [reopenedDispatch, dispatching.operation_id]]) {
    let calls = 0; const fixture = graphFixture(); fixture.provider.transport.request = async () => { calls += 1; throw new Error('must not call provider'); };
    const controller = new ConversationController({ engine, actionJournal: current, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
    assert.deepEqual(await controller.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0 }); assert.equal(calls, 0); assert.notEqual((await current.detail(id)).state, 'completed');
  }
});

test('auth epoch changes and hostile journal summaries cannot manufacture restart completion', async t => {
  const saved = await acknowledgedDraftJournal(t); let releaseList; const listGate = new Promise(resolve => { releaseList = resolve; });
  const identity = graphFixture(); await identity.provider.startAuth(); const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${identity.provider.getAccountFingerprint()}`;
  const fixture = graphFixture({ drafts: [providerDraft({ id: 'draft-race', marker })], pauseList: () => listGate }); await fixture.provider.startAuth();
  const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
  const pending = controller.reconcileRestartActions(); while (fixture.counts().list === 0) await new Promise(resolve => setImmediate(resolve)); fixture.provider.clearAuth(); releaseList();
  assert.equal((await pending).completed, 0); assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged');

  let completes = 0; const methods = ['health', 'prepare', 'authorize', 'dispatch', 'acknowledge', 'beginReconciliation', 'complete', 'cancel', 'failDefinitive', 'markUnknown'];
  const hostile = Object.fromEntries(methods.map(name => [name, name === 'health' ? () => ({ state: 'ready', error: null }) : name === 'complete' ? async () => { completes += 1; } : async () => {}]));
  Object.defineProperty(hostile, 'summary', { value: async () => ({ records: new Proxy([], {}) }) });
  const noProvider = createMicrosoftGraphTools(graphFixture().provider); const hostileController = new ConversationController({ engine, actionJournal: hostile, toolRegistry: noProvider });
  assert.deepEqual(await hostileController.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0 }); assert.equal(completes, 0);

  let getterCalls = 0; const accessorSummary = { ...hostile, summary: async () => { const output = {}; Object.defineProperty(output, 'records', { get() { getterCalls += 1; return []; } }); return output; } };
  const accessorController = new ConversationController({ engine, actionJournal: accessorSummary, toolRegistry: noProvider });
  assert.deepEqual(await accessorController.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0 }); assert.equal(getterCalls, 0);

  const stable = { ...hostile, summary: async () => ({ health: {}, total: 0, active: 0, records: [] }) }; const stableController = new ConversationController({ engine, actionJournal: stable, toolRegistry: noProvider }); stable.summary = async () => { throw new Error('replacement summary must not run'); };
  assert.deepEqual(await stableController.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0 });
  const summaryAccessor = { ...hostile }; Object.defineProperty(summaryAccessor, 'summary', { get() { throw new Error('summary getter must not run'); } });
  assert.throws(() => new ConversationController({ engine, actionJournal: summaryAccessor, toolRegistry: noProvider }), /actionJournal does not implement/u);
  const summaryProxy = { ...hostile, summary: new Proxy(async () => ({ records: [] }), {}) };
  assert.throws(() => new ConversationController({ engine, actionJournal: summaryProxy, toolRegistry: noProvider }), /actionJournal does not implement/u);

  const realTools = createMicrosoftGraphTools(graphFixture().provider); const control = readGraphRestartControl(realTools['mail.create_draft']);
  let candidateGetterCalls = 0; const candidate = {};
  for (const key of ['operation_id', 'tool_name', 'state', 'arguments_digest', 'operation_digest']) Object.defineProperty(candidate, key, { enumerable: true, get() { candidateGetterCalls += 1; return 'forged'; } });
  assert.equal(await control.reconcile(candidate), null); assert.equal(candidateGetterCalls, 0);
  assert.equal(await control.reconcile(new Proxy({}, { get() { throw new Error('proxy getter must not run'); } })), null);
});
