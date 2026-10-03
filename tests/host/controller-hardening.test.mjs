import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ConversationController, DEFAULT_CONFIRMATION_TIMEOUT_MS } from '../../host/agent/controller.mjs';
import { validateEvent } from '../../host/agent/assistant-events.mjs';
import { mergeConfig, validateConfig } from '../../host/agent/config.mjs';
import { ActionJournal } from '../../host/agent/action-journal.mjs';
import { CONTEXT_DEFAULTS, utf8Bytes } from '../../host/agent/context-budget.mjs';
import { ENGINE_MAX_MESSAGE_BYTES, TRUNCATION_MARKER, toolResultByteCap, truncateUtf8 } from '../../host/agent/tool-result-cap.mjs';
import { REDACTED, looksLikeCredential, summarizeToolArguments } from '../../host/agent/argument-summary.mjs';
import { FixtureEngineClient } from '../../host/engine/fixture-engine.mjs';
import { NativeEngineError } from '../../host/engine/native-engine-client.mjs';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';

const tick = () => new Promise(resolve => setTimeout(resolve, 1));
async function until(predicate) { while (!predicate()) await tick(); }

// Emits each scripted tool call in turn (one per model call), then `answer`.
// Records every request so tests can inspect exactly what the engine saw.
function scriptedEngine({ calls = [], answer = 'All done.', fail } = {}) {
  const engine = {
    requests: [],
    async *generate({ messages, tools, signal }) {
      engine.requests.push({ messages: structuredClone(messages), tools: structuredClone(tools) });
      if (fail) { yield* fail(engine.requests.length, signal); return; }
      const done = messages.slice(messages.findLastIndex(message => message.role === 'user')).filter(message => message.role === 'tool').length;
      if (done < calls.length) { yield { kind: 'tool_call_chunk', text: JSON.stringify(calls[done]) }; return; }
      yield { kind: 'text_delta', text: answer }; yield { kind: 'done', finish_reason: 'stop' };
    },
  };
  return engine;
}
const textResult = ({ id, name }, text, extra = {}) => ({ id, name, status: 'ok', content: [{ type: 'text', text }], metadata: { truncated: false, duration_ms: 0 }, ...extra });
const readTool = (name, execute) => ({ name, risk_tier: 'T0', side_effect: 'none', description: 'test tool', parameters: { type: 'object', properties: {}, additionalProperties: false }, execute });
async function run(controller, { sessionId = 'ses_harden01', requestId = 'req_harden01', message = 'go', ...options } = {}) {
  const events = []; const result = await controller.runTurn({ sessionId, requestId, message, onEvent: event => events.push(event), ...options });
  events.forEach(validateEvent); return { result, events };
}
async function workspace(t, prefix) {
  const path = await realpath(await mkdtemp(join(tmpdir(), prefix))); await chmod(path, 0o700);
  t.after(() => rm(path, { recursive: true, force: true })); return path;
}
async function journal(t) {
  const path = await workspace(t, 'lae-harden-journal-'); let id = 0; let step = 0;
  const opened = await ActionJournal.open({ directory: path, testOnly: true, idFactory: () => `act_${(++id).toString(16).padStart(32, '0')}`, now: () => new Date(Date.UTC(2026, 0, 1, 0, 0, step++)).toISOString() });
  t.after(() => opened.close?.()); return opened;
}

// ---- 1. per-message limit is the engine's UTF-8 byte limit ------------------

test('a user message is limited in UTF-8 bytes, not UTF-16 characters', async () => {
  const engine = scriptedEngine(); const controller = new ConversationController({ engine });
  for (const message of ['漢'.repeat(20000), '😀'.repeat(9000), 'a'.repeat(ENGINE_MAX_MESSAGE_BYTES + 1)]) {
    assert.ok(message.length <= 32768 || message.startsWith('a'), 'the multi-byte cases pass a character-count check');
    await assert.rejects(controller.runTurn({ sessionId: 'ses_bytes001', requestId: 'req_bytes001', message }), error => {
      assert.equal(error.code, 'invalid_message_too_large'); assert.equal(error.bytes, utf8Bytes(message)); assert.equal(error.limit_bytes, 32768);
      assert.match(error.message, /bytes of UTF-8 text; the limit is 32768 bytes/); return true;
    });
  }
  assert.equal(engine.requests.length, 0, 'the engine is never asked'); assert.equal(controller.sessions.has('ses_bytes001'), false, 'refused before any session state changes');
  await assert.rejects(controller.runTurn({ sessionId: 'ses_bytes001', requestId: 'req_bytes002', message: '   ' }), error => error.code === 'invalid_message');
  const ok = await run(controller, { sessionId: 'ses_bytes001', requestId: 'req_bytes003', message: '漢字'.repeat(1000) });
  assert.equal(ok.result.state, 'COMPLETED');
  // Exactly at the byte limit the gate accepts; the token budget decides next.
  const edge = await run(controller, { sessionId: 'ses_bytes002', requestId: 'req_bytes004', message: 'a'.repeat(ENGINE_MAX_MESSAGE_BYTES) });
  assert.notEqual(edge.result.error, 'invalid_message_too_large');
});

