import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chmod, mkdtemp, realpath, readFile, mkdir, rm, writeFile } from 'node:fs/promises';
import { createExternalToolRegistry } from '../../host/providers/index.mjs';
import { BrowserActionProvider, CdpClient, createBrowserActionTools, browserActionDefinitions, hostIsPrivate, publicAddress, publicUrl } from '../../host/providers/browser-actions.mjs';
import { MicrosoftGraphProvider, MicrosoftDeviceCodeCredential, MicrosoftGraphHttpsTransport, createMicrosoftGraphTools } from '../../host/providers/microsoft-graph.mjs';
import { CopilotCliProvider as ProductionCopilotCliProvider, createCopilotTool as productionCreateCopilotTool, createCopilotVersionCheck, killCopilotProcessTree } from '../../host/providers/copilot-cli.mjs';
import { OperatorGrantStore } from '../../host/providers/operator-grants.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { mergeConfig, validateConfig } from '../../host/agent/config.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { createWorkspaceContextReader } from '../../host/providers/copilot-context.mjs';
import { ActionJournal } from '../../host/agent/action-journal.mjs';

class CopilotCliProvider extends ProductionCopilotCliProvider { constructor(options = {}) { super({ testOnly: true, protocol: 'legacy_stdin', ...options }); } }
const createCopilotTool = options => options instanceof ProductionCopilotCliProvider ? productionCreateCopilotTool(options) : productionCreateCopilotTool({ testOnly: true, protocol: 'legacy_stdin', ...(options ?? {}) });

const call = (name, arguments_, id = `call_${name.replaceAll('.', '_')}`) => ({ id, name, arguments: arguments_ });
const value = result => JSON.parse(result.content[0].text);
const revision = 'a'.repeat(64);
const configuredWorkspace = () => ({ id: 'project', path: '/approved/workspace', read: true, write: true });
async function journal(t) { const path = await realpath(await mkdtemp(join(tmpdir(), 'lae-external-journal-'))); await chmod(path, 0o700); t.after(() => rm(path, { recursive: true, force: true })); return ActionJournal.open({ directory: path, testOnly: true }); }

test('external contract publishes complete strict schemas for every tool', async () => {
  const contract = JSON.parse(await readFile(new URL('../../contracts/external-tools/v0.1.0.json', import.meta.url), 'utf8'));
  assert.equal(contract.version, '0.1.0'); assert.equal(contract.additionalProperties, false);
  assert.equal(new Set(contract.tools.map(tool => tool.name)).size, 17);
  const riskTiers = new Set(contract.$defs.tool.properties.risk_tier.enum); for (const tool of contract.tools) { assert.ok(riskTiers.has(tool.risk_tier), `${tool.name} uses an undeclared risk tier`); assert.equal(tool.input_schema.type, 'object'); assert.equal(tool.input_schema.additionalProperties, false); assert.ok(typeof tool.input_schema.properties === 'object'); } for (const name of ['browser.session_start', 'browser.inspect_links', 'browser.inspect_page', 'browser.follow_link', 'browser.session_close']) { const definition = browserActionDefinitions[name]; assert.equal(definition.parameters.additionalProperties, false); assert.ok(definition.description); } assert.equal(createExternalToolRegistry()['browser.session_start'], undefined); assert.equal(createExternalToolRegistry()['browser.fill_field'], undefined); assert.equal(createExternalToolRegistry()['browser.activate_control'], undefined);
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

test('Graph hostile response arrays are bounded before projection', async () => {
  let observed; const hostile = Array.from({ length: 100000 }, (_, index) => index === 0 ? { id: 'm-safe', subject: 'safe' } : { id: `m-${index}`, subject: 'discarded' }); const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async request => { observed = request; return { status: 200, body: { value: hostile } }; } } });
  const output = value(await tools['mail.list_messages'].execute(call('mail.list_messages', { limit: 1 }))); assert.equal(output.messages.length, 1); assert.equal(output.messages[0].id, 'm-safe'); assert.equal(output.truncated, true); assert.equal(observed.query.$top, 1);
});

test('Graph draft reconciliation uses the private journal marker and never replays an ambiguous create', async () => {
  const binding = { operation_id: `act_${'1'.repeat(32)}`, operation_digest: 'a'.repeat(64), arguments_digest: 'b'.repeat(64), preview_digest: 'c'.repeat(64) };
  const requests = []; const transport = { request: async request => { requests.push(request); if (request.method === 'POST') { request.onDispatch?.(); throw Object.assign(new Error('late timeout'), { code: 'provider_timeout' }); } return { status: 200, body: { value: [{ id: 'draft-reconciled', internetMessageHeaders: [{ name: 'x-lae-operation', value: `${binding.operation_id}:${binding.operation_digest}` }], subject: 'x', body: { content: 'x', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }] }] } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.create_draft']; const request = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_marker'); await tool.preview(request);
  const first = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(first.provider_completion, 'verified'); assert.equal(first.reconciliation, 'unique_exact_draft'); assert.deepEqual(requests[0].body.internetMessageHeaders, [{ name: 'x-lae-operation', value: `${binding.operation_id}:${binding.operation_digest}` }]);
  const second = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(second.code, 'provider_write_already_attempted'); assert.equal(requests.filter(item => item.method === 'POST').length, 1);
});

test('Graph create reconciliation requires exact requested draft content', async () => {
  const binding = { operation_id: `act_${'3'.repeat(32)}`, operation_digest: 'c'.repeat(64), arguments_digest: 'd'.repeat(64), preview_digest: 'e'.repeat(64) };
  const marker = `${binding.operation_id}:${binding.operation_digest}`; let posts = 0;
  const transport = { request: async request => {
    if (request.method === 'POST') { posts += 1; request.onDispatch?.(); throw Object.assign(new Error('late timeout'), { code: 'provider_timeout' }); }
    return { status: 200, body: { value: [{ id: 'draft-mismatch', internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], subject: 'different', body: { content: 'x', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }] }] } };
  } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.create_draft'];
  const request = call('mail.create_draft', { to: ['alice@example.com'], subject: 'requested', body: 'x' }, 'call_marker_mismatch'); await tool.preview(request);
  const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } }));
  assert.equal(output.code, 'provider_action_reconciling'); assert.equal(output.reconciliation, 'draft_not_found'); assert.equal(posts, 1);
});

test('Graph create token failure never reconciles a matching-marker draft before POST dispatch', async () => {
  const binding = { operation_id: `act_${'4'.repeat(32)}`, operation_digest: 'f'.repeat(64), arguments_digest: '0'.repeat(64), preview_digest: '1'.repeat(64) };
  const marker = `${binding.operation_id}:${binding.operation_digest}`; let transportCalls = 0;
  const credentialSource = { getAccessToken: async () => { throw Object.assign(new Error('token timeout'), { code: 'provider_timeout' }); } };
  const transport = { request: async () => { transportCalls += 1; return { status: 200, body: { value: [{ id: 'matching-draft', internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], subject: 'x', body: { content: 'x', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }] }] } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource, transport })['mail.create_draft']; const request = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_create_token_timeout'); await tool.preview(request);
  const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } }));
  assert.equal(output.code, 'provider_timeout'); assert.equal(transportCalls, 0);
});

test('Graph reconciliation requires an acknowledged dispatch and complete collection page', async () => {
  const binding = { operation_id: `act_${'9'.repeat(32)}`, operation_digest: 'a'.repeat(64), arguments_digest: 'b'.repeat(64), preview_digest: 'c'.repeat(64) };
  let mode = 'malformed'; let posts = 0; let reads = 0;
  const transport = { request: async request => {
    if (request.method === 'POST') { posts += 1; return { status: 201, body: { id: 'created-without-ack' } }; }
    reads += 1;
    if (mode === 'malformed') return { status: 200, body: {} };
    return { status: 200, body: { value: [{ id: 'draft-page', internetMessageHeaders: [{ name: 'x-lae-operation', value: `${binding.operation_id}:${binding.operation_digest}` }], subject: 'x', body: { content: 'x', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }] }], '@odata.nextLink': 'https://graph.microsoft.com/v1.0/next' } };
  } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.create_draft']; const request = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_collection_guard'); await tool.preview(request);
  const noAck = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(noAck.reconciliation, 'dispatch_unconfirmed'); assert.equal(posts, 1); assert.equal(reads, 0);
  // A separately acknowledged late response still cannot be verified from a
  // malformed or paginated collection, because uniqueness is unproven.
  const lateTransport = { request: async req => { if (req.method === 'POST') { req.onDispatch?.(); throw Object.assign(new Error('late'), { code: 'provider_timeout' }); } return mode === 'malformed' ? { status: 200, body: {} } : { status: 200, body: { value: [], '@odata.nextLink': 'next' } }; } };
  const lateTool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: lateTransport })['mail.create_draft']; const lateCall = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_collection_late'); await lateTool.preview(lateCall); const late = value(await lateTool.execute({ ...lateCall, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(late.reconciliation, 'draft_collection_unavailable'); mode = 'next'; const paged = value(await lateTool.execute({ ...lateCall, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(paged.code, 'provider_write_already_attempted');
});

