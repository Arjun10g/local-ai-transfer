// Windows read-only filesystem trio, exercised on POSIX.
//
// Path-string rules run with path.win32 semantics.  Filesystem behaviour is a
// simulation: FakeWindowsFs models what libuv reports on NTFS (junctions and
// symlinks through isSymbolicLink(), realpath returning on-disk case with long
// names and a stripped verbatim prefix, bigint dev/ino, open following reparse
// points).  These tests prove the policy's decisions given those reports; they
// do NOT prove that a real Windows machine reports them.
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { canonicalWindowsDrivePath, createWindowsReadOnlyFilesystemTools, isSafeWindowsComponent, validateWindowsRelativePath, windowsCaseKey, WindowsWorkspacePolicy } from '../../host/tools/local/windows-filesystem.mjs';
import { createFilesystemTools } from '../../host/tools/local/filesystem.mjs';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';
import { requiresDurableAction } from '../../host/agent/controller.mjs';

const call = (name, arguments_, id = 'call_win01') => ({ id, name, arguments: arguments_ });
const text = output => JSON.parse(output.content[0].text);
const hash = value => createHash('sha256').update(value).digest('hex');
const fsError = code => Object.assign(new Error(code), { code });
// Recoverable outcomes are ordinary failed tool results with a fixed,
// path-free message; invariant violations still reject.
async function failedWith(promise, code, label) {
  const output = await promise; assert.equal(output.status, 'failed', label); const body = text(output);
  assert.equal(body.code, code, label); assert.equal(typeof body.message, 'string'); assert.deepEqual(Object.keys(body).sort(), ['code', 'message'], label);
  return body;
}

