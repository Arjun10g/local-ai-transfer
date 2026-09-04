import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createExternalToolRegistry } from '../../host/providers/index.mjs';
import { MicrosoftGraphProvider, createMicrosoftGraphTools } from '../../host/providers/microsoft-graph.mjs';
import { CopilotCliProvider, createCopilotTool } from '../../host/providers/copilot-cli.mjs';
import { OperatorGrantStore } from '../../host/providers/operator-grants.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { mergeConfig, validateConfig } from '../../host/agent/config.mjs';

const call = (name, arguments_, id = `call_${name.replaceAll('.', '_')}`) => ({ id, name, arguments: arguments_ });
const value = result => JSON.parse(result.content[0].text);
const revision = 'a'.repeat(64);

test('external contract publishes complete strict schemas for every tool', async () => {
  const contract = JSON.parse(await readFile(new URL('../../contracts/external-tools/v0.1.0.json', import.meta.url), 'utf8'));
  assert.equal(contract.version, '0.1.0'); assert.equal(contract.additionalProperties, false);
  assert.equal(new Set(contract.tools.map(tool => tool.name)).size, 13);
  for (const tool of contract.tools) { assert.equal(tool.input_schema.type, 'object'); assert.equal(tool.input_schema.additionalProperties, false); assert.ok(typeof tool.input_schema.properties === 'object'); }
});

test('Graph provider is disabled/auth typed and rejects unknown or oversized arguments', async () => {
  const tools = createMicrosoftGraphTools(); const disabled = value(await tools['mail.list_messages'].execute(call('mail.list_messages', {}))); assert.equal(disabled.code, 'provider_disabled');
  const names = ['mail.list_messages', 'mail.read_message', 'mail.create_draft', 'mail.send_draft', 'mail.mark_read', 'teams.list_chats', 'teams.list_messages', 'teams.send_message'];
  for (const name of names) assert.equal(tools[name].input_schema.additionalProperties, false);
  for (const name of names) await assert.rejects(() => tools[name].execute(call(name, { unknown: true })), error => error.code === 'invalid_tool_arguments');
  await assert.rejects(() => tools['mail.create_draft'].execute(call('mail.create_draft', { to: ['not-an-address'], subject: 'x', body: 'x' })), error => error.code === 'invalid_tool_arguments');
  const unauthorized = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => { throw new Error('no account'); } }, transport: { request: async () => ({ status: 200, body: {} }) } });
  const authResult = value(await unauthorized['mail.list_messages'].execute(call('mail.list_messages', {}))); assert.equal(authResult.code, 'provider_unauthorized');
  const offline = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => { throw Object.assign(new Error('offline'), { code: 'offline' }); } } }); const offlineResult = value(await offline['mail.list_messages'].execute(call('mail.list_messages', {}))); assert.equal(offlineResult.code, 'provider_offline');
});

test('Graph fake transport receives fixed /me endpoints and bounded projections', async () => {
  const requests = []; const transport = { request: async request => { requests.push(request); if (request.path.includes('/messages/') && request.method === 'GET') return { status: 200, body: { id: 'm-1', receivedDateTime: '2026-09-04T00:00:00Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Subject', isRead: false, importance: 'high', body: { content: '<script>evil()</script><style>x{}</style><b>Body &amp; more</b>' } } }; return { status: 200, body: { value: [{ id: 'm-1', receivedDateTime: '2026-09-04T00:00:00Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Subject', isRead: false, importance: 'high', bodyPreview: '<b>Preview</b>', headers: 'secret', webUrl: 'https://teams.microsoft.com/l/chat/0/0' }] } }; } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport });
  const listed = value(await tools['mail.list_messages'].execute(call('mail.list_messages', { folder: 'inbox', unread_only: true, limit: 1 }))); assert.equal(listed.messages[0].preview, 'Preview'); assert.equal(listed.messages[0].headers, undefined); assert.equal(requests[0].origin, 'https://graph.microsoft.com'); assert.equal(requests[0].path, '/v1.0/me/mailFolders/inbox/messages'); assert.equal(requests[0].query.$top, 1); assert.equal(requests[0].headers.authorization, 'Bearer synthetic-token');
  const read = value(await tools['mail.read_message'].execute(call('mail.read_message', { message_id: 'm-1', max_bytes: 32 }))); assert.equal(read.message.text, 'Body & more'); assert.equal(requests[1].path, '/v1.0/me/messages/m-1'); assert.equal(requests[1].headers.Prefer, 'outlook.body-content-type="text"'); assert.equal(requests[1].query.$select.includes('body'), true);
  const chats = value(await tools['teams.list_chats'].execute(call('teams.list_chats', { limit: 1 }))); assert.deepEqual(chats.chats, [{ id: 'm-1', topic: '', type: 'unknown', last_updated: null, participants: [] }]); assert.equal(requests[2].path, '/v1.0/me/chats'); assert.equal(requests[2].query.$select.includes('members'), false);
  const messages = value(await tools['teams.list_messages'].execute(call('teams.list_messages', { chat_id: 'chat-1', limit: 1 }))); assert.equal(requests.at(-1).path, '/v1.0/chats/chat-1/messages'); assert.deepEqual(requests.at(-1).query, { '$top': 1 }); assert.equal(messages.messages[0].web_url, 'https://teams.microsoft.com/l/chat/0/0');
});