test('Graph proof collections reject malformed or duplicate entries instead of filtering them', async () => {
  const binding = { operation_id: `act_${'8'.repeat(32)}`, operation_digest: 'a'.repeat(64), arguments_digest: 'b'.repeat(64), preview_digest: 'c'.repeat(64) }; const marker = `${binding.operation_id}:${binding.operation_digest}`;
  let posts = 0; const draftTransport = { request: async request => { if (request.method === 'POST') { request.onDispatch?.(); throw Object.assign(new Error('late'), { code: 'provider_timeout' }); } return { status: 200, body: { value: [{ id: 'missing-id', subject: 'bad' }, { id: 'draft-good', internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], subject: 'x', body: { content: 'x', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }] }] } }; } };
  const draftTool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: draftTransport })['mail.create_draft']; const draftCall = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_bad_draft_collection'); await draftTool.preview(draftCall); const draftResult = value(await draftTool.execute({ ...draftCall, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(draftResult.reconciliation, 'draft_collection_unavailable');
  const teamsTransport = { request: async request => { if (request.method === 'POST') { posts += 1; request.onDispatch?.(); return { status: 201, body: { id: 'team-should-not-send', chatId: 'chat-bad', body: { contentType: 'text', content: 'x' } } }; } return { status: 200, body: { value: [{ id: 'team-incomplete', body: { contentType: 'text', content: 'old' } }] } }; } };
  const teamsTool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: teamsTransport })['teams.send_message']; const teamsCall = call('teams.send_message', { chat_id: 'chat-bad', body: 'x' }, 'call_bad_team_collection'); await teamsTool.preview(teamsCall); const teamsResult = value(await teamsTool.execute({ ...teamsCall, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(teamsResult.code, 'provider_invalid_response'); assert.equal(posts, 0);
});

test('HTTPS dispatch acknowledgement occurs only after fetch handoff', async () => {
  const events = []; const transport = new MicrosoftGraphHttpsTransport({ requestTimeoutMs: 1000, fetchImpl: async () => { events.push('fetch'); return new Response(null, { status: 202 }); } });
  await transport.request({ origin: 'https://graph.microsoft.com', method: 'POST', path: '/v1.0/me/messages', onDispatch: () => events.push('dispatch') }); assert.deepEqual(events, ['fetch', 'dispatch']);
  const failed = new MicrosoftGraphHttpsTransport({ requestTimeoutMs: 1000, fetchImpl: () => { events.push('throw-fetch'); throw new Error('before handoff'); } }); await assert.rejects(() => failed.request({ origin: 'https://graph.microsoft.com', method: 'POST', path: '/v1.0/me/messages', onDispatch: () => events.push('bad-dispatch') }), error => error.code === 'provider_offline'); assert.deepEqual(events, ['fetch', 'dispatch', 'throw-fetch']);
});

test('Graph mark-read is repeatable only through desired-state verification', async () => {
  const binding = { operation_id: `act_${'2'.repeat(32)}`, operation_digest: 'a'.repeat(64), arguments_digest: 'b'.repeat(64), preview_digest: 'c'.repeat(64) }; let current = false; let patches = 0;
  const transport = { request: async request => { if (request.method === 'PATCH') { patches += 1; request.onDispatch?.(); current = true; return { status: 204, body: {} }; } return { status: 200, body: { id: 'm-read', isRead: current } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.mark_read']; const request = call('mail.mark_read', { message_id: 'm-read', is_read: true }, 'call_mark_repeat'); await tool.preview(request);
  const first = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(first.provider_completion, 'verified'); assert.equal(first.reconciliation, 'post_write_get_verified');
  const second = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' }, internal: { journal_binding: binding } })); assert.equal(second.provider_completion, 'verified'); assert.equal(second.reconciliation, 'pre_read_already_desired'); assert.equal(patches, 1);
});

test('Graph mark-read token failure cannot verify a concurrent desired state before PATCH dispatch', async () => {
  let patches = 0; const transport = { request: async request => { if (request.method === 'PATCH') patches += 1; return { status: 200, body: { id: 'm-concurrent', isRead: true } }; } }; const credentialSource = { getAccessToken: async () => { throw Object.assign(new Error('token timeout'), { code: 'provider_timeout' }); } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource, transport })['mail.mark_read']; const request = call('mail.mark_read', { message_id: 'm-concurrent', is_read: true }, 'call_mark_no_dispatch'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.code, 'provider_timeout'); assert.equal(output.provider_completion, undefined); assert.equal(patches, 0);
});

test('Graph mark-read timeout after PATCH dispatch may verify only a subsequent desired-state GET', async () => {
  let reads = 0; const transport = { request: async request => { if (request.method === 'PATCH') { request.onDispatch?.(); throw Object.assign(new Error('late patch'), { code: 'provider_timeout' }); } reads += 1; return { status: 200, body: { id: 'm-timeout-proof', isRead: reads > 1 } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.mark_read']; const request = call('mail.mark_read', { message_id: 'm-timeout-proof', is_read: true }, 'call_mark_timeout_proof'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.provider_completion, 'verified'); assert.equal(output.reconciliation, 'timeout_get_verified'); assert.equal(reads, 2);
});

test('Graph send-draft 202 is verified only by draft disappearance and one new Sent Item', async () => {
  let sentDispatched = false; const marker = `act_${'a'.repeat(32)}:${'b'.repeat(64)}`; const requests = []; const draft = { id: 'draft-proof', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], '@odata.etag': 'etag-proof', changeKey: 'change-proof' }; const sent = { id: 'sent-proof', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], sentDateTime: new Date().toISOString().replace(/\.\d{3}Z$/u, '.1234567Z') };
  const transport = { request: async request => { requests.push(request); if (request.method === 'POST') { request.onDispatch?.(); sentDispatched = true; return { status: 202, body: {} }; } if (request.path.includes('/me/messages/draft-proof')) return sentDispatched ? { status: 404, body: {} } : { status: 200, body: draft }; if (request.path.includes('/sentitems')) { if (request.query.$select === 'id,sentDateTime') return { status: 200, body: { value: [] } }; return { status: 200, body: { value: [sent] } }; } return { status: 200, body: draft }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-proof' }, 'call_send_proof'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.provider_completion, 'verified'); assert.equal(output.reconciliation, 'unique_sent_item'); assert.equal(requests.find(item => item.path.endsWith('/send')).headers['Idempotency-Key'], undefined); const sentReads = requests.filter(item => item.path.includes('/sentitems')); assert.equal(sentReads.length, 2); for (const sentRead of sentReads) { assert.equal(sentRead.query.$top, 50); assert.equal(sentRead.query.$orderby, 'sentDateTime desc'); }
});

test('Graph draft/Sent proof accepts canonical UTC dates and rejects non-UTC offsets', async () => {
  const marker = `act_${'e'.repeat(32)}:${'f'.repeat(64)}`; const draft = { id: 'draft-date-proof', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], '@odata.etag': 'etag-date', changeKey: 'change-date' }; let sentDispatch = false;
  const sentWithValidUtc = { id: 'sent-date-proof', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], sentDateTime: new Date().toISOString().replace(/\.\d{3}Z$/u, '.999Z') };
  const transport = { request: async request => { if (request.method === 'POST') { request.onDispatch?.(); sentDispatch = true; return { status: 202, body: {} }; } if (request.path.includes('/me/messages/draft-date-proof')) return sentDispatch ? { status: 404, body: {} } : { status: 200, body: draft }; if (request.path.includes('/sentitems')) { if (request.query.$select === 'id,sentDateTime') return { status: 200, body: { value: [] } }; return { status: 200, body: { value: [{ ...sentWithValidUtc, sentDateTime: '2026-02-30T00:00:00.000Z' }] } }; } return { status: 200, body: draft }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-date-proof' }, 'call_date_proof'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const invalid = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(invalid.reconciliation, 'sent_collection_unavailable');
  let posts = 0; const baselineTransport = { request: async request => { if (request.method === 'POST') { posts += 1; request.onDispatch?.(); return { status: 202, body: {} }; } if (request.path.includes('/sentitems')) return { status: 200, body: { value: [{ id: 'bad-baseline', sentDateTime: '2026-02-30T00:00:00.000Z' }] } }; return { status: 200, body: draft }; } };
  const baselineTool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: baselineTransport })['mail.send_draft']; const baseline = call('mail.send_draft', { draft_id: 'draft-date-proof' }, 'call_date_baseline'); await baselineTool.preview(baseline); await baselineTool.preview({ ...baseline, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const rejected = value(await baselineTool.execute({ ...baseline, authorization: { kind: 'user_confirmation' } })); assert.equal(rejected.code, 'provider_invalid_response'); assert.equal(posts, 0);
  const rejectBaseline = async (date, id) => { let attempted = 0; const transport = { request: async request => { if (request.method === 'POST') { attempted += 1; request.onDispatch?.(); return { status: 202, body: {} }; } if (request.path.includes('/sentitems')) return { status: 200, body: { value: [{ id: 'bad-date', sentDateTime: date }] } }; return { status: 200, body: draft }; } }; const candidate = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const candidateCall = call('mail.send_draft', { draft_id: 'draft-date-proof' }, id); await candidate.preview(candidateCall); await candidate.preview({ ...candidateCall, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const result = value(await candidate.execute({ ...candidateCall, authorization: { kind: 'user_confirmation' } })); assert.equal(result.code, 'provider_invalid_response'); assert.equal(attempted, 0); };
  await rejectBaseline('2026-01-01T00:00:00.1234567890123Z', 'call_date_precision'); await rejectBaseline('2026-01-01T00:00:00.000+01:00', 'call_date_non_utc'); await rejectBaseline('2026-01-01T00:00:00.000+15:00', 'call_date_offset');
});

test('Graph Sent baseline accepts a strict 12-digit fraction before dispatch', async () => {
  let posts = 0; const marker = `act_${'7'.repeat(32)}:${'8'.repeat(64)}`; const draft = { id: 'draft-fraction12', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], '@odata.etag': 'etag-fraction12', changeKey: 'change-fraction12' }; const timestamp = new Date().toISOString().replace(/\.\d{3}Z$/u, '.123456789012Z'); const transport = { request: async request => { if (request.method === 'POST') { posts += 1; request.onDispatch?.(); throw Object.assign(new Error('late'), { code: 'provider_timeout' }); } if (request.path.includes('/sentitems')) return { status: 200, body: { value: [{ id: 'baseline-fraction12', sentDateTime: timestamp }] } }; return { status: 200, body: draft }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-fraction12' }, 'call_fraction12'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(posts, 1); assert.equal(output.code, 'provider_action_reconciling');
});

test('Graph Sent baseline accepts canonical UTC with no fractional digits', async () => {
  let posts = 0; const marker = `act_${'9'.repeat(32)}:${'a'.repeat(64)}`; const draft = { id: 'draft-fraction0', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], '@odata.etag': 'etag-fraction0', changeKey: 'change-fraction0' }; const transport = { request: async request => { if (request.method === 'POST') { posts += 1; request.onDispatch?.(); throw Object.assign(new Error('late'), { code: 'provider_timeout' }); } if (request.path.includes('/sentitems')) return { status: 200, body: { value: [{ id: 'baseline-fraction0', sentDateTime: '2026-01-01T00:00:00Z' }] } }; return { status: 200, body: draft }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-fraction0' }, 'call_fraction0'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(posts, 1); assert.equal(output.code, 'provider_action_reconciling');
});

test('Graph send-draft never verifies an old or concurrently matching Sent Item', async () => {
  let sentDispatched = false; const marker = `act_${'c'.repeat(32)}:${'d'.repeat(64)}`; let posts = 0; const draft = { id: 'draft-old-proof', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], '@odata.etag': 'etag-old-proof', changeKey: 'change-old-proof' }; const oldSent = { id: 'sent-old-proof', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], internetMessageHeaders: [{ name: 'x-lae-operation', value: marker }], sentDateTime: '2020-01-01T00:00:00.000Z' };
  const transport = { request: async request => { if (request.method === 'POST') { request.onDispatch?.(); posts += 1; sentDispatched = true; return { status: 202, body: {} }; } if (request.path.includes('/me/messages/draft-old-proof')) return sentDispatched ? { status: 404, body: {} } : { status: 200, body: draft }; if (request.path.includes('/sentitems')) return request.query.$select === 'id,sentDateTime' ? { status: 200, body: { value: [] } } : { status: 200, body: { value: [oldSent] } }; return { status: 200, body: draft }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-old-proof' }, 'call_send_old_proof'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.code, 'provider_action_reconciling'); assert.equal(output.reconciliation, 'sent_item_not_found'); assert.equal(posts, 1);
});

test('Graph send-draft token failure before transport never authorizes reconciliation', async () => {
  let tokenCalls = 0; let transportCalls = 0; let posts = 0; const draft = { id: 'draft-token-timeout', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], '@odata.etag': 'etag-token', changeKey: 'change-token' };
  const transport = { request: async request => { transportCalls += 1; if (request.method === 'POST') posts += 1; return { status: 200, body: draft }; } }; const credentialSource = { getAccessToken: async () => { tokenCalls += 1; if (tokenCalls > 1) throw Object.assign(new Error('token timeout'), { code: 'provider_timeout' }); return 'synthetic-token'; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-token-timeout' }, 'call_token_timeout'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.code, 'provider_timeout'); assert.equal(posts, 0); assert.equal(transportCalls, 1);
});

test('Graph send-draft requires both preview-bound version fields before any send', async () => {
  let posts = 0;
  const transport = { request: async request => {
    if (request.method === 'POST') { posts += 1; return { status: 202, body: {} }; }
    return { status: 200, body: { id: 'draft-no-version', subject: 'Subject', body: { content: 'Body', contentType: 'Text' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], changeKey: 'present-but-no-etag' } };
  } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft'];
  const request = call('mail.send_draft', { draft_id: 'draft-no-version' }, 'call_no_version');
  await tool.preview(request);
  await assert.rejects(() => tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }), error => error.code === 'provider_invalid_response');
  assert.equal(posts, 0);
});

test('Teams send accepts a validated 201 resource and never sends a generic idempotency header', async () => {
  const requests = []; const transport = { request: async request => { requests.push(request); if (request.method === 'POST') { request.onDispatch?.(); return { status: 201, body: { id: 'team-message-1', chatId: 'chat-proof', body: { contentType: 'text', content: 'hello' } } }; } return { status: 200, body: { value: [] } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['teams.send_message']; const request = call('teams.send_message', { chat_id: 'chat-proof', body: 'hello' }, 'call_team_proof'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.provider_completion, 'verified'); const post = requests.find(item => item.method === 'POST'); assert.equal(post.headers['Idempotency-Key'], undefined); assert.equal(post.body.body.contentType, 'text');
});

test('Teams send timeout remains manual and is never retried from memory', async () => {
  let posts = 0; const transport = { request: async request => { if (request.method === 'POST') { request.onDispatch?.(); posts += 1; throw Object.assign(new Error('ambiguous'), { code: 'provider_timeout' }); } return { status: 200, body: { value: [] } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['teams.send_message']; const request = call('teams.send_message', { chat_id: 'chat-timeout', body: 'hello' }, 'call_team_timeout'); await tool.preview(request); const first = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(first.code, 'provider_action_reconciling'); const retry = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(retry.code, 'provider_write_already_attempted'); assert.equal(posts, 1);
});

test('Teams token failure before POST does not mark dispatch or authorize reconciliation', async () => {
  let tokenCalls = 0; let posts = 0; let requests = 0;
  const credentialSource = { getAccessToken: async () => { tokenCalls += 1; if (tokenCalls > 1) throw Object.assign(new Error('token timeout'), { code: 'provider_timeout' }); return 'synthetic-token'; } };
  const transport = { request: async request => { requests += 1; if (request.method === 'POST') posts += 1; return { status: 200, body: { value: [] } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource, transport })['teams.send_message'];
  const request = call('teams.send_message', { chat_id: 'chat-token-timeout', body: 'hello' }, 'call_team_token_timeout');
  await tool.preview(request);
  const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } }));
  assert.equal(output.code, 'provider_timeout'); assert.equal(posts, 0); assert.equal(requests, 1);
});

test('Teams timeout does not verify an old or concurrent identical message without trusted sender/time proof', async () => {
  let listCount = 0; let posts = 0; const old = { id: 'team-old', createdDateTime: '2020-01-01T00:00:00.000Z', from: { user: { id: 'signed-in-user' } }, body: { contentType: 'text', content: 'same body' } }; const concurrent = { ...old, id: 'team-concurrent' };
  const transport = { request: async request => { if (request.method === 'POST') { request.onDispatch?.(); posts += 1; throw Object.assign(new Error('ambiguous'), { code: 'provider_timeout' }); } if (request.path.includes('/messages')) { listCount += 1; return { status: 200, body: { value: [listCount === 1 ? old : concurrent] } }; } return { status: 200, body: {} }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['teams.send_message']; const request = call('teams.send_message', { chat_id: 'chat-old-proof', body: 'same body' }, 'call_team_old_proof'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.code, 'provider_action_reconciling'); assert.equal(output.reconciliation, 'timeout_chat_message_not_found'); assert.equal(posts, 1);
});

test('Teams timeout proof rejects an impossible creation timestamp', async () => {
  let lists = 0; const validTime = new Date().toISOString().replace(/\.\d{3}Z$/u, '.001Z'); const transport = { request: async request => { if (request.method === 'POST') { request.onDispatch?.(); throw Object.assign(new Error('ambiguous'), { code: 'provider_timeout' }); } lists += 1; return { status: 200, body: { value: [{ id: `team-date-${lists}`, createdDateTime: lists === 1 ? validTime : '2026-02-30T00:00:00.000Z', from: { user: { id: 'signed-in-user' } }, body: { contentType: 'text', content: 'same body' } }] } }; } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['teams.send_message']; const request = call('teams.send_message', { chat_id: 'chat-date', body: 'same body' }, 'call_team_date'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.reconciliation, 'timeout_chat_collection_unavailable'); assert.equal(lists, 2);
});

test('Graph writes require preview, bind proposals, retain high-impact confirmation, and deduplicate retries', async () => {
  const requests = []; const transport = { request: async request => { requests.push(request); if (request.method === 'GET' && request.path.includes('/me/messages/')) return { status: 200, body: { id: request.path.endsWith('/m-1') ? 'm-1' : 'draft-1', subject: 'Existing', body: { content: 'Existing draft' }, isRead: false, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], '@odata.etag': 'etag-1', changeKey: 'change-1' } }; if (request.method === 'GET' && request.path.includes('/sentitems')) return { status: 200, body: { value: [] } }; if (request.method === 'POST' && request.path.endsWith('/messages')) { request.onDispatch?.(); return { status: 201, body: { id: 'draft-1' } }; } if (request.method === 'POST' && request.path.endsWith('/send')) { request.onDispatch?.(); return { status: 202, body: {} }; } return { status: 204, body: {} }; } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport });
  const createCall = call('mail.create_draft', { to: ['alice@example.com'], cc: ['bob@example.com'], subject: 'Hello', body: 'Synthetic body' }, 'call_create_draft');
  const unproposed = value(await tools['mail.create_draft'].execute(createCall)); assert.equal(unproposed.code, 'provider_permission_insufficient');
  const preview = await tools['mail.create_draft'].preview(createCall); assert.deepEqual(preview.recipients, ['alice@example.com', 'bob@example.com']); const created = value(await tools['mail.create_draft'].execute({ ...createCall, authorization: { kind: 'user_confirmation' } })); assert.equal(created.accepted, true); assert.equal(created.provider_completion, 'verified');
  const retried = value(await tools['mail.create_draft'].execute({ ...createCall, authorization: { kind: 'user_confirmation' } })); assert.equal(retried.code, 'provider_write_already_attempted'); assert.equal(requests.filter(request => request.path === '/v1.0/me/messages').length, 1); assert.deepEqual(requests[0].body.toRecipients, [{ emailAddress: { address: 'alice@example.com' } }]);
  const sendCall = call('mail.send_draft', { draft_id: 'draft-1' }, 'call_send_draft'); const genericSendPreview = await tools['mail.send_draft'].preview(sendCall); assert.equal(genericSendPreview.preview_authorization_required, true); const sendPreview = await tools['mail.send_draft'].preview({ ...sendCall, authorization: { kind: 'user_confirmation' }, preview_authorized: true }); assert.match(sendPreview.proposal_revision, /^[a-f0-9]{64}$/); assert.equal(tools['mail.send_draft'].confirmationRequired(sendCall), true); const sent = value(await tools['mail.send_draft'].execute({ ...sendCall, authorization: { kind: 'user_confirmation' } })); assert.equal(sent.code, 'provider_action_reconciling'); const sendRequest = requests.find(request => request.path.endsWith('/send')); assert.equal(sendRequest.headers['Idempotency-Key'], undefined); assert.equal(sendRequest.headers['If-Match'], 'etag-1');
  const stale = call('mail.mark_read', { message_id: 'm-1', is_read: true }, 'call_mark'); await tools['mail.mark_read'].preview(stale); const changed = value(await tools['mail.mark_read'].execute(call('mail.mark_read', { message_id: 'm-2', is_read: true }, 'call_mark'))); assert.equal(changed.code, 'provider_permission_insufficient');
});

test('full_access is operator-scoped, revocable, and never model-selected', async () => {
  let now = 1000; const grants = new OperatorGrantStore({ now: () => now }); const grant = grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'account', profile: 'full_access', expiresAt: 5000 });
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-2' } }) }, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-1', testOnly: true }); const tools = createMicrosoftGraphTools(provider); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }); await tools['mail.create_draft'].preview(draft); assert.equal(tools['mail.create_draft'].confirmationRequired(draft), false); const authorization = await tools['mail.create_draft'].authorize(draft); assert.equal(authorization.kind, 'operator_grant'); assert.equal(value(await tools['mail.create_draft'].execute({ ...draft, authorization })).accepted, true); grants.revoke('microsoft.graph.mail'); assert.equal(tools['mail.create_draft'].confirmationRequired(draft), true); const revoked = value(await tools['mail.create_draft'].execute({ ...draft, authorization })); assert.equal(revoked.code, 'provider_permission_revoked'); assert.equal(grant.profile, 'full_access'); now = 6000; assert.equal(grants.matches('microsoft.graph.mail', { provider: 'microsoft_graph', accountFingerprint: 'acct-1', scope: 'account' }), false);
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
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-2', testOnly: true }); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_revoke'); await tools['mail.create_draft'].preview(draft); const authorization = await tools['mail.create_draft'].authorize(draft); const pending = tools['mail.create_draft'].execute({ ...draft, authorization }); await new Promise(resolve => setImmediate(resolve)); grants.revoke('microsoft.graph.mail'); const revoked = value(await pending); assert.equal(revoked.code, 'provider_cancelled');
});

test('Graph transport timeout is bounded and typed', async () => {
  const tools = createMicrosoftGraphTools({ enabled: true, requestTimeoutMs: 100, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => new Promise(() => {}) } });
  const started = Date.now(); const output = value(await tools['mail.list_messages'].execute(call('mail.list_messages', {}))); assert.equal(output.code, 'provider_timeout'); assert.ok(Date.now() - started < 1000);
});

test('Microsoft device-code credential is explicit, bounded, and memory-only', async () => {
  let now = 0; let polls = 0; const requests = []; const transport = { request: async request => { requests.push(request); if (request.path.endsWith('/devicecode')) return { status: 200, body: { device_code: 'device', user_code: 'CODE', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } }; polls += 1; return polls === 1 ? { status: 400, body: { error: 'authorization_pending' } } : { status: 200, body: { access_token: 'access-token-memory-only', expires_in: 3600, scope: 'User.Read Mail.Read' } }; } }; const credential = new MicrosoftDeviceCodeCredential({ tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read', 'Mail.Read'], transport, now: () => now, sleep: async milliseconds => { now += milliseconds; } });
  await assert.rejects(() => credential.getAccessToken(), error => error.code === 'provider_unauthorized'); await credential.start(); assert.equal(await credential.getAccessToken(), 'access-token-memory-only'); assert.equal(requests.length, 3); assert.deepEqual(credential.authStatus(), { state: 'authenticated', prompt: null }); assert.throws(() => new MicrosoftDeviceCodeCredential({ tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['Mail.Read'], transport }), /explicit/); assert.throws(() => new MicrosoftDeviceCodeCredential({ tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['https://graph.microsoft.com/.default'], transport }), /explicit/);
});

test('Microsoft Graph device auth verifies an opaque account and gates delegated operations', async () => {
  let now = 0; const transport = { request: async request => request.path.endsWith('/devicecode') ? { status: 200, body: { device_code: 'device', user_code: 'CODE', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } } : request.path.endsWith('/token') ? { status: 200, body: { access_token: 'access-token-memory-only', expires_in: 3600 } } : { status: 200, body: { id: 'user-opaque-1', userPrincipalName: 'user@example.com' } } }; const provider = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read', 'Chat.Read'], transport, now: () => now, sleep: async milliseconds => { now += milliseconds; } }); await provider.startAuth(); assert.match(provider.getAccountFingerprint(), /^[a-f0-9]{64}$/u); assert.equal(provider.authStatus().accountFingerprint, provider.getAccountFingerprint()); await assert.rejects(() => provider.request({ method: 'POST', path: '/v1.0/chats/chat-1/messages', body: { body: { content: 'x' } } }), error => error.code === 'provider_unauthorized');
});

test('Graph auth expiry and cancellation clear cached access and require explicit restart', async () => {
  let now = 0; let tokenRequests = 0;
  const transport = { request: async request => {
    if (request.path.endsWith('/devicecode')) return { status: 200, body: { device_code: 'device', user_code: 'CODE', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } };
    tokenRequests += 1; return { status: 200, body: { access_token: `access-${tokenRequests}`, expires_in: 120 } };
  } };
  const credential = new MicrosoftDeviceCodeCredential({ tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read'], transport, now: () => now, sleep: async milliseconds => { now += milliseconds; } });
  await credential.start(); assert.equal(credential.authStatus().state, 'authenticated');
  now = 70000; assert.equal(credential.authStatus().state, 'expired'); await assert.rejects(() => credential.getAccessToken(), error => error.code === 'provider_unauthorized');
  await credential.start(); assert.equal(tokenRequests, 2); credential.cancel(); assert.equal(credential.authStatus().state, 'idle'); await assert.rejects(() => credential.getAccessToken(), error => error.code === 'provider_unauthorized');
});

test('Graph rejects Mail.Read-only PATCH and validates authorization proofs exactly', async () => {
  let calls = 0;
  const transport = { request: async request => {
    calls += 1;
    if (request.path.endsWith('/devicecode')) return { status: 200, body: { device_code: 'device', user_code: 'CODE', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } };
    if (request.path.endsWith('/token')) return { status: 200, body: { access_token: 'access', expires_in: 3600 } };
    return { status: 200, body: {} };
  } };
  const provider = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read', 'Mail.Read'], transport, sleep: async () => {} });
  await provider.credentialSource.start(); await assert.rejects(() => provider.request({ method: 'PATCH', path: '/v1.0/me/messages/m-1', body: { isRead: true } }), error => error.code === 'provider_unauthorized'); assert.equal(calls, 2);
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-auth' } }) } }); const request = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_auth_shape'); await tools['mail.create_draft'].preview(request); const bad = value(await tools['mail.create_draft'].execute({ ...request, authorization: { kind: 'user_confirmation', generation: 'a'.repeat(32) } })); assert.equal(bad.code, 'provider_permission_insufficient');
});

test('Graph detects changed draft tails, preserves 202 status, and uses text Teams bodies', async () => {
  let draftReads = 0; let sent = 0; let teamRequest; let teamPost;
  const transport = { request: async request => {
    if (request.path.includes('/me/messages/draft-tail')) { draftReads += 1; return { status: 200, body: { id: 'draft-tail', subject: 'Subject', body: { content: draftReads === 1 ? 'A'.repeat(700) : `${'A'.repeat(699)}B` }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], '@odata.etag': 'etag-tail', changeKey: 'key-1' } }; }
    if (request.path.includes('/send')) { request.onDispatch?.(); sent += 1; return { status: 202, body: {} }; }
    teamRequest = request; if (request.method === 'POST') { request.onDispatch?.(); teamPost = request; return { status: 202, body: {} }; } return { status: 200, body: { value: [] } };
  } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport }); const send = call('mail.send_draft', { draft_id: 'draft-tail' }, 'call_tail'); assert.equal((await tools['mail.send_draft'].preview(send)).preview_authorization_required, true);
  // The first preview above is intentionally generic for always_ask; authorize
  // the private read and then execute against a changed tail.
  await tools['mail.send_draft'].preview({ ...send, preview_authorized: true, authorization: { kind: 'user_confirmation' } });
  const result = value(await tools['mail.send_draft'].execute({ ...send, authorization: { kind: 'user_confirmation' } })); assert.equal(result.code, 'provider_permission_insufficient'); assert.equal(sent, 0);
  const team = call('teams.send_message', { chat_id: 'chat-1', body: 'hello' }, 'call_team_status'); await tools['teams.send_message'].preview(team); const teamResult = value(await tools['teams.send_message'].execute({ ...team, authorization: { kind: 'user_confirmation' } })); assert.equal(teamResult.code, 'provider_action_reconciling'); assert.equal(teamPost.body.body.contentType, 'text'); assert.equal(teamPost.headers['Idempotency-Key'], undefined);
});

test('Graph refuses draft recipient changes and discloses create-body truncation', async () => {
  let reads = 0; let sends = 0;
  const transport = { request: async request => {
    if (request.method === 'GET') { reads += 1; return { status: 200, body: { id: 'draft-recipient', subject: 'Subject', body: { content: 'Stable body' }, toRecipients: [{ emailAddress: { address: reads === 1 ? 'alice@example.com' : 'bob@example.com', name: 'Recipient' } }], '@odata.etag': 'etag-recipient', changeKey: 'change-recipient' } }; }
    sends += 1; return { status: 201, body: { id: 'draft-recipient' } };
  } };
  const tools = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport }); const send = call('mail.send_draft', { draft_id: 'draft-recipient' }, 'call_recipient_change'); await tools['mail.send_draft'].preview(send); await tools['mail.send_draft'].preview({ ...send, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const refused = value(await tools['mail.send_draft'].execute({ ...send, authorization: { kind: 'user_confirmation' } })); assert.equal(refused.code, 'provider_permission_insufficient'); assert.equal(sends, 0);
  const create = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: '😀'.repeat(300) }, 'call_create_truncation'); const createPreview = await tools['mail.create_draft'].preview(create); assert.equal(createPreview.body_truncated, true); assert.ok(Buffer.byteLength(createPreview.body_preview, 'utf8') <= 512);
  const multiline = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'line one\n\tline two\r\n' }, 'call_create_multiline'); assert.equal((await tools['mail.create_draft'].preview(multiline)).body_preview, 'line one\n\tline two\r\n');
  const empty = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: '' }, 'call_create_empty'); assert.equal((await tools['mail.create_draft'].preview(empty)).body_preview, '');
});

test('Graph send-draft refuses an unchanged body when the preview-bound ETag changes', async () => {
  let reads = 0; let sends = 0; const transport = { request: async request => {
    if (request.method === 'GET') { reads += 1; return { status: 200, body: { id: 'draft-etag', subject: 'Stable', body: { content: 'Same body' }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], '@odata.etag': reads === 1 ? 'etag-old' : 'etag-new', changeKey: 'change-key' } }; }
    sends += 1; return { status: 202, body: {} };
  } };
  const tool = createMicrosoftGraphTools({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport })['mail.send_draft']; const request = call('mail.send_draft', { draft_id: 'draft-etag' }, 'call_etag_change'); await tool.preview(request); await tool.preview({ ...request, preview_authorized: true, authorization: { kind: 'user_confirmation' } }); const refused = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(refused.code, 'provider_permission_insufficient'); assert.equal(sends, 0); assert.equal(reads, 2);
});

