import assert from 'node:assert/strict';
import test from 'node:test';

import { createFilesystemTools } from '../../host/tools/local/filesystem.mjs';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';
import { HostServer } from '../../host/server/host-server.mjs';

const names = ['fs.list', 'fs.read_text', 'fs.search_text', 'fs.write_new', 'fs.apply_patch'];

test('Windows filesystem capabilities are withheld with explicit NOT_READY status', () => {
  const registry = createLocalToolRegistry({
    platform: 'win32',
    workspaces: [{ id: 'project', path: 'C:\\approved\\workspace', read: true, write: true }]
  });
  assert.deepEqual(Object.keys(registry), ['time.now', 'system.get_info']);
  for (const name of names) {
    const capability = registry.capabilitySnapshot.tools[name];
    assert.equal(capability.advertised, false, name);
    assert.equal(capability.status, 'NOT_READY', name);
    assert.equal(capability.reason, 'platform_path_safety_unavailable', name);
  }
});

test('direct Windows filesystem calls refuse before argument validation or policy access', async () => {
  const policy = new Proxy({}, { get() { throw new Error('workspace policy must not be touched'); } });
  const tools = createFilesystemTools(policy, { platform: 'win32' });
  const hostileArguments = new Proxy({}, { get() { throw new Error('arguments must not be read'); } });
  const call = name => ({ id: `windows-${name}`, name, arguments: hostileArguments });
  for (const name of names) {
    await assert.rejects(() => tools[name].execute(call(name)), error => error.code === 'platform_path_safety_unavailable');
  }
  await assert.rejects(() => tools['fs.apply_patch'].preview(call('fs.apply_patch')), error => error.code === 'platform_path_safety_unavailable');
});

test('Windows refusal is outermost of operator-grant policy', async () => {
  const policy = new Proxy({}, { get() { throw new Error('workspace policy must not be touched'); } });
  const grantControl = {
    grantFor() { throw new Error('grant policy must not be consulted'); },
    store: { subscribe() { throw new Error('grant store must not be consulted'); } }
  };
  const tools = createFilesystemTools(policy, { platform: 'win32', grantControl });
  const call = { id: 'windows-write', name: 'fs.write_new', get arguments() { throw new Error('arguments must not be read'); } };
  for (const hook of ['confirmationRequired', 'authorize', 'execute']) await assert.rejects(() => tools['fs.write_new'][hook](call), error => error.code === 'platform_path_safety_unavailable');
  const patchCall = { id: 'windows-patch', name: 'fs.apply_patch', get arguments() { throw new Error('arguments must not be read'); } };
  for (const hook of ['confirmationRequired', 'authorize', 'preview', 'execute']) await assert.rejects(() => tools['fs.apply_patch'][hook](patchCall), error => error.code === 'platform_path_safety_unavailable');
});

test('host status preserves local capability truth without making ready imply filesystem readiness', async t => {
  const registry = createLocalToolRegistry({ platform: 'win32', workspaces: [{ id: 'project', path: 'C:\\approved\\workspace', read: true, write: true }] });
  const controller = { cancelActive() {} };
  const host = new HostServer({ controller, engine: { health: async () => ({ ready: true, backend: 'fixture' }) }, localCapabilities: registry.capabilitySnapshot });
  const address = await host.listen(0); t.after(() => host.close());
  const response = await fetch(`${address.url}/api/status`, { headers: { authorization: `Bearer ${address.token}` } });
  const status = await response.json();
  assert.equal(status.engine.ready, true);
  assert.equal(status.local_capabilities.platform, 'win32');
  assert.equal(status.local_capabilities.tools['fs.read_text'].status, 'NOT_READY');
  assert.equal(status.local_capabilities.tools['fs.read_text'].advertised, false);
});
