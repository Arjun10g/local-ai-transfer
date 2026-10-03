import test from 'node:test';
import assert from 'node:assert/strict';
import { setup, delay } from './delegate-helpers.mjs';

const caller = { client: 'vscode', name: 'copilot' };

test('POST jobs: exact keys, types, sizes, and content type are enforced', async t => {
  const env = await setup(t);
  const post = (body, extra) => env.call('POST', '/api/delegate/jobs', { body, ...extra });
  const bad = async (body, status, error, extra) => { const response = await post(body, extra); assert.equal(response.status, status, JSON.stringify(body)?.slice(0, 80)); if (error) assert.equal(response.json.error, error); };
  await bad({ task: 'x', caller, extra: 1 }, 400, 'invalid_request_body');
  await bad({ task: 'x' }, 400, 'invalid_request_body');
  await bad({ caller }, 400, 'invalid_request_body');
  await bad({ task: 'x', caller: { client: 'c' } }, 400, 'invalid_request_body');
  await bad({ task: 'x', caller: { client: 'c', name: 'n', email: 'e' } }, 400, 'invalid_request_body');
  await bad({ task: 'x', caller: { client: 'c'.repeat(65), name: 'n' } }, 400, 'invalid_request_body');
  await bad({ task: 'x', caller: { client: '', name: 'n' } }, 400, 'invalid_request_body');
  await bad({ task: 'x', caller: 'vscode' }, 400, 'invalid_request_body');
  await bad({ task: 7, caller }, 400, 'invalid_request_body');
  await bad({ task: 'x', context: 7, caller }, 400, 'invalid_request_body');
  await bad({ task: 'x', allow_files: 'yes', caller }, 400, 'invalid_request_body');
  await bad({ task: '   ', caller }, 400, 'invalid_request_body');
  // Only invisible characters: stripped to nothing, so refused.
  await bad({ task: '‮​\u0007', caller }, 400, 'invalid_request_body');
  await bad({ task: 'x'.repeat(4001), caller }, 413, 'task_too_large');
  await bad({ task: 'x', context: 'c'.repeat(16001), caller }, 413, 'context_too_large');
  await bad(undefined, 413, 'body_too_large', { raw: JSON.stringify({ task: 'x', caller, context: 'c'.repeat(120000) }) });
  await bad(undefined, 400, 'invalid_json', { raw: '{"task":"x","task":"y","caller":{"client":"c","name":"n"}}' });
  await bad(undefined, 400, 'invalid_json', { raw: '{"task":' });
  await bad({ task: 'x', caller }, 415, 'unsupported_content_type', { headers: { 'content-type': 'text/plain' } });
  // Limits count code points, as a JSON Schema maxLength does: 4,000 emoji
  // (8,000 UTF-16 units) is a valid task.
  const emoji = await post({ task: '\u{1F600}'.repeat(4000), caller });
  assert.equal(emoji.status, 202);
  // The whole job must fit the engine's per-message byte bound.
  await bad({ task: 'x', context: '\u{1F600}'.repeat(16000), caller }, 413, 'job_too_large');
  assert.equal(env.delegate.pendingCount(), 1, 'only the valid job exists');
  assert.equal(env.engine.calls.length, 0, 'nothing ran: the job awaits the operator');
});

test('a new job awaits approval on the laptop; the card shows caller and task, masked and bounded', async t => {
  const env = await setup(t);
  const response = await env.submit({ task: 'Use password=hunter2 to‮ summarise', context: 'x'.repeat(1234), caller: { client: 'vscode‮', name: 'copilot' } });
  assert.equal(response.status, 202);
  assert.match(response.json.job_id, /^[A-Za-z0-9_-]{8,64}$/); assert.equal(response.json.status, 'awaiting_approval');
  assert.deepEqual(Object.keys(response.json).sort(), ['job_id', 'status']);
  const view = (await env.ui('GET', '/api/delegation')).json;
  assert.equal(view.pending.length, 1);
  const card = view.pending[0];
  assert.equal(card.job_id, response.json.job_id);
  assert.deepEqual(card.caller, { client: 'vscode', name: 'copilot' }, 'invisible characters are stripped from caller labels');
  assert.match(card.task, /password=\[redacted\]/); assert.doesNotMatch(card.task, /hunter2/);
  assert.equal(card.task.includes('‮'), false);
  assert.equal(card.task_masked, true); assert.equal(card.context_chars, 1234);
  assert.equal(card.allow_files, false); assert.equal(card.files_requested, false);
  assert.ok(card.expires_in_ms > 0 && card.expires_in_ms <= 60000);
  assert.equal(JSON.stringify(view).includes('xxxxxxxxxx'), false, 'the context itself is never on the card');
  const status = (await env.ui('GET', '/api/status')).json.delegate;
  assert.deepEqual(status, { enabled: true, queue: 1, approval: 'per_job' });
});

