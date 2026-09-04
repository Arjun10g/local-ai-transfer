import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseStrictJson, parseToolCall, validateToolResult, EnvelopeError, makeToolResult } from '../../host/agent/tool-envelope.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { FixtureEngineClient } from '../../host/engine/fixture-engine.mjs';
import { validateToolArguments } from '../../host/tools/local/argument-validation.mjs';
import { WorkspacePolicy, validateRelativePath } from '../../host/tools/local/workspace-policy.mjs';
import { createFilesystemTools } from '../../host/tools/local/filesystem.mjs';
import { createSystemTools } from '../../host/tools/local/system-tools.mjs';

const call = (name, arguments_, id = 'call_adv01') => ({ id, name, arguments: arguments_ });
const auth = token => ({ authorization: `Bearer ${token}` });
const bodyHeaders = token => ({ ...auth(token), 'content-type': 'application/json' });
const resultText = result => JSON.parse(result.content[0].text);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'lae-adversarial-'));
  await writeFile(join(root, 'notes.txt'), 'safe workspace text\n', 'utf8');
  return root;
}

function oneToolEngine(toolCall, continuation = 'continued safely') {
  return {
    async *generate({ messages }) {
      if (!messages.some(message => message.role === 'tool')) {
        yield { kind: 'tool_call_chunk', text: JSON.stringify(toolCall) };
        return;
      }
      yield { kind: 'text_delta', text: continuation };
      yield { kind: 'done', usage: { prompt_tokens: 1, completion_tokens: 1 } };
    }
  };
}

async function waitForEvent(events, name) {
  for (;;) {
    const found = events.find(event => event.event === name);
    if (found) return found;
    await sleep(1);
  }
}

test('strict envelope parser rejects malformed, duplicate, trailing, and resource-heavy inputs', () => {
  assert.deepEqual(parseStrictJson('{"ok":true,"n":-1.25e2}'), { ok: true, n: -125 });
  const invalid = [
    '{"a":1,"a":2}', '{"a":1} trailing', '{"a":NaN}', '{"a":Infinity}',
    '{"a":"\\x"}', '{"a": [1,]}', '{"a": 01}', '{"a": "unterminated}',
    '{"a":' + '['.repeat(9) + '0' + ']'.repeat(9) + '}',
    '{' + Array.from({ length: 33 }, (_, i) => `"k${i}":${i}`).join(',') + '}',
    '[' + Array.from({ length: 33 }, () => '0').join(',') + ']'
  ];
  for (const input of invalid) assert.throws(() => parseStrictJson(input), EnvelopeError, input);
  assert.throws(() => parseStrictJson(JSON.stringify({ value: '😀'.repeat(5000) })), /exceeds/);
});

test('tool-call validation rejects prototype keys, shape confusion, invalid IDs, and oversized arguments', () => {
  assert.deepEqual(parseToolCall('{"id":"call_ok01","name":"time.now","arguments":{}}').name, 'time.now');
  const invalid = [
    '{"id":"call_ok01","name":"time.now","arguments":[],"x":1}',
    '{"id":"call_ok01","name":"Time.now","arguments":{}}',
    '{"id":"bad id","name":"time.now","arguments":{}}',
    '{"id":"call_ok01","name":"time.now","arguments":null}',
    '{"id":"call_ok01","name":"time.now","arguments":{' + Array.from({ length: 33 }, (_, i) => `"k${i}":${i}`).join(',') + '}}'
  ];
  for (const input of invalid) assert.throws(() => parseToolCall(input), EnvelopeError, input);
  // Parsed objects are normalized without invoking Object.prototype setters.
  const parsed = parseToolCall('{"id":"call_ok01","name":"time.now","arguments":{"format":"utc"}}');
  assert.equal(Object.getPrototypeOf(parsed.arguments), Object.prototype);
});

test('tool-call parser preserves __proto__ as inert own data', () => {
  const parsed = parseToolCall('{"id":"call_proto1","name":"time.now","arguments":{"__proto__":{"polluted":true}}}');
  assert.equal(Object.hasOwn(parsed.arguments, '__proto__'), true);
  assert.equal(parsed.arguments.polluted, undefined);
  assert.equal(Object.getPrototypeOf(parsed.arguments), Object.prototype);
});