test('Graph writes require preview, bind proposals, retain high-impact confirmation, and deduplicate retries', async () => {
  const requests = []; const transport = { request: async request => { requests.push(request); if (request.method === 'GET' && request.path.includes('/me/messages/')) return { status: 200, body: { id: 'draft-1', subject: 'Existing', body: { content: 'Existing draft' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], '@odata.etag': 'etag-1' } }; return request.method === 'POST' && request.path.endsWith('/messages') ? { status: 201, body: { id: 'draft-1' } } : { status: 204, body: {} }; } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport });
  const createCall = call('mail.create_draft', { to: ['alice@example.com'], cc: ['bob@example.com'], subject: 'Hello', body: 'Synthetic body' }, 'call_create_draft');
  const unproposed = value(await tools['mail.create_draft'].execute(createCall)); assert.equal(unproposed.code, 'provider_permission_insufficient');
  const preview = await tools['mail.create_draft'].preview(createCall); assert.deepEqual(preview.recipients, ['alice@example.com', 'bob@example.com']); const created = value(await tools['mail.create_draft'].execute({ ...createCall, authorization: { kind: 'user_confirmation' } })); assert.equal(created.accepted, true); assert.equal(created.idempotency, 'new');
  const retried = value(await tools['mail.create_draft'].execute({ ...createCall, authorization: { kind: 'user_confirmation' } })); assert.equal(retried.idempotency, 'replayed'); assert.equal(requests.filter(request => request.path === '/v1.0/me/messages').length, 1); assert.deepEqual(requests[0].body.toRecipients, [{ emailAddress: { address: 'alice@example.com' } }]);
  const sendCall = call('mail.send_draft', { draft_id: 'draft-1' }, 'call_send_draft'); const sendPreview = await tools['mail.send_draft'].preview(sendCall); assert.match(sendPreview.proposal_revision, /^[a-f0-9]{64}$/); assert.equal(tools['mail.send_draft'].confirmationRequired(sendCall), true); const sent = value(await tools['mail.send_draft'].execute({ ...sendCall, authorization: { kind: 'user_confirmation' } })); assert.equal(sent.resource_id, 'draft-1'); assert.equal(requests.at(-1).headers['Idempotency-Key'].includes('call_send_draft'), true);
  const stale = call('mail.mark_read', { message_id: 'm-1', is_read: true }, 'call_mark'); await tools['mail.mark_read'].preview(stale); const changed = value(await tools['mail.mark_read'].execute(call('mail.mark_read', { message_id: 'm-2', is_read: true }, 'call_mark'))); assert.equal(changed.code, 'provider_permission_insufficient');
});

