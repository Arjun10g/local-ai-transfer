// Windows read-only filesystem trio (fs.list, fs.read_text, fs.search_text).
//
// Why this is separate from filesystem.mjs: the POSIX implementation depends
// on O_NOFOLLOW, which Node does not define on Windows, so it stays
// fail-closed there.  Win32 has no Node-reachable openat/no-follow open, so
// this module substitutes a check-then-verify protocol built only from Node
// built-ins:
//
//   1. strict Win32 path-string validation of the model-supplied relative
//      path (no traversal, ADS, device names, UNC/device/verbatim prefixes,
//      trailing dot/space, 8.3 short names, confusable separators, ...);
//   2. the workspace root is re-canonicalised with libuv's realpath
//      (GetFinalPathNameByHandleW, as fs.realpathSync.native) on every call and
//      must be a local drive path, never UNC/verbatim/volume-GUID or a drive
//      root;
//   3. every component below the root is lstat'ed (bigint): reparse points
//      (symlinks AND junctions — libuv reports both through isSymbolicLink())
//      are rejected, and every component must sit on the root's volume so a
//      mounted volume that libuv reports as a plain directory still fails;
//   4. realpath of the target must equal root + the requested components,
//      compared per component with a case-folded key, which rejects 8.3
//      expansion and any reparse that resolved elsewhere;
//   5. the file is opened read-only and fstat (bigint) dev+ino+size+mtime of
//      the open handle must equal the lstat result, and the whole component
//      chain is re-lstat'ed after the open, so a swap between check and open
//      reads nothing.
//
// Writes (fs.write_new, fs.apply_patch) are intentionally NOT provided here:
// they remain fail-closed on Windows (platform-safety.mjs) because a create or
// replace through a swapped ancestor cannot be undone by a post-hoc identity
// check, and durable actions additionally require the action journal.
//
// Every filesystem primitive and the path module are injectable so the
// string rules and the swap/reparse decisions can be exercised on POSIX with
// path.win32 semantics and simulated stat results (tests/host/windows-*.mjs).
// Real NTFS behaviour is NOT exercised by those tests.
import { createHash } from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import { lstat, open, opendir, realpath } from 'node:fs/promises';
import path from 'node:path';
import { makeToolResult } from '../../agent/tool-envelope.mjs';
import { WorkspaceError } from './workspace-policy.mjs';
import { validateToolArguments } from './argument-validation.mjs';
import { filesystemDefinitions } from './filesystem.mjs';
import { abortable, DIRECTORY_BATCH, isCancelled, readAt, recoverableReadTool, SEARCH_BUDGETS, throwIfAborted } from './read-tool-support.mjs';
import { utf8Window } from './utf8-window.mjs';

// Same bounds as the POSIX implementation so the model sees one contract.
const MAX_READ = 65536; const MAX_SEARCH_FILES = 200; const MAX_SEARCH_MATCHES = 500;
const MAX_READ_FILE_BYTES = 8 * 1024 * 1024; const MAX_LIST_SIZE_REPORT = 1048576;
const MAX_RELATIVE_PATH = 1024; const MAX_COMPONENT = 255; const MAX_COMPONENTS = 64; const MAX_ROOT_PATH = 1024;

// Characters Win32 forbids in a name.  `:` covers alternate data streams and
// drive-relative `C:foo`; `<`, `>` and `"` are DOS wildcard aliases inside
// FindFirstFile, `?`/`*` are wildcards, and C0 controls are invalid in names.
const INVALID_NAME_CHARS = /[<>:"|?*\u0000-\u001f\u007f]/u;
// DOS device names stay devices with an extension or trailing spaces
// (`NUL.txt`, `CON .log`).  Windows also treats superscript digits 1-3 as
// COM/LPT digits, and CONIN$/CONOUT$/CLOCK$ open devices by name.
const RESERVED_DEVICE = /^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3])(?:[ .].*)?$/iu;
// 8.3 short names (`PROGRA~1`) alias long names; rejecting `~<digit>` keeps
// one spelling per file.  Long names that legitimately contain `~1` are
// therefore unreachable; that is an accepted false-negative.
const SHORT_NAME = /~[0-9]/u;
// "Best-fit" ANSI conversion maps these to `/`, `\`, `:`, `.` or wildcards
// when any downstream component uses a narrow (A) API; ¥ and ₩ are the
// backslash in the CP932/CP949 code pages.  They also make previews lie.
const CONFUSABLE = /[\u2215\u2044\u29f5\u29f8\u29f9\ufe68\uff0f\uff3c\u2216\u00a5\u20a9\uff1a\ufe55\ufe13\uff0e\u2024\ufe52\uff1f\uff0a\uff1c\uff1e\uff02\uff5c]/u;
// Controls, bidi/zero-width format characters, lone surrogates, private use
// and line/paragraph separators: invisible or unrenderable in a preview.
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/u;

