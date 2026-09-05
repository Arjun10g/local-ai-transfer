import assert from 'node:assert/strict';
import test from 'node:test';

import { createFilesystemTools } from '../../host/tools/local/filesystem.mjs';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';

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