class FakeWindowsFs {
  constructor() { this.nextIno = 1000n; this.drives = new Map(); this.hooks = { beforeOpen: null, beforeOpendir: null, afterReaddir: null }; this.opened = []; this.addDrive('C:', 7n); }
  addDrive(letter, dev) { this.drives.set(letter.toUpperCase(), { name: letter.toUpperCase(), type: 'dir', children: new Map(), dev, ino: this.nextIno++, nlink: 1n, mtimeNs: 1n }); }
  #parent(target) { const parts = target.split('\\'); return { dir: this.#find(parts.length === 2 ? `${parts[0]}\\` : parts.slice(0, -1).join('\\'), true).node, name: parts.at(-1) }; }
  #insert(target, node) { const { dir, name } = this.#parent(target); node.name = name; node.dev ??= dir.dev; node.ino ??= this.nextIno++; node.nlink ??= 1n; node.mtimeNs ??= 5n; dir.children.set(windowsCaseKey(name), node); return node; }
  dir(target, extra = {}) { return this.#insert(target, { type: 'dir', children: new Map(), ...extra }); }
  file(target, content, extra = {}) { return this.#insert(target, { type: 'file', content: Buffer.from(content), ...extra }); }
  link(target, linkTarget) { return this.#insert(target, { type: 'link', target: linkTarget }); }
  remove(target) { const { dir, name } = this.#parent(target); dir.children.delete(windowsCaseKey(name)); }
  #child(dir, part) { const key = windowsCaseKey(part); return dir.children.get(key) ?? [...dir.children.values()].find(node => node.shortName && windowsCaseKey(node.shortName) === key); }
  // Resolve like NT name lookup: every reparse point in a non-final position
  // is followed; the final one only when followLast.
  #find(target, followLast) {
    if (typeof target !== 'string' || !/^[A-Za-z]:\\/u.test(target)) throw fsError('ENOENT');
    let queue = target.split('\\').filter(Boolean); let drive = this.drives.get(queue.shift().toUpperCase()); if (!drive) throw fsError('ENOENT');
    let stack = [drive]; let hops = 0;
    while (queue.length) {
      const part = queue.shift(); const current = stack.at(-1); if (current.type !== 'dir') throw fsError('ENOTDIR');
      const child = this.#child(current, part); if (!child) throw fsError('ENOENT');
      if (child.type === 'link' && (queue.length || followLast)) { if (++hops > 32) throw fsError('ELOOP'); const parts = child.target.split('\\').filter(Boolean); stack = [this.drives.get(parts.shift().toUpperCase())]; queue = [...parts, ...queue]; continue; }
      stack.push(child);
    }
    const names = stack.map(node => node.name); return { node: stack.at(-1), path: names.length === 1 ? `${names[0]}\\` : names.join('\\') };
  }
  static stat(node) {
    return { dev: node.dev, ino: node.ino, nlink: node.nlink, size: BigInt(node.type === 'file' ? node.content.length : 0), mtimeNs: node.mtimeNs, isFile: () => node.type === 'file', isDirectory: () => node.type === 'dir', isSymbolicLink: () => node.type === 'link' };
  }
  api() {
    return {
      realpath: async target => this.#find(target, true).path,
      lstat: async target => FakeWindowsFs.stat(this.#find(target, false).node),
      opendir: async target => {
        await this.hooks.beforeOpendir?.(target);
        const { node } = this.#find(target, true); if (node.type !== 'dir') throw fsError('ENOTDIR');
        const names = [...node.children.values()].map(child => child.name); this.directoryReads = (this.directoryReads ?? 0) + 1; let index = 0;
        return { read: async () => { if (index < names.length) return { name: names[index++] }; await this.hooks.afterReaddir?.(target); return null; }, close: async () => { this.closedDirs = (this.closedDirs ?? 0) + 1; } };
      },
      open: async target => {
        await this.hooks.beforeOpen?.(target); const { node } = this.#find(target, true); this.opened.push(target);
        return { stat: async () => FakeWindowsFs.stat(node), read: async (buffer, offset, length, position) => { const bytesRead = Math.max(0, Math.min(length, node.content.length - position)); if (bytesRead) node.content.copy(buffer, offset, position, position + bytesRead); return { bytesRead }; }, close: async () => {} };
      }
    };
  }
}

function workspaceFixture() {
  const fs = new FakeWindowsFs();
  fs.dir('C:\\Users'); fs.dir('C:\\Users\\Op'); fs.dir('C:\\Users\\Op\\Project'); fs.dir('C:\\Users\\Op\\Project\\docs');
  fs.file('C:\\Users\\Op\\Project\\notes.txt', 'deadline: Friday\nsecond line\n');
  fs.file('C:\\Users\\Op\\Project\\docs\\Résumé.txt', 'deadline: Monday ünïcödé\n');
  fs.dir('C:\\Users\\Op\\Secrets'); fs.file('C:\\Users\\Op\\Secrets\\id_rsa', 'PRIVATE KEY deadline');
  const policy = new WindowsWorkspacePolicy([{ id: 'project', path: 'c:\\users\\op\\project', read: true, write: false }], { pathModule: path.win32, fs: fs.api() });
  return { fs, policy, tools: createWindowsReadOnlyFilesystemTools(policy) };
}

// ---------------------------------------------------------------- path strings

const HOSTILE_PATHS = [
  ['..', 'parent traversal'], ['..\\secret', 'backslash traversal'], ['../secret', 'slash traversal'], ['a/../../secret', 'nested traversal'], ['a\\..\\..\\b', 'nested backslash traversal'],
  ['.', 'dot segment'], ['a/./b', 'inner dot segment'], ['...', 'all-dot name (trailing dot)'], ['a//b', 'empty segment'], ['a/', 'trailing separator'],
  ['C:\\Windows\\win.ini', 'drive absolute'], ['C:/Windows/win.ini', 'drive absolute forward'], ['C:foo', 'drive-relative'], ['c:..\\x', 'drive-relative traversal'],
  ['\\Windows', 'rooted backslash'], ['/etc/passwd', 'rooted slash'], ['\\\\server\\share\\x', 'UNC'], ['//server/share/x', 'UNC forward slash'],
  ['\\\\.\\PhysicalDrive0', 'device namespace'], ['\\\\?\\C:\\Windows', 'verbatim prefix'], ['\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\x', 'GLOBALROOT junction-like'], ['\\\\?\\UNC\\server\\share', 'verbatim UNC'], ['\\??\\C:\\x', 'NT object prefix'],
  ['notes.txt:secret', 'alternate data stream'], ['notes.txt::$DATA', 'default data stream'], ['docs:$I30:$INDEX_ALLOCATION', 'directory index stream'],
  ['CON', 'device CON'], ['nul', 'device nul lowercase'], ['NUL.txt', 'device with extension'], ['Aux.tar.gz', 'device with double extension'], ['COM1', 'COM1'], ['lpt9.log', 'LPT9'], ['COM\u00b9', 'COM superscript one'], ['LPT\u00b2.txt', 'LPT superscript two'], ['CONIN$', 'console input'], ['CONOUT$', 'console output'], ['docs/PRN', 'device in nested segment'], ['CON .txt', 'device with space before extension'],
  ['notes.txt.', 'trailing dot'], ['notes.txt ', 'trailing space'], ['docs./notes.txt', 'trailing dot directory'], ['docs /x', 'trailing space directory'],
  ['PROGRA~1', '8.3 short name'], ['docs/NOTES~1.TXT', '8.3 short file name'],
  ['notes\u0000.txt', 'NUL byte'], ['notes\u0007.txt', 'control character'], ['a\u202Etxt.exe', 'bidi override'], ['a\u200Bb', 'zero-width space'], ['a\uD800b', 'lone surrogate'],
  ['a\uFF0F..\uFF0Fsecret', 'fullwidth solidus traversal'], ['a\uFF3C..', 'fullwidth reverse solidus'], ['a\u2215b', 'division slash'], ['a\u29F5b', 'reverse solidus operator'], ['a\u00A5..\u00A5x', 'yen sign (CP932 backslash)'], ['a\u20A9b', 'won sign (CP949 backslash)'], ['\uFF0E\uFF0E/x', 'fullwidth dots'], ['notes\uFF1Astream', 'fullwidth colon'],
  ['*.txt', 'wildcard star'], ['note?.txt', 'wildcard question'], ['a<b', 'DOS wildcard <'], ['a>b', 'DOS wildcard >'], ['a"b', 'DOS wildcard quote'], ['a|b', 'pipe'],
  ['docs\\sub/notes.txt', 'mixed separators'], ['docs/sub\\..', 'mixed separators with traversal'],
  [`${'a'.repeat(256)}`, 'component longer than 255'], [`${'a/'.repeat(512)}a`, 'path longer than 1024'], [Array.from({ length: 65 }, () => 'd').join('/'), 'more than 64 components'],
  ['', 'empty when not allowed'], [null, 'non-string'], [42, 'number']
];

test(`attack table: ${HOSTILE_PATHS.length} hostile workspace-relative paths are rejected with path.win32 semantics`, () => {
  assert.ok(HOSTILE_PATHS.length >= 40);
  for (const [value, label] of HOSTILE_PATHS) assert.throws(() => validateWindowsRelativePath(value, { pathModule: path.win32 }), error => error.code === 'invalid_path', label);
});

test('benign relative paths normalise to one model-facing spelling', () => {
  for (const [value, relative] of [['notes.txt', 'notes.txt'], ['docs\\Résumé.txt', 'docs/Résumé.txt'], ['docs/Résumé.txt', 'docs/Résumé.txt'], ['Report 2026 (final).md', 'Report 2026 (final).md'], ['日本語/ファイル.txt', '日本語/ファイル.txt'], ['.env.example', '.env.example'], ['CONSOLE.txt', 'CONSOLE.txt'], ['COM10', 'COM10'], ['tilde~x.txt', 'tilde~x.txt']]) {
    assert.equal(validateWindowsRelativePath(value, { pathModule: path.win32 }).relative, relative, value);
  }
  assert.equal(validateWindowsRelativePath('', { allowEmpty: true, pathModule: path.win32 }).relative, '');
  assert.equal(isSafeWindowsComponent('NUL'), false); assert.equal(isSafeWindowsComponent('notes.txt'), true);
});

test('workspace roots must be local drive paths; realpath outputs naming devices, shares or volumes are refused', () => {
  assert.equal(canonicalWindowsDrivePath('c:/Users/Op/Project/'), 'C:\\Users\\Op\\Project');
  for (const value of ['\\\\server\\share\\ws', '//server/share/ws', '\\\\?\\C:\\ws', '\\\\?\\UNC\\server\\share', '\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\ws', '\\\\.\\C:\\ws', 'C:ws', 'ws', '/ws', 'C:\\ws\\..\\Windows', 'C:\\ws:stream', 'C:\\ws.', 'C:\\ws \\x', 'C:\\CON', 'C:\\', 'C:\\ws\0', '', 7]) {
    assert.throws(() => canonicalWindowsDrivePath(value), error => error.code === 'invalid_workspace', String(value));
  }
  assert.equal(canonicalWindowsDrivePath('C:\\', { allowDriveRoot: true }), 'C:\\');
});

test('case key folds 1:1 like the NTFS upcase table and never expands', () => {
  assert.equal(windowsCaseKey('Résumé.TXT'), windowsCaseKey('rÉsumÉ.txt'));
  assert.notEqual(windowsCaseKey('straße'), windowsCaseKey('STRASSE'));
});

// ------------------------------------------------------------ simulated NTFS

test('read-only trio lists, reads and searches inside the workspace with unchanged output contracts', async () => {
  const { tools } = workspaceFixture();
  const listed = text(await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project' })));
  assert.deepEqual(listed.entries.map(entry => [entry.name, entry.type]).sort(), [['docs', 'directory'], ['notes.txt', 'file']]);
  const read = text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'NOTES.TXT' })));
  assert.equal(read.text, 'deadline: Friday\nsecond line\n'); assert.equal(read.sha256, hash('deadline: Friday\nsecond line\n')); assert.equal(read.path, 'NOTES.TXT');
  const unicode = text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'docs\\résumé.txt', offset_bytes: 0, max_bytes: 64 })));
  assert.equal(unicode.text, 'deadline: Monday ünïcödé\n');
  const searched = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline' })));
  assert.deepEqual(searched.matches.map(match => match.path).sort(), ['docs/Résumé.txt', 'notes.txt']);
  assert.equal(searched.matches.some(match => match.text.includes('PRIVATE')), false);
});