const invalid = message => new WorkspaceError('invalid_path', message);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const result = (call, status, text, truncated = false) => makeToolResult({ id: call.id, name: call.name, status, text, truncated });
const boundedInt = (value, fallback, min, max) => Number.isInteger(value) ? Math.min(max, Math.max(min, value)) : fallback;
const argsObject = call => call.arguments ?? {};

// NTFS compares names through a 1:1 upcase table.  JS toUpperCase can expand
// (ß -> SS), so only single-unit mappings are applied; anything else is kept
// verbatim, which can only make two names compare *unequal* (fail-closed).
export function windowsCaseKey(value) {
  let key = '';
  for (const char of value) { const upper = char.toUpperCase(); key += upper.length === char.length ? upper : char; }
  return key;
}

function componentProblem(part) {
  if (!part) return 'empty path segment';
  if (part === '.' || part === '..') return 'dot segment';
  if (part.length > MAX_COMPONENT) return 'segment too long';
  if (INVALID_NAME_CHARS.test(part)) return 'reserved character (ADS, wildcard or control)';
  if (INVISIBLE.test(part)) return 'invisible or format character';
  if (CONFUSABLE.test(part)) return 'confusable separator or punctuation';
  if (/[ .]$/u.test(part)) return 'trailing dot or space';
  if (RESERVED_DEVICE.test(part)) return 'reserved device name';
  if (SHORT_NAME.test(part)) return '8.3 short-name form';
  return null;
}

export function isSafeWindowsComponent(part) { return typeof part === 'string' && componentProblem(part) === null; }

// Model-supplied workspace-relative path.  Returns the validated components;
// the model-facing spelling always uses `/`.
export function validateWindowsRelativePath(input, { allowEmpty = false, pathModule = path.win32 } = {}) {
  if (typeof input !== 'string' || input.length > MAX_RELATIVE_PATH || input.includes('\0')) throw invalid('path is invalid');
  if (!input) { if (allowEmpty) return Object.freeze({ components: Object.freeze([]), relative: '' }); throw invalid('path must not be empty'); }
  // Any leading separator is rooted: `\x`, `\\server\share` (UNC), `\\.\`
  // (device namespace) and `\\?\` (verbatim, disables Win32 normalisation).
  if (input.startsWith('\\') || input.startsWith('/')) throw invalid('path must be relative');
  if (/^[A-Za-z]:/u.test(input) || pathModule.isAbsolute(input)) throw invalid('path must be relative');
  // One separator style per path: mixing is never needed and is a common
  // obfuscation for filters that only normalise one of them.
  if (input.includes('/') && input.includes('\\')) throw invalid('path mixes separators');
  const components = input.split(/[\\/]/u);
  if (components.length > MAX_COMPONENTS) throw invalid('path is too deep');
  for (const part of components) { const problem = componentProblem(part); if (problem) throw invalid(`path contains a forbidden segment: ${problem}`); }
  return Object.freeze({ components: Object.freeze(components), relative: components.join('/') });
}

