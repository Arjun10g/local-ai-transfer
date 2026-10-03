// Host identity handshake: the bridge must prove the listener on host.json's port is the BMO
// host (HMAC over a fresh nonce, port and pid, keyed by the delegate key) BEFORE any request
// carries the key. Ids [HS1]..[HS5] are bridge requirements beyond the research checklist;
// tests/host/mcp-checklist.test.mjs audits that each is covered.

import test from 'node:test';
import assert from 'node:assert/strict';
import { HostClient, UNVERIFIED_MESSAGE } from '../../host/mcp/host-client.mjs';
import { CopilotCliLikeClient, TEST_KEY, startBridge, startFakeHost, structured } from './mcp-fake-host.mjs';

async function fakeHost(t, options) { const host = await startFakeHost(options); t.after(() => host.close()); return host; }
const client = (host, options = {}) => new HostClient({ key: TEST_KEY, discover: host.discover, handshakeTimeoutMs: 300, ...options });

function assertKeyNeverSent(host) {
  assert.equal(host.keyed().length, 0, 'a keyed request reached an unverified listener');
  const bytes = host.receivedText();
  assert.ok(!bytes.includes(TEST_KEY), 'the key appeared in bytes sent to the listener');
  assert.ok(!/authorization/i.test(bytes), 'an Authorization header reached the listener');
}

test('[HS1] a correct proof is accepted; the handshake carries only a fresh nonce, never the key, and is cached', async t => {
  const host = await fakeHost(t);
  const bmo = client(host);
  await bmo.health();
  await bmo.health();
  await bmo.startJob({ task: 't', allow_files: false, caller: { client: 'c', name: 'n' } });
  const [handshake, ...others] = host.handshakes();
  assert.equal(others.length, 0, 'verification must be cached for the same host.json identity');
  assert.equal(host.requests[0], handshake, 'the handshake must precede every keyed request');
  assert.equal(handshake.headers.authorization, undefined);
  assert.equal(handshake.headers.origin, undefined);
  assert.match(handshake.headers['content-type'], /^application\/json/);
  assert.deepEqual(Object.keys(handshake.body), ['nonce']);
  assert.match(handshake.body.nonce, /^[A-Za-z0-9_-]{43}$/);
  const handshakeBytes = host.receivedText().split('GET /api/delegate/health')[0];
  assert.ok(!handshakeBytes.includes(TEST_KEY));
  assert.equal(host.keyed().length, 3);
});

for (const mode of ['wrong-key', 'wrong-pid', 'wrong-port', 'no-proof', 'short-proof', 'not-json', 'status-500', 'huge', 'hang']) {
  test(`[HS2] [HS3] an impostor answering "${mode}" is unverified and never receives the key`, async t => {
    const host = await fakeHost(t);
    host.state.handshake = mode;
    const started = Date.now();
    const error = await client(host).health().catch(failure => failure);
    assert.equal(error.code, 'host_unverified', `${mode}: ${error.code}`);
    assert.equal(error.message, UNVERIFIED_MESSAGE);
    assert.ok(Date.now() - started < 1_500);
    assertKeyNeverSent(host);
  });
}

test('[HS2] a replayed proof for an old nonce is rejected after host.json changes', async t => {
  const host = await fakeHost(t);
  const bmo = client(host);
  await bmo.health();
  const keyedBefore = host.keyed().length;
  host.state.startedAt = new Date(Date.now() + 1000).toISOString(); // host.json now names a new instance
  host.state.handshake = 'replay'; // answers with the proof for the FIRST nonce it ever saw
  const error = await bmo.health().catch(failure => failure);
  assert.equal(error.code, 'host_unverified');
  assert.equal(host.handshakes().length, 2);
  assert.notEqual(host.handshakes()[0].body.nonce, host.handshakes()[1].body.nonce, 'nonces must be fresh');
  assert.equal(host.keyed().length, keyedBefore, 'no keyed request after the failed re-verification');
});

test('[HS4] a host.json identity change or a connection failure forces re-verification', async t => {
  let dropNext = false;
  const host = await fakeHost(t, { hooks: { health: async ({ req }) => { if (!dropNext) return false; dropNext = false; req.socket.destroy(); return true; } } });
  const bmo = client(host);
  await bmo.health();
  assert.equal(host.handshakes().length, 1);
  host.state.startedAt = '2030-01-01T00:00:00.000Z';
  await bmo.health();
  assert.equal(host.handshakes().length, 2, 'started_at change must re-verify');
  dropNext = true;
  await assert.rejects(() => bmo.health(), error => error.code === 'host_unreachable');
  await bmo.health();
  assert.equal(host.handshakes().length, 3, 'a connection failure must re-verify');
  const nonces = host.handshakes().map(request => request.body.nonce);
  assert.equal(new Set(nonces).size, nonces.length);
});

test('[HS4] concurrent requests share one handshake', async t => {
  const host = await fakeHost(t);
  const bmo = client(host);
  await Promise.all([bmo.health(), bmo.health(), bmo.health()]);
  assert.equal(host.handshakes().length, 1);
});

test('[HS5] a proof failure is cached briefly (<= 5 s), then BMO is re-checked', async t => {
  let clock = 1_000_000;
  const host = await fakeHost(t);
  host.state.handshake = 'wrong-key';
  const bmo = client(host, { now: () => clock });
  await assert.rejects(() => bmo.health(), error => error.code === 'host_unverified');
  await assert.rejects(() => bmo.health(), error => error.code === 'host_unverified');
  assert.equal(host.handshakes().length, 1, 'failure is cached within its window');
  clock += 5_001;
  host.state.handshake = 'correct'; // e.g. the operator restarted BMO
  await bmo.health();
  assert.equal(host.handshakes().length, 2);
  assert.equal(host.keyed().length, 1);
});

test('[HS3] a listener that resets the handshake connection gets no key; a refused port reads as not running', async t => {
  const host = await fakeHost(t);
  host.state.handshake = 'reset';
  const error = await client(host).health().catch(failure => failure);
  assert.equal(error.code, 'host_unreachable');
  assertKeyNeverSent(host);
  const port = host.port;
  await host.close();
  const refused = await new HostClient({ key: TEST_KEY, discover: async () => ({ ok: true, port, pid: process.pid, startedAt: 'x' }) }).health().catch(failure => failure);
  assert.equal(refused.code, 'bmo_not_running');
});

test('[HS1] through MCP: an impostor yields a fixed, secret-free tool error and no job is created', async t => {
  const host = await fakeHost(t);
  host.state.handshake = 'wrong-pid';
  const { client: mcp } = startBridge(t, { host, ClientClass: CopilotCliLikeClient });
  for (const [name, args] of [['bmo_ask', { task: 'hello' }], ['bmo_health', {}], ['bmo_job_status', { job_id: 'job_AAAAAAAAAAAAAAAAAAAAAAAA' }]]) {
    const reply = await mcp.callTool(name, args);
    assert.equal(reply.result.isError, true);
    assert.equal(structured(reply).error.code, 'host_unverified');
    assert.equal(structured(reply).error.message, UNVERIFIED_MESSAGE);
    const text = JSON.stringify(reply);
    assert.ok(!text.includes(TEST_KEY) && !text.includes(String(host.port)));
  }
  assert.equal(host.jobs.size, 0);
  assertKeyNeverSent(host);
});
