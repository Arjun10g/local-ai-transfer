import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { makeToolResult, parseStrictJson, parseToolCall, coerceBooleanArguments, validateToolCall, validateToolResult, ToolCallStreamDecoder, EnvelopeError } from '../../host/agent/tool-envelope.mjs';
import { validateEvent } from '../../host/agent/assistant-events.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { FixtureEngineClient } from '../../host/engine/fixture-engine.mjs';
import { HostServer, isWithinDirectory } from '../../host/server/host-server.mjs';
import { mergeConfig } from '../../host/agent/config.mjs';
import { OperatorGrantStore, OperatorGrantControl, buildOperatorGrantBindings } from '../../host/providers/operator-grants.mjs';

const auth = token => ({ authorization: `Bearer ${token}` });
const rawPost = ({ port, host, origin, body }) => new Promise((resolve, reject) => { const request = http.request({ hostname: '127.0.0.1', port, path: '/bootstrap', method: 'POST', headers: { host, origin, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) } }, response => { response.resume(); response.once('end', () => resolve(response.statusCode)); }); request.once('error', reject); request.end(body); });

test('strict parser rejects duplicate, trailing, deep, and oversized JSON', () => {
  assert.deepEqual(parseStrictJson('{"a":[true,null,2]}'), { a: [true, null, 2] });
  for (const input of ['{"a":1,"a":2}', '{"a":1} trailing', '{"a":[[[[[[[[[1]]]]]]]]]}']) assert.throws(() => parseStrictJson(input), EnvelopeError);
  assert.throws(() => parseStrictJson('{"a":"x"}'.replace('x', 'x'.repeat(5000))), /exceeds/);
});

test('tool envelope is exact and result is bounded', () => {
  assert.deepEqual(parseToolCall('{"id":"call_1","name":"time.now","arguments":{"format":"local"}}'), { id: 'call_1', name: 'time.now', arguments: { format: 'local' } });
  assert.throws(() => parseToolCall('{"id":"call_1","name":"time.now","arguments":{},"extra":true}'), /unknown field/);
  assert.throws(() => parseToolCall('{"id":"call_1","name":"time.now","arguments":[]}'), /arguments/);
  assert.deepEqual(validateToolResult({ id: 'call_1', name: 'time.now', status: 'ok', content: [{ type: 'text', text: 'ok' }], metadata: { truncated: false, duration_ms: 1 } }).status, 'ok');
  const prototypeKey = parseStrictJson('{"__proto__":{"polluted":true}}');
  assert.equal(Object.hasOwn(prototypeKey, '__proto__'), true); assert.equal(Object.prototype.polluted, undefined);
  const inheritedResult = Object.create({ id: 'call_1', name: 'time.now', status: 'ok', content: [], metadata: { truncated: false, duration_ms: 0 } });
  assert.throws(() => validateToolResult(inheritedResult), /missing field/);
  const inheritedCall = Object.create({ id: 'call_1', name: 'time.now', arguments: {} });
  assert.throws(() => validateToolCall(inheritedCall), EnvelopeError);
});

test('Qwen XML tool calls remain safe across split chunks', () => {
  const decoder = new ToolCallStreamDecoder(); const events = [];
  for (const chunk of ['prefix ', '<tool_', 'call>\n<function=time.now>\n<parameter=format>\nlocal\n</parameter>\n</function>', '</tool_call>']) events.push(...decoder.push(chunk));
  events.push(...decoder.finish());
  assert.equal(events.filter(e => e.kind === 'text_delta').map(e => e.text).join(''), '');
  assert.equal(events.filter(e => e.kind === 'tool_call_chunk').length, 1);
  const callEvent = events.find(e => e.kind === 'tool_call_chunk');
  assert.equal(parseToolCall(callEvent.text).name, 'time.now');
  assert.throws(() => parseToolCall('<tool_call>\n<function=time.now>\n</function>\n</tool_call> trailing'), EnvelopeError);
  const incomplete = new ToolCallStreamDecoder(); incomplete.push('<tool_call>{'); assert.throws(() => incomplete.finish(), /unterminated/);
  const partialTag = new ToolCallStreamDecoder(); partialTag.push('answer <tool_'); assert.throws(() => partialTag.finish(), /incomplete/);
});