// ---- 2. oversized tool results are capped as they arrive --------------------

test('a 1 MB tool result is truncated with a visible marker and the turn completes', async () => {
  const call = { id: 'call_bigout01', name: 'test.dump', arguments: {} };
  const engine = scriptedEngine({ calls: [call], answer: 'Summarised.' });
  const big = 'x'.repeat(1024 * 1024);
  const controller = new ConversationController({ engine, toolRegistry: { 'test.dump': readTool('test.dump', async c => textResult(c, big)) } });
  const { result, events } = await run(controller, { requestId: 'req_bigout01' });
  assert.equal(result.state, 'COMPLETED');
  const completed = events.find(event => event.event === 'tool.completed').data.result;
  assert.equal(completed.metadata.truncated, true);
  const sent = engine.requests[1].messages.at(-1); assert.equal(sent.role, 'tool');
  assert.match(sent.content, TRUNCATION_MARKER); assert.match(sent.content, /\[Output truncated: \d+ of 1048576 bytes shown\.\]$/);
  assert.ok(utf8Bytes(sent.content) <= ENGINE_MAX_MESSAGE_BYTES, 'within the engine per-message cap');
  const cap = toolResultByteCap({ history: engine.requests[0].messages, pending: [{ role: 'assistant', content: JSON.stringify(call) }], tools: engine.requests[0].tools, contextTokens: 8192, maxOutputTokens: CONTEXT_DEFAULTS.maxOutputTokens });
  assert.ok(utf8Bytes(sent.content) <= cap, 'within a quarter of the remaining budget');
  assert.equal(sent.content, completed.content[0].text, 'the UI sees what the model sees');
});

test('a result under the envelope limit but over the engine cap is cut on a character boundary', async () => {
  const call = { id: 'call_bigout02', name: 'test.dump', arguments: {} };
  const engine = scriptedEngine({ calls: [call] });
  const text = 'é漢😀'.repeat(6000); // 54,000 bytes, 24,000 UTF-16 units
  const controller = new ConversationController({ engine, toolRegistry: { 'test.dump': readTool('test.dump', async c => textResult(c, text)) } });
  const { result } = await run(controller, { requestId: 'req_bigout02' });
  assert.equal(result.state, 'COMPLETED');
  const sent = engine.requests[1].messages.at(-1).content;
  assert.ok(utf8Bytes(sent) < utf8Bytes(text)); assert.ok(!sent.includes('\uFFFD'), 'no broken multi-byte sequence');
  assert.ok(text.startsWith(sent.replace(TRUNCATION_MARKER, '')), 'the head is kept verbatim');
  // A small result is untouched.
  assert.deepEqual(truncateUtf8('short', 512), { text: 'short', truncated: false, shownBytes: 5, totalBytes: 5 });
});

test('a provider result is never cut before attestation, but its model-visible projection is capped', async () => {
  const call = { id: 'call_page0001', name: 'browser.inspect_page', arguments: {} };
  const engine = scriptedEngine({ calls: [call] });
  // 16,384 CJK characters: within the browser projection's character bound,
  // but 49,152 bytes, over the engine's per-message byte cap.
  const page = JSON.stringify({ state: 'ready', text: '漢'.repeat(16384) });
  const tool = { ...readTool('browser.inspect_page', async c => textResult(c, page)), risk_tier: 'T1', side_effect: 'browser_read' };
  const controller = new ConversationController({ engine, toolRegistry: { 'browser.inspect_page': tool } });
  const { result, events } = await run(controller, { requestId: 'req_page0001' });
  assert.equal(result.state, 'COMPLETED');
  const sent = engine.requests[1].messages.at(-1).content;
  assert.ok(utf8Bytes(sent) <= ENGINE_MAX_MESSAGE_BYTES); assert.match(sent, TRUNCATION_MARKER);
  assert.equal(events.find(event => event.event === 'tool.completed').data.result.metadata.truncated, true);
});

