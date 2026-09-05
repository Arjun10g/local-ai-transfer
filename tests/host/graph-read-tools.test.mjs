import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationController, requiresDurableAction } from '../../host/agent/controller.mjs';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { MicrosoftGraphProvider, MicrosoftGraphHttpsTransport, createMicrosoftGraphTools, graphDefinitions } from '../../host/providers/microsoft-graph.mjs';
import { createExternalToolRegistry } from '../../host/providers/index.mjs';

const call = (name, arguments_, id = `call_${name.replaceAll('.', '_')}`) => ({ id, name, arguments: arguments_ });
const payload = output => JSON.parse(output.content[0].text);
const credentialSource = { getAccessToken: async () => 'synthetic-token' };
const provider = transport => new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource, transport, accountFingerprint: 'acct-read', testOnly: true });
const mail = (id = 'mail-1') => ({ id, receivedDateTime: '2026-09-05T12:34:56.123Z', from: { emailAddress: { name: 'Sender', address: 'sender@example.com' } }, subject: 'Status', isRead: false, importance: 'normal', bodyPreview: '<b>Preview &amp; safe</b>' });
const mailDetail = (content = '<p>Body &amp; detail</p>') => { const value = mail(); delete value.bodyPreview; return { ...value, body: { contentType: 'html', content } }; };
const teamMessage = (id = 'message-1', text = '<p>Hello <b>team</b></p>') => ({ id, replyToId: null, createdDateTime: '2026-09-05T12:34:56Z', lastModifiedDateTime: '2026-09-05T12:35:00.123456789012Z', importance: 'normal', from: { user: { id: 'user-1', displayName: 'Colleague' } }, body: { contentType: 'html', content: text } });
const collection = (values, nextLink) => ({ value: values, ...(nextLink ? { '@odata.nextLink': nextLink } : {}) });

test('Graph read catalog is complete, least-scoped, strict, and not configured by default', () => {
  const expected = {
    'mail.list_messages': ['Mail.Read'], 'mail.search_messages': ['Mail.Read'], 'mail.read_message': ['Mail.Read'],
    'teams.list_chats': ['Chat.Read'], 'teams.list_messages': ['Chat.Read'], 'teams.read_message': ['Chat.Read'],
    'teams.list_channels': ['Channel.ReadBasic.All'], 'teams.list_channel_messages': ['ChannelMessage.Read.All'], 'teams.read_channel_message': ['ChannelMessage.Read.All']
  };
  for (const [name, scopes] of Object.entries(expected)) {
    assert.deepEqual(graphDefinitions[name].required_scopes, scopes);
    const outbound = name === 'mail.search_messages';
    assert.equal(graphDefinitions[name].risk_tier, outbound ? 'T2' : 'T1');
    assert.equal(graphDefinitions[name].side_effect, outbound ? 'external_query' : name.startsWith('mail.') ? 'read_mail' : 'read_teams');
    assert.equal(graphDefinitions[name].data_egress, outbound ? 'search_query' : 'none');
    assert.equal(graphDefinitions[name].requires_confirmation, outbound);
    assert.equal(requiresDurableAction(graphDefinitions[name]), false);
    assert.equal(graphDefinitions[name].parameters.additionalProperties, false);
  }
  const registry = createExternalToolRegistry();
  for (const name of Object.keys(expected)) assert.equal(registry[name], undefined);
  assert.equal(registry.providerStatus().microsoft_graph, 'disabled');
});

test('configured Graph catalog advertises only tools covered by the explicit delegated scopes', () => {
  const clientId = '00001111-aaaa-2222-bbbb-3333cccc4444'; const transport = { request: async () => { throw new Error('must not authenticate'); } };
  const mailChat = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read', 'Mail.Read', 'Chat.Read'], transport });
  assert.deepEqual([...mailChat.configuredToolNames()].sort(), ['mail.list_messages', 'mail.read_message', 'mail.search_messages', 'teams.list_chats', 'teams.list_messages', 'teams.read_message'].sort());
  const channels = new MicrosoftGraphProvider({ enabled: true, tenant: 'organizations', clientId, scopes: ['User.Read', 'Channel.ReadBasic.All', 'ChannelMessage.Read.All'], transport });
  assert.deepEqual([...channels.configuredToolNames()].sort(), ['teams.list_channel_messages', 'teams.list_channels', 'teams.read_channel_message'].sort());
});

