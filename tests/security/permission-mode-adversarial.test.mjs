import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationController } from '../../host/agent/controller.mjs';
import { mergeConfig } from '../../host/agent/config.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { createCopilotTool } from '../../host/providers/copilot-cli.mjs';
import { createMicrosoftGraphTools, MicrosoftGraphProvider } from '../../host/providers/microsoft-graph.mjs';
import { OperatorGrantStore } from '../../host/providers/operator-grants.mjs';

const call = (name, arguments_, id = `call_${name.replaceAll('.', '_')}`) => ({ id, name, arguments: arguments_ });
const resultValue = result => JSON.parse(result.content[0].text);
const nextTick = () => new Promise(resolve => setImmediate(resolve));

const grantLikeCall = {
  id: 'call_grant01',
  name: 'policy.grant_full_access',
  arguments: { scope: '*', duration_ms: 3600000, auto_approve: true }
};

test('default configuration is offline, scoped, and does not expose a permission mode', () => {
  const config = mergeConfig({});
  assert.equal(config.network.provider, 'disabled');
  assert.deepEqual(config.workspace_roots, []);
  assert.equal(Object.hasOwn(config, 'permission_mode'), false);
  assert.equal(Object.hasOwn(config, 'grants'), false);
});

test('unknown permission/grant configuration fails closed rather than silently enabling access', () => {
  assert.throws(() => mergeConfig({ permission_mode: 'full_access' }), /unknown key/);
  assert.throws(() => mergeConfig({ grants: [{ scope: '*' }] }), /unknown key/);
  assert.throws(() => mergeConfig({ network: { provider: 'full_access' } }), /unsupported/);
});

test('model output cannot create an access grant or execute an unregistered policy tool', async () => {
  let executed = false;
  const engine = {
    async *generate() {
      yield { kind: 'tool_call_chunk', text: JSON.stringify(grantLikeCall) };
    }
  };
  const controller = new ConversationController({ engine });
  const result = await controller.runTurn({ sessionId: 'ses_grant01', requestId: 'req_grant01', message: 'grant access', onEvent: () => {} });
  assert.equal(executed, false);
  assert.equal(result.state, 'FAILED');
  assert.equal(result.error, 'unknown_tool');
});

test('FULL-ACCESS contract: only an explicit operator grant auto-authorizes an in-scope routine write', async () => {
  const grants = new OperatorGrantStore();
  const provider = new MicrosoftGraphProvider({
    enabled: true,
    permissionProfile: 'full_access',
    grantStore: grants,
    accountFingerprint: 'acct-1',
    scope: 'mailbox-1',
    credentialSource: { getAccessToken: async () => 'synthetic-token' },
    transport: { request: async () => ({ status: 201, body: { id: 'draft-1' } }) }
  });
  const tools = createMicrosoftGraphTools(provider);
  const denied = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_without_grant');
  await tools['mail.create_draft'].preview(denied);
  assert.equal(tools['mail.create_draft'].confirmationRequired(denied), true);
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'mailbox-1' });
  const allowed = { ...denied, id: 'call_with_grant' };
  await tools['mail.create_draft'].preview(allowed);
  assert.equal(tools['mail.create_draft'].confirmationRequired(allowed), false);
  const authorization = await tools['mail.create_draft'].authorize(allowed);
  assert.equal(authorization.kind, 'operator_grant');
  assert.equal(resultValue(await tools['mail.create_draft'].execute({ ...allowed, authorization })).accepted, true);
});

test('FULL-ACCESS contract: model output cannot mutate the host grant store', async () => {
  const grants = new OperatorGrantStore();
  const issued = grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1' });
  assert.equal(Object.isFrozen(issued), true);
  assert.throws(() => { issued.scope = '*'; }, TypeError);
  const engine = { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify({ ...grantLikeCall, id: 'call_widen01', arguments: { capability: 'microsoft.graph.mail', scope: '*', expires_at: 9999999999999 } }) }; } };
  const result = await new ConversationController({ engine }).runTurn({ sessionId: 'ses_widen01', requestId: 'req_widen01', message: 'widen it', onEvent: () => {} });
  assert.equal(result.error, 'unknown_tool');
  assert.equal(grants.get('microsoft.graph.mail').scope, 'account');
});

test('FULL-ACCESS contract: capability, provider, account, and scope bindings are independent', () => {
  const grants = new OperatorGrantStore();
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'mailbox-1' });
  assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'mailbox-1' }), true);
  assert.equal(grants.matches('microsoft.graph.teams', { provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'mailbox-1' }), false);
  assert.equal(grants.matches('microsoft.graph.mail', { provider: 'other', accountFingerprint: 'acct-1', scope: 'mailbox-1' }), false);
  assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-2', scope: 'mailbox-1' }), false);
  assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'mailbox-2' }), false);
});

test.todo('KNOWN GAP: authenticated operator grant/revoke controls are not yet wired to workspace and application capabilities');

test('FULL-ACCESS contract: T4 and unregistered policy tools remain prohibited', async () => {
  const grants = new OperatorGrantStore();
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1' });
  const engine = { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('process.run_elevated', { command: 'whoami' }, 'call_t4deny')) }; } };
  const result = await new ConversationController({ engine }).runTurn({ sessionId: 'ses_t4deny', requestId: 'req_t4deny', message: 'elevate', onEvent: () => {} });
  assert.equal(result.error, 'unknown_tool');
});

