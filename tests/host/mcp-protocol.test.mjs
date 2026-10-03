// MCP protocol conformance for the BMO bridge, both protocol generations.
// Test titles carry the checklist ids from docs/research/COPILOT_MCP_COMPATIBILITY.md §8
// in [brackets]; tests/host/mcp-checklist.test.mjs audits that every id is covered.

import test from 'node:test';
import assert from 'node:assert/strict';
import { CopilotCliLikeClient, LineClient, VsCodeLikeClient, startBridge, startFakeHost, structured, MODERN_META } from './mcp-fake-host.mjs';
import { SERVER_INFO, SUPPORTED_VERSIONS } from '../../host/mcp/server.mjs';
import { TOOL_DEFINITIONS } from '../../host/mcp/tools.mjs';

async function hostFor(t, options) { const host = await startFakeHost(options); t.after(() => host.close()); return host; }

test('[C3] initialize echoes each supported legacy version and answers 2025-11-25 for unknown ones', async t => {
  for (const [requested, expected] of [['2025-11-25', '2025-11-25'], ['2025-06-18', '2025-06-18'], ['2025-03-26', '2025-03-26'], ['2024-11-05', '2025-11-25'], ['2026-07-28', '2025-11-25'], [undefined, '2025-11-25']]) {
    const { client } = startBridge(t, { ClientClass: VsCodeLikeClient });
    const reply = await client.request('initialize', { protocolVersion: requested, capabilities: {}, clientInfo: { name: 'x', version: '1' } });
    assert.equal(reply.result.protocolVersion, expected, `requested ${requested}`);
    assert.deepEqual(reply.result.capabilities, { tools: { listChanged: false } });
    assert.equal(reply.result.serverInfo.name, 'bmo');
    assert.equal(typeof reply.result.instructions, 'string');
    assert.equal(reply.result.resultType, undefined);
  }
});

test('[C4] [CLI1] server/discover answers the Copilot CLI probe with versions, capabilities, serverInfo and cache hints', async t => {
  const { client } = startBridge(t, { ClientClass: CopilotCliLikeClient });
  const reply = await client.discover();
  const result = reply.result;
  assert.equal(result.resultType, 'complete');
  assert.ok(result.supportedVersions.includes('2026-07-28'));
  assert.ok(result.supportedVersions.includes('2025-11-25'));
  assert.deepEqual(result.capabilities, { tools: { listChanged: false } });
  assert.deepEqual(result._meta['io.modelcontextprotocol/serverInfo'], SERVER_INFO);
  assert.ok(Number.isInteger(result.ttlMs) && result.ttlMs >= 0);
  assert.ok(['public', 'private'].includes(result.cacheScope));
});