test('Graph read arguments reject ambiguous IDs, OData fragments, unsafe search, and cursor mixing before token access', async () => {
  let tokenReads = 0; const tools = createMicrosoftGraphTools({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource: { getAccessToken: async () => { tokenReads += 1; return 'token'; } }, transport: { request: async () => { throw new Error('must not request'); } }, testOnly: true });
  const cases = [
    ['mail.read_message', { message_id: '../messages/other' }],
    ['teams.list_messages', { chat_id: 'chat/other' }],
    ['teams.read_channel_message', { team_id: 'team?x', channel_id: 'channel', message_id: 'message' }],
    ['mail.search_messages', { query: '" OR from:anyone' }],
    ['mail.search_messages', { query: 'hidden\u202equery' }],
    ['teams.list_messages', { chat_id: 'chat-1', search_text: 'hidden\u0085query' }],
    ['mail.list_messages', { page_cursor: `gpg_${'a'.repeat(32)}`, limit: 1 }],
    ['teams.list_channels', { limit: 1 }]
  ];
  for (const [name, args] of cases) await assert.rejects(() => tools[name].execute(call(name, args)), error => error.code === 'invalid_tool_arguments');
  assert.equal(tokenReads, 0);
});

test('Outlook list, search, and detail use fixed /me paths and safe bounded projections', async () => {
  const requests = [];
  const tools = createMicrosoftGraphTools(provider({ request: async request => {
    requests.push(request);
    if (request.path.includes('/messages/mail-1')) return { status: 200, body: mailDetail('<script>drop()</script><style>x{}</style><p>Body &amp; detail</p>') };
    return { status: 200, body: collection([mail()]) };
  } }));
  const listed = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { folder: 'archive', unread_only: true, limit: 1 })));
  assert.equal(listed.messages[0].preview, 'Preview & safe'); assert.equal(listed.source_untrusted, true); assert.equal(listed.next_page, null);
  const searchCall = call('mail.search_messages', { query: 'quarterly status', folder: 'sentitems', limit: 1 });
  await tools['mail.search_messages'].preview(searchCall);
  const searched = payload(await tools['mail.search_messages'].execute({ ...searchCall, authorization: { kind: 'user_confirmation' } }));
  assert.equal(searched.kind, 'mail_search_page');
  const detail = payload(await tools['mail.read_message'].execute(call('mail.read_message', { message_id: 'mail-1', max_bytes: 32 })));
  assert.equal(detail.message.text, 'Body & detail'); assert.equal(JSON.stringify(detail).includes('drop()'), false);
  assert.deepEqual(requests.map(item => item.path), ['/v1.0/me/mailFolders/archive/messages', '/v1.0/me/mailFolders/sentitems/messages', '/v1.0/me/messages/mail-1']);
  assert.equal(requests[0].query.$filter, undefined); assert.equal(requests[0].query.$top, 25); assert.equal(requests[1].query.$search, '"quarterly status"');
  assert.equal(requests[2].headers.Prefer, 'outlook.body-content-type="text"');
});

