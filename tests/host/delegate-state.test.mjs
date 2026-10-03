import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdir, readFile, readdir, stat, symlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { DELEGATE_KEY_PATTERN, ensureStateDir, loadOrCreateDelegateKey, readDelegateKey, removeHostFile, resolveStateDir, rotateDelegateKey, writeHostFile } from '../../host/delegate/state.mjs';
import { delegateOptionsFromConfig, validateConfig } from '../../host/agent/config.mjs';
import { setup, tempDir } from './delegate-helpers.mjs';

const posix = process.platform !== 'win32';
const mode = async path => (await stat(path)).mode & 0o777;

test('the state directory is per user on each platform, with an absolute override', () => {
  assert.equal(resolveStateDir({ env: { LOCALAPPDATA: 'C:\\Users\\op\\AppData\\Local' }, platform: 'win32' }), 'C:\\Users\\op\\AppData\\Local\\BMO');
  assert.throws(() => resolveStateDir({ env: {}, platform: 'win32' }), { code: 'state_dir_invalid' });
  assert.throws(() => resolveStateDir({ env: { LOCALAPPDATA: '\\\\server\\share' }, platform: 'win32' }), { code: 'state_dir_invalid' });
  assert.equal(resolveStateDir({ env: {}, platform: 'darwin', home: '/Users/op' }), '/Users/op/Library/Application Support/BMO');
  assert.equal(resolveStateDir({ env: {}, platform: 'linux', home: '/home/op' }), '/home/op/.local/state/bmo');
  assert.equal(resolveStateDir({ env: { XDG_STATE_HOME: '/xdg' }, platform: 'linux', home: '/home/op' }), '/xdg/bmo');
  assert.equal(resolveStateDir({ env: { XDG_STATE_HOME: 'relative' }, platform: 'linux', home: '/home/op' }), '/home/op/.local/state/bmo');
  assert.equal(resolveStateDir({ env: { BMO_STATE_DIR: '/tmp/bmo-x' }, platform: 'linux', home: '/home/op' }), '/tmp/bmo-x');
  assert.throws(() => resolveStateDir({ env: { BMO_STATE_DIR: 'rel/dir' }, platform: 'linux' }), { code: 'state_dir_invalid' });
});

test('the directory is created 0700, an open one is closed, a symlink is refused', { skip: !posix }, async t => {
  const dir = await tempDir(t);
  const fresh = join(dir, 'a', 'BMO');
  assert.deepEqual(await ensureStateDir(fresh), { owner_verified: true, mode_verified: true });
  assert.equal(await mode(fresh), 0o700);
  const open = join(dir, 'open'); await mkdir(open, { mode: 0o755 }); await chmod(open, 0o755);
  await ensureStateDir(open); assert.equal(await mode(open), 0o700);
  const link = join(dir, 'link'); await symlink(fresh, link);
  await assert.rejects(ensureStateDir(link), { code: 'state_dir_unsafe' });
});

test('the key: 32 random bytes as base64url, 0600, stable until rotated, malformed refused', async t => {
  const dir = await tempDir(t); await ensureStateDir(dir);
  const key = await loadOrCreateDelegateKey(dir);
  assert.match(key, DELEGATE_KEY_PATTERN); assert.equal(Buffer.from(key, 'base64url').length, 32);
  if (posix) assert.equal(await mode(join(dir, 'delegate-key')), 0o600);
  assert.equal(await loadOrCreateDelegateKey(dir), key, 'a second start keeps the pasted key valid');
  const rotated = await rotateDelegateKey(dir);
  assert.notEqual(rotated, key); assert.equal(await readDelegateKey(dir), rotated);
  if (posix) assert.equal(await mode(join(dir, 'delegate-key')), 0o600);
  assert.deepEqual((await readdir(dir)).sort(), ['delegate-key'], 'no temporary file is left behind');
  await writeFile(join(dir, 'delegate-key'), 'not a key\n');
  await assert.rejects(readDelegateKey(dir), { code: 'delegate_key_invalid' });
  await assert.rejects(loadOrCreateDelegateKey(dir), { code: 'delegate_key_invalid' }, 'a damaged key is never silently replaced');
  if (posix) {
    await writeFile(join(dir, 'delegate-key'), `${rotated}\n`); await chmod(join(dir, 'delegate-key'), 0o644);
    assert.equal(await readDelegateKey(dir), rotated); assert.equal(await mode(join(dir, 'delegate-key')), 0o600);
  }
  assert.equal(await readDelegateKey(join(dir, 'missing')), null);
});

