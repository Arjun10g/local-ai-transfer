import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, stat, symlink, unlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { WorkspacePolicy, validateRelativePath } from '../../host/tools/local/workspace-policy.mjs';
import { createFilesystemTools } from '../../host/tools/local/filesystem.mjs';
import { createSystemTools } from '../../host/tools/local/system-tools.mjs';
import { timeNowTool } from '../../host/tools/time-now.mjs';
import { mergeConfig } from '../../host/agent/config.mjs';

const call = (name, arguments_, id = 'call_test01') => ({ id, name, arguments: arguments_ });
const text = result => JSON.parse(result.content[0].text);
const hash = value => createHash('sha256').update(value).digest('hex');

async function fixture() { const root = await mkdtemp(join(tmpdir(), 'lae-tools-')); await mkdir(join(root, 'nested')); await writeFile(join(root, 'notes.txt'), 'deadline: Friday\nsecond line\n', 'utf8'); await writeFile(join(root, 'nested', 'other.txt'), 'deadline: Monday\n', 'utf8'); return root; }

test('workspace policy rejects traversal, ADS, reserved names, absolute paths, and symlink escape', async () => {
  for (const value of ['../secret', 'a/../../secret', 'notes.txt:stream', 'CON.txt', 'NUL', '/tmp/secret', 'C:\\secret', '\\\\server\\share']) assert.throws(() => validateRelativePath(value), error => ['invalid_path'].includes(error.code));
  const root = await fixture(); const outside = await mkdtemp(join(tmpdir(), 'lae-outside-')); await writeFile(join(outside, 'secret.txt'), 'secret');
  try { await symlink(outside, join(root, 'link')); } catch { return; }
  const policy = new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]); await assert.rejects(() => policy.regularFile('project', 'link/secret.txt'), error => error.code === 'path_escape');
});

test('workspace config supports explicit read/write policy', () => {
  const config = mergeConfig({ workspace_roots: [{ id: 'project', path: '/tmp/project', read: true, write: false }] }); assert.equal(config.workspace_roots[0].write, false);
  assert.throws(() => mergeConfig({ workspace_roots: [{ id: 'project', path: '/tmp/project', extra: true }] }), /workspace_roots invalid/);
  assert.deepEqual(mergeConfig({ applications: { teams: { executable_id: 'teams', executable: 'ms-teams.exe', args: [] } } }).applications.teams.args, []);
  for (const applications of [{ bad: { executable: 'x', args: 'no' } }, { bad: { executable: 'x\nattack', args: [] } }, { bad: { executable: 'x', args: ['ok\nattack'] } }, { '../bad': { executable: 'x', args: [] } }]) assert.throws(() => mergeConfig({ applications }), /applications/);
});

test('filesystem read/list/search are bounded and literal', async () => {
  const root = await fixture(); const tools = createFilesystemTools(new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]));
  const listed = text(await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: '' }))); assert.equal(listed.entry_count, 2); assert.ok(listed.entries.some(entry => entry.name === 'notes.txt'));
  const read = text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt', max_bytes: 8 }))); assert.equal(read.text, 'deadline'); assert.equal(read.truncated, true); assert.equal(read.hash_scope, 'returned_bytes');
  const searched = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', path: '', query: 'deadline', max_files: 10 }))); assert.equal(searched.matches.length, 2); assert.equal(searched.matches[0].line, 1); const nested = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', path: 'nested', query: 'deadline' }))); assert.equal(nested.matches[0].path, 'nested/other.txt');
});

test('write_new is create-only and apply_patch requires current base hash', async () => {
  const root = await fixture(); const tools = createFilesystemTools(new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]));
  const created = text(await tools['fs.write_new'].execute(call('fs.write_new', { workspace_id: 'project', path: 'new.txt', content: 'new content' }))); assert.equal(created.created, true); assert.equal((await stat(join(root, 'new.txt'))).mode & 0o777, 0o600); await assert.rejects(() => tools['fs.write_new'].execute(call('fs.write_new', { workspace_id: 'project', path: 'new.txt', content: 'overwrite' })), error => error.code === 'already_exists');
  const original = Buffer.from(await readFile(join(root, 'notes.txt'))); const replacement = 'updated deadline\n'; const patchCall = call('fs.apply_patch', { workspace_id: 'project', path: 'notes.txt', base_sha256: hash(original), replacement }); const preview = await tools['fs.apply_patch'].preview(patchCall); assert.equal(preview.changed, true); const applied = text(await tools['fs.apply_patch'].execute(patchCall)); assert.equal(applied.applied, true); assert.equal(await readFile(join(root, 'notes.txt'), 'utf8'), replacement);
  await assert.rejects(() => tools['fs.apply_patch'].execute(call('fs.apply_patch', { workspace_id: 'project', path: 'notes.txt', base_sha256: hash(original), replacement: 'stale' })), error => error.code === 'base_hash_mismatch');
});