// ---- 3. a failed turn rolls back to what the user saw -----------------------

test('a turn that fails before any output removes its user message; prior history is untouched', async () => {
  let failing = false;
  const engine = scriptedEngine({ fail: async function* () { if (failing) throw new NativeEngineError('engine_timeout', 'timed out'); yield { kind: 'text_delta', text: 'Hello.' }; yield { kind: 'done' }; } });
  const controller = new ConversationController({ engine });
  await run(controller, { requestId: 'req_roll0001', message: 'first' });
  const session = controller.sessions.get('ses_harden01'); const before = structuredClone(session.history); const bytesBefore = session.history_bytes;
  failing = true;
  const { result, events } = await run(controller, { requestId: 'req_roll0002', message: 'second' });
  assert.equal(result.state, 'FAILED'); assert.equal(session.state, 'FAILED');
  assert.equal(events.at(-1).event, 'request.failed'); assert.equal(events.at(-1).data.user_message_kept, false);
  assert.deepEqual(session.history, before, 'the unanswered message is gone and nothing else changed');
  assert.equal(session.history_bytes, bytesBefore);
  failing = false;
  await run(controller, { requestId: 'req_roll0003', message: 'third' });
  assert.deepEqual(engine.requests.at(-1).messages.map(message => message.content), ['first', 'Hello.', 'third'], 'the next prompt extends the old prefix exactly');
});

test('a partial answer keeps the user message and records the cut-off answer; cancel without output rolls back', async () => {
  const engine = scriptedEngine({ fail: async function* (n, signal) {
    if (n === 1) { yield { kind: 'text_delta', text: 'The first half' }; throw new NativeEngineError('engine_timeout', 'stalled'); }
    await new Promise((resolve, reject) => { signal.addEventListener('abort', () => reject(Object.assign(new Error('cancelled'), { code: 'cancelled' })), { once: true }); });
  } });
  const controller = new ConversationController({ engine });
  const partial = await run(controller, { requestId: 'req_part0001', message: 'explain' });
  const session = controller.sessions.get('ses_harden01');
  assert.equal(partial.result.state, 'FAILED'); assert.equal(partial.events.at(-1).data.user_message_kept, true);
  assert.deepEqual(session.history.map(message => message.role), ['user', 'assistant']);
  assert.match(session.history[1].content, /^The first half\n\n\[This answer was interrupted before it finished\.\]$/);
  const events = []; const pending = controller.runTurn({ sessionId: 'ses_harden01', requestId: 'req_part0002', message: 'never mind', onEvent: event => events.push(event) });
  await until(() => engine.requests.length === 2); controller.cancel('req_part0002');
  const cancelled = await pending;
  assert.equal(cancelled.state, 'CANCELLED'); assert.equal(events.at(-1).data.user_message_kept, false);
  assert.deepEqual(session.history.map(message => message.role), ['user', 'assistant']);
  assert.equal(session.history_bytes, session.history.reduce((sum, message) => sum + Buffer.byteLength(JSON.stringify(message)), 0));
});

test('a completed tool step is kept when the continuation fails, because its effect happened', async () => {
  const call = { id: 'call_keep0001', name: 'test.read', arguments: {} };
  let n = 0;
  const engine = scriptedEngine({ fail: async function* () { n += 1; if (n === 1) { yield { kind: 'tool_call_chunk', text: JSON.stringify(call) }; return; } throw new NativeEngineError('engine_timeout', 'stalled'); } });
  const controller = new ConversationController({ engine, toolRegistry: { 'test.read': readTool('test.read', async c => textResult(c, 'value')) } });
  const { result, events } = await run(controller, { requestId: 'req_keep0001' });
  assert.equal(result.state, 'FAILED'); assert.equal(events.at(-1).data.user_message_kept, true);
  assert.deepEqual(controller.sessions.get('ses_harden01').history.map(message => message.role), ['user', 'assistant', 'tool']);
});