test('Microsoft HTTPS transport uses redirect errors, JSON framing, and safe retry policy', async () => {
  let calls = 0; const transport = new MicrosoftGraphHttpsTransport({ requestTimeoutMs: 1000, sleep: async () => {}, fetchImpl: async (_url, options) => { calls += 1; assert.equal(options.redirect, 'error'); return new Response('{"error":"busy"}', { status: 503, headers: { 'content-type': 'application/json' } }); } }); const response = await transport.request({ origin: 'https://graph.microsoft.com', method: 'POST', path: '/v1.0/me/messages', body: { subject: 'x' } }); assert.equal(response.status, 503); assert.equal(calls, 1); await assert.rejects(() => transport.request({ origin: 'https://graph.microsoft.com', method: 'GET', path: '/v1.0/me', headers: { 'x-forwarded-for': 'evil' } }), error => error.code === 'provider_destination_rejected'); await assert.rejects(() => transport.request({ origin: 'https://graph.microsoft.com', method: 'POST', path: '/v1.0/me/messages', headers: { 'Idempotency-Key': 'false-contract' } }), error => error.code === 'provider_destination_rejected');
});

test('Graph send accepts an empty 202 response exactly once', async () => {
  let calls = 0;
  const transport = new MicrosoftGraphHttpsTransport({
    requestTimeoutMs: 1000,
    sleep: async () => {},
    fetchImpl: async (url, options) => {
      calls += 1;
      assert.equal(new URL(url).pathname, '/v1.0/me/messages/draft-202/send');
      assert.equal(options.method, 'POST');
      return new Response(null, { status: 202 });
    },
  });
  const response = await transport.request({ origin: 'https://graph.microsoft.com', method: 'POST', path: '/v1.0/me/messages/draft-202/send' });
  assert.equal(response.status, 202);
  assert.deepEqual(response.body, {});
  assert.equal(calls, 1);
});

test('Graph writes are at-most-once across concurrent and timeout retries', async () => {
  let dispatches = 0; const transport = { request: async request => { request.onDispatch?.(); dispatches += 1; await new Promise((resolve, reject) => request.signal.addEventListener('abort', () => reject(Object.assign(new Error('timed out'), { code: 'ETIMEDOUT' })))); } };
  const tools = createMicrosoftGraphTools({ enabled: true, requestTimeoutMs: 100, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport }); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_once'); await tools['mail.create_draft'].preview(draft); const authorization = { kind: 'user_confirmation' }; const first = tools['mail.create_draft'].execute({ ...draft, authorization }); await new Promise(resolve => setImmediate(resolve)); const concurrent = value(await tools['mail.create_draft'].execute({ ...draft, authorization })); assert.equal(concurrent.code, 'provider_write_already_attempted'); const timedOut = value(await first); assert.equal(timedOut.code, 'provider_action_reconciling'); const retry = value(await tools['mail.create_draft'].execute({ ...draft, authorization })); assert.equal(retry.code, 'provider_write_already_attempted'); assert.equal(dispatches, 1);
});

test('controller advertises external parameters and distinguishes operator authorization from user confirmation', async t => {
  const grants = new OperatorGrantStore(); grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-controller', profile: 'full_access' }); let requests = 0; const provider = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-controller', credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async request => { requests += 1; request.onDispatch?.(); return { status: 201, body: { id: 'draft-controller' } }; } }, testOnly: true }); const registry = createMicrosoftGraphTools(provider); let advertised;
  const engine = { async *generate({ tools, messages }) { advertised = tools.find(item => item.function.name === 'mail.create_draft'); if (!messages.some(item => item.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_controller', name: 'mail.create_draft', arguments: { to: ['alice@example.com'], subject: 'x', body: 'x' } }) }; return; } yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const events = []; const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: registry }); const output = await controller.runTurn({ sessionId: 'ses_controller', requestId: 'req_controller', message: 'draft a message', onEvent: event => events.push(event) }); assert.equal(output.state, 'COMPLETED'); assert.deepEqual(advertised.function.parameters, registry['mail.create_draft'].parameters); assert.equal(events.some(event => event.event === 'tool.confirmation_required'), false); assert.equal(events.find(event => event.event === 'tool.started').data.authorization, 'operator_grant'); const completed = JSON.parse(events.find(event => event.event === 'tool.completed').data.result.content[0].text); assert.equal(completed.provider_completion, 'verified'); assert.equal(completed.reconciliation, 'created_resource'); assert.equal(JSON.stringify(completed).includes('operation_digest'), false); assert.equal(requests, 1);
});

test('controller rejects a genuine Graph attestation when its serialized payload is mutated', async t => {
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async request => { request.onDispatch?.(); return { status: 201, body: { id: 'draft-tampered' } }; } }, testOnly: true });
  const registry = createMicrosoftGraphTools(provider);
  const originalExecute = registry['mail.create_draft'].execute;
  registry['mail.create_draft'].execute = async callValue => {
    const output = await originalExecute(callValue);
    const payload = value(output); payload.state = 'tampered'; output.content[0].text = JSON.stringify(payload); return output;
  };
  const engine = { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) yield { kind: 'tool_call_chunk', text: JSON.stringify(call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_graph_tampered')) }; else { yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; } } };
  const events = []; const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: registry }); const pending = controller.runTurn({ sessionId: 'ses_graph_tampered', requestId: 'req_graph_tampered', message: 'draft it', onEvent: event => events.push(event) });
  while (!events.some(event => event.event === 'tool.confirmation_required')) await new Promise(resolve => setImmediate(resolve));
  const confirmation = events.find(event => event.event === 'tool.confirmation_required'); assert.equal(controller.confirm(confirmation.data.confirmation_id, true, { requestId: 'req_graph_tampered', callId: 'call_graph_tampered' }), true);
  const output = await pending; assert.equal(output.state, 'COMPLETED'); const completed = events.find(event => event.event === 'tool.completed').data.result; assert.match(completed.content[0].text, /action_completion_unverified/); assert.equal((await controller.actionJournal.summary()).records[0].state, 'reconciling');
});

