import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { makeToolResult } from '../../host/agent/tool-envelope.mjs';
import {
  ConversationController,
  NATIVE_SUPERVISOR_DISPATCH_OWNER,
  NATIVE_SUPERVISOR_HANDOFF_VERSION,
} from '../../host/agent/controller.mjs';
import { ACTION_JOURNAL_HEALTH_ERRORS } from '../../host/agent/action-journal.mjs';

const operationId = 'act_' + 'a'.repeat(32);
const digest = value => value.repeat(64);

async function runAsPlatform(platform, operation) {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { ...descriptor, value: platform });
  try { return await operation(); }
  finally { Object.defineProperty(process, 'platform', descriptor); }
}

class MockJournal {
  constructor({ authorized = {}, nativeOnly = true, healthState = 'ready' } = {}) {
    this.calls = [];
    this.authorized = authorized;
    this.nativeOnly = nativeOnly;
    this.healthState = healthState;
  }

  health() { return { state: this.healthState, error: this.healthState === 'ready' ? null : 'unavailable' }; }
  async prepare(args) { this.calls.push('prepare'); this.prepareArgs = args; return { operation_id: operationId, state: 'prepared', sequence: 0, receipt_hash: digest('0') }; }
  async authorize(id, kind) { this.calls.push(['authorize', id, kind]); return { operation_id: id, state: 'authorized', sequence: this.authorized.sequence ?? 1, receipt_hash: this.authorized.receipt_hash ?? digest('1') }; }
  async dispatch() { this.calls.push('dispatch'); if (this.nativeOnly) throw new Error('dispatch must be owner-native'); return { operation_id: operationId, state: 'dispatching', sequence: 2, receipt_hash: digest('2') }; }
  async acknowledge() { this.calls.push('acknowledge'); if (this.nativeOnly) throw new Error('acknowledge must be owner-native'); return { operation_id: operationId, state: 'acknowledged', sequence: 3, receipt_hash: digest('3') }; }
  async beginReconciliation() { this.calls.push('beginReconciliation'); if (this.nativeOnly) throw new Error('reconciliation must be owner-native'); return { operation_id: operationId, state: 'reconciling', sequence: 3, receipt_hash: digest('3') }; }
  async complete() { this.calls.push('complete'); if (this.nativeOnly) throw new Error('completion must be owner-native'); return { operation_id: operationId, state: 'completed', sequence: 4, receipt_hash: digest('4') }; }
  async cancel() { this.calls.push('cancel'); }
  async failDefinitive(id, resolution) { this.calls.push(['failDefinitive', id, resolution]); return { operation_id: id, state: 'failed_definitive', sequence: 2, receipt_hash: digest('2') }; }
  async markUnknown() { this.calls.push('markUnknown'); throw new Error('unknown must be owner-native after handoff'); }
}

function engineFor(name, arguments_, observedTools = null) {
  let generated = false;
  return {
    async *generate({ tools } = {}) {
      observedTools?.push(tools);
      if (!generated) {
        generated = true;
        yield { kind: 'tool_call_chunk', text: JSON.stringify({ id: 'call_native_01', name, arguments: arguments_ }) };
      } else yield { kind: 'text_delta', text: 'done' };
      yield { kind: 'done', usage: {} };
    },
  };
}

function tool(name, sideEffect, execute) {
  const native = name === 'app.open' || name === 'process.run_allowlisted';
  return {
    name,
    version: name === 'process.run_allowlisted' ? '0.1.0' : '1.0.0',
    risk_tier: sideEffect === 'process_execution' ? 'T3' : sideEffect === 'create' ? 'T2' : 'T1',
    side_effect: sideEffect,
    network: name === 'process.run_allowlisted',
    ...(name === 'process.run_allowlisted' ? { data_egress: 'operator_configured' } : {}),
    requires_confirmation: native,
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    authorize: async () => ({ kind: 'user_confirmation', journal_dispatch_owner: 'caller_override' }),
    ...(native ? { confirmationRequired: async () => false } : {}),
    execute,
  };
}

