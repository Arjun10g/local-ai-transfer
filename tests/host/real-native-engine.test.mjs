import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';

import { NativeEngineClient } from '../../host/engine/native-engine-client.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';

const root = join(import.meta.dirname, '..', '..');
const model = process.env.LAE_QWEN35_MODEL;
const size = process.env.LAE_QWEN35_MODEL_SIZE;
const sha256 = process.env.LAE_QWEN35_MODEL_SHA256;
const executable = process.env.LAE_QWEN35_ENGINE ?? join(root, 'out', 'build-real', 'native', process.platform === 'win32' ? 'lae-engine.exe' : 'lae-engine');
const enabled = Boolean(model && size && sha256 && existsSync(executable));

function stop(child) {
  return new Promise(resolveStop => {
    if (child.exitCode !== null) return resolveStop();
    const killTimer = setTimeout(() => child.kill('SIGKILL'), 30000);
    child.once('close', () => { clearTimeout(killTimer); resolveStop(); });
    child.kill('SIGTERM');
  });
}

async function startReal(token) {
  const child = spawn(executable, ['serve', '--port', '0', '--backend', 'cpu', '--model', resolve(model), '--size', size, '--sha256', sha256, '--context', '512', '--token-stdin'], { stdio: ['pipe', 'pipe', 'pipe'] }); child.stdin.end(`${token}\n`);
  let stdout = ''; let stderr = '';
  child.stderr.on('data', chunk => { stderr = `${stderr}${chunk}`.slice(-8192); });
  const ready = await new Promise((resolveReady, reject) => {
    const timer = setTimeout(() => reject(new Error(`real engine readiness timeout: ${stderr}`)), 120000);
    child.stdout.on('data', chunk => { stdout += chunk; for (const line of stdout.split(/\r?\n/)) { try { const value = JSON.parse(line); if (value.event === 'ready') { clearTimeout(timer); resolveReady(value); return; } } catch {} } });
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', (code, signal) => { clearTimeout(timer); reject(new Error(`real engine exited (${code ?? signal}): ${stderr}`)); });
  });
  return { child, port: ready.port, diagnostics: () => stderr };
}

test('real Qwen model runs through NativeEngineClient and ConversationController', { skip: !enabled, timeout: 360000 }, async t => {
  const token = randomBytes(24).toString('base64url');
  const engine = await startReal(token); t.after(() => stop(engine.child));
  const client = new NativeEngineClient({ endpoint: `http://127.0.0.1:${engine.port}`, token, model: 'qwen35-9b-q4-k-m', backend: 'cpu', timeoutMs: 120000, maxTokens: 2 }); t.after(() => client.shutdown());
  const health = await client.health(); assert.equal(health.ready, true); assert.equal(health.model, 'qwen35-9b-q4-k-m'); assert.match(health.backend, /\/cpu$/);
  const controller = new ConversationController({ engine: client }); controller.createSession('ses_realhost01');
  const result = await controller.runTurn({ sessionId: 'ses_realhost01', requestId: 'req_realhost01', message: 'Reply with exactly OK.' });
  assert.equal(result.state, 'COMPLETED', engine.diagnostics());
  assert.ok(result.text.trim(), engine.diagnostics());
});
