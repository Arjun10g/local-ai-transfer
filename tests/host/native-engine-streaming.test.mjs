import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { setTimeout as delay } from 'node:timers/promises';
import { NativeEngineClient } from '../../host/engine/native-engine-client.mjs';

// A scripted stand-in for lae-engine's HTTP surface. Like the real server it
// writes the SSE head (with X-Request-Id) before any model work, then frames.
async function fakeEngine(chat, { cancelled = true } = {}) {
  const log = []; let sessions = 0; let requests = 0; const open = new Set();
  const server = http.createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk;
    const entry = { method: request.method, url: request.url, body: body ? JSON.parse(body) : null }; log.push(entry);
    if (request.url === '/v1/sessions') { response.writeHead(201, { 'content-type': 'application/json' }); response.end(JSON.stringify({ id: `sess-${++sessions}` })); return; }
    if (request.url.startsWith('/v1/cancel/')) { response.writeHead(200, { 'content-type': 'application/json' }); response.end(JSON.stringify({ cancelled })); return; }
    if (request.url === '/v1/chat/completions') {
      const id = `req-${++requests}`; open.add(response); response.once('close', () => open.delete(response));
      await chat({ request, response, body: entry.body, id, head: () => { response.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': id }); response.flushHeaders(); } });
      return;
    }
    response.writeHead(404, { 'content-type': 'application/json' }); response.end('{"error":{"code":"not_found"}}');
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return { log, port: server.address().port, close: () => { for (const response of open) response.destroy(); const closed = new Promise(resolve => server.close(resolve)); server.closeAllConnections(); return closed; } };
}

const delta = text => `data: ${JSON.stringify({ id: 'x', choices: [{ delta: { content: text } }] })}\n\n`;
const finish = (reason, usage) => `data: ${JSON.stringify({ id: 'x', choices: [{ delta: {}, finish_reason: reason }], ...(usage ? { usage } : {}) })}\n\n`;
const DONE = 'data: [DONE]\n\n';
const messages = [{ role: 'user', content: 'hello' }];

function client(port, options = {}) {
  return new NativeEngineClient({ endpoint: `http://127.0.0.1:${port}`, token: 'native-streaming-test-token', model: 'fixture', backend: 'fixture-cpu', ...options });
}
async function collect(iterable) { const frames = []; for await (const frame of iterable) frames.push(frame); return frames; }
const cancels = log => log.filter(entry => entry.url.startsWith('/v1/cancel/')).map(entry => entry.url.slice('/v1/cancel/'.length));
async function until(predicate, ms = 3000) { const end = Date.now() + ms; while (!predicate()) { if (Date.now() > end) throw new Error('condition not reached'); await delay(10); } }

test('NativeEngineClient defaults to a 1024-token answer and accepts 1-2048', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); response.end(delta('hi') + finish('stop') + DONE); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  assert.equal(native.maxTokens, 1024);
  await collect(native.generate({ requestId: 'req_default01', sessionId: 'ses_default01', messages }));
  assert.equal(engine.log.find(entry => entry.url === '/v1/chat/completions').body.max_tokens, 1024);
  assert.equal(client(engine.port, { maxTokens: 2048 }).maxTokens, 2048);
  assert.equal(client(engine.port, { maxTokens: 1 }).maxTokens, 1);
  for (const maxTokens of [0, 2049, '64', 1.5, null]) assert.throws(() => client(engine.port, { maxTokens }), /1-2048/, String(maxTokens));
  assert.throws(() => client(engine.port, { firstTokenTimeoutMs: 999 }), /first-token timeout/);
  assert.throws(() => client(engine.port, { idleTimeoutMs: 1800001 }), /idle timeout/);
  assert.throws(() => client(engine.port, { totalTimeoutMs: 'soon' }), /total timeout/);
  const defaults = client(engine.port);
  assert.deepEqual([defaults.firstTokenTimeoutMs, defaults.idleTimeoutMs, defaults.totalTimeoutMs], [900000, 120000, 1800000]);
});

test('NativeEngineClient yields text before the engine finishes, then length and real usage on done', async t => {
  let release; const released = new Promise(resolve => { release = resolve; });
  const engine = await fakeEngine(async ({ response, head }) => {
    head(); response.write(delta('Hello') + delta(' wor'));
    await released; // the rest is only sent once the client has shown the first words
    response.end(delta('ld <') + finish('length', { prompt_tokens: 41, completion_tokens: 3 }) + DONE);
  }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const frames = [];
  for await (const frame of native.generate({ requestId: 'req_stream01', sessionId: 'ses_stream01', messages })) { frames.push(frame); if (frames.length === 1) release(); }
  assert.deepEqual(frames[0], { kind: 'text_delta', text: 'Hello' });
  assert.equal(frames.filter(frame => frame.kind === 'text_delta').map(frame => frame.text).join(''), 'Hello world <');
  // The held `<` is flushed before `done`, so `done` really is last.
  assert.deepEqual(frames.at(-1), { kind: 'done', finish_reason: 'length', usage: { prompt_tokens: 41, completion_tokens: 3 } });
  assert.deepEqual(cancels(engine.log), [], 'a finished generation is not cancelled');
});

test('NativeEngineClient falls back to a counted usage when the engine omits it', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); response.end(delta('a') + delta('b') + finish('stop') + DONE); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const frames = await collect(native.generate({ requestId: 'req_usage01', sessionId: 'ses_usage01', messages }));
  assert.deepEqual(frames.at(-1), { kind: 'done', finish_reason: 'stop', usage: { completion_tokens: 2 } });
  const garbage = await fakeEngine(async ({ response, head }) => { head(); response.end(delta('a') + finish('stop', { prompt_tokens: -4, completion_tokens: 'many' }) + DONE); }); t.after(garbage.close);
  const other = client(garbage.port); t.after(() => other.shutdown());
  assert.deepEqual((await collect(other.generate({ requestId: 'req_usage02', sessionId: 'ses_usage02', messages }))).at(-1).usage, { completion_tokens: 1 });
});

