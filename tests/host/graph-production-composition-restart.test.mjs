import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createExternalToolRegistry, MicrosoftGraphProvider, MicrosoftDeviceCodeCredential, OperatorGrantStore } from '../../host/providers/index.mjs';
import { createMicrosoftGraphTools } from '../../host/providers/microsoft-graph.mjs';
import { readGraphReadAttestation } from '../../host/providers/microsoft-graph-reads.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { mergeConfig } from '../../host/agent/config.mjs';
import { createHostComposition } from '../../lae-host.mjs';

const clientId = '00001111-aaaa-2222-bbbb-3333cccc4444';
const call = (name, arguments_, id) => ({ id: id ?? `call_${name.replaceAll('.', '_')}`, name, arguments: arguments_ });
const json = result => JSON.parse(result.content[0].text);
const authHeaders = address => ({ authorization: `Bearer ${address.token}`, 'content-type': 'application/json' });
const assertNoCredentialFields = value => assert.doesNotMatch(JSON.stringify(value), /"(?:access_token|device_code|client_id|tenant|scopes|accountFingerprint|raw_error)"\s*:/iu);
const assertNoCredentialValues = (value, secrets) => { const serialized = JSON.stringify(value); for (const secret of secrets) assert.equal(serialized.includes(secret), false, 'public auth output contains a private value'); assertNoCredentialFields(value); };
function deferred() { let resolve; let reject; const promise = new Promise((resolvePromise, rejectPromise) => { resolve = resolvePromise; reject = rejectPromise; }); return { promise, resolve, reject }; }
const nextTurn = () => new Promise(resolve => setImmediate(resolve));

function fakeGraphProvider({ requests = [], response, grantStore } = {}) {
  const credentialSource = { getAccessToken: async () => 't'.repeat(32) };
  const transport = { request: async request => {
    requests.push(request);
    if (response) return response(request);
    return { status: 200, body: { value: [] } };
  } };
  const provider = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource, transport, grantStore, accountFingerprint: 'acct-test', testOnly: true });
  return { provider, transport };
}

test('production Graph composition has no credential or transport injection path', async t => {
  const hostApi = JSON.parse(await readFile(new URL('../../contracts/host-api/contract.json', import.meta.url), 'utf8'));
  const ui = await readFile(new URL('../../ui/app.js', import.meta.url), 'utf8');
  assert.deepEqual(hostApi.exact_bodies.provider_auth_microsoft_graph.public_status_fields, ['state', 'prompt', 'account_verified']);
  assert.deepEqual(hostApi.exact_bodies.provider_auth_microsoft_graph.public_prompt_fields, ['userCode', 'verificationUri']);
  assert.equal(hostApi.exact_bodies.provider_auth_microsoft_graph.public_status_excludes.includes('accountFingerprint'), true);
  assert.match(ui, /account_verified/); assert.doesNotMatch(ui, /accountFingerprint/);

  const graphConfig = { enabled: true, tenant: 'organizations', client_id: clientId, scopes: ['User.Read', 'Mail.Read'] };
  let engineShutdowns = 0;
  const composition = await createHostComposition({ fileConfig: { providers: { microsoft_graph: graphConfig } }, env: { LAE_ENGINE_MODE: 'fixture' }, engineFactory: async () => ({ async *generate() {}, async shutdown() { engineShutdowns += 1; } }) });
  t.after(() => composition.host.close());
  assert.equal(composition.host instanceof HostServer, true);
  assert.equal(composition.externalTools.providerAuthStatus().microsoft_graph.state, 'idle');
  assert.deepEqual(composition.externalTools.providerStatus(), { microsoft_graph: 'ready', copilot: 'disabled', browser_actions: 'disabled' });
  assert.equal(engineShutdowns, 0);
  assert.equal(composition.host.server, null);
  assert.equal(Object.hasOwn(composition.externalTools.providerAuthStatus().microsoft_graph, 'accountFingerprint'), true);
  const issuedStatus = composition.externalTools.providerAuthStatus().microsoft_graph;
  assert.equal(Object.isFrozen(issuedStatus), true);
  assert.throws(() => { issuedStatus.accountVerified = true; }, TypeError);
  for (const key of ['token', 'access_token', 'credential_source', 'transport', 'auth_transport']) {
    assert.throws(() => mergeConfig({ providers: { microsoft_graph: { [key]: 'opaque' } } }), /unknown key/u);
  }
});