test('tool-result validation is strict and output construction clamps untrusted text', () => {
  const valid = makeToolResult({ id: 'call_result01', name: 'time.now', text: 'ok' });
  assert.equal(validateToolResult(valid).status, 'ok');
  for (const value of [
    { ...valid, status: 'running' },
    { ...valid, metadata: { truncated: false, duration_ms: -1 } },
    { ...valid, metadata: { truncated: false, duration_ms: 1, extra: true } },
    { ...valid, content: [{ type: 'html', text: '<script>' }] },
    { ...valid, content: [{ type: 'text', text: 'x'.repeat(65537) }] }
  ]) assert.throws(() => validateToolResult(value), /invalid|unknown/);
  const clamped = makeToolResult({ id: 'call_result01', name: 'time.now', text: 'x'.repeat(100000) });
  assert.equal(clamped.content[0].text.length, 65536);
});

test('controller rejects unknown tools and malformed model calls without executing anything', async () => {
  let executions = 0;
  const unknown = new ConversationController({ engine: oneToolEngine(call('not.registered', {}, 'call_unknown1')) });
  const unknownResult = await unknown.runTurn({ sessionId: 'ses_unknown', requestId: 'req_unknown', message: 'run', onEvent: () => {} });
  assert.equal(unknownResult.error, 'unknown_tool');
  const malformed = new ConversationController({
    engine: { async *generate() { yield { kind: 'tool_call_chunk', text: '{"id":"bad id","name":"time.now","arguments":{}}' }; } },
    toolRegistry: { 'time.now': { name: 'time.now', execute: async () => { executions++; } } }
  });
  const malformedResult = await malformed.runTurn({ sessionId: 'ses_malformed', requestId: 'req_malformed', message: 'run', onEvent: () => {} });
  assert.equal(malformedResult.state, 'FAILED');
  assert.equal(executions, 0);
});

test('controller bounds multi-step tool loops and oversized tool-call chunks', async () => {
  let executions = 0;
  const looping = new ConversationController({
    maxToolCalls: 2,
    engine: { async *generate() { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('test.loop', {}, 'call_loop01')) }; } },
    toolRegistry: { 'test.loop': { name: 'test.loop', execute: async value => { executions++; return makeToolResult({ id: value.id, name: value.name, text: 'loop' }); } } }
  });
  const loopResult = await looping.runTurn({ sessionId: 'ses_loop01', requestId: 'req_loop01', message: 'loop', onEvent: () => {} });
  assert.equal(loopResult.error, 'tool_call_limit_exceeded');
  assert.equal(executions, 2);

  const oversized = new ConversationController({ engine: { async *generate() { yield { kind: 'tool_call_chunk', text: ' '.repeat(32769) }; } } });
  const oversizedResult = await oversized.runTurn({ sessionId: 'ses_large1', requestId: 'req_large1', message: 'large', onEvent: () => {} });
  assert.equal(oversizedResult.error, 'tool_call_too_large');
});

test('confirmation expires into denial and cannot be replayed', async () => {
  let executions = 0;
  const controller = new ConversationController({
    confirmationTimeoutMs: 10,
    engine: oneToolEngine(call('test.confirm', {}, 'call_expire1'), 'denied after expiry'),
    toolRegistry: { 'test.confirm': { name: 'test.confirm', risk_tier: 'T2', requires_confirmation: true, execute: async () => { executions++; return makeToolResult({ id: 'call_expire1', name: 'test.confirm' }); } } }
  });
  const events = [];
  const run = controller.runTurn({ sessionId: 'ses_expire', requestId: 'req_expire', message: 'confirm', onEvent: event => events.push(event) });
  const required = await waitForEvent(events, 'tool.confirmation_required');
  const result = await run;
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 0);
  assert.equal(controller.confirm(required.data.confirmation_id, true, { requestId: 'req_expire', callId: 'call_expire1' }), false);
  assert.equal(controller.pending.size, 0);
});