// ---- 4. confirmation timeout ------------------------------------------------

test('the confirmation timeout defaults to 120 s and is configurable and validated', () => {
  const engine = scriptedEngine();
  assert.equal(DEFAULT_CONFIRMATION_TIMEOUT_MS, 120000);
  assert.equal(new ConversationController({ engine }).confirmationTimeoutMs, 120000);
  assert.equal(new ConversationController({ engine, confirmationTimeoutMs: 45000 }).confirmationTimeoutMs, 45000);
  assert.throws(() => new ConversationController({ engine, confirmationTimeoutMs: 0 }), /confirmationTimeoutMs/);
  assert.equal(mergeConfig({}).host.confirmation_timeout_ms, 120000);
  assert.equal(mergeConfig({ host: { confirmation_timeout_ms: 300000 } }).host.confirmation_timeout_ms, 300000);
  for (const bad of [4999, 600001, 1.5, '120000']) assert.throws(() => validateConfig({ host: { confirmation_timeout_ms: bad } }), /confirmation_timeout_ms/);
});

test('an expired confirmation is neither approval nor denial, and a late click is recognised as expired', async () => {
  let executed = 0;
  const call = { id: 'call_expire01', name: 'test.confirm', arguments: {} };
  const engine = scriptedEngine({ calls: [call], answer: 'Should I try again?' });
  const controller = new ConversationController({ engine, confirmationTimeoutMs: 30, toolRegistry: { 'test.confirm': { ...readTool('test.confirm', async c => { executed += 1; return textResult(c, 'ran'); }), risk_tier: 'T1', side_effect: 'read_sensitive', requires_confirmation: true } } });
  const { result, events } = await run(controller, { requestId: 'req_expire01' });
  assert.equal(result.state, 'COMPLETED'); assert.equal(executed, 0);
  const required = events.find(event => event.event === 'tool.confirmation_required');
  const failed = events.find(event => event.event === 'tool.failed');
  assert.equal(failed.data.code, 'confirmation_expired'); assert.equal(failed.data.confirmation_id, required.data.confirmation_id);
  assert.equal(events.some(event => event.event === 'tool.started'), false, 'nothing started');
  const completed = events.find(event => event.event === 'tool.completed').data.result;
  assert.equal(completed.status, 'cancelled'); assert.match(completed.content[0].text, /expired/); assert.doesNotMatch(completed.content[0].text, /denied this action/);
  assert.match(engine.requests[1].messages.at(-1).content, /neither approved nor denied/);
  const ids = { requestId: 'req_expire01', callId: 'call_expire01' };
  assert.equal(controller.confirm(required.data.confirmation_id, true, ids), false, 'a late approval runs nothing');
  assert.equal(controller.confirmationExpired(required.data.confirmation_id, ids), true);
  assert.equal(controller.confirmationExpired(required.data.confirmation_id, { requestId: 'req_other001', callId: 'call_expire01' }), false);
  assert.equal(controller.confirmationExpired('cnf_unknown000000', ids), false);
});

// ---- 5. what the user is approving ------------------------------------------