test('full_access is operator-scoped, revocable, and never model-selected', async () => {
  let now = 1000; const grants = new OperatorGrantStore({ now: () => now }); const grant = grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'account', profile: 'full_access', expiresAt: 5000 });
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-2' } }) }, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-1' }); const tools = createMicrosoftGraphTools(provider); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }); await tools['mail.create_draft'].preview(draft); assert.equal(tools['mail.create_draft'].confirmationRequired(draft), false); const authorization = await tools['mail.create_draft'].authorize(draft); assert.equal(authorization.kind, 'operator_grant'); assert.equal(value(await tools['mail.create_draft'].execute({ ...draft, authorization })).accepted, true); grants.revoke('microsoft.graph.mail'); assert.equal(tools['mail.create_draft'].confirmationRequired(draft), true); const revoked = value(await tools['mail.create_draft'].execute({ ...draft, authorization })); assert.equal(revoked.code, 'provider_permission_revoked'); assert.equal(grant.profile, 'full_access'); now = 6000; assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'account' }), false);
});

test('operator grant replacement invalidates the prior generation and profile cannot widen access', async () => {
  const grants = new OperatorGrantStore(); let revoked = 0; grants.subscribe('microsoft.graph.mail', () => { revoked += 1; });
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1', profile: 'ask_before_writes' });
  assert.equal(grants.get('microsoft.graph.mail').profile, 'ask_before_writes');
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-2', profile: 'full_access' });
  assert.equal(revoked, 1); assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-2', scope: 'account' }), true);
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-3' } }) }, permissionProfile: 'always_ask', grantStore: grants, accountFingerprint: 'acct-2' });
  assert.equal(provider.grantValid('microsoft.graph.mail'), false);
});

test('Graph grant revocation aborts an in-flight routine write', async () => {
  const grants = new OperatorGrantStore(); grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-2', profile: 'full_access' });
  const transport = { request: async request => await new Promise((resolve, reject) => { request.signal.addEventListener('abort', () => reject(Object.assign(new Error('cancelled'), { code: 'provider_cancelled' }))); }) };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-2' }); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_revoke'); await tools['mail.create_draft'].preview(draft); const authorization = await tools['mail.create_draft'].authorize(draft); const pending = tools['mail.create_draft'].execute({ ...draft, authorization }); await new Promise(resolve => setImmediate(resolve)); grants.revoke('microsoft.graph.mail'); const revoked = value(await pending); assert.equal(revoked.code, 'provider_cancelled');
});

test('Graph transport timeout is bounded and typed', async () => {
  const tools = createMicrosoftGraphTools({ enabled: true, requestTimeoutMs: 100, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => new Promise(() => {}) } });
  const started = Date.now(); const output = value(await tools['mail.list_messages'].execute(call('mail.list_messages', {}))); assert.equal(output.code, 'provider_timeout'); assert.ok(Date.now() - started < 1000);
});

test('Graph writes are at-most-once across concurrent and timeout retries', async () => {
  let dispatches = 0; const transport = { request: async request => { dispatches += 1; await new Promise((resolve, reject) => request.signal.addEventListener('abort', () => reject(Object.assign(new Error('timed out'), { code: 'ETIMEDOUT' })))); } };
  const tools = createMicrosoftGraphTools({ enabled: true, requestTimeoutMs: 100, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport }); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_once'); await tools['mail.create_draft'].preview(draft); const authorization = { kind: 'user_confirmation' }; const first = tools['mail.create_draft'].execute({ ...draft, authorization }); await new Promise(resolve => setImmediate(resolve)); const concurrent = value(await tools['mail.create_draft'].execute({ ...draft, authorization })); assert.equal(concurrent.code, 'provider_write_already_attempted'); const timedOut = value(await first); assert.equal(timedOut.code, 'provider_timeout'); const retry = value(await tools['mail.create_draft'].execute({ ...draft, authorization })); assert.equal(retry.code, 'provider_timeout'); assert.equal(retry.idempotency, 'replayed'); assert.equal(dispatches, 1);
});