test('outbound Outlook search cannot bypass preview and confirmation under ask-before-writes', async () => {
  let requests = 0;
  const graph = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource, accountFingerprint: 'acct-read', testOnly: true, transport: { request: async request => {
    requests += 1;
    if (requests !== 1) return { status: 200, body: collection([mail(`mail-${requests}`)]) };
    const next = new URL(`https://graph.microsoft.com${request.path}`);
    for (const [key, value] of Object.entries(request.query)) next.searchParams.set(key, String(value));
    next.searchParams.set('$skiptoken', 'next-search-page');
    return { status: 200, body: collection([mail()], next.href) };
  } } });
  const tool = createMicrosoftGraphTools(graph)['mail.search_messages'];
  const request = call('mail.search_messages', { query: 'quarterly status', folder: 'sentitems', limit: 1 }, 'call_search_guard');
  assert.equal(graph.confirmationRequired('mail.search_messages'), true);
  assert.equal(payload(await tool.execute(request)).code, 'provider_permission_insufficient');
  const preview = await tool.preview(request);
  assert.deepEqual({ action: preview.action, destination: preview.destination, query: preview.query, data_categories: preview.data_categories }, { action: 'search_messages', destination: '/me/mailFolders/sentitems/messages', query: 'quarterly status', data_categories: ['search_query'] });
  assert.equal(payload(await tool.execute(request)).code, 'provider_permission_insufficient');
  assert.equal(payload(await tool.execute({ ...request, authorization: { kind: 'policy' } })).code, 'provider_permission_insufficient');
  assert.equal(requests, 0);
  const firstPage = payload(await tool.execute({ ...request, authorization: { kind: 'user_confirmation' } }));
  assert.equal(firstPage.kind, 'mail_search_page'); assert.match(firstPage.next_page, /^gpg_[0-9a-f]{32}$/u);
  assert.equal(requests, 1);

  const nextRequest = call('mail.search_messages', { page_cursor: firstPage.next_page }, 'call_search_guard_page_2');
  assert.equal(payload(await tool.execute(nextRequest)).code, 'provider_permission_insufficient');
  const nextPreview = await tool.preview(nextRequest);
  assert.equal(nextPreview.query, 'quarterly status'); assert.equal(nextPreview.destination, '/me/mailFolders/sentitems/messages');
  assert.equal(payload(await tool.execute(nextRequest)).code, 'provider_permission_insufficient'); assert.equal(requests, 1);
  assert.equal(payload(await tool.execute({ ...nextRequest, authorization: { kind: 'user_confirmation' } })).kind, 'mail_search_page');
  assert.equal(requests, 2);

  let deniedRequests = 0; let deniedController;
  const deniedTools = createMicrosoftGraphTools(new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource, accountFingerprint: 'acct-read', testOnly: true, transport: { request: async () => { deniedRequests += 1; return { status: 200, body: collection([mail()]) }; } } }));
  let turn = 0; const events = [];
  const engine = { async *generate() { if (turn++ === 0) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('mail.search_messages', { query: 'private phrase' }, 'call_search_denied')) }; return; } yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; } };
  deniedController = new ConversationController({ engine, toolRegistry: deniedTools });
  const denied = await deniedController.runTurn({ sessionId: 'session_search_deny', requestId: 'request_search_deny', message: 'search', onEvent: event => { events.push(event); if (event.event === 'tool.confirmation_required') queueMicrotask(() => deniedController.confirm(event.data.confirmation_id, false, { requestId: event.request_id, callId: event.data.call.id })); } });
  assert.equal(denied.state, 'COMPLETED'); assert.equal(deniedRequests, 0);
  const confirmation = events.find(event => event.event === 'tool.confirmation_required'); assert.equal(confirmation.data.risk_tier, 'T2'); assert.equal(confirmation.data.preview.query, 'private phrase');
});

