// POSIX read trio: recoverable failures, UTF-8-safe windows, search
// reporting/budgets and cancellation.  Runs against a real temporary
// directory; hangs are injected through the policy seam.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { WorkspacePolicy } from '../../host/tools/local/workspace-policy.mjs';
import { createFilesystemTools } from '../../host/tools/local/filesystem.mjs';
import { utf8Window } from '../../host/tools/local/utf8-window.mjs';

const call = (name, arguments_, extra = {}) => ({ id: 'call_posix01', name, arguments: arguments_, ...extra });
const text = output => JSON.parse(output.content[0].text);

async function workspace(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'lae-read-tools-'))); t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, 'docs')); await writeFile(join(root, 'notes.txt'), 'deadline: Friday\nsecond line\n');
  const policy = new WorkspacePolicy([{ id: 'project', path: root, read: true, write: true }]);
  return { root, policy, tools: createFilesystemTools(policy) };
}
async function failedWith(promise, code, label) {
  const output = await promise; assert.equal(output.status, 'failed', label); const body = text(output);
  assert.deepEqual(Object.keys(body).sort(), ['code', 'message'], label); assert.equal(body.code, code, label); return body;
}

test('recoverable read errors are failed results with path-free messages; escapes and write conflicts still throw', async t => {
  const { root, tools } = await workspace(t);
  await writeFile(join(root, 'binary.bin'), Buffer.from([0xff, 0xfe, 0x00, 0x81])); await writeFile(join(root, 'big.txt'), Buffer.alloc(8 * 1024 * 1024 + 1, 0x61));
  for (const [tool, args, code] of [
    ['fs.read_text', { path: 'Notes-typo.txt' }, 'not_found'], ['fs.read_text', { path: '../outside.txt' }, 'invalid_path'], ['fs.read_text', { path: 'docs' }, 'not_regular_file'],
    ['fs.read_text', { path: 'binary.bin' }, 'not_text'], ['fs.read_text', { path: 'big.txt' }, 'file_too_large'], ['fs.list', { path: 'notes.txt' }, 'not_directory'],
    ['fs.list', { path: 'missing' }, 'not_found'], ['fs.search_text', { path: 'missing', query: 'x' }, 'not_found'], ['fs.list', { workspace_id: 'nope' }, 'unknown_workspace']
  ]) {
    const body = await failedWith(tools[tool].execute(call(tool, { workspace_id: 'project', ...args })), code, `${tool} ${args.path}`);
    assert.equal(body.message.includes(root), false); if (args.path) assert.equal(body.message.includes(args.path), false);
  }
  assert.equal(text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' }))).bytes, 29, 'the corrected call succeeds');
  const outside = await realpath(await mkdtemp(join(tmpdir(), 'lae-read-outside-'))); t.after(() => rm(outside, { recursive: true, force: true }));
  await writeFile(join(outside, 'secret.txt'), 'secret'); await symlink(outside, join(root, 'escape'));
  await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'escape/secret.txt' })), error => error.code === 'path_escape');
  await assert.rejects(() => tools['fs.write_new'].execute(call('fs.write_new', { workspace_id: 'project', path: 'notes.txt', content: 'x' })), error => error.code === 'already_exists', 'write contract unchanged');
});

test('POSIX byte windows never split a character at the cut, at an offset, or at the 64 KiB boundary', async t => {
  const { root, tools } = await workspace(t); const sample = 'a漢字😀b€ü\n'; await writeFile(join(root, 'mixed.txt'), sample);
  const read = args => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'mixed.txt', ...args })).then(text);
  const head = await read({ max_bytes: 6 }); assert.equal(head.text, 'a漢'); assert.equal(head.bytes, 4); assert.equal(head.truncated, true);
  const middle = await read({ offset_bytes: 2, max_bytes: 10 }); assert.equal(middle.offset_bytes, 4); assert.equal(middle.text, '字😀b');
  let offset = 0; let joined = ''; for (let guard = 0; guard < 20; guard++) { const part = await read({ offset_bytes: offset, max_bytes: 5 }); joined += part.text; offset = part.offset_bytes + part.bytes; if (!part.truncated) break; }
  assert.equal(joined, sample);
  await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'mixed.txt', offset_bytes: 7, max_bytes: 2 })), 'max_bytes_too_small');
  const big = `a${'€'.repeat(30000)}`; await writeFile(join(root, 'big-utf8.txt'), big);
  const first = text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'big-utf8.txt' })));
  assert.equal(first.text, big.slice(0, 21846)); assert.equal(first.bytes, 65536); assert.equal(first.truncated, true);
  await writeFile(join(root, 'cut.txt'), Buffer.from('ok 漢', 'utf8').subarray(0, 5));
  await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'cut.txt' })), 'not_text');
  assert.deepEqual(utf8Window(Buffer.from('ab'), { atStart: true, atEnd: false }).bytes, Buffer.from('ab'));
});

