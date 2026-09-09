#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { FixtureEngineClient } from './host/engine/fixture-engine.mjs';
import { NativeEngineClient } from './host/engine/native-engine-client.mjs';
import { ConversationController } from './host/agent/controller.mjs';
import { ActionJournal, DescriptorActionJournal } from './host/agent/action-journal.mjs';
import { HostServer } from './host/server/host-server.mjs';
import { mergeConfig } from './host/agent/config.mjs';
import { createLocalToolRegistry } from './host/tools/local/index.mjs';
import { createExternalToolRegistry, OperatorGrantStore, OperatorGrantControl, buildOperatorGrantBindings } from './host/providers/index.mjs';

async function loadFileConfig(env) {
  const configPath = env.LAE_CONFIG_PATH;
  if (!configPath) return {};
  try { return JSON.parse(await readFile(configPath, 'utf8')); } catch (error) { throw new Error(`config_load_failed: ${error.message}`); }
}

/** Build production wiring without binding a socket or starting a request. */
export async function createHostComposition({ fileConfig = {}, env = process.env, engineFactory } = {}) {
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
  const token = env.LAE_ENGINE_TOKEN;
  const model = env.LAE_ENGINE_MODEL ?? fileConfig.engine?.model;
  const backend = env.LAE_ENGINE_BACKEND ?? fileConfig.engine?.backend;
  const requestTimeoutMs = Number(env.LAE_ENGINE_TIMEOUT_MS ?? fileConfig.engine?.request_timeout_ms ?? 120000);
  if (mode === 'fixture' && (endpoint || token)) throw new Error('native engine settings supplied while fixture mode is selected');
  const config = mergeConfig({ ...fileConfig, engine: { ...(fileConfig.engine ?? {}), mode } });
  if (mode === 'native' && (!model || !backend)) throw new Error('native engine model and backend must be explicit in config or environment');
  let engine; let operatorGrants; let externalTools; let actionJournal;
  try {
    engine = engineFactory ? await engineFactory() : mode === 'native' ? new NativeEngineClient({ endpoint, token, model, backend, timeoutMs: requestTimeoutMs }) : new FixtureEngineClient();
    if (mode === 'native') await engine.waitReady();
    const grantStore = new OperatorGrantStore();
    operatorGrants = new OperatorGrantControl({ store: grantStore, bindings: buildOperatorGrantBindings(config) });
    externalTools = createExternalToolRegistry({ config: config.providers, workspaceRoots: config.workspace_roots, graph: { grantStore } });
    if (journalDescriptor !== undefined) actionJournal = await DescriptorActionJournal.open({ fd: journalDescriptor, ownsDescriptor: true });
    else if (journalDirectory !== undefined) actionJournal = await ActionJournal.open({ directory: journalDirectory });
    const processEnvironment = Object.fromEntries(['SystemRoot', 'WINDIR'].filter(key => typeof env[key] === 'string').map(key => [key, env[key]]));
    const localTools = createLocalToolRegistry({ workspaces: config.workspace_roots, applications: config.applications, process_actions: config.process_actions, processEnvironment, networkProvider: config.network.provider, grantControl: operatorGrants });
    const localCapabilities = localTools.capabilitySnapshot;
    const toolRegistry = { ...localTools, ...externalTools };
    const controller = new ConversationController({ engine, actionJournal, toolRegistry });
    const host = new HostServer({ controller, engine, config, providers: externalTools.providerStatus, providerAuth: externalTools.providerAuthControl, providerShutdown: externalTools.shutdown, operatorGrants, actionJournal, localCapabilities });
    return { config, mode, engine, grantStore, operatorGrants, externalTools, localTools, toolRegistry, controller, host, actionJournal };
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
  else console.log(JSON.stringify({ ready: true, host: address.host, port: address.port, bootstrap: 'hidden-use-approved-launcher', engine: composition.mode, network: composition.config.network.provider, action_journal: composition.actionJournal?.health().state ?? 'unavailable' }));
  return { ...composition, address };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const running = await bootstrap();
  const shutdown = async () => { await running.host.close(); process.exit(0); };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
}
