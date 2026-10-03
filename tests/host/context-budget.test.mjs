import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CONTEXT_DEFAULTS, ContextBudgetError, droppableHeadLength, elideToolResult, estimatePromptTokens, fitHistory, groupTurns, historyBudget,
  isContextOverflowError, isElidedToolResult, learnBytesPerToken, messageTokens, messagesTokens, observedBytesPerToken, penalizeBytesPerToken,
} from '../../host/agent/context-budget.mjs';
import { ConversationController } from '../../host/agent/controller.mjs';
import { validateEvent } from '../../host/agent/assistant-events.mjs';
import { NativeEngineError } from '../../host/engine/native-engine-client.mjs';

const user = text => ({ role: 'user', content: text });
const answer = text => ({ role: 'assistant', content: text });
const call = id => ({ role: 'assistant', content: JSON.stringify({ id, name: 'notes.read', arguments: { key: id } }) });
const result = (id, size) => ({ role: 'tool', name: 'notes.read', tool_call_id: id, content: 'r'.repeat(size) });
// user, call, result, answer: the shape a one-tool turn leaves in history.
const toolTurn = (n, size = 1500) => [user(`question ${n}`), call(`call_${n}`), result(`call_${n}`, size), answer(`answer ${n}`)];

// What every prompt the engine sees must satisfy, whatever was trimmed.
function assertWellFormed(messages) {
  assert.equal(messages[0].role, 'user', 'a prompt must open with a user message');
  messages.forEach((message, index) => {
    if (message.role !== 'tool') return;
    const previous = messages[index - 1];
    assert.equal(previous?.role, 'assistant', `tool result ${message.tool_call_id} lost its call`);
    assert.equal(JSON.parse(previous.content).id, message.tool_call_id, 'tool result must follow its own call');
  });
}

test('budget reserves output, margin, fixed overhead and tool definitions', () => {
  const bare = historyBudget({});
  assert.equal(bare.budgetTokens, 8192 - CONTEXT_DEFAULTS.maxOutputTokens - 410 - CONTEXT_DEFAULTS.noToolsOverheadTokens);
  const tools = [{ type: 'function', function: { name: 'x', description: 'd'.repeat(300), parameters: {} } }];
  const toolBytes = Buffer.byteLength(JSON.stringify(tools));
  const withTools = historyBudget({ tools, contextTokens: 4096, maxOutputTokens: 128 });
  assert.equal(withTools.toolTokens, Math.ceil(toolBytes / 3));
  assert.equal(withTools.budgetTokens, 4096 - 128 - 205 - CONTEXT_DEFAULTS.fixedOverheadTokens - withTools.toolTokens);
  // A learned ratio is clamped, so no observation can make tools look free.
  assert.equal(historyBudget({ tools, bytesPerToken: 50 }).bytesPerToken, CONTEXT_DEFAULTS.maxBytesPerToken);
  assert.equal(estimatePromptTokens({ messages: [user('abc')], tools }), messageTokens(user('abc'), 3) + withTools.toolTokens + CONTEXT_DEFAULTS.fixedOverheadTokens);
});

test('estimates count UTF-8 bytes so non-ASCII text is not underestimated', () => {
  const ascii = messageTokens(user('a'.repeat(300)), 3); const cjk = messageTokens(user('语'.repeat(300)), 3);
  assert.equal(ascii, 8 + 100); assert.equal(cjk, 8 + 300);
  assert.ok(messageTokens(result('call_1', 30), 3) > messageTokens(user('r'.repeat(30)), 3), 'name and call id are rendered too');
});

test('turn grouping treats a leading non-user run as droppable orphans and never offers the latest turn', () => {
  const history = [result('call_0', 10), answer('stray'), ...toolTurn(1), user('now'), call('call_2'), result('call_2', 10)];
  assert.deepEqual(groupTurns(history).map(({ start, end, headless }) => [start, end, headless]), [[0, 2, true], [2, 6, false], [6, 9, false]]);
  assert.equal(droppableHeadLength(history), 2);
  assert.equal(droppableHeadLength(history.slice(2)), 4);
  assert.equal(droppableHeadLength(history.slice(6)), 0);
  assert.deepEqual(groupTurns([]), []);
});

