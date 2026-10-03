import test from 'node:test';
import assert from 'node:assert/strict';
import { setup, delay } from './delegate-helpers.mjs';

const approve = (env, id) => env.ui('POST', `/api/delegation/jobs/${id}/decision`, { approved: true });
async function waitFor(predicate, timeoutMs = 5000) { const end = Date.now() + timeoutMs; while (Date.now() < end) { if (predicate()) return; await delay(5); } throw new Error('condition not reached'); }
async function userChat(env, message = 'hello') {
  const response = await fetch(`${env.address.url}/api/chat`, { method: 'POST', headers: { authorization: `Bearer ${env.address.token}`, 'content-type': 'application/json' }, body: JSON.stringify({ session_id: 'ses_operator_01', message, request_id: `req_${Math.random().toString(36).slice(2, 12)}` }) });
  return { status: response.status, text: await response.text() };
}

test('a job waits while the operator\'s turn runs and never preempts it', async t => {
  const env = await setup(t);
  env.engine.plan.push({ hold: true }, { text: 'job answer' });
  const chat = userChat(env);
  await waitFor(() => env.engine.active);
  const id = (await env.submit()).json.job_id; await approve(env, id);
  await delay(300);
  const queued = (await env.poll(id)).json;
  assert.equal(queued.status, 'queued'); assert.equal(queued.phase, 'waiting_for_engine');
  assert.equal((await env.call('GET', '/api/delegate/health')).json.busy, true);
  assert.equal(env.engine.cancels.length, 0, 'the operator\'s turn was not touched'); assert.equal(env.engine.refused, 0);
  env.engine.releaseHolds();
  assert.match((await chat).text, /message\.completed/);
  const done = await env.until(id, job => job.status === 'completed');
  assert.equal(done.answer, 'job answer');
  assert.equal(env.engine.calls[0].sessionId, 'ses_operator_01'); assert.match(env.engine.calls[1].sessionId, /^dlg_/);
});

test('a job also waits for a turn started on the operator\'s controller outside the chat route', async t => {
  const env = await setup(t);
  env.engine.plan.push({ hold: true }, { text: 'job answer' });
  const turn = env.controller.runTurn({ sessionId: 'ses_direct_001', message: 'hi', requestId: 'req_direct_001' });
  await waitFor(() => env.engine.active);
  const id = (await env.submit()).json.job_id; await approve(env, id);
  await delay(400);
  assert.equal((await env.poll(id)).json.status, 'queued'); assert.equal(env.engine.calls.length, 1);
  assert.equal(env.engine.refused, 0, 'the job never tried the engine while the turn ran');
  env.engine.releaseHolds(); await turn;
  assert.equal((await env.until(id, job => job.status === 'completed')).answer, 'job answer');
});

test('the operator\'s new turn stops a running job, which is queued again and then finishes', async t => {
  const env = await setup(t);
  env.engine.lingerMs = 300; // the engine stays BUSY briefly after the cancel
  env.engine.plan.push({ hold: true }, { text: 'user answer' }, { text: 'job answer' });
  const id = (await env.submit()).json.job_id; await approve(env, id);
  await env.until(id, job => job.status === 'running');
  await waitFor(() => env.engine.active);
  const jobRequest = env.engine.active;
  const chat = await userChat(env);
  assert.equal(chat.status, 200); assert.match(chat.text, /user answer/); assert.equal(env.engine.refused, 0, 'the turn waited for the engine to be free');
  assert.ok(env.engine.cancels.includes(jobRequest), 'the job generation was cancelled on the engine');
  const done = await env.until(id, job => job.status === 'completed');
  assert.equal(done.answer, 'job answer');
  assert.deepEqual(env.engine.calls.map(call => call.sessionId.startsWith('dlg_') ? 'job' : 'user'), ['job', 'user', 'job']);
  assert.equal(env.engine.deleted.filter(id => id.startsWith('dlg_')).length, 2, 'each attempt released its engine session');
});

test('a job preempted more than three times fails as preempted', async t => {
  const env = await setup(t);
  env.engine.plan.push({ hold: true }, { hold: true }, { hold: true }, { hold: true });
  const id = (await env.submit()).json.job_id; await approve(env, id);
  for (let round = 0; round < 4; round += 1) {
    await waitFor(() => env.engine.active);
    const release = await env.delegate.interactiveBegin();
    release();
  }
  const failed = await env.until(id, job => job.status === 'failed');
  assert.equal(failed.error.code, 'preempted');
});

