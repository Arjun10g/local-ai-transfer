#!/usr/bin/env node
import { spawn as spawnProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { lstat, readFile, realpath } from 'node:fs/promises';
import { basename, isAbsolute, resolve } from 'node:path';
import { createConnection } from 'node:net';
import { pathToFileURL } from 'node:url';

import { ConversationController } from './host/agent/controller.mjs';
import { mergeConfig } from './host/agent/config.mjs';
import { parseStrictJson } from './host/agent/tool-envelope.mjs';
import { NativeEngineClient } from './host/engine/native-engine-client.mjs';
import { createExternalToolRegistry, OperatorGrantStore, OperatorGrantControl, buildOperatorGrantBindings } from './host/providers/index.mjs';
import { HostServer } from './host/server/host-server.mjs';
import { createLocalToolRegistry } from './host/tools/local/index.mjs';

export const PRODUCT_MODEL_FILE = 'Qwen3.5-9B-Q4_K_M.gguf';
export const PRODUCT_MODEL_ID = 'qwen35-9b-q4-k-m';
export const PRODUCT_MODEL_SIZE = 5629109088;
export const PRODUCT_MODEL_SHA256 = 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b';
export const LLAMA_CPP_REVISION = '3581ba0cf591b3f772fbb002de0f70e294bc0396';
const READY_LIMIT = 64 * 1024;
const START_TIMEOUT_MS = 180_000;

function fail(message) { throw new Error(message); }
function exactKeys(value, allowed, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${name} must be an object`);
  for (const key of Object.keys(value)) if (!allowed.includes(key)) fail(`${name} has unknown key: ${key}`);
  return value;
}

export function parseSupervisorArguments(argv) {
  if (!Array.isArray(argv)) fail('arguments must be an array');
  const output = Object.create(null); const allowed = new Set(['--engine', '--model', '--config', '--bootstrap-pipe', '--launch-gate-pipe']);
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]; const value = argv[index + 1];
    if (!allowed.has(key) || typeof value !== 'string' || !value || Object.hasOwn(output, key)) fail('invalid or duplicate supervisor argument');
    output[key] = value;
  }
  if (!output['--engine'] || !output['--model'] || !output['--bootstrap-pipe'] || !output['--launch-gate-pipe']) fail('--engine, --model, --bootstrap-pipe, and --launch-gate-pipe are required');
  return { enginePath: output['--engine'], modelPath: output['--model'], configPath: output['--config'], bootstrapPipe: output['--bootstrap-pipe'], launchGatePipe: output['--launch-gate-pipe'] };
}

async function regularCanonicalFile(input, label, { expectedName } = {}) {
  if (typeof input !== 'string' || !isAbsolute(input) || input.length > 4096 || input.includes('\0')) fail(`${label} path must be a bounded absolute path`);
  const before = await lstat(input).catch(() => null);
  if (!before?.isFile() || before.isSymbolicLink()) fail(`${label} must be a regular non-link file`);
  const canonical = await realpath(input);
  const after = await lstat(canonical).catch(() => null);
  if (!after?.isFile() || after.isSymbolicLink() || before.dev !== after.dev || before.ino !== after.ino) fail(`${label} changed while it was resolved`);
  if (expectedName && basename(canonical).toLowerCase() !== expectedName.toLowerCase()) fail(`${label} filename must be ${expectedName}`);
  return canonical;
}

export async function resolveSupervisorInputs(options, { platform = process.platform, architecture = process.arch } = {}) {
  exactKeys(options, ['enginePath', 'modelPath', 'configPath', 'bootstrapPipe', 'launchGatePipe'], 'supervisor options');
  if (platform !== 'win32') fail('portable supervisor requires Windows x64');
  if (architecture !== 'x64') fail('portable supervisor requires Windows x64');
  if (typeof options.bootstrapPipe !== 'string' || !/^LocalBMO-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(options.bootstrapPipe)) fail('bootstrap pipe identity is invalid');
  if (typeof options.launchGatePipe !== 'string' || !/^LocalBMOGate-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(options.launchGatePipe)) fail('launch gate pipe identity is invalid');
  if (options.launchGatePipe === options.bootstrapPipe) fail('launcher pipes must be distinct');
  const enginePath = await regularCanonicalFile(options.enginePath, 'engine', { expectedName: 'lae-engine-cpu.exe' });
  const modelPath = await regularCanonicalFile(options.modelPath, 'model', { expectedName: PRODUCT_MODEL_FILE });
  let fileConfig = {};
  if (options.configPath) {
    const configPath = await regularCanonicalFile(options.configPath, 'host config');
    const configText = await readFile(configPath, 'utf8');
    fileConfig = parseStrictJson(configText, { maxBytes: 64 * 1024, maxDepth: 16, maxString: 8192, maxArray: 64, maxObject: 64 });
    exactKeys(fileConfig, ['version', 'host', 'workspace_roots', 'applications', 'process_actions', 'network', 'providers'], 'host config');
  }
  const config = mergeConfig({ ...fileConfig, engine: { mode: 'native', model: PRODUCT_MODEL_ID, backend: 'cpu', request_timeout_ms: 120000 } });
  return { enginePath, modelPath, bootstrapPipe: options.bootstrapPipe, launchGatePipe: options.launchGatePipe, config };
}

export async function awaitLaunchGate(pipeName, { connectImpl = createConnection } = {}) {
  if (typeof pipeName !== 'string' || !/^LocalBMOGate-[0-9a-f-]{36}$/iu.test(pipeName)) fail('launch gate handoff is invalid');
  const pipePath = `\\\\.\\pipe\\${pipeName}`;
  await new Promise((resolveGate, rejectGate) => {
    const socket = connectImpl(pipePath); let settled = false; let bytes = '';
    const finish = error => { if (settled) return; settled = true; clearTimeout(timer); socket.removeAllListeners(); socket.destroy(); error ? rejectGate(new Error('launch gate handoff failed')) : resolveGate(); };
    const timer = setTimeout(() => finish(new Error('timeout')), 10_000);
    socket.once('error', finish);
    socket.on('data', chunk => {
      bytes += chunk.toString('utf8');
      if (bytes.length > 3) return finish(new Error('invalid gate'));
      if (bytes === 'GO\n') finish();
    });
    socket.once('end', () => { if (!settled) finish(new Error('incomplete gate')); });
  });
}

export async function publishBootstrapUrl(pipeName, bootstrapUrl, { connectImpl = createConnection } = {}) {
  if (typeof pipeName !== 'string' || !/^LocalBMO-[0-9a-f-]{36}$/iu.test(pipeName) ||
      typeof bootstrapUrl !== 'string' || !/^http:\/\/127\.0\.0\.1:\d{1,5}\/#bootstrap=[A-Za-z0-9_-]{43}$/u.test(bootstrapUrl)) fail('bootstrap handoff is invalid');
  const pipePath = `\\\\.\\pipe\\${pipeName}`;
  await new Promise((resolveWrite, rejectWrite) => {
    const socket = connectImpl(pipePath); let settled = false;
    const finish = error => { if (settled) return; settled = true; clearTimeout(timer); socket.removeAllListeners(); error ? rejectWrite(new Error('bootstrap handoff failed')) : resolveWrite(); };
    const timer = setTimeout(() => { socket.destroy(); finish(new Error('timeout')); }, 10_000);
    socket.once('error', finish); socket.once('connect', () => socket.end(`${bootstrapUrl}\n`, () => finish()));
  });
}

function minimalChildEnvironment(environment = process.env) {
  return Object.fromEntries(['SystemRoot', 'WINDIR', 'TEMP', 'TMP'].filter(key => typeof environment[key] === 'string').map(key => [key, environment[key]]));
}

export function validateServingBuildInfo(value) {
  exactKeys(value, ['engine_version', 'api_version', 'backend', 'llama_cpp_revision', 'model', 'product_model_size_bytes', 'product_model_sha256'], 'serving engine build info');
  if (value.backend !== `llama.cpp/${LLAMA_CPP_REVISION.slice(0, 8)}/cpu` || value.llama_cpp_revision !== LLAMA_CPP_REVISION || value.model !== PRODUCT_MODEL_ID ||
      value.product_model_size_bytes !== PRODUCT_MODEL_SIZE || value.product_model_sha256 !== PRODUCT_MODEL_SHA256) fail('serving engine identity does not match the portable CPU product');
  return value;
}

export function parseReadyEvent(text) {
  const value = exactKeys(parseStrictJson(text, { maxBytes: 4096, maxDepth: 2, maxString: 128, maxArray: 1, maxObject: 8 }), ['event', 'port', 'bind', 'token_required'], 'engine ready event');
  if (value.event !== 'ready' || value.bind !== '127.0.0.1' || value.token_required !== true || !Number.isInteger(value.port) || value.port < 1 || value.port > 65535) fail('engine emitted an invalid ready event');
  return value;
}

export function launchEngine({ enginePath, modelPath }, { spawnImpl = spawnProcess, environment = process.env, token = randomBytes(32).toString('base64url'), timeoutMs = START_TIMEOUT_MS } = {}) {
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(token)) fail('generated engine token is invalid');
  const args = ['serve', '--model', modelPath, '--backend', 'cpu', '--context', '8192', '--token-stdin'];
  const child = spawnImpl(enginePath, args, { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'], env: minimalChildEnvironment(environment) });
  const ready = new Promise((accept, reject) => {
    let settled = false; let pending = ''; let observedBytes = 0;
    const finish = (error, value) => { if (settled) return; settled = true; clearTimeout(timer); child.off('error', onError); child.off('exit', onExit); child.stdout?.off('data', onData); if (!error) child.stdout?.on('data', () => {}); error ? reject(error) : accept(value); };
    const onError = () => finish(new Error('engine process could not be started'));
    const onExit = () => finish(new Error('engine exited before readiness'));
    const onData = chunk => {
      observedBytes += chunk.length; if (observedBytes > READY_LIMIT) return finish(new Error('engine readiness output exceeded its bound'));
      pending += chunk.toString('utf8');
      while (pending.includes('\n')) {
        const newline = pending.indexOf('\n'); const line = pending.slice(0, newline).replace(/\r$/u, ''); pending = pending.slice(newline + 1);
        if (!line) continue;
        try { return finish(null, parseReadyEvent(line)); } catch { return finish(new Error('engine readiness output was invalid')); }
      }
    };
    const timer = setTimeout(() => finish(new Error('engine readiness timed out')), timeoutMs);
    child.once('error', onError); child.once('exit', onExit); child.stdout?.on('data', onData);
  });
  child.stdin?.end(`${token}\n`);
  return { child, token, args, ready };
}

async function stopChild(child) {
  if (!child || child.exitCode !== null || child.killed) return;
  child.kill('SIGTERM');
  await new Promise(resolveStop => { const timer = setTimeout(() => { child.kill('SIGKILL'); resolveStop(); }, 5000); child.once('exit', () => { clearTimeout(timer); resolveStop(); }); });
}

export async function createHostRuntime({ config, engineEndpoint, engineToken }) {
  const engine = new NativeEngineClient({ endpoint: engineEndpoint, token: engineToken, model: PRODUCT_MODEL_ID, backend: 'cpu', timeoutMs: config.engine.request_timeout_ms });
  await engine.waitReady();
  validateServingBuildInfo(await engine.buildInfo());
  const grantStore = new OperatorGrantStore();
  const operatorGrants = new OperatorGrantControl({ store: grantStore, bindings: buildOperatorGrantBindings(config) });
  const externalTools = createExternalToolRegistry({ config: config.providers, workspaceRoots: config.workspace_roots, graph: { grantStore } });
  const processEnvironment = Object.fromEntries(['SystemRoot', 'WINDIR'].filter(key => typeof process.env[key] === 'string').map(key => [key, process.env[key]]));
  const controller = new ConversationController({ engine, toolRegistry: { ...createLocalToolRegistry({ workspaces: config.workspace_roots, applications: config.applications, process_actions: config.process_actions, processEnvironment, networkProvider: config.network.provider, grantControl: operatorGrants }), ...externalTools } });
  const host = new HostServer({ controller, engine, config, providers: externalTools.providerStatus, providerAuth: externalTools.providerAuthControl, operatorGrants });
  const address = await host.listen(0);
  return { host, address, externalTools };
}

export async function runPortableSupervisor(options, dependencies = {}) {
  const resolved = await resolveSupervisorInputs(options, { platform: dependencies.platform ?? process.platform, architecture: dependencies.architecture ?? process.arch });
  // The launcher signals only after AssignProcessToJobObject succeeds. No child
  // can therefore escape the kill-on-close job during the assignment window.
  await (dependencies.awaitLaunchGateImpl ?? awaitLaunchGate)(resolved.launchGatePipe, dependencies);
  const launched = (dependencies.launchEngineImpl ?? launchEngine)(resolved, dependencies);
  let runtime;
  try {
    const ready = await launched.ready;
    runtime = await (dependencies.createHostRuntimeImpl ?? createHostRuntime)({ config: resolved.config, engineEndpoint: `http://127.0.0.1:${ready.port}`, engineToken: launched.token });
    // Both bearers remain only in memory. The one-time fragment URL crosses a
    // private launcher pipe and is never written to stdout/stderr or argv.
    await (dependencies.publishBootstrapImpl ?? publishBootstrapUrl)(resolved.bootstrapPipe, runtime.address.bootstrap_url, dependencies);
    if (launched.child.exitCode !== null) fail('engine exited while the host was starting');
    await new Promise((resolveRun, rejectRun) => {
      const cleanup = () => { process.off('SIGINT', onSignal); process.off('SIGTERM', onSignal); launched.child.off('exit', onEngineExit); runtime.host.server?.off('close', onHostClose); };
      const onSignal = () => { cleanup(); resolveRun(); };
      const onEngineExit = () => { cleanup(); rejectRun(new Error('engine exited while the host was running')); };
      const onHostClose = () => { cleanup(); resolveRun(); };
      process.once('SIGINT', onSignal); process.once('SIGTERM', onSignal);
      launched.child.once('exit', onEngineExit); runtime.host.server?.once('close', onHostClose);
    });
  } finally {
    await runtime?.externalTools?.shutdown?.();
    await runtime?.host?.close?.();
    await stopChild(launched.child);
  }
}

const isMain = process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (isMain) {
  if (process.versions.node !== '24.20.0') fail('portable supervisor requires the pinned packaged Node.js 24.20.0 runtime');
  await runPortableSupervisor(parseSupervisorArguments(process.argv.slice(2)));
}
