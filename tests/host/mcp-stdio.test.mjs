// The real `node lae-mcp.mjs` process: stdout hygiene, stderr screening, framing edge cases,
// shutdown on EOF and signals, and argv handling. Checklist ids in [brackets].

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { LineSplitter, MAX_MESSAGE_BYTES, takeDelegateKey } from '../../host/mcp/stdio.mjs';
import { MODERN_META, TEST_KEY, makeStateDir, sleep, startFakeHost } from './mcp-fake-host.mjs';

const ENTRY = fileURLToPath(new URL('../../lae-mcp.mjs', import.meta.url));

function launch(t, { env = {}, args = [] } = {}) {
  const child = spawn(process.execPath, [ENTRY, ...args], { env: { PATH: process.env.PATH ?? '', ...env }, stdio: ['pipe', 'pipe', 'pipe'] });
  const out = []; const err = [];
  child.stdout.on('data', chunk => out.push(chunk));
  child.stderr.on('data', chunk => err.push(chunk));
  const exited = new Promise(resolve => child.on('exit', (code, signal) => resolve({ code, signal, at: Date.now() })));
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  const stdoutText = () => Buffer.concat(out).toString('utf8');
  const stderrText = () => Buffer.concat(err).toString('utf8');
  const replies = () => stdoutText().split('\n').filter(Boolean).map(line => JSON.parse(line));
  const waitFor = async (predicate, timeoutMs = 10_000) => {
    const started = Date.now();
    for (;;) {
      let found; try { found = replies().find(predicate); } catch {}
      if (found) return found;
      if (Date.now() - started > timeoutMs) throw new Error(`timed out; stdout so far: ${stdoutText().slice(0, 500)}`);
      await sleep(10);
    }
  };
  const send = message => child.stdin.write(`${JSON.stringify(message)}\n`);
  return { child, exited, stdoutText, stderrText, replies, waitFor, send };
}

async function hostWithState(t, hostOptions) {
  const host = await startFakeHost(hostOptions);
  t.after(() => host.close());
  const stateDir = await makeStateDir(t, { port: host.port });
  return { host, env: { BMO_DELEGATE_KEY: TEST_KEY, BMO_STATE_DIR: stateDir } };
}

function assertCleanStdout(text) {
  assert.ok(text.length > 0);
  assert.ok(text.endsWith('\n'), 'stdout must end on a complete line');
  assert.ok(!text.includes('\r'));
  for (const line of text.slice(0, -1).split('\n')) {
    const message = JSON.parse(line); // throws on any non-JSON line
    for (const item of Array.isArray(message) ? message : [message]) {
      assert.equal(item.jsonrpc, '2.0', line.slice(0, 200));
      assert.ok(Object.hasOwn(item, 'result') || Object.hasOwn(item, 'error') || typeof item.method === 'string', line.slice(0, 200));
    }
  }
}

test('[C1] [C22] stdout carries only JSON-RPC lines and stderr never carries the key, task, context or answer', async t => {
  const { env } = await hostWithState(t, { answer: 'ANSWER-MARKER-31' });
  const bridge = launch(t, { env });
  bridge.send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'Visual Studio Code', version: '1.140.0' } } });
  bridge.child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\r\n'); // Windows CRLF
  bridge.child.stdin.write('this is not json\n');
  bridge.child.stdin.write(`{"jsonrpc":"2.0","id":"big","method":"tools/call","params":{"name":"bmo_ask","arguments":{"task":"${'z'.repeat(MAX_MESSAGE_BYTES + 10)}"}}}\n`);
  bridge.child.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/list"}\r\n');
  bridge.send({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'bmo_ask', arguments: { task: 'TASK-MARKER-12 reword', context: 'CONTEXT-MARKER-34' }, _meta: { progressToken: 'p' } } });
  bridge.send({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'bmo_ask', arguments: { task: 'x' }, _meta: MODERN_META } });
  const answer = await bridge.waitFor(message => message.id === 3);
  await bridge.waitFor(message => message.id === 4);
  assert.equal(answer.result.structuredContent.answer, 'ANSWER-MARKER-31');
  assert.equal((await bridge.waitFor(message => message.id === 2)).result.tools.length, 4);
  assert.ok(bridge.replies().some(message => message.error?.code === -32700));
  assert.ok(bridge.replies().some(message => message.error?.code === -32600 && /1 MiB/.test(message.error.message)));
  assert.equal(bridge.replies().filter(message => message.id === 'big').length, 0);
  bridge.child.stdin.end();
  await bridge.exited;
  assertCleanStdout(bridge.stdoutText());
  const log = bridge.stderrText();
  assert.match(log, /lae-mcp .* info ready key_configured=true/);
  for (const forbidden of [TEST_KEY, 'TASK-MARKER', 'CONTEXT-MARKER', 'ANSWER-MARKER', 'zzzzzzzz']) assert.ok(!log.includes(forbidden), forbidden);
});