test('Teams chat and channel list/detail paths bind every identity and expose only safe text', async () => {
  const requests = [];
  const tools = createMicrosoftGraphTools(provider({ request: async request => {
    requests.push(request);
    if (request.path === '/v1.0/me/chats') return { status: 200, body: collection([{ id: 'chat-1', topic: null, chatType: 'group', lastUpdatedDateTime: '2026-09-05T12:00:00Z' }]) };
    if (request.path.endsWith('/channels')) return { status: 200, body: collection([{ id: 'channel-1', displayName: 'General', description: '<b>Team</b>', membershipType: 'standard' }]) };
    if (request.path.endsWith('/messages')) return { status: 200, body: collection([teamMessage()]) };
    return { status: 200, body: teamMessage() };
  } }));
  assert.equal(payload(await tools['teams.list_chats'].execute(call('teams.list_chats', {}))).chats[0].type, 'group');
  assert.equal(payload(await tools['teams.list_messages'].execute(call('teams.list_messages', { chat_id: 'chat-1' }))).messages[0].text, 'Hello team');
  assert.equal(payload(await tools['teams.read_message'].execute(call('teams.read_message', { chat_id: 'chat-1', message_id: 'message-1' }))).message.sender.name, 'Colleague');
  assert.equal(payload(await tools['teams.list_channels'].execute(call('teams.list_channels', { team_id: 'team-1' }))).channels[0].description, 'Team');
  assert.equal(payload(await tools['teams.list_channel_messages'].execute(call('teams.list_channel_messages', { team_id: 'team-1', channel_id: 'channel-1' }))).messages[0].text, 'Hello team');
  assert.equal(payload(await tools['teams.read_channel_message'].execute(call('teams.read_channel_message', { team_id: 'team-1', channel_id: 'channel-1', message_id: 'message-1' }))).message.id, 'message-1');
  assert.deepEqual(requests.map(request => request.path), ['/v1.0/me/chats', '/v1.0/chats/chat-1/messages', '/v1.0/chats/chat-1/messages/message-1', '/v1.0/teams/team-1/channels', '/v1.0/teams/team-1/channels/channel-1/messages', '/v1.0/teams/team-1/channels/channel-1/messages/message-1']);
  assert.deepEqual(requests.map(request => request.method), Array(6).fill('GET'));
});

test('Teams bounded search is explicitly page-local and does not alter the fixed Graph query', async () => {
  let observed;
  const tools = createMicrosoftGraphTools(provider({ request: async request => { observed = request; return { status: 200, body: collection([teamMessage('one', 'Alpha'), teamMessage('two', 'Beta alpha'), teamMessage('three', 'Gamma')]) }; } }));
  const output = payload(await tools['teams.list_messages'].execute(call('teams.list_messages', { chat_id: 'chat-1', search_text: 'alpha', limit: 3 })));
  assert.deepEqual(output.messages.map(item => item.id), ['one', 'two']); assert.equal(output.search_scope, 'current_graph_page');
  assert.deepEqual(observed.query, { '$top': 50, '$select': 'id,replyToId,createdDateTime,lastModifiedDateTime,importance,from,body' });
});

test('opaque pagination accepts only exact Graph origin/path/static query and is one-use/account-bound', async () => {
  const requests = []; let page = 0;
  const graph = provider({ request: async request => { requests.push(request); page += 1; const next = new URL(request.path, 'https://graph.microsoft.com'); for (const [key, value] of Object.entries(request.query)) next.searchParams.set(key, String(value)); next.searchParams.set('$skiptoken', 'opaque-next'); return { status: 200, body: collection([mail(`mail-${page}`)], page === 1 ? next.toString() : null) }; } });
  const tools = createMicrosoftGraphTools(graph);
  const first = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { folder: 'inbox', limit: 1 })));
  assert.match(first.next_page, /^gpg_[a-f0-9]{32}$/u); assert.equal(JSON.stringify(first).includes('skiptoken'), false);
  const second = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: first.next_page })));
  assert.equal(second.messages[0].id, 'mail-2'); assert.equal(requests[1].query.$skiptoken, 'opaque-next');
  const replay = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: first.next_page })));
  assert.equal(replay.code, 'provider_invalid_request');

  let account = 'account-a'; let issued = true;
  const changing = new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', accountFingerprint: 'account-a', credentialSource, testOnly: true, transport: { request: async request => { const next = new URL(request.path, 'https://graph.microsoft.com'); for (const [key, value] of Object.entries(request.query)) next.searchParams.set(key, String(value)); next.searchParams.set('$skip', '1'); return { status: 200, body: collection([mail()], issued ? next.toString() : null) }; } } });
  changing.readBoundary.accountFingerprint = () => account;
  const changingTools = createMicrosoftGraphTools(changing); const cursor = payload(await changingTools['mail.list_messages'].execute(call('mail.list_messages', {}))).next_page;
  account = 'account-b'; issued = false;
  assert.equal(payload(await changingTools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: cursor }))).code, 'provider_invalid_request');
});