test('approve runs the job; deny and expiry never run it', async t => {
  const env = await setup(t, { options: { approval_timeout_ms: 300 } });
  const approved = (await env.submit()).json.job_id;
  const denied = (await env.submit()).json.job_id;
  const expired = (await env.submit()).json.job_id;
  env.engine.plan.push({ text: 'The note says hello.' });
  assert.deepEqual((await env.ui('POST', `/api/delegation/jobs/${approved}/decision`, { approved: true })).json, { accepted: true, status: 'queued' });
  assert.equal((await env.ui('POST', `/api/delegation/jobs/${denied}/decision`, { approved: false })).status, 200);
  const done = await env.until(approved, job => job.status === 'completed');
  assert.equal(done.answer, 'The note says hello.'); assert.equal(done.truncated, false); assert.equal(done.error, undefined);
  const no = (await env.poll(denied)).json;
  assert.equal(no.status, 'denied'); assert.equal(no.error.code, 'operator_denied'); assert.equal(typeof no.error.message, 'string'); assert.equal(no.answer, undefined);
  const late = await env.until(expired, job => job.status === 'expired');
  assert.equal(late.error.code, 'approval_expired');
  assert.equal(env.engine.calls.length, 1, 'only the approved job reached the engine');
  // Late or repeated answers are refused honestly.
  assert.equal((await env.ui('POST', `/api/delegation/jobs/${expired}/decision`, { approved: true })).status, 410);
  assert.equal((await env.ui('POST', `/api/delegation/jobs/${denied}/decision`, { approved: true })).status, 409);
  assert.equal((await env.ui('POST', '/api/delegation/jobs/job_unknown_123/decision', { approved: true })).status, 404);
  assert.equal((await env.ui('POST', `/api/delegation/jobs/${approved}/decision`, { approved: 'yes' })).status, 400);
  assert.equal((await env.ui('POST', `/api/delegation/jobs/${approved}/decision`, { approved: true, extra: 1 })).status, 400);
});

test('long poll: returns early on a change, waits out quiet periods, validates wait', async t => {
  const env = await setup(t);
  const id = (await env.submit()).json.job_id;
  const quick = Date.now(); const now = await env.poll(id, 0);
  assert.equal(now.json.status, 'awaiting_approval'); assert.equal(now.json.phase, 'waiting_for_operator'); assert.ok(Date.now() - quick < 500);
  assert.equal(typeof now.json.elapsed_s, 'number'); assert.equal(now.json.truncated, false);
  const quiet = Date.now(); const waited = await env.poll(id, 1);
  assert.equal(waited.json.status, 'awaiting_approval'); assert.ok(Date.now() - quiet >= 900, 'waited about a second');
  env.engine.plan.push({ hold: true });
  const started = Date.now(); const pending = env.poll(id, 10);
  await delay(200);
  await env.ui('POST', `/api/delegation/jobs/${id}/decision`, { approved: true });
  const changed = await pending;
  assert.notEqual(changed.json.status, 'awaiting_approval'); assert.ok(Date.now() - started < 3000, 'returned on the change, not after 10 s');
  for (const query of ['wait=16', 'wait=-1', 'wait=1.5', 'wait=abc', 'wait=1&wait=2', 'foo=1', 'wait=01x']) assert.equal((await env.call('GET', `/api/delegate/jobs/${id}?${query}`)).status, 400, query);
  assert.equal((await env.call('GET', '/api/delegate/jobs/short')).status, 404);
  assert.equal((await env.call('GET', '/api/delegate/jobs/job_doesnotexist_000')).status, 404);
  env.engine.releaseHolds();
});