test('controller carries journal binding only across the private provider call boundary', async t => {
  const grants = new OperatorGrantStore(); grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-binding', profile: 'full_access' }); let observed;
  const provider = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', grantStore: grants, accountFingerprint: 'acct-binding', credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async () => ({ status: 201, body: { id: 'draft-binding' } }) }, testOnly: true }); const registry = createMicrosoftGraphTools(provider); const execute = registry['mail.create_draft'].execute; registry['mail.create_draft'].execute = callValue => { observed = callValue.internal; return execute(callValue); };
  let advertised; const engine = { async *generate({ tools, messages }) { advertised = tools.find(item => item.function.name === 'mail.create_draft'); if (!messages.some(item => item.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_binding', name: 'mail.create_draft', arguments: { to: ['alice@example.com'], subject: 'x', body: 'x' } }) }; return; } yield { kind: 'text_delta', text: 'done' }; } };
  const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: registry }); const output = await controller.runTurn({ sessionId: 'ses_binding', requestId: 'req_binding', message: 'draft it' }); assert.equal(output.state, 'COMPLETED'); assert.match(observed.journal_binding.operation_id, /^act_[a-f0-9]{32}$/u); for (const key of ['operation_digest', 'arguments_digest', 'preview_digest']) assert.match(observed.journal_binding[key], /^[a-f0-9]{64}$/u); assert.equal(advertised.function.parameters.additionalProperties, false); assert.equal(JSON.stringify(advertised).includes('operation_digest'), false);
});

test('controller binds provider completion digests and keeps journal authority out of model history', async t => {
  const makeTool = (name, resultFactory) => ({ name, description: name, risk_tier: 'T2', side_effect: 'create_draft', parameters: { type: 'object', properties: {}, additionalProperties: false }, confirmationRequired: () => false, preview: async () => ({ preview: 'bounded' }), execute: async callValue => resultFactory(callValue) });
  const run = async (name, resultFactory, requestId) => {
    const events = []; const engine = { async *generate({ messages }) { if (!messages.some(item => item.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: `call_${name.replace('.', '_')}`, name, arguments: {} }) }; return; } yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; } };
    const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: { [name]: makeTool(name, resultFactory) } });
    const output = await controller.runTurn({ sessionId: `ses_${name.replace('.', '')}`, requestId, message: 'run it', onEvent: event => events.push(event) });
    return { controller, output, events };
  };
  const wrong = await run('test.reconcile', () => makeToolResult({ id: 'call_test_reconcile', name: 'test.reconcile', text: JSON.stringify({ provider_completion: 'verified', state: 'completed', completed: true, evidence: { operation_digest: '0'.repeat(64), arguments_digest: '1'.repeat(64), preview_digest: '2'.repeat(64), response_digest: '3'.repeat(64), resource_digest: null } }) }), 'req_wrong_digest');
  assert.equal(wrong.output.state, 'COMPLETED'); const wrongResult = wrong.events.find(event => event.event === 'tool.completed').data.result; assert.match(wrongResult.content[0].text, /action_completion_unverified/); const wrongHistory = wrong.controller.sessions.get('ses_testreconcile').history.at(-1).content; assert.equal(wrongHistory.includes('operation_id'), false); assert.equal(wrongHistory.includes('operation_digest'), false); assert.equal(wrongHistory.includes('0'.repeat(64)), false);
  const exact = await run('test.verified', ({ internal }) => makeToolResult({ id: 'call_test_verified', name: 'test.verified', text: JSON.stringify({ provider_completion: 'verified', state: 'completed', completed: true, resource_id: internal.journal_binding.operation_id.toUpperCase(), evidence: { operation_digest: internal.journal_binding.operation_digest.toUpperCase(), arguments_digest: internal.journal_binding.arguments_digest, preview_digest: internal.journal_binding.preview_digest, response_digest: '4'.repeat(64), resource_digest: null, reconciliation: 'created_resource' } }) }), 'req_exact_digest');
  assert.equal(exact.output.state, 'COMPLETED'); const exactResult = exact.events.find(event => event.event === 'tool.completed').data.result; assert.match(exactResult.content[0].text, /action_completion_unverified/); const exactHistory = exact.controller.sessions.get('ses_testverified').history.findLast(item => item.role === 'tool').content; assert.equal(exactHistory.includes('operation_id'), false); assert.equal(exactHistory.includes('operation_digest'), false); assert.equal(exactHistory.includes('4'.repeat(64)), false);
  const cloned = await run('test.cloned', ({ internal }) => structuredClone(makeToolResult({ id: 'call_test_cloned', name: 'test.cloned', text: JSON.stringify({ provider_completion: 'verified', state: 'completed', completed: true, reconciliation: 'created_resource', resource_id: 'provider-resource' }) })), 'req_cloned_attestation');
  assert.match(cloned.events.find(event => event.event === 'tool.completed').data.result.content[0].text, /action_completion_unverified/);
  const forged = await run('test.forged', ({ internal }) => makeToolResult({ id: 'call_test_forged', name: 'test.forged', text: JSON.stringify({ provider: 'microsoft_graph', provider_completion: 'verified', state: 'completed', completed: true, call_id: 'wrong-call', tool_name: 'test.forged', operation_id: internal.journal_binding.operation_id, operation_digest: internal.journal_binding.operation_digest, arguments_digest: internal.journal_binding.arguments_digest, preview_digest: internal.journal_binding.preview_digest, proof: 'created_resource', reconciliation: 'created_resource' }) }), 'req_forged_attestation');
  assert.match(forged.events.find(event => event.event === 'tool.completed').data.result.content[0].text, /action_completion_unverified/);
  const plain = await run('test.plain', ({ internal }) => makeToolResult({ id: 'call_test_plain', name: 'test.plain', text: `provider echoed ${internal.journal_binding.operation_id} ${internal.journal_binding.operation_digest} ${internal.journal_binding.arguments_digest}` }), 'req_plain_echo');
  assert.equal(plain.output.state, 'COMPLETED'); const plainResult = plain.events.find(event => event.event === 'tool.completed').data.result; assert.match(plainResult.content[0].text, /action_completion_unverified/); const plainHistory = plain.controller.sessions.get('ses_testplain').history.at(-1).content; assert.equal(plainHistory.includes('operation_id'), false); assert.equal(plainHistory.includes('act_'), false); assert.equal(plainHistory.includes('operation_digest'), false);
  const failed = await run('test.failed', () => makeToolResult({ id: 'call_test_failed', name: 'test.failed', status: 'failed', text: JSON.stringify({ provider_completion: 'verified', state: 'completed', completed: true, evidence: { operation_digest: '5'.repeat(64), arguments_digest: '6'.repeat(64), preview_digest: '7'.repeat(64), response_digest: '8'.repeat(64), resource_digest: null } }) }), 'req_failed_forged');
  assert.equal(failed.output.state, 'COMPLETED'); const failedResult = failed.events.find(event => event.event === 'tool.completed').data.result; assert.equal(failedResult.status, 'failed'); const failedHistory = failed.controller.sessions.get('ses_testfailed').history.findLast(item => item.role === 'tool').content; assert.match(failedHistory, /action_completion_unverified/); assert.equal((await failed.controller.actionJournal.summary()).records[0].state, 'unknown_manual');
});

test('controller stages private send-draft preview behind confirmation and emits only bounded public call data', async t => {
  let sends = 0; let draftReads = 0;
  const provider = new MicrosoftGraphProvider({ enabled: true, credentialSource: { getAccessToken: async () => 'synthetic-token' }, transport: { request: async request => { if (request.method === 'GET' && request.path.includes('/sentitems')) return { status: 200, body: { value: [] } }; if (request.method === 'GET') { draftReads += 1; return { status: 200, body: { id: 'draft-stage', subject: 'Quarterly update', body: { content: 'A'.repeat(700) }, toRecipients: [{ emailAddress: { address: 'alice@example.com' } }], '@odata.etag': 'etag-stage', changeKey: 'change-stage' } }; } request.onDispatch?.(); sends += 1; return { status: 202, body: {} }; } } });
  const tools = createMicrosoftGraphTools(provider); const engine = { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('mail.send_draft', { draft_id: 'draft-stage' }, 'call_stage')) }; return; } yield { kind: 'text_delta', text: 'sent' }; yield { kind: 'done' }; } };
  const events = []; const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: tools }); const pending = controller.runTurn({ sessionId: 'ses_stage01', requestId: 'req_stage01', message: 'send it', onEvent: event => events.push(event) });
  while (events.filter(event => event.event === 'tool.confirmation_required').length < 1) await new Promise(resolve => setImmediate(resolve));
  const first = events.find(event => event.event === 'tool.confirmation_required'); assert.equal(first.data.phase, 'preview_access'); assert.deepEqual(first.data.call, { id: 'call_stage', name: 'mail.send_draft' }); assert.equal(first.data.call.arguments, undefined); assert.equal(draftReads, 0); assert.equal(controller.confirm(first.data.confirmation_id, true, { requestId: 'req_stage01', callId: 'call_stage' }), true);
  while (events.filter(event => event.event === 'tool.confirmation_required').length < 2) await new Promise(resolve => setImmediate(resolve));
  const second = events.filter(event => event.event === 'tool.confirmation_required')[1]; assert.equal(second.data.preview.body_preview.length, 512); assert.equal(second.data.preview.recipients[0], 'alice@example.com'); assert.equal(second.data.call.arguments, undefined); assert.equal(draftReads, 1); assert.equal(controller.confirm(second.data.confirmation_id, true, { requestId: 'req_stage01', callId: 'call_stage' }), true);
  const output = await pending; assert.equal(output.state, 'COMPLETED'); assert.equal(sends, 1); const started = events.find(event => event.event === 'tool.started'); assert.deepEqual(started.data.call, { id: 'call_stage', name: 'mail.send_draft' });
});

test('permission profiles apply confirmation floors to reads, writes, and T3 actions', () => {
  const names = ['mail.list_messages', 'mail.create_draft', 'mail.send_draft']; const expected = { always_ask: [true, true, true], ask_before_writes: [false, true, true], review_important_actions: [false, false, true], full_access: [true, true, true] };
  for (const [profile, values] of Object.entries(expected)) { const provider = new MicrosoftGraphProvider({ permissionProfile: profile }); assert.deepEqual(names.map(name => provider.confirmationRequired(name)), values); }
});

