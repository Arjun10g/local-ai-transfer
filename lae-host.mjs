#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { FixtureEngineClient } from './host/engine/fixture-engine.mjs';
import { NativeEngineClient } from './host/engine/native-engine-client.mjs';
import { ConversationController } from './host/agent/controller.mjs';
import { ActionJournal } from './host/agent/action-journal.mjs';
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
  const engine = engineFactory ? await engineFactory() : mode === 'native' ? new NativeEngineClient({ endpoint, token, model, backend, timeoutMs: requestTimeoutMs }) : new FixtureEngineClient();
  if (mode === 'native') await engine.waitReady();
  const grantStore = new OperatorGrantStore();
  const operatorGrants = new OperatorGrantControl({ store: grantStore, bindings: buildOperatorGrantBindings(config) });
  const externalTools = createExternalToolRegistry({ config: config.providers, workspaceRoots: config.workspace_roots, graph: { grantStore } });
  const processEnvironment = Object.fromEntries(['SystemRoot', 'WINDIR'].filter(key => typeof env[key] === 'string').map(key => [key, env[key]]));
  const actionJournal = env.LAE_ACTION_JOURNAL_DIR ? await ActionJournal.open({ directory: env.LAE_ACTION_JOURNAL_DIR }) : undefined;
  const localTools = createLocalToolRegistry({ workspaces: config.workspace_roots, applications: config.applications, process_actions: config.process_actions, processEnvironment, networkProvider: config.network.provider, grantControl: operatorGrants });
  const localCapabilities = localTools.capabilitySnapshot;
  const toolRegistry = { ...localTools, ...externalTools };
  const controller = new ConversationController({ engine, actionJournal, toolRegistry });
  const host = new HostServer({ controller, engine, config, providers: externalTools.providerStatus, providerAuth: externalTools.providerAuthControl, providerShutdown: externalTools.shutdown, operatorGrants, actionJournal, localCapabilities });
  return { config, mode, engine, grantStore, operatorGrants, externalTools, localTools, toolRegistry, controller, host, actionJournal };
}

export async function bootstrap({ fileConfig, env = process.env } = {}) {
  const resolvedConfig = fileConfig ?? await loadFileConfig(env);
  const composition = await createHostComposition({ fileConfig: resolvedConfig, env });
  const address = await composition.host.listen(Number(env.LAE_PORT ?? 0));
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