test('junction or symlink at any component is rejected even when it points inside the workspace', async () => {
  const { fs, tools } = workspaceFixture();
  fs.link('C:\\Users\\Op\\Project\\escape', 'C:\\Users\\Op\\Secrets'); fs.link('C:\\Users\\Op\\Project\\inner', 'C:\\Users\\Op\\Project\\docs'); fs.link('C:\\Users\\Op\\Project\\key.txt', 'C:\\Users\\Op\\Secrets\\id_rsa');
  for (const target of ['escape/id_rsa', 'inner/Résumé.txt', 'key.txt']) await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: target })), 'reparse_point_rejected', target);
  await failedWith(tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'escape' })), 'reparse_point_rejected');
  const listed = text(await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project' })));
  assert.equal(listed.entries.find(entry => entry.name === 'escape').type, 'other');
  const searched = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'PRIVATE' })));
  assert.deepEqual(searched.matches, [], 'search never follows a reparse point');
});

test('a junction-reached workspace root is canonicalised; a root that resolves to a share, volume GUID or drive root is unavailable', async () => {
  const { fs } = workspaceFixture(); fs.dir('C:\\Links'); fs.link('C:\\Links\\proj', 'C:\\Users\\Op\\Project');
  const viaLink = createWindowsReadOnlyFilesystemTools(new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Links\\proj' }], { fs: fs.api() }));
  assert.equal(text(await viaLink['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' }))).bytes, 29);
  for (const resolved of ['\\\\fileserver\\share\\Project', '\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\Project', 'C:\\']) {
    const api = { ...fs.api(), realpath: async () => resolved };
    const tools = createWindowsReadOnlyFilesystemTools(new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: api }));
    await assert.rejects(() => tools['fs.list'].execute(call('fs.list', { workspace_id: 'project' })), error => error.code === 'workspace_unavailable', resolved);
  }
});