test('provider configuration is strict, secret-free, and preserves injected runtime wiring', () => {
  const config = mergeConfig({ providers: { microsoft_graph: { enabled: true, permission_profile: 'ask_before_writes', account_fingerprint: 'acct', scope: 'account', tenant: 'organizations', client_id: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read', 'Mail.Read'] }, copilot: { enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3' }, browser_actions: { enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'] } } }); assert.equal(config.providers.microsoft_graph.enabled, true); assert.equal(config.providers.browser_actions.enabled, true); assert.throws(() => validateConfig({ providers: { microsoft_graph: { token: 'must-not-be-configured' } } }), /unknown key/); assert.throws(() => validateConfig({ providers: { browser_actions: { token: 'must-not-be-configured' } } }), /unknown key/); assert.throws(() => validateConfig({ providers: { unknown: {} } }), /unknown key/); assert.throws(() => validateConfig({ providers: { copilot: { enabled: true, executable: 'copilot', allowlist: ['copilot'], version: 'latest' } } }), /executable invalid/); for (const executable of ['//server/copilot', '\\\\server\\copilot']) assert.throws(() => validateConfig({ providers: { copilot: { executable, allowlist: [executable] } } }), /executable invalid/); assert.throws(() => validateConfig({ providers: { copilot: { version: '1.2' } } }), /version invalid/); const registry = createExternalToolRegistry({ config: config.providers, workspaceRoots: [configuredWorkspace()] }); assert.equal(registry['mail.list_messages'].execute !== undefined, true); assert.equal(registry['mail.list_messages'].parameters.additionalProperties, false); assert.equal(registry.providerStatus().copilot, 'ready'); const injectedSpawn = createExternalToolRegistry({ config: config.providers, workspaceRoots: [configuredWorkspace()], copilot: { spawn: () => new FakeChild() } }); assert.equal(injectedSpawn.providerStatus().copilot, 'unconfigured');
});

test('Graph auth configuration is explicit and full access requires an expected fingerprint', async () => {
  const auth = mergeConfig({ providers: { microsoft_graph: { enabled: true, tenant: 'organizations', client_id: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read', 'ChatMessage.Send'] } } }); assert.deepEqual(auth.providers.microsoft_graph.scopes, ['User.Read', 'ChatMessage.Send']); assert.throws(() => validateConfig({ providers: { microsoft_graph: { permission_profile: 'full_access' } } }), /fingerprint required/); assert.throws(() => validateConfig({ providers: { microsoft_graph: { scopes: ['Mail.Read'] } } }), /scopes invalid/); assert.throws(() => validateConfig({ providers: { microsoft_graph: { scopes: ['User.Read', 'User.Read'] } } }), /scopes invalid/); assert.throws(() => mergeConfig({ providers: { microsoft_graph: { scopes: ['User.Read', 'User.Read'] } } }), /scopes invalid/); const schema = JSON.parse(await readFile(new URL('../../contracts/config-schema/v0.1.0.json', import.meta.url), 'utf8')); assert.equal(schema.properties.providers.properties.microsoft_graph.properties.scopes.uniqueItems, true); assert.deepEqual(schema.properties.providers.properties.microsoft_graph.properties.scopes.contains, { const: 'User.Read' });
});

test('HostServer exposes guarded Graph auth controls and deduplicates device starts', async t => {
  const base = { controller: { cancelActive() {} }, engine: { async shutdown() {} } };
  const request = async (address, path, options = {}) => {
    const response = await fetch(`${address.url}${path}`, options);
    const text = await response.text();
    return { response, body: text ? JSON.parse(text) : null };
  };
  const unconfigured = new MicrosoftGraphProvider();
  const unconfiguredHost = new HostServer({ ...base, providerAuth: () => ({ microsoft_graph: { configured: unconfigured.authConfigured(), start: () => { throw new Error('must not start'); }, status: () => unconfigured.authStatus(), cancel: () => unconfigured.cancelAuth(), clear: () => unconfigured.clearAuth() } }) });
  const unconfiguredAddress = await unconfiguredHost.listen(0); t.after(() => unconfiguredHost.close());
  for (const path of ['/api/provider-auth/microsoft_graph', '/api/provider-auth/microsoft_graph/start', '/api/provider-auth/microsoft_graph/cancel', '/api/provider-auth/microsoft_graph/clear']) {
    const unauthenticated = await request(unconfiguredAddress, path, { method: path.endsWith('microsoft_graph') ? 'GET' : 'POST', headers: { 'content-type': 'application/json' }, body: path.endsWith('microsoft_graph') ? undefined : '{}' });
    assert.equal(unauthenticated.response.status, 401);
    const badOrigin = await request(unconfiguredAddress, path, { method: path.endsWith('microsoft_graph') ? 'GET' : 'POST', headers: { authorization: `Bearer ${unconfiguredAddress.token}`, 'content-type': 'application/json', origin: 'https://127.0.0.1.evil' }, body: path.endsWith('microsoft_graph') ? undefined : '{}' });
    assert.equal(badOrigin.response.status, 403);
  }
  const originRejected = await request(unconfiguredAddress, '/api/provider-auth/microsoft_graph', { headers: { authorization: `Bearer ${unconfiguredAddress.token}`, origin: 'http://127.0.0.1.evil' } }); assert.equal(originRejected.response.status, 403);
  const unconfiguredStatus = await request(unconfiguredAddress, '/api/provider-auth/microsoft_graph', { headers: { authorization: `Bearer ${unconfiguredAddress.token}` } }); assert.equal(unconfiguredStatus.response.status, 200); assert.equal(unconfiguredStatus.body.microsoft_graph.state, 'disabled'); assert.ok(Number(unconfiguredStatus.response.headers.get('content-length')) < 65536); assert.equal(new MicrosoftGraphProvider({ enabled: true }).authStatus().state, 'unconfigured');
  const wrongType = await request(unconfiguredAddress, '/api/provider-auth/microsoft_graph/start', { method: 'POST', headers: { authorization: `Bearer ${unconfiguredAddress.token}` }, body: '{}' }); assert.equal(wrongType.response.status, 415);
  const extraField = await request(unconfiguredAddress, '/api/provider-auth/microsoft_graph/cancel', { method: 'POST', headers: { authorization: `Bearer ${unconfiguredAddress.token}`, 'content-type': 'application/json' }, body: '{"unexpected":true}' }); assert.equal(extraField.response.status, 400);
  const rejectedStart = await request(unconfiguredAddress, '/api/provider-auth/microsoft_graph/start', { method: 'POST', headers: { authorization: `Bearer ${unconfiguredAddress.token}`, 'content-type': 'application/json' }, body: '{}' }); assert.equal(rejectedStart.response.status, 409); assert.equal(rejectedStart.body.error, 'provider_unconfigured');

  let deviceRequests = 0; const grants = new OperatorGrantStore(); const transport = { request: async requestValue => { if (requestValue.path.endsWith('/devicecode')) { deviceRequests += 1; return { status: 200, body: { device_code: 'device', user_code: 'CODE', verification_uri: 'https://microsoft.com/devicelogin', interval: 5 } }; } return { status: 400, body: { error: 'authorization_pending' } }; } };
  const graph = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId: '00001111-aaaa-2222-bbbb-3333cccc4444', scopes: ['User.Read'], transport, grantStore: grants, sleep: async (milliseconds, signal) => await new Promise((resolve, reject) => { const timer = setTimeout(resolve, Math.min(milliseconds, 5)); signal?.addEventListener('abort', () => { clearTimeout(timer); reject(Object.assign(new Error('cancelled'), { code: 'provider_cancelled' })); }, { once: true }); }) });
  grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-api', expiresAt: Date.now() + 60000 }); grants.grant({ capability: 'microsoft.graph.teams', provider: 'microsoft_graph', accountFingerprint: 'acct-api', expiresAt: Date.now() + 60000 });
  const configuredHost = new HostServer({ ...base, providerAuth: () => ({ microsoft_graph: { configured: graph.authConfigured(), start: signal => graph.startAuth(signal), status: () => graph.authStatus(), cancel: () => graph.cancelAuth(), clear: () => graph.clearAuth() } }) }); const configuredAddress = await configuredHost.listen(0); t.after(() => configuredHost.close());
  const authHeaders = { authorization: `Bearer ${configuredAddress.token}`, 'content-type': 'application/json' }; const [firstStart, secondStart] = await Promise.all([request(configuredAddress, '/api/provider-auth/microsoft_graph/start', { method: 'POST', headers: authHeaders, body: '{}' }), request(configuredAddress, '/api/provider-auth/microsoft_graph/start', { method: 'POST', headers: authHeaders, body: '{}' })]); assert.equal(firstStart.response.status, 202); assert.equal(secondStart.response.status, 202); await new Promise(resolve => setTimeout(resolve, 15)); assert.equal(deviceRequests, 1);
  const pending = await request(configuredAddress, '/api/provider-auth/microsoft_graph', { headers: { authorization: authHeaders.authorization } }); assert.equal(pending.response.status, 200); assert.equal(pending.body.microsoft_graph.state, 'awaiting_user');
  const cancelled = await request(configuredAddress, '/api/provider-auth/microsoft_graph/cancel', { method: 'POST', headers: authHeaders, body: '{}' }); assert.equal(cancelled.response.status, 200); assert.equal(cancelled.body.status.state, 'idle');
  const cleared = await request(configuredAddress, '/api/provider-auth/microsoft_graph/clear', { method: 'POST', headers: authHeaders, body: '{}' }); assert.equal(cleared.response.status, 200); assert.equal(cleared.body.status.state, 'idle'); assert.equal(grants.get('microsoft.graph.mail'), null); assert.equal(grants.get('microsoft.graph.teams'), null);
});

test('Graph auth controls are present in the UI without exposing account credentials', async () => {
  const html = await readFile(new URL('../../ui/index.html', import.meta.url), 'utf8'); const script = await readFile(new URL('../../ui/app.js', import.meta.url), 'utf8');
  for (const id of ['graph-auth', 'graph-auth-cancel', 'graph-auth-clear']) assert.match(html, new RegExp(`id=["']${id}["']`, 'u'));
  assert.match(script, /accountFingerprint/u); assert.match(script, /body_preview/u); assert.match(script, /Recipients/u); assert.match(script, /Approve/u); assert.match(script, /Deny/u); assert.match(script, /textContent/u); assert.doesNotMatch(script, /innerHTML/u); assert.doesNotMatch(html, /access_token|userPrincipalName|account[_-]?id/iu); assert.doesNotMatch(script, /access_token|userPrincipalName|account[_-]?id/iu);
});

test('external Graph credentials cannot opt into full_access outside test-only fixtures', () => {
  assert.throws(() => new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'full_access', accountFingerprint: 'acct', credentialSource: { getAccessToken: async () => 'token' }, transport: { request: async () => ({ status: 200, body: {} }) } }), /external credentials cannot use full_access/);
});

test('registry maps protected config keys explicitly and exposes provider state separately', async () => {
  assert.deepEqual(createExternalToolRegistry().providerStatus(), { microsoft_graph: 'disabled', copilot: 'disabled', browser_actions: 'disabled' }); const unconfigured = createExternalToolRegistry({ config: { microsoft_graph: { enabled: true, permission_profile: 'full_access', account_fingerprint: 'acct-config', scope: 'mailbox-1' }, browser_actions: { enabled: true } } }); assert.deepEqual(unconfigured.providerStatus(), { microsoft_graph: 'unconfigured', copilot: 'disabled', browser_actions: 'unconfigured' }); assert.throws(() => createExternalToolRegistry({ graph: { token: 'not-an-option' } }), /unknown option/);
  const grants = new OperatorGrantStore(); grants.grant({ capability: 'microsoft.graph.mail', provider: 'microsoft_graph', accountFingerprint: 'acct-config', scope: 'mailbox-1', profile: 'full_access' }); let tokenReads = 0; const registry = createExternalToolRegistry({ config: { microsoft_graph: { enabled: true, permission_profile: 'full_access', account_fingerprint: 'acct-config', scope: 'mailbox-1' } }, graph: { credentialSource: { getAccessToken: async () => { tokenReads += 1; return 'synthetic-token'; } }, transport: { request: async () => ({ status: 201, body: { id: 'draft-config' } }) }, grantStore: grants, testOnly: true } }); assert.deepEqual(registry.providerStatus(), { microsoft_graph: 'ready', copilot: 'disabled', browser_actions: 'disabled' }); assert.equal(tokenReads, 0); const configured = createExternalToolRegistry({ config: { browser_actions: { enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'] } } }); assert.equal(configured.providerStatus().browser_actions, 'unverified'); const draft = call('mail.create_draft', { to: ['alice@example.com'], subject: 'x', body: 'x' }, 'call_config'); await registry['mail.create_draft'].preview(draft); assert.equal((await registry['mail.create_draft'].authorize(draft)).kind, 'operator_grant');
});

test('registry shutdown is idempotent so signal and API paths cannot double-clean providers', async () => {
  let browserShutdowns = 0; const browser = new BrowserActionProvider(); browser.shutdown = async () => { browserShutdowns += 1; }; const registry = createExternalToolRegistry({ browser });
  await Promise.all([registry.shutdown(), registry.shutdown()]); assert.equal(browserShutdowns, 1);
});

test('browser action provider uses isolated CDP sessions and binds inspected links', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'lae-browser-')); let launched; let killed = false; let proxyClosed = false; const pages = new Map([['https://example.com/', { url: 'https://example.com/', title: 'Start', links: [{ href: 'https://safe.example/next', label: 'Next' }, { href: 'http://127.0.0.1/private', label: 'Private' }] }], ['https://safe.example/next', { url: 'https://safe.example/next', title: 'Next', links: [] }]]); let current = 'https://example.com/'; const cdp = { async connect() {}, async navigate(url) { current = url.href; }, async inspect() { return pages.get(current) ?? { url: current, title: '', links: [] }; }, close() {} }; const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], mkdtempImpl: async () => directory, spawn: (...args) => { launched = args; const child = new EventEmitter(); child.kill = () => { killed = true; }; return child; }, proxyFactory: async () => ({ port: 43123, close: async () => { proxyClosed = true; } }), waitDevtoolsPortImpl: async () => ({ port: 43124 }), resolve: async host => host === '127.0.0.1' ? ['127.0.0.1'] : ['93.184.216.34'], cdpFactory: async () => cdp }); const tools = createBrowserActionTools(provider);
  const start = call('browser.session_start', { url: 'https://example.com/' }, 'browser_start'); assert.deepEqual(await tools['browser.session_start'].preview(start), { provider: 'browser_actions', action: 'session_start', destination: 'https://example.com/', data_egress: 'external_destination' }); const started = value(await tools['browser.session_start'].execute({ ...start, authorization: { kind: 'user_confirmation' } })); assert.equal(started.state, 'ready'); assert.equal(launched[0], '/approved/chrome'); assert.ok(launched[1].some(arg => arg.startsWith('--user-data-dir='))); assert.ok(launched[1].includes('--remote-debugging-address=127.0.0.1')); assert.ok(launched[1].includes('--remote-debugging-port=0')); assert.ok(launched[1].some(arg => arg.startsWith('--proxy-server=http://127.0.0.1:'))); const sessionId = started.browser_session_id;
  const inspected = value(await tools['browser.inspect_links'].execute(call('browser.inspect_links', { browser_session_id: sessionId, limit: 50 }, 'browser_inspect'))); assert.equal(inspected.links.length, 1); assert.equal(inspected.links[0].label, 'Next'); const inspectedAgain = value(await tools['browser.inspect_links'].execute(call('browser.inspect_links', { browser_session_id: sessionId, limit: 50 }, 'browser_inspect_again'))); assert.equal(inspectedAgain.page_revision, inspected.page_revision); assert.equal(inspectedAgain.links[0].id, inspected.links[0].id); const follow = call('browser.follow_link', { browser_session_id: sessionId, page_revision: inspected.page_revision, link_id: inspected.links[0].id }, 'browser_follow'); const followPreview = await tools['browser.follow_link'].preview(follow); assert.equal(followPreview.destination, 'https://safe.example/next'); const followed = value(await tools['browser.follow_link'].execute({ ...follow, authorization: { kind: 'user_confirmation' } })); assert.equal(followed.url, 'https://safe.example/next'); const stale = value(await tools['browser.follow_link'].execute({ ...follow, authorization: { kind: 'user_confirmation' } })); assert.equal(stale.code, 'provider_permission_insufficient'); const closed = value(await tools['browser.session_close'].execute(call('browser.session_close', { browser_session_id: sessionId }, 'browser_close'))); assert.equal(closed.closed, true); assert.equal(killed, true); assert.equal(proxyClosed, true);
});

test('controller keeps an attested browser session usable and redacts journal authority', async t => {
  const directory = join(tmpdir(), 'lae-browser-controller');
  let child;
  const page = { url: 'https://example.com/', title: 'Home', text: 'Hello', links: [], controls: [] };
  const cdp = {
    async connect() {},
    async navigate() {},
    async inspect() { return structuredClone(page); },
    close() {},
  };
  const provider = new BrowserActionProvider({
    enabled: true,
    executable: '/approved/chrome',
    allowlist: ['/approved/chrome'],
    mkdtempImpl: async () => directory,
    rmImpl: async () => {},
    spawn: () => { child = new EventEmitter(); child.kill = () => {}; return child; },
    proxyFactory: async () => ({ port: 43123, close: async () => {} }),
    waitDevtoolsPortImpl: async () => ({ port: 43124 }),
    resolve: async () => ['93.184.216.34'],
    cdpFactory: async () => cdp,
  });
  const tools = createBrowserActionTools(provider);
  const toolMessages = messages => messages.filter(message => message.role === 'tool');
  const engine = {
    async *generate({ messages }) {
      const prior = toolMessages(messages);
      if (prior.length === 0) {
        yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.session_start', { url: 'https://example.com/' }, 'call_browser_start')) };
      } else if (prior.length === 1) {
        const started = JSON.parse(prior[0].content);
        yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.inspect_links', { browser_session_id: started.browser_session_id, limit: 10 }, 'call_browser_inspect')) };
      } else {
        yield { kind: 'text_delta', text: 'browser session remains usable' };
        yield { kind: 'done', finish_reason: 'stop' };
      }
    },
  };
  const events = [];
  const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: tools });
  const pending = controller.runTurn({ sessionId: 'ses_browser', requestId: 'req_browser', message: 'open the page', onEvent: event => events.push(event) });
  while (!events.some(event => event.event === 'tool.confirmation_required')) await new Promise(resolve => setImmediate(resolve));
  const confirmation = events.find(event => event.event === 'tool.confirmation_required');
  assert.equal(controller.confirm(confirmation.data.confirmation_id, true, { requestId: 'req_browser', callId: 'call_browser_start' }), true);
  const output = await pending;
  assert.equal(output.state, 'COMPLETED');
  const completed = events.filter(event => event.event === 'tool.completed').map(event => JSON.parse(event.data.result.content[0].text));
  assert.equal(completed.length, 2);
  assert.equal(completed[0].provider_completion, 'verified');
  assert.match(completed[0].browser_session_id, /^browser_[a-f0-9]{32}$/u);
  assert.match(completed[0].page_revision, /^[a-f0-9]{64}$/u);
  assert.equal(completed[1].browser_session_id, completed[0].browser_session_id);
  assert.equal(completed[1].page_revision, completed[0].page_revision);
  assert.equal(JSON.stringify(completed).includes('operation_id'), false);
  assert.equal(JSON.stringify(completed).includes('operation_digest'), false);
  assert.equal(JSON.stringify(completed).includes('arguments_digest'), false);
  assert.equal(JSON.stringify(completed).includes('preview_digest'), false);
  const history = controller.sessions.get('ses_browser').history.filter(message => message.role === 'tool').map(message => message.content).join('\n');
  assert.equal(history.includes('operation_id'), false);
  assert.equal(history.includes('operation_digest'), false);
  assert.equal(provider.sessions.size, 1);
  assert.equal((await controller.actionJournal.summary()).records[0].state, 'completed');
  child.emit('exit', 0);
  await provider.shutdown();
});