test('fs.write_new confirmation shows a bounded, credential-screened summary and executes the original arguments', async t => {
  const root = await workspace(t, 'lae-harden-ws-');
  const tools = createLocalToolRegistry({ workspaces: [{ id: 'notes', path: root, read: true, write: true }] });
  const secret = `API_KEY=sk-${'A1b2'.repeat(10)}\n`;
  const content = `# Plan\n${'line of text\n'.repeat(40)}${secret}`;
  const plain = `Shopping list\n\u202Eeggs\n${'milk '.repeat(100)}`;
  const calls = [
    { id: 'call_write0001', name: 'fs.write_new', arguments: { workspace_id: 'notes', path: 'plan.md', content } },
    { id: 'call_write0002', name: 'fs.write_new', arguments: { workspace_id: 'notes', path: 'list.txt', content: plain } },
  ];
  const engine = scriptedEngine({ calls });
  const controller = new ConversationController({ engine, actionJournal: await journal(t), confirmationTimeoutMs: 5000, toolRegistry: { 'fs.write_new': tools['fs.write_new'] } });
  const events = [];
  const result = await controller.runTurn({ sessionId: 'ses_write001', requestId: 'req_write001', message: 'save my notes', onEvent: event => {
    events.push(event);
    if (event.event === 'tool.confirmation_required') queueMicrotask(() => controller.confirm(event.data.confirmation_id, true, { requestId: 'req_write001', callId: event.data.call.id }));
  } });
  events.forEach(validateEvent);
  assert.equal(result.state, 'COMPLETED');
  const [first, second] = events.filter(event => event.event === 'tool.confirmation_required').map(event => event.data);
  assert.deepEqual(first.call, { id: 'call_write0001', name: 'fs.write_new' }, 'call keeps its {id, name} shape');
  // Only the credential VALUE is masked; the rest stays visible (hiding the
  // whole value let a secret blind the card to a payload beside it), and a
  // long value shows its tail too, so the masked key at the end is seen.
  const shown = Array.from(content.replace(secret, 'API_KEY=[redacted]\n'));
  assert.deepEqual(first.arguments_summary, { kind: 'file_create', workspace_id: 'notes', path: 'plan.md', content_bytes: utf8Bytes(content), content_excerpt: shown.slice(0, 200).join(''), content_excerpt_tail: shown.slice(-200).join(''), content_redacted: false, content_masked: true, content_excerpt_truncated: true, content_chars: Array.from(content).length });
  assert.ok(first.arguments_summary.content_excerpt_tail.endsWith('API_KEY=[redacted]\n'));
  assert.equal(JSON.stringify(events).includes('A1b2A1b2'), false, 'the secret never reaches an event');
  assert.equal(second.arguments_summary.content_redacted, false); assert.equal(second.arguments_summary.content_excerpt_truncated, true);
  assert.equal(Array.from(second.arguments_summary.content_excerpt).length, 200, 'bounded to 200 characters');
  assert.ok(!second.arguments_summary.content_excerpt.includes('\u202E'), 'bidi override neutralised');
  const proposed = events.find(event => event.event === 'tool.proposed' && event.data.call.id === 'call_write0001');
  assert.deepEqual(proposed.data.arguments_summary, first.arguments_summary);
  assert.equal(await readFile(join(root, 'plan.md'), 'utf8'), content, 'the approved call wrote the ORIGINAL content, secret included');
  assert.equal(await readFile(join(root, 'list.txt'), 'utf8'), plain);
});

test('argument summaries for patch, process, URL, app and clipboard are bounded and screened', () => {
  const patch = summarizeToolArguments('fs.apply_patch', { workspace_id: 'notes', path: 'a.txt', base_sha256: 'a'.repeat(64), replacement: 'new text '.repeat(1000) }, { old_bytes: 12, changed: true, diff: 'x'.repeat(8192) });
  assert.equal(patch.kind, 'file_replace'); assert.equal(patch.old_bytes, 12); assert.equal(patch.new_bytes, 9000); assert.equal(patch.changed, true);
  assert.equal(Array.from(patch.replacement_excerpt).length, 200); assert.equal(JSON.stringify(patch).includes('xxxx'), false, 'the full diff is not copied');
  const proc = summarizeToolArguments('process.run_allowlisted', { action_id: 'build', parameters: { target: 'web', api_token: 'abc', note: 'password=hunter2' } }, { executable: '/usr/bin/make', argv: ['make', 'web', `--header=Authorization: Bearer ${'z'.repeat(40)}`], cwd: 'proj' });
  // Span masking: values are masked in place; a credential-NAMED parameter
  // is still hidden whole.
  assert.deepEqual(proc, { kind: 'process_run', action_id: 'build', executable: '/usr/bin/make', argv: ['make', 'web', '--header=Authorization: Bearer [redacted]'], cwd: 'proj', parameters: { target: 'web', api_token: REDACTED, note: 'password=[redacted]' } });
  assert.equal(JSON.stringify(proc).includes('hunter2') || JSON.stringify(proc).includes('zzzz'), false, 'no secret reaches the summary');
  const url = summarizeToolArguments('browser.open_url', { url: 'https://example.com/docs/page?lang=en&access_token=eyJhbGciOi.secret&x=1#frag' });
  assert.equal(url.url, 'https://example.com/docs/page?lang=en&access_token=[redacted]&x=1#frag');
  assert.deepEqual(summarizeToolArguments('app.open', { app_id: 'notepad' }), { kind: 'open_app', app_id: 'notepad' });
  const clip = summarizeToolArguments('clipboard.write', { text: `ghp_${'q'.repeat(36)}` });
  // The token is masked in place rather than the whole excerpt hidden.
  assert.equal(clip.text_excerpt, '[redacted]'); assert.equal(clip.text_redacted, false); assert.equal(clip.text_masked, true); assert.equal(JSON.stringify(clip).includes('qqqq'), false);
  assert.equal(summarizeToolArguments('time.now', { format: 'local' }), null);
  assert.equal(summarizeToolArguments('fs.write_new', { workspace_id: 'n', path: 'p', content: 'x'.repeat(65536) }).content_bytes, 65536);
  for (const value of ['Authorization: Basic abc', 'see https://user:pw@host/x', '-----BEGIN RSA PRIVATE KEY-----', 'AKIAABCDEFGHIJKLMNOP', '"client_secret": "v"']) assert.equal(looksLikeCredential(value), true, value);
  for (const value of ['max_tokens: 5', 'Meeting at 10:30 about the token budget', 'C:\\Users\\me\\notes.txt']) assert.equal(looksLikeCredential(value), false, value);
});