test('receipt Qwen XML grammar parses at every byte boundary and assigns host correlation', async () => {
  const xml = '<tool_call>\n<function=system.get_info>\n</function>\n</tool_call>';
  for (let split = 0; split <= xml.length; split++) {
    const decoder = new ToolCallStreamDecoder(); const events = [...decoder.push(xml.slice(0, split)), ...decoder.push(xml.slice(split)), ...decoder.finish()];
    const calls = events.filter(e => e.kind === 'tool_call_chunk'); assert.equal(calls.length, 1, `split ${split}`);
    const parsed = parseToolCall(calls[0].text); assert.match(parsed.id, /^call_[A-Za-z0-9_-]{32}$/); assert.equal(parsed.name, 'system.get_info'); assert.deepEqual(parsed.arguments, {});
  }
  for (const malformed of [
    '<tool_call><function=system.get_info><parameter=x bad>1</parameter></function></tool_call>',
    '<tool_call><function=system.get_info><parameter=x>1</parameter><parameter=x>2</parameter></function></tool_call>',
    '<tool_call><function=system.get_info><parameter=x><nested/></parameter></function></tool_call>',
    '<tool_call><function=system.get_info></function></tool_call>suffix'
  ]) assert.throws(() => parseToolCall(malformed), EnvelopeError);
  const unknownArgument = new ConversationController({ engine: { async *generate() { yield { kind: 'tool_call_chunk', text: '<tool_call><function=system.get_info><parameter=x>1</parameter></function></tool_call>' }; } }, toolRegistry: { 'system.get_info': { name: 'system.get_info', risk_tier: 'T0', side_effect: 'none', execute: async () => { throw new Error('must not execute'); } } } });
  assert.equal((await unknownArgument.runTurn({ sessionId: 'ses_xmlbad', requestId: 'req_xmlbad', message: 'bad arg' })).error, 'invalid_tool_arguments');
});

