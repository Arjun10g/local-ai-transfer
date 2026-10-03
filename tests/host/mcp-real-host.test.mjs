// End-to-end: the MCP bridge against the REAL BMO host (HostServer + DelegateService with a
// scripted engine, wired by the host side's own tests/host/delegate-helpers.mjs). Discovery
// goes through the real host.json the host writes, so the file format, auth, routes, phases
// and error bodies are all checked against the implementation rather than the fake.

import test from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { setup as startRealHost, tempDir } from './delegate-helpers.mjs';
import { readHostInfo, NOT_RUNNING_MESSAGE } from '../../host/mcp/host-client.mjs';
import { StartLimiter } from '../../host/mcp/tools.mjs';
import { CopilotCliLikeClient, VsCodeLikeClient, startBridge, structured, sleep } from './mcp-fake-host.mjs';

async function bridgeTo(t, real, { key = real.key, ClientClass = CopilotCliLikeClient } = {}) {
  return startBridge(t, { key, ClientClass, discover: () => readHostInfo({ stateDir: real.stateDir }), limiter: new StartLimiter({ limit: 100 }) });
}
const onlyJobId = real => { const ids = [...real.delegate.jobs.keys()]; assert.equal(ids.length, 1); return ids[0]; };

test('[VS1] real host: health, ask awaiting the operator, approval on the laptop, answer via bmo_job_status', async t => {
  const real = await startRealHost(t);
  real.engine.plan.push({ text: 'Reworded: the meeting moved to Friday.' });
  const { client } = await bridgeTo(t, real, { ClientClass: VsCodeLikeClient });
  await client.handshake();
  const health = await client.callTool('bmo_health', {});
  assert.equal(health.result.isError, false, JSON.stringify(health.result));
  assert.equal(structured(health).approval, 'per_job');
  const pending = await client.callTool('bmo_ask', { task: 'Reword: meeting is Friday now', context: 'Team note' });
  assert.equal(pending.result.isError, false, JSON.stringify(pending.result));
  assert.equal(structured(pending).status, 'awaiting_approval');
  assert.equal(structured(pending).phase, 'waiting_for_operator');
  assert.match(structured(pending).job_id, /^job_[A-Za-z0-9_-]{24}$/);
  assert.match(structured(pending).next_step, /human must approve/);
  assert.equal(onlyJobId(real), structured(pending).job_id);
  real.delegate.decide(structured(pending).job_id, true); // the operator presses Approve
  let done;
  for (let attempt = 0; attempt < 10 && structured(done ?? pending).status !== 'completed'; attempt += 1) done = await client.callTool('bmo_job_status', { job_id: structured(pending).job_id, wait_seconds: 1 });
  assert.equal(structured(done).status, 'completed', JSON.stringify(done.result));
  assert.equal(structured(done).answer, 'Reworded: the meeting moved to Friday.');
  assert.ok(structured(done).provenance);
});

test('[CLI1] real host: cancel, unknown job, a denial, and the queue cap all map to the bridge contract', async t => {
  const real = await startRealHost(t);
  const { client } = await bridgeTo(t, real);
  await client.discover();
  const first = await client.callTool('bmo_ask', { task: 'first' });
  const cancelled = await client.callTool('bmo_job_cancel', { job_id: structured(first).job_id });
  assert.equal(cancelled.result.isError, false, JSON.stringify(cancelled.result));
  assert.equal(structured(cancelled).status, 'cancelled');
  const unknown = await client.callTool('bmo_job_status', { job_id: 'job_AAAAAAAAAAAAAAAAAAAAAAAA', wait_seconds: 0 });
  assert.equal(structured(unknown).error.code, 'unknown_job');
  const denied = await client.callTool('bmo_ask', { task: 'deny me' });
  real.delegate.decide(structured(denied).job_id, false);
  const deniedStatus = await client.callTool('bmo_job_status', { job_id: structured(denied).job_id, wait_seconds: 1 });
  assert.equal(structured(deniedStatus).status, 'denied');
  assert.equal(deniedStatus.result.isError, true);
  assert.match(structured(deniedStatus).error.message, /declined this job on their laptop/);
  for (let index = 0; index < 3; index += 1) assert.equal(structured(await client.callTool('bmo_ask', { task: `fill ${index}` })).status, 'awaiting_approval');
  const full = await client.callTool('bmo_ask', { task: 'one too many' });
  assert.equal(structured(full).error.code, 'queue_full');
  assert.ok(structured(full).retry_after_s > 0);
});

test('[C15] real host: Stop on an in-flight bmo_ask cancels the job on the host', async t => {
  const real = await startRealHost(t);
  const { client } = await bridgeTo(t, real);
  client.send({ jsonrpc: '2.0', id: 'stop-me', method: 'tools/call', params: { name: 'bmo_ask', arguments: { task: 'long one' }, _meta: client.meta() } });
  for (let waited = 0; real.delegate.jobs.size === 0 && waited < 3000; waited += 10) await sleep(10);
  const jobId = onlyJobId(real);
  await sleep(100);
  client.notify('notifications/cancelled', { requestId: 'stop-me' });
  for (let waited = 0; real.delegate.jobs.get(jobId)?.status !== 'cancelled' && waited < 3000; waited += 10) await sleep(10);
  assert.equal(real.delegate.jobs.get(jobId).status, 'cancelled');
  await sleep(200);
  assert.deepEqual(client.responsesFor('stop-me'), []);
});

test('real host: a wrong key is unauthorized, and delegation off (no host.json) reads as not running or off', async t => {
  const real = await startRealHost(t);
  const wrong = (await bridgeTo(t, real, { key: 'A'.repeat(43) })).client;
  const rejected = await wrong.callTool('bmo_ask', { task: 'hi' });
  assert.equal(structured(rejected).error.code, 'unauthorized');
  assert.match(structured(rejected).error.message, /Start-BMO\.ps1 -ShowDelegateKey/);
  assert.equal(real.delegate.jobs.size, 0);
  const emptyState = join(await tempDir(t, 'bmo-mcp-off-'), 'state');
  const { client } = startBridge(t, { key: real.key, ClientClass: CopilotCliLikeClient, discover: () => readHostInfo({ stateDir: emptyState }) });
  const off = await client.callTool('bmo_health', {});
  assert.equal(structured(off).error.message, NOT_RUNNING_MESSAGE);
});