test('POSIX search reports skipped files and keeps the existing output fields', async t => {
  const { root, tools } = await workspace(t); await writeFile(join(root, 'huge.log'), `deadline ${'x'.repeat(70000)}`); await writeFile(join(root, 'blob.bin'), Buffer.from([0xff, 0xff]));
  const output = await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline' })); const body = text(output);
  for (const key of ['query', 'matches', 'files_seen', 'truncated']) assert.ok(Object.hasOwn(body, key), key);
  assert.deepEqual(body.skipped, { too_large: 1, not_text: 1, unreadable: 0 }); assert.equal(body.truncated, true); assert.equal(output.metadata.truncated, true);
  assert.deepEqual(body.matches.map(match => match.path), ['notes.txt']);
});

test('POSIX search budgets directories and per-directory entries; list streams with a look-ahead', async t => {
  const { root, tools } = await workspace(t);
  for (let index = 0; index < 300; index++) await mkdir(join(root, 'docs', `d${index}`));
  const wide = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', path: 'docs', query: 'deadline' })));
  assert.equal(wide.directories_seen, 256); assert.equal(wide.truncated, true);
  await mkdir(join(root, 'crowded')); for (let index = 0; index < 2100; index++) await writeFile(join(root, 'crowded', `.git-${index}`), '');
  const crowded = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', path: 'crowded', query: 'x' })));
  assert.equal(crowded.files_seen, 0); assert.equal(crowded.truncated, true, 'a directory over the per-directory cap is reported, not read whole');
  const listed = await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'crowded', max_entries: 10 }));
  assert.equal(text(listed).entries.length, 10); assert.equal(listed.metadata.truncated, true);
});

test('POSIX read tools stop promptly on abort, including when a step never settles', async t => {
  const { policy } = await workspace(t); const never = new Promise(() => {});
  const hanging = createFilesystemTools({ resolve: () => never, regularFile: () => never });
  for (const [name, args] of [['fs.list', {}], ['fs.read_text', { path: 'notes.txt' }], ['fs.search_text', { query: 'x' }]]) {
    const controller = new AbortController(); const started = Date.now(); setTimeout(() => controller.abort(), 20);
    await assert.rejects(() => hanging[name].execute(call(name, { workspace_id: 'project', ...args }, { signal: controller.signal })), error => error.code === 'cancelled', name);
    assert.ok(Date.now() - started < 1000, `${name} stopped promptly`);
  }
  // A walk whose per-file step hangs is cancelled and does no more work.
  const { root } = await workspace(t); for (let index = 0; index < 50; index++) await writeFile(join(root, 'docs', `f${index}.txt`), 'deadline');
  const real = new WorkspacePolicy([{ id: 'project', path: root }]); let fileSteps = 0;
  const slow = { resolve: (...args) => real.resolve(...args), regularFile: async (...args) => { fileSteps++; if (fileSteps > 3) return never; return real.regularFile(...args); } };
  const controller = new AbortController(); setTimeout(() => controller.abort(), 30);
  await assert.rejects(() => createFilesystemTools(slow)['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline' }, { signal: controller.signal })), error => error.code === 'cancelled');
  const stoppedAt = fileSteps; await new Promise(resolve => setTimeout(resolve, 40)); assert.equal(fileSteps, stoppedAt, 'no work after abort');
  const aborted = new AbortController(); aborted.abort();
  await assert.rejects(() => createFilesystemTools(policy)['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' }, { signal: aborted.signal })), error => error.code === 'cancelled');
});

test('fs.apply_patch result is a content-free summary: no diff, no old or new file text', async t => {
  const { root, tools } = await workspace(t);
  const { createHash } = await import('node:crypto'); const hash = value => createHash('sha256').update(value).digest('hex');
  const original = 'title\npassword=hunter2\nkeep\n'; await writeFile(join(root, 'notes.env'), original);
  const replacement = 'title\nOPENAI_API_KEY=sk-live0123456789abcdef\nadded line\nkeep\n';
  const output = await tools['fs.apply_patch'].execute(call('fs.apply_patch', { workspace_id: 'project', path: 'notes.env', base_sha256: hash(original), replacement }));
  const raw = output.content[0].text; const body = JSON.parse(raw);
  for (const secret of ['hunter2', 'password', 'sk-live', 'OPENAI_API_KEY', 'added line', 'title', 'keep']) assert.equal(raw.includes(secret), false, `result leaks ${secret}`);
  assert.equal(Object.hasOwn(body, 'diff'), false); assert.equal(Object.hasOwn(body, 'diff_truncated'), false);
  assert.deepEqual(body, { applied: true, path: 'notes.env', base_sha256: hash(original), replacement_sha256: hash(replacement), old_bytes: Buffer.byteLength(original), new_bytes: Buffer.byteLength(replacement), changed: true, lines_removed: 1, lines_added: 2, final_sha256: hash(replacement) });
  // The confirmation preview (what the UI renders and argument-summary masks) is unchanged.
  const preview = await tools['fs.apply_patch'].preview(call('fs.apply_patch', { workspace_id: 'project', path: 'notes.env', base_sha256: hash(replacement), replacement: original }));
  assert.equal(typeof preview.diff, 'string');
});