test('8.3 short names and other realpath aliases are rejected after resolution as well as by string rules', async () => {
  const { fs, policy } = workspaceFixture(); fs.dir('C:\\Users\\Op\\Project\\Long Directory Name', { shortName: 'LONGDI~1' }); fs.file('C:\\Users\\Op\\Project\\Long Directory Name\\a.txt', 'x');
  await assert.rejects(() => policy.resolve('project', 'LONGDI~1/a.txt', { expect: 'file' }), error => error.code === 'invalid_path');
  // Even if a short name evaded the string rule, realpath's long name would
  // not match the requested component.
  fs.dir('C:\\Users\\Op\\Project\\Other Long Name', { shortName: 'OTHERL' }); fs.file('C:\\Users\\Op\\Project\\Other Long Name\\b.txt', 'y');
  await assert.rejects(() => policy.resolve('project', 'OTHERL/b.txt', { expect: 'file' }), error => error.code === 'path_escape');
});

test('a mounted volume that lstat reports as a plain directory is rejected by the volume-serial check', async () => {
  const { fs, tools } = workspaceFixture(); fs.addDrive('D:', 99n); fs.dir('D:\\Data'); fs.file('D:\\Data\\x.txt', 'other volume');
  fs.dir('C:\\Users\\Op\\Project\\mnt', { dev: 99n });
  await assert.rejects(() => tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'mnt' })), error => error.code === 'path_escape');
});

test('hard-linked files and files without a stable identity are not read', async () => {
  const { fs, tools } = workspaceFixture(); fs.file('C:\\Users\\Op\\Project\\linked.txt', 'alias', { nlink: 2n }); fs.file('C:\\Users\\Op\\Project\\fat.txt', 'no id', { ino: 0n });
  await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'linked.txt' })), 'hard_link_rejected');
  await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'fat.txt' })), error => error.code === 'path_changed');
});