test('receipt Qwen XML parameters normalize safely and controller bounds tool results', async () => {
  const parsed = parseToolCall('<tool_call>\n<function=time.now>\n<parameter=format>\nlocal\n</parameter>\n</function>\n</tool_call>');
  assert.equal(parsed.name, 'time.now'); assert.deepEqual(parsed.arguments, { format: 'local' }); assert.match(parsed.id, /^call_/);
  const badResult = new ConversationController({ engine: { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_badresult', name: 'test.bad', arguments: {} }) }; } }, toolRegistry: { 'test.bad': { name: 'test.bad', risk_tier: 'T1', side_effect: 'read_sensitive', timeout_ms: 100, execute: async () => ({ id: 'call_badresult', name: 'test.bad', content: [] }) } } });
  assert.equal((await badResult.runTurn({ sessionId: 'ses_badres', requestId: 'req_badres', message: 'bad' })).error, 'invalid_tool_result');
  const timed = new ConversationController({ engine: { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_timeout', name: 'test.slow', arguments: {} }) }; } }, toolRegistry: { 'test.slow': { name: 'test.slow', risk_tier: 'T1', side_effect: 'read_sensitive', timeout_ms: 5, execute: async () => new Promise(resolve => setTimeout(resolve, 50)) } } });
  assert.equal((await timed.runTurn({ sessionId: 'ses_timeout', requestId: 'req_timeout', message: 'slow' })).error, 'tool_timeout');
  let executed = false;
  const previewTimed = new ConversationController({ engine: { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_preview', name: 'test.preview', arguments: {} }) }; } }, toolRegistry: { 'test.preview': { name: 'test.preview', risk_tier: 'T1', side_effect: 'read_sensitive', timeout_ms: 5, preview: async () => new Promise(resolve => setTimeout(resolve, 50)), execute: async () => { executed = true; return makeToolResult({ id: 'call_preview', name: 'test.preview' }); } } } });
  assert.equal((await previewTimed.runTurn({ sessionId: 'ses_preview', requestId: 'req_preview', message: 'preview' })).error, 'tool_timeout'); assert.equal(executed, false);
  const literal = parseToolCall('<tool_call><function=browser.open_url><parameter=url>https://example.test/a?x=1&amp;raw=2</parameter></function></tool_call>'.replace('&amp;', '&'));
  assert.equal(literal.arguments.url, 'https://example.test/a?x=1&raw=2');
  const code = parseToolCall('<tool_call><function=fs.write_new><parameter=content>if (a < b && c > d) {\n  return "ok";\n}</parameter></function></tool_call>');
  assert.match(code.arguments.content, /a < b && c > d/);
  const indented = parseToolCall('<tool_call><function=fs.write_new><parameter=content>\n  first line  \n  second line\n</parameter></function></tool_call>');
  assert.equal(indented.arguments.content, '  first line  \n  second line');
  for (const attack of ['<tool_call><function=fs.write_new><parameter=content><parameter=x>bad</parameter></parameter></function></tool_call>', '<tool_call><function=fs.write_new><parameter=content>bad</function></parameter></function></tool_call>']) assert.throws(() => parseToolCall(attack), EnvelopeError);
});

test('shared Qwen XML value vectors remain aligned with the host parser', async () => {
  const vectors = JSON.parse(await readFile(new URL('../model/qwen_xml_vectors.json', import.meta.url), 'utf8'));
  assert.equal(vectors.schema, 'local_bmo.qwen-xml-vectors.v1');
  for (const vector of vectors.vectors) {
    if (vector.reject) assert.throws(() => parseToolCall(vector.xml), EnvelopeError);
    else {
      const parsed = parseToolCall(vector.xml);
      assert.deepEqual({ name: parsed.name, arguments: parsed.arguments }, vector.expected, vector.id);
    }
  }
});

test('shared boolean-coercion vectors agree with the Python evaluator', async () => {
  // Qwen3.5 can write `True`/`False`; coercion is by declared type, so a string
  // parameter holding the word keeps it. Rejected values are never defaulted.
  const { coercion } = JSON.parse(await readFile(new URL('../model/qwen_xml_vectors.json', import.meta.url), 'utf8'));
  const schema = coercion.tool.function.parameters;
  assert.ok(coercion.cases.length >= 12);
  for (const vector of coercion.cases) {
    const parsed = parseToolCall(vector.xml);
    const args = coerceBooleanArguments(schema, parsed.arguments);
    if (vector.reject) {
      assert.deepEqual(args, vector.coerced, vector.id);
      assert.notEqual(typeof args.flag, 'boolean', `${vector.id}: must not be defaulted to a boolean`);
    } else {
      assert.deepEqual({ name: parsed.name, arguments: args }, vector.expected, vector.id);
    }
  }
});

test('controller coerces a Python-style boolean before the tool runs, and refuses a non-boolean', async () => {
  const ran = [];
  const tool = { name: 'test.flags', description: 'Flag fixture.', risk_tier: 'T1', side_effect: 'read_sensitive',
    parameters: { type: 'object', properties: { flag: { type: 'boolean' }, note: { type: 'string' } }, additionalProperties: false },
    execute: async ({ id, name, arguments: args }) => { ran.push(args); return { id, name, status: 'ok', content: [{ type: 'text', text: 'ok' }], metadata: { truncated: false, duration_ms: 0 } }; } };
  const run = async (xml, tag) => {
    const engine = { async *generate({ messages }) {
      if (!messages.some(m => m.role === 'tool')) { const decoder = new ToolCallStreamDecoder(); for (const e of decoder.push(xml)) yield e; for (const e of decoder.finish()) yield e; return; }
      yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' };
    } };
    return new ConversationController({ engine, toolRegistry: { 'test.flags': tool } }).runTurn({ sessionId: `ses_${tag}`, requestId: `req_${tag}`, message: 'go' });
  };
  const call = (flag, note) => `<tool_call><function=test.flags><parameter=flag>${flag}</parameter>${note === undefined ? '' : `<parameter=note>${note}</parameter>`}</function></tool_call>`;
  assert.equal((await run(call('False', 'False'), 'bool01')).state, 'COMPLETED');
  assert.deepEqual(ran.at(-1), { flag: false, note: 'False' }, 'the boolean is coerced; the string parameter keeps its text');
  ran.length = 0;
  const refused = await run(call('yes'), 'bool02');
  assert.equal(refused.state === 'COMPLETED', false, 'a non-boolean must still be refused');
  assert.equal(ran.length, 0, 'and the tool must not run');
});

test('controller propagates complete tool schema and ordered tool result correlation', async () => {
  const seen = [];
  const engine = { async *generate({ messages, tools }) {
    seen.push({ messages: structuredClone(messages), tools: structuredClone(tools) });
    if (!messages.some(m => m.role === 'tool')) {
      const decoder = new ToolCallStreamDecoder();
      for (const chunk of ['<tool_', 'call>\n<function=test.echo>\n<parameter=value>\nok\n</parameter>\n</function></tool_call>']) for (const event of decoder.push(chunk)) yield event;
      for (const event of decoder.finish()) yield event;
      return;
    }
    yield { kind: 'text_delta', text: 'done' }; yield { kind: 'done', finish_reason: 'stop' };
  } };
  const controller = new ConversationController({ engine, toolRegistry: {
    'test.echo': { name: 'test.echo', description: 'Echo a value.', risk_tier: 'T1', side_effect: 'read_sensitive', parameters: { type: 'object', properties: { value: { type: 'string', maxLength: 64 } }, required: ['value'], additionalProperties: false }, execute: async ({ id, name, arguments: args }) => ({ id, name, status: 'ok', content: [{ type: 'text', text: args.value }], metadata: { truncated: false, duration_ms: 0 } }) }
  } });
  const result = await controller.runTurn({ sessionId: 'ses_xml01', requestId: 'req_xml01', message: 'use echo' });
  assert.equal(result.state, 'COMPLETED'); assert.equal(result.text, 'done'); assert.equal(seen.length, 2);
  assert.equal(seen[0].tools.find(t => t.function.name === 'test.echo').type, 'function');
  assert.match(seen[1].messages.at(-1).tool_call_id, /^call_/); assert.equal(seen[1].messages.at(-1).content, 'ok');
  assert.deepEqual(seen[1].messages.map(message => message.role), ['user', 'assistant', 'tool']); assert.match(seen[1].messages[1].content, /<tool_call>/);
});

test('controller rejects text mixed with a tool frame before preview or execution', async () => {
  let executed = false;
  const controller = new ConversationController({ engine: { async *generate() { yield { kind: 'text_delta', text: 'leaked answer' }; yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_mix01', name: 'test.mix', arguments: {} }) }; } }, toolRegistry: { 'test.mix': { name: 'test.mix', risk_tier: 'T1', side_effect: 'read_sensitive', execute: async () => { executed = true; return makeToolResult({ id: 'call_mix01', name: 'test.mix' }); } } } });
  const result = await controller.runTurn({ sessionId: 'ses_mix01', requestId: 'req_mix01', message: 'mixed' });
  assert.equal(result.error, 'mixed_tool_call_output'); assert.equal(executed, false);
});

test('controller enforces every fs.apply_patch base/payload combination', async () => {
  for (const [base, payload] of [['base_sha256', 'replacement'], ['base_sha256', 'patch'], ['base_hash', 'replacement'], ['base_hash', 'patch']]) {
    const args = { workspace_id: 'project', path: 'x', [base]: 'a'.repeat(64), [payload]: 'body' };
    const engine = { async *generate({ messages }) { if (!messages.some(m => m.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_combo01', name: 'fs.apply_patch', arguments: args }) }; return; } yield { kind: 'text_delta', text: 'ok' }; } };
    const controller = new ConversationController({ engine, toolRegistry: { 'fs.apply_patch': { name: 'fs.apply_patch', risk_tier: 'T1', side_effect: 'read_sensitive', execute: async ({ id, name }) => ({ id, name, status: 'ok', content: [{ type: 'text', text: 'ok' }], metadata: { truncated: false, duration_ms: 0 } }) } } });
    assert.equal((await controller.runTurn({ sessionId: `ses_${payload}${base.slice(-2)}`, requestId: `req_${payload}${base.slice(-2)}`, message: 'patch' })).state, 'COMPLETED');
  }
});

test('controller executes deterministic time.now loop and preserves event order', async () => {
  const controller = new ConversationController({ engine: new FixtureEngineClient({ chunkSize: 3, delayMs: 0 }) });
  const events = []; const session = controller.createSession('ses_fixture');
  const result = await controller.runTurn({ sessionId: session.id, requestId: 'req_fixture', message: 'What time is it?', onEvent: event => events.push(event) });
  assert.equal(result.state, 'COMPLETED'); assert.match(result.text, /fixture clock reports/);
  assert.equal(events.at(-1).event, 'metrics.snapshot');
  assert.ok(events.some(e => e.event === 'tool.proposed')); assert.ok(events.some(e => e.event === 'tool.completed'));
  for (let i = 1; i < events.length; i++) assert.equal(events[i].sequence, events[i - 1].sequence + 1);
  assert.doesNotThrow(() => events.forEach(validateEvent));
});

test('controller cancellation leaves reusable terminal state', async () => {
  const controller = new ConversationController({ engine: new FixtureEngineClient({ chunkSize: 1, delayMs: 5 }) });
  const promise = controller.runTurn({ sessionId: 'ses_cancel', requestId: 'req_cancel', message: 'hello', onEvent: () => {} });
  setTimeout(() => controller.cancel('req_cancel'), 15);
  const result = await promise; assert.equal(result.state, 'CANCELLED'); assert.equal(controller.state('ses_cancel'), 'CANCELLED');
  const next = await controller.runTurn({ sessionId: 'ses_cancel', requestId: 'req_next01', message: 'hello', onEvent: () => {} }); assert.equal(next.state, 'COMPLETED');
});

test('controller bounds sessions with deterministic LRU eviction and history', async () => {
  const controller = new ConversationController({ engine: new FixtureEngineClient({ delayMs: 0 }), maxSessions: 2, maxHistoryMessages: 3, maxHistoryBytes: 2048 });
  controller.createSession('ses_lru01'); controller.createSession('ses_lru02'); controller.getSession('ses_lru01'); controller.createSession('ses_lru03');
  assert.equal(controller.sessions.has('ses_lru01'), true); assert.equal(controller.sessions.has('ses_lru02'), false); assert.equal(controller.sessions.has('ses_lru03'), true);
  await controller.runTurn({ sessionId: 'ses_hist01', requestId: 'req_hist01', message: 'hello', onEvent: () => {} });
  await controller.runTurn({ sessionId: 'ses_hist01', requestId: 'req_hist02', message: 'second bounded turn', onEvent: () => {} });
  const session = controller.sessions.get('ses_hist01'); assert.ok(session); assert.ok(session.history.length <= 3); assert.ok(session.history_bytes <= 2048);
  assert.equal(controller.resetSession('ses_hist01'), true); assert.equal(session.history.length, 0); assert.equal(session.history_bytes, 0);
});

test('host enforces loopback auth/origin, static allowlist, and streams fixture events', async t => {
  const engine = new FixtureEngineClient({ chunkSize: 4, delayMs: 0 }); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine });
  const address = await host.listen(0); t.after(() => host.close());
  const health = await fetch(`${address.url}/healthz`); assert.equal(health.status, 200);
  const unauthorized = await fetch(`${address.url}/api/status`); assert.equal(unauthorized.status, 401);
  const multibyteUnauthorized = await fetch(`${address.url}/api/status`, { headers: { authorization: `Bearer ${'é'.repeat(43)}` } }); assert.equal(multibyteUnauthorized.status, 401);
  const forbidden = await fetch(`${address.url}/api/status`, { headers: { ...auth(address.token), origin: 'https://attacker.invalid' } }); assert.equal(forbidden.status, 403);
  const page = await fetch(`${address.url}/`); assert.equal(page.status, 200); const pageText = await page.text(); assert.equal(pageText.includes(address.token), false); assert.equal(pageText.includes('lae-token'), false); assert.equal(pageText.includes('__LAE_BOOTSTRAP__'), false);
  const sessionResponse = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: '{}' }); assert.equal(sessionResponse.status, 201); const session = await sessionResponse.json();
  const response = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ session_id: session.session_id, request_id: 'req_http01', message: 'What time is it?' }) });
  assert.equal(response.status, 200); const stream = await response.text(); assert.match(stream, /event: tool\.completed/); assert.match(stream, /event: message\.completed/);
  const traversal = await fetch(`${address.url}/../package.json`, { headers: auth(address.token) }); assert.equal(traversal.status, 404);
});

test('browser bootstrap is same-origin, one-time, replay-safe, and absent from served assets', async t => {
  const engine = new FixtureEngineClient(); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine });
  const address = await host.listen(0); t.after(() => host.close());
  const bootstrapUrl = new URL(address.bootstrap_url); const parameters = new URLSearchParams(bootstrapUrl.hash.slice(1)); const nonce = parameters.get('bootstrap');
  assert.match(nonce, /^[A-Za-z0-9_-]{43}$/); assert.equal(address.bootstrap_url.includes(address.token), false); assert.equal(bootstrapUrl.search, '');
  const app = await (await fetch(`${address.url}/app.js`)).text(); assert.equal(app.includes(nonce), false); assert.equal(app.includes(address.token), false); assert.ok(app.indexOf('history.replaceState') < app.indexOf("fetch('/bootstrap'"));
  const serverSource = await readFile(new URL('../../host/server/host-server.mjs', import.meta.url), 'utf8'); assert.equal(serverSource.includes('console.log'), false); assert.equal(serverSource.includes('console.error'), false);
  const hostile = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: 'http://127.0.0.1:9', 'content-type': 'application/json' }, body: JSON.stringify({ nonce }) }); assert.equal(hostile.status, 403);
  const wrongHost = await rawPost({ port: address.port, host: `localhost:${address.port}`, origin: address.url, body: JSON.stringify({ nonce }) }); assert.equal(wrongHost, 403);
  const leakedReferrer = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, referer: `${address.url}/#bootstrap=${nonce}`, 'content-type': 'application/json' }, body: JSON.stringify({ nonce }) }); assert.equal(leakedReferrer.status, 403);
  const duplicate = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json' }, body: `{"nonce":"${nonce}","nonce":"${nonce}"}` }); assert.equal(duplicate.status, 400);
  const invalid = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json' }, body: JSON.stringify({ nonce: 'A'.repeat(43) }) }); assert.equal(invalid.status, 401);
  const exchanged = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json' }, body: JSON.stringify({ nonce }) }); assert.equal(exchanged.status, 200); assert.equal(exchanged.headers.get('cache-control'), 'no-store'); assert.equal(exchanged.headers.get('referrer-policy'), 'no-referrer'); const issued = await exchanged.json(); assert.equal(issued.token, address.token);
  const replay = await fetch(`${address.url}/bootstrap`, { method: 'POST', headers: { origin: address.url, 'sec-fetch-site': 'same-origin', 'content-type': 'application/json' }, body: JSON.stringify({ nonce }) }); assert.equal(replay.status, 410); assert.equal(host.address().bootstrap_url, null); assert.match(app, /referrerPolicy:'no-referrer'/); assert.match(app, /cache:'no-store'/);
  const protectedApi = await fetch(`${address.url}/api/status`); assert.equal(protectedApi.status, 401);
  const authorizedApi = await fetch(`${address.url}/api/status`, { headers: auth(issued.token) }); assert.equal(authorizedApi.status, 200);
});

test('config rejects unknown/non-loopback settings and host enforces body bound', async t => {
  assert.throws(() => mergeConfig({ unknown: true }), /unknown key/);
  assert.throws(() => mergeConfig({ host: { bind: '0.0.0.0' } }), /127\.0\.0\.1/);
  const engine = new FixtureEngineClient(); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine, config: { host: { max_body_bytes: 1024 } } }); const address = await host.listen(0); t.after(() => host.close());
  const response = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ padding: 'x'.repeat(2000) }) }); assert.equal(response.status, 413);
});