test('controller advertises Graph reads but refuses hidden durable writes before provider transport without a journal', async () => {
  const requests = [];
  const { provider } = fakeGraphProvider({ requests, response: async () => ({ status: 200, body: { value: [] } }) });
  const registry = createExternalToolRegistry({ graph: provider });
  const advertised = [];
  let generation = 0;
  const engine = { async *generate({ tools }) {
    advertised.push(tools.map(tool => tool.function.name));
    if (generation++ === 0) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('mail.list_messages', { limit: 1 }, 'call_graph_read')) }; return; }
    yield { kind: 'text_delta', text: 'read complete' }; yield { kind: 'done' };
  } };
  const controller = new ConversationController({ engine, toolRegistry: registry });
  const result = await controller.runTurn({ sessionId: 'session_graph_read', requestId: 'request_graph_read', message: 'read mail' });
  assert.equal(result.state, 'COMPLETED');
  assert.equal(advertised[0].includes('mail.list_messages'), true);
  for (const name of ['mail.create_draft', 'mail.send_draft', 'mail.mark_read', 'teams.send_message']) assert.equal(advertised[0].includes(name), false, name);
  assert.equal(requests.length, 1);

  let tokenReads = 0; const hiddenRequests = [];
  const hidden = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource: { getAccessToken: async () => { tokenReads += 1; return 'u'.repeat(32); } }, transport: { request: async request => { hiddenRequests.push(request); return { status: 201, body: { id: 'unexpected' } }; } }, accountFingerprint: 'acct-hidden', testOnly: true });
  const hiddenRegistry = createExternalToolRegistry({ graph: hidden });
  const hiddenEngine = { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('mail.create_draft', { to: ['alice@example.com'], subject: 'safe', body: 'safe' }, 'call_hidden_write')) }; } };
  const hiddenController = new ConversationController({ engine: hiddenEngine, toolRegistry: hiddenRegistry });
  const refused = await hiddenController.runTurn({ sessionId: 'session_hidden_write', requestId: 'request_hidden_write', message: 'create a draft' });
  assert.equal(refused.state, 'FAILED');
  assert.equal(refused.error, 'action_journal_unavailable');
  assert.equal(hiddenRequests.length, 0);
  assert.equal(tokenReads, 0);
});