test('system information is bounded; clipboard is typed offline on non-Windows', async () => {
  const tools = createSystemTools({ platform: 'linux' }); const info = text(await tools['system.get_info'].execute(call('system.get_info', {}))); assert.equal(info.platform, 'linux'); assert.ok(Number.isInteger(info.cpus)); assert.equal(Object.prototype.hasOwnProperty.call(info, 'env'), false);
  const clipboard = text(await tools['clipboard.read'].execute(call('clipboard.read', {}))); assert.equal(clipboard.code, 'platform_unsupported'); const write = text(await tools['clipboard.write'].execute(call('clipboard.write', { text: 'sensitive' }))); assert.equal(write.code, 'platform_unsupported');
});

test('app/browser tools use allowlisted executable+argv and reject unsafe URLs', async () => {
  const tools = createSystemTools({ platform: 'linux', applications: { probe: { executable_id: 'probe', executable: process.execPath, args: ['-e', ''] } }, browserExecutable: process.execPath });
  const opened = text(await tools['app.open'].execute(call('app.open', { app_id: 'probe' }))); assert.equal(opened.opened, true); const browserCall = call('browser.open_url', { url: 'https://example.com/docs' }); const preview = await tools['browser.open_url'].preview(browserCall); assert.deepEqual(preview, { destination: 'https://example.com/docs', provider: 'disabled', data_egress: 'external_navigation' }); const browserDisabled = text(await tools['browser.open_url'].execute(browserCall)); assert.equal(browserDisabled.code, 'provider_disabled'); const enabled = createSystemTools({ platform: 'linux', browserExecutable: process.execPath, networkProvider: 'browser_open' }); const browser = text(await enabled['browser.open_url'].execute(call('browser.open_url', { url: 'https://example.com/docs' }))); assert.equal(browser.host, 'example.com');
  await assert.rejects(() => tools['browser.open_url'].execute(call('browser.open_url', { url: 'file:///etc/passwd' })), /invalid_url|unsafe_url/); await assert.rejects(() => tools['browser.open_url'].execute(call('browser.open_url', { url: 'https://127.0.0.1/' })), /private_url/);
});

test('every local tool rejects unknown fields and wrong argument types', async () => {
  const root = await fixture(); const fs = createFilesystemTools(new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }])); const system = createSystemTools({ platform: 'linux' });
  const tools = { ...fs, ...system, 'time.now': { execute: async value => timeNowTool({ id: value.id, arguments: value.arguments }) } }; for (const [name, tool] of Object.entries(tools)) await assert.rejects(() => tool.execute(call(name, { unknown: true })), error => error.code === 'invalid_tool_arguments');
  await assert.rejects(() => fs['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt', max_bytes: 'large' })), error => error.code === 'invalid_tool_arguments');
});

test('filesystem patch rechecks canonical target before replacement', async () => {
  const root = await fixture(); const realPolicy = new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]); const original = realPolicy.regularFile.bind(realPolicy); let calls = 0; realPolicy.regularFile = async (...args) => { const file = await original(...args); if (++calls === 4) throw Object.assign(new Error('reparse target changed'), { code: 'path_changed' }); return file; };
  const tools = createFilesystemTools(realPolicy); const old = Buffer.from(await readFile(join(root, 'notes.txt'))); const patch = call('fs.apply_patch', { workspace_id: 'project', path: 'notes.txt', base_sha256: hash(old), replacement: 'should not apply' }); await assert.rejects(() => tools['fs.apply_patch'].execute(patch), error => error.code === 'path_changed'); assert.equal(await readFile(join(root, 'notes.txt'), 'utf8'), 'deadline: Friday\nsecond line\n');
});

test('filesystem safe-open rejects a final-component symlink swap', async () => {
  const root = await fixture(); const outside = await mkdtemp(join(tmpdir(), 'lae-race-')); const target = join(root, 'race.txt'); const escaped = join(outside, 'secret.txt'); await writeFile(target, 'inside only', 'utf8'); await writeFile(escaped, 'must not be read', 'utf8'); const originalStat = await stat(target); let first = true;
  const racePolicy = { regularFile: async () => { if (first) { first = false; await unlink(target); await symlink(escaped, target); } return { canonical: target, path: 'race.txt', stat: originalStat }; } };
  const tools = createFilesystemTools(racePolicy); await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'race.txt' })), error => error.code === 'path_changed'); await unlink(target);
});

test('filesystem operations fail closed when Windows handle safety is unavailable', async () => {
  const root = await fixture(); const policy = new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]); const tools = createFilesystemTools(policy, { platform: 'win32' });
  await assert.rejects(() => tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: '' })), error => error.code === 'platform_path_safety_unavailable');
  await assert.rejects(() => tools['fs.write_new'].execute(call('fs.write_new', { workspace_id: 'project', path: 'blocked.txt', content: 'blocked' })), error => error.code === 'platform_path_safety_unavailable');
});

test('confirmation UI discloses browser destination and carries both binding fields', async () => {
  const source = await readFile(new URL('../../ui/app.js', import.meta.url), 'utf8'); assert.match(source, /External destination \(network egress\)/); assert.match(source, /previews\.set\(ev\.data\.call\.id,destination\)/); assert.match(source, /request_id:ev\.request_id,call_id:call\.id/);
});