test('[C2] stdin EOF exits within 2 s, aborts in-flight host calls and cancels the job the caller never saw', async t => {
  const { host, env } = await hostWithState(t, { hooks: { get: async () => true } }); // polls hang forever
  const bridge = launch(t, { env });
  bridge.send({ jsonrpc: '2.0', id: 'ask', method: 'tools/call', params: { name: 'bmo_ask', arguments: { task: 'long' }, _meta: MODERN_META } });
  const started = Date.now();
  while (!host.requests.some(request => request.method === 'GET')) { if (Date.now() - started > 5000) throw new Error('no poll'); await sleep(10); }
  const eofAt = Date.now();
  bridge.child.stdin.end();
  const { code, at } = await bridge.exited;
  assert.equal(code, 0);
  assert.ok(at - eofAt < 2_000, `exit took ${at - eofAt} ms`);
  await sleep(50);
  assert.equal(host.requests.find(request => request.method === 'GET').closed, true, 'orphaned long-poll');
  assert.equal(host.requests.filter(request => request.url.endsWith('/cancel')).length, 1);
  assert.equal(bridge.replies().filter(message => message.id === 'ask').length, 0);
});

test('[C2] stdin EOF with nothing in flight exits promptly with code 0', async t => {
  const bridge = launch(t, { env: { BMO_DELEGATE_KEY: TEST_KEY, BMO_STATE_DIR: '/nonexistent-bmo-state' } });
  bridge.send({ jsonrpc: '2.0', id: 1, method: 'server/discover', params: { _meta: MODERN_META } });
  await bridge.waitFor(message => message.id === 1);
  const eofAt = Date.now();
  bridge.child.stdin.end();
  const { code, at } = await bridge.exited;
  assert.equal(code, 0);
  assert.ok(at - eofAt < 1_000);
  assertCleanStdout(bridge.stdoutText());
});

test('SIGTERM and SIGINT shut the bridge down cleanly', { skip: process.platform === 'win32' && 'POSIX signals' }, async t => {
  for (const signal of ['SIGTERM', 'SIGINT']) {
    const bridge = launch(t, { env: { BMO_DELEGATE_KEY: TEST_KEY } });
    bridge.send({ jsonrpc: '2.0', id: 1, method: 'ping' });
    await bridge.waitFor(message => message.id === 1);
    const sentAt = Date.now();
    bridge.child.kill(signal);
    const { code, at } = await bridge.exited;
    assert.equal(code, 0, signal);
    assert.ok(at - sentAt < 2_000);
    assert.match(bridge.stderrText(), new RegExp(`shutdown reason=${signal.toLowerCase()}`));
  }
});

test('the key is never accepted on argv: any argument is refused without being echoed', async t => {
  const refused = launch(t, { args: ['--key', 'SUPER-SECRET-ARGV-KEY'], env: { BMO_DELEGATE_KEY: TEST_KEY } });
  refused.child.stdin.end(); // a bridge that wrongly started would now exit 0 instead of hanging
  const { code } = await refused.exited;
  assert.equal(code, 2);
  assert.equal(refused.stdoutText(), '');
  assert.ok(!refused.stderrText().includes('SUPER-SECRET-ARGV-KEY'));
  assert.match(refused.stderrText(), /BMO_DELEGATE_KEY environment variable/);
  const help = launch(t, { args: ['--help'] });
  assert.equal((await help.exited).code, 0);
  assert.equal(help.stdoutText(), '');
  // Source-level guard: the entry point consults argv only to refuse it, and the key is read
  // from the environment in exactly one place.
  const entry = await readFile(ENTRY, 'utf8');
  assert.equal((entry.match(/process\.argv/g) ?? []).length, 1);
  const stdioSource = await readFile(fileURLToPath(new URL('../../host/mcp/stdio.mjs', import.meta.url)), 'utf8');
  assert.equal((stdioSource.match(/BMO_DELEGATE_KEY/g) ?? []).length >= 1, true);
  assert.ok(!/argv/.test(stdioSource));
});

test('the delegate key is taken from the environment once and then removed from it', () => {
  const env = { BMO_DELEGATE_KEY: `  ${TEST_KEY}\n`, OTHER: 'x' };
  assert.equal(takeDelegateKey(env), TEST_KEY);
  assert.deepEqual(env, { OTHER: 'x' });
  for (const bad of ['${input:bmo-delegate-key}', '${BMO_DELEGATE_KEY}', '', 'short', 'with space inside 0123456789', 'é'.repeat(20), 'x'.repeat(513)]) assert.equal(takeDelegateKey({ BMO_DELEGATE_KEY: bad }), '');
});

test('LineSplitter: chunk boundaries, split UTF-8, CRLF, BOM, blank lines, oversize recovery, final unterminated line', () => {
  const lines = []; let oversize = 0;
  const splitter = new LineSplitter({ maxBytes: 32, onLine: line => lines.push(line), onOversize: () => { oversize += 1; } });
  const euro = Buffer.from('€'); // 3 bytes, split across chunks below
  splitter.push(Buffer.concat([Buffer.from('﻿{"a":"'), euro.subarray(0, 1)]));
  splitter.push(Buffer.concat([euro.subarray(1), Buffer.from('"}\r\n\n   \n{"b":1}')]));
  splitter.push(Buffer.from(`\n${'x'.repeat(40)}`)); // over the cap before its newline arrives
  splitter.push(Buffer.from(`${'y'.repeat(10)}\n{"c":2}\n${'w'.repeat(33)}\n`));
  splitter.push(Buffer.from('{"d":3}'));
  splitter.end();
  assert.deepEqual(lines, ['{"a":"€"}', '{"b":1}', '{"c":2}', '{"d":3}']);
  assert.equal(oversize, 2);
});