// ---- 7. fixture usage no longer feeds a fake token count --------------------

test('the fixture engine reports no prompt-token count, so ratio learning stays off', async () => {
  const fixture = new FixtureEngineClient({ delayMs: 0 });
  const frames = []; for await (const frame of fixture.generate({ requestId: 'req_fixture1', messages: [{ role: 'user', content: 'hello there' }] })) frames.push(frame);
  const answer = frames.filter(frame => frame.kind === 'text_delta').map(frame => frame.text).join('');
  assert.deepEqual(frames.at(-1).usage, { completion_tokens: Math.ceil(utf8Bytes(answer) / CONTEXT_DEFAULTS.bytesPerToken) });
  const controller = new ConversationController({ engine: fixture });
  await run(controller, { sessionId: 'ses_fixture1', requestId: 'req_fixture2', message: 'what time is it?' });
  assert.equal(controller.sessions.get('ses_fixture1').bytes_per_token, null);
});

// ---- 8. tools off -----------------------------------------------------------

test('tools: off sends an empty tool list and refuses a call it never offered', async () => {
  let executed = 0;
  const registry = { 'test.read': readTool('test.read', async c => { executed += 1; return textResult(c, 'value'); }) };
  const plain = scriptedEngine({ answer: 'Plain chat.' });
  const controller = new ConversationController({ engine: plain, toolRegistry: registry });
  const off = await run(controller, { requestId: 'req_tools001', tools: 'off' });
  assert.equal(off.result.state, 'COMPLETED'); assert.deepEqual(plain.requests[0].tools, []);
  assert.equal(off.events[0].data.tools, 'off');
  await run(controller, { requestId: 'req_tools002' });
  assert.ok(plain.requests[1].tools.length >= 2, 'the next turn may switch tools back on');
  const rogue = scriptedEngine({ calls: [{ id: 'call_rogue001', name: 'test.read', arguments: {} }] });
  const strict = new ConversationController({ engine: rogue, toolRegistry: registry });
  const refused = await run(strict, { requestId: 'req_tools003', tools: 'off' });
  assert.equal(refused.result.error, 'tool_call_not_offered'); assert.equal(executed, 0);
  await assert.rejects(strict.runTurn({ sessionId: 'ses_tools001', requestId: 'req_tools004', message: 'x', tools: 'none' }), error => error.code === 'invalid_tools_mode');
});

// ---- finish reason ------------------------------------------------------------

test('message.completed carries the engine finish reason so the UI can offer Continue', async () => {
  const reasons = ['length', 'stop', undefined, 'bogus'];
  const engine = { async *generate() { const reason = reasons.shift(); yield { kind: 'text_delta', text: 'Partial answer' }; yield { kind: 'done', ...(reason === undefined ? {} : { finish_reason: reason }), usage: { prompt_tokens: 40, completion_tokens: 2 } }; } };
  const controller = new ConversationController({ engine });
  const seen = [];
  for (const n of [1, 2, 3, 4]) {
    const { events } = await run(controller, { requestId: `req_finish00${n}` });
    seen.push(events.find(event => event.event === 'message.completed').data.finish_reason);
  }
  assert.deepEqual(seen, ['length', 'stop', 'stop', 'stop']);
});