test('caller limit retains every sanitized provider item before advancing Graph pagination', async () => {
  let requests = 0;
  const graph = provider({ request: async request => {
    requests += 1;
    if (requests === 1) {
      const next = new URL(request.path, 'https://graph.microsoft.com');
      for (const [key, value] of Object.entries(request.query)) next.searchParams.set(key, String(value));
      next.searchParams.set('$skiptoken', 'provider-page-2');
      return { status: 200, body: collection([mail('m1'), mail('m2'), mail('m3')], next.toString()) };
    }
    if (requests === 2) {
      assert.equal(request.query.$skiptoken, 'provider-page-2');
      const next = new URL(request.path, 'https://graph.microsoft.com');
      for (const [key, value] of Object.entries(request.query)) next.searchParams.set(key, String(value));
      next.searchParams.set('$skiptoken', 'provider-page-3');
      return { status: 200, body: collection([mail('m4')], next.toString()) };
    }
    assert.equal(request.query.$skiptoken, 'provider-page-3');
    return { status: 200, body: collection([mail('m5')]) };
  } });
  const tools = createMicrosoftGraphTools(graph);
  const first = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { limit: 1 }, 'limit_page_1')));
  const second = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: first.next_page }, 'limit_page_2')));
  const third = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: second.next_page }, 'limit_page_3')));
  assert.deepEqual([first.messages[0].id, second.messages[0].id, third.messages[0].id], ['m1', 'm2', 'm3']);
  assert.equal(requests, 1);
  const fourth = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: third.next_page }, 'limit_page_4')));
  assert.equal(fourth.messages[0].id, 'm4'); assert.match(fourth.next_page, /^gpg_[a-f0-9]{32}$/u); assert.equal(requests, 2);
  const fifth = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { page_cursor: fourth.next_page }, 'limit_page_5')));
  assert.equal(fifth.messages[0].id, 'm5'); assert.equal(fifth.next_page, null); assert.equal(requests, 3);
});

test('local filtering retains matching leftovers without skips and rejects provider cross-page duplicates', async () => {
  let requests = 0;
  const graph = provider({ request: async request => {
    requests += 1;
    if (requests === 1) {
      const next = new URL(request.path, 'https://graph.microsoft.com');
      for (const [key, value] of Object.entries(request.query)) next.searchParams.set(key, String(value));
      next.searchParams.set('$skiptoken', 'teams-page-2');
      return { status: 200, body: collection([teamMessage('m1', 'alpha one'), teamMessage('m2', 'beta'), teamMessage('m3', 'alpha three')], next.toString()) };
    }
    return { status: 200, body: collection([teamMessage('m3', 'alpha duplicate')]) };
  } });
  const tools = createMicrosoftGraphTools(graph);
  const first = payload(await tools['teams.list_messages'].execute(call('teams.list_messages', { chat_id: 'chat-1', search_text: 'alpha', limit: 1 }, 'filter_page_1')));
  const second = payload(await tools['teams.list_messages'].execute(call('teams.list_messages', { page_cursor: first.next_page }, 'filter_page_2')));
  assert.deepEqual([first.messages[0].id, second.messages[0].id], ['m1', 'm3']); assert.equal(requests, 1);
  const duplicate = payload(await tools['teams.list_messages'].execute(call('teams.list_messages', { page_cursor: second.next_page }, 'filter_page_3')));
  assert.equal(duplicate.code, 'provider_invalid_response'); assert.equal(requests, 2);
});