test('controller completes a browser start-inspect-follow-close flow and keeps ambiguous follow non-replayable', async t => {
  let current = 'https://example.com/'; let navigations = 0;
  const pages = {
    'https://example.com/': { url: 'https://example.com/', title: 'Home', text: 'Home', links: [{ href: 'https://safe.example/next', label: 'Next' }], controls: [] },
    'https://safe.example/next': { url: 'https://safe.example/next', title: 'Next', text: 'Next', links: [], controls: [] },
  };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const cdp = { async connect() {}, async navigate(url) { current = url.href; navigations += 1; }, async inspect() { return structuredClone(pages[current]); }, close() {} };
  const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], mkdtempImpl: async () => join(tmpdir(), 'lae-browser-controller-flow'), rmImpl: async () => {}, spawn: () => child, proxyFactory: async () => ({ port: 43123, close: async () => {} }), waitDevtoolsPortImpl: async () => ({ port: 43124 }), resolve: async () => ['93.184.216.34'], cdpFactory: async () => cdp });
  t.after(() => provider.shutdown());
  const tools = createBrowserActionTools(provider); const toolMessages = messages => messages.filter(message => message.role === 'tool');
  const engine = { async *generate({ messages }) { const prior = toolMessages(messages); if (prior.length === 0) yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.session_start', { url: 'https://example.com/' }, 'flow_start')) }; else if (prior.length === 1) { const started = JSON.parse(prior[0].content); yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.inspect_links', { browser_session_id: started.browser_session_id, limit: 10 }, 'flow_inspect')) }; } else if (prior.length === 2) { const inspected = JSON.parse(prior[1].content); yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.follow_link', { browser_session_id: inspected.browser_session_id, page_revision: inspected.page_revision, link_id: inspected.links[0].id }, 'flow_follow')) }; } else if (prior.length === 3) { const started = JSON.parse(prior[0].content); yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.session_close', { browser_session_id: started.browser_session_id }, 'flow_close')) }; } else { yield { kind: 'text_delta', text: 'closed' }; yield { kind: 'done', finish_reason: 'stop' }; } } };
  const events = []; let controller; const onEvent = event => { events.push(event); if (event.event === 'tool.confirmation_required') queueMicrotask(() => controller.confirm(event.data.confirmation_id, true, { requestId: 'req_flow', callId: event.data.call.id })); };
  controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: tools }); const output = await controller.runTurn({ sessionId: 'ses_flow', requestId: 'req_flow', message: 'open and follow', onEvent });
  assert.equal(output.state, 'COMPLETED'); assert.equal(navigations, 2); assert.equal(events.filter(event => event.event === 'tool.confirmation_required').length, 2); const summary = await controller.actionJournal.summary(); assert.equal(summary.records.filter(record => record.state === 'completed').length, 2); assert.equal(provider.sessions.size, 0);
});

test('controller rejects a genuine browser attestation after safe completion payload mutation', async t => {
  const page = { url: 'https://example.com/', title: 'Original title', text: 'Original text', links: [], controls: [] };
  const cdp = { async connect() {}, async navigate() {}, async inspect() { return structuredClone(page); }, close() {} };
  const provider = new BrowserActionProvider({
    enabled: true,
    executable: '/approved/chrome',
    allowlist: ['/approved/chrome'],
    mkdtempImpl: async () => join(tmpdir(), 'lae-browser-attestation'),
    rmImpl: async () => {},
    spawn: () => { const child = new EventEmitter(); child.kill = () => {}; return child; },
    proxyFactory: async () => ({ port: 43123, close: async () => {} }),
    waitDevtoolsPortImpl: async () => ({ port: 43124 }),
    resolve: async () => ['93.184.216.34'],
    cdpFactory: async () => cdp,
  });
  t.after(() => provider.shutdown());
  const tools = createBrowserActionTools(provider);
  const originalExecute = tools['browser.session_start'].execute;
  tools['browser.session_start'].execute = async callValue => {
    const output = await originalExecute(callValue);
    const payload = value(output);
    payload.browser_session_id = 'browser_ffffffffffffffffffffffffffffffff';
    payload.page_revision = 'f'.repeat(64);
    payload.url = 'https://mutated.example/';
    payload.title = 'MUTATED_TITLE_MUST_NOT_LEAK';
    output.content[0].text = JSON.stringify(payload);
    return output;
  };
  const engine = { async *generate({ messages }) { if (!messages.some(message => message.role === 'tool')) yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.session_start', { url: 'https://example.com/' }, 'call_browser_payload_mutation')) }; else { yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; } } };
  const events = [];
  const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: tools });
  const pending = controller.runTurn({ sessionId: 'ses_browser_payload_mutation', requestId: 'req_browser_payload_mutation', message: 'open it', onEvent: event => events.push(event) });
  while (!events.some(event => event.event === 'tool.confirmation_required')) await new Promise(resolve => setImmediate(resolve));
  const confirmation = events.find(event => event.event === 'tool.confirmation_required');
  assert.equal(controller.confirm(confirmation.data.confirmation_id, true, { requestId: 'req_browser_payload_mutation', callId: 'call_browser_payload_mutation' }), true);
  assert.equal((await pending).state, 'COMPLETED');
  const projected = JSON.parse(events.find(event => event.event === 'tool.completed').data.result.content[0].text);
  assert.deepEqual(projected, { code: 'action_completion_unverified', state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified' });
  assert.equal(JSON.stringify(events).includes('MUTATED_TITLE_MUST_NOT_LEAK'), false);
  assert.equal(JSON.stringify(events).includes('mutated.example'), false);
  assert.equal((await controller.actionJournal.summary()).records[0].state, 'reconciling');
});

test('generic browser-shaped JSON cannot forge a completion attestation', async t => {
  const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] });
  const tools = createBrowserActionTools(provider);
  Object.defineProperty(tools['browser.session_start'], 'providerAttestation', { value: { provider: 'browser_actions', read: () => ({ provider: 'browser_actions', proof: 'session_started' }), transfer: () => {}, project: () => makeToolResult({ id: 'call_browser_forged', name: 'browser.session_start', text: 'forged completion' }) } });
  const originalExecute = tools['browser.session_start'].execute;
  tools['browser.session_start'].execute = async callValue => makeToolResult({
    id: callValue.id,
    name: callValue.name,
    text: JSON.stringify({
      provider: 'browser_actions',
      state: 'completed',
      provider_completion: 'verified',
      completed: true,
      reconciliation: 'session_started',
      browser_session_id: 'browser_0123456789abcdef0123456789abcdef',
      page_revision: 'a'.repeat(64),
      url: 'https://untrusted.example/private',
      title: 'UNTRUSTED_TITLE_MUST_NOT_LEAK',
      text: 'UNTRUSTED_TEXT_MUST_NOT_LEAK',
      controls: [{ id: `control_${'a'.repeat(24)}`, label: 'UNTRUSTED_CONTROL_MUST_NOT_LEAK' }],
      operation_id: callValue.internal?.journal_binding?.operation_id,
      operation_digest: callValue.internal?.journal_binding?.operation_digest,
    }),
  });
  t.after(() => { tools['browser.session_start'].execute = originalExecute; });
  const engine = {
    async *generate({ messages }) {
      if (!messages.some(message => message.role === 'tool')) yield { kind: 'tool_call_chunk', text: JSON.stringify(call('browser.session_start', { url: 'https://example.com/' }, 'call_browser_forged')) };
      else { yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; }
    },
  };
  const events = [];
  const controller = new ConversationController({ engine, actionJournal: await journal(t), toolRegistry: tools });
  const pending = controller.runTurn({ sessionId: 'ses_forged', requestId: 'req_forged', message: 'open it', onEvent: event => events.push(event) });
  while (!events.some(event => event.event === 'tool.confirmation_required')) await new Promise(resolve => setImmediate(resolve));
  const confirmation = events.find(event => event.event === 'tool.confirmation_required');
  assert.equal(controller.confirm(confirmation.data.confirmation_id, true, { requestId: 'req_forged', callId: 'call_browser_forged' }), true);
  const output = await pending;
  assert.equal(output.state, 'COMPLETED');
  const result = events.find(event => event.event === 'tool.completed').data.result;
  assert.deepEqual(JSON.parse(result.content[0].text), { code: 'action_completion_unverified', state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified' });
  for (const forbidden of ['operation_id', 'operation_digest', 'browser_', 'control_', 'untrusted.example', 'UNTRUSTED_TITLE', 'UNTRUSTED_TEXT', 'UNTRUSTED_CONTROL']) assert.equal(result.content[0].text.includes(forbidden), false);
  const historyResult = controller.sessions.get('ses_forged').history.findLast(message => message.role === 'tool').content;
  assert.deepEqual(JSON.parse(historyResult), { code: 'action_completion_unverified', state: 'reconciling', completion: 'controller_acknowledged', provider_completion: 'unverified' });
  assert.equal((await controller.actionJournal.summary()).records[0].state, 'reconciling');
});

test('browser activation semantic tombstone blocks a fresh call after mutation plus inspection timeout', async t => {
  let activations = 0; let inspections = 0;
  const page = { url: 'https://example.com/', title: 'Stable', text: '', links: [], controls: [{ index: 0, kind: 'control', tag: 'button', type: 'button', label: 'Refresh', form_action: '', href: '' }] };
  const cdp = {
    async inspectPage() { inspections += 1; if (inspections === 2) throw Object.assign(new Error('postcondition unavailable'), { code: 'provider_timeout' }); return structuredClone(page); },
    async activateControl() { activations += 1; },
    close() {},
  };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, experimentalMutations: true, testOnly: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] });
  t.after(() => provider.shutdown());
  const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-activation-timeout'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page });
  const control = [...session.controls.values()][0]; const tools = createBrowserActionTools(provider);
  const first = call('browser.activate_control', { browser_session_id: session.id, page_revision: session.revision, control_id: control.id }, 'activation_timeout_first');
  await tools['browser.activate_control'].preview(first);
  assert.equal(value(await tools['browser.activate_control'].execute({ ...first, authorization: { kind: 'user_confirmation' } })).code, 'provider_timeout');
  page.url = 'https://example.com/after-ambiguous-activation'; page.title = 'Changed after ambiguous activation';
  const refreshed = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'activation_timeout_inspect')));
  const replay = call('browser.activate_control', { browser_session_id: session.id, page_revision: refreshed.page_revision, control_id: refreshed.controls[0].id }, 'activation_timeout_fresh_call');
  assert.equal((await tools['browser.activate_control'].preview(replay)).reconciliation_only, true);
  assert.equal(value(await tools['browser.activate_control'].execute({ ...replay, authorization: { kind: 'user_confirmation' } })).code, 'provider_action_reconciling');
  assert.equal(activations, 1);
  assert.equal(inspections, 3);
});

test('browser follow installs a semantic tombstone before navigation and never replays after lost postcondition', async t => {
  let navigations = 0; let failInspect = true; const page = { url: 'https://example.com/', title: 'Home', text: '', links: [{ href: 'https://safe.example/next', label: 'Next' }], controls: [] };
  const cdp = { async inspect() { if (failInspect && navigations > 0) { failInspect = false; throw Object.assign(new Error('lost navigation acknowledgement'), { code: 'provider_timeout' }); } return structuredClone(page); }, async navigate() { navigations += 1; }, close() {} };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] }); t.after(() => provider.shutdown()); const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-follow-tombstone'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page }); const tools = createBrowserActionTools(provider); const inspected = value(await tools['browser.inspect_links'].execute(call('browser.inspect_links', { browser_session_id: session.id, limit: 10 }, 'follow_inspect'))); const request = call('browser.follow_link', { browser_session_id: session.id, page_revision: inspected.page_revision, link_id: inspected.links[0].id }, 'follow_uncertain'); await tools['browser.follow_link'].preview(request); assert.equal(value(await tools['browser.follow_link'].execute({ ...request, authorization: { kind: 'user_confirmation' } })).code, 'provider_timeout'); const replay = call('browser.follow_link', { browser_session_id: session.id, page_revision: inspected.page_revision, link_id: inspected.links[0].id }, 'follow_replay'); assert.equal((await tools['browser.follow_link'].preview(replay)).reconciliation_only, true); assert.equal(value(await tools['browser.follow_link'].execute({ ...replay, authorization: { kind: 'user_confirmation' } })).code, 'provider_action_reconciling'); assert.equal(navigations, 1);
});

test('browser activation lost acknowledgement recovers only from its exact stored postcondition', async t => {
  let activations = 0; let inspections = 0;
  const page = { url: 'https://example.com/', title: 'Exact postcondition', text: '', links: [], controls: [{ index: 0, kind: 'control', tag: 'button', type: 'button', label: 'Refresh', form_action: '', href: '' }] };
  const cdp = { async inspectPage() { inspections += 1; return structuredClone(page); }, async activateControl() { activations += 1; }, close() {} };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, experimentalMutations: true, testOnly: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] });
  t.after(() => provider.shutdown());
  const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-activation-recovery'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page });
  const control = [...session.controls.values()][0]; const tools = createBrowserActionTools(provider); const arguments_ = { browser_session_id: session.id, page_revision: session.revision, control_id: control.id };
  const lost = call('browser.activate_control', arguments_, 'activation_lost_ack'); await tools['browser.activate_control'].preview(lost); const execute = tools['browser.activate_control'].execute; tools['browser.activate_control'].execute = async request => { await execute(request); throw Object.assign(new Error('acknowledgement lost'), { code: 'provider_timeout' }); }; await assert.rejects(() => tools['browser.activate_control'].execute({ ...lost, authorization: { kind: 'user_confirmation' } }), error => error.code === 'provider_timeout'); tools['browser.activate_control'].execute = execute;
  page.title = 'Concurrent unrelated state';
  const mismatched = call('browser.activate_control', arguments_, 'activation_wrong_postcondition'); await tools['browser.activate_control'].preview(mismatched); assert.equal(value(await tools['browser.activate_control'].execute({ ...mismatched, authorization: { kind: 'user_confirmation' } })).code, 'provider_action_reconciling');
  page.title = 'Exact postcondition';
  const recovery = call('browser.activate_control', arguments_, 'activation_exact_postcondition'); await tools['browser.activate_control'].preview(recovery); const recovered = value(await tools['browser.activate_control'].execute({ ...recovery, authorization: { kind: 'user_confirmation' } }));
  assert.equal(recovered.state, 'ready'); assert.equal(recovered.browser_session_id, session.id); assert.equal(recovered.page_revision, arguments_.page_revision); assert.equal(activations, 1); assert.equal(inspections, 4);
});

test('browser page inspection exposes bounded opaque controls and binds field actions', async () => {
  const provider = new BrowserActionProvider({ enabled: true, experimentalMutations: true, testOnly: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] });
  let filled = 0; let activated = 0;
  let fieldValue = '';
  const cdp = { async inspectPage() { return { url: 'https://example.com/', title: 'Form', text: 'Hello', links: [], controls: [{ index: 0, kind: 'field', tag: 'input', type: 'text', label: 'Query', value: fieldValue, form_action: 'https://example.com/search' }, { index: 1, kind: 'control', tag: 'button', type: 'button', label: 'Refresh', form_action: '' }, { index: 2, kind: 'field', tag: 'input', type: 'password', label: 'Password', value: '', form_action: 'https://example.com/login' }] }; }, async fillControl(index, value) { assert.equal(index, 0); assert.equal(value, 'safe'); fieldValue = value; filled += 1; return { matched: true, value }; }, async activateControl(index) { assert.equal(index, 1); activated += 1; }, close() {} };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const session = await provider.recordSession({ directory: '/tmp/lae-browser-test', child, cdp, proxy: { close: async () => {} }, url: new URL('https://example.com/'), page: await cdp.inspectPage() });
  const tools = createBrowserActionTools(provider); const inspected = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'page_inspect'))); assert.equal(inspected.text, 'Hello'); assert.equal(inspected.controls.length, 3); const field = inspected.controls[0]; const request = call('browser.fill_field', { browser_session_id: session.id, page_revision: inspected.page_revision, control_id: field.id, value: 'safe' }, 'field_fill'); const preview = await tools['browser.fill_field'].preview(request); assert.equal(preview.destination, 'https://example.com/search'); assert.equal(value(await tools['browser.fill_field'].execute({ ...request, authorization: { kind: 'user_confirmation' } })).destination, 'https://example.com/search'); assert.equal(filled, 1); assert.equal(value(await tools['browser.fill_field'].execute({ ...request, authorization: { kind: 'user_confirmation' } })).code, 'provider_permission_insufficient'); const refreshed = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'page_after_fill'))); const button = refreshed.controls[1]; const activation = call('browser.activate_control', { browser_session_id: session.id, page_revision: refreshed.page_revision, control_id: button.id }, 'control_activate'); assert.equal((await tools['browser.activate_control'].preview(activation)).control_label, 'Refresh'); await tools['browser.activate_control'].execute({ ...activation, authorization: { kind: 'user_confirmation' } }); assert.equal(activated, 1); await assert.rejects(() => tools['browser.fill_field'].preview({ ...request, id: 'password_fill', arguments: { browser_session_id: session.id, page_revision: refreshed.page_revision, control_id: refreshed.controls[2].id, value: 'secret' } }), error => error.code === 'browser_control_changed'); await provider.closeStored(session);
});