test('new Graph composition has no stale auth, grant, proposal, write ledger, cursor, or read attestation authority', async () => {
  const firstRequests = [];
  const firstGrants = new OperatorGrantStore();
  firstGrants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-test', scope: 'account' });
  const first = fakeGraphProvider({ requests: firstRequests, response: request => {
    if (request.method === 'POST') { request.onDispatch?.(); return { status: 201, body: { id: 'draft-first' } }; }
    return { status: 200, body: { value: [
      { id: 'message-first', receivedDateTime: '2026-09-05T12:34:56Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Subject', isRead: false, importance: 'normal', bodyPreview: 'Body' },
      { id: 'message-second', receivedDateTime: '2026-09-05T12:35:56Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Subject 2', isRead: true, importance: 'normal', bodyPreview: 'Body 2' }
    ] } };
  }, grantStore: firstGrants });
  const firstTools = createMicrosoftGraphTools(first.provider);
  const draftCall = call('mail.create_draft', { to: ['alice@example.com'], subject: 'Subject', body: 'Body' }, 'call_stale_draft');
  await firstTools['mail.create_draft'].preview(draftCall);
  const firstWrite = json(await firstTools['mail.create_draft'].execute({ ...draftCall, authorization: { kind: 'user_confirmation' } }));
  assert.equal(firstWrite.accepted, true);
  const readCall = call('mail.list_messages', { limit: 1 }, 'call_first_page');
  const firstPageResult = await firstTools['mail.list_messages'].execute(readCall);
  const firstPage = json(firstPageResult);
  assert.equal(typeof firstPage.next_page, 'string');
  assert.ok(readGraphReadAttestation(firstPageResult));
  assert.equal(readGraphReadAttestation(structuredClone(firstPageResult)), null);
  assert.equal(first.provider.proposals.size > 0, true);
  assert.equal(first.provider.writeLedger.size > 0, true);
  assert.equal(first.provider.readBoundary.pages.size, 1);
  assert.ok(firstGrants.get('microsoft.graph.mail'));

  const secondRequests = [];
  const second = fakeGraphProvider({ requests: secondRequests });
  const secondTools = createMicrosoftGraphTools(second.provider);
  assert.equal(second.provider.proposals.size, 0);
  assert.equal(second.provider.writeLedger.size, 0);
  assert.equal(second.provider.readBoundary.pages.size, 0);
  assert.equal(second.provider.grantStore, undefined);
  const staleWrite = json(await secondTools['mail.create_draft'].execute({ ...draftCall, authorization: { kind: 'user_confirmation' } }));
  assert.equal(staleWrite.code, 'provider_permission_insufficient');
  const stalePage = json(await secondTools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: firstPage.next_page }, 'call_stale_page')));
  assert.equal(stalePage.code, 'provider_invalid_request');
  assert.equal(secondRequests.length, 0);

  let firstAuthRequests = 0;
  const authTransport = { request: async request => {
    if (request.path.endsWith('/devicecode')) { firstAuthRequests += 1; return { status: 200, body: { device_code: 'a'.repeat(32), user_code: 'AUTH-1234', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } }; }
    if (request.path.endsWith('/token')) { firstAuthRequests += 1; return { status: 200, body: { access_token: 'k'.repeat(32), expires_in: 3600, scope: 'User.Read Mail.Read' } }; }
    firstAuthRequests += 1; return { status: 200, body: { id: 'restart-account' } };
  } };
  const authenticated = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read', 'Mail.Read'], transport: authTransport, sleep: async () => {} });
  await authenticated.startAuth();
  assert.equal(authenticated.authStatus().state, 'authenticated');
  const restarted = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read', 'Mail.Read'], transport: { request: async () => { throw new Error('restart must not authenticate'); } }, sleep: async () => {} });
  assert.equal(restarted.authStatus().state, 'idle');
  assert.equal(restarted.authStatus().prompt, null);
  await assert.rejects(() => restarted.credentialSource.getAccessToken(), error => error.code === 'provider_unauthorized');
  assert.equal(firstAuthRequests, 3);
});

test('same credential cancel detaches the old run and late device/sleep work cannot overwrite a restart', async () => {
  const deviceRuns = [deferred(), deferred()]; let deviceRequests = 0; const callbacks = [];
  const transport = { request: async request => {
    if (request.path.endsWith('/devicecode')) return deviceRuns[deviceRequests++].promise;
    throw new Error('late run must not reach token transport');
  } };
  const sleepGate = deferred();
  const credential = new MicrosoftDeviceCodeCredential({ tenant: 'organizations', clientId, scopes: ['User.Read'], transport, sleep: async () => sleepGate.promise, onUserCode: value => callbacks.push(value.userCode) });
  const oldRun = credential.start().catch(error => error.code);
  const coalescedRun = credential.start().catch(error => error.code);
  await nextTurn();
  assert.equal(deviceRequests, 1);
  credential.cancel();
  const freshRun = credential.start().catch(error => error.code);
  await nextTurn();
  deviceRuns[0].resolve({ status: 200, body: { device_code: 'o'.repeat(32), user_code: 'OLD0-CODE', verification_uri: 'https://microsoft.com/devicelogin' } });
  await nextTurn();
  assert.equal(deviceRequests, 2);
  assert.equal(credential.authStatus().state, 'requesting_device_code');
  assert.deepEqual(callbacks, []);
  deviceRuns[1].resolve({ status: 200, body: { device_code: 'n'.repeat(32), user_code: 'NEW0-CODE', verification_uri: 'https://microsoft.com/devicelogin' } });
  await nextTurn();
  assert.equal(credential.authStatus().state, 'awaiting_user');
  assert.deepEqual(callbacks, ['NEW0-CODE']);
  credential.cancel(); sleepGate.resolve();
  assert.deepEqual(await Promise.all([oldRun, coalescedRun, freshRun]), ['provider_cancelled', 'provider_cancelled', 'provider_cancelled']);
  assert.equal(credential.authStatus().state, 'idle');
  assert.equal(credential.authStatus().prompt, null);
});