test('Windows native app/process actions fail definitively before handoff, journal dispatch, or provider execution', async () => {
  let executions = 0;
  const journal = new MockJournal();
  const controller = new ConversationController({
    engine: engineFor('app.open', {}),
    actionJournal: journal,
    toolRegistry: { 'app.open': tool('app.open', 'launch', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }); }) },
  });

  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native01', requestId: 'req_native01', message: 'open it' }));
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.deepEqual({ riskTier: journal.prepareArgs.riskTier, sideEffect: journal.prepareArgs.sideEffect }, { riskTier: 'T1', sideEffect: 'launch' });
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'failDefinitive']);
  assert.deepEqual(journal.calls.at(-1), ['failDefinitive', operationId, 'pre_dispatch_failure']);
  assert.equal(NATIVE_SUPERVISOR_DISPATCH_OWNER, 'native_supervisor');
  assert.equal(NATIVE_SUPERVISOR_HANDOFF_VERSION, 'native-supervisor-handoff.v1');
});

test('caller owner override, missing bridge, throwing bridge, and callback-shaped bypasses are ignored', async () => {
  let executions = 0; const journal = new MockJournal();
  const maliciousTool = {
    ...tool('process.run_allowlisted', 'process_execution', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'process.run_allowlisted', status: 'ok', text: '{}' }); }),
    nativeSupervisor: () => { throw new Error('malicious bridge callback'); },
    executeNative: () => { throw new Error('alternate bridge callback'); },
  };
  const controller = new ConversationController({ platform: 'linux', engine: engineFor('process.run_allowlisted', {}), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': maliciousTool } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native02', requestId: 'req_native02', message: 'run it' }));
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.equal(journal.calls.includes('dispatch'), false);
  assert.equal(journal.calls.includes('acknowledge'), false);
  assert.equal(journal.calls.includes('beginReconciliation'), false);
  assert.equal(journal.calls.includes('complete'), false);
  assert.equal(journal.calls.includes('markUnknown'), false);
});

test('malformed authorized readback remains pre-handoff refusal and cannot execute', async () => {
  let executions = 0; const journal = new MockJournal({ authorized: { sequence: 2, receipt_hash: 'not-a-digest' } });
  const controller = new ConversationController({ platform: 'linux', engine: engineFor('app.open', {}), actionJournal: journal, toolRegistry: { 'app.open': tool('app.open', 'launch', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }); }) } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native03', requestId: 'req_native03', message: 'open it' }));
  assert.equal(result.error, 'native_supervisor_unavailable');
  assert.equal(executions, 0);
  assert.deepEqual(journal.calls.at(-1), ['failDefinitive', operationId, 'pre_dispatch_failure']);
});

test('canonical native metadata rejects a POSIX forged risk/side-effect descriptor at admission', async () => {
  const journal = new MockJournal();
  const forgedMetadata = {
    ...tool('app.open', 'launch', async () => makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' })),
    risk_tier: 'T0',
    side_effect: 'none',
  };
  assert.throws(() => new ConversationController({ engine: engineFor('app.open', {}), actionJournal: journal, toolRegistry: { 'app.open': forgedMetadata } }), /canonical security metadata/u);
  assert.deepEqual(journal.calls, []);
});

test('canonical native metadata rejects missing fields and explicit undefined egress', () => {
  for (const mutate of [descriptor => { delete descriptor.network; }, descriptor => { descriptor.data_egress = undefined; }]) {
    const descriptor = tool('app.open', 'launch', async () => makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }));
    mutate(descriptor);
    assert.throws(() => new ConversationController({ engine: engineFor('app.open', {}), toolRegistry: { 'app.open': descriptor } }), /canonical security metadata/u);
  }
});

test('unavailable journal never advertises a native action even with falsified metadata', async () => {
  const advertised = []; const journal = new MockJournal({ healthState: 'unavailable' });
  const canonicalMetadata = tool('process.run_allowlisted', 'process_execution', async () => makeToolResult({ id: 'call_native_01', name: 'process.run_allowlisted', status: 'ok', text: '{}' }));
  const controller = new ConversationController({ engine: engineFor('process.run_allowlisted', {}, advertised), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': canonicalMetadata } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_native05', requestId: 'req_native05', message: 'run it' }));
  assert.equal(result.error, 'action_journal_unavailable');
  assert.equal(advertised[0].some(item => item.function?.name === 'process.run_allowlisted'), false);
});