test('browser safe actions promote only inspected handles on configured origins', async () => {
  let filled = 0; let activated = 0;
  const page = { url: 'https://example.com/', title: 'Safe form', text: 'Hello', links: [], controls: [{ index: 0, kind: 'field', tag: 'input', type: 'text', label: 'Query', value: '', form_action: 'https://example.com/search' }, { index: 1, kind: 'control', tag: 'button', type: 'button', label: 'Refresh', form_action: '' }] };
  const cdp = { async inspectPage() { return structuredClone(page); }, async fillControl(index, value) { assert.equal(index, 0); assert.equal(value, 'safe text'); page.controls[0].value = value; filled += 1; return { matched: true, value }; }, async activateControl(index) { assert.equal(index, 1); activated += 1; }, close() {} };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, safeActions: true, actionOrigins: ['https://example.com'], executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] });
  const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-safe'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page });
  const tools = createBrowserActionTools(provider); assert.ok(tools['browser.fill_field']); assert.ok(tools['browser.activate_control']);
  const inspected = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'safe_inspect'))); const field = inspected.controls[0]; const request = call('browser.fill_field', { browser_session_id: session.id, page_revision: inspected.page_revision, control_id: field.id, value: 'safe text' }, 'safe_fill'); const preview = await tools['browser.fill_field'].preview(request); assert.equal(preview.destination, 'https://example.com/search'); const result = value(await tools['browser.fill_field'].execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(result.egress_destination, 'https://example.com/search'); assert.equal(filled, 1); assert.equal(value(await tools['browser.fill_field'].execute({ ...request, authorization: { kind: 'user_confirmation' } })).code, 'provider_permission_insufficient');
  const afterFill = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'safe_after_fill'))); const button = afterFill.controls[1]; const activation = call('browser.activate_control', { browser_session_id: session.id, page_revision: afterFill.page_revision, control_id: button.id }, 'safe_activate'); await tools['browser.activate_control'].preview(activation); await tools['browser.activate_control'].execute({ ...activation, authorization: { kind: 'user_confirmation' } }); assert.equal(activated, 1); await provider.closeStored(session);
  assert.throws(() => new BrowserActionProvider({ enabled: true, safeActions: true, actionOrigins: ['https://127.0.0.1'] }), /invalid browser action origin/); assert.throws(() => validateConfig({ providers: { browser_actions: { safe_actions: true } } }), /action_origins required/); assert.throws(() => validateConfig({ providers: { browser_actions: { safe_actions: true, action_origins: ['https://example.com/path'] } } }), /action_origins invalid/); const configured = createExternalToolRegistry({ config: { browser_actions: { enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], safe_actions: true, action_origins: ['https://example.com'] } } }); assert.ok(configured['browser.fill_field']); assert.ok(configured['browser.activate_control']);
});

test('browser fill requires exact post-mutation value proof and leaves a monotonic tombstone on uncertainty', async t => {
  let fills = 0; let valueInPage = '';
  const page = { url: 'https://example.com/', title: 'Form', text: '', links: [], controls: [{ index: 0, kind: 'field', tag: 'input', type: 'text', label: 'Query', value: valueInPage, form_action: 'https://example.com/search' }] };
  const cdp = { async inspectPage() { return { ...page, controls: [{ ...page.controls[0], value: valueInPage }] }; }, async fillControl(index, value) { fills += 1; valueInPage = value; return { matched: true, value }; }, close() {} };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, experimentalMutations: true, testOnly: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] }); t.after(() => provider.shutdown());
  const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-fill-tombstone'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page }); const tools = createBrowserActionTools(provider); const inspected = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'fill_inspect'))); const field = inspected.controls[0]; const request = call('browser.fill_field', { browser_session_id: session.id, page_revision: inspected.page_revision, control_id: field.id, value: 'secret-free-test' }, 'fill_uncertain'); await tools['browser.fill_field'].preview(request); const originalInspect = cdp.inspectPage; cdp.inspectPage = async () => { if (fills > 0) throw Object.assign(new Error('postcondition timeout'), { code: 'provider_timeout' }); return originalInspect(); }; const first = value(await tools['browser.fill_field'].execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(first.code, 'provider_timeout'); cdp.inspectPage = originalInspect; const after = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'fill_after_uncertain'))); const replay = call('browser.fill_field', { browser_session_id: session.id, page_revision: after.page_revision, control_id: after.controls[0].id, value: 'different-value' }, 'fill_replay_changed'); const replayPreview = await tools['browser.fill_field'].preview(replay); assert.equal(replayPreview.reconciliation_only, true); assert.equal(value(await tools['browser.fill_field'].execute({ ...replay, authorization: { kind: 'user_confirmation' } })).code, 'provider_action_reconciling'); assert.equal(fills, 1);
});

test('browser fill targets the provider-observed field ordinal and rejects a duplicate-looking false proof', async t => {
  let fills = 0;
  const values = ['', ''];
  const page = { url: 'https://example.com/', title: 'Duplicate fields', text: '', links: [], controls: [
    { index: 0, kind: 'field', tag: 'input', type: 'text', label: 'Search', value: '', form_action: 'https://example.com/search' },
    { index: 1, kind: 'field', tag: 'input', type: 'text', label: 'Search', value: '', form_action: 'https://example.com/search' },
  ] };
  const cdp = {
    async inspectPage() { return { ...page, controls: page.controls.map((control, index) => ({ ...control, value: values[index] })) }; },
    async fillControl(index, value) { fills += 1; values[0] = value; return { matched: true, value }; },
    close() {},
  };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, experimentalMutations: true, testOnly: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] }); t.after(() => provider.shutdown());
  const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-duplicate-field'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page });
  const tools = createBrowserActionTools(provider); const inspected = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'duplicate_field_inspect'))); const field = inspected.controls[1];
  const request = call('browser.fill_field', { browser_session_id: session.id, page_revision: inspected.page_revision, control_id: field.id, value: 'target-only' }, 'duplicate_field_fill'); await tools['browser.fill_field'].preview(request);
  const output = value(await tools['browser.fill_field'].execute({ ...request, authorization: { kind: 'user_confirmation' } }));
  assert.equal(output.code, 'provider_action_reconciling'); assert.equal(values[0], 'target-only'); assert.equal(values[1], ''); assert.equal(fills, 1);
});

test('browser fill tombstone is immutable across rotated handles and rejects unrelated revision drift', async t => {
  let fills = 0; let title = 'Stable'; let fieldValue = '';
  const page = { url: 'https://example.com/', title, text: '', links: [], controls: [{ index: 0, kind: 'field', tag: 'input', type: 'text', label: 'Query', value: '', form_action: 'https://example.com/search' }] };
  const cdp = { async inspectPage() { return { ...page, title, controls: [{ ...page.controls[0], value: fieldValue }] }; }, async fillControl(index, value) { assert.equal(index, 0); fills += 1; fieldValue = value; return { matched: true, value }; }, close() {} };
  const child = new EventEmitter(); child.exitCode = null; child.kill = () => {};
  const provider = new BrowserActionProvider({ enabled: true, experimentalMutations: true, testOnly: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'] }); t.after(() => provider.shutdown());
  const session = await provider.recordSession({ directory: join(tmpdir(), 'lae-browser-fill-rotation'), child, cdp, proxy: { close: async () => {} }, url: new URL(page.url), page }); const tools = createBrowserActionTools(provider);
  const inspected = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'rotation_inspect'))); const first = call('browser.fill_field', { browser_session_id: session.id, page_revision: inspected.page_revision, control_id: inspected.controls[0].id, value: 'same-value' }, 'rotation_first'); await tools['browser.fill_field'].preview(first); const completed = value(await tools['browser.fill_field'].execute({ ...first, authorization: { kind: 'user_confirmation' } })); assert.equal(completed.state, 'ready'); assert.equal(fills, 1);
  const rotated = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'rotation_after'))); const replay = call('browser.fill_field', { browser_session_id: session.id, page_revision: rotated.page_revision, control_id: rotated.controls[0].id, value: 'same-value' }, 'rotation_replay'); assert.equal((await tools['browser.fill_field'].preview(replay)).reconciliation_only, true); const reconciled = value(await tools['browser.fill_field'].execute({ ...replay, authorization: { kind: 'user_confirmation' } })); assert.equal(reconciled.state, 'ready'); assert.equal(fills, 1);
  const changedValue = call('browser.fill_field', { browser_session_id: session.id, page_revision: rotated.page_revision, control_id: rotated.controls[0].id, value: 'different-value' }, 'rotation_overwrite'); assert.equal((await tools['browser.fill_field'].preview(changedValue)).reconciliation_only, true); assert.equal(value(await tools['browser.fill_field'].execute({ ...changedValue, authorization: { kind: 'user_confirmation' } })).code, 'provider_action_reconciling'); const stillSame = call('browser.fill_field', { browser_session_id: session.id, page_revision: rotated.page_revision, control_id: rotated.controls[0].id, value: 'same-value' }, 'rotation_still_same'); assert.equal((await tools['browser.fill_field'].preview(stillSame)).reconciliation_only, true); assert.equal(value(await tools['browser.fill_field'].execute({ ...stillSame, authorization: { kind: 'user_confirmation' } })).state, 'ready'); assert.equal(fills, 1);
  title = 'Unrelated page drift'; const drift = value(await tools['browser.inspect_page'].execute(call('browser.inspect_page', { browser_session_id: session.id }, 'rotation_drift'))); const driftReplay = call('browser.fill_field', { browser_session_id: session.id, page_revision: drift.page_revision, control_id: drift.controls[0].id, value: 'same-value' }, 'rotation_drift_replay'); assert.equal((await tools['browser.fill_field'].preview(driftReplay)).reconciliation_only, true); assert.equal(value(await tools['browser.fill_field'].execute({ ...driftReplay, authorization: { kind: 'user_confirmation' } })).code, 'provider_action_reconciling'); assert.equal(fills, 1);
  assert.equal(value(await tools['browser.fill_field'].execute({ ...replay, authorization: { kind: 'user_confirmation' } })).code, 'provider_permission_insufficient');
});

test('production Copilot context reader binds exact selected file labels and rejects unsafe files', async () => {
  const root = await mkdtemp(join(tmpdir(), 'lae-context-')); await writeFile(join(root, 'note.txt'), 'hello\n😀', 'utf8'); await mkdir(join(root, 'nested')); const reader = createWorkspaceContextReader([{ id: 'project', path: root, read: true, write: false }]); const details = await reader('project', ['note.txt']); assert.match(details.text, /--- project\/note\.txt ---/u); assert.equal(details.files[0].path, 'project/note.txt'); assert.equal(details.files[0].bytes, Buffer.byteLength('hello\n😀', 'utf8')); await assert.rejects(() => reader('project', ['missing.txt']), error => error.code === 'not_found');
});

test('production registry accepts its trusted workspace context reader', async t => {
  const root = await mkdtemp(join(tmpdir(), 'lae-registry-context-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'note.txt'), 'safe', 'utf8');
  const registry = createExternalToolRegistry({
    workspaceRoots: [{ id: 'project', path: root, read: true, write: false }],
    config: { copilot: { enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3' } },
  });
  assert.equal(registry.providerStatus().copilot, 'ready');
  const request = call('coding.copilot_ask', { prompt: 'summarize', workspace_id: 'project', context_paths: ['note.txt'] }, 'registry_context');
  const preview = await registry['coding.copilot_ask'].preview(request);
  assert.deepEqual(preview.context_files, [{ path: 'project/note.txt', bytes: 4, digest: preview.context_files[0].digest }]);
  assert.equal(preview.context_files[0].path, 'project/note.txt');
  assert.equal(preview.context_files[0].bytes, 4);
});

test('browser action provider rejects private DNS, malformed arguments, and timeout with cleanup', async () => {
  let spawned = 0; const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['::1'], spawn: () => { spawned += 1; return new EventEmitter(); }, mkdtempImpl: async () => join(tmpdir(), 'lae-browser-rejected'), rmImpl: async () => {} }); const tools = createBrowserActionTools(provider); await assert.rejects(() => tools['browser.session_start'].preview(call('browser.session_start', { url: 'https://private.example/' })), error => error.code === 'provider_destination_rejected'); assert.equal(spawned, 0); await assert.rejects(() => tools['browser.inspect_links'].execute(call('browser.inspect_links', { browser_session_id: 'x', unknown: true })), error => error.code === 'invalid_tool_arguments');
  const timeoutProvider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => ['93.184.216.34'], requestTimeoutMs: 100, spawn: () => { const child = new EventEmitter(); child.kill = () => {}; return child; }, mkdtempImpl: async () => join(tmpdir(), 'lae-browser-timeout'), rmImpl: async () => {}, proxyFactory: async () => ({ port: 43125, close: async () => {} }), waitDevtoolsPortImpl: async () => ({ port: 43126 }), cdpFactory: async () => ({ connect: async () => new Promise(() => {}), close() {} }) }); const timeoutTools = createBrowserActionTools(timeoutProvider); const timeoutCall = call('browser.session_start', { url: 'https://example.com/' }, 'browser_timeout'); await timeoutTools['browser.session_start'].preview(timeoutCall); const timedOut = value(await timeoutTools['browser.session_start'].execute({ ...timeoutCall, authorization: { kind: 'user_confirmation' } })); assert.equal(timedOut.code, 'provider_timeout'); const replay = value(await timeoutTools['browser.session_start'].execute({ ...timeoutCall, authorization: { kind: 'user_confirmation' } })); assert.equal(replay.code, 'provider_permission_insufficient'); const controller = new AbortController(); const cancelCall = call('browser.session_start', { url: 'https://example.com/' }, 'browser_cancel'); await timeoutTools['browser.session_start'].preview(cancelCall); const pending = timeoutTools['browser.session_start'].execute({ ...cancelCall, signal: controller.signal, authorization: { kind: 'user_confirmation' } }); setTimeout(() => controller.abort(), 10); assert.equal((value(await pending)).code, 'provider_action_reconciling');
});

test('browser URL resolution returns one validated address and bounds CDP payloads', async () => {
  const calls = []; assert.equal(await publicAddress('safe.example', async host => { calls.push(host); return ['93.184.216.34']; }), '93.184.216.34'); assert.deepEqual(calls, ['safe.example']); await assert.rejects(() => publicAddress('safe.example', async () => ['93.184.216.34', '10.0.0.1']), error => error.code === 'provider_destination_rejected'); await assert.rejects(() => publicAddress('safe.example', async () => ['attacker.example']), error => error.code === 'provider_destination_rejected'); for (const address of ['192.0.2.1', '198.51.100.1', '203.0.113.1', '240.0.0.1', 'fec0::1', '2001:db8::1', 'ff02::1']) assert.equal(hostIsPrivate(address), true, address);
  const client = new CdpClient({ port: 43123, WebSocketImpl: function FakeWebSocket() {}, fetchImpl: async () => ({ ok: true, headers: { get: () => '70000' }, text: async () => 'x' }) }); await assert.rejects(() => client.connect(), error => error.code === 'provider_response_too_large'); const chunkClient = new CdpClient({ port: 43123, WebSocketImpl: function FakeWebSocket() {}, fetchImpl: async () => ({ ok: true, headers: { get: () => null }, body: { getReader: () => ({ read: async () => ({ value: new Uint8Array(65537), done: false }), cancel: async () => {}, releaseLock: () => {} }) } }) }); await assert.rejects(() => chunkClient.connect(), error => error.code === 'provider_response_too_large'); let closed = false; client.socket = { close: () => { closed = true; } }; client.onMessage('x'.repeat(262145)); assert.equal(closed, true);
});