test('authenticated operator grant API grants, projects, revokes, and rejects widening', async t => {
  const config = mergeConfig({ providers: { microsoft_graph: { enabled: true, permission_profile: 'full_access', account_fingerprint: 'acct-test', scope: 'account' } }, applications: { outlook: { executable: 'C:\\Program Files\\Microsoft Office\\root\\Office16\\OUTLOOK.EXE', args: [] } } }); // absolute: bare names are refused by config
  const store = new OperatorGrantStore(); const grants = new OperatorGrantControl({ store, bindings: buildOperatorGrantBindings(config) });
  const engine = new FixtureEngineClient(); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine, config, operatorGrants: grants }); const address = await host.listen(0); t.after(() => host.close());
  assert.equal((await fetch(`${address.url}/api/operator-grants`)).status, 401);
  const listed = await fetch(`${address.url}/api/operator-grants`, { headers: auth(address.token) }); assert.equal(listed.status, 200); const initial = await listed.json(); assert.deepEqual(initial.capabilities.map(value => value.capability), ['local.application:outlook', 'local.clipboard', 'microsoft.graph.mail', 'microsoft.graph.teams']);
  const endpoint = `${address.url}/api/operator-grants/microsoft.graph.mail`;
  for (const value of [{}, { granted: true }, { granted: true, duration_ms: 59999 }, { granted: false, duration_ms: 60000 }, { granted: true, duration_ms: 60000, scope: '*' }]) {
    const response = await fetch(endpoint, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify(value) }); assert.equal(response.status, 400);
  }
  const granted = await fetch(endpoint, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ granted: true, duration_ms: 60000 }) }); assert.equal(granted.status, 200); const projection = await granted.json(); assert.equal(projection.granted, true); assert.equal(Object.hasOwn(projection, 'account_fingerprint'), false); assert.equal(Object.hasOwn(projection, 'generation'), false);
  const unknown = await fetch(`${address.url}/api/operator-grants/microsoft.graph.admin`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ granted: true, duration_ms: 60000 }) }); assert.equal(unknown.status, 404);
  const revoked = await fetch(endpoint, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ granted: false }) }); assert.equal(revoked.status, 200); assert.equal((await revoked.json()).granted, false);
  grants.grant('microsoft.graph.mail', 60000); grants.grant('microsoft.graph.teams', 60000);
  const all = await fetch(`${address.url}/api/operator-grants/revoke-all`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: '{}' }); assert.equal(all.status, 200); assert.equal((await all.json()).revoked, 2); assert.equal(store.grants.size, 0);
});