test('[C5] an unsupported modern version gets -32022 with data.supported; malformed modern metadata gets -32602', async t => {
  const { client } = startBridge(t, { ClientClass: CopilotCliLikeClient });
  const unsupported = await client.request('tools/list', { _meta: { ...MODERN_META, 'io.modelcontextprotocol/protocolVersion': '1900-01-01' } });
  assert.equal(unsupported.error.code, -32022);
  assert.deepEqual(unsupported.error.data.supported, [...SUPPORTED_VERSIONS]);
  assert.equal(unsupported.error.data.requested, '1900-01-01');
  // A legacy version in per-request metadata is supported only through initialize. The
  // reply must NOT be a modern error, so a probing dual-era client falls back to initialize.
  const legacyInMeta = await client.request('server/discover', { _meta: { ...MODERN_META, 'io.modelcontextprotocol/protocolVersion': '2025-11-25' } });
  assert.equal(legacyInMeta.error.code, -32602);
  const missingCaps = await client.request('tools/list', { _meta: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28' } });
  assert.equal(missingCaps.error.code, -32602);
  const nonStringVersion = await client.request('tools/list', { _meta: { ...MODERN_META, 'io.modelcontextprotocol/protocolVersion': 20260728 } });
  assert.equal(nonStringVersion.error.code, -32602);
  // Neither era established: no handshake and no modern metadata.
  const bare = await client.request('tools/list', {});
  assert.equal(bare.error.code, -32602);
  assert.deepEqual(bare.error.data.supported, [...SUPPORTED_VERSIONS]);
});

test('[C6] modern results carry resultType "complete" and serverInfo; legacy results omit resultType', async t => {
  const host = await hostFor(t);
  const modern = startBridge(t, { host, ClientClass: CopilotCliLikeClient }).client;
  for (const reply of [await modern.discover(), await modern.listTools(), await modern.callTool('bmo_health', {}), await modern.request('ping', { _meta: modern.meta() })]) {
    assert.equal(reply.result.resultType, 'complete');
    assert.deepEqual(reply.result._meta['io.modelcontextprotocol/serverInfo'], SERVER_INFO);
  }
  const legacy = startBridge(t, { host, ClientClass: VsCodeLikeClient }).client;
  await legacy.handshake();
  for (const reply of [await legacy.listTools(), await legacy.callTool('bmo_health', {}), await legacy.request('ping')]) {
    assert.equal(reply.result.resultType, undefined);
    assert.equal(reply.result._meta, undefined);
  }
});

test('[C7] tools/list is identical and deterministically ordered on every call, in both eras, host up or down', async t => {
  const modern = startBridge(t, { ClientClass: CopilotCliLikeClient }).client; // no host at all
  const first = await modern.listTools();
  const second = await modern.listTools();
  assert.equal(JSON.stringify(first.result.tools), JSON.stringify(second.result.tools));
  assert.ok(Number.isInteger(first.result.ttlMs));
  assert.ok(['public', 'private'].includes(first.result.cacheScope));
  assert.equal(first.result.nextCursor, undefined);
  const host = await hostFor(t);
  const legacy = startBridge(t, { host, ClientClass: VsCodeLikeClient }).client;
  await legacy.handshake();
  const legacyList = await legacy.listTools();
  assert.equal(JSON.stringify(legacyList.result.tools), JSON.stringify(first.result.tools));
  assert.equal(legacyList.result.ttlMs, undefined);
  assert.deepEqual(first.result.tools.map(tool => tool.name), ['bmo_ask', 'bmo_job_status', 'bmo_job_cancel', 'bmo_health']);
  // Definitions are frozen constants: nothing at runtime (or from the host) can rewrite them.
  assert.ok(Object.isFrozen(TOOL_DEFINITIONS) && Object.isFrozen(TOOL_DEFINITIONS[0].annotations) && Object.isFrozen(TOOL_DEFINITIONS[0].inputSchema.properties.task));
});

test('[C8] [VS2] tool names match the Copilot regex, are short and unique, and VS Code tool IDs stay unique within 64 chars', async t => {
  const { client } = startBridge(t, { ClientClass: CopilotCliLikeClient });
  const { tools } = (await client.listTools()).result;
  const names = tools.map(tool => tool.name);
  assert.equal(new Set(names).size, names.length);
  assert.ok(names.length <= 5);
  for (const name of names) {
    assert.match(name, /^[A-Za-z0-9_-]{1,16}$/, name);
    assert.match(name, /^[a-zA-Z0-9_-]{1,128}$/); // Copilot API (G5)
  }
  assert.equal(SERVER_INFO.name, 'bmo');
  // VS Code: (prefix + name).replaceAll('.', '_').slice(0, 64); collisions drop tools (V12).
  const ids = names.map(name => `mcp_bmo_${name}`.replaceAll('.', '_'));
  for (const id of ids) assert.ok(id.length <= 64, id);
  assert.equal(new Set(ids.map(id => id.slice(0, 64))).size, ids.length);
});

function walk(value, visit, path = '') {
  visit(value, path);
  if (value && typeof value === 'object') for (const [key, child] of Object.entries(value)) walk(child, visit, `${path}/${key}`);
}

test('[C9] every inputSchema is a flat object schema with additionalProperties:false, bounded strings, no $ref or composition', async t => {
  const { client } = startBridge(t, { ClientClass: CopilotCliLikeClient });
  for (const tool of (await client.listTools()).result.tools) {
    assert.equal(tool.inputSchema.type, 'object', tool.name);
    assert.equal(tool.inputSchema.additionalProperties, false, tool.name);
    walk(tool.inputSchema, (node, path) => {
      if (!node || typeof node !== 'object') return;
      for (const banned of ['$ref', 'anyOf', 'oneOf', 'allOf', '$defs', 'not', 'if']) assert.ok(!Object.hasOwn(node, banned), `${tool.name}${path} uses ${banned}`);
      if (node.type === 'string') assert.ok(Number.isInteger(node.maxLength), `${tool.name}${path} string without maxLength`);
    });
    assert.equal(tool.outputSchema.type, 'object');
    assert.equal(tool.outputSchema.additionalProperties, false);
  }
});

test('[C10] unknown tool is JSON-RPC -32602; unknown or undeclared methods are -32601', async t => {
  const { client } = startBridge(t, { ClientClass: VsCodeLikeClient });
  await client.handshake();
  const unknownTool = await client.callTool('fs.read_text', {});
  assert.equal(unknownTool.error.code, -32602);
  assert.equal((await client.request('tools/call', { arguments: {} })).error.code, -32602);
  assert.equal((await client.request('tools/call', { name: 'bmo_health', arguments: [] })).error.code, -32602);
  for (const method of ['resources/list', 'prompts/list', 'logging/setLevel', 'completion/complete', 'tasks/get', 'no/such/method']) {
    const reply = await client.request(method, {});
    assert.equal(reply.error.code, -32601, method);
  }
});

test('[C16] [C17] capabilities declare tools only, and a full session never emits log notifications, list_changed or server requests', async t => {
  const host = await hostFor(t);
  const { client } = startBridge(t, { host, ClientClass: VsCodeLikeClient });
  const init = await client.handshake();
  assert.deepEqual(Object.keys(init.result.capabilities), ['tools']);
  await client.listTools();
  await client.callTool('bmo_ask', { task: 'Summarise this', context: 'some text' }, { progressToken: 'p1' });
  await client.callTool('bmo_health', {});
  await client.request('ping');
  for (const message of client.messages) {
    assert.notEqual(message.method, 'notifications/message');
    assert.notEqual(message.method, 'notifications/tools/list_changed');
    assert.ok(!(Object.hasOwn(message, 'method') && Object.hasOwn(message, 'id')), 'server sent a request');
  }
  const discover = await startBridge(t, { ClientClass: CopilotCliLikeClient }).client.discover();
  assert.deepEqual(Object.keys(discover.result.capabilities), ['tools']);
});

const EXPECTED_ANNOTATIONS = {
  bmo_ask: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  bmo_job_status: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  bmo_job_cancel: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  bmo_health: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
};

test('[C18] [VS1] annotations match the design: bmo_ask and bmo_job_cancel are never read-only, polling tools are', async t => {
  const { client } = startBridge(t, { ClientClass: VsCodeLikeClient });
  await client.handshake();
  const { tools } = (await client.listTools()).result;
  for (const tool of tools) {
    const { title, ...hints } = tool.annotations;
    assert.deepEqual(hints, EXPECTED_ANNOTATIONS[tool.name], tool.name);
    assert.equal(typeof title, 'string');
    assert.equal(tool.title, title);
    assert.ok(tool.description.length <= 600, `${tool.name} description too long`);
  }
  assert.notEqual(tools.find(tool => tool.name === 'bmo_ask').annotations.readOnlyHint, true);
});

test('tool descriptions say when to delegate, when not to, and that the model is small, local and fallible', () => {
  const ask = TOOL_DEFINITIONS.find(tool => tool.name === 'bmo_ask').description;
  for (const phrase of [/summaris/i, /extract/i, /classif/i, /Do NOT use for code edits/i, /web/i, /secrets/i, /high-stakes/i, /small/i, /may be wrong/i, /approve/i, /bmo_job_status/]) assert.match(ask, phrase);
});

test('ping works before and after initialize, and in the modern era', async t => {
  const { client } = startBridge(t, { ClientClass: VsCodeLikeClient });
  assert.deepEqual((await client.request('ping')).result, {});
  await client.handshake();
  assert.deepEqual((await client.request('ping')).result, {});
  const modern = startBridge(t, { ClientClass: CopilotCliLikeClient }).client;
  assert.equal((await modern.request('ping', { _meta: modern.meta() })).result.resultType, 'complete');
});

test('JSON-RPC envelope errors: non-objects, null ids and wrong versions are -32600; notifications and client responses get no reply', async t => {
  const { client } = startBridge(t, { ClientClass: VsCodeLikeClient });
  await client.handshake();
  client.sendRaw('42\n');
  const notObject = await client.waitFor(message => message.error?.code === -32600);
  assert.equal(notObject.id, undefined);
  client.send({ jsonrpc: '2.0', id: null, method: 'tools/list' });
  client.send({ jsonrpc: '1.0', id: 'v1', method: 'tools/list' });
  const wrongVersion = await client.waitFor(message => message.id === 'v1');
  assert.equal(wrongVersion.error.code, -32600);
  client.send({ jsonrpc: '2.0', id: 'r1', result: {} }); // a stray client "response"
  client.notify('notifications/unknown', {});
  client.notify('notifications/cancelled', { requestId: 'never-issued' });
  const before = client.messages.length;
  await client.request('ping', undefined, { id: 'after' });
  const extra = client.messages.slice(before).filter(message => message.id !== 'after');
  assert.deepEqual(extra, []);
  assert.equal(client.messages.filter(message => message.error?.code === -32600).length, 3);
});

test('[C10] malformed JSON yields a parse error (-32700) without an id and the server keeps serving', async t => {
  const { client } = startBridge(t, { ClientClass: VsCodeLikeClient });
  client.sendRaw('{"jsonrpc":"2.0","id":1,"method":\n');
  const parse = await client.waitFor(message => message.error?.code === -32700);
  assert.equal(Object.hasOwn(parse, 'id'), false);
  assert.equal((await client.handshake()).result.protocolVersion, '2025-11-25');
});

test('batches are refused except for a 2025-03-26 session, where they are answered as an array', async t => {
  const host = await hostFor(t);
  const modern = startBridge(t, { host, ClientClass: VsCodeLikeClient }).client;
  await modern.handshake('2025-11-25');
  modern.send([{ jsonrpc: '2.0', id: 'b1', method: 'ping' }]);
  const refused = await modern.waitFor(message => message.error?.code === -32600);
  assert.match(refused.error.message, /Batch/);
  const old = startBridge(t, { host, ClientClass: VsCodeLikeClient }).client;
  await old.handshake('2025-03-26');
  old.send([{ jsonrpc: '2.0', id: 'b1', method: 'ping' }, { jsonrpc: '2.0', method: 'notifications/initialized' }, { jsonrpc: '2.0', id: 'b2', method: 'tools/call', params: { name: 'bmo_health', arguments: {} } }]);
  const batchLine = await old.waitFor(message => Array.isArray(message));
  assert.deepEqual(batchLine.map(reply => reply.id).sort(), ['b1', 'b2']);
  assert.equal(batchLine.find(reply => reply.id === 'b2').result.structuredContent.ready, true);
  old.send([]);
  await old.waitFor(message => message.error?.message?.includes('empty batch'));
});

test('a duplicate in-flight request id is rejected without disturbing the original call', async t => {
  const host = await hostFor(t, { completeAfterMs: 400 });
  const { client } = startBridge(t, { host, ClientClass: CopilotCliLikeClient });
  client.send({ jsonrpc: '2.0', id: 'dup', method: 'tools/call', params: { name: 'bmo_ask', arguments: { task: 'one' }, _meta: client.meta() } });
  await new Promise(resolve => setTimeout(resolve, 50));
  client.send({ jsonrpc: '2.0', id: 'dup', method: 'tools/call', params: { name: 'bmo_health', arguments: {}, _meta: client.meta() } });
  const rejected = await client.waitFor(reply => reply.id === 'dup' && reply.error);
  assert.equal(rejected.error.code, -32600);
  const original = await client.waitFor(reply => reply.id === 'dup' && reply.result);
  assert.equal(original.result.structuredContent.status, 'completed');
  assert.equal(client.responsesFor('dup').length, 2);
});

test('[VS1] a scripted VS Code session: handshake, list, delegate, answer', async t => {
  const host = await hostFor(t, { answer: 'Three bullet summary.' });
  const { client } = startBridge(t, { host, ClientClass: VsCodeLikeClient });
  await client.handshake();
  const names = (await client.listTools()).result.tools.map(tool => tool.name);
  assert.ok(names.includes('bmo_ask'));
  const reply = await client.callTool('bmo_ask', { task: 'Summarise', context: 'long text' });
  assert.equal(structured(reply).status, 'completed');
  assert.equal(structured(reply).answer, 'Three bullet summary.');
  assert.equal(reply.result.isError, false);
  assert.equal(host.jobs.size, 1);
  assert.deepEqual([...host.jobs.values()][0].body.caller, { client: 'Visual Studio Code', name: '1.140.0' });
});

test('[CLI1] a scripted Copilot CLI session: discover probe, stateless list and call, no handshake needed', async t => {
  const host = await hostFor(t, { answer: 'Classified: invoice' });
  const { client } = startBridge(t, { host, ClientClass: CopilotCliLikeClient });
  assert.equal((await client.discover()).result.resultType, 'complete');
  assert.equal((await client.listTools()).result.tools.length, 4);
  const reply = await client.callTool('bmo_ask', { task: 'Classify', context: 'Invoice #1' });
  assert.equal(structured(reply).answer, 'Classified: invoice');
  assert.deepEqual([...host.jobs.values()][0].body.caller, { client: 'github-copilot-cli', name: '1.0.91' });
  // A legacy client and a modern client may share one dual-era process.
  const legacyReply = await client.request('initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'Visual Studio Code', version: '1.140.0' } });
  assert.equal(legacyReply.result.protocolVersion, '2025-11-25');
  assert.equal((await client.listTools()).result.resultType, 'complete');
});

test('a plain line client that skips initialize and metadata gets an actionable -32602 (era cannot be inferred)', async t => {
  const { client } = startBridge(t, { ClientClass: LineClient });
  const reply = await client.request('tools/call', { name: 'bmo_health', arguments: {} });
  assert.equal(reply.error.code, -32602);
  assert.match(reply.error.message, /initialize/);
});