test('pending jobs are capped at three (409 + retry_after_s), and starts at six a minute (429)', async t => {
  const env = await setup(t);
  const ids = [];
  for (let i = 0; i < 3; i += 1) { const response = await env.submit(); assert.equal(response.status, 202); ids.push(response.json.job_id); }
  const full = await env.submit();
  assert.equal(full.status, 409); assert.equal(full.json.error, 'queue_full'); assert.ok(full.json.retry_after_s >= 1);
  // Deny them so the queue is empty, then show the per-minute start limit.
  for (const id of ids) await env.ui('POST', `/api/delegation/jobs/${id}/decision`, { approved: false });
  for (let i = 0; i < 3; i += 1) { const response = await env.submit(); assert.equal(response.status, 202); await env.ui('POST', `/api/delegation/jobs/${response.json.job_id}/decision`, { approved: false }); }
  const limited = await env.submit();
  assert.equal(limited.status, 429); assert.equal(limited.json.error, 'rate_limited'); assert.ok(limited.json.retry_after_s >= 1 && limited.json.retry_after_s <= 60);
});

test('an operator grant approves delegated jobs only, shows as granted, and its revocation withdraws its approvals', async t => {
  const env = await setup(t);
  // Some other grant does not approve a job.
  assert.equal((await env.ui('POST', '/api/operator-grants/local.clipboard', { granted: true, duration_ms: 900000 })).status, 200);
  const other = (await env.submit()).json;
  assert.equal(other.status, 'awaiting_approval');
  assert.equal((await env.call('GET', '/api/delegate/health')).json.approval, 'per_job');
  // The delegate grant (15 minutes) approves new jobs with no card.
  assert.equal((await env.ui('POST', '/api/operator-grants/delegate.read_only_jobs', { granted: true, duration_ms: 900000 })).status, 200);
  assert.equal((await env.call('GET', '/api/delegate/health')).json.approval, 'granted');
  assert.equal((await env.ui('GET', '/api/status')).json.delegate.approval, 'granted');
  // Hold the engine for the operator so granted jobs stay queued.
  const release = await env.delegate.interactiveBegin();
  const granted = (await env.submit()).json;
  assert.equal(granted.status, 'queued');
  assert.deepEqual((await env.ui('GET', '/api/delegation')).json.pending.map(card => card.job_id), [other.job_id], 'a granted job never shows a card');
  await env.ui('POST', `/api/delegation/jobs/${other.job_id}/decision`, { approved: true });
  // Revoking the grant cancels what it approved, not what the operator approved.
  assert.equal((await env.ui('POST', '/api/operator-grants/delegate.read_only_jobs', { granted: false })).status, 200);
  assert.equal((await env.poll(granted.job_id)).json.status, 'cancelled');
  assert.equal((await env.poll(other.job_id)).json.status, 'queued');
  assert.equal((await env.call('GET', '/api/delegate/health')).json.approval, 'per_job');
  const after = (await env.submit()).json; assert.equal(after.status, 'awaiting_approval');
  release();
  await env.until(other.job_id, job => job.status === 'completed');
});

test('health reports readiness, queue, busy, and approval mode', async t => {
  const env = await setup(t);
  const health = await env.call('GET', '/api/delegate/health');
  assert.equal(health.status, 200);
  assert.deepEqual(health.json, { ready: true, engine: 'ready', queue: 0, busy: false, approval: 'per_job' });
  assert.equal((await env.call('GET', '/api/delegate/health?x=1')).status, 404);
  assert.equal((await env.call('POST', '/api/delegate/health', { body: {} })).status, 404);
  assert.equal((await env.call('GET', '/api/delegate/nothing')).status, 404);
});

test('the delegate grant approves nothing but delegated jobs', async t => {
  const { applyOperatorGrantPolicy } = await import('../../host/providers/operator-tool-policy.mjs');
  const env = await setup(t);
  await env.ui('POST', '/api/operator-grants/delegate.read_only_jobs', { granted: true, duration_ms: 3600000 });
  assert.equal(env.delegate.approvalMode(), 'granted');
  // A local tool guarded by its own capability still asks the operator.
  const clipboard = applyOperatorGrantPolicy({ name: 'clipboard.write', risk_tier: 'T2', requires_confirmation: true, execute: async () => ({}) }, { grantControl: env.grants, capabilityForCall: () => 'local.clipboard' });
  assert.equal(await clipboard.confirmationRequired({ arguments: {} }, {}), true);
  // Even a tool that named the delegate capability is not a delegated job: the
  // binding is for provider local_delegate, which no tool policy uses.
  assert.deepEqual(env.grants.list().filter(item => item.granted).map(item => item.capability), ['delegate.read_only_jobs']);
});