test('confirmation approval is bound to request and call and cancellation wins', async () => {
  let executions = 0;
  const controller = new ConversationController({
    confirmationTimeoutMs: 1000,
    engine: oneToolEngine(call('test.confirm', {}, 'call_bind01')),
    toolRegistry: { 'test.confirm': { name: 'test.confirm', risk_tier: 'T2', requires_confirmation: true, execute: async () => { executions++; return makeToolResult({ id: 'call_bind01', name: 'test.confirm' }); } } }
  });
  const events = [];
  const run = controller.runTurn({ sessionId: 'ses_bind01', requestId: 'req_bind01', message: 'confirm', onEvent: event => events.push(event) });
  const required = await waitForEvent(events, 'tool.confirmation_required');
  assert.equal(controller.confirm(required.data.confirmation_id, true), false);
  assert.equal(controller.confirm(required.data.confirmation_id, true, { requestId: 'req_other', callId: 'call_bind01' }), false);
  assert.equal(controller.cancel('req_bind01'), true);
  assert.equal((await run).state, 'CANCELLED');
  assert.equal(executions, 0);
  assert.equal(controller.confirm(required.data.confirmation_id, true, { requestId: 'req_bind01', callId: 'call_bind01' }), false);
});

test('controller treats a tool result with the wrong correlation ID as a failure', async () => {
  const controller = new ConversationController({
    engine: oneToolEngine(call('test.result', {}, 'call_expected')),
    toolRegistry: { 'test.result': { name: 'test.result', execute: async () => makeToolResult({ id: 'call_other', name: 'test.result' }) } }
  });
  const result = await controller.runTurn({ sessionId: 'ses_result', requestId: 'req_result', message: 'result', onEvent: () => {} });
  assert.equal(result.state, 'FAILED');
  assert.equal(result.error, 'tool_result_mismatch');
});

test('controller validates the full tool-result schema before continuing', async () => {
  let continuation = false;
  const controller = new ConversationController({
    engine: {
      async *generate({ messages }) {
        if (!messages.some(message => message.role === 'tool')) {
          yield { kind: 'tool_call_chunk', text: JSON.stringify(call('test.invalid_result', {}, 'call_invalid_result')) };
          return;
        }
        continuation = true;
        yield { kind: 'text_delta', text: 'must not continue' };
      }
    },
    toolRegistry: {
      'test.invalid_result': {
        name: 'test.invalid_result',
        execute: async () => ({ id: 'call_invalid_result', name: 'test.invalid_result', status: 'running', content: [], metadata: { truncated: false, duration_ms: 0 } })
      }
    }
  });
  const result = await controller.runTurn({ sessionId: 'ses_invalid_result', requestId: 'req_invalid_result', message: 'run', onEvent: () => {} });
  assert.equal(result.state, 'FAILED');
  assert.equal(result.error, 'invalid_tool_result');
  assert.equal(continuation, false);
});

test('provider/tool failures fail closed without a continuation or hidden fallback', async () => {
  let continuation = false;
  const controller = new ConversationController({
    engine: {
      async *generate({ messages }) {
        if (!messages.some(message => message.role === 'tool')) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call('test.failure', {}, 'call_fail01')) }; return; }
        continuation = true;
        yield { kind: 'text_delta', text: 'must not continue' };
      }
    },
    toolRegistry: { 'test.failure': { name: 'test.failure', execute: async () => { throw Object.assign(new Error('provider timeout'), { code: 'provider_timeout' }); } } }
  });
  const result = await controller.runTurn({ sessionId: 'ses_fail01', requestId: 'req_fail01', message: 'try provider', onEvent: () => {} });
  assert.equal(result.state, 'FAILED');
  assert.equal(continuation, false);
  assert.equal(result.error, 'provider_timeout');
});

test('controller enforces each tool definition timeout and aborts the operation', async () => {
  let aborted = false;
  const controller = new ConversationController({
    engine: oneToolEngine(call('test.timeout', {}, 'call_timeout1')),
    toolRegistry: {
      'test.timeout': {
        name: 'test.timeout',
        timeout_ms: 10,
        execute: async value => await new Promise(resolve => {
          value.signal.addEventListener('abort', () => { aborted = true; resolve(makeToolResult({ id: value.id, name: value.name, status: 'cancelled' })); }, { once: true });
        })
      }
    }
  });
  const started = Date.now();
  const result = await controller.runTurn({ sessionId: 'ses_timeout1', requestId: 'req_timeout1', message: 'run', onEvent: () => {} });
  assert.equal(result.state, 'FAILED');
  assert.equal(result.error, 'tool_timeout');
  assert.equal(aborted, true);
  assert.ok(Date.now() - started < 500);
});

