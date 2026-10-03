#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { FixtureEngineClient } from './host/engine/fixture-engine.mjs';
import { NativeEngineClient } from './host/engine/native-engine-client.mjs';
import { ConversationController } from './host/agent/controller.mjs';
import { CONTEXT_DEFAULTS } from './host/agent/context-budget.mjs';
import { ActionJournal, DescriptorActionJournal } from './host/agent/action-journal.mjs';
import { HostServer } from './host/server/host-server.mjs';
import { ENGINE_GENERATION_LIMITS, delegateOptionsFromConfig, memoryOptionsFromConfig, mergeConfig } from './host/agent/config.mjs';
import { DelegateService, DELEGATE_GRANT_BINDING } from './host/delegate/service.mjs';
import { createDelegateToolRegistry } from './host/delegate/tools.mjs';
import { resolveStateDir } from './host/delegate/state.mjs';
import { createLocalToolRegistry } from './host/tools/local/index.mjs';
import { createExternalToolRegistry, OperatorGrantStore, OperatorGrantControl, buildOperatorGrantBindings } from './host/providers/index.mjs';

async function loadFileConfig(env) {
  const configPath = env.LAE_CONFIG_PATH;
  if (!configPath) return {};
  try { return JSON.parse(await readFile(configPath, 'utf8')); } catch (error) { throw new Error(`config_load_failed: ${error.message}`); }
}

// The controller's token budget must describe the window the engine really
// runs (`--context`, default 8192), or it either wastes the window or sends
// prompts the engine refuses.  512 is the smallest window that still fits a
// short plain-chat turn; 16384 is the engine's own ceiling.
export const CONTEXT_TOKEN_RANGE = Object.freeze({ min: 512, max: 16384, fallback: CONTEXT_DEFAULTS.contextTokens });
const inContextRange = value => Number.isInteger(value) && value >= CONTEXT_TOKEN_RANGE.min && value <= CONTEXT_TOKEN_RANGE.max;

export function parseContextTokensEnv(value) {
  if (value === undefined) return null;
  const parsed = typeof value === 'string' && /^[1-9]\d{0,5}$/u.test(value) ? Number(value) : NaN;
  if (!inContextRange(parsed)) throw new Error(`invalid LAE_CONTEXT_TOKENS; expected an integer ${CONTEXT_TOKEN_RANGE.min}-${CONTEXT_TOKEN_RANGE.max}`);
  return parsed;
}

// GET /metrics (bearer-authenticated, like every engine route) reports the
// live llama.cpp window as `runtime.context_tokens`.  null when the engine
// cannot say: the fixture has no endpoint, and a backend without a loaded
// context reports `runtime: {}`.
export async function readEngineContextTokens(engine, { timeoutMs = 10000 } = {}) {
  if (typeof engine?.request !== 'function') return null;
  try {
    const response = await engine.request('/metrics', {}, { timeoutMs });
    const text = await response.text(); if (text.length > 65536) return null;
    const value = JSON.parse(text)?.runtime?.context_tokens;
    return Number.isInteger(value) ? value : null;
  } catch { return null; }
}

// The smaller of the operator's setting and the engine's real window: a
// budget larger than the engine's window overflows, a smaller one is merely
// conservative.  A window the engine reports outside the range is refused
// rather than silently replaced by a guess.
export function resolveContextTokens({ envTokens = null, engineTokens = null } = {}) {
  if (engineTokens !== null && !inContextRange(engineTokens)) throw new Error(`engine context window ${engineTokens} is outside ${CONTEXT_TOKEN_RANGE.min}-${CONTEXT_TOKEN_RANGE.max} tokens`);
  const known = [envTokens, engineTokens].filter(value => value !== null);
  return known.length ? Math.min(...known) : CONTEXT_TOKEN_RANGE.fallback;
}

// The engine reserves the request's `max_tokens` out of the same window the
// controller budgets, so both must use one number: the engine client's
// `maxTokens`, lowered for a small window so the answer never takes more
// than a quarter of it (the controller's own limit).
export function outputTokenReservation(engine, contextTokens) {
  const engineMax = Number.isInteger(engine?.maxTokens) && engine.maxTokens > 0 ? engine.maxTokens : CONTEXT_DEFAULTS.maxOutputTokens;
  const maxOutputTokens = Math.min(engineMax, Math.floor(contextTokens / 4));
  if (Number.isInteger(engine?.maxTokens) && engine.maxTokens > maxOutputTokens) engine.maxTokens = maxOutputTokens;
  return maxOutputTokens;
}

