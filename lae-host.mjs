#!/usr/bin/env node
import { FixtureEngineClient } from './host/engine/fixture-engine.mjs';
import { ConversationController } from './host/agent/controller.mjs';
import { HostServer } from './host/server/host-server.mjs';
import { mergeConfig } from './host/agent/config.mjs';

const config = mergeConfig({ engine: { mode: 'fixture' }, network: { provider: 'disabled' } });
const engine = new FixtureEngineClient();
const controller = new ConversationController({ engine });
const host = new HostServer({ controller, engine, config });
const address = await host.listen(Number(process.env.LAE_PORT ?? 0));
console.log(JSON.stringify({ ready: true, host: address.host, port: address.port, engine: 'fixture-0.1.0', network: 'disabled' }));
const shutdown = async () => { await host.close(); process.exit(0); };
process.once('SIGINT', shutdown); process.once('SIGTERM', shutdown);
