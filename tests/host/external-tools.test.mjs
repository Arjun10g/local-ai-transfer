import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import { createExternalToolRegistry } from '../../host/providers/index.mjs';
import { MicrosoftGraphProvider, createMicrosoftGraphTools } from '../../host/providers/microsoft-graph.mjs';
import { CopilotCliProvider, createCopilotTool } from '../../host/providers/copilot-cli.mjs';
import { OperatorGrantStore } from '../../host/providers/operator-grants.mjs';

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
  const requests = []; const transport = { request: async request => { requests.push(request); if (request.path.includes('/messages/') && request.method === 'GET') return { status: 200, body: { id: 'm-1', receivedDateTime: '2026-09-04T00:00:00Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Subject', isRead: false, importance: 'high', body: { content: '<b>Body</b>' } } }; return { status: 200, body: { value: [{ id: 'm-1', receivedDateTime: '2026-09-04T00:00:00Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Subject', isRead: false, importance: 'high', bodyPreview: '<b>Preview</b>', headers: 'secret' }] } }; } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport });
  const listed = value(await tools['mail.list_messages'].execute(call('mail.list_messages', { folder: 'inbox', unread_only: true, limit: 1 }))); assert.equal(listed.messages[0].preview, 'Preview'); assert.equal(listed.messages[0].headers, undefined); assert.equal(requests[0].origin, 'https://graph.microsoft.com'); assert.equal(requests[0].path, '/v1.0/me/mailFolders/inbox/messages'); assert.equal(requests[0].query.$top, 1); assert.equal(requests[0].headers.authorization, 'Bearer synthetic-token');
  const read = value(await tools['mail.read_message'].execute(call('mail.read_message', { message_id: 'm-1', max_bytes: 32 }))); assert.equal(read.message.text, 'Body'); assert.equal(requests[1].path, '/v1.0/me/messages/m-1'); assert.equal(requests[1].query.$select.includes('body'), true);
  const chats = value(await tools['teams.list_chats'].execute(call('teams.list_chats', { limit: 1 }))); assert.deepEqual(chats.chats, [{ id: 'm-1', topic: '', type: 'unknown', last_updated: null, participants: [] }]);
});

test('Graph writes require preview, bind proposals, retain high-impact confirmation, and deduplicate retries', async () => {
  const requests = []; const transport = { request: async request => { requests.push(request); return request.method === 'POST' && request.path.endsWith('/messages') ? { status: 201, body: { id: 'draft-1' } } : { status: 204, body: {} }; } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport });
  const createCall = call('mail.create_draft', { to: ['alice@example.com'], cc: ['bob@example.com'], subject: 'Hello', body: 'Synthetic body' }, 'call_create_draft');
  const unproposed = value(await tools['mail.create_draft'].execute(createCall)); assert.equal(unproposed.code, 'provider_permission_insufficient');
  const preview = await tools['mail.create_draft'].preview(createCall); assert.deepEqual(preview.recipients, ['alice@example.com', 'bob@example.com']); const created = value(await tools['mail.create_draft'].execute({ ...createCall, confirmationApproved: true })); assert.equal(created.accepted, true); assert.equal(created.idempotency, 'new');
  const retried = value(await tools['mail.create_draft'].execute({ ...createCall, confirmationApproved: true })); assert.equal(retried.idempotency, 'replayed'); assert.equal(requests.filter(request => request.path === '/v1.0/me/messages').length, 1); assert.deepEqual(requests[0].body.toRecipients, [{ emailAddress: { address: 'alice@example.com' } }]);
  const sendCall = call('mail.send_draft', { draft_id: 'draft-1', proposal_revision: revision }, 'call_send_draft'); const sendPreview = await tools['mail.send_draft'].preview(sendCall); assert.equal(sendPreview.proposal_revision, revision); assert.equal(tools['mail.send_draft'].confirmationRequired(sendCall), true); const sent = value(await tools['mail.send_draft'].execute({ ...sendCall, confirmationApproved: true })); assert.equal(sent.resource_id, 'draft-1'); assert.equal(requests.at(-1).headers['Idempotency-Key'].includes('call_send_draft'), true);
  const stale = call('mail.mark_read', { message_id: 'm-1', is_read: true }, 'call_mark'); await tools['mail.mark_read'].preview(stale); const changed = value(await tools['mail.mark_read'].execute(call('mail.mark_read', { message_id: 'm-2', is_read: true }, 'call_mark'))); assert.equal(changed.code, 'provider_permission_insufficient');
});