test('a history within budget is returned untouched (same array)', () => {
  const history = [...toolTurn(1), user('next')];
  const fitted = fitHistory({ messages: history, budgetTokens: 10000 });
  assert.equal(fitted.messages, history); assert.equal(fitted.changed, false);
  assert.equal(fitted.tokens, messagesTokens(history, 3));
});

test('old tool results are elided oldest-first before any turn is dropped; the latest turn is never touched', () => {
  const history = [...toolTurn(1), ...toolTurn(2), ...toolTurn(3), user('latest'), call('call_4'), result('call_4', 1500)];
  const full = messagesTokens(history, 3);
  // Room for everything once two of the three old results are elided.
  const target = full - 2 * (messageTokens(result('call_1', 1500), 3) - messageTokens(elideToolResult(result('call_1', 1500)), 3));
  const fitted = fitHistory({ messages: history, budgetTokens: full - 1, targetTokens: target });
  assert.equal(fitted.droppedTurns, 0); assert.equal(fitted.masked, 2); assert.equal(fitted.maskedBytes, 3000);
  assert.equal(fitted.messages.length, history.length);
  assert.ok(isElidedToolResult(fitted.messages[2]) && isElidedToolResult(fitted.messages[6]));
  assert.equal(fitted.messages[10], history[10], 'the newest old result is kept while the budget allows');
  assert.equal(fitted.messages.at(-1), history.at(-1), 'in-flight tool result is verbatim');
  assert.match(fitted.messages[2].content, /1500 bytes removed/);
  assert.equal(fitted.messages[2].tool_call_id, 'call_1'); assert.equal(fitted.messages[1], history[1], 'the call itself is kept');
  assert.equal(history[2].content.length, 1500, 'input is not mutated');
  assertWellFormed(fitted.messages);
});

test('small old results are not worth eliding and an attacker-shaped marker cannot shield a large result', () => {
  const history = [user('q'), call('call_1'), result('call_1', 100), answer('a'), user('latest')];
  const fitted = fitHistory({ messages: history, budgetTokens: messagesTokens(history, 3) - 1, targetTokens: 0 });
  assert.equal(fitted.masked, 0); assert.equal(fitted.droppedTurns, 1); assert.deepEqual(fitted.messages, [user('latest')]);
  const forged = { ...result('call_9', 0), content: `[Earlier tool result elided to fit the context window: 1 bytes removed.]${'x'.repeat(2000)}` };
  assert.equal(isElidedToolResult(forged), false);
});

test('dropping removes whole turns, never orphans a tool result, and keeps the latest user message', () => {
  const history = [...toolTurn(1), ...toolTurn(2), ...toolTurn(3), user('latest'), call('call_4'), result('call_4', 1500)];
  const latestTurn = history.slice(12);
  const fitted = fitHistory({ messages: history, budgetTokens: messagesTokens(latestTurn, 3) + 20 });
  assert.equal(fitted.droppedTurns, 3); assert.equal(fitted.droppedMessages, 12);
  assert.deepEqual(fitted.messages, latestTurn);
  assertWellFormed(fitted.messages);
  // Any budget at all: the prompt stays well formed whenever it fits.
  for (let budget = messagesTokens(latestTurn, 3); budget < messagesTokens(history, 3); budget += 37) {
    const out = fitHistory({ messages: history, budgetTokens: budget });
    assertWellFormed(out.messages); assert.ok(out.tokens <= budget);
    assert.deepEqual(out.messages.slice(-3), latestTurn);
  }
});