// Generation deadlines and answer cap for the native client: environment
// first, then the config file; anything unset keeps the client's default.
export function engineGenerationOptions(env, engineConfig = {}) {
  const option = { first_token_timeout_ms: 'firstTokenTimeoutMs', idle_timeout_ms: 'idleTimeoutMs', total_timeout_ms: 'totalTimeoutMs', max_tokens: 'maxTokens' };
  const out = {};
  for (const [key, min, max, envName] of ENGINE_GENERATION_LIMITS) {
    const raw = env[envName];
    if (raw !== undefined) {
      const value = typeof raw === 'string' && /^[1-9]\d{0,9}$/u.test(raw) ? Number(raw) : NaN;
      if (!Number.isInteger(value) || value < min || value > max) throw new Error(`invalid ${envName}; expected an integer ${min}-${max}`);
      out[option[key]] = value;
    } else if (engineConfig[key] !== undefined) out[option[key]] = engineConfig[key];
  }
  return out;
}

/** Build production wiring without binding a socket or starting a request. */
export async function createHostComposition({ fileConfig = {}, env = process.env, engineFactory } = {}) {
  // The engine bearer token is read once and removed from the live process
  // environment before anything else runs, so no child (tools, providers,
  // launched apps) inherits it and a crash dump of the environment omits it.
  // A caller-supplied env object is left alone: only process.env is inherited.
  const token = env.LAE_ENGINE_TOKEN;
  if (env === process.env) delete process.env.LAE_ENGINE_TOKEN;
  const journalDirectory = env.LAE_ACTION_JOURNAL_DIR;
  const journalDescriptorText = env.LAE_ACTION_JOURNAL_FD;
  if (journalDirectory !== undefined && journalDescriptorText !== undefined) throw new Error('action journal directory and descriptor are mutually exclusive');
  let journalDescriptor;
  if (journalDescriptorText !== undefined) {
    if (typeof journalDescriptorText !== 'string' || !/^(?:[3-9]|[1-9]\d+)$/u.test(journalDescriptorText)) throw new Error('invalid LAE_ACTION_JOURNAL_FD');
    journalDescriptor = Number(journalDescriptorText); if (!Number.isSafeInteger(journalDescriptor) || journalDescriptor > 0x7fffffff) throw new Error('invalid LAE_ACTION_JOURNAL_FD');
  }
  const configuredMode = fileConfig.engine?.mode;
  const envMode = env.LAE_ENGINE_MODE;
  if (envMode && !['fixture', 'native'].includes(envMode)) throw new Error('invalid LAE_ENGINE_MODE; expected fixture or native');
  if (envMode && configuredMode && envMode !== configuredMode) throw new Error('engine mode conflict between config and LAE_ENGINE_MODE');
  const mode = envMode ?? configuredMode ?? 'fixture';
  const endpoint = env.LAE_ENGINE_ENDPOINT ?? fileConfig.engine?.endpoint;
  const model = env.LAE_ENGINE_MODEL ?? fileConfig.engine?.model;
  const backend = env.LAE_ENGINE_BACKEND ?? fileConfig.engine?.backend;
  const requestTimeoutMs = Number(env.LAE_ENGINE_TIMEOUT_MS ?? fileConfig.engine?.request_timeout_ms ?? 120000);
  if (mode === 'fixture' && (endpoint || token)) throw new Error('native engine settings supplied while fixture mode is selected');
  const envContextTokens = parseContextTokensEnv(env.LAE_CONTEXT_TOKENS);
  const config = mergeConfig({ ...fileConfig, engine: { ...(fileConfig.engine ?? {}), mode } });
  const generationOptions = engineGenerationOptions(env, config.engine);
  // Delegated jobs are off unless the config or LAE_DELEGATE_ENABLED=1 turns
  // them on; off means no routes, no key file, no host.json.
  const delegateOptions = delegateOptionsFromConfig(config, env);
  const stateDir = delegateOptions.enabled ? resolveStateDir({ env }) : null;
  if (mode === 'native' && (!model || !backend)) throw new Error('native engine model and backend must be explicit in config or environment');
  let engine; let operatorGrants; let externalTools; let actionJournal;
  try {
    engine = engineFactory ? await engineFactory() : mode === 'native' ? new NativeEngineClient({ endpoint, token, model, backend, timeoutMs: requestTimeoutMs, ...generationOptions }) : new FixtureEngineClient();
    if (mode === 'native') await engine.waitReady();
    const grantStore = new OperatorGrantStore();
    operatorGrants = new OperatorGrantControl({ store: grantStore, bindings: [...buildOperatorGrantBindings(config), ...(delegateOptions.enabled ? [DELEGATE_GRANT_BINDING] : [])] });
    externalTools = createExternalToolRegistry({ config: config.providers, workspaceRoots: config.workspace_roots, graph: { grantStore } });
    if (journalDescriptor !== undefined) actionJournal = await DescriptorActionJournal.open({ fd: journalDescriptor, ownsDescriptor: true });
    else if (journalDirectory !== undefined) actionJournal = await ActionJournal.open({ directory: journalDirectory });
    const processEnvironment = Object.fromEntries(['SystemRoot', 'WINDIR'].filter(key => typeof env[key] === 'string').map(key => [key, env[key]]));
    const localTools = createLocalToolRegistry({ workspaces: config.workspace_roots, applications: config.applications, process_actions: config.process_actions, processEnvironment, networkProvider: config.network.provider, grantControl: operatorGrants });
    const localCapabilities = localTools.capabilitySnapshot;
    const toolRegistry = { ...localTools, ...externalTools };
    const contextTokens = resolveContextTokens({ envTokens: envContextTokens, engineTokens: await readEngineContextTokens(engine) });
    const maxOutputTokens = outputTokenReservation(engine, contextTokens);
    const controller = new ConversationController({ engine, actionJournal, toolRegistry, contextTokens, maxOutputTokens, confirmationTimeoutMs: config.host.confirmation_timeout_ms, memory: memoryOptionsFromConfig(config) });
    await controller.reconcileRestartActions();
    let delegate = null;
    if (delegateOptions.enabled) {
      // Its own registry: read-only tools, files only in flagged folders.
      const delegateTools = createDelegateToolRegistry({ workspaceRoots: config.workspace_roots });
      delegate = new DelegateService({ engine, userController: controller, toolRegistry: delegateTools.registry, workspaceIds: delegateTools.workspaceIds, stateDir, grantControl: operatorGrants, options: delegateOptions, contextTokens, maxOutputTokens, requestTimeoutMs: config.host.request_timeout_ms });
    }
    const host = new HostServer({ controller, engine, config, providers: externalTools.providerStatus, providerAuth: externalTools.providerAuthControl, providerShutdown: externalTools.shutdown, operatorGrants, actionJournal, localCapabilities, delegate });
    return { config, mode, engine, grantStore, operatorGrants, externalTools, localTools, toolRegistry, controller, host, actionJournal, delegate };
  } catch (error) {
    for (const cleanup of [() => operatorGrants?.revokeAll?.(), () => externalTools?.shutdown?.(), () => actionJournal?.close?.(), () => engine?.shutdown?.()]) {
      try { await cleanup(); } catch {}
    }
    throw error;
  }
}

export async function bootstrap({ fileConfig, env = process.env, compositionFactory = createHostComposition } = {}) {
  const resolvedConfig = fileConfig ?? await loadFileConfig(env);
  const composition = await compositionFactory({ fileConfig: resolvedConfig, env });
  let address;
  try { address = await composition.host.listen(Number(env.LAE_PORT ?? 0)); }
  catch (error) { try { await composition.host.close?.(); } catch {} throw error; }
  if (env.LAE_REVEAL_BOOTSTRAP_URL === '1') console.log(address.bootstrap_url);
  else console.log(JSON.stringify({ ready: true, host: address.host, port: address.port, bootstrap: 'hidden-use-approved-launcher', engine: composition.mode, context_tokens: composition.controller?.contextTokens ?? null, network: composition.config.network.provider, action_journal: composition.actionJournal?.health().state ?? 'unavailable', delegate: composition.delegate ? 'enabled' : 'off' }));
  return { ...composition, address };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const running = await bootstrap();
  const shutdown = async () => { await running.host.close(); process.exit(0); };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
}