test('late abort-insensitive token completion cannot authenticate or clear a newer run', async () => {
  const oldToken = deferred(); const sleepGates = []; let deviceRequests = 0; let tokenRequests = 0;
  const transport = { request: async request => {
    if (request.path.endsWith('/devicecode')) { deviceRequests += 1; return { status: 200, body: { device_code: `device-${deviceRequests}`, user_code: `CODE-000${deviceRequests}`, verification_uri: 'https://microsoft.com/devicelogin' } }; }
    tokenRequests += 1; return oldToken.promise;
  } };
  const credential = new MicrosoftDeviceCodeCredential({ tenant: 'organizations', clientId, scopes: ['User.Read'], transport, sleep: async () => { const gate = deferred(); sleepGates.push(gate); return gate.promise; } });
  const oldRun = credential.start().catch(error => error.code);
  await nextTurn(); sleepGates[0].resolve();
  for (let attempt = 0; attempt < 10 && tokenRequests < 1; attempt++) await nextTurn();
  assert.equal(tokenRequests, 1);
  credential.cancel();
  const freshRun = credential.start().catch(error => error.code);
  await nextTurn();
  assert.equal(deviceRequests, 2);
  assert.equal(credential.authStatus().state, 'awaiting_user');
  oldToken.resolve({ status: 200, body: { access_token: 'late-token'.repeat(8), expires_in: 3600, scope: 'User.Read' } });
  await nextTurn();
  assert.equal(credential.authStatus().state, 'awaiting_user');
  assert.equal(credential.cached, null);
  credential.cancel(); sleepGates[1].resolve();
  assert.deepEqual(await Promise.all([oldRun, freshRun]), ['provider_cancelled', 'provider_cancelled']);
  assert.equal(credential.authStatus().state, 'idle');
});

test('authenticated Graph clear removes token, auth fingerprint projection, prompt, and grants', async () => {
  const transport = { request: async request => {
    if (request.path.endsWith('/devicecode')) return { status: 200, body: { device_code: 'c'.repeat(32), user_code: 'CLEAR-CODE', verification_uri: 'https://microsoft.com/devicelogin' } };
    if (request.path.endsWith('/token')) return { status: 200, body: { access_token: 'z'.repeat(32), expires_in: 3600, scope: 'User.Read Mail.Read' } };
    return { status: 200, body: { id: 'clear-account' } };
  } };
  const grants = new OperatorGrantStore();
  const provider = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', tenant: 'organizations', clientId, scopes: ['User.Read', 'Mail.Read'], transport, grantStore: grants, sleep: async () => {} });
  await provider.startAuth();
  const fingerprint = provider.getAccountFingerprint();
  for (const capability of ['microsoft.graph.mail', 'microsoft.graph.teams']) grants.grant({ capability, provider: 'microsoft_graph', accountFingerprint: fingerprint });
  assert.equal(provider.authStatus().state, 'authenticated');
  assert.equal(provider.authStatus().accountFingerprint, fingerprint);
  assert.equal(provider.authStatus().accountVerified, true);
  provider.authenticatedAccountFingerprint = 'opaque-account';
  assert.equal(provider.authStatus().accountVerified, false);
  provider.authenticatedAccountFingerprint = fingerprint;
  provider.clearAuth();
  assert.deepEqual(provider.authStatus(), { state: 'idle', prompt: null, accountFingerprint: null, accountVerified: false });
  assert.equal(provider.credentialSource.authStatus().state, 'idle');
  assert.equal(provider.credentialSource.cached, null);
  assert.equal(grants.get('microsoft.graph.mail'), null);
  assert.equal(grants.get('microsoft.graph.teams'), null);
  await assert.rejects(() => provider.credentialSource.getAccessToken(), error => error.code === 'provider_unauthorized');
});