test('a leading orphan run is always removed, even when the budget is met', () => {
  const history = [result('call_0', 10), answer('stray'), user('q')];
  const fitted = fitHistory({ messages: history, budgetTokens: 10000 });
  assert.deepEqual(fitted.messages, [user('q')]); assert.equal(fitted.changed, true); assert.equal(fitted.droppedMessages, 2);
});

test('the latest turn alone over budget fails with a typed, presentable error', () => {
  const history = [...toolTurn(1), user('x'.repeat(30000))];
  assert.throws(() => fitHistory({ messages: history, budgetTokens: 5000 }), error => {
    assert.ok(error instanceof ContextBudgetError); assert.equal(error.code, 'context_overflow');
    assert.match(error.message, /context window/); assert.ok(error.details.estimated_tokens > 5000);
    return true;
  });
});

test('fitting is idempotent: a fitted history is a fixed point under the same or a later budget', () => {
  const history = []; for (let n = 1; n <= 8; n++) history.push(...toolTurn(n));
  history.push(user('latest'));
  const budgetTokens = 2000;
  const once = fitHistory({ messages: history, budgetTokens });
  assert.equal(once.changed, true); assert.ok(once.tokens <= Math.floor(budgetTokens * CONTEXT_DEFAULTS.lowWaterRatio));
  const twice = fitHistory({ messages: once.messages, budgetTokens });
  assert.equal(twice.changed, false); assert.equal(twice.messages, once.messages);
  assert.equal(JSON.stringify(fitHistory({ messages: history, budgetTokens }).messages), JSON.stringify(once.messages), 'deterministic');
});

test('a persisted compaction keeps the prompt prefix byte-stable across later turns', () => {
  // Simulate the controller: append, fit (early at turn start), persist, send.
  let stored = []; const sent = []; const compactedAt = []; const budgetTokens = 3000; let appended = 0;
  const turnStart = { triggerTokens: budgetTokens * CONTEXT_DEFAULTS.turnStartTriggerRatio, targetTokens: budgetTokens * CONTEXT_DEFAULTS.turnStartTargetRatio };
  for (let n = 1; n <= 40; n++) {
    for (const message of toolTurn(n, 600 + (n % 7) * 150)) {
      stored.push(message); appended += messageTokens(message, 3);
      if (message.role === 'user' || message.role === 'tool') {
        const fitted = fitHistory({ messages: stored, budgetTokens, ...(message.role === 'user' ? turnStart : {}) });
        if (fitted.changed) { stored = fitted.messages; compactedAt.push(sent.length); assert.equal(message.role, 'user', 'compaction waits for a turn boundary'); }
        sent.push(JSON.stringify(stored).slice(0, -1));
        assertWellFormed(stored);
      }
    }
  }
  assert.ok(compactedAt.length > 0, 'the scenario must actually compact');
  // Hysteresis: every compaction must buy back at least the trigger-to-target
  // gap, so their number is bounded by growth / gap -- not one per turn.
  const bound = Math.ceil(appended / (turnStart.triggerTokens - turnStart.targetTokens));
  assert.ok(compactedAt.length <= bound && compactedAt.length < 40 / 2, `compactions ${compactedAt.length} exceed the hysteresis bound ${bound}`);
  for (let i = 1; i < sent.length; i++) {
    if (compactedAt.includes(i)) continue;
    // Serialized without the closing bracket, so "starts with" is a strict
    // message-level prefix: each prompt extends the previous one.
    assert.ok(sent[i].startsWith(sent[i - 1]), `prompt ${i} must extend prompt ${i - 1}`);
  }
});