test('a swap to a junction between the lstat walk and open is caught by the handle identity', async () => {
  const { fs, tools } = workspaceFixture();
  fs.hooks.beforeOpen = async () => { fs.hooks.beforeOpen = null; fs.remove('C:\\Users\\Op\\Project\\docs'); fs.link('C:\\Users\\Op\\Project\\docs', 'C:\\Users\\Op\\Secrets'); fs.file('C:\\Users\\Op\\Secrets\\Résumé.txt', 'PRIVATE swapped'); };
  await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'docs/Résumé.txt' })), error => error.code === 'path_changed');
});

test('a final component swapped and restored around open is caught only by the handle identity', async () => {
  const { fs } = workspaceFixture(); const original = fs.api();
  // Same size and mtime as notes.txt, so only dev+ino can tell them apart.
  fs.file('C:\\Users\\Op\\Secrets\\twin.txt', `${'PRIVATE'.padEnd(28, '!')}\n`);
  // The chain re-check after open sees the genuine file again, so the open
  // handle's dev+ino is the only evidence of what was actually read.
  const api = { ...original, open: async target => {
    if (!target.endsWith('notes.txt')) return original.open(target);
    const genuine = await original.lstat(target); fs.remove(target); fs.link(target, 'C:\\Users\\Op\\Secrets\\twin.txt');
    try { return await original.open(target); } finally { fs.remove(target); fs.file(target, 'deadline: Friday\nsecond line\n', { ino: genuine.ino, mtimeNs: genuine.mtimeNs }); }
  } };
  const policy = new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: api });
  const tools = createWindowsReadOnlyFilesystemTools(policy);
  await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' })), error => error.code === 'path_changed');
  const searched = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'PRIVATE' })));
  assert.deepEqual(searched.matches, []);
});

test('an ancestor swapped and restored around open is caught by the post-open chain check', async () => {
  const { fs, tools } = workspaceFixture(); const original = fs.api(); let lstats = 0;
  // The file reached through open is genuine, but the docs directory the
  // walk verified is replaced by a different directory object afterwards.
  const api = { ...original, lstat: async target => { const info = await original.lstat(target); if (target.endsWith('\\docs') && ++lstats === 2) return { ...info, ino: info.ino + 1n }; return info; } };
  const policy = new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: api });
  await assert.rejects(() => createWindowsReadOnlyFilesystemTools(policy)['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'docs/Résumé.txt' })), error => error.code === 'path_changed');
});

test('content changing between lstat and open (size/mtime) is refused', async () => {
  const { fs, tools } = workspaceFixture();
  fs.hooks.beforeOpen = async () => { fs.hooks.beforeOpen = null; const node = fs.api(); const before = await node.lstat('C:\\Users\\Op\\Project\\notes.txt'); assert.ok(before); fs.file('C:\\Users\\Op\\Project\\notes.txt', 'replaced in place', { ino: before.ino }); };
  await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' })), error => error.code === 'path_changed');
});

test('directory swapped during readdir is refused for list and skipped by search', async () => {
  const { fs, tools } = workspaceFixture();
  fs.hooks.afterReaddir = async target => { if (!target.endsWith('docs')) return; fs.hooks.afterReaddir = null; fs.remove('C:\\Users\\Op\\Project\\docs'); fs.link('C:\\Users\\Op\\Project\\docs', 'C:\\Users\\Op\\Secrets'); };
  await assert.rejects(() => tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'docs' })), error => error.code === 'path_changed');
  const second = workspaceFixture();
  second.fs.hooks.afterReaddir = async target => { if (!target.endsWith('docs')) return; second.fs.hooks.afterReaddir = null; second.fs.remove('C:\\Users\\Op\\Project\\docs'); second.fs.link('C:\\Users\\Op\\Project\\docs', 'C:\\Users\\Op\\Secrets'); };
  const searched = text(await second.tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline' })));
  assert.deepEqual(searched.matches.map(match => match.path), ['notes.txt'], 'the swapped directory is skipped, never searched');
});

test('list and search hide names the policy could never resolve and keep every bound', async () => {
  const { fs, tools } = workspaceFixture();
  fs.file('C:\\Users\\Op\\Project\\NUL', 'device-named file created via verbatim path'); fs.file('C:\\Users\\Op\\Project\\trailing.', 'x'); fs.file('C:\\Users\\Op\\Project\\bidi\u202Etxt.exe', 'x'); fs.dir('C:\\Users\\Op\\Project\\.git'); fs.file('C:\\Users\\Op\\Project\\.git\\config', 'deadline');
  for (let index = 0; index < 30; index++) fs.file(`C:\\Users\\Op\\Project\\docs\\f${index}.txt`, 'deadline');
  const listed = text(await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project' })));
  assert.deepEqual(listed.entries.map(entry => entry.name).sort(), ['.git', 'docs', 'notes.txt']);
  const limited = await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'docs', max_entries: 5 })); assert.equal(text(limited).entries.length, 5); assert.equal(limited.metadata.truncated, true);
  const searched = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline', max_files: 10 })));
  assert.equal(searched.files_seen, 10); assert.equal(searched.truncated, true); assert.equal(searched.matches.some(match => match.path.startsWith('.git')), false);
  const shallow = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline', max_depth: 0 })));
  assert.deepEqual(shallow.matches.map(match => match.path), ['notes.txt']);
  fs.file('C:\\Users\\Op\\Project\\big.txt', 'x'.repeat(8 * 1024 * 1024 + 1));
  await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'big.txt' })), 'file_too_large');
  const partial = text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt', offset_bytes: 10, max_bytes: 6 })));
  assert.equal(partial.text, 'Friday'); assert.equal(partial.truncated, true);
});