test('controller advertises external parameters and distinguishes operator authorization from user confirmation', async () => {
  const grants = new OperatorGrantStore(); grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-controller', profile: 'full_access' }); let requests = 0; const provider = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-controller', credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => { requests += 1; return { status: 201, body: { id: 'draft-controller' } }; } } }); const registry = createMicrosoftGraphTools(provider); let advertised;
  const engine = { async *generate({ tools, messages }) { advertised = tools.find(item => item.function.name === 'mail.create_draft'); if (!messages.some(item => item.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_controller', name: 'mail.create_draft', arguments: { to: ['alice@example.com'], subject: 'x', body: 'x' } }) }; return; } yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const events = []; const controller = new ConversationController({ engine, toolRegistry: registry }); const output = await controller.runTurn({ sessionId: 'ses_controller', requestId: 'req_controller', message: 'draft a message', onEvent: event => events.push(event) }); assert.equal(output.state, 'COMPLETED'); assert.deepEqual(advertised.function.parameters, registry['mail.create_draft'].parameters); assert.equal(events.some(event => event.event === 'tool.confirmation_required'), false); assert.equal(events.find(event => event.event === 'tool.started').data.authorization, 'operator_grant'); assert.equal(requests, 1);
});

test('permission profiles apply confirmation floors to reads, writes, and T3 actions', () => {
  const names = ['mail.list_messages', 'mail.create_draft', 'mail.send_draft']; const expected = { always_ask: [true, true, true], ask_before_writes: [false, true, true], review_important_actions: [false, false, true], full_access: [true, true, true] };
  for (const [profile, values] of Object.entries(expected)) { const provider = new MicrosoftGraphProvider({ permissionProfile: profile }); assert.deepEqual(names.map(name => provider.confirmationRequired(name)), values); }
});

test('provider configuration is strict, secret-free, and preserves injected runtime wiring', () => {
  const config = mergeConfig({ providers: { microsoft_graph: { enabled: true, permission_profile: 'ask_before_writes', account_fingerprint: 'acct', scope: 'account' }, copilot: { enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3' } } }); assert.equal(config.providers.microsoft_graph.enabled, true); assert.throws(() => validateConfig({ providers: { microsoft_graph: { token: 'must-not-be-configured' } } }), /unknown key/); assert.throws(() => validateConfig({ providers: { unknown: {} } }), /unknown key/); const registry = createExternalToolRegistry({ config: config.providers }); assert.equal(registry['mail.list_messages'].execute !== undefined, true); assert.equal(registry['mail.list_messages'].parameters.additionalProperties, false);
});

test('registry maps protected config keys explicitly and exposes provider state separately', async () => {
  assert.deepEqual(createExternalToolRegistry().providerStatus(), { microsoft_graph: 'disabled', copilot: 'disabled' }); const unconfigured = createExternalToolRegistry({ config: { microsoft_graph: { enabled: true, permission_profile: 'full_access', account_fingerprint: 'acct-config', scope: 'mailbox-1' } } }); assert.deepEqual(unconfigured.providerStatus(), { microsoft_graph: 'unconfigured', copilot: 'disabled' }); assert.throws(() => createExternalToolRegistry({ graph: { token: 'not-an-option' } }), /unknown option/);
  const grants = new OperatorGrantStore(); grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-config', scope: 'mailbox-1', profile: 'full_access' }); const registry = createExternalToolRegistry({ config: { microsoft_graph: { enabled: true, permission_profile: 'full_access', account_fingerprint: 'acct-config', scope: 'mailbox-1' } }, graph: { credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-config' } }) }, grantStore: grants } }); assert.deepEqual(registry.providerStatus(), { microsoft_graph: 'ready', copilot: 'disabled' }); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_config'); await registry['mail.create_draft'].preview(draft); assert.equal((await registry['mail.create_draft'].authorize(draft)).kind, 'operator_grant');
});

class FakeChild extends EventEmitter {
  constructor({ finish = true } = {}) { super(); this.stdout = new EventEmitter(); this.stderr = new EventEmitter(); this.input = ''; this.finish = finish; this.stdin = { end: (text) => { this.input = text; if (this.finish) queueMicrotask(() => { this.stdout.emit('data', Buffer.from('copilot response')); this.emit('close', 0); }); } }; }
  kill() { this.emit('close', null); }
}

