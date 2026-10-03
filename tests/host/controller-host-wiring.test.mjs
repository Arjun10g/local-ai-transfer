import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createHostComposition, engineGenerationOptions, outputTokenReservation, parseContextTokensEnv, readEngineContextTokens, resolveContextTokens } from '../../lae-host.mjs';
import { NativeEngineClient } from '../../host/engine/native-engine-client.mjs';
import { validateConfig } from '../../host/agent/config.mjs';

const TOKEN = 'wiring-test-token-0123456789';

// Loopback stand-in for lae-engine: only the routes host startup touches, and
// every one demands the bearer token, as the real engine does.
async function fakeEngine(t, runtime) {
  const seen = [];
  const server = http.createServer((req, res) => {
    seen.push({ path: req.url, authorization: req.headers.authorization });
    const send = (status, body) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (req.headers.authorization !== `Bearer ${TOKEN}`) return send(401, { error: { code: 'unauthorized' } });
    if (req.url === '/readyz') return send(200, { ready: true, lifecycle: 'READY' });
    if (req.url === '/metrics') return send(200, { requests: 0, runtime });
    return send(404, { error: { code: 'not_found' } });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  return { endpoint: `http://127.0.0.1:${server.address().port}`, seen };
}

test('LAE_CONTEXT_TOKENS is validated to 512-16384', () => {
  assert.equal(parseContextTokensEnv(undefined), null);
  assert.equal(parseContextTokensEnv('512'), 512); assert.equal(parseContextTokensEnv('16384'), 16384);
  for (const bad of ['511', '16385', '8192.0', '08192', ' 8192', 'abc', '']) assert.throws(() => parseContextTokensEnv(bad), /LAE_CONTEXT_TOKENS/, bad);
});

test('the window is the smaller of the operator setting and the engine report, defaulting to 8192', () => {
  assert.equal(resolveContextTokens(), 8192);
  assert.equal(resolveContextTokens({ envTokens: 4096 }), 4096);
  assert.equal(resolveContextTokens({ engineTokens: 6144 }), 6144);
  assert.equal(resolveContextTokens({ envTokens: 16384, engineTokens: 8192 }), 8192);
  assert.throws(() => resolveContextTokens({ engineTokens: 256 }), /outside 512-16384/);
});

test('the output reservation follows engine.maxTokens and shrinks only for a small window', () => {
  const engine = { maxTokens: 1024 };
  assert.equal(outputTokenReservation(engine, 8192), 1024); assert.equal(engine.maxTokens, 1024);
  assert.equal(outputTokenReservation(engine, 2048), 512); assert.equal(engine.maxTokens, 512, 'the engine reserves what the budget reserves');
  assert.equal(outputTokenReservation({}, 8192), 1024, 'a fixture without maxTokens keeps the budget default');
  assert.equal(outputTokenReservation({}, 2048), 512);
});

test('the runtime window is read from GET /metrics with the bearer token', async t => {
  const { endpoint, seen } = await fakeEngine(t, { context_tokens: 4096, n_batch: 512 });
  const client = new NativeEngineClient({ endpoint, token: TOKEN, model: 'test-model', backend: 'cpu' });
  assert.equal(await readEngineContextTokens(client), 4096);
  assert.deepEqual(seen.at(-1), { path: '/metrics', authorization: `Bearer ${TOKEN}` });
  const wrong = new NativeEngineClient({ endpoint, token: 'x'.repeat(24), model: 'test-model', backend: 'cpu' });
  assert.equal(await readEngineContextTokens(wrong), null, 'an unreadable report falls back rather than guessing');
  const { endpoint: bare } = await fakeEngine(t, {});
  assert.equal(await readEngineContextTokens(new NativeEngineClient({ endpoint: bare, token: TOKEN, model: 'test-model', backend: 'cpu' })), null);
  assert.equal(await readEngineContextTokens({ async *generate() {} }), null);
});

test('native composition budgets the engine runtime window and the configured confirmation timeout', async t => {
  const { endpoint } = await fakeEngine(t, { context_tokens: 6144 });
  const env = { LAE_ENGINE_MODE: 'native', LAE_ENGINE_ENDPOINT: endpoint, LAE_ENGINE_TOKEN: TOKEN, LAE_ENGINE_MODEL: 'test-model', LAE_ENGINE_BACKEND: 'cpu' };
  const composition = await createHostComposition({ fileConfig: { host: { confirmation_timeout_ms: 90000 } }, env });
  t.after(() => composition.host.close());
  assert.equal(composition.controller.contextTokens, 6144);
  assert.equal(composition.controller.maxOutputTokens, composition.engine.maxTokens, 'one output reservation for engine and budget');
  assert.equal(composition.controller.confirmationTimeoutMs, 90000);
  const capped = await createHostComposition({ env: { ...env, LAE_CONTEXT_TOKENS: '4096' } });
  t.after(() => capped.host.close());
  assert.equal(capped.controller.contextTokens, 4096); assert.equal(capped.controller.confirmationTimeoutMs, 120000);
  await assert.rejects(createHostComposition({ env: { ...env, LAE_CONTEXT_TOKENS: '99999' } }), /LAE_CONTEXT_TOKENS/);
});

test('fixture composition uses LAE_CONTEXT_TOKENS or the 8192 default', async t => {
  const plain = await createHostComposition({ env: { LAE_ENGINE_MODE: 'fixture' } }); t.after(() => plain.host.close());
  assert.equal(plain.controller.contextTokens, 8192);
  const small = await createHostComposition({ env: { LAE_ENGINE_MODE: 'fixture', LAE_CONTEXT_TOKENS: '2048' } }); t.after(() => small.host.close());
  assert.equal(small.controller.contextTokens, 2048);
});

test('native generation deadlines and the answer cap come from env or config, validated', async t => {
  assert.deepEqual(engineGenerationOptions({}, {}), {});
  assert.deepEqual(engineGenerationOptions({ LAE_ENGINE_MAX_TOKENS: '512', LAE_ENGINE_IDLE_TIMEOUT_MS: '60000' }, { max_tokens: 2048, first_token_timeout_ms: 1200000 }), { firstTokenTimeoutMs: 1200000, idleTimeoutMs: 60000, maxTokens: 512 });
  for (const [name, bad] of [['LAE_ENGINE_MAX_TOKENS', '4096'], ['LAE_ENGINE_FIRST_TOKEN_TIMEOUT_MS', '10s'], ['LAE_ENGINE_TOTAL_TIMEOUT_MS', '999'], ['LAE_ENGINE_IDLE_TIMEOUT_MS', '']]) assert.throws(() => engineGenerationOptions({ [name]: bad }), new RegExp(name));
  assert.throws(() => validateConfig({ engine: { max_tokens: 0 } }), /engine.max_tokens/);
  assert.throws(() => validateConfig({ engine: { total_timeout_ms: 7200001 } }), /engine.total_timeout_ms/);
  const { endpoint } = await fakeEngine(t, { context_tokens: 8192 });
  const env = { LAE_ENGINE_MODE: 'native', LAE_ENGINE_ENDPOINT: endpoint, LAE_ENGINE_TOKEN: TOKEN, LAE_ENGINE_MODEL: 'test-model', LAE_ENGINE_BACKEND: 'cpu', LAE_ENGINE_MAX_TOKENS: '512' };
  const composition = await createHostComposition({ fileConfig: { engine: { first_token_timeout_ms: 1200000, idle_timeout_ms: 90000, total_timeout_ms: 3600000 } }, env });
  t.after(() => composition.host.close());
  assert.equal(composition.engine.maxTokens, 512); assert.equal(composition.engine.firstTokenTimeoutMs, 1200000);
  assert.equal(composition.engine.idleTimeoutMs, 90000); assert.equal(composition.engine.totalTimeoutMs, 3600000);
  assert.equal(composition.controller.maxOutputTokens, 512, 'the budget reserves the answer cap the engine will be sent');
});