test('unknown, unreadable and invalid-root workspaces fail closed', async () => {
  const fs = new FakeWindowsFs(); fs.dir('C:\\Work');
  const policy = new WindowsWorkspacePolicy([{ id: 'hidden', path: 'C:\\Work', read: false }, { id: 'share', path: '\\\\server\\share' }], { fs: fs.api() });
  const tools = createWindowsReadOnlyFilesystemTools(policy);
  await failedWith(tools['fs.list'].execute(call('fs.list', { workspace_id: 'nope' })), 'unknown_workspace');
  await failedWith(tools['fs.list'].execute(call('fs.list', { workspace_id: 'hidden' })), 'read_not_allowed');
  await assert.rejects(() => tools['fs.list'].execute(call('fs.list', { workspace_id: 'share' })), error => error.code === 'workspace_unavailable');
  assert.equal(policy.isUsable('share'), false);
  assert.throws(() => createWindowsReadOnlyFilesystemTools({ resolve() {} }), /WindowsWorkspacePolicy is required/u);
});

// ------------------------------------------------- recoverable vs. hard

test('a wrong path is an ordinary failed result the model can recover from; escapes still refuse the turn', async () => {
  const { fs, tools } = workspaceFixture(); fs.file('C:\\Users\\Op\\Project\\binary.bin', Buffer.from([0xff, 0xfe, 0x00, 0x81]));
  for (const [args, code, tool = 'fs.read_text'] of [[{ path: 'Notes-typo.txt' }, 'not_found'], [{ path: 'docs/missing/x.txt' }, 'not_found'], [{ path: '../Secrets/id_rsa' }, 'invalid_path'], [{ path: 'C:\\Users\\Op\\Secrets\\id_rsa' }, 'invalid_path'], [{ path: 'docs' }, 'not_regular_file'], [{ path: 'binary.bin' }, 'not_text'], [{ path: 'notes.txt' }, 'not_directory', 'fs.list'], [{ path: 'nope' }, 'not_found', 'fs.search_text']]) {
    const body = await failedWith(tools[tool].execute(call(tool, { workspace_id: 'project', ...(tool === 'fs.search_text' ? { query: 'x' } : {}), ...args })), code, `${tool} ${args.path}`);
    assert.equal(body.message.includes(args.path), false, 'message never echoes the path'); assert.equal(/[A-Za-z]:\\|Secrets|Users/u.test(body.message), false);
  }
  assert.equal(text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' }))).bytes, 29, 'the next call with the right path succeeds');
  fs.addDrive('D:', 99n); fs.dir('C:\\Users\\Op\\Project\\mnt', { dev: 99n });
  await assert.rejects(() => tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'mnt' })), error => error.code === 'path_escape');
  await assert.rejects(() => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 7 })), error => error.code === 'invalid_tool_arguments');
});

// ----------------------------------------------------------- UTF-8 windows