test('Graph account verification rejects whitespace/control /me IDs while allowing safe string identities', async () => {
  for (const invalidId of ['                ', 'bad\u0000id', '']) {
    const transport = { request: async request => {
      if (request.path.endsWith('/devicecode')) return { status: 200, body: { device_code: 'v'.repeat(32), user_code: 'VALID-0001', verification_uri: 'https://microsoft.com/devicelogin' } };
      if (request.path.endsWith('/token')) return { status: 200, body: { access_token: 'w'.repeat(32), expires_in: 3600, scope: 'User.Read' } };
      return { status: 200, body: { id: invalidId } };
    } };
    const provider = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read'], transport, sleep: async () => {} });
    await assert.rejects(() => provider.startAuth(), error => error.code === 'provider_unauthorized');
    assert.equal(provider.authStatus().state, 'idle');
    assert.equal(provider.authStatus().accountVerified, false);
    assert.equal(provider.authStatus().accountFingerprint, null);
    assert.equal(provider.credentialSource.cached, null);
  }
});

test('Graph clear detaches learned identity while preserving an explicit account pin', async () => {
  let identity = 'account-a';
  const transport = { request: async request => {
    if (request.path.endsWith('/devicecode')) return { status: 200, body: { device_code: 'p'.repeat(32), user_code: 'PINNED-0001', verification_uri: 'https://microsoft.com/devicelogin' } };
    if (request.path.endsWith('/token')) return { status: 200, body: { access_token: 'q'.repeat(32), expires_in: 3600, scope: 'User.Read' } };
    return { status: 200, body: { id: identity } };
  } };
  const learned = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read'], transport, sleep: async () => {} });
  await learned.startAuth();
  const accountA = learned.getAccountFingerprint();
  assert.equal(learned.authStatus().accountVerified, true);
  learned.clearAuth();
  assert.equal(learned.getAccountFingerprint(), 'unknown');
  assert.equal(learned.authStatus().accountVerified, false);
  identity = 'account-b';
  await learned.startAuth();
  assert.notEqual(learned.getAccountFingerprint(), accountA);
  assert.equal(learned.authStatus().accountVerified, true);

  identity = 'account-a';
  const pinned = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read'], accountFingerprint: accountA, transport, sleep: async () => {} });
  await pinned.startAuth();
  pinned.clearAuth();
  assert.equal(pinned.getAccountFingerprint(), accountA);
  assert.equal(pinned.authStatus().accountVerified, false);
  identity = 'account-b';
  await assert.rejects(() => pinned.startAuth(), error => error.code === 'provider_unauthorized');
  assert.equal(pinned.getAccountFingerprint(), accountA);
  assert.equal(pinned.authStatus().accountVerified, false);
});

