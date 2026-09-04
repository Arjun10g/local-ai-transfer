import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import test from 'node:test';

import {
  LLAMA_CPP_REVISION, PRODUCT_MODEL_FILE, PRODUCT_MODEL_ID, PRODUCT_MODEL_SHA256,
  PRODUCT_MODEL_SIZE, awaitLaunchGate, launchEngine, parseReadyEvent, parseSupervisorArguments,
  resolveSupervisorInputs, runPortableSupervisor, validateServingBuildInfo,
} from '../../portable-supervisor.mjs';

async function inputs(t) {
  const directory = await mkdtemp(join(tmpdir(), 'lae-supervisor-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const enginePath = join(directory, 'lae-engine-cpu.exe'); const modelPath = join(directory, PRODUCT_MODEL_FILE);
  await writeFile(enginePath, 'MZ'); await writeFile(modelPath, 'GGUF');
  return { enginePath, modelPath, bootstrapPipe: 'LocalBMO-12345678-1234-1234-1234-123456789abc', launchGatePipe: 'LocalBMOGate-12345678-1234-1234-1234-123456789abc' };
}

test('portable supervisor rejects caller identity substitution and duplicate config keys', async t => {
  const argv = ['--engine', 'E', '--model', 'M', '--bootstrap-pipe', 'LocalBMO-12345678-1234-1234-1234-123456789abc', '--launch-gate-pipe', 'LocalBMOGate-12345678-1234-1234-1234-123456789abc'];
  assert.deepEqual(parseSupervisorArguments(argv), { enginePath: 'E', modelPath: 'M', configPath: undefined, bootstrapPipe: 'LocalBMO-12345678-1234-1234-1234-123456789abc', launchGatePipe: 'LocalBMOGate-12345678-1234-1234-1234-123456789abc' });
  assert.throws(() => parseSupervisorArguments([...argv, '--engine', 'X']), /duplicate/);
  assert.throws(() => parseSupervisorArguments([...argv, '--token', 'secret']), /invalid/);
  const value = await inputs(t); const configPath = join(value.enginePath, '..', 'host.json');
  await writeFile(configPath, '{"version":"0.1.0","version":"0.1.0"}');
  await assert.rejects(resolveSupervisorInputs({ ...value, configPath }, { platform: 'win32', architecture: 'x64' }), /duplicate key/);
  await writeFile(configPath, '{"engine":{"mode":"fixture"}}');
  await assert.rejects(resolveSupervisorInputs({ ...value, configPath }, { platform: 'win32', architecture: 'x64' }), /unknown key: engine/);
});

test('portable supervisor pins build and ready identities', () => {
  const serving = { engine_version: '0.1.0', api_version: '0.1.0', backend: `llama.cpp/${LLAMA_CPP_REVISION.slice(0, 8)}/cpu`, llama_cpp_revision: LLAMA_CPP_REVISION, model: PRODUCT_MODEL_ID, product_model_size_bytes: PRODUCT_MODEL_SIZE, product_model_sha256: PRODUCT_MODEL_SHA256 };
  assert.equal(validateServingBuildInfo(serving), serving);
  assert.throws(() => validateServingBuildInfo({ ...serving, backend: 'llama.cpp/3581ba0c/vulkan' }), /identity/);
  assert.deepEqual(parseReadyEvent('{"event":"ready","port":3210,"bind":"127.0.0.1","token_required":true}\n'), { event: 'ready', port: 3210, bind: '127.0.0.1', token_required: true });
  assert.throws(() => parseReadyEvent('{"event":"ready","event":"ready","port":3210,"bind":"127.0.0.1","token_required":true}'), /duplicate key/);
});

test('launch gate accepts only the bounded post-job-assignment signal', async () => {
  const pipe = 'LocalBMOGate-12345678-1234-1234-1234-123456789abc';
  const connection = payload => {
    const socket = new PassThrough();
    queueMicrotask(() => socket.end(payload));
    return socket;
  };
  await awaitLaunchGate(pipe, { connectImpl: () => connection('GO\n') });
  await assert.rejects(awaitLaunchGate(pipe, { connectImpl: () => connection('GO\nextra') }), /handoff failed/);
  await assert.rejects(awaitLaunchGate('LocalBMO-12345678-1234-1234-1234-123456789abc', { connectImpl: () => connection('GO\n') }), /invalid/);
});

test('engine token travels only over stdin and exact CPU launch has no fallback', async () => {
  const child = new EventEmitter(); child.stdout = new PassThrough(); child.stdin = new PassThrough(); child.exitCode = null; child.killed = false;
  let invocation; let stdin = ''; child.stdin.on('data', chunk => { stdin += chunk; });
  const spawnImpl = (file, args, options) => { invocation = { file, args, options }; return child; };
  const token = 'A'.repeat(43); const launched = launchEngine({ enginePath: 'C:\\pkg\\lae-engine-cpu.exe', modelPath: `D:\\models\\${PRODUCT_MODEL_FILE}` }, { spawnImpl, token, environment: { SystemRoot: 'C:\\Windows', LAE_ENGINE_TOKEN: 'must-not-inherit' }, timeoutMs: 1000 });
  child.stdout.write('{"event":"ready","port":4321,"bind":"127.0.0.1","token_required":true}\n');
  assert.equal((await launched.ready).port, 4321);
  assert.deepEqual(invocation.args, ['serve', '--model', `D:\\models\\${PRODUCT_MODEL_FILE}`, '--backend', 'cpu', '--context', '8192', '--token-stdin']);
  assert.equal(invocation.args.includes(token), false); assert.equal(JSON.stringify(invocation.options.env).includes(token), false); assert.equal(Object.hasOwn(invocation.options.env, 'LAE_ENGINE_TOKEN'), false);
  assert.equal(stdin, `${token}\n`); assert.equal(invocation.options.shell, false); assert.deepEqual(invocation.options.stdio, ['pipe', 'pipe', 'ignore']);
});

test('foreground supervisor keeps bootstrap and bearers out of console and tears down both runtimes', async t => {
  const value = await inputs(t); const child = new EventEmitter(); child.exitCode = null; child.killed = false; child.kill = () => { child.killed = true; queueMicrotask(() => child.emit('exit', 0)); return true; };
  const server = new EventEmitter(); let hostClosed = false; let toolsClosed = false; const bearer = 'B'.repeat(43); const bootstrap = 'N'.repeat(43); const consoleOutput = []; const handoff = [];
  let gateOpened = false; let engineLaunchedAfterGate = false;
  const run = runPortableSupervisor(value, {
    platform: 'win32', architecture: 'x64', queryBuildInfoImpl: async () => {},
    awaitLaunchGateImpl: async pipe => { assert.equal(pipe, value.launchGatePipe); gateOpened = true; },
    launchEngineImpl: () => { engineLaunchedAfterGate = gateOpened; return { child, token: bearer, ready: Promise.resolve({ port: 4321 }) }; },
    createHostRuntimeImpl: async ({ engineToken }) => { assert.equal(engineToken, bearer); setImmediate(() => server.emit('close')); return { address: { token: 'H'.repeat(43), bootstrap_url: `http://127.0.0.1:9000/#bootstrap=${bootstrap}` }, host: { server, close: async () => { hostClosed = true; } }, externalTools: { shutdown: async () => { toolsClosed = true; } } }; },
    publishBootstrapImpl: async (pipe, url) => handoff.push({ pipe, url }), print: item => consoleOutput.push(item),
  });
  await run;
  assert.deepEqual(handoff, [{ pipe: value.bootstrapPipe, url: `http://127.0.0.1:9000/#bootstrap=${bootstrap}` }]);
  assert.deepEqual(consoleOutput, []); assert.equal(JSON.stringify(consoleOutput).includes(bootstrap), false); assert.equal(JSON.stringify(consoleOutput).includes(bearer), false); assert.equal(JSON.stringify(consoleOutput).includes('H'.repeat(43)), false);
  assert.equal(hostClosed, true); assert.equal(toolsClosed, true); assert.equal(child.killed, true);
  assert.equal(engineLaunchedAfterGate, true);
});
