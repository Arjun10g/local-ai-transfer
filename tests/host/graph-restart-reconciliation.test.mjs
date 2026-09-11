import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ActionJournal, createActionBinding } from '../../host/agent/action-journal.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { createHostComposition } from '../../lae-host.mjs';
import { createMicrosoftGraphTools, MicrosoftGraphProvider, readGraphRestartControl } from '../../host/providers/microsoft-graph.mjs';

const CLIENT_ID = '00001111-aaaa-2222-bbbb-3333cccc4444';
const args = Object.freeze({ to: ['alice@example.com'], subject: 'restart subject', body: 'restart body' });
const preview = Object.freeze({ provider: 'microsoft_graph', action: 'create_draft' });
const engine = Object.freeze({ async *generate() {} });

async function directory(t) {
  const path = await realpath(await mkdtemp(join(tmpdir(), 'lae-graph-restart-')));
  await chmod(path, 0o700); t.after(() => rm(path, { recursive: true, force: true })); return path;
}

async function acknowledgedDraftJournal(t, { state = 'acknowledged', arguments_ = args, toolName = 'mail.create_draft', riskTier = 'T2', sideEffect = 'create_draft' } = {}) {
  const path = await directory(t); let tick = 0;
  const journal = await ActionJournal.open({ directory: path, testOnly: true, idFactory: () => `act_${'1'.repeat(32)}`, now: () => new Date(Date.UTC(2026, 8, 9, 0, 0, tick++)).toISOString() });
  const binding = createActionBinding({ requestId: 'request_restart01', callId: 'call_restart0001', toolName, arguments: arguments_, preview });
  const receipt = await journal.prepare({ requestId: 'request_restart01', callId: 'call_restart0001', toolName, riskTier, sideEffect, argumentsDigest: binding.argumentsDigest, previewDigest: binding.previewDigest, operationDigest: binding.operationDigest });
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
  assert.deepEqual(first, { state: 'completed', examined: 1, completed: 1, blocked: 0, code: null }); assert.deepEqual(second, first);
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
    // A draft written before this slice carries only `operation:digest`. It is
    // well formed and its operation binding matches, but it proves nothing
    // about which account holds it, so it can never complete a record.
    ['legacy two-part marker', ({ binding }) => graphFixture({ drafts: [providerDraft({ id: 'draft-legacy', marker: `${binding.operation_id}:${binding.operation_digest}` })] })],
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
  assert.deepEqual(await controller.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0, blocked: 0, code: null });
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
    assert.deepEqual(await controller.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0, blocked: 0, code: null }); assert.equal(calls, 0); assert.notEqual((await current.detail(id)).state, 'completed');
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
  assert.deepEqual(await hostileController.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0, blocked: 0, code: null }); assert.equal(completes, 0);

  let getterCalls = 0; const accessorSummary = { ...hostile, summary: async () => { const output = {}; Object.defineProperty(output, 'records', { get() { getterCalls += 1; return []; } }); return output; } };
  const accessorController = new ConversationController({ engine, actionJournal: accessorSummary, toolRegistry: noProvider });
  assert.deepEqual(await accessorController.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0, blocked: 0, code: null }); assert.equal(getterCalls, 0);

  const stable = { ...hostile, summary: async () => ({ health: {}, total: 0, active: 0, records: [] }) }; const stableController = new ConversationController({ engine, actionJournal: stable, toolRegistry: noProvider }); stable.summary = async () => { throw new Error('replacement summary must not run'); };
  assert.deepEqual(await stableController.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0, blocked: 0, code: null });
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