// Operator-configured roots and realpath outputs.  Only `X:\...` on a local
// drive letter is accepted.  realpath on Windows strips the `\\?\` verbatim
// prefix for drive paths, so a canonical value that still carries `\\?\`,
// `\\.\`, `\\?\UNC\`, `\\?\Volume{GUID}` or `\\server\share` names a device,
// a network share or a letterless volume, and all are refused.
export function canonicalWindowsDrivePath(value, { allowDriveRoot = false } = {}) {
  if (typeof value !== 'string' || value.length < 3 || value.length > MAX_ROOT_PATH || /[\u0000-\u001f\u007f]/u.test(value)) throw new WorkspaceError('invalid_workspace', 'workspace path is invalid');
  if (/^[\\/]{2}/u.test(value)) throw new WorkspaceError('invalid_workspace', 'UNC, device and verbatim workspace paths are not supported');
  if (!/^[A-Za-z]:[\\/]/u.test(value)) throw new WorkspaceError('invalid_workspace', 'workspace path must be an absolute local drive path');
  const segments = value.slice(3).split(/[\\/]/u);
  if (segments.at(-1) === '') segments.pop();
  if (segments.some(segment => !segment || segment === '.' || segment === '..' || INVALID_NAME_CHARS.test(segment) || /[ .]$/u.test(segment) || RESERVED_DEVICE.test(segment))) throw new WorkspaceError('invalid_workspace', 'workspace path contains a forbidden segment');
  if (!segments.length && !allowDriveRoot) throw new WorkspaceError('invalid_workspace', 'a whole drive cannot be a workspace');
  return `${value[0].toUpperCase()}:\\${segments.join('\\')}`;
}

const sameIdentity = (left, right) => typeof left?.ino === 'bigint' && typeof right?.ino === 'bigint' && left.ino !== 0n && left.ino === right.ino && left.dev === right.dev;
const sameContentStamp = (left, right) => left.size === right.size && left.mtimeNs === right.mtimeNs;
const nodeType = info => info.isSymbolicLink() ? 'link' : info.isDirectory() ? 'directory' : info.isFile() ? 'file' : 'other';

async function closeQuietly(handle) { await handle?.close().catch(() => {}); }

export const defaultWindowsFilesystem = Object.freeze({
  // fs.promises.realpath is libuv's uv_fs_realpath, i.e. fs.realpathSync.native
  // (GetFinalPathNameByHandleW), not the JS lstat-walk of fs.realpath.
  realpath: target => realpath(target),
  lstat: target => lstat(target, { bigint: true }),
  open: target => open(target, fsConstants.O_RDONLY),
  // opendir streams entries in small batches instead of materialising a
  // whole directory the way readdir does.
  opendir: target => opendir(target, { bufferSize: DIRECTORY_BATCH })
});