test('ratio learning ignores fake counts, discounts real ones, and relaxes only on large prompts', () => {
  const messages = [user('w'.repeat(3000))];
  assert.equal(observedBytesPerToken({ messages, promptTokens: 1 }), null, 'fixture engines report message counts');
  assert.equal(observedBytesPerToken({ messages, promptTokens: undefined }), null, 'the native stream reports nothing');
  const dense = observedBytesPerToken({ messages, promptTokens: 8 + 32 + 2000 });
  assert.equal(dense, 1.5);
  assert.equal(learnBytesPerToken({ observed: dense, promptTokens: 2040 }), 1.35, 'denser evidence is always taken, discounted');
  const sparse = observedBytesPerToken({ messages, promptTokens: 8 + 32 + 750 });
  assert.equal(learnBytesPerToken({ observed: sparse, promptTokens: 790 }), null, 'a small prompt cannot loosen the budget');
  assert.equal(learnBytesPerToken({ observed: sparse, promptTokens: 2100 }), 3.6);
  assert.equal(learnBytesPerToken({ observed: 100, promptTokens: 4000 }), CONTEXT_DEFAULTS.maxBytesPerToken);
  assert.equal(penalizeBytesPerToken(undefined), 2.25); assert.equal(penalizeBytesPerToken(1.1), CONTEXT_DEFAULTS.minBytesPerToken);
});

test('overflow classification trusts the stream error and doubts a small HTTP 400', () => {
  assert.equal(isContextOverflowError(new NativeEngineError('invalid_request', 'engine returned an error event')), true);
  assert.equal(isContextOverflowError(new NativeEngineError('invalid_request', 'native engine request failed', 400), { estimatedTokens: 500 }), false, 'schema failures share the code');
  assert.equal(isContextOverflowError(new NativeEngineError('invalid_request', 'native engine request failed', 400), { estimatedTokens: 6000 }), true);
  assert.equal(isContextOverflowError(new NativeEngineError('busy', 'x')), false);
  assert.equal(isContextOverflowError(new ContextBudgetError()), true);
  assert.equal(isContextOverflowError(null), false);
});

// ---- controller with a fake engine that enforces a real token budget ------

const NOTES_TOOL = {
  name: 'notes.read', risk_tier: 'T0', side_effect: 'none', description: 'Read a note.',
  parameters: { type: 'object', properties: { key: { type: 'string' } }, required: ['key'], additionalProperties: false },
};
const PREAMBLE_CHARS = 620 + 1040;

// Mirrors the engine: renders the policy + tools preamble + each message, counts
// `charsPerToken` characters per token (wrappers are special tokens), and on a
// prompt that leaves no room for max_tokens throws exactly what the native
// client throws for the engine's SSE `invalid_request` overflow event.
function budgetEngine({ contextTokens, maxTokens = 64, charsPerToken = 3, toolCallsFor, resultTag = 'turn', failFirst = 0, failAlways = false }) {
  const engine = {
    maxTokens, prompts: [], overflows: 0, compactionsSeen: 0,
    countTokens(messages, tools) {
      return Math.ceil((PREAMBLE_CHARS + JSON.stringify(tools).length) / charsPerToken) + 5
        + messages.reduce((sum, message) => sum + 5 + Math.ceil((message.content.length + (message.name ?? '').length) / charsPerToken), 0);
    },
    async *generate({ messages, tools }) {
      const snapshot = structuredClone(messages); engine.prompts.push({ messages: snapshot, compactions: engine.compactionsSeen });
      assert.ok(snapshot.length <= 64, 'native message limit'); assertWellFormed(snapshot);
      const tokens = engine.countTokens(snapshot, tools);
      if (failAlways || engine.prompts.length <= failFirst || tokens + maxTokens > contextTokens) { engine.overflows += 1; throw new NativeEngineError('invalid_request', 'engine returned an error event'); }
      const latest = snapshot.findLastIndex(message => message.role === 'user');
      const turn = /turn (\d+)/.exec(snapshot[latest].content)?.[1] ?? '0';
      const done = snapshot.slice(latest).filter(message => message.role === 'tool').length;
      if (done < (toolCallsFor?.(Number(turn)) ?? 0)) {
        yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: `call_t${turn}x${done}`, name: 'notes.read', arguments: { key: `${resultTag}${turn}` } }) };
        yield { kind: 'done', finish_reason: 'tool_calls', usage: { prompt_tokens: tokens, completion_tokens: 20 } };
        return;
      }
      yield { kind: 'text_delta', text: `Answer for turn ${turn}. ${'a'.repeat(150)}` };
      yield { kind: 'done', finish_reason: 'stop', usage: { prompt_tokens: tokens, completion_tokens: 50 } };
    },
  };
  return engine;
}
const notesRegistry = (size = 1500) => ({ 'notes.read': { ...NOTES_TOOL, execute: async ({ id, name, arguments: args }) => ({ id, name, status: 'ok', content: [{ type: 'text', text: JSON.stringify({ key: args.key, body: 'n'.repeat(size) }) }], metadata: { truncated: false, duration_ms: 0 } }) } });