test('host Graph auth controls expose bounded prompt state but no credential material', async t => {
  let deviceRequests = 0;
  const transport = { request: async request => {
    if (request.path.endsWith('/devicecode')) { deviceRequests += 1; return { status: 200, body: { device_code: 'd'.repeat(32), user_code: 'ABC123XYZ', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } }; }
    throw new Error('unexpected auth completion');
  } };
  const sleep = (_milliseconds, signal) => new Promise((resolve, reject) => {
    const abort = () => { signal?.removeEventListener('abort', abort); reject(Object.assign(new Error('cancelled'), { code: 'provider_cancelled' })); };
    signal?.addEventListener('abort', abort, { once: true });
  });
  const provider = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read'], transport, sleep });
  const registry = createExternalToolRegistry({ graph: provider });
  const host = new HostServer({ controller: { cancelActive() {} }, engine: { async shutdown() {} }, providerAuth: registry.providerAuthControl, providerShutdown: registry.shutdown });
  const address = await host.listen(0); t.after(() => host.close());
  const request = async (path, options = {}) => { const response = await fetch(`${address.url}${path}`, { ...options, headers: { ...authHeaders(address), ...(options.headers ?? {}) } }); return { response, body: await response.json() }; };
  const initial = await request('/api/provider-auth/microsoft_graph');
  assert.equal(initial.body.microsoft_graph.state, 'idle');
  assert.equal(initial.body.microsoft_graph.prompt, null);
  const start = await request('/api/provider-auth/microsoft_graph/start', { method: 'POST', body: '{}' });
  assert.equal(start.response.status, 202);
  assertNoCredentialFields(start.body);
  for (let attempt = 0; attempt < 20 && provider.authStatus().state !== 'awaiting_user'; attempt++) await new Promise(resolve => setImmediate(resolve));
  const pending = await request('/api/provider-auth/microsoft_graph');
  assert.equal(pending.body.microsoft_graph.state, 'awaiting_user');
  assert.equal(pending.body.microsoft_graph.prompt.userCode, 'ABC123XYZ');
  assert.equal(pending.body.microsoft_graph.prompt.userCode.length <= 128, true);
  assert.equal(pending.body.microsoft_graph.prompt.verificationUri.length <= 256, true);
  assertNoCredentialFields(pending.body);
  const cancelled = await request('/api/provider-auth/microsoft_graph/cancel', { method: 'POST', body: '{}' });
  assert.equal(cancelled.body.status.state, 'idle');
  assert.equal(cancelled.body.status.prompt, null);
  assertNoCredentialFields(cancelled.body);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(deviceRequests, 1);
  const cleared = await request('/api/provider-auth/microsoft_graph/clear', { method: 'POST', body: '{}' });
  assert.equal(cleared.body.status.state, 'idle');
  assert.equal(cleared.body.status.prompt, null);
  assertNoCredentialFields(cleared.body);
});