test('filesystem argument schemas reject injection-shaped, oversized, non-integer, and extra arguments', () => {
  const names = ['fs.list', 'fs.read_text', 'fs.search_text', 'fs.write_new', 'fs.apply_patch', 'clipboard.write', 'app.open', 'browser.open_url'];
  for (const name of names) assert.throws(() => validateToolArguments(name, { unknown: true }), error => error.code === 'invalid_tool_arguments');
  assert.throws(() => validateToolArguments('fs.read_text', { workspace_id: 'project', path: 'x', max_bytes: 1.5 }), error => error.code === 'invalid_tool_arguments');
  assert.throws(() => validateToolArguments('fs.read_text', { workspace_id: 'project', path: 'x', offset_bytes: -1 }), error => error.code === 'invalid_tool_arguments');
  assert.throws(() => validateToolArguments('fs.read_text', { workspace_id: 'project', path: 'x\0y' }), error => error.code === 'invalid_tool_arguments');
  assert.throws(() => validateToolArguments('fs.write_new', { workspace_id: 'project', path: 'x', content: '😀'.repeat(20000) }), error => error.code === 'invalid_tool_arguments');
  assert.throws(() => validateToolArguments('fs.apply_patch', { workspace_id: 'project', path: 'x', base_sha256: 'a'.repeat(64), base_hash: 'b'.repeat(64), replacement: 'x' }), error => error.code === 'invalid_tool_arguments');
  assert.throws(() => validateToolArguments('fs.apply_patch', { workspace_id: 'project', path: 'x', base_sha256: 'a'.repeat(64), replacement: 'x', patch: 'y' }), error => error.code === 'invalid_tool_arguments');
});

test('workspace path policy rejects traversal, alternate separators, ADS, reserved names, and absolute paths', () => {
  for (const value of ['../secret', '..\\secret', 'a/../../secret', 'a\\..\\secret', './notes', 'a//b', 'notes.txt:stream', 'CON.txt', 'NUL', '/tmp/secret', 'C:/secret', '\\\\server\\share', '']) {
    assert.throws(() => validateRelativePath(value), error => error.code === 'invalid_path', value);
  }
  assert.equal(validateRelativePath('nested/notes.txt'), 'nested/notes.txt');
});

test('filesystem refuses all operations on a platform without descriptor-safe handles', async () => {
  const root = await fixture();
  const tools = createFilesystemTools(new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]), { platform: 'win32' });
  const calls = {
    'fs.list': { workspace_id: 'project', path: '' },
    'fs.read_text': { workspace_id: 'project', path: 'notes.txt' },
    'fs.search_text': { workspace_id: 'project', query: 'safe' },
    'fs.write_new': { workspace_id: 'project', path: 'new.txt', content: 'new' },
    'fs.apply_patch': { workspace_id: 'project', path: 'notes.txt', base_sha256: 'a'.repeat(64), replacement: 'new' }
  };
  for (const [name, arguments_] of Object.entries(calls)) await assert.rejects(() => tools[name].execute(call(name, arguments_, 'call_win01')), error => error.code === 'platform_path_safety_unavailable');
});

test('filesystem does not follow symlinks during search and preserves create-only 0600 writes', async () => {
  const root = await fixture();
  const outside = await fixture();
  const { symlink, stat } = await import('node:fs/promises');
  await writeFile(join(outside, 'secret.txt'), 'outside-secret', 'utf8');
  await symlink(outside, join(root, 'outside-link'));
  const tools = createFilesystemTools(new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]));
  const searched = resultText(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'outside-secret' })));
  assert.equal(searched.matches.length, 0);
  const created = resultText(await tools['fs.write_new'].execute(call('fs.write_new', { workspace_id: 'project', path: 'created.txt', content: 'created' })));
  assert.equal(created.created, true);
  assert.equal((await stat(join(root, 'created.txt'))).mode & 0o777, 0o600);
});

test.todo('KNOWN LIMITATION: parent-directory replacement races need descriptor-relative or Windows handle-relative primitives beyond current Node path APIs');