test('NativeEngineClient tolerates a prefill longer than the stall limit', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); await delay(1600); response.end(delta('late but fine') + finish('stop') + DONE); }); t.after(engine.close);
  const native = client(engine.port, { firstTokenTimeoutMs: 5000, idleTimeoutMs: 1000 }); t.after(() => native.shutdown());
  const frames = await collect(native.generate({ requestId: 'req_prefill01', sessionId: 'ses_prefill01', messages }));
  assert.equal(frames[0].text, 'late but fine');
});

test('NativeEngineClient times out a prefill with no first token and cancels it in the engine', async t => {
  const engine = await fakeEngine(async ({ head }) => { head(); }); t.after(engine.close);
  const native = client(engine.port, { firstTokenTimeoutMs: 1000, idleTimeoutMs: 1000 }); t.after(() => native.shutdown());
  const started = Date.now();
  await assert.rejects(collect(native.generate({ requestId: 'req_first01', sessionId: 'ses_first01', messages })), error => error.code === 'engine_timeout' && error.reason === 'first_token');
  assert.ok(Date.now() - started < 4000);
  await until(() => cancels(engine.log).length === 1); assert.deepEqual(cancels(engine.log), ['req-1']);
});

test('NativeEngineClient times out a decode that stalls between frames', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); response.write(delta('one')); }); t.after(engine.close);
  const native = client(engine.port, { firstTokenTimeoutMs: 10000, idleTimeoutMs: 1000 }); t.after(() => native.shutdown());
  const frames = [];
  await assert.rejects(async () => { for await (const frame of native.generate({ requestId: 'req_stall01', sessionId: 'ses_stall01', messages })) frames.push(frame); }, error => error.code === 'engine_timeout' && error.reason === 'stalled');
  assert.deepEqual(frames, [{ kind: 'text_delta', text: 'one' }]);
  await until(() => cancels(engine.log).length === 1);
});

test('NativeEngineClient enforces the overall ceiling on a slow but steady stream', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); while (!response.destroyed) { response.write(delta('.')); await delay(200); } }); t.after(engine.close);
  const native = client(engine.port, { firstTokenTimeoutMs: 5000, idleTimeoutMs: 1000, totalTimeoutMs: 1500 }); t.after(() => native.shutdown());
  await assert.rejects(collect(native.generate({ requestId: 'req_total01', sessionId: 'ses_total01', messages })), error => error.code === 'engine_timeout' && error.reason === 'total');
  await until(() => cancels(engine.log).length === 1);
});

