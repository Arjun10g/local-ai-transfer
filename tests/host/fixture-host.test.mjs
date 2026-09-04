import test from 'node:test';
import assert from 'node:assert/strict';
import { parseStrictJson, parseToolCall, validateToolResult, EnvelopeError } from '../../host/agent/tool-envelope.mjs';
import { validateEvent } from '../../host/agent/assistant-events.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { FixtureEngineClient } from '../../host/engine/fixture-engine.mjs';
import { HostServer } from '../../host/server/host-server.mjs';

const auth = token => ({ authorization: `Bearer ${token}` });

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

test('host enforces loopback auth/origin, static allowlist, and streams fixture events', async t => {
  const engine = new FixtureEngineClient({ chunkSize: 4, delayMs: 0 }); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine });
  const address = await host.listen(0); t.after(() => host.close());
  const health = await fetch(`${address.url}/healthz`); assert.equal(health.status, 200);
  const unauthorized = await fetch(`${address.url}/api/status`); assert.equal(unauthorized.status, 401);
  const forbidden = await fetch(`${address.url}/api/status`, { headers: { ...auth(address.token), origin: 'https://attacker.invalid' } }); assert.equal(forbidden.status, 403);
  const page = await fetch(`${address.url}/`, { headers: auth(address.token) }); assert.equal(page.status, 200); assert.match(await page.text(), /window\.__LAE_TOKEN__ = '[^']+'/);
  const sessionResponse = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: '{}' }); assert.equal(sessionResponse.status, 201); const session = await sessionResponse.json();
  const response = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: { ...auth(address.token), 'content-type': 'application/json' }, body: JSON.stringify({ session_id: session.session_id, request_id: 'req_http01', message: 'What time is it?' }) });
  assert.equal(response.status, 200); const stream = await response.text(); assert.match(stream, /event: tool\.completed/); assert.match(stream, /event: message\.completed/);
  const traversal = await fetch(`${address.url}/../package.json`, { headers: auth(address.token) }); assert.equal(traversal.status, 404);
});

