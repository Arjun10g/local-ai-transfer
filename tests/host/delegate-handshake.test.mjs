import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { ConversationController } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { rotateDelegateKey } from '../../host/delegate/state.mjs';
import { handshakeProof, HANDSHAKE_LIMIT } from '../../host/delegate/service.mjs';
import { ScriptEngine, setup } from './delegate-helpers.mjs';

const NONCE = 'n0nce_ABCDEFGHIJKLMNOPQRST';
// The bridge's side, written out independently of the host's helper.
const expected = (key, nonce, port, pid) => createHmac('sha256', key).update(`bmo-delegate-handshake-v1|${nonce}|${port}|${pid}`).digest('base64url');
const shake = (env, body, headers = {}) => env.call('POST', '/api/delegate/handshake', { body, auth: null, headers });

test('the proof is the HMAC of the nonce, port, and pid under the delegate key', async t => {
  const env = await setup(t);
  const response = await shake(env, { nonce: NONCE });
  assert.equal(response.status, 200); assert.deepEqual(Object.keys(response.json), ['proof']);
  assert.equal(response.json.proof, expected(env.key, NONCE, env.address.port, process.pid));
  assert.equal(response.json.proof, handshakeProof(env.key, NONCE, env.address.port, process.pid));
  assert.match(response.json.proof, /^[A-Za-z0-9_-]{43}$/);
  // It binds the port and the pid: a stale host.json naming either wrongly cannot be answered.
  assert.notEqual(response.json.proof, expected(env.key, NONCE, env.address.port + 1, process.pid));
  assert.notEqual(response.json.proof, expected(env.key, NONCE, env.address.port, process.pid + 1));
  // Without the key the proof cannot be produced, and no key material is returned.
  assert.notEqual(response.json.proof, expected('A'.repeat(43), NONCE, env.address.port, process.pid));
  assert.equal(response.text.includes(env.key), false);
  // A different nonce gives a different proof (no replay of an old answer).
  assert.notEqual((await shake(env, { nonce: `${NONCE}x` })).json.proof, response.json.proof);
});

test('rotating the key changes the proof without a restart', async t => {
  const env = await setup(t);
  const before = (await shake(env, { nonce: NONCE })).json.proof;
  const fresh = await rotateDelegateKey(env.stateDir);
  const after = (await shake(env, { nonce: NONCE })).json.proof;
  assert.notEqual(after, before); assert.equal(after, expected(fresh, NONCE, env.address.port, process.pid));
});

test('the nonce and body are validated strictly; no key may be sent here', async t => {
  const env = await setup(t);
  for (const nonce of ['short', 'x'.repeat(21), 'x'.repeat(65), `${'x'.repeat(30)}=`, `${'x'.repeat(30)}+`, `${'x'.repeat(30)} `, `${'x'.repeat(30)}é`, 42, null]) {
    const response = await shake(env, { nonce });
    assert.equal(response.status, 400, JSON.stringify(nonce)); assert.equal(response.json.proof, undefined);
  }
  assert.equal((await shake(env, { nonce: 'x'.repeat(22) })).status, 200);
  assert.equal((await shake(env, { nonce: 'x'.repeat(64) })).status, 200);
  assert.equal((await shake(env, { nonce: NONCE, extra: 1 })).status, 400);
  assert.equal((await shake(env, {})).status, 400);
  assert.equal((await shake(env, { nonce: NONCE }, { 'content-type': 'text/plain' })).status, 415);
  assert.equal((await env.call('POST', '/api/delegate/handshake', { body: { nonce: NONCE } })).status, 400, 'a request carrying the key is refused');
  assert.equal((await env.call('POST', '/api/delegate/handshake', { body: { nonce: NONCE }, auth: env.address.token })).status, 400, 'and so is the UI bearer');
  assert.equal((await env.call('GET', '/api/delegate/handshake', { auth: null })).status, 404);
  assert.equal((await env.call('POST', '/api/delegate/handshake?x=1', { body: { nonce: NONCE }, auth: null })).status, 404);
});

test('Origin and Host are checked as on every delegate route', async t => {
  const env = await setup(t);
  const { port } = env.address;
  for (const origin of [`http://127.0.0.1:${port}`, 'https://attacker.invalid']) assert.equal((await shake(env, { nonce: NONCE }, { origin })).status, 403, origin);
  for (const host of [`127.0.0.1:${port + 1}`, `rebind.attacker.invalid:${port}`]) assert.equal((await shake(env, { nonce: NONCE }, { host })).status, 403, host);
  assert.equal((await shake(env, { nonce: NONCE }, { host: `localhost:${port}` })).status, 200);
});

test('handshakes have their own rate limit, which does not touch key checks', async t => {
  const env = await setup(t);
  for (let i = 0; i < HANDSHAKE_LIMIT; i += 1) assert.equal((await shake(env, { nonce: NONCE })).status, 200);
  const limited = await shake(env, { nonce: NONCE });
  assert.equal(limited.status, 429); assert.equal(limited.json.error, 'rate_limited'); assert.equal(limited.json.proof, undefined);
  assert.ok(limited.json.retry_after_s >= 1 && limited.json.retry_after_s <= 60);
  // Invalid requests count too, so a flood of junk is capped as well.
  assert.equal((await shake(env, { nonce: 'bad' })).status, 429);
  assert.equal((await env.call('GET', '/api/delegate/health')).status, 200, 'the keyed routes are unaffected');
});

test('disabled: the handshake is 404 like every delegate route', async t => {
  const engine = new ScriptEngine();
  const host = new HostServer({ controller: new ConversationController({ engine }), engine });
  const address = await host.listen(0); t.after(() => host.close());
  const response = await fetch(`${address.url}/api/delegate/handshake`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ nonce: NONCE }) });
  assert.equal(response.status, 404);
});