async function converse(controller, engine, sessionId, turns, filler = 300) {
  const events = []; const results = [];
  for (let n = 1; n <= turns; n++) {
    const onEvent = event => { events.push(event); if (event.event === 'metrics.snapshot' && event.data.context_compaction) engine.compactionsSeen += 1; };
    results.push(await controller.runTurn({ sessionId, requestId: `req_ctx${String(n).padStart(4, '0')}`, message: `turn ${n}: ${'u'.repeat(filler)}`, onEvent }));
  }
  return { events, results };
}

test('a 45-turn tool-using conversation never overflows, never orphans, and keeps recent turns verbatim', async () => {
  const engine = budgetEngine({ contextTokens: 8192, maxTokens: 256, toolCallsFor: n => (n % 5 === 0 ? 2 : n % 2) });
  const controller = new ConversationController({ engine, toolRegistry: notesRegistry() });
  const { events, results } = await converse(controller, engine, 'ses_ctx_long', 45);
  assert.deepEqual(results.map(r => r.state), Array(45).fill('COMPLETED'));
  assert.equal(engine.overflows, 0, 'the estimate must be conservative against a 3-chars/token tokenizer');
  assert.ok(events.every(event => event.event !== 'request.failed'));
  events.forEach(validateEvent);
  const compactions = events.filter(event => event.data.context_compaction).map(event => event.data.context_compaction);
  assert.ok(compactions.length >= 3, 'the scenario must compact repeatedly');
  assert.ok(compactions.length <= 45 / 3, `compaction is rare, not per turn (got ${compactions.length})`);
  assert.ok(compactions.some(c => c.masked_tool_results > 0) && compactions.some(c => c.dropped_turns > 0));
  for (const c of compactions) { assert.equal(c.reason, 'budget'); assert.equal(c.context_tokens, 8192); assert.ok(c.estimated_prompt_tokens + 256 <= 8192); }

  for (const [index, { messages, compactions: seen }] of engine.prompts.entries()) {
    const latest = messages.findLastIndex(message => message.role === 'user');
    const turn = Number(/turn (\d+)/.exec(messages[latest].content)[1]);
    assert.equal(messages[latest].content.length, `turn ${turn}: `.length + 300, 'latest user message is verbatim');
    for (const message of messages.slice(latest)) if (message.role === 'tool') assert.ok(!isElidedToolResult(message) && message.content.length > 1500, 'in-flight results are verbatim');
    if (turn > 1) assert.ok(messages.some(message => message.role === 'user' && message.content.startsWith(`turn ${turn - 1}:`)), `turn ${turn - 1} is still in context at turn ${turn}`);
    // Elision is oldest-first: once a verbatim result appears, none after it is elided.
    const tools = messages.slice(0, latest).filter(message => message.role === 'tool').map(isElidedToolResult);
    assert.deepEqual(tools, [...tools].sort((a, b) => b - a), 'only the oldest results are elided');
    const previous = engine.prompts[index - 1];
    if (previous && previous.compactions === seen) assert.deepEqual(messages.slice(0, previous.messages.length), previous.messages, `prompt ${index} strictly extends prompt ${index - 1}`);
    // Tool continuations are where the engine reuses the prefix; compaction
    // must have happened at the turn's start instead.
    else if (previous) assert.equal(messages.at(-1).role, 'user', `compaction before prompt ${index} interrupted a tool loop`);
  }
  const session = controller.sessions.get('ses_ctx_long');
  assert.ok(session.history.length <= 64 && session.history_bytes <= 262144);
  assert.equal(session.history_bytes, session.history.reduce((sum, message) => sum + Buffer.byteLength(JSON.stringify(message)), 0), 'byte accounting survives compaction');
});