// A create_draft flow whose Graph responses each consume a slice of the tool
// budget. `now` is the provider's injected clock, so the walk is deterministic
// and no real time is spent.
function budgetFixture({ candidates = 20, stepMs = 1000 } = {}) {
  let clock = 0; let posts = 0; let gets = 0; let lists = 0;
  const ids = Array.from({ length: candidates }, (_, index) => `draft-budget-${index}`);
  const transport = { request: async request => {
    clock += stepMs;
    if (request.method === 'POST' && request.path === '/v1.0/me/messages') { posts += 1; request.onDispatch?.(); throw Object.assign(new Error('response never arrived'), { code: 'provider_timeout' }); }
    if (request.path === '/v1.0/me/mailFolders/drafts/messages') { lists += 1; return { status: 200, body: { value: ids.map(id => ({ id })) } }; }
    if (request.path.startsWith('/v1.0/me/messages/')) {
      gets += 1; const id = decodeURIComponent(request.path.slice('/v1.0/me/messages/'.length));
      return { status: 200, body: { id, subject: 'unrelated subject', body: { contentType: 'Text', content: 'unrelated body' }, toRecipients: [{ emailAddress: { address: 'other@example.com' } }], ccRecipients: [], internetMessageHeaders: [], changeKey: 'unrelated-change-key' } };
    }
    throw new Error('unexpected injected request');
  } };
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport, now: () => clock });
  return { provider, counts: () => ({ posts, gets, lists }), clock: () => clock };
}

test('in-flight draft proof stops inside the tool budget and reports a typed inconclusive result', async t => {
  const direct = budgetFixture();
  const binding = createActionBinding({ requestId: 'request_budget001', callId: 'call_budget00001', toolName: 'mail.create_draft', arguments: args, preview });
  const tools = createMicrosoftGraphTools(direct.provider);
  const request = { id: 'call_budget00001', name: 'mail.create_draft', arguments: args };
  await tools['mail.create_draft'].preview(request);
  const output = await tools['mail.create_draft'].execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: { operation_id: `act_${'4'.repeat(32)}`, operation_digest: binding.operationDigest, arguments_digest: binding.argumentsDigest, preview_digest: binding.previewDigest } } });
  // A budget overrun is data, not a thrown tool failure.
  assert.equal(output.status, 'ok');
  const payload = JSON.parse(output.content[0].text);
  assert.equal(payload.code, 'provider_action_reconciling'); assert.equal(payload.reconciliation, 'draft_proof_budget_exhausted');
  assert.equal(payload.completed, false); assert.equal(payload.provider_completion, 'unverified'); assert.equal(payload.state, 'reconciling');
  const counts = direct.counts();
  assert.equal(counts.posts, 1); assert.equal(counts.lists, 1);
  assert.ok(counts.gets > 0 && counts.gets < 20, `expected a partial bounded walk, saw ${counts.gets}`);
  assert.ok(direct.clock() < 10000, 'the proof walk must stop inside the 10 s mail.create_draft budget');

  // The same overrun through the production controller leaves the durable
  // record `reconciling`. `unknown_manual` is the state this must never
  // regress to: it is excluded from every automatic recovery path.
  const live = budgetFixture();
  const journal = await ActionJournal.open({ directory: await directory(t), testOnly: true });
  const events = [];
  const turnEngine = { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_budget_turn', name: 'mail.create_draft', arguments: args }) }; else { yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' }; } } };
  const controller = new ConversationController({ engine: turnEngine, actionJournal: journal, toolRegistry: createMicrosoftGraphTools(live.provider) });
  const pending = controller.runTurn({ sessionId: 'ses_budget0001', requestId: 'req_budget0001', message: 'draft it', onEvent: event => events.push(event) });
  while (!events.some(event => event.event === 'tool.confirmation_required')) await new Promise(resolve => setImmediate(resolve));
  const confirmation = events.find(event => event.event === 'tool.confirmation_required');
  assert.equal(controller.confirm(confirmation.data.confirmation_id, true, { requestId: 'req_budget0001', callId: 'call_budget_turn' }), true);
  assert.equal((await pending).state, 'COMPLETED');
  const record = (await journal.summary()).records[0];
  assert.equal(record.state, 'reconciling');
  assert.equal(live.counts().posts, 1);
});