test('browser URL policy blocks non-HTTPS, credentials, file URLs, and private IPv4 before spawn', async () => {
  const tools = createSystemTools({ platform: 'linux', networkProvider: 'disabled', browserExecutable: process.execPath });
  for (const rawUrl of ['file:///etc/passwd', 'http://example.com', 'https://user:pass@example.com', 'https://localhost/', 'https://127.0.0.1/', 'https://10.0.0.1/', 'https://192.168.1.1/']) {
    await assert.rejects(() => tools['browser.open_url'].preview(call('browser.open_url', { url: rawUrl })), /unsafe_url|private_url/);
  }
  const preview = await tools['browser.open_url'].preview(call('browser.open_url', { url: 'https://example.com/a;echo unsafe' }));
  assert.deepEqual(preview, { destination: 'https://example.com/a;echo%20unsafe', provider: 'disabled', data_egress: 'external_navigation' });
  const disabled = resultText(await tools['browser.open_url'].execute(call('browser.open_url', { url: 'https://example.com/' })));
  assert.equal(disabled.code, 'provider_disabled');
  assert.equal(disabled.destination, 'https://example.com/');
});

test('browser provider process failure is returned as a typed failure and never retried through another provider', async () => {
  const tools = createSystemTools({ platform: 'linux', browserExecutable: '/definitely/not-a-real-browser', networkProvider: 'browser_open' });
  const failed = resultText(await tools['browser.open_url'].execute(call('browser.open_url', { url: 'https://example.com/' }, 'call_browser_fail')));
  assert.equal(failed.code, 'ENOENT');
});

test.todo('KNOWN GAP: browser URL policy must reject bracketed private IPv6 literals (for example https://[::1]/)');
test.todo('KNOWN GAP: browser URL policy must resolve DNS and reject hostnames that resolve to private/link-local addresses');

test('browser and app launches use argv boundaries rather than shell interpretation', async () => {
  const root = await mkdtemp(join(tmpdir(), 'lae-spawn-'));
  const capture = join(root, 'argv.txt');
  const marker = join(root, 'injected');
  const script = join(root, 'capture.mjs');
  await writeFile(script, `import { writeFile } from 'node:fs/promises'; await writeFile(${JSON.stringify(capture)}, process.argv.slice(2).join('\\n'));`, 'utf8');
  const url = `https://example.com/a;touch ${marker}`;
  const system = createSystemTools({ platform: 'linux', browserExecutable: process.execPath, networkProvider: 'browser_open', applications: { probe: { executable_id: 'probe', executable: process.execPath, args: [script, 'fixed-arg;not-shell'] } } });
  const browser = resultText(await system['browser.open_url'].execute(call('browser.open_url', { url })));
  assert.equal(browser.opened, true);
  const app = resultText(await system['app.open'].execute(call('app.open', { app_id: 'probe' })));
  assert.equal(app.opened, true);
  for (let i = 0; i < 50; i++) { try { await readFile(capture); break; } catch { await sleep(2); } }
  const captured = await readFile(capture, 'utf8');
  assert.match(captured, /fixed-arg;not-shell/);
  assert.equal(captured.includes(marker), false);
});

test('system info does not disclose the process environment and unsupported clipboard stays offline', async () => {
  const tools = createSystemTools({ platform: 'linux' });
  const info = resultText(await tools['system.get_info'].execute(call('system.get_info', {})));
  assert.equal(Object.hasOwn(info, 'env'), false);
  assert.equal(resultText(await tools['clipboard.read'].execute(call('clipboard.read', {}))).code, 'platform_unsupported');
  assert.equal(resultText(await tools['clipboard.write'].execute(call('clipboard.write', { text: 'secret' }))).code, 'platform_unsupported');
});

