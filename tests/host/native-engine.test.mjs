import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import http from 'node:http';
import { NativeEngineClient } from '../../host/engine/native-engine-client.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';

const root = join(import.meta.dirname, '..', '..');
const command = (file, args, options = {}) => new Promise((resolve, reject) => { const child = spawn(file, args, { ...options, stdio: ['ignore', 'pipe', 'pipe'] }); let stdout = ''; let stderr = ''; child.stdout.on('data', chunk => { stdout += chunk; }); child.stderr.on('data', chunk => { stderr += chunk; }); child.once('error', reject); child.once('close', (code, signal) => code === 0 ? resolve({ stdout, stderr }) : reject(new Error(`${file} failed (${code ?? signal}): ${stderr}`))); });

async function buildFixture() {
  const buildDir = await mkdtemp(join(tmpdir(), 'lae-native-client-'));
  await command('cmake', ['-S', root, '-B', buildDir, '-DCMAKE_BUILD_TYPE=Release']);
  await command('cmake', ['--build', buildDir, '--target', 'lae-engine', '-j2']);
  return join(buildDir, 'native', process.platform === 'win32' ? 'lae-engine.exe' : 'lae-engine');
}

async function startFixture(executable, token) {
  const child = spawn(executable, ['serve', '--port', '0', '--token-stdin'], { stdio: ['pipe', 'pipe', 'pipe'] }); child.stdin.end(`${token}\n`); let output = ''; let error = '';
  child.stderr.on('data', chunk => { error += chunk; });
  const ready = await new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error(`fixture readiness timeout: ${error}`)), 10000); child.stdout.on('data', chunk => { output += chunk; for (const line of output.split(/\r?\n/)) { try { const value = JSON.parse(line); if (value.event === 'ready') { clearTimeout(timer); resolve(value); return; } } catch {} } }); child.once('error', reject); child.once('exit', (code, signal) => reject(new Error(`fixture exited (${code ?? signal}): ${error}`))); });
  return { child, port: ready.port };
}

function stop(child) { return new Promise(resolve => { if (child.exitCode !== null) return resolve(); child.once('close', resolve); child.kill('SIGTERM'); }); }

test('NativeEngineClient integrates authenticated native fixture streaming, sessions, cancellation, and clean shutdown', async t => {
  if (process.platform === 'win32' && !existsSync('cmake.exe')) return t.skip('cmake unavailable');
  if (process.platform !== 'win32' && !existsSync('/usr/bin/cmake') && !existsSync('/opt/homebrew/bin/cmake')) return t.skip('cmake unavailable');
  const executable = await buildFixture(); assert.equal(existsSync(executable), true); const engineToken = randomBytes(24).toString('base64url'); const fixture = await startFixture(executable, engineToken); t.after(() => stop(fixture.child));
  const client = new NativeEngineClient({ endpoint: `http://127.0.0.1:${fixture.port}`, token: engineToken, model: 'fixture', backend: 'fixture-cpu', timeoutMs: 10000 }); t.after(() => client.shutdown());
  const health = await client.health(); assert.equal(health.ready, true); assert.equal(health.backend, 'fixture-cpu/0.1.0'); assert.equal(health.model, 'fixture'); assert.equal((await client.waitReady()).ready, true);
  const mismatched = new NativeEngineClient({ endpoint: `http://127.0.0.1:${fixture.port}`, token: engineToken, model: 'qwen35-9b-q4-k-m', backend: 'cpu', timeoutMs: 10000 }); t.after(() => mismatched.shutdown());
  await assert.rejects(() => mismatched.health(), /identity does not match/);
  const controller = new ConversationController({ engine: client }); const session = controller.createSession('ses_native01'); const events = [];
  const result = await controller.runTurn({ sessionId: session.id, requestId: 'req_native01', message: 'hello', onEvent: event => events.push(event) });
  assert.equal(result.state, 'COMPLETED'); assert.match(result.text, /fixture response/); assert.ok(client.sessions.has(session.id)); assert.ok(events.some(event => event.event === 'message.completed'));
  const nativeResponse = await fetch(`${client.baseUrl}/v1/chat/completions`, { method: 'POST', headers: client.headers({ 'content-type': 'application/json' }), body: JSON.stringify({ model: 'fixture', session_id: client.sessions.get(session.id), messages: [{ role: 'user', content: 'cancel this native request' }], stream: true, max_tokens: 64 }) });
  assert.equal(nativeResponse.ok, true); const nativeRequestId = nativeResponse.headers.get('x-request-id'); assert.match(nativeRequestId ?? '', /^[A-Za-z0-9_-]+$/);
  assert.equal(await client.postCancel(nativeRequestId), true);
  const nativeBody = await nativeResponse.text(); assert.match(nativeBody, /finish_reason":"cancelled/);
  const cancelled = controller.runTurn({ sessionId: session.id, requestId: 'req_native02', message: 'hello', onEvent: () => {} }); setTimeout(() => controller.cancel('req_native02'), 20); const cancelledResult = await cancelled;
  assert.equal(cancelledResult.state, 'CANCELLED'); assert.equal(controller.state(session.id), 'CANCELLED');
  assert.equal(await client.deleteSession(session.id), true); assert.equal(client.sessions.has(session.id), false);
});

