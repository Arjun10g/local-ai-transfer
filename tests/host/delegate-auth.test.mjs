import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { ConversationController } from '../../host/agent/controller.mjs';
import { HostServer } from '../../host/server/host-server.mjs';
import { createHostComposition } from '../../lae-host.mjs';
import { rotateDelegateKey } from '../../host/delegate/state.mjs';
import { ScriptEngine, setup, tempDir } from './delegate-helpers.mjs';

test('delegation is off by default: every delegate route is 404 and no key or host.json is written', async t => {
  const engine = new ScriptEngine();
  const host = new HostServer({ controller: new ConversationController({ engine }), engine });
  const address = await host.listen(0); t.after(() => host.close());
  for (const path of ['/api/delegate/health', '/api/delegate/jobs', '/api/delegate']) {
    const response = await fetch(`${address.url}${path}`, { headers: { authorization: `Bearer ${'k'.repeat(43)}` } });
    assert.equal(response.status, 404, path);
  }
  assert.equal((await fetch(`${address.url}/api/delegation`, { headers: { authorization: `Bearer ${address.token}` } })).status, 404);
  const status = await (await fetch(`${address.url}/api/status`, { headers: { authorization: `Bearer ${address.token}` } })).json();
  assert.deepEqual(status.delegate, { enabled: false, queue: 0, approval: 'per_job' });

  // The production composition: no flag, no config -> no service, no files.
  const dir = await tempDir(t); const stateDir = join(dir, 'state');
  const off = await createHostComposition({ env: { LAE_ENGINE_MODE: 'fixture', BMO_STATE_DIR: stateDir } });
  t.after(() => off.host.close());
  await off.host.listen(0);
  assert.equal(off.delegate, null);
  assert.equal(off.operatorGrants.list().some(item => item.capability === 'delegate.read_only_jobs'), false);
  await assert.rejects(stat(stateDir), { code: 'ENOENT' });
  await assert.rejects(createHostComposition({ env: { LAE_ENGINE_MODE: 'fixture', BMO_STATE_DIR: stateDir, LAE_DELEGATE_ENABLED: 'yes' } }), /LAE_DELEGATE_ENABLED/);
});

test('LAE_DELEGATE_ENABLED=1 turns it on: key and host.json appear, the grant is offered, close removes host.json', async t => {
  const dir = await tempDir(t); const stateDir = join(dir, 'state');
  const on = await createHostComposition({ env: { LAE_ENGINE_MODE: 'fixture', BMO_STATE_DIR: stateDir, LAE_DELEGATE_ENABLED: '1' } });
  const address = await on.host.listen(0);
  assert.ok(on.delegate);
  assert.deepEqual((await readdir(stateDir)).sort(), ['delegate-key', 'host.json']);
  const record = JSON.parse(await readFile(join(stateDir, 'host.json'), 'utf8'));
  assert.deepEqual(Object.keys(record), ['version', 'port', 'pid', 'started_at']);
  assert.equal(record.port, address.port); assert.equal(record.pid, process.pid);
  assert.equal(on.operatorGrants.list().some(item => item.capability === 'delegate.read_only_jobs'), true);
  await on.host.close();
  assert.deepEqual(await readdir(stateDir), ['delegate-key']);
});

test('only the delegate key opens delegate routes, and it opens nothing else', async t => {
  const env = await setup(t);
  assert.equal((await env.call('GET', '/api/delegate/health')).status, 200);
  assert.equal((await env.call('GET', '/api/delegate/health', { auth: null })).status, 401);
  assert.equal((await env.call('GET', '/api/delegate/health', { auth: 'A'.repeat(43) })).status, 401);
  assert.equal((await env.call('GET', '/api/delegate/health', { headers: { authorization: `Basic ${env.key}` } })).status, 401);
  // The UI bearer is a different credential and must not work here.
  assert.equal((await env.call('GET', '/api/delegate/health', { auth: env.address.token })).status, 401);
  // And the delegate key must not work on any UI route.
  for (const path of ['/api/status', '/api/delegation', '/api/operator-grants']) assert.equal((await env.call('GET', path)).status, 401, path);
  assert.equal((await env.call('POST', '/api/delegation/stop', { body: {} })).status, 401);
  assert.equal((await env.call('POST', '/api/operator-grants/delegate.read_only_jobs', { body: { granted: true, duration_ms: 900000 } })).status, 401);
});