test('hostile nextLink origin, path, duplicate, unknown, missing, and static-query changes fail closed', async () => {
  const links = [
    'https://evil.example/v1.0/me/mailFolders/inbox/messages?%24top=1&%24select=id%2CreceivedDateTime%2Cfrom%2Csubject%2CisRead%2Cimportance%2CbodyPreview&%24skip=1',
    'https://graph.microsoft.com/v1.0/me/messages?%24top=1&%24select=id%2CreceivedDateTime%2Cfrom%2Csubject%2CisRead%2Cimportance%2CbodyPreview&%24skip=1',
    'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24top=1&%24top=1&%24select=id%2CreceivedDateTime%2Cfrom%2Csubject%2CisRead%2Cimportance%2CbodyPreview&%24skip=1',
    'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24top=1&%24select=id%2CreceivedDateTime%2Cfrom%2Csubject%2CisRead%2Cimportance%2CbodyPreview&next=https%3A%2F%2Fevil.example&%24skip=1',
    'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24top=2&%24select=id%2CreceivedDateTime%2Cfrom%2Csubject%2CisRead%2Cimportance%2CbodyPreview&%24skip=1',
    'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24top=1&%24select=id%2CreceivedDateTime%2Cfrom%2Csubject%2CisRead%2Cimportance%2CbodyPreview'
  ];
  for (const nextLink of links) {
    const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: collection([mail()], nextLink) }) }));
    const output = payload(await tools['mail.list_messages'].execute(call('mail.list_messages', { limit: 1 })));
    assert.equal(output.code, 'provider_invalid_response', nextLink);
  }
});

test('strict projections reject unknown/attachment fields, duplicate IDs, malformed dates/content, oversize items, and identity mixups', async () => {
  const cases = [
    { value: collection([{ ...mail(), attachments: [{ id: 'secret' }] }]) },
    { value: collection([mail(), mail()]) },
    { value: collection([{ ...mail(), receivedDateTime: '2026-02-30T00:00:00Z' }]) },
    { value: collection([{ ...mail(), subject: 'x'.repeat(999) }]) },
    { value: { ...teamMessage(), body: { contentType: 'rtf', content: 'x' } }, tool: 'teams.read_message', args: { chat_id: 'chat-1', message_id: 'message-1' } },
    { value: teamMessage('message-1', 'x'.repeat(65537)), tool: 'teams.read_message', args: { chat_id: 'chat-1', message_id: 'message-1', max_bytes: 60000 } },
    { value: teamMessage('other'), tool: 'teams.read_message', args: { chat_id: 'chat-1', message_id: 'message-1' } }
  ];
  for (const entry of cases) {
    const name = entry.tool ?? 'mail.list_messages'; const args = entry.args ?? {};
    const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: entry.value }) }));
    assert.equal(payload(await tools[name].execute(call(name, args))).code, 'provider_invalid_response');
  }
  const huge = Array.from({ length: 51 }, (_, index) => mail(`mail-${index}`));
  const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: collection(huge) }) }));
  assert.equal(payload(await tools['mail.list_messages'].execute(call('mail.list_messages', {}))).code, 'provider_invalid_response');
});

test('safe text removes encoded, nested, malformed, and unclosed active content before stripping introduced controls', async () => {
  const bodies = [
    ['before<script>alert(1)</script><style>secret{}</style>after', 'before after'],
    ['before<script><style>nested</style>secret', 'before'],
    ['before<scr<script>ipt>malformed</script>after', 'before after'],
    ['before &lt;script&gt;encoded&lt;/script&gt; after', 'before after'],
    ['before &amp;lt;style&amp;gt;double&amp;lt;/style&amp;gt; after', 'before after'],
    ['before &#x3c;script&#x3e;numeric&#x3c;/script&#x3e; after', 'before after'],
    ['before<style>unclosed', 'before'],
    ['A&#27;B&#x1b;C&#x85;D&#0;E', 'A B C D E']
  ];
  for (const [content, expected] of bodies) {
    const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: mailDetail(content) }) }));
    const result = payload(await tools['mail.read_message'].execute(call('mail.read_message', { message_id: 'mail-1' })));
    assert.equal(result.message.text, expected, content);
    assert.equal(/[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/u.test(result.message.text), false);
    assert.equal(/script|style|alert|secret|nested|malformed|encoded|double|numeric|unclosed/iu.test(result.message.text), false);
  }
});