test('a denser tokenizer than estimated is absorbed by one retry and the learned ratio', async () => {
  const engine = budgetEngine({ contextTokens: 4096, charsPerToken: 1.6, toolCallsFor: n => n % 2 });
  const controller = new ConversationController({ engine, contextTokens: 4096, toolRegistry: notesRegistry(700) });
  const { events, results } = await converse(controller, engine, 'ses_ctx_dense', 30, 200);
  assert.deepEqual(results.map(r => r.state), Array(30).fill('COMPLETED'));
  assert.ok(engine.overflows <= 2, `learning must stop repeat overflows (got ${engine.overflows})`);
  assert.ok(controller.sessions.get('ses_ctx_dense').bytes_per_token < 1.6, 'the session learned the denser ratio');
  for (const { messages } of engine.prompts) assertWellFormed(messages);
  events.forEach(validateEvent);
});

test('an engine overflow is retried once after harder trimming, with a more conservative ratio', async () => {
  const engine = budgetEngine({ contextTokens: 8192, toolCallsFor: () => 1 });
  const controller = new ConversationController({ engine, toolRegistry: notesRegistry() });
  await converse(controller, engine, 'ses_ctx_retry', 3);
  const before = engine.prompts.length; engine.prompts.length = 0; const events = [];
  const failing = budgetEngine({ contextTokens: 8192, toolCallsFor: () => 1, failFirst: 1 }); controller.engine = failing;
  const result = await controller.runTurn({ sessionId: 'ses_ctx_retry', requestId: 'req_ctxretry', message: 'turn 4: again', onEvent: event => events.push(event) });
  assert.ok(before > 0); assert.equal(result.state, 'COMPLETED');
  assert.equal(failing.overflows, 1); assert.equal(failing.prompts.length, 3, 'overflowed call, its retry, and the tool continuation');
  const compaction = events.find(event => event.data.context_compaction)?.data.context_compaction;
  assert.equal(compaction.reason, 'engine_overflow'); assert.ok(compaction.masked_tool_results + compaction.dropped_turns > 0);
  assert.ok(failing.prompts[1].messages.length < failing.prompts[0].messages.length || JSON.stringify(failing.prompts[1].messages).length < JSON.stringify(failing.prompts[0].messages).length);
  assert.equal(events.filter(event => event.event === 'message.delta').length, 1, 'no duplicated output');
});

test('a persistent engine overflow surfaces a typed, presentable error after exactly one retry', async () => {
  const engine = budgetEngine({ contextTokens: 8192, toolCallsFor: () => 1 });
  const controller = new ConversationController({ engine, toolRegistry: notesRegistry() });
  await converse(controller, engine, 'ses_ctx_fail', 2);
  const failing = budgetEngine({ contextTokens: 8192, failAlways: true }); controller.engine = failing; const events = [];
  const result = await controller.runTurn({ sessionId: 'ses_ctx_fail', requestId: 'req_ctxfail1', message: 'turn 3', onEvent: event => events.push(event) });
  assert.equal(result.state, 'FAILED'); assert.equal(result.error, 'context_overflow'); assert.equal(failing.prompts.length, 2);
  const failed = events.at(-1); assert.equal(failed.event, 'request.failed'); assert.equal(failed.data.code, 'context_overflow'); assert.match(failed.data.message, /context window/);
  // Nothing left to trim: no pointless identical retry.
  const fresh = new ConversationController({ engine: budgetEngine({ contextTokens: 8192, failAlways: true }) });
  const alone = await fresh.runTurn({ sessionId: 'ses_ctx_fresh', requestId: 'req_ctxfresh', message: 'hello', onEvent: () => {} });
  assert.equal(alone.error, 'context_overflow'); assert.equal(fresh.engine.prompts.length, 1);
});