test('byte windows never split a character: CJK and emoji at the cut, at an offset, and at the 64 KiB boundary', async () => {
  const { fs, tools } = workspaceFixture();
  const sample = 'a漢字😀b€ü\n'; fs.file('C:\\Users\\Op\\Project\\mixed.txt', sample);
  const read = args => tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'mixed.txt', ...args })).then(text);
  // 'a'(1) '漢'(3) '字'(3) '😀'(4): a 6-byte window ends inside 字.
  const head = await read({ max_bytes: 6 }); assert.equal(head.text, 'a漢'); assert.equal(head.bytes, 4); assert.equal(head.truncated, true);
  // An offset inside 漢 skips its continuation bytes and reports the real start.
  const middle = await read({ offset_bytes: 2, max_bytes: 10 }); assert.equal(middle.offset_bytes, 4); assert.equal(middle.text, '字😀b'); assert.equal(middle.bytes, 8);
  // Chained reads from offset_bytes + bytes reassemble the file exactly.
  let offset = 0; let joined = ''; for (let guard = 0; guard < 20; guard++) { const part = await read({ offset_bytes: offset, max_bytes: 5 }); joined += part.text; offset = part.offset_bytes + part.bytes; if (!part.truncated) break; }
  assert.equal(joined, sample);
  await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'mixed.txt', offset_bytes: 7, max_bytes: 2 })), 'max_bytes_too_small');
  const big = `a${'€'.repeat(30000)}`; fs.file('C:\\Users\\Op\\Project\\big-utf8.txt', big); // byte 65536 falls inside a €
  const first = text(await tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'big-utf8.txt' })));
  assert.equal(first.text, big.slice(0, 1 + 21845)); assert.equal(first.bytes, 65536); assert.equal(first.truncated, true);
  fs.file('C:\\Users\\Op\\Project\\cut.txt', Buffer.from('ok 漢', 'utf8').subarray(0, 5));
  await failedWith(tools['fs.read_text'].execute(call('fs.read_text', { workspace_id: 'project', path: 'cut.txt' })), 'not_text');
});

// ---------------------------------------------------------- search budgets

test('search reports skipped large files instead of silently missing them', async () => {
  const { fs, tools } = workspaceFixture(); fs.file('C:\\Users\\Op\\Project\\huge.log', `deadline ${'x'.repeat(70000)}`); fs.file('C:\\Users\\Op\\Project\\blob.bin', Buffer.from([0xff, 0xff]));
  const output = await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline' })); const body = text(output);
  assert.deepEqual(body.skipped, { too_large: 1, not_text: 1, unreadable: 0 }); assert.equal(body.truncated, true); assert.equal(output.metadata.truncated, true);
  assert.equal(body.matches.some(match => match.path === 'huge.log'), false);
});

test('directories and entries are budgeted and directories are streamed, not loaded whole', async () => {
  const wide = workspaceFixture(); for (let index = 0; index < 300; index++) wide.fs.dir(`C:\\Users\\Op\\Project\\d${index}`);
  const body = text(await wide.tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', query: 'deadline' })));
  assert.equal(body.directories_seen, 256); assert.equal(body.truncated, true);
  const huge = workspaceFixture(); for (let index = 0; index < 2500; index++) huge.fs.file(`C:\\Users\\Op\\Project\\docs\\e${index}.bin`, '', { size: 1 });
  let reads = 0; const api = huge.fs.api(); const counted = { ...api, opendir: async target => { const dir = await api.opendir(target); return { read: async () => { reads++; return dir.read(); }, close: () => dir.close() }; } };
  const tools = createWindowsReadOnlyFilesystemTools(new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: counted }));
  const listed = await tools['fs.list'].execute(call('fs.list', { workspace_id: 'project', path: 'docs', max_entries: 10 }));
  assert.equal(text(listed).entries.length, 10); assert.equal(listed.metadata.truncated, true); assert.equal(reads, 11, 'list reads max_entries + 1 look-ahead');
  reads = 0; const searched = text(await tools['fs.search_text'].execute(call('fs.search_text', { workspace_id: 'project', path: 'docs', query: 'zz', max_files: 200 })));
  assert.ok(reads <= 2001, `per-directory entry cap (read ${reads})`); assert.equal(searched.truncated, true);
  assert.equal(huge.fs.closedDirs >= 2, true, 'every directory stream is closed');
});

// ------------------------------------------------------------ cancellation