test('HostServer applies the same explicit auth projection to status and every control response', async t => {
  const secrets = ['KNOWN-ACCESS-TOKEN', 'KNOWN-DEVICE-CODE', clientId, 'KNOWN-FINGERPRINT', 'raw provider error text'];
  const raw = { state: 'authenticated', accountFingerprint: 'KNOWN-FINGERPRINT', accountVerified: false, prompt: { userCode: 'SAFE-CODE', verificationUri: 'https://microsoft.com/devicelogin', expiresAt: 123 }, access_token: 'KNOWN-ACCESS-TOKEN', device_code: 'KNOWN-DEVICE-CODE', renamed: { bearer: 'KNOWN-ACCESS-TOKEN', client: clientId, nested: ['KNOWN-DEVICE-CODE', 'KNOWN-FINGERPRINT'] }, raw_error: 'raw provider error text', arbitrary: 'must-drop' };
  const control = { configured: true, start: () => {}, status: () => raw, cancel: () => {}, clear: () => {} };
  const host = new HostServer({ controller: { cancelActive() {} }, engine: { async shutdown() {} }, providerAuth: () => ({ microsoft_graph: control }) });
  const address = await host.listen(0); t.after(() => host.close());
  const headers = authHeaders(address);
  const get = await fetch(`${address.url}/api/provider-auth/microsoft_graph`, { headers });
  const getBody = await get.json();
  assert.deepEqual(Object.keys(getBody.microsoft_graph).sort(), ['account_verified', 'prompt', 'state']);
  assert.equal(getBody.microsoft_graph.state, 'unavailable');
  assert.equal(getBody.microsoft_graph.prompt, null);
  assert.equal(getBody.microsoft_graph.account_verified, false);
  assertNoCredentialValues(getBody, secrets);
  for (const action of ['start', 'cancel', 'clear']) {
    const response = await fetch(`${address.url}/api/provider-auth/microsoft_graph/${action}`, { method: 'POST', headers, body: '{}' });
    const body = await response.json();
    assert.deepEqual(Object.keys(body.status).sort(), ['account_verified', 'prompt', 'state']);
    assert.equal(body.status.state, 'unavailable');
    assert.equal(body.status.prompt, null);
    assert.equal(body.status.account_verified, false);
    assertNoCredentialValues(body, secrets);
  }

  const rawError = 'raw provider error text';
  const failingControl = { configured: true, start: () => { throw Object.assign(new Error(rawError), { code: rawError }); }, status: () => { throw Object.assign(new Error(rawError), { code: rawError }); }, cancel: () => { throw Object.assign(new Error(rawError), { code: rawError }); }, clear: () => { throw Object.assign(new Error(rawError), { code: rawError }); } };
  const failingHost = new HostServer({ controller: { cancelActive() {} }, engine: { async shutdown() {} }, providerAuth: () => ({ microsoft_graph: failingControl }) });
  const failingAddress = await failingHost.listen(0); t.after(() => failingHost.close());
  const failingHeaders = authHeaders(failingAddress);
  const failingGet = await fetch(`${failingAddress.url}/api/provider-auth/microsoft_graph`, { headers: failingHeaders });
  assertNoCredentialValues(await failingGet.json(), [rawError]);
  for (const action of ['start', 'cancel', 'clear']) {
    const response = await fetch(`${failingAddress.url}/api/provider-auth/microsoft_graph/${action}`, { method: 'POST', headers: failingHeaders, body: '{}' });
    assertNoCredentialValues(await response.json(), [rawError]);
  }

  const factorySentinel = 'provider-auth-factory-secret-error';
  const factoryHost = new HostServer({ controller: { cancelActive() {} }, engine: { async shutdown() {} }, providerAuth: () => { throw new Error(factorySentinel); } });
  const factoryAddress = await factoryHost.listen(0); t.after(() => factoryHost.close());
  const factoryHeaders = authHeaders(factoryAddress);
  const factoryGet = await fetch(`${factoryAddress.url}/api/provider-auth/microsoft_graph`, { headers: factoryHeaders });
  assert.equal(factoryGet.status, 200); assert.deepEqual((await factoryGet.json()).microsoft_graph, { state: 'unavailable', prompt: null, account_verified: false });
  for (const action of ['start', 'cancel', 'clear']) {
    const response = await fetch(`${factoryAddress.url}/api/provider-auth/microsoft_graph/${action}`, { method: 'POST', headers: factoryHeaders, body: '{}' });
    assert.equal(response.status, 409); assertNoCredentialValues(await response.json(), [factorySentinel]);
  }

  const getterSentinel = 'provider-auth-getter-secret-error';
  const providerAuthValue = {};
  Object.defineProperty(providerAuthValue, 'microsoft_graph', { get() { throw new Error(getterSentinel); } });
  const getterHost = new HostServer({ controller: { cancelActive() {} }, engine: { async shutdown() {} }, providerAuth: () => providerAuthValue });
  const getterAddress = await getterHost.listen(0); t.after(() => getterHost.close());
  const getterHeaders = authHeaders(getterAddress);
  const getterGet = await fetch(`${getterAddress.url}/api/provider-auth/microsoft_graph`, { headers: getterHeaders });
  assert.equal(getterGet.status, 200); assert.deepEqual((await getterGet.json()).microsoft_graph, { state: 'unavailable', prompt: null, account_verified: false });
  for (const action of ['start', 'cancel', 'clear']) {
    const response = await fetch(`${getterAddress.url}/api/provider-auth/microsoft_graph/${action}`, { method: 'POST', headers: getterHeaders, body: '{}' });
    assert.equal(response.status, 409); assertNoCredentialValues(await response.json(), [getterSentinel]);
  }
});

