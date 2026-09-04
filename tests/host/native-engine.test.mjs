import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
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
  const child = spawn(executable, ['serve', '--port', '0', '--token', token], { stdio: ['ignore', 'pipe', 'pipe'] }); let output = ''; let error = '';
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
  const cancelled = controller.runTurn({ sessionId: session.id, requestId: 'req_native02', message: 'hello', onEvent: () => {} }); setTimeout(() => controller.cancel('req_native02'), 20); const cancelledResult = await cancelled;
  assert.equal(cancelledResult.state, 'CANCELLED'); assert.equal(controller.state(session.id), 'CANCELLED');
  assert.equal(await client.deleteSession(session.id), true); assert.equal(client.sessions.has(session.id), false);
});

test('NativeEngineClient requires explicit model/backend identity', () => {
  assert.throws(() => new NativeEngineClient({ endpoint: 'http://127.0.0.1:1234', token: 'native-client-test-token' }), /model identity is required/);
});

test('launcher rejects invalid or conflicting engine selection without fixture fallback', () => {
  const invalid = spawnSync(process.execPath, ['lae-host.mjs'], { cwd: root, env: { ...process.env, LAE_ENGINE_MODE: 'unexpected' }, encoding: 'utf8' });
  assert.notEqual(invalid.status, 0); assert.match(`${invalid.stderr}${invalid.stdout}`, /invalid LAE_ENGINE_MODE/);
  const conflict = spawnSync(process.execPath, ['lae-host.mjs'], { cwd: root, env: { ...process.env, LAE_ENGINE_MODE: 'fixture', LAE_ENGINE_ENDPOINT: 'http://127.0.0.1:1', LAE_ENGINE_TOKEN: 'not-used-token-123456' }, encoding: 'utf8' });
  assert.notEqual(conflict.status, 0); assert.match(`${conflict.stderr}${conflict.stdout}`, /native engine settings supplied/);
});