test('an aborted call stops promptly even when a filesystem primitive never settles, and late handles are closed', async () => {
  const { fs } = workspaceFixture(); const api = fs.api(); let lstatCalls = 0; let hang = false; const never = new Promise(() => {});
  const hanging = { ...api, lstat: async target => { lstatCalls++; if (hang) return never; return api.lstat(target); } };
  const tools = createWindowsReadOnlyFilesystemTools(new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: hanging }));
  for (const name of ['fs.search_text', 'fs.read_text', 'fs.list']) {
    const controller = new AbortController(); hang = true; lstatCalls = 0;
    const started = Date.now(); setTimeout(() => controller.abort(), 20);
    await assert.rejects(() => tools[name].execute({ ...call(name, { workspace_id: 'project', path: name === 'fs.read_text' ? 'notes.txt' : '', ...(name === 'fs.search_text' ? { query: 'deadline' } : {}) }), signal: controller.signal }), error => error.code === 'cancelled', name);
    assert.ok(Date.now() - started < 1000, `${name} stopped promptly`); const after = lstatCalls; await new Promise(resolve => setTimeout(resolve, 30)); assert.equal(lstatCalls, after, `${name} does no work after abort`);
    hang = false;
  }
  // A search aborted mid-walk is not swallowed by the per-entry skip logic.
  let entries = 0; const slow = { ...api, lstat: async target => { if (++entries > 3) await new Promise(resolve => setTimeout(resolve, 5)); return api.lstat(target); } };
  for (let index = 0; index < 200; index++) fs.file(`C:\\Users\\Op\\Project\\docs\\s${index}.txt`, 'deadline');
  const slowTools = createWindowsReadOnlyFilesystemTools(new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: slow }));
  const controller = new AbortController(); setTimeout(() => controller.abort(), 30);
  await assert.rejects(() => slowTools['fs.search_text'].execute({ ...call('fs.search_text', { workspace_id: 'project', query: 'deadline' }), signal: controller.signal }), error => error.code === 'cancelled');
  const stoppedAt = entries; await new Promise(resolve => setTimeout(resolve, 40)); assert.ok(entries <= stoppedAt + 1, 'walk stopped');
  // open settles after the abort: the handle must still be closed.
  let closed = false; let finishOpen; const lateOpen = { ...api, open: () => new Promise(resolve => { finishOpen = async () => { const handle = await api.open('C:\\Users\\Op\\Project\\notes.txt'); resolve({ ...handle, close: async () => { closed = true; } }); }; }) };
  const lateTools = createWindowsReadOnlyFilesystemTools(new WindowsWorkspacePolicy([{ id: 'project', path: 'C:\\Users\\Op\\Project' }], { fs: lateOpen }));
  const abortOpen = new AbortController(); const pending = lateTools['fs.read_text'].execute({ ...call('fs.read_text', { workspace_id: 'project', path: 'notes.txt' }), signal: abortOpen.signal });
  await new Promise(resolve => setTimeout(resolve, 10)); abortOpen.abort(); await assert.rejects(() => pending, error => error.code === 'cancelled');
  await finishOpen(); await new Promise(resolve => setImmediate(resolve)); assert.equal(closed, true);
});

// ------------------------------------------------------------- registration

test('win32 registry serves only the read-only trio, only for explicitly configured local-drive workspaces', () => {
  assert.deepEqual(Object.keys(createLocalToolRegistry({ platform: 'win32' })), ['time.now', 'system.get_info'], 'no default workspace');
  const registry = createLocalToolRegistry({ platform: 'win32', workspaces: [{ id: 'project', path: 'C:\\Users\\Op\\Project', read: true, write: true }] });
  assert.deepEqual(Object.keys(registry), ['time.now', 'system.get_info', 'fs.list', 'fs.read_text', 'fs.search_text']);
  for (const name of ['fs.list', 'fs.read_text', 'fs.search_text']) { assert.deepEqual({ ...registry.capabilitySnapshot.tools[name] }, { advertised: true, reason: 'workspace_configured', profile: 'windows_read_only' }); assert.equal(registry[name].risk_tier, 'T0'); assert.equal(requiresDurableAction(registry[name]), false, `${name} needs no action journal`); }
  for (const name of ['fs.write_new', 'fs.apply_patch']) { assert.equal(registry.capabilitySnapshot.tools[name].status, 'NOT_READY'); assert.equal(registry.capabilitySnapshot.tools[name].reason, 'platform_path_safety_unavailable'); }
  const invalidRoot = createLocalToolRegistry({ platform: 'win32', workspaces: [{ id: 'share', path: '\\\\server\\share' }, { id: 'posix', path: '/home/op' }] });
  assert.deepEqual(Object.keys(invalidRoot), ['time.now', 'system.get_info']); assert.equal(invalidRoot.capabilitySnapshot.tools['fs.read_text'].reason, 'workspace_path_invalid');
  const unreadable = createLocalToolRegistry({ platform: 'win32', workspaces: [{ id: 'project', path: 'C:\\Users\\Op\\Project', read: false, write: true }] });
  assert.deepEqual(Object.keys(unreadable), ['time.now', 'system.get_info']); assert.equal(unreadable.capabilitySnapshot.tools['fs.list'].reason, 'workspace_unconfigured');
});

test('the POSIX O_NOFOLLOW implementation still refuses on win32 for every tool', async () => {
  const tools = createFilesystemTools({}, { platform: 'win32' });
  for (const name of ['fs.list', 'fs.read_text', 'fs.search_text', 'fs.write_new']) await assert.rejects(() => tools[name].execute(call(name, { workspace_id: 'project', path: 'x', query: 'x', content: 'x' })), error => error.code === 'platform_path_safety_unavailable', name);
});