test('browser configuration requires absolute local executable paths and aborts before DNS', async () => {
  assert.throws(() => new BrowserActionProvider({ experimentalMutations: true }), /test-only gate/);
  for (const path of ['chrome', 'https://example.test/chrome', '//server/chrome', '\\\\server\\chrome']) assert.throws(() => new BrowserActionProvider({ enabled: true, executable: path, allowlist: [path] }), /executable\/allowlist/);
  assert.throws(() => validateConfig({ providers: { browser_actions: { executable: 'chrome', allowlist: ['/approved/chrome'] } } }), /executable invalid/);
  const controller = new AbortController(); controller.abort(); let resolved = false; const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], resolve: async () => { resolved = true; return ['93.184.216.34']; } }); await assert.rejects(() => provider.preview({ id: 'aborted', name: 'browser.session_start', arguments: { url: 'https://example.com/' }, signal: controller.signal }), error => error.code === 'provider_cancelled'); assert.equal(resolved, false);
});

test('CDP close rejects pending commands and load waiters directly', async () => {
  const client = new CdpClient({ port: 43123 }); client.socket = { send() {}, close() {} }; const command = client.send('Page.enable', {}); const load = client.waitLoad(); client.close(); await assert.rejects(command, error => error.code === 'provider_cancelled'); await assert.rejects(load, error => error.code === 'provider_cancelled'); assert.equal(client.pending.size, 0); assert.equal(client.eventWaiters.length, 0);
});

test('CDP endpoint lookup forbids redirects and startup observes child failure', async () => {
  let options; const client = new CdpClient({ port: 43123, WebSocketImpl: function FakeWebSocket() {}, fetchImpl: async (_url, requestOptions) => { options = requestOptions; let read = false; return { ok: true, headers: { get: () => null }, body: { getReader: () => ({ read: async () => read ? ({ done: true }) : (read = true, { value: Buffer.from('[]'), done: false }), cancel: async () => {}, releaseLock: () => {} }) } }; } }); await assert.rejects(() => client.connect(), error => error.code === 'provider_unconfigured'); assert.equal(options.redirect, 'error');
  const directory = await mkdtemp(join(tmpdir(), 'lae-browser-')); const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], mkdtempImpl: async () => directory, rmImpl: async () => {}, proxyFactory: async () => ({ port: 43127, close: async () => {} }), spawn: () => { const child = new EventEmitter(); queueMicrotask(() => child.emit('error', new Error('spawn failed'))); return child; }, waitDevtoolsPortImpl: async () => new Promise(() => {}), resolve: async () => ['93.184.216.34'] }); const tool = createBrowserActionTools(provider)['browser.session_start']; const request = call('browser.session_start', { url: 'https://example.com/' }, 'browser_start_failure'); await tool.preview(request); const failure = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(failure.code, 'provider_failed');
});

test('browser child exit closes the owned session and temporary proxy', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'lae-browser-')); let child; let removed = false; let proxyClosed = false; const cdp = { async connect() {}, async navigate() {}, async inspect() { return { url: 'https://example.com/', title: '', links: [] }; }, close() {} }; const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], mkdtempImpl: async () => directory, rmImpl: async () => { removed = true; }, spawn: () => { child = new EventEmitter(); child.kill = () => {}; return child; }, proxyFactory: async () => ({ port: 43123, close: async () => { proxyClosed = true; } }), waitDevtoolsPortImpl: async () => ({ port: 43124 }), resolve: async () => ['93.184.216.34'], cdpFactory: async () => cdp }); const tools = createBrowserActionTools(provider); const start = call('browser.session_start', { url: 'https://example.com/' }, 'browser_exit'); await tools['browser.session_start'].preview(start); const started = value(await tools['browser.session_start'].execute({ ...start, authorization: { kind: 'user_confirmation' } })); child.emit('exit', 1); await new Promise(resolve => setImmediate(resolve)); const stale = value(await tools['browser.inspect_links'].execute(call('browser.inspect_links', { browser_session_id: started.browser_session_id }, 'browser_exit_inspect'))); assert.equal(stale.code, 'browser_session_stale'); assert.equal(removed, true); assert.equal(proxyClosed, true);
});

test('browser action startup rejects an unsafe redirect before recording a session', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'lae-browser-')); let killed = false; let removed = false; const child = new EventEmitter(); child.kill = () => { killed = true; }; const provider = new BrowserActionProvider({ enabled: true, executable: '/approved/chrome', allowlist: ['/approved/chrome'], mkdtempImpl: async () => directory, rmImpl: async () => { removed = true; }, spawn: () => child, proxyFactory: async () => ({ port: 43123, close: async () => {} }), waitDevtoolsPortImpl: async () => ({ port: 43124 }), resolve: async host => host === 'private.example' ? ['192.168.1.1'] : ['93.184.216.34'], cdpFactory: async () => ({ async connect() {}, async navigate() {}, async inspect() { return { url: 'https://private.example/', title: '', links: [] }; }, close() {} }) }); const tools = createBrowserActionTools(provider); const start = call('browser.session_start', { url: 'https://example.com/' }, 'browser_redirect'); await tools['browser.session_start'].preview(start); const rejected = value(await tools['browser.session_start'].execute({ ...start, authorization: { kind: 'user_confirmation' } })); assert.equal(rejected.code, 'provider_destination_rejected'); assert.equal(killed, true); assert.equal(removed, true);
});

class FakeChild extends EventEmitter {
  constructor({ finish = true } = {}) { super(); this.stdout = new EventEmitter(); this.stderr = new EventEmitter(); this.input = ''; this.finish = finish; this.stdin = { end: (text) => { this.input = text; if (this.finish) queueMicrotask(() => { this.stdout.emit('data', Buffer.from('copilot response')); this.emit('close', 0); }); } }; }
  kill() { this.emit('close', null); }
}

test('Copilot bridge uses stdin/minimal environment and exposes explicit cloud-egress preview', async () => {
  const children = []; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3', versionCheck: async () => true, environment: { PATH: '/approved', SECRET_TOKEN: 'must-not-pass' }, readContext: Object.assign(async () => 'const x = 1;', { estimate: async () => 12 }), spawn: (executable, args, options) => { const child = new FakeChild(); children.push({ executable, args, options, child }); return child; } }); const tool = createCopilotTool(provider); assert.equal(tool.input_schema.additionalProperties, false); const copilotCall = call('coding.copilot_ask', { prompt: 'Explain this', workspace_id: 'project', context_paths: ['src/index.js'] }, 'call_copilot'); const preview = await tool.preview(copilotCall); assert.equal(preview.destination, 'GitHub Copilot cloud'); assert.equal(preview.egress_bytes, 44); const output = value(await tool.execute({ ...copilotCall, authorization: { kind: 'user_confirmation' } })); assert.equal(output.stdout, 'copilot response'); assert.equal(output.cli_version, '1.2.3'); assert.equal(children[0].executable, '/approved/copilot'); assert.deepEqual(children[0].args, ['-s', '--no-auto-update', '--no-color', '--no-custom-instructions', '--no-experimental', '--no-remote', '--no-remote-export', '--no-ask-user', '--disable-builtin-mcps', '--disallow-temp-dir', '--log-level=none', '--available-tools=']); assert.equal(children[0].options.shell, false); assert.equal(children[0].options.detached, true); assert.deepEqual(children[0].options.env, { PATH: '/approved' }); assert.match(children[0].child.input, /const x = 1/);
  await assert.rejects(() => tool.preview(call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: ['..\\secret'] }, 'call_path')), error => error.code === 'invalid_tool_arguments');
  const disabled = createCopilotTool(); const disabledCall = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_disabled'); await disabled.preview(disabledCall); const failure = value(await disabled.execute({ ...disabledCall, authorization: { kind: 'user_confirmation' } })); assert.equal(failure.code, 'copilot_cli_unavailable');
});

test('Copilot production ACP framing never puts prompt in argv and cancels permission requests', async () => {
  let launched; const acpRequests = []; const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.exitCode = null; child.stdin = { write(line) { const request = JSON.parse(line); acpRequests.push(request); queueMicrotask(() => { if (request.method === 'initialize') child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { protocolVersion: 1 } })}\n`)); else if (request.method === 'session/new') child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { sessionId: 'session-1' } })}\n`)); else if (request.method === 'session/prompt') { child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', method: 'session/update', params: { sessionId: 'session-1', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'ACP answer' } } } })}\n`)); child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { stopReason: 'end_turn' } })}\n`)); child.emit('close', 0); } }); return true; }, end() {} }; child.kill = () => child.emit('close', null);
  const provider = new CopilotCliProvider({ protocol: 'acp', enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], version: '1.2.3', versionCheck: async () => true, readContext: async () => ({ text: '', files: [] }), environment: { PATH: '/safe', SECRET_TOKEN: 'nope' }, spawn: (...args) => { launched = args; return child; } }); const tool = createCopilotTool(provider); const request = call('coding.copilot_ask', { prompt: 'do not leak', workspace_id: 'project', context_paths: [] }, 'call_acp'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.stdout, 'ACP answer'); assert.deepEqual(launched[1].slice(0, 2), ['--acp', '--stdio']); assert.equal(launched[1].includes('--prompt'), false); assert.equal(JSON.stringify(launched[1]).includes('do not leak'), false); assert.equal(launched[2].env.PATH, '/safe'); assert.equal(launched[2].env.SECRET_TOKEN, undefined); assert.deepEqual(acpRequests[0].params.clientCapabilities, {}); assert.equal(acpRequests[1].params.cwd.startsWith('/'), true); assert.deepEqual(acpRequests[1].params.mcpServers, []);
});

test('Copilot ACP rejects empty or control-character session IDs', async () => {
  for (const [index, invalidSessionId] of ['', 'bad\nid', 'bad\u0000id'].entries()) {
    const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.exitCode = null;
    child.stdin = { write(line) { const request = JSON.parse(line); queueMicrotask(() => {
      const result = request.method === 'initialize' ? { protocolVersion: 1 } : { sessionId: invalidSessionId };
      child.stdout.emit('data', Buffer.from(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result })}\n`));
    }); return true; }, end() {} }; child.kill = () => {};
    const provider = new CopilotCliProvider({ protocol: 'acp', enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, spawn: () => child });
    const tool = createCopilotTool(provider); const request = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, `call_bad_session_${index}`);
    await tool.preview(request);
    assert.equal(value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })).code, 'provider_failed');
  }
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

test('Copilot version probe is exact, bounded, cancellable, and minimally scoped', async () => {
  const launches = [];
  const check = createCopilotVersionCheck({ expectedVersion: '1.2.3', environment: { PATH: '/safe', SystemRoot: 'C:\\Windows', SECRET_TOKEN: 'no' }, spawn: (executable, args, options) => { const child = new FakeChild({ finish: false }); child.pid = 41; launches.push({ executable, args, options, child }); queueMicrotask(() => { child.stdout.emit('data', Buffer.from('GitHub Copilot CLI v1.2.3\n')); child.exitCode = 0; child.emit('close', 0); }); return child; } });
  assert.equal(await check('C:\\Tools\\copilot.exe'), true);
  assert.deepEqual(launches[0].args, ['--no-auto-update', '--no-color', 'version']);
  assert.deepEqual(launches[0].options.env, { PATH: '/safe', SystemRoot: 'C:\\Windows' });
  assert.throws(() => createCopilotVersionCheck({ expectedVersion: 'latest' }), /exact semantic version/);
  const mismatch = createCopilotVersionCheck({ expectedVersion: '1.2.3', spawn: () => { const child = new FakeChild({ finish: false }); queueMicrotask(() => { child.stdout.emit('data', Buffer.from('10.1.2.30')); child.exitCode = 0; child.emit('close', 0); }); return child; } });
  assert.equal(await mismatch('/copilot'), false);
  const controller = new AbortController(); controller.abort(); await assert.rejects(() => check('/copilot', controller.signal), error => error.code === 'provider_cancelled');
});

test('Copilot dispatch is at-most-once across concurrent and timed-out retries', async () => {
  let launches = 0; let killed = 0;
  const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => '', timeoutMs: 100, killProcess: async () => { killed += 1; }, spawn: () => { launches += 1; return new FakeChild({ finish: false }); } });
  const tool = createCopilotTool(provider); const request = call('coding.copilot_ask', { prompt: 'bounded', workspace_id: 'project', context_paths: [] }, 'call_once_copilot'); await tool.preview(request); const authorized = { ...request, authorization: { kind: 'user_confirmation' } }; const first = tool.execute(authorized); await new Promise(resolve => setImmediate(resolve)); const concurrent = value(await tool.execute(authorized)); assert.equal(concurrent.code, 'provider_request_already_attempted'); const timedOut = value(await first); assert.equal(timedOut.code, 'provider_timeout'); const replay = value(await tool.execute(authorized)); assert.equal(replay.code, 'provider_timeout'); assert.equal(launches, 1); assert.equal(killed, 1);
});

test('Copilot output overflow terminates the process and fails closed', async () => {
  let child; let killed = 0; const provider = new CopilotCliProvider({ enabled: true, executable: '/approved/copilot', allowlist: ['/approved/copilot'], versionCheck: async () => true, readContext: async () => '', maxOutput: 1024, killProcess: async () => { killed += 1; }, spawn: () => { child = new FakeChild({ finish: false }); queueMicrotask(() => child.stdout.emit('data', Buffer.alloc(1025, 65))); return child; } }); const tool = createCopilotTool(provider); const request = call('coding.copilot_ask', { prompt: 'x', workspace_id: 'project', context_paths: [] }, 'call_overflow'); await tool.preview(request); const output = value(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } })); assert.equal(output.code, 'provider_response_too_large'); assert.equal(killed, 1);
});

test('Copilot Windows tree termination uses fixed taskkill argv without a shell', async () => {
  const calls = []; const child = new FakeChild({ finish: false }); child.pid = 4321; child.exitCode = null; child.signalCode = null;
  await killCopilotProcessTree(child, { platform: 'win32', graceMs: 100, spawn: (executable, args, options) => { calls.push({ executable, args, options }); const killer = new FakeChild({ finish: false }); killer.exitCode = 0; queueMicrotask(() => { child.exitCode = 1; child.emit('close', 1); killer.emit('close', 0); }); return killer; } });
  assert.deepEqual(calls[0].args, ['/PID', '4321', '/T', '/F']); assert.equal(calls[0].executable, 'taskkill.exe'); assert.equal(calls[0].options.shell, false);
});

test('Copilot provider rejects unsafe lifecycle bounds', () => {
  assert.throws(() => new ProductionCopilotCliProvider({ protocol: 'legacy_stdin' }), /test gate/);
  assert.throws(() => new ProductionCopilotCliProvider({ protocol: 'acp', readContext: async () => '' }), /test gate/);
  assert.throws(() => new CopilotCliProvider({ timeoutMs: NaN }), /invalid Copilot timeout/);
  assert.throws(() => new CopilotCliProvider({ timeoutMs: 99 }), /invalid Copilot timeout/);
  assert.throws(() => new CopilotCliProvider({ maxOutput: 1023 }), /invalid Copilot output limit/);
  assert.throws(() => new CopilotCliProvider({ maxOutput: 65537 }), /invalid Copilot output limit/);
});