test('pending confirmation cancellation resolves immediately and cannot replay', async () => {
  const engine = { async *generate({ messages }) { if (!messages.some(m => m.role === 'tool')) { yield { kind: 'tool_call_chunk', text: '{"id":"call_cancel1","name":"test.confirm","arguments":{}}' }; return; } yield { kind: 'text_delta', text: 'unexpected continuation' }; } };
  const controller = new ConversationController({ engine, confirmationTimeoutMs: 10000, toolRegistry: { 'test.confirm': { name: 'test.confirm', risk_tier: 'T1', side_effect: 'read_sensitive', requires_confirmation: true, execute: async () => { throw new Error('must not execute'); } } } });
  const events = []; const promise = controller.runTurn({ sessionId: 'ses_wait01', requestId: 'req_wait01', message: 'confirm', onEvent: event => events.push(event) });
  while (!events.some(e => e.event === 'tool.confirmation_required')) await new Promise(resolve => setTimeout(resolve, 1));
  const required = events.find(e => e.event === 'tool.confirmation_required'); assert.equal(controller.cancel('req_wait01'), true);
  const result = await promise; assert.equal(result.state, 'CANCELLED'); assert.equal(controller.pending.size, 0); assert.equal(controller.confirm(required.data.confirmation_id, true, { requestId: 'req_wait01', callId: 'call_cancel1' }), false);
  assert.equal(events.some(e => e.event === 'tool.started'), false); assert.equal(events.at(-1).event, 'request.cancelled');
});