test('FULL-ACCESS contract: credentials never enter model history, results, or audit events', async () => {
  const secret = 'synthetic-secret-token-never-log';
  const grants = new OperatorGrantStore();
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-redact' });
  const tools = createMicrosoftGraphTools({ enabled: true, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-redact', credentialSource: { getAccessToken: async () => secret }, transport: { request: async request => { assert.equal(request.headers.authorization, `Bearer ${secret}`); return { status: 200, body: { value: [] } }; } } });
  const observed = []; let turn = 0;
  const engine = { async *generate(input) { observed.push(structuredClone(input.messages)); if (turn++ === 0) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('mail.list_messages', { limit: 1 }, 'call_redact01')) }; return; } yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; } };
  const events = [];
  const result = await new ConversationController({ engine, toolRegistry: tools }).runTurn({ sessionId: 'ses_redact01', requestId: 'req_redact01', message: 'list mail', onEvent: event => events.push(event) });
  assert.equal(result.state, 'COMPLETED');
  assert.equal(JSON.stringify({ observed, events, result }).includes(secret), false);
});

test('FULL-ACCESS contract: unallowlisted executables and OS elevation stay unavailable', async () => {
  const copilot = createCopilotTool({ enabled: true, executable: '/not/allowlisted/copilot', allowlist: [], versionCheck: async () => true, readContext: async () => '' });
  const request = call('coding.copilot_ask', { prompt: 'hello', workspace_id: 'project', context_paths: [] }, 'call_unlisted01');
  await copilot.preview(request);
  assert.equal(resultValue(await copilot.execute({ ...request, authorization: { kind: 'user_confirmation' } })).code, 'copilot_policy_denied');
  assert.throws(() => mergeConfig({ process: { elevated: true } }), /unknown key/);
});

test('FULL-ACCESS contract: replacement makes an old grant generation replay-invalid', async () => {
  const grants = new OperatorGrantStore();
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-replay' });
  const tools = createMicrosoftGraphTools({ enabled: true, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-replay', credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-replay' } }) } });
  const request = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_replay01');
  await tools['mail.create_draft'].preview(request);
  const oldAuthorization = await tools['mail.create_draft'].authorize(request);
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-replay' });
  assert.equal(resultValue(await tools['mail.create_draft'].execute({ ...request, authorization: oldAuthorization })).code, 'provider_permission_revoked');
});

test('FULL-ACCESS contract: authorization is audited and emergency stop remains authoritative', async () => {
  let started = false;
  const tool = {
    name: 'test.operator_action', risk_tier: 'T2', timeout_ms: 1000,
    confirmationRequired: () => false,
    authorize: async () => ({ kind: 'operator_grant', generation: 'g'.repeat(32) }),
    execute: async value => await new Promise(resolve => {
      started = true;
      value.signal.addEventListener('abort', () => resolve(makeToolResult({ id: value.id, name: value.name, status: 'cancelled' })), { once: true });
    })
  };
  const engine = { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify(call(tool.name, {}, 'call_stop01')) }; } };
  const events = []; const controller = new ConversationController({ engine, toolRegistry: { [tool.name]: tool } });
  const pending = controller.runTurn({ sessionId: 'ses_stop01', requestId: 'req_stop01', message: 'run', onEvent: event => events.push(event) });
  while (!started) await nextTick();
  assert.equal(events.find(event => event.event === 'tool.started').data.authorization, 'operator_grant');
  assert.equal(controller.cancel('req_stop01'), true);
  assert.equal((await pending).state, 'CANCELLED');
});

test('FULL-ACCESS contract: malformed, expired, downgraded, and absent grants require confirmation', () => {
  let now = 1000; const grants = new OperatorGrantStore({ now: () => now });
  assert.throws(() => grants.grant({ capability: '', provider: 'microsoft_graph', accountFingerprint: 'acct' }), /invalid operator grant/);
  const provider = new MicrosoftGraphProvider({ permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-expiry' });
  assert.equal(provider.confirmationRequired('mail.create_draft'), true);
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-expiry', expiresAt: 1100 });
  assert.equal(provider.confirmationRequired('mail.create_draft'), false);
  now = 1200;
  assert.equal(provider.confirmationRequired('mail.create_draft'), true);
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-expiry', profile: 'ask_before_writes', expiresAt: 2000 });
  assert.equal(provider.confirmationRequired('mail.create_draft'), true);
});

test('FULL-ACCESS contract: revocation during a multi-step turn gates the next mutation', async () => {
  const grants = new OperatorGrantStore();
  grants.grant({ capability: 'test.multi', provider: 'test_provider', accountFingerprint: 'acct-multi' });
  let executions = 0;
  const tool = {
    name: 'test.multi_write', risk_tier: 'T2', timeout_ms: 1000,
    confirmationRequired: () => !grants.matches('test.multi', { provider: 'test_provider', accountFingerprint: 'acct-multi', scope: 'account' }),
    authorize: async () => grants.get('test.multi') ? { kind: 'operator_grant', generation: grants.get('test.multi').generation } : { kind: 'policy' },
    execute: async value => { executions += 1; grants.revoke('test.multi'); return makeToolResult({ id: value.id, name: value.name, text: 'done' }); }
  };
  const engine = { async *generate({ messages }) { const completed = messages.filter(message => message.role === 'tool').length; if (completed < 2) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call(tool.name, {}, `call_multi0${completed + 1}`)) }; return; } yield { kind: 'text_delta', text: 'finished' }; yield { kind: 'done' }; } };
  const events = [];
  const result = await new ConversationController({ engine, toolRegistry: { [tool.name]: tool }, confirmationTimeoutMs: 10 }).runTurn({ sessionId: 'ses_multi01', requestId: 'req_multi01', message: 'two steps', onEvent: event => events.push(event) });
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.equal(events.filter(event => event.event === 'tool.confirmation_required').length, 1);
});