test('malformed or hostile journal health is a finite unavailable refusal', async () => {
  const healthValues = [null, { state: 'blocked', error: 'raw provider failure' }, { state: 'blocked', error: 'action_journal_access_token_supersecret' }, { state: 'blocked', error: `action_journal_${'x'.repeat(97)}` }];
  for (const [index, health] of healthValues.entries()) {
    const journal = new MockJournal({ healthState: 'blocked' }); journal.health = () => health;
    const controller = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { throw new Error('preview must not run'); }) } });
    const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: `ses_health_${index}`, requestId: `req_health_${index}`, message: 'run it' }));
    assert.equal(result.error, 'action_journal_unavailable');
  }
  const throwing = new MockJournal({ healthState: 'blocked' });
  const controller = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: throwing, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { throw new Error('preview must not run'); }) } });
  Object.defineProperty(throwing, 'health', { get() { throw new Error('health getter sentinel'); } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_health_getter', requestId: 'req_health_getter', message: 'run it' }));
  assert.equal(result.error, 'action_journal_unavailable');

  let healthCalls = 0; let previewCalls = 0; const flipping = new MockJournal({ healthState: 'blocked' });
  flipping.health = () => { healthCalls += 1; return healthCalls === 1 ? { state: 'blocked', error: 'action_journal_platform_unavailable' } : { state: 'ready', error: null }; };
  const flipController = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: flipping, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { previewCalls += 1; }) } });
  const flipped = await runAsPlatform('win32', () => flipController.runTurn({ sessionId: 'ses_health_flip', requestId: 'req_health_flip', message: 'run it' }));
  assert.equal(flipped.error, 'action_journal_platform_unavailable'); assert.equal(healthCalls, 1); assert.equal(previewCalls, 0);

  const proxyTarget = Object.freeze({ state: 'blocked', error: 'action_journal_platform_unavailable' });
  const proxy = new Proxy(proxyTarget, { getOwnPropertyDescriptor(target, key) { if (key === 'state') return { configurable: false, enumerable: true, get: () => target.state }; return Reflect.getOwnPropertyDescriptor(target, key); } });
  const proxyJournal = new MockJournal({ healthState: 'blocked' }); proxyJournal.health = () => proxy;
  const proxyController = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: proxyJournal, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { throw new Error('preview must not run'); }) } });
  const proxied = await runAsPlatform('win32', () => proxyController.runTurn({ sessionId: 'ses_health_proxy', requestId: 'req_health_proxy', message: 'run it' }));
  assert.equal(proxied.error, 'action_journal_unavailable');

  for (const [index, [health, label]] of [[0, [{ state: 'ready', error: null }, 'ready']], [1, [{ state: 'blocked', error: 'action_journal_platform_unavailable' }, 'blocked']]]) {
    let executions = 0; const transparent = new MockJournal({ healthState: 'blocked' }); transparent.health = () => new Proxy(health, {});
    const transparentController = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: transparent, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { executions += 1; }) } });
    const transparentResult = await runAsPlatform('win32', () => transparentController.runTurn({ sessionId: `ses_health_transparent_${index}`, requestId: `req_health_transparent_${index}`, message: 'run it' }));
    assert.equal(transparentResult.error, 'action_journal_unavailable', label); assert.equal(executions, 0, label); assert.deepEqual(transparent.calls, []);
  }
});

test('journal health preserves every exact contract diagnostic and nothing else', async () => {
  for (const [index, error] of ACTION_JOURNAL_HEALTH_ERRORS.entries()) {
    const journal = new MockJournal({ healthState: 'blocked' }); journal.health = () => ({ state: 'blocked', error });
    const controller = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: journal, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { throw new Error('preview must not run'); }) } });
    const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: `ses_diag_${index}`, requestId: `req_diag_${index}`, message: 'run it' }));
    assert.equal(result.error, error);
  }
});