test('full_access is operator-scoped, revocable, and never model-selected', async () => {
  let now = 1000; const grants = new OperatorGrantStore({ now: () => now }); const grant = grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'account', profile: 'full_access', expiresAt: 5000 });
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-2' } }) }, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-1' }); const tools = createMicrosoftGraphTools(provider); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }); await tools['mail.create_draft'].preview(draft); assert.equal(tools['mail.create_draft'].confirmationRequired(draft), false); assert.equal(value(await tools['mail.create_draft'].execute(draft)).accepted, true); grants.revoke('microsoft.graph.mail'); assert.equal(tools['mail.create_draft'].confirmationRequired(draft), true); const revoked = value(await tools['mail.create_draft'].execute({ ...draft, id: 'call_new' })); assert.equal(revoked.code, 'provider_permission_insufficient'); assert.equal(grant.profile, 'full_access'); now = 6000; assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'account' }), false);
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
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-2' }); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_revoke'); await tools['mail.create_draft'].preview(draft); const pending = tools['mail.create_draft'].execute(draft); await new Promise(resolve => setImmediate(resolve)); grants.revoke('microsoft.graph.mail'); const revoked = value(await pending); assert.equal(revoked.code, 'provider_cancelled');
});

test('Graph transport timeout is bounded and typed', async () => {
  const tools = createMicrosoftGraphTools({ enabled: true, requestTimeoutMs: 100, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => new Promise(() => {}) } });
  const started = Date.now(); const output = value(await tools['mail.list_messages'].execute(call('mail.list_messages', {}))); assert.equal(output.code, 'provider_timeout'); assert.ok(Date.now() - started < 1000);
});

class FakeChild extends EventEmitter {
  constructor({ finish = true } = {}) { super(); this.stdout = new EventEmitter(); this.stderr = new EventEmitter(); this.input = ''; this.finish = finish; this.stdin = { end: (text) => { this.input = text; if (this.finish) queueMicrotask(() => { this.stdout.emit('data', Buffer.from('copilot response')); this.emit('close', 0); }); } }; }
  kill() { this.emit('close', null); }
}

test('Copilot bridge uses stdin/minimal environment and exposes explicit cloud-egress preview', async () => {
  const children = []; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3', versionCheck: async () => true, environment: { PATH: '/approved', SECRET_TOKEN: 'must-not-pass' }, readContext: Object.assign(async () => 'const x = 1;', { estimate: async () => 12 }), spawn: (executable, args, options) => { const child = new FakeChild(); children.push({ executable, args, options, child }); return child; } }); const tool = createCopilotTool(provider); assert.equal(tool.input_schema.additionalProperties, false); const copilotCall = call('coding.copilot_ask', { prompt: 'Explain this', workspace_id: 'project', context_paths: ['src/index.js'] }, 'call_copilot'); const preview = await tool.preview(copilotCall); assert.equal(preview.destination, 'GitHub Copilot cloud'); assert.equal(preview.egress_bytes, 24); const output = value(await tool.execute(copilotCall)); assert.equal(output.stdout, 'copilot response'); assert.equal(output.cli_version, '1.2.3'); assert.equal(children[0].executable, '/approved/copilot'); assert.deepEqual(children[0].args, ['--prompt', '-']); assert.equal(children[0].options.shell, false); assert.deepEqual(children[0].options.env, { PATH: '/approved' }); assert.match(children[0].child.input, /const x = 1/);
  const disabled = createCopilotTool(); await disabled.preview(call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_disabled')); const failure = value(await disabled.execute(call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_disabled'))); assert.equal(failure.code, 'copilot_cli_unavailable');
});

test('Copilot cancellation does not wait for orphans', async () => {
  const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => '', spawn: () => new FakeChild({ finish: false }) }); const tool = createCopilotTool(provider); const c = new AbortController(); const request = call('coding.copilot_ask', { prompt: 'cancel', workspace_id: 'project', context_paths: [] }, 'call_cancel'); await tool.preview(request); const pending = tool.execute({ ...request, signal: c.signal }); c.abort(); const output = value(await pending); assert.equal(output.code, 'provider_cancelled');
});