test('host.json: exact shape, 0600, atomic overwrite of a stale file, removed only by its writer', async t => {
  const dir = await tempDir(t); await ensureStateDir(dir);
  await writeFile(join(dir, 'host.json'), '{"version":1,"port":1,"pid":1,"started_at":"stale"}');
  const record = await writeHostFile(dir, { port: 43123 });
  const read = JSON.parse(await readFile(join(dir, 'host.json'), 'utf8'));
  assert.deepEqual(read, record); assert.deepEqual(Object.keys(read), ['version', 'port', 'pid', 'started_at']);
  assert.equal(read.version, 1); assert.equal(read.port, 43123); assert.equal(read.pid, process.pid); assert.ok(!Number.isNaN(Date.parse(read.started_at)));
  if (posix) assert.equal(await mode(join(dir, 'host.json')), 0o600);
  assert.equal(await removeHostFile(dir, { pid: process.pid + 1 }), false, 'another host\'s file is left alone');
  assert.equal(await removeHostFile(dir, { port: 1 }), false);
  assert.equal(await removeHostFile(dir, { port: 43123 }), true);
  assert.deepEqual(await readdir(dir), []);
  await assert.rejects(writeHostFile(dir, { port: 0 }), { code: 'host_file_invalid' });
});

test('the running host writes host.json with its port and removes it on close', async t => {
  const env = await setup(t);
  const record = JSON.parse(await readFile(join(env.stateDir, 'host.json'), 'utf8'));
  assert.equal(record.port, env.address.port);
  if (posix) assert.equal(await mode(env.stateDir), 0o700);
  await env.host.close();
  assert.deepEqual(await readdir(env.stateDir), ['delegate-key']);
});

test('config: delegate options validate, default off, and only readable folders with simple ids can be flagged', () => {
  assert.deepEqual(delegateOptionsFromConfig({}, {}), { enabled: false, approval_timeout_ms: 120000, max_runtime_ms: 600000, max_output_tokens: 2048, max_tool_calls: 4 });
  assert.equal(delegateOptionsFromConfig({ delegate: { enabled: true } }, {}).enabled, true);
  assert.equal(delegateOptionsFromConfig({}, { LAE_DELEGATE_ENABLED: '1' }).enabled, true);
  assert.equal(delegateOptionsFromConfig({}, { LAE_DELEGATE_ENABLED: '0' }).enabled, false);
  assert.throws(() => delegateOptionsFromConfig({}, { LAE_DELEGATE_ENABLED: 'true' }), /LAE_DELEGATE_ENABLED/);
  assert.doesNotThrow(() => validateConfig({ delegate: { enabled: true, max_runtime_ms: 600000, approval_timeout_ms: 120000 }, workspace_roots: [{ id: 'notes', path: '/n', delegate: true }] }));
  for (const bad of [{ delegate: { enabled: 'yes' } }, { delegate: { unknown: 1 } }, { delegate: { max_runtime_ms: 5 } }, { delegate: { max_tool_calls: 9 } }, { workspace_roots: [{ id: 'n', path: '/n', delegate: 'yes' }] }, { workspace_roots: [{ id: 'n', path: '/n', read: false, delegate: true }] }, { workspace_roots: [{ id: 'has space', path: '/n', delegate: true }] }]) assert.throws(() => validateConfig(bad), undefined, JSON.stringify(bad));
});