export class WindowsWorkspacePolicy {
  #pathModule; #fs;
  constructor(workspaces = [], { pathModule = path.win32, fs = defaultWindowsFilesystem } = {}) {
    this.#pathModule = pathModule; this.#fs = fs; this.workspaces = new Map();
    for (const item of workspaces) {
      const entry = typeof item === 'string' ? { id: `workspace-${this.workspaces.size}`, path: item, read: true, write: true } : item;
      if (!entry || typeof entry.id !== 'string' || !entry.id || typeof entry.path !== 'string') throw new WorkspaceError('invalid_workspace', 'workspace requires id and path');
      if (this.workspaces.has(entry.id)) throw new WorkspaceError('duplicate_workspace', 'workspace id is duplicated');
      // An unusable root string disables only that workspace; the registry
      // reports it instead of crashing startup or guessing a default.
      let configured = null; try { configured = canonicalWindowsDrivePath(entry.path, { allowDriveRoot: true }); } catch {}
      this.workspaces.set(entry.id, Object.freeze({ id: entry.id, path: configured, read: entry.read !== false, usable: configured !== null }));
    }
  }
  isUsable(id) { return this.workspaces.get(id)?.usable === true; }
  get(id) { const workspace = this.workspaces.get(id); if (!workspace) throw new WorkspaceError('unknown_workspace', 'workspace is not configured'); return workspace; }
  lstat(target, signal) { return abortable(this.#fs.lstat(target), signal); }
  async root(id, { signal } = {}) {
    const workspace = this.get(id); if (!workspace.read) throw new WorkspaceError('read_not_allowed', 'workspace is not readable'); if (!workspace.usable) throw new WorkspaceError('workspace_unavailable', 'workspace root is not a local drive path');
    let canonical; try { canonical = canonicalWindowsDrivePath(await abortable(this.#fs.realpath(workspace.path), signal)); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('workspace_unavailable', 'workspace root is unavailable or not a local drive path'); }
    let info; try { info = await this.lstat(canonical, signal); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('workspace_unavailable', 'workspace root is unavailable'); }
    if (info.isSymbolicLink() || !info.isDirectory() || typeof info.ino !== 'bigint' || info.ino === 0n) throw new WorkspaceError('workspace_unavailable', 'workspace root has no stable directory identity');
    return { id, canonical, stat: info };
  }
  // Lexical walk from the canonical root with per-component lstat.  `expect`
  // is 'file' or 'directory' for the final component.
  async resolve(id, input, { allowEmpty = false, expect, signal } = {}) {
    const { components, relative } = validateWindowsRelativePath(input, { allowEmpty, pathModule: this.#pathModule });
    const root = await this.root(id, { signal }); const chain = [{ path: root.canonical, stat: root.stat }]; let current = root.canonical;
    for (const [index, part] of components.entries()) {
      current = this.#pathModule.join(current, part);
      let info; try { info = await this.lstat(current, signal); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('not_found', 'path does not exist'); }
      if (info.isSymbolicLink()) throw new WorkspaceError('reparse_point_rejected', 'symlinks and junctions are not followed');
      // A mounted volume (or other reparse libuv does not classify as a
      // link) reports as a directory; the volume serial still changes.
      if (info.dev !== root.stat.dev) throw new WorkspaceError('path_escape', 'path crosses a volume boundary');
      if (typeof info.ino !== 'bigint' || info.ino === 0n) throw new WorkspaceError('path_changed', 'path has no stable file identity');
      if (index < components.length - 1 && !info.isDirectory()) throw new WorkspaceError('not_found', 'path does not exist');
      chain.push({ path: current, stat: info });
    }
    const last = chain.at(-1).stat;
    if (expect === 'file') {
      if (!last.isFile()) throw new WorkspaceError('not_regular_file', 'only regular files are supported');
      // A hard link is an alias the root boundary cannot see.
      if (last.nlink !== 1n) throw new WorkspaceError('hard_link_rejected', 'multiply-linked files are not read');
    }
    if (expect === 'directory' && !last.isDirectory()) throw new WorkspaceError('not_directory', 'path is not a directory');
    let canonical; try { canonical = canonicalWindowsDrivePath(await abortable(this.#fs.realpath(current), signal)); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('path_escape', 'path does not canonicalise inside the workspace'); }
    this.#assertCanonicalMatches(root.canonical, components, canonical);
    return { root, path: relative, components, candidate: current, canonical, chain, stat: last };
  }
  #assertCanonicalMatches(rootCanonical, components, canonical) {
    // Root prefix: exact, because both sides come from the same realpath call
    // and per-directory case sensitivity can make `A` and `a` distinct
    // siblings.  Below the root: case-folded per component, because the model
    // may legitimately spell an existing name in another case.
    if (!components.length) { if (canonical !== rootCanonical) throw new WorkspaceError('path_escape', 'resolved path escapes workspace'); return; }
    const prefix = `${rootCanonical}\\`;
    if (!canonical.startsWith(prefix)) throw new WorkspaceError('path_escape', 'resolved path escapes workspace');
    const rest = canonical.slice(prefix.length).split('\\');
    if (rest.length !== components.length || rest.some((name, index) => windowsCaseKey(name) !== windowsCaseKey(components[index]))) throw new WorkspaceError('path_escape', 'resolved path is an alias of the requested path');
  }
  // Re-lstat the whole chain; any identity or type change means something
  // was swapped while we worked.
  async verifyChain(resolved, signal) {
    for (const link of resolved.chain) {
      let info; try { info = await this.lstat(link.path, signal); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('path_changed', 'path changed during access'); }
      if (info.isSymbolicLink() || !sameIdentity(info, link.stat) || nodeType(info) !== nodeType(link.stat)) throw new WorkspaceError('path_changed', 'path changed during access');
    }
  }
  async openFile(id, input, { signal } = {}) {
    const resolved = await this.resolve(id, input, { expect: 'file', signal });
    let handle; try { handle = await abortable(this.#fs.open(resolved.candidate), signal, closeQuietly); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('path_changed', 'file changed before open'); }
    try {
      const opened = await abortable(handle.stat({ bigint: true }), signal);
      // The open follows reparse points; the handle's identity proves which
      // file it actually reached.
      if (!opened.isFile() || opened.nlink !== 1n || !sameIdentity(opened, resolved.stat) || !sameContentStamp(opened, resolved.stat)) throw new WorkspaceError('path_changed', 'file changed during safe open');
      await this.verifyChain(resolved, signal);
      return { ...resolved, handle, opened };
    } catch (error) { await closeQuietly(handle); throw error; }
  }
  // Reads at most `limit` names (plus one look-ahead to report whether more
  // exist) without loading the whole directory.
  async readDirectory(resolved, { limit, signal } = {}) {
    let dir; try { dir = await abortable(this.#fs.opendir(resolved.candidate), signal, closeQuietly); } catch (error) { if (isCancelled(error)) throw error; throw new WorkspaceError('not_found', 'directory is unavailable'); }
    const names = []; let more = false;
    try {
      while (true) {
        const entry = await abortable(dir.read(), signal); if (!entry) break;
        if (names.length >= limit) { more = true; break; }
        names.push(entry.name);
      }
    } finally { await closeQuietly(dir); }
    // opendir takes a path, not a handle: confirm the directory we listed is
    // still the one we verified.
    await this.verifyChain(resolved, signal);
    return { names, more };
  }
}

function textContent(bytes) { try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch { throw new WorkspaceError('not_text', 'file is not valid UTF-8 text'); } }

const { directories: MAX_SEARCH_DIRECTORIES, entries: MAX_SEARCH_ENTRIES, entriesPerDirectory: MAX_DIRECTORY_ENTRIES } = SEARCH_BUDGETS;

export function createWindowsReadOnlyFilesystemTools(policy) {
  if (!(policy instanceof WindowsWorkspacePolicy)) throw new TypeError('WindowsWorkspacePolicy is required');
  const join = (directory, name) => path.win32.join(directory, name);
  const list = async call => {
    const signal = call.signal; const args = validateToolArguments('fs.list', argsObject(call)); const resolved = await policy.resolve(args.workspace_id, args.path ?? '', { allowEmpty: true, expect: 'directory', signal });
    const maxEntries = boundedInt(args.max_entries, 100, 1, 500); const { names, more } = await policy.readDirectory(resolved, { limit: maxEntries, signal }); const output = [];
    for (const name of names) {
      // Names this policy could never resolve (device names, trailing dots,
      // ADS, invisible characters) are not surfaced to the model.
      if (!isSafeWindowsComponent(name)) continue;
      let info; try { info = await policy.lstat(join(resolved.candidate, name), signal); } catch (error) { if (isCancelled(error)) throw error; continue; }
      const type = nodeType(info);
      output.push({ name, type: type === 'link' ? 'other' : type, size_bytes: type === 'file' ? Math.min(Number(info.size), MAX_LIST_SIZE_REPORT) : 0 });
    }
    return result(call, 'ok', JSON.stringify({ workspace_id: args.workspace_id, path: resolved.path, entries: output, entry_count: output.length }), more);
  };
  const readText = async call => {
    const signal = call.signal; const args = validateToolArguments('fs.read_text', argsObject(call)); const maxBytes = boundedInt(args.max_bytes, MAX_READ, 1, MAX_READ); const offset = boundedInt(args.offset_bytes, 0, 0, 1048576);
    const file = await policy.openFile(args.workspace_id, args.path, { signal }); let window; let size;
    try {
      size = Number(file.opened.size); if (size > MAX_READ_FILE_BYTES) throw new WorkspaceError('file_too_large', 'file exceeds the bounded operation size');
      const raw = await readAt(file.handle, maxBytes, offset, signal);
      const after = await abortable(file.handle.stat({ bigint: true }), signal); if (!sameIdentity(after, file.opened) || !sameContentStamp(after, file.opened)) throw new WorkspaceError('path_changed', 'file changed during read');
      window = utf8Window(raw, { atStart: offset === 0, atEnd: offset + raw.length >= size });
      if (!window.bytes.length && offset + window.start < size) throw new WorkspaceError(raw.length < 4 && raw.length === maxBytes ? 'max_bytes_too_small' : 'not_text', 'no complete character in the requested window');
    } finally { await closeQuietly(file.handle); }
    const { bytes } = window; const start = offset + window.start; const content = textContent(bytes); const truncated = start + bytes.length < size;
    // offset_bytes is where the returned text really starts, so offset_bytes
    // + bytes is always a valid character boundary to continue from.
    return result(call, 'ok', JSON.stringify({ workspace_id: args.workspace_id, path: file.path, text: content, sha256: sha256(bytes), hash_scope: 'returned_bytes', offset_bytes: start, bytes: bytes.length, truncated }), truncated);
  };
  const searchText = async call => {
    const signal = call.signal; const args = validateToolArguments('fs.search_text', argsObject(call)); const id = args.workspace_id;
    const start = await policy.resolve(id, args.path ?? '', { allowEmpty: true, expect: 'directory', signal }); const maxFiles = boundedInt(args.max_files, MAX_SEARCH_FILES, 1, MAX_SEARCH_FILES); const maxMatches = boundedInt(args.max_matches, MAX_SEARCH_MATCHES, 1, MAX_SEARCH_MATCHES); const maxDepth = boundedInt(args.max_depth, 8, 0, 16);
    const matches = []; const skipped = { too_large: 0, not_text: 0, unreadable: 0 }; let filesSeen = 0; let directoriesSeen = 0; let entriesSeen = 0; let truncated = false;
    const exhausted = () => filesSeen >= maxFiles || matches.length >= maxMatches || directoriesSeen >= MAX_SEARCH_DIRECTORIES || entriesSeen >= MAX_SEARCH_ENTRIES;
    const walk = async (directory, depth) => {
      throwIfAborted(signal);
      if (depth > maxDepth || exhausted()) { truncated = true; return; }
      directoriesSeen++;
      let listing; try { listing = await policy.readDirectory(directory, { limit: Math.min(MAX_DIRECTORY_ENTRIES, MAX_SEARCH_ENTRIES - entriesSeen), signal }); } catch (error) { if (isCancelled(error)) throw error; skipped.unreadable++; return; }
      if (listing.more) truncated = true;
      for (const name of listing.names) {
        throwIfAborted(signal);
        if (exhausted()) { truncated = true; return; }
        entriesSeen++;
        if (name.startsWith('.git') || !isSafeWindowsComponent(name)) continue;
        const rel = directory.path ? `${directory.path}/${name}` : name;
        // Types come from lstat, not the dirent: libuv reports every reparse
        // point (including cloud-file placeholders) as a dirent link.
        let info; try { info = await policy.lstat(join(directory.candidate, name), signal); } catch (error) { if (isCancelled(error)) throw error; skipped.unreadable++; continue; }
        if (info.isSymbolicLink()) continue;
        if (info.isDirectory()) { let child; try { child = await policy.resolve(id, rel, { expect: 'directory', signal }); } catch (error) { if (isCancelled(error)) throw error; skipped.unreadable++; continue; } await walk(child, depth + 1); continue; }
        if (!info.isFile()) continue; filesSeen++;
        // Skipped files make the answer incomplete; say so instead of
        // letting "no match" read as "not present".
        if (Number(info.size) > MAX_READ) { skipped.too_large++; truncated = true; continue; }
        let bytes; try { const file = await policy.openFile(id, rel, { signal }); try { if (Number(file.opened.size) > MAX_READ) { skipped.too_large++; truncated = true; continue; } bytes = await readAt(file.handle, MAX_READ, 0, signal); } finally { await closeQuietly(file.handle); } } catch (error) { if (isCancelled(error)) throw error; skipped.unreadable++; continue; }
        let content; try { content = textContent(bytes); } catch { skipped.not_text++; continue; }
        let from = 0; while (matches.length < maxMatches) { const index = content.indexOf(args.query, from); if (index < 0) break; const before = content.slice(0, index); matches.push({ path: rel, line: before.split('\n').length, column: index - (before.lastIndexOf('\n') + 1) + 1, text: content.slice(Math.max(0, index - 80), Math.min(content.length, index + args.query.length + 80)) }); from = index + Math.max(1, args.query.length); }
        if (matches.length >= maxMatches) truncated = true;
      }
    };
    await walk(start, 0);
    return result(call, 'ok', JSON.stringify({ query: args.query, matches, files_seen: filesSeen, directories_seen: directoriesSeen, skipped, truncated }), truncated);
  };
  return {
    'fs.list': { ...filesystemDefinitions['fs.list'], execute: recoverableReadTool(list) },
    'fs.read_text': { ...filesystemDefinitions['fs.read_text'], execute: recoverableReadTool(readText) },
    'fs.search_text': { ...filesystemDefinitions['fs.search_text'], execute: recoverableReadTool(searchText) }
  };
}