test('quote-aware visible-text projection never exposes attributes or transformed control fields', async () => {
  const bodies = [
    ['<div title="hidden > attribute">visible</div>', 'visible'],
    ['before&lt;div data-note=&quot;hidden &gt; attribute&quot;&gt;visible&lt;/div&gt;after', 'before visible after'],
    ['<script data-note="hidden > attribute">secret()</script>safe', 'safe'],
    ["before<style data-note='hidden > attribute'>secret", 'before'],
    ['before<div title="hidden > attribute" after', 'before']
  ];
  for (const [content, expected] of bodies) {
    const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: mailDetail(content) }) }));
    const result = payload(await tools['mail.read_message'].execute(call('mail.read_message', { message_id: 'mail-1' })));
    assert.equal(result.message.text, expected, content);
    assert.equal(JSON.stringify(result).includes('hidden > attribute'), false, content);
    assert.equal(/[\p{Cc}\p{Cf}]/u.test(JSON.stringify(result)), false, content);
  }

  const tools = createMicrosoftGraphTools(provider({ request: async request => {
    if (request.path === '/v1.0/me/chats') return { status: 200, body: collection([{ id: 'chat-1', topic: 'Topic&#x202e;<b title="hidden > topic">Visible</b>', chatType: 'group', lastUpdatedDateTime: '2026-09-05T12:00:00Z' }]) };
    if (request.path.endsWith('/channels')) return { status: 200, body: collection([{ id: 'channel-1', displayName: 'Gen&#x85;eral<style>hidden</style>', description: 'Desc&#x1b;ription', membershipType: 'standard' }]) };
    if (request.path.endsWith('/messages')) return { status: 200, body: collection([{ ...teamMessage(), from: { user: { id: 'user-1', displayName: 'Coll&#x202e;eague<script>hidden</script>' } } }]) };
    return { status: 200, body: { ...mailDetail('safe'), subject: 'Sub&#x85;ject<script>hidden</script>', from: { emailAddress: { name: 'Send&#x202e;er', address: 'sender&#x1b;@example.com' } } } };
  } }));
  const chat = payload(await tools['teams.list_chats'].execute(call('teams.list_chats', {})));
  const channel = payload(await tools['teams.list_channels'].execute(call('teams.list_channels', { team_id: 'team-1' })));
  const team = payload(await tools['teams.list_messages'].execute(call('teams.list_messages', { chat_id: 'chat-1' })));
  const detail = payload(await tools['mail.read_message'].execute(call('mail.read_message', { message_id: 'mail-1' })));
  const visible = JSON.stringify({ chat, channel, team, detail });
  assert.equal(/[\p{Cc}\p{Cf}]/u.test(visible), false);
  assert.equal(visible.includes('hidden'), false);
  assert.equal(chat.chats[0].topic, 'Topic Visible');
  assert.equal(channel.channels[0].name, 'Gen eral');
  assert.equal(team.messages[0].sender.name, 'Coll eague');
  assert.equal(detail.message.subject, 'Sub ject');
  assert.deepEqual(detail.message.from, { name: 'Send er', address: 'sender @example.com' });
});

test('Graph HTTPS fake-fetch rejects duplicate JSON, invalid UTF-8, wrong content type, and oversized bodies', async () => {
  const bodies = [
    new Response('{"value":[],"value":[]}', { status: 200, headers: { 'content-type': 'application/json' } }),
    new Response(Uint8Array.from([0xc3, 0x28]), { status: 200, headers: { 'content-type': 'application/json' } }),
    new Response('{"value":[]}', { status: 200, headers: { 'content-type': 'text/html' } }),
    new Response('{"value":[]}', { status: 200, headers: { 'content-type': 'application/json', 'content-length': String(256 * 1024 + 1) } })
  ];
  for (const response of bodies) {
    const transport = new MicrosoftGraphHttpsTransport({ fetchImpl: async () => response });
    await assert.rejects(() => transport.request({ origin: 'https://graph.microsoft.com', method: 'GET', path: '/v1.0/me/chats' }), error => ['provider_invalid_response', 'provider_response_too_large'].includes(error.code));
  }
});