test('host rejects hostile origin/host/auth and malformed bounded session bodies', async t => {
  const engine = new FixtureEngineClient({ delayMs: 0 });
  const controller = new ConversationController({ engine });
  const host = new HostServer({ controller, engine, config: { host: { max_body_bytes: 1024 } } });
  const address = await host.listen(0);
  t.after(() => host.close());
  assert.equal((await fetch(`${address.url}/healthz`)).status, 200);
  assert.equal((await fetch(`${address.url}/api/status`)).status, 401);
  assert.equal(host.allowedRequest({ socket: { remoteAddress: '127.0.0.1' }, headers: { host: 'attacker.invalid' } }), false);
  assert.equal((await fetch(`${address.url}/api/status`, { headers: { ...auth(address.token), origin: 'https://127.0.0.1.evil' } })).status, 403);
  assert.equal((await fetch(`${address.url}/api/status`, { headers: { authorization: `Bearer ${address.token} wrong` } })).status, 401);
  const malformed = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: bodyHeaders(address.token), body: '{not-json' });
  assert.equal(malformed.status, 400);
  const oversized = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ padding: 'x'.repeat(2000) }) });
  assert.equal(oversized.status, 413);
  const sessionResponse = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: bodyHeaders(address.token), body: '{}' });
  const session = await sessionResponse.json();
  assert.match(session.session_id, /^[A-Za-z0-9_-]{8,96}$/);
  const noType = await fetch(`${address.url}/api/sessions`, { method: 'POST', headers: auth(address.token), body: '{}' }); assert.equal(noType.status, 415);
  const cancelNoType = await fetch(`${address.url}/api/cancel`, { method: 'POST', headers: auth(address.token), body: JSON.stringify({ request_id: 'req_valid01' }) }); assert.equal(cancelNoType.status, 415);
  const wrongType = await fetch(`${address.url}/api/tool-confirmations/cnf_missing`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ approved: 'yes', request_id: 'req_valid01', call_id: 'call_valid01' }) }); assert.equal(wrongType.status, 400);
  for (let attempt = 0; attempt < 20; attempt++) await fetch(`${address.url}/api/status`);
  assert.equal((await fetch(`${address.url}/api/status`)).status, 429);
});

test('HostServer strictly validates confirmation bodies and contains chat handler faults', async t => {
  const engine = new FixtureEngineClient({ delayMs: 0 }); const controller = new ConversationController({ engine }); const host = new HostServer({ controller, engine }); const address = await host.listen(0); t.after(() => host.close());
  const extra = await fetch(`${address.url}/api/tool-confirmations/cnf_missing`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ approved: true, request_id: 'req_valid01', call_id: 'call_valid01', extra: true }) });
  assert.equal(extra.status, 400);
  const badRequest = await fetch(`${address.url}/api/chat`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ session_id: 'ses_valid01', request_id: 'bad id', message: 'hello' }) });
  assert.equal(badRequest.status, 400); assert.equal((await badRequest.json()).error, 'invalid_request_id');
  const faulty = new HostServer({ controller: { runTurn: async () => { throw new Error('handler fault'); } }, engine }); const faultyAddress = await faulty.listen(0); t.after(() => faulty.close());
  const faultResponse = await fetch(`${faultyAddress.url}/api/chat`, { method: 'POST', headers: bodyHeaders(faultyAddress.token), body: JSON.stringify({ session_id: 'ses_valid01', request_id: 'req_valid01', message: 'hello' }) });
  assert.equal(faultResponse.status, 500); assert.equal((await faultResponse.json()).error, 'request_failed');
});

test('confirmation endpoint integration requires both correlation fields and rejects replay', async t => {
  const engine = oneToolEngine(call('test.confirm', {}, 'call_http01'));
  const controller = new ConversationController({ engine, confirmationTimeoutMs: 1000, toolRegistry: { 'test.confirm': { name: 'test.confirm', risk_tier: 'T2', requires_confirmation: true, execute: async value => makeToolResult({ id: value.id, name: value.name }) } } });
  const host = new HostServer({ controller, engine });
  const address = await host.listen(0);
  t.after(() => host.close());
  const events = [];
  const run = controller.runTurn({ sessionId: 'ses_http01', requestId: 'req_http01', message: 'confirm', onEvent: event => events.push(event) });
  const required = await waitForEvent(events, 'tool.confirmation_required');
  const missing = await fetch(`${address.url}/api/tool-confirmations/${required.data.confirmation_id}`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ approved: true }) });
  assert.equal(missing.status, 400);
  const mismatch = await fetch(`${address.url}/api/tool-confirmations/${required.data.confirmation_id}`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ approved: true, request_id: 'req_other01', call_id: 'call_http01' }) });
  assert.equal(mismatch.status, 404);
  const approved = await fetch(`${address.url}/api/tool-confirmations/${required.data.confirmation_id}`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ approved: true, request_id: 'req_http01', call_id: 'call_http01' }) });
  assert.equal(approved.status, 200);
  assert.equal((await run).state, 'COMPLETED');
  const replay = await fetch(`${address.url}/api/tool-confirmations/${required.data.confirmation_id}`, { method: 'POST', headers: bodyHeaders(address.token), body: JSON.stringify({ approved: true, request_id: 'req_http01', call_id: 'call_http01' }) });
  assert.equal(replay.status, 404);
});