test('auth projection drops every secret-shaped user code on every public auth endpoint', async t => {
  const sentinels = ['KNOWN-ACCESS-TOKEN', 'KNOWN-DEVICE-CODE', clientId, 'KNOWN-FINGERPRINT', 'raw provider error text'];
  let currentCode = sentinels[0];
  const control = { configured: true, start: () => {}, status: () => ({ state: 'awaiting_user', accountVerified: false, prompt: { userCode: currentCode, verificationUri: 'https://microsoft.com/devicelogin' } }), cancel: () => {}, clear: () => {} };
  const host = new HostServer({ controller: { cancelActive() {} }, engine: { async shutdown() {} }, providerAuth: () => ({ microsoft_graph: control }) });
  const address = await host.listen(0); t.after(() => host.close());
  const headers = authHeaders(address);
  for (const sentinel of sentinels) {
    currentCode = sentinel;
    const get = await fetch(`${address.url}/api/provider-auth/microsoft_graph`, { headers });
    const getBody = await get.json();
    assert.equal(getBody.microsoft_graph.prompt, null);
    assertNoCredentialValues(getBody, sentinels);
    for (const action of ['start', 'cancel', 'clear']) {
      const response = await fetch(`${address.url}/api/provider-auth/microsoft_graph/${action}`, { method: 'POST', headers, body: '{}' });
      const body = await response.json();
      assert.equal(body.status.prompt, null);
      assertNoCredentialValues(body, sentinels);
    }
  }
});

test('Graph delegated scopes and operator grants stay bound to account and scope', async () => {
  const noTransport = { request: async () => { throw new Error('transport must not be called'); } };
  const scoped = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read', 'Mail.Read'], transport: noTransport });
  assert.equal(scoped.configuredToolNames().includes('mail.list_messages'), true);
  assert.equal(scoped.configuredToolNames().includes('mail.create_draft'), false);
  assert.equal(scoped.configuredToolNames().includes('teams.list_chats'), false);

  let requests = 0; const grants = new OperatorGrantStore();
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-bound', scope: 'account' });
  const provider = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', accountFingerprint: 'acct-bound', scope: 'account', grantStore: grants, credentialSource: { getAccessToken: async () => 'g'.repeat(32) }, transport: { request: async request => { requests += 1; request.onDispatch?.(); return { status: 201, body: { id: 'draft-bound' } }; } }, testOnly: true });
  const tool = createMicrosoftGraphTools(provider)['mail.create_draft'];
  const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'Bound', body: 'Bound' }, 'call_bound');
  await tool.preview(draft);
  const authorization = await tool.authorize(draft);
  assert.equal(authorization.kind, 'operator_grant');
  assert.equal(json(await tool.execute({ ...draft, authorization })).accepted, true);
  assert.equal(requests, 1);

  const foreign = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', accountFingerprint: 'acct-foreign', scope: 'account', grantStore: grants, credentialSource: { getAccessToken: async () => 'f'.repeat(32) }, transport: { request: async () => { requests += 1; return { status: 201, body: { id: 'unexpected' } }; } }, testOnly: true });
  const foreignTool = createMicrosoftGraphTools(foreign)['mail.create_draft'];
  const foreignCall = call('mail.create_draft', { to: ['alice@example.com'], subject: 'Foreign', body: 'Foreign' }, 'call_foreign');
  await foreignTool.preview(foreignCall);
  assert.equal((await foreignTool.authorize(foreignCall)).kind, 'policy');
  assert.equal(json(await foreignTool.execute(foreignCall)).code, 'provider_permission_insufficient');
  const wrongScope = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', accountFingerprint: 'acct-bound', scope: 'other', grantStore: grants, credentialSource: { getAccessToken: async () => 's'.repeat(32) }, transport: { request: async () => { requests += 1; return { status: 201, body: { id: 'unexpected' } }; } }, testOnly: true });
  const wrongScopeTool = createMicrosoftGraphTools(wrongScope)['mail.create_draft'];
  const wrongScopeCall = call('mail.create_draft', { to: ['alice@example.com'], subject: 'Other', body: 'Other' }, 'call_scope');
  await wrongScopeTool.preview(wrongScopeCall);
  assert.equal((await wrongScopeTool.authorize(wrongScopeCall)).kind, 'policy');
  assert.equal(json(await wrongScopeTool.execute(wrongScopeCall)).code, 'provider_permission_insufficient');
  assert.equal(requests, 1);
});