test('Graph read failures are finite for 401, 403, 429, timeout, and cancellation', async () => {
  for (const [status, code] of [[401, 'provider_unauthorized'], [403, 'provider_permission_insufficient'], [429, 'provider_rate_limited']]) {
    const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status, body: {} }) }));
    assert.equal(payload(await tools['mail.list_messages'].execute(call('mail.list_messages', {}))).code, code);
  }
  const timeoutTools = createMicrosoftGraphTools(new MicrosoftGraphProvider({ enabled: true, permissionProfile: 'ask_before_writes', credentialSource, accountFingerprint: 'acct', requestTimeoutMs: 100, testOnly: true, transport: { request: async () => await new Promise(() => {}) } }));
  assert.equal(payload(await timeoutTools['mail.list_messages'].execute(call('mail.list_messages', {}))).code, 'provider_timeout');
  const controller = new AbortController(); controller.abort();
  const cancelled = createMicrosoftGraphTools(provider({ request: async () => { throw new Error('must not run'); } }));
  assert.equal(payload(await cancelled['mail.list_messages'].execute({ ...call('mail.list_messages', {}), signal: controller.signal })).code, 'provider_cancelled');
});

async function controllerRun(toolRegistry, toolName = 'mail.read_message', args = { message_id: 'mail-1' }) {
  let turn = 0; const seen = []; const events = [];
  const engine = { async *generate(input) { seen.push(structuredClone(input.messages)); if (turn++ === 0) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call(toolName, args, 'call_controller_read')) }; return; } yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done' }; } };
  const controller = new ConversationController({ engine, toolRegistry });
  const output = await controller.runTurn({ sessionId: 'session_read01', requestId: 'request_read01', message: 'read it', onEvent: event => events.push(event) });
  return { controller, events, output, seen };
}

test('controller exposes only exact adapter-attested safe Graph read payloads', async () => {
  const tools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: mailDetail('<script>secret()</script><p>Safe text</p>') }) }));
  const run = await controllerRun(tools);
  assert.equal(run.output.state, 'COMPLETED');
  const completed = run.events.find(event => event.event === 'tool.completed').data.result;
  const body = payload(completed); assert.equal(body.message.text, 'Safe text'); assert.equal(body.source_untrusted, true); assert.equal(JSON.stringify(run).includes('secret()'), false);
});

test('controller replaces forged, cloned, or post-attestation-mutated Graph read payloads with one fixed summary', async () => {
  const definition = graphDefinitions['mail.read_message'];
  const arbitrary = { ...definition, execute: callValue => makeToolResult({ id: callValue.id, name: callValue.name, text: JSON.stringify({ message: { id: 'forged', text: 'provider secret' }, operation_id: 'private' }) }) };
  const forged = await controllerRun({ 'mail.read_message': arbitrary });
  assert.deepEqual(payload(forged.events.find(event => event.event === 'tool.completed').data.result), { provider: 'microsoft_graph', state: 'unverified', code: 'provider_read_unverified' });
  assert.equal(JSON.stringify(forged).includes('provider secret'), false); assert.equal(JSON.stringify(forged).includes('operation_id'), false);

  const genuineTools = createMicrosoftGraphTools(provider({ request: async () => ({ status: 200, body: mailDetail('genuine') }) }));
  const original = genuineTools['mail.read_message'].execute;
  genuineTools['mail.read_message'].execute = async value => { const output = await original(value); output.content[0].text = JSON.stringify({ message: { id: 'mutated', text: 'leak' } }); return output; };
  const mutated = await controllerRun(genuineTools);
  assert.deepEqual(payload(mutated.events.find(event => event.event === 'tool.completed').data.result), { provider: 'microsoft_graph', state: 'unverified', code: 'provider_read_unverified' });
  assert.equal(JSON.stringify(mutated).includes('leak'), false);
});