test('Copilot bridge uses stdin/minimal environment and exposes explicit cloud-egress preview', async () => {
  const children = []; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3', versionCheck: async () => true, environment: { PATH: '/approved', SECRET_TOKEN: 'must-not-pass' }, readContext: Object.assign(async () => 'const x = 1;', { estimate: async () => 12 }), spawn: (executable, args, options) => { const child = new FakeChild(); children.push({ executable, args, options, child }); return child; } }); const tool = createCopilotTool(provider); assert.equal(tool.input_schema.additionalProperties, false); const copilotCall = call('coding.copilot_ask', { prompt: 'Explain this', workspace_id: 'project', context_paths: ['src/index.js'] }, 'call_copilot'); const preview = await tool.preview(copilotCall); assert.equal(preview.destination, 'GitHub Copilot cloud'); assert.equal(preview.egress_bytes, 44); const output = value(await tool.execute({ ...copilotCall, authorization: { kind: 'user_confirmation' } })); assert.equal(output.stdout, 'copilot response'); assert.equal(output.cli_version, '1.2.3'); assert.equal(children[0].executable, '/approved/copilot'); assert.deepEqual(children[0].args, ['-s', '--no-auto-update', '--no-custom-instructions', '--no-remote', '--no-remote-export', '--no-ask-user', '--disable-builtin-mcps', '--available-tools=']); assert.equal(children[0].options.shell, false); assert.deepEqual(children[0].options.env, { PATH: '/approved' }); assert.match(children[0].child.input, /const x = 1/);
  await assert.rejects(() => tool.preview(call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: ['..\\secret'] }, 'call_path')), error => error.code === 'invalid_tool_arguments');
  const disabled = createCopilotTool(); const disabledCall = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_disabled'); await disabled.preview(disabledCall); const failure = value(await disabled.execute({ ...disabledCall, authorization: { kind: 'user_confirmation' } })); assert.equal(failure.code, 'copilot_cli_unavailable');
});

test('Copilot cancellation does not wait for orphans', async () => {
  let killed = 0; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => '', killProcess: () => { killed += 1; }, spawn: () => new FakeChild({ finish: false }) }); const tool = createCopilotTool(provider); const c = new AbortController(); const request = call('coding.copilot_ask', { prompt: 'cancel', workspace_id: 'project', context_paths: [] }, 'call_cancel'); await tool.preview(request); const pending = tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, signal: c.signal }); await new Promise(resolve => setImmediate(resolve)); c.abort(); const output = value(await pending); assert.equal(output.code, 'provider_cancelled'); assert.equal(killed, 1);
});

test('Copilot rejects version mismatch and never returns stderr or broad flags', async () => {
  let child; let spawnArgs; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => '', spawn: (executable, args) => { spawnArgs = args; child = new FakeChild(); queueMicrotask(() => { child.stderr.emit('data', Buffer.from('secret stderr')); child.stdout.emit('data', Buffer.from('answer')); child.emit('close', 0); }); return child; } }); const tool = createCopilotTool(provider); const request = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_redact'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.stdout, 'answer'); assert.equal(JSON.stringify(output).includes('secret stderr'), false); assert.ok(spawnArgs.every(arg => !arg.includes('allow-all'))); assert.equal(spawnArgs.includes('--prompt'), false);
  const mismatch = createCopilotTool({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => false, readContext: async () => '' }); const mismatchCall = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_mismatch'); await mismatch.preview(mismatchCall); assert.equal(value(await mismatch.execute({ ...mismatchCall, authorization: { kind: 'user_confirmation' } })).code, 'copilot_policy_denied');
});

test('Copilot binds context at preview and decodes split UTF-8 output safely', async () => {
  let context = 'stable'; let child; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => context, spawn: () => { child = new FakeChild({ finish: false }); queueMicrotask(() => { child.stdout.emit('data', Buffer.from([0xf0])); child.stdout.emit('data', Buffer.from([0x9f, 0x98, 0x80])); child.emit('close', 0); }); return child; } }); const tool = createCopilotTool(provider); const request = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: ['src/index.js'] }, 'call_utf8'); await tool.preview(request); context = 'changed'; assert.equal(value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })).code, 'copilot_policy_denied');
  context = 'stable'; const next = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: ['src/index.js'] }, 'call_utf8_next'); await tool.preview(next); assert.equal(value(await tool.execute({ ...next, authorization: { kind: 'user_confirmation' } })).stdout, '😀');
});