test('NativeEngineClient requires explicit model/backend identity', () => {
  assert.throws(() => new NativeEngineClient({ endpoint: 'http://127.0.0.1:1234', token: 'native-client-test-token' }), /model identity is required/);
  assert.throws(() => new NativeEngineClient({ endpoint: 'http://localhost:1234', token: 'native-client-test-token', model: 'fixture', backend: 'fixture-cpu' }), /numeric loopback/);
});

test('NativeEngineClient binds the 256-token ceiling and bearer header', () => {
  const client = new NativeEngineClient({ endpoint: 'http://127.0.0.1:1234', token: 'native-client-boundary-token', model: 'fixture', backend: 'fixture-cpu', maxTokens: 256 });
  assert.equal(client.maxTokens, 256);
  assert.throws(() => new NativeEngineClient({ endpoint: 'http://127.0.0.1:1234', token: 'native-client-boundary-token', model: 'fixture', backend: 'fixture-cpu', maxTokens: 257 }), /1-256/);
  const headers = client.headers({ authorization: 'Bearer attacker', Authorization: 'Bearer attacker-two', 'content-type': 'application/json' });
  assert.equal(headers.authorization, 'Bearer native-client-boundary-token');
  assert.equal(headers.Authorization, undefined);
  assert.equal(headers['content-type'], 'application/json');
});

test('NativeEngineClient readiness passes the caller deadline to each request', async () => {
  const client = new NativeEngineClient({ endpoint: 'http://127.0.0.1:1234', token: 'native-client-readiness-token', model: 'fixture', backend: 'fixture-cpu' });
  const observed = [];
  client.ready = async options => { observed.push(options.timeoutMs); return { ready: true, lifecycle: 'READY' }; };
  const result = await client.waitReady({ timeoutMs: 25, intervalMs: 1 });
  assert.equal(result.ready, true);
  assert.equal(observed.length, 1);
  assert.ok(observed[0] <= 25);
});

test('NativeEngineClient timeout covers a stalled SSE response body', async t => {
  const server = http.createServer((request, response) => {
    if (request.url === '/v1/sessions') { response.writeHead(201, { 'content-type': 'application/json' }); response.end('{"id":"sess-timeout"}'); return; }
    if (request.url === '/v1/chat/completions') { response.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': 'req-native-timeout' }); response.flushHeaders(); response.write(': waiting\n\n'); return; }
    if (request.url === '/v1/cancel/req-native-timeout') { response.writeHead(200, { 'content-type': 'application/json' }); response.end('{"cancelled":true}'); return; }
    response.writeHead(404); response.end();
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  t.after(() => new Promise(resolve => server.close(resolve)));
  const client = new NativeEngineClient({ endpoint: `http://127.0.0.1:${server.address().port}`, token: 'native-stream-timeout-token', model: 'qwen35-9b-q4-k-m', backend: 'cpu', timeoutMs: 1000, maxTokens: 2 });
  t.after(() => client.shutdown());
  await assert.rejects(async () => { for await (const _frame of client.generate({ requestId: 'req_timeout01', sessionId: 'ses_timeout01', messages: [{ role: 'user', content: 'hello' }] })) {} }, error => error?.code === 'engine_timeout');
});

test('NativeEngineClient rejects oversized JSON and SSE frames with typed errors', async t => {
  const server = http.createServer((request, response) => {
    if (request.url === '/v1/sessions') { response.writeHead(201, { 'content-type': 'application/json' }); response.end('{"id":"sess-bounded"}'); return; }
    if (request.url === '/v1/chat/completions') {
      response.writeHead(200, { 'content-type': 'text/event-stream', 'x-request-id': 'req-bounded' });
      response.end(`data: ${'x'.repeat(300 * 1024)}\n\n`); return;
    }
    response.writeHead(404); response.end();
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  t.after(() => new Promise(resolve => server.close(resolve)));
  const client = new NativeEngineClient({ endpoint: `http://127.0.0.1:${server.address().port}`, token: 'native-stream-bounded-token', model: 'fixture', backend: 'fixture-cpu', timeoutMs: 3000, maxTokens: 2 });
  t.after(() => client.shutdown());
  await assert.rejects(async () => { for await (const _frame of client.generate({ requestId: 'req_bound01', sessionId: 'ses_bound01', messages: [{ role: 'user', content: 'hello' }] })) {} }, error => error?.code === 'engine_stream_line_too_large');
});

test('launcher rejects invalid or conflicting engine selection without fixture fallback', () => {
  const invalid = spawnSync(process.execPath, ['lae-host.mjs'], { cwd: root, env: { ...process.env, LAE_ENGINE_MODE: 'unexpected' }, encoding: 'utf8' });
  assert.notEqual(invalid.status, 0); assert.match(`${invalid.stderr}${invalid.stdout}`, /invalid LAE_ENGINE_MODE/);
  const conflict = spawnSync(process.execPath, ['lae-host.mjs'], { cwd: root, env: { ...process.env, LAE_ENGINE_MODE: 'fixture', LAE_ENGINE_ENDPOINT: 'http://127.0.0.1:1', LAE_ENGINE_TOKEN: 'not-used-token-123456' }, encoding: 'utf8' });
  assert.notEqual(conflict.status, 0); assert.match(`${conflict.stderr}${conflict.stdout}`, /native engine settings supplied/);
});