test('proof request budgets are clamped, refuse an unusable remainder, and shorten the transport deadline', async () => {
  let clock = 0;
  const provider = new MicrosoftGraphProvider({ enabled: true, requestTimeoutMs: 5000, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async request => new Promise((_, reject) => { request.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { code: 'ETIMEDOUT' })), { once: true }); }) }, now: () => clock });
  assert.deepEqual(provider.proofRequestBudget(null), { allowed: true, timeoutMs: undefined });
  assert.deepEqual(provider.proofRequestBudget(1000), { allowed: true, timeoutMs: 1000 });
  assert.deepEqual(provider.proofRequestBudget(100000), { allowed: true, timeoutMs: 5000 });
  clock = 900; assert.deepEqual(provider.proofRequestBudget(1000), { allowed: false, timeoutMs: undefined });
  clock = 1000; assert.deepEqual(provider.proofRequestBudget(1000), { allowed: false, timeoutMs: undefined });
  clock = 5000; assert.deepEqual(provider.proofRequestBudget(1000), { allowed: false, timeoutMs: undefined });
  // The budget can only shorten the configured transport deadline.
  for (const invalid of [0, -1, 5001, 1.5, '100']) await assert.rejects(provider.request({ method: 'GET', path: '/v1.0/me', query: {}, timeoutMs: invalid }), error => error.code === 'provider_invalid_request');
  const started = Date.now();
  await assert.rejects(provider.request({ method: 'GET', path: '/v1.0/me', query: {}, timeoutMs: 60 }), error => error.code === 'provider_timeout');
  assert.ok(Date.now() - started < 4000, 'a bounded proof request must not wait for the full transport timeout');
});

test('a restart pass reads authorization state without mutating it and never issues a request without a live token', async t => {
  const saved = await acknowledgedDraftJournal(t);
  const revoked = [];
  const grantStore = { get: () => null, revoke: capability => { revoked.push(capability); return true; }, subscribe: () => () => {} };
  const identity = graphFixture(); await identity.provider.startAuth();
  const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${identity.provider.getAccountFingerprint()}`;
  // A restarted host holds no token: memory-only device-code credentials never
  // survive a process boundary.
  const cold = graphFixture({ drafts: [providerDraft({ id: 'draft-cold', marker })] });
  Object.assign(cold.provider, { grantStore });
  let coldRequests = 0; const coldTransport = cold.provider.transport.request;
  cold.provider.transport.request = async request => { coldRequests += 1; return coldTransport(request); };
  const coldController = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(cold.provider) });
  assert.deepEqual(await coldController.reconcileRestartActions(), { state: 'completed', examined: 1, completed: 0, blocked: 0, code: null });
  assert.equal(coldRequests, 0);
  assert.deepEqual(revoked, []);
  assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged');

  // An authenticated pass must not re-run the account check either. `status()`
  // mutates authorization state and reinstalls the session fingerprint on the
  // way out, which would make the post-proof epoch guard compare against a
  // value sampled after the clear it exists to detect.
  const warm = graphFixture({ drafts: [providerDraft({ id: 'draft-warm', marker })] });
  Object.assign(warm.provider, { grantStore });
  await warm.provider.startAuth();
  const accountChecks = () => warm.calls.filter(entry => entry.path === '/v1.0/me').length;
  const beforePass = accountChecks();
  const warmController = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(warm.provider) });
  assert.deepEqual(await warmController.reconcileRestartActions(), { state: 'completed', examined: 1, completed: 1, blocked: 0, code: null });
  assert.equal(accountChecks(), beforePass);
  assert.deepEqual(revoked, []);
  assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'completed');
});

test('a token that cannot outlive the pass and the credential refresh threshold is refused', async t => {
  // `now` is 0 for this fixture, so `expiresAt` is the remaining lifetime.
  // 75 s clears `getAccessToken`'s own 60 s refresh threshold today but would
  // cross it during the 30 s pass, so the precondition must refuse it.
  for (const [label, remainingMs, expected] of [['75 s remaining', 75000, 0], ['120 s remaining', 120000, 1]]) {
    const saved = await acknowledgedDraftJournal(t);
    const revoked = [];
    const grantStore = { get: () => null, revoke: capability => { revoked.push(capability); return true; }, subscribe: () => () => {} };
    const identity = graphFixture(); await identity.provider.startAuth();
    const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${identity.provider.getAccountFingerprint()}`;
    const fixture = graphFixture({ drafts: [providerDraft({ id: 'draft-liveness', marker })] });
    Object.assign(fixture.provider, { grantStore });
    await fixture.provider.startAuth();
    fixture.provider.credentialSource.cached = { ...fixture.provider.credentialSource.cached, expiresAt: remainingMs };
    const before = fixture.calls.length;
    const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
    assert.deepEqual(await controller.reconcileRestartActions(), { state: 'completed', examined: 1, completed: expected, blocked: 0, code: null }, label);
    assert.equal(fixture.calls.length > before, expected === 1, label);
    assert.deepEqual(revoked, [], label);
    assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, expected === 1 ? 'completed' : 'acknowledged', label);
  }
});