test('NativeEngineClient cancel during prefill reaches the engine by request id', async t => {
  const engine = await fakeEngine(async ({ head }) => { head(); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const running = collect(native.generate({ requestId: 'req_cancel01', sessionId: 'ses_cancel01', messages }));
  await until(() => native.active.get('req_cancel01')?.nativeRequestId === 'req-1');
  assert.equal(native.cancel('req_cancel01'), true);
  await assert.rejects(running, error => error.code === 'cancelled');
  await until(() => cancels(engine.log).length === 1); assert.deepEqual(cancels(engine.log), ['req-1']);
});

test('NativeEngineClient caller abort is a cancel that reaches the engine', async t => {
  const engine = await fakeEngine(async ({ head }) => { head(); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const abort = new AbortController();
  const running = collect(native.generate({ requestId: 'req_abort01', sessionId: 'ses_abort01', messages, signal: abort.signal }));
  await until(() => native.active.get('req_abort01')?.nativeRequestId);
  abort.abort();
  await assert.rejects(running, error => error.code === 'cancelled');
  await until(() => cancels(engine.log).length === 1);
});

test('NativeEngineClient cancel that races the SSE head still reaches the engine', async t => {
  let arrived = false;
  const engine = await fakeEngine(async ({ head }) => { arrived = true; await delay(300); head(); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const running = collect(native.generate({ requestId: 'req_race01', sessionId: 'ses_race01', messages }));
  await until(() => arrived);
  const started = Date.now(); native.cancel('req_race01');
  await assert.rejects(running, error => error.code === 'cancelled');
  assert.ok(Date.now() - started < 250, 'the caller is not kept waiting for the head');
  await until(() => cancels(engine.log).length === 1); assert.deepEqual(cancels(engine.log), ['req-1']);
});

test('NativeEngineClient stops the engine when the consumer abandons the stream', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); response.write(delta('Sure. ') + delta('<tool_call>')); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  for await (const frame of native.generate({ requestId: 'req_leave01', sessionId: 'ses_leave01', messages })) { assert.equal(frame.text, 'Sure. '); break; }
  await until(() => cancels(engine.log).length === 1);
  assert.equal(native.active.size, 0);
});

test('NativeEngineClient replaces an evicted native session once and retries', async t => {
  const engine = await fakeEngine(async ({ response, head, body }) => {
    head();
    if (body.session_id === 'sess-1') { response.end('data: {"error":{"code":"not_found"}}\n\ndata: [DONE]\n\n'); return; }
    response.end(delta('fresh') + finish('stop') + DONE);
  }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const frames = await collect(native.generate({ requestId: 'req_evict01', sessionId: 'ses_evict01', messages }));
  assert.deepEqual(frames.map(frame => frame.kind), ['text_delta', 'done']);
  assert.equal(native.sessions.get('ses_evict01'), 'sess-2');
  assert.deepEqual(engine.log.filter(entry => entry.url === '/v1/chat/completions').map(entry => entry.body.session_id), ['sess-1', 'sess-2']);
  assert.deepEqual(cancels(engine.log), []);
});

test('NativeEngineClient retries an evicted session at most once', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); response.end('data: {"error":{"code":"not_found"}}\n\ndata: [DONE]\n\n'); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  await assert.rejects(collect(native.generate({ requestId: 'req_evict02', sessionId: 'ses_evict02', messages })), error => error.code === 'not_found');
  assert.equal(engine.log.filter(entry => entry.url === '/v1/chat/completions').length, 2);
  // Other engine errors are not retried.
  const busy = await fakeEngine(async ({ response, head }) => { head(); response.end('data: {"error":{"code":"busy"}}\n\ndata: [DONE]\n\n'); }); t.after(busy.close);
  const other = client(busy.port); t.after(() => other.shutdown());
  await assert.rejects(collect(other.generate({ requestId: 'req_evict03', sessionId: 'ses_evict03', messages })), error => error.code === 'busy');
  assert.equal(busy.log.filter(entry => entry.url === '/v1/chat/completions').length, 1);
});

test('NativeEngineClient also replaces a session refused with HTTP 404', async t => {
  let calls = 0;
  const engine = await fakeEngine(async ({ response, head }) => {
    if (++calls === 1) { response.writeHead(404, { 'content-type': 'application/json' }); response.end('{"error":{"code":"not_found"}}'); return; }
    head(); response.end(delta('ok') + finish('stop') + DONE);
  }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const frames = await collect(native.generate({ requestId: 'req_evict04', sessionId: 'ses_evict04', messages }));
  assert.equal(frames[0].text, 'ok'); assert.equal(native.sessions.get('ses_evict04'), 'sess-2');
});

test('NativeEngineClient hides deep-mode reasoning from the answer text', async t => {
  const engine = await fakeEngine(async ({ response, head }) => { head(); response.end(delta('weighing it') + delta('</thi') + delta('nk>\n\n') + delta('Answer.') + finish('stop') + DONE); }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const frames = await collect(native.generate({ requestId: 'req_deep01', sessionId: 'ses_deep01', messages, mode: 'deep' }));
  assert.equal(frames.filter(frame => frame.kind === 'text_delta').map(frame => frame.text).join(''), '\n\nAnswer.');
  assert.equal(frames.filter(frame => frame.kind === 'reasoning_delta').map(frame => frame.text).join(''), 'weighing it');
});

test('ConversationController over the streaming client: whitespace-led call runs, text-led call is refused', async t => {
  const { ConversationController } = await import('../../host/agent/controller.mjs');
  const call = '<tool_call>\n<function=time.now>\n<parameter=format>\nlocal\n</parameter>\n</function>\n</tool_call>';
  let script = [];
  const engine = await fakeEngine(async ({ response, head, body }) => {
    head();
    const answered = body.messages.some(message => message.role === 'tool');
    const parts = answered ? ['It is ', 'noon.'] : script;
    response.end(parts.map(delta).join('') + finish('stop', { prompt_tokens: 100, completion_tokens: parts.length }) + DONE);
  }); t.after(engine.close);
  const native = client(engine.port); t.after(() => native.shutdown());
  const controller = new ConversationController({ engine: native });

  // Split inside the tag on purpose: only whitespace precedes the call.
  script = ['\n', '\n<to', 'ol_call>', call.slice('<tool_call>'.length)];
  const events = [];
  const ok = await controller.runTurn({ sessionId: 'ses_ctrl01', requestId: 'req_ctrl01', message: 'time?', onEvent: event => events.push(event) });
  assert.equal(ok.state, 'COMPLETED'); assert.equal(ok.text, 'It is noon.');
  assert.ok(events.some(event => event.event === 'tool.completed'));
  assert.deepEqual(events.filter(event => event.event === 'message.delta').map(event => event.data.text), ['It is ', 'noon.']);

  script = ['Sure, checking. ', call];
  const refused = []; const mixed = await controller.runTurn({ sessionId: 'ses_ctrl02', requestId: 'req_ctrl02', message: 'time?', onEvent: event => refused.push(event) });
  assert.equal(mixed.state, 'FAILED'); assert.equal(mixed.error, 'mixed_tool_call_output');
  assert.equal(refused.some(event => event.event.startsWith('tool.')), false, 'the call after streamed text never runs');
  assert.equal(refused.filter(event => event.event === 'message.delta').map(event => event.data.text).join(''), 'Sure, checking. ');

  // Quoting tool-like tags (a file, a tool result) is an ordinary answer...
  script = ['The file contains <tool_', 'response>{"ok":true}</tool_response> and <tool_calls>.'];
  const quoted = await controller.runTurn({ sessionId: 'ses_ctrl03', requestId: 'req_ctrl03', message: 'what is in it?', onEvent: () => {} });
  assert.equal(quoted.state, 'COMPLETED'); assert.equal(quoted.text, 'The file contains <tool_response>{"ok":true}</tool_response> and <tool_calls>.');
  // ...but a real call after such prose is still mixed output and never runs.
  script = ['Found <tool_response>. ', call];
  const after = []; const quotedThenCall = await controller.runTurn({ sessionId: 'ses_ctrl04', requestId: 'req_ctrl04', message: 'time?', onEvent: event => after.push(event) });
  assert.equal(quotedThenCall.error, 'mixed_tool_call_output'); assert.equal(after.some(event => event.event.startsWith('tool.')), false);
});