test('a non-overflow engine error is surfaced unchanged and never retried', async () => {
  let calls = 0;
  const engine = { async *generate() { calls += 1; throw new NativeEngineError('invalid_request', 'native engine request failed', 400); } };
  const controller = new ConversationController({ engine });
  const result = await controller.runTurn({ sessionId: 'ses_ctx_schema', requestId: 'req_ctxschema', message: 'hello', onEvent: () => {} });
  assert.equal(result.error, 'invalid_request'); assert.equal(calls, 1);
});

test('an oversized latest message fails before the engine is called, and the session recovers', async () => {
  const engine = budgetEngine({ contextTokens: 4096 });
  const controller = new ConversationController({ engine, contextTokens: 4096 }); const events = [];
  const big = await controller.runTurn({ sessionId: 'ses_ctx_big', requestId: 'req_ctxbig01', message: `turn 1: ${'b'.repeat(30000)}`, onEvent: event => events.push(event) });
  assert.equal(big.error, 'context_overflow'); assert.equal(engine.prompts.length, 0);
  assert.match(events.at(-1).data.message, /Shorten the message/);
  const next = await controller.runTurn({ sessionId: 'ses_ctx_big', requestId: 'req_ctxbig02', message: 'turn 2: short', onEvent: () => {} });
  assert.equal(next.state, 'COMPLETED');
  assert.deepEqual(engine.prompts[0].messages.map(message => message.role), ['user'], 'the unsendable turn was dropped whole');
});

test('hard message/byte bounds drop whole turns and never orphan a tool result', async () => {
  const engine = budgetEngine({ contextTokens: 8192, toolCallsFor: () => 1 });
  const controller = new ConversationController({ engine, maxHistoryMessages: 6, maxHistoryBytes: 8192, toolRegistry: notesRegistry(200) });
  await converse(controller, engine, 'ses_ctx_hard', 6, 20);
  const session = controller.sessions.get('ses_ctx_hard');
  assert.ok(session.history.length <= 6 && session.history_bytes <= 8192);
  assertWellFormed(session.history);
  for (const { messages } of engine.prompts) assertWellFormed(messages);
});

test('stored history never starts mid-turn after a hard-bound trim', () => {
  const controller = new ConversationController({ engine: { async *generate() {} }, maxHistoryMessages: 5, maxHistoryBytes: 4096 });
  const session = controller.createSession('ses_ctx_store');
  for (let n = 1; n <= 12; n++) for (const message of toolTurn(n, n % 3 === 0 ? 2500 : 100)) {
    controller._appendHistory(session, message);
    assertWellFormed(session.history);
    assert.equal(session.history_bytes, session.history.reduce((sum, item) => sum + Buffer.byteLength(JSON.stringify(item)), 0));
    const latest = session.history.findLastIndex(item => item.role === 'user');
    // Bounds hold for everything older than the latest turn, which is never split.
    if (latest > 0) assert.ok(session.history.length <= 5 && session.history_bytes <= 4096);
  }
});

test('context limits are validated and default output reservation follows the engine client', () => {
  const engine = { maxTokens: 128, async *generate() {} };
  assert.equal(new ConversationController({ engine }).maxOutputTokens, 128);
  assert.throws(() => new ConversationController({ engine, contextTokens: 32768 }), /context limits/);
  assert.throws(() => new ConversationController({ engine: { async *generate() {} }, contextTokens: 1024, maxOutputTokens: 512 }), /context limits/);
});