test('an auth epoch change during a pass blocks completion even when the same account is re-verified', async t => {
  const saved = await acknowledgedDraftJournal(t);
  let releaseList; const listGate = new Promise(resolve => { releaseList = resolve; });
  const identity = graphFixture(); await identity.provider.startAuth(); const account = identity.provider.getAccountFingerprint();
  const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${account}`;
  const fixture = graphFixture({ drafts: [providerDraft({ id: 'draft-epoch', marker })], pauseList: () => listGate });
  await fixture.provider.startAuth();
  assert.equal(fixture.provider.verifiedAccountFingerprint(), account);
  const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
  const pending = controller.reconcileRestartActions();
  while (fixture.counts().list === 0) await new Promise(resolve => setImmediate(resolve));
  fixture.provider.clearAuth(); await fixture.provider.startAuth();
  // The identity is restored, so only the epoch sampled before the proof can
  // detect that the operator cleared the session mid-pass.
  assert.equal(fixture.provider.verifiedAccountFingerprint(), account);
  releaseList();
  assert.deepEqual(await pending, { state: 'completed', examined: 1, completed: 0, blocked: 0, code: null });
  assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged');
});

test('a durable complete failure is reported as a typed blocked count and leaves the record untouched', async t => {
  const cases = [
    ['typed journal failure', Object.assign(new Error('blocked'), { code: 'action_journal_blocked' }), 'action_journal_blocked'],
    ['untyped failure', new Error('opaque internal detail'), 'action_journal_complete_failed'],
    ['foreign typed failure', Object.assign(new Error('opaque internal detail'), { code: 'provider_unauthorized' }), 'action_journal_complete_failed'],
  ];
  for (const [label, failure, expected] of cases) {
    const saved = await acknowledgedDraftJournal(t);
    const identity = graphFixture(); await identity.provider.startAuth();
    const marker = `${saved.binding.operation_id}:${saved.binding.operation_digest}:${identity.provider.getAccountFingerprint()}`;
    const restarted = graphFixture({ drafts: [providerDraft({ id: 'draft-blocked', marker })] }); await restarted.provider.startAuth();
    const wrapper = Object.fromEntries(['health', 'prepare', 'authorize', 'dispatch', 'acknowledge', 'beginReconciliation', 'cancel', 'failDefinitive', 'markUnknown'].map(name => [name, (...input) => saved.journal[name](...input)]));
    wrapper.summary = (...input) => saved.journal.summary(...input);
    wrapper.complete = async () => { throw failure; };
    const controller = new ConversationController({ engine, actionJournal: wrapper, toolRegistry: createMicrosoftGraphTools(restarted.provider) });
    const output = await controller.reconcileRestartActions();
    assert.deepEqual(output, { state: 'degraded', examined: 1, completed: 0, blocked: 1, code: expected }, label);
    // Metadata only: no message, no operation identity, no content.
    assert.equal(JSON.stringify(output).includes('opaque internal detail'), false, label);
    assert.equal(JSON.stringify(output).includes(saved.binding.operation_id), false, label);
    assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged', label);
  }
});

test('acknowledged records for other Graph tools are skipped with zero provider callbacks', async t => {
  const saved = await acknowledgedDraftJournal(t, { toolName: 'mail.send_draft', riskTier: 'T3', sideEffect: 'send_mail', arguments_: { draft_id: 'draft-existing' } });
  const fixture = graphFixture(); await fixture.provider.startAuth();
  let calls = 0; fixture.provider.transport.request = async () => { calls += 1; throw new Error('must not call provider'); };
  const controller = new ConversationController({ engine, actionJournal: saved.journal, toolRegistry: createMicrosoftGraphTools(fixture.provider) });
  assert.deepEqual(await controller.reconcileRestartActions(), { state: 'completed', examined: 0, completed: 0, blocked: 0, code: null });
  assert.equal(calls, 0);
  assert.equal((await saved.journal.detail(saved.binding.operation_id)).state, 'acknowledged');
});

test('production composition startup invokes the restart reconciliation pass on its own controller', async t => {
  const original = ConversationController.prototype.reconcileRestartActions;
  t.after(() => { Object.defineProperty(ConversationController.prototype, 'reconcileRestartActions', { value: original, writable: true, enumerable: false, configurable: true }); });
  let passes = 0; let observed = null; let passArguments = null;
  Object.defineProperty(ConversationController.prototype, 'reconcileRestartActions', { value: function patched(...input) { passes += 1; observed = this; passArguments = input; return original.apply(this, input); }, writable: true, enumerable: false, configurable: true });
  const composition = await createHostComposition({ fileConfig: {}, env: { LAE_ENGINE_MODE: 'fixture' }, engineFactory: async () => ({ async *generate() {}, async shutdown() {} }) });
  t.after(() => composition.host.close());
  assert.equal(passes, 1);
  assert.equal(observed, composition.controller);
  assert.deepEqual(passArguments, []);
});

test('a successful device-code authentication triggers exactly one restart reconciliation pass', async t => {
  const settle = async predicate => { for (let attempt = 0; attempt < 5000 && !predicate(); attempt += 1) await new Promise(resolve => setImmediate(resolve)); };
  const build = start => {
    let passes = 0; let passArguments = null;
    const controller = { cancelActive() {}, reconcileRestartActions: (...input) => { passes += 1; passArguments = input; return Promise.resolve({ state: 'unavailable', examined: 0, completed: 0, blocked: 0, code: 'action_journal_unavailable' }); } };
    const host = new HostServer({ controller, engine: { async shutdown() {} }, providerAuth: () => ({ microsoft_graph: { configured: true, start, status: () => null, cancel: () => {}, clear: () => {} } }) });
    return { host, passes: () => passes, passArguments: () => passArguments };
  };
  let starts = 0;
  const accepted = build(async () => { starts += 1; return 'account-fingerprint'; });
  const acceptedAddress = await accepted.host.listen(0); t.after(() => accepted.host.close());
  const headers = { authorization: `Bearer ${acceptedAddress.token}`, 'content-type': 'application/json' };
  const response = await fetch(`${acceptedAddress.url}/api/provider-auth/microsoft_graph/start`, { method: 'POST', headers, body: '{}' });
  assert.equal(response.status, 202); await response.json();
  await settle(() => accepted.passes() > 0);
  assert.equal(starts, 1); assert.equal(accepted.passes(), 1); assert.deepEqual(accepted.passArguments(), []);

  // A failed authentication must never run the pass: the proof path requires a
  // freshly verified canonical account.
  const refused = build(async () => { throw new Error('device flow refused'); });
  const refusedAddress = await refused.host.listen(0); t.after(() => refused.host.close());
  const refusedResponse = await fetch(`${refusedAddress.url}/api/provider-auth/microsoft_graph/start`, { method: 'POST', headers: { ...headers, authorization: `Bearer ${refusedAddress.token}` }, body: '{}' });
  assert.equal(refusedResponse.status, 202); await refusedResponse.json();
  await settle(() => false);
  assert.equal(refused.passes(), 0);
});