test('cancel stops a running generation, frees the slot, and the next job runs', async t => {
  const env = await setup(t);
  env.engine.plan.push({ hold: true }, { text: 'second' });
  const first = (await env.submit()).json.job_id; await approve(env, first);
  await env.until(first, job => job.status === 'running'); await waitFor(() => env.engine.active);
  const running = env.engine.active;
  const second = (await env.submit()).json.job_id; await approve(env, second);
  const cancelled = await env.call('POST', `/api/delegate/jobs/${first}/cancel`, { body: {} });
  assert.deepEqual(cancelled.json, { job_id: first, status: 'cancelled' });
  assert.ok(env.engine.cancels.includes(running));
  assert.equal((await env.until(second, job => job.status === 'completed')).answer, 'second');
  const final = (await env.poll(first)).json;
  assert.equal(final.status, 'cancelled'); assert.equal(final.error, undefined); assert.equal(final.answer, undefined);
  // Cancelling again, or a finished job, is harmless; unknown is 404.
  assert.equal((await env.call('POST', `/api/delegate/jobs/${first}/cancel`, { body: {} })).json.status, 'cancelled');
  assert.equal((await env.call('POST', `/api/delegate/jobs/${second}/cancel`, { body: {} })).json.status, 'completed');
  assert.equal((await env.call('POST', '/api/delegate/jobs/job_unknown_123/cancel', { body: {} })).status, 404);
  assert.equal((await env.call('POST', `/api/delegate/jobs/${first}/cancel`, { body: { now: true } })).status, 400);
  assert.equal((await env.call('POST', `/api/delegate/jobs/${first}/cancel`, { body: {}, headers: { 'content-type': 'text/plain' } })).status, 415);
});

test('cancel and stop-all withdraw a job still awaiting approval', async t => {
  const env = await setup(t);
  const a = (await env.submit()).json.job_id; const b = (await env.submit()).json.job_id;
  assert.equal((await env.call('POST', `/api/delegate/jobs/${a}/cancel`, { body: {} })).json.status, 'cancelled');
  assert.deepEqual((await env.ui('GET', '/api/delegation')).json.pending.map(card => card.job_id), [b]);
  assert.deepEqual((await env.ui('POST', '/api/delegation/stop', {})).json, { cancelled: 1 });
  assert.equal((await env.poll(b)).json.status, 'cancelled');
  assert.equal(env.engine.calls.length, 0);
});

test('the wall-clock cap stops a job that runs too long', async t => {
  const env = await setup(t, { options: { max_runtime_ms: 200 } });
  env.engine.plan.push({ hold: true });
  const id = (await env.submit()).json.job_id; await approve(env, id);
  const failed = await env.until(id, job => job.status === 'failed');
  assert.equal(failed.error.code, 'job_timeout'); assert.equal(env.engine.cancels.length >= 1, true);
});

test('the token cap stops a job that writes too much', async t => {
  const env = await setup(t, { options: { max_output_tokens: 64 } });
  env.engine.plan.push({ chunks: Array.from({ length: 200 }, () => 'word word ') });
  const id = (await env.submit()).json.job_id; await approve(env, id);
  const failed = await env.until(id, job => job.status === 'failed');
  assert.equal(failed.error.code, 'token_limit'); assert.equal(failed.answer, undefined);
});

test('a busy engine requeues the job a few times, then fails it as engine_busy', async t => {
  const env = await setup(t);
  env.engine.plan.push({ busy: true }, { text: 'after busy' });
  const id = (await env.submit()).json.job_id; const started = Date.now(); await approve(env, id);
  assert.equal((await env.until(id, job => job.status === 'completed')).answer, 'after busy');
  assert.ok(Date.now() - started >= 900, 'it backed off before retrying');
  env.engine.plan.push({ busy: true }, { busy: true }, { busy: true }, { busy: true });
  const again = (await env.submit()).json.job_id; await approve(env, again);
  const failed = await env.until(again, job => job.status === 'failed', { timeoutMs: 10000 });
  assert.equal(failed.error.code, 'engine_busy');
});

test('a job also waits for the operator\'s background memory summary to finish', async t => {
  let finish; const memory = new Promise(resolve => { finish = resolve; });
  const env = await setup(t, { userController: { active: null, memoryIdle: () => memory, cancelActive() { return false; } } });
  const id = (await env.submit()).json.job_id; await approve(env, id);
  await delay(300);
  assert.equal((await env.poll(id)).json.status, 'queued'); assert.equal(env.engine.calls.length, 0);
  finish();
  await env.until(id, job => job.status === 'completed');
});