test('journal method snapshot rejects accessors and ignores post-construction replacement', async () => {
  const blocked = new MockJournal({ healthState: 'blocked' });
  const blockedController = new ConversationController({ engine: engineFor('process.run_allowlisted', {}), actionJournal: blocked, toolRegistry: { 'process.run_allowlisted': tool('process.run_allowlisted', 'process_execution', async () => { throw new Error('preview must not run'); }) } });
  blocked.health = () => ({ state: 'ready', error: null });
  blocked.prepare = () => { throw new Error('replacement prepare must not run'); };
  const blockedResult = await runAsPlatform('win32', () => blockedController.runTurn({ sessionId: 'ses_journal_replace_health', requestId: 'req_journal_replace_health', message: 'run it' }));
  assert.equal(blockedResult.error, 'action_journal_unavailable');

  const replaced = new MockJournal({ nativeOnly: false }); const replacements = { prepare: 0, authorize: 0, dispatch: 0 };
  const replacedController = new ConversationController({ engine: engineFor('test.write', {}), actionJournal: replaced, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } });
  for (const name of Object.keys(replacements)) replaced[name] = () => { replacements[name] += 1; throw new Error(`replacement ${name} must not run`); };
  const replacedResult = await runAsPlatform('linux', () => replacedController.runTurn({ sessionId: 'ses_journal_replace_methods', requestId: 'req_journal_replace_methods', message: 'write it' }));
  assert.equal(replacedResult.state, 'COMPLETED'); assert.deepEqual(replacements, { prepare: 0, authorize: 0, dispatch: 0 });

  const accessor = new MockJournal(); Object.defineProperty(accessor, 'prepare', { get() { return async () => {}; } });
  assert.throws(() => new ConversationController({ engine: engineFor('test.write', {}), actionJournal: accessor, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } }), /actionJournal does not implement/u);
  const proxied = new Proxy(new MockJournal(), {});
  assert.throws(() => new ConversationController({ engine: engineFor('test.write', {}), actionJournal: proxied, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } }), /actionJournal does not implement/u);

  const methodNames = ['health', 'prepare', 'authorize', 'dispatch', 'acknowledge', 'beginReconciliation', 'complete', 'cancel', 'failDefinitive', 'markUnknown'];
  for (const methodName of methodNames) {
    const hostile = new MockJournal(); hostile[methodName] = new Proxy(hostile[methodName], { apply() { throw new Error('callable proxy must be rejected'); } });
    assert.throws(() => new ConversationController({ engine: engineFor('test.write', {}), actionJournal: hostile, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } }), /actionJournal does not implement/u);

    const inheritedHostile = new MockJournal();
    const inheritedPrototype = Object.create(Object.getPrototypeOf(inheritedHostile));
    Object.defineProperty(inheritedPrototype, methodName, { configurable: true, value: new Proxy(Object.getPrototypeOf(inheritedHostile)[methodName], { apply() { throw new Error('inherited callable proxy must be rejected'); } }) });
    Object.setPrototypeOf(inheritedHostile, inheritedPrototype);
    assert.throws(() => new ConversationController({ engine: engineFor('test.write', {}), actionJournal: inheritedHostile, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } }), /actionJournal does not implement/u);
  }

  const controllerForJournal = journal => new ConversationController({ engine: engineFor('test.write', {}), actionJournal: journal, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } });
  for (const bindVariant of ['getter', 'throwing']) {
    for (const methodName of methodNames) {
      const hostile = new MockJournal(); const original = Object.getPrototypeOf(hostile)[methodName];
      const replacement = function (...args) { return Reflect.apply(original, this, args); };
      if (bindVariant === 'getter') Object.defineProperty(replacement, 'bind', { configurable: true, get() { throw new Error('poisoned bind getter'); } });
      else Object.defineProperty(replacement, 'bind', { configurable: true, value() { throw new Error('poisoned bind call'); } });
      hostile[methodName] = replacement;
      assert.doesNotThrow(() => controllerForJournal(hostile), `${bindVariant} ${methodName}`);
    }
  }

  const bindProxyJournal = new MockJournal({ nativeOnly: false }); const bindProxyCalls = Object.fromEntries(methodNames.map(name => [name, 0]));
  for (const methodName of methodNames) {
    const original = Object.getPrototypeOf(bindProxyJournal)[methodName];
    const replacement = function (...args) { return Reflect.apply(original, this, args); };
    Object.defineProperty(replacement, 'bind', { configurable: true, value() {
      return new Proxy(function (...args) { bindProxyCalls[methodName] += 1; return Reflect.apply(replacement, this, args); }, {});
    } });
    bindProxyJournal[methodName] = replacement;
  }
  const bindProxyController = controllerForJournal(bindProxyJournal);
  const bindProxyResult = await runAsPlatform('linux', () => bindProxyController.runTurn({ sessionId: 'ses_journal_bind_proxy', requestId: 'req_journal_bind_proxy', message: 'write it' }));
  assert.equal(bindProxyResult.state, 'COMPLETED'); assert.deepEqual(bindProxyCalls, Object.fromEntries(methodNames.map(name => [name, 0])));

  const stable = new MockJournal({ nativeOnly: false }); const proxyCalls = Object.fromEntries(methodNames.map(name => [name, 0]));
  const stableController = new ConversationController({ engine: engineFor('test.write', {}), actionJournal: stable, toolRegistry: { 'test.write': tool('test.write', 'create', async () => makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' })) } });
  for (const methodName of methodNames) stable[methodName] = new Proxy(stable[methodName], { apply(target, thisArg, args) { proxyCalls[methodName] += 1; return Reflect.apply(target, thisArg, args); } });
  const stableResult = await runAsPlatform('linux', () => stableController.runTurn({ sessionId: 'ses_journal_proxy_replace', requestId: 'req_journal_proxy_replace', message: 'write it' }));
  assert.equal(stableResult.state, 'COMPLETED'); assert.deepEqual(proxyCalls, Object.fromEntries(methodNames.map(name => [name, 0])));
});

test('registry snapshots defeat post-admission mutation and public map replacement', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const mutable = tool('test.write', 'create', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ engine: engineFor('test.write', {}), actionJournal: journal, toolRegistry: { 'test.write': mutable } });
  mutable.name = 'app.open'; mutable.risk_tier = 'T0'; mutable.side_effect = 'none'; mutable.execute = () => { throw new Error('mutated descriptor executed'); };
  Object.defineProperty(controller, 'tools', { value: new Map([['test.write', { name: 'app.open', execute: () => { throw new Error('replaced map used'); } }]]) });
  const result = await runAsPlatform('linux', () => controller.runTurn({ sessionId: 'ses_snapshot', requestId: 'req_snapshot', message: 'write it' }));
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('accessor-backed canonical metadata is rejected before registry admission', async () => {
  const accessorTool = tool('app.open', 'launch', async () => makeToolResult({ id: 'call_native_01', name: 'app.open', status: 'ok', text: '{}' }));
  Object.defineProperty(accessorTool, 'risk_tier', { configurable: true, get() { throw new Error('hostile getter'); } });
  assert.throws(() => new ConversationController({ engine: engineFor('app.open', {}), toolRegistry: { 'app.open': accessorTool } }), /data property/u);
});

test('POSIX durable tools retain controller-owned journal dispatch and completion', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const writeTool = tool('test.write', 'create', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'test.write', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ engine: engineFor('test.write', {}), actionJournal: journal, toolRegistry: { 'test.write': writeTool } });
  const result = await runAsPlatform('linux', () => controller.runTurn({ sessionId: 'ses_posix01', requestId: 'req_posix01', message: 'write it' }));
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('Windows non-native durable tools retain controller ownership', async () => {
  let executions = 0; const journal = new MockJournal({ nativeOnly: false });
  const browserTool = tool('browser.open_url', 'external_navigation', async () => { executions++; return makeToolResult({ id: 'call_native_01', name: 'browser.open_url', status: 'ok', text: '{}' }); });
  const controller = new ConversationController({ engine: engineFor('browser.open_url', {}), actionJournal: journal, toolRegistry: { 'browser.open_url': browserTool } });
  const result = await runAsPlatform('win32', () => controller.runTurn({ sessionId: 'ses_other01', requestId: 'req_other01', message: 'browse it' }));
  assert.equal(result.state, 'COMPLETED');
  assert.equal(executions, 1);
  assert.deepEqual(journal.calls.map(call => Array.isArray(call) ? call[0] : call), ['prepare', 'authorize', 'dispatch', 'acknowledge', 'complete']);
});

test('controller source defines exact non-overridable handoff and no native/process fallback', async () => {
  const source = await readFile(new URL('../../host/agent/controller.mjs', import.meta.url), 'utf8');
  for (const field of ['version:', 'journal_dispatch_owner:', 'operation_id:', 'request_ref:', 'call_ref:', 'tool:', 'risk:', 'side_effect:', 'args_digest:', 'preview_digest:', 'operation_digest:', 'authorization_kind:', 'authorized_sequence:', 'authorized_receipt_digest:', 'authorized_event_digest:', 'accepted: false']) assert.match(source, new RegExp(field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.match(source, /nativeSupervisorOwnerFor\(tool\.name, tool, process\.platform\)/u);
  assert.doesNotMatch(source, /platform\s*=\s*process\.platform/u);
  assert.match(source, /#tools/u);
  assert.doesNotMatch(source, /this\.tools/u);
  assert.match(source, /platform === 'win32'/u);
  assert.doesNotMatch(source, /nativeSupervisor\?\./u);
  assert.doesNotMatch(source, /node:child_process/u);
  assert.doesNotMatch(source, /spawn\(/u);
  assert.doesNotMatch(source, /authorized_event_digest\s*=\s*authorized\?\.receipt_hash/u);
  assert.doesNotMatch(source, /PowerShell|powershell|cmd(?:\.exe)?|shell/u);
  assert.doesNotMatch(source, /from ['"](?:\.\/.+)?native-supervisor/u);
});