test('any Origin and any Host other than the literal loopback authority is refused, key or not', async t => {
  const env = await setup(t);
  const { port } = env.address;
  for (const origin of [`http://127.0.0.1:${port}`, `http://localhost:${port}`, 'https://attacker.invalid', 'null']) {
    const response = await env.call('GET', '/api/delegate/health', { headers: { origin } });
    assert.equal(response.status, 403, origin);
  }
  // A job POST from a page (CSRF) is refused even with a valid key.
  assert.equal((await env.call('POST', '/api/delegate/jobs', { body: { task: 'x', caller: { client: 'c', name: 'n' } }, headers: { origin: `http://127.0.0.1:${port}` } })).status, 403);
  for (const host of [`127.0.0.1:${port + 1}`, `localhost:${port + 1}`, '127.0.0.1', `rebind.attacker.invalid:${port}`, `127.0.0.1.nip.io:${port}`]) {
    assert.equal((await env.call('GET', '/api/delegate/health', { headers: { host } })).status, 403, host);
  }
  assert.equal((await env.call('GET', '/api/delegate/health', { headers: { host: `localhost:${port}` } })).status, 200);
  assert.equal(env.delegate.pendingCount(), 0, 'nothing was created by the refused requests');
});

test('failed keys are rate limited separately from the UI, with retry_after_s', async t => {
  const env = await setup(t);
  for (let i = 0; i < 19; i += 1) assert.equal((await env.call('GET', '/api/delegate/health', { auth: `${'x'.repeat(42)}${i % 10}` })).status, 401);
  const limited = await env.call('GET', '/api/delegate/health', { auth: 'y'.repeat(43) });
  assert.equal(limited.status, 429); assert.equal(limited.json.error, 'auth_rate_limited');
  assert.ok(Number.isInteger(limited.json.retry_after_s) && limited.json.retry_after_s >= 1 && limited.json.retry_after_s <= 60);
  assert.equal(limited.headers['retry-after'], String(limited.json.retry_after_s));
  // Even the right key waits out the window: no oracle while guessing.
  assert.equal((await env.call('GET', '/api/delegate/health')).status, 429);
  // The operator's UI is not locked out by a guessing delegate caller.
  assert.equal((await env.ui('GET', '/api/status')).status, 200);
});

test('rotating the key takes effect at once on a running host', async t => {
  const env = await setup(t);
  assert.equal((await env.call('GET', '/api/delegate/health')).status, 200);
  const fresh = await rotateDelegateKey(env.stateDir);
  assert.notEqual(fresh, env.key);
  assert.equal((await env.call('GET', '/api/delegate/health')).status, 401, 'the old key stops working');
  assert.equal((await env.call('GET', '/api/delegate/health', { auth: fresh })).status, 200);
});

test('the delegate key never appears in any response, header, or the status route', async t => {
  const env = await setup(t);
  const { json } = await env.submit({ context: `the key is ${env.key}` });
  await env.ui('GET', '/api/delegation'); await env.ui('GET', '/api/status');
  await env.ui('POST', `/api/delegation/jobs/${json.job_id}/decision`, { approved: true });
  env.engine.plan.push({ text: `echo ${env.key} and ${env.address.token}` });
  const done = await env.until(json.job_id, job => job.status === 'completed');
  assert.doesNotMatch(done.answer, new RegExp(env.key)); assert.doesNotMatch(done.answer, new RegExp(env.address.token));
  await env.call('GET', '/api/delegate/health');
  for (const response of env.responses) {
    assert.equal(response.text.includes(env.key), false, response.text.slice(0, 200));
    assert.equal(JSON.stringify(response.headers).includes(env.key), false);
  }
});