test('asset containment helper is separator-safe and connection cap is explicit', async t => {
  assert.equal(isWithinDirectory('/tmp/ui', '/tmp/ui/index.html'), true); assert.equal(isWithinDirectory('/tmp/ui', '/tmp/ui-other/index.html'), false); assert.equal(isWithinDirectory('/tmp/ui', '/tmp/ui/../secret'), false);
  const engine = new FixtureEngineClient(); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine, config: { host: { max_connections: 2 } } }); const address = await host.listen(0); t.after(() => host.close());
  assert.equal(host.server.maxConnections, 2); const response = await fetch(`${address.url}/api/status`, { headers: auth(address.token) }); const status = await response.json(); assert.equal(status.limits.max_connections, 2);
});

test('confirmation is request/call bound and denial continues as a safe tool result', async () => {
  const engine = { async *generate({ messages }) { if (!messages.some(m => m.role === 'tool')) { yield { kind: 'tool_call_chunk', text: '{"id":"call_safe1","name":"test.confirm","arguments":{}}' }; return; } yield { kind: 'text_delta', text: 'Denied safely.' }; yield { kind: 'done', finish_reason: 'stop' }; } };
  const controller = new ConversationController({ engine, confirmationTimeoutMs: 1000, toolRegistry: { 'test.confirm': { name: 'test.confirm', risk_tier: 'T1', side_effect: 'read_sensitive', requires_confirmation: true, execute: async () => { throw new Error('must not execute'); } } } });
  const events = []; const promise = controller.runTurn({ sessionId: 'ses_confirm', requestId: 'req_confirm', message: 'do it', onEvent: event => events.push(event) });
  while (!events.some(e => e.event === 'tool.confirmation_required')) await new Promise(resolve => setTimeout(resolve, 1));
  const required = events.find(e => e.event === 'tool.confirmation_required'); assert.equal(controller.confirm(required.data.confirmation_id, true), false); assert.equal(controller.confirm(required.data.confirmation_id, true, { requestId: 'req_confirm' }), false); assert.equal(controller.confirm(required.data.confirmation_id, true, { callId: 'call_safe1' }), false); assert.equal(controller.confirm(required.data.confirmation_id, true, { requestId: 'req_other', callId: 'call_safe1' }), false); assert.equal(controller.confirm(required.data.confirmation_id, false, { requestId: 'req_confirm', callId: 'call_safe1' }), true);
  const result = await promise; assert.equal(result.state, 'COMPLETED'); assert.match(result.text, /Denied safely/); assert.equal(events.find(e => e.event === 'tool.completed').data.result.status, 'denied');
});
