import { createHash, randomUUID } from 'node:crypto';
import { open, readFile, readdir, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { makeToolResult } from '../../agent/tool-envelope.mjs';
import { WorkspaceError } from './workspace-policy.mjs';
import { validateToolArguments } from './argument-validation.mjs';

const MAX_READ = 65536; const MAX_SEARCH_FILES = 200; const MAX_SEARCH_MATCHES = 500;
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const result = (call, status, text, truncated = false, durationMs = 0) => makeToolResult({ id: call.id, name: call.name, status, text, truncated, durationMs });
const boundedInt = (value, fallback, min, max) => Number.isInteger(value) ? Math.min(max, Math.max(min, value)) : fallback;
const argsObject = call => call.arguments ?? {};

export const filesystemDefinitions = Object.freeze({
  'fs.list': { name: 'fs.list', version: '1.0.0', risk_tier: 'T0', side_effect: 'none', network: false, requires_confirmation: false, timeout_ms: 3000, output_limit: 32768 },
  'fs.read_text': { name: 'fs.read_text', version: '1.0.0', risk_tier: 'T0', side_effect: 'none', network: false, requires_confirmation: false, timeout_ms: 3000, output_limit: MAX_READ },
  'fs.search_text': { name: 'fs.search_text', version: '1.0.0', risk_tier: 'T0', side_effect: 'none', network: false, requires_confirmation: false, timeout_ms: 5000, output_limit: 65536 },
  'fs.write_new': { name: 'fs.write_new', version: '1.0.0', risk_tier: 'T2', side_effect: 'create', network: false, requires_confirmation: true, timeout_ms: 5000, output_limit: 8192 },
  'fs.apply_patch': { name: 'fs.apply_patch', version: '1.0.0', risk_tier: 'T2', side_effect: 'replace', network: false, requires_confirmation: true, timeout_ms: 5000, output_limit: 16384 }
});

function textContent(bytes) { try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch { throw new WorkspaceError('not_text', 'file is not valid UTF-8 text'); } }
async function boundedFile(policy, call, { maxBytes = MAX_READ } = {}) {
  const args = argsObject(call); const file = await policy.regularFile(args.workspace_id, args.path); if (file.stat.size > maxBytes) throw new WorkspaceError('file_too_large', 'file exceeds the bounded operation size'); return file;
}

export function createFilesystemTools(policy) {
  if (!policy) throw new TypeError('WorkspacePolicy is required');
  const list = async call => {
    const args = validateToolArguments('fs.list', argsObject(call)); const resolved = await policy.resolve(args.workspace_id, args.path ?? '', { allowEmpty: true, mustExist: true }); const info = await stat(resolved.canonical); if (!info.isDirectory()) throw new WorkspaceError('not_directory', 'path is not a directory');
    const maxEntries = boundedInt(args.max_entries, 100, 1, 500); const entries = await readdir(resolved.canonical, { withFileTypes: true }); const output = []; let truncated = entries.length > maxEntries;
    for (const entry of entries.slice(0, maxEntries)) { if (entry.name.includes(':') || entry.name === '.' || entry.name === '..') continue; let size = 0; try { if (entry.isFile()) size = (await stat(join(resolved.canonical, entry.name))).size; } catch {} output.push({ name: entry.name, type: entry.isDirectory() ? 'directory' : entry.isFile() ? 'file' : 'other', size_bytes: Math.min(size, 1048576) }); }
    return result(call, 'ok', JSON.stringify({ workspace_id: args.workspace_id, path: resolved.path, entries: output, entry_count: output.length }), truncated);
  };
  const readText = async call => {
    const args = validateToolArguments('fs.read_text', argsObject(call)); const maxBytes = boundedInt(args.max_bytes, MAX_READ, 1, MAX_READ); const offset = boundedInt(args.offset_bytes, 0, 0, 1048576); const file = await boundedFile(policy, call, { maxBytes: 8 * 1024 * 1024 }); const stable = await policy.regularFile(args.workspace_id, args.path); if (stable.canonical !== file.canonical || stable.stat.size !== file.stat.size || stable.stat.mtimeMs !== file.stat.mtimeMs) throw new WorkspaceError('path_changed', 'file changed during authorization');
    const handle = await open(stable.canonical, 'r'); let bytes; try { bytes = Buffer.alloc(maxBytes); const read = await handle.read(bytes, 0, maxBytes, offset); bytes = bytes.subarray(0, read.bytesRead); } finally { await handle.close(); }
    const content = textContent(bytes); const truncated = offset + bytes.length < file.stat.size; return result(call, 'ok', JSON.stringify({ workspace_id: args.workspace_id, path: file.path, text: content, sha256: sha256(bytes), hash_scope: 'returned_bytes', offset_bytes: offset, bytes: bytes.length, truncated }), truncated);
  };
  const searchText = async call => {
    const args = validateToolArguments('fs.search_text', argsObject(call));
    const root = await policy.resolve(args.workspace_id, args.path ?? '', { allowEmpty: true, mustExist: true }); const maxFiles = boundedInt(args.max_files, MAX_SEARCH_FILES, 1, MAX_SEARCH_FILES); const maxMatches = boundedInt(args.max_matches, MAX_SEARCH_MATCHES, 1, MAX_SEARCH_MATCHES); const maxDepth = boundedInt(args.max_depth, 8, 0, 16); const matches = []; let filesSeen = 0; let truncated = false;
    const walk = async (directory, depth, relativePath) => { if (depth > maxDepth || filesSeen >= maxFiles || matches.length >= maxMatches) { truncated = true; return; } let entries; try { entries = await readdir(directory, { withFileTypes: true }); } catch { return; }
      for (const entry of entries) { if (filesSeen >= maxFiles || matches.length >= maxMatches) { truncated = true; return; } if (entry.name.includes(':') || entry.name.startsWith('.git')) continue; const child = join(directory, entry.name); const rel = relativePath ? `${relativePath}/${entry.name}` : entry.name; if (entry.isSymbolicLink()) continue; if (entry.isDirectory()) { await walk(child, depth + 1, rel); continue; } if (!entry.isFile()) continue; filesSeen++;
        let bytes; try { const file = await policy.regularFile(args.workspace_id, rel); if (file.stat.size > MAX_READ) continue; bytes = await readFile(file.canonical); } catch { continue; } let content; try { content = textContent(bytes); } catch { continue; }
        let from = 0; while (matches.length < maxMatches) { const index = content.indexOf(args.query, from); if (index < 0) break; const before = content.slice(0, index); matches.push({ path: rel, line: before.split('\n').length, column: index - (before.lastIndexOf('\n') + 1) + 1, text: content.slice(Math.max(0, index - 80), Math.min(content.length, index + args.query.length + 80)) }); from = index + Math.max(1, args.query.length); } if (matches.length >= maxMatches) truncated = true;
      }
    };
    await walk(root.canonical, 0, root.path); return result(call, 'ok', JSON.stringify({ query: args.query, matches, files_seen: filesSeen, truncated }), truncated);
  };
  const writeNew = async call => {
    const args = validateToolArguments('fs.write_new', argsObject(call)); const parent = await policy.parent(args.workspace_id, args.path); const stableParent = parent.path ? await policy.resolve(args.workspace_id, parent.path, { write: true, mustExist: true }) : await policy.resolve(args.workspace_id, '', { write: true, mustExist: true, allowEmpty: true }); if (stableParent.canonical !== parent.canonical) throw new WorkspaceError('path_changed', 'parent changed during authorization'); const target = join(stableParent.canonical, parent.name); let handle; try { handle = await open(target, 'wx', 0o600); await handle.writeFile(args.content, 'utf8'); await handle.sync(); } catch (error) { if (error.code === 'EEXIST') throw new WorkspaceError('already_exists', 'write_new refuses overwrite'); throw error; } finally { await handle?.close(); } return result(call, 'ok', JSON.stringify({ created: true, path: parent.relative, bytes: Buffer.byteLength(args.content), sha256: sha256(Buffer.from(args.content)) }));
  };
  const patchPreview = async call => {
    const args = validateToolArguments('fs.apply_patch', argsObject(call)); const baseHash = args.base_sha256 ?? args.base_hash; const file = await boundedFile(policy, call, { maxBytes: 2 * 1024 * 1024 }); const stable = await policy.regularFile(args.workspace_id, args.path); if (stable.canonical !== file.canonical || stable.stat.size !== file.stat.size || stable.stat.mtimeMs !== file.stat.mtimeMs) throw new WorkspaceError('path_changed', 'file changed during authorization'); const oldBytes = await readFile(stable.canonical); const replacement = args.replacement ?? args.patch; const oldText = textContent(oldBytes); const newBytes = Buffer.from(replacement, 'utf8'); const diff = `--- ${file.path}\n+++ ${file.path}\n- ${oldText}\n+ ${replacement}`.slice(0, 8192); return { file: stable, oldBytes, replacement, baseHash, oldHash: sha256(oldBytes), newBytes, preview: { path: file.path, base_sha256: sha256(oldBytes), replacement_sha256: sha256(newBytes), old_bytes: oldBytes.length, new_bytes: newBytes.length, changed: oldText !== replacement, diff, diff_truncated: diff.length >= 8192 } };
  };
  const applyPatch = async call => {
    const prepared = await patchPreview(call); if (prepared.oldHash.toLowerCase() !== prepared.baseHash.toLowerCase()) throw new WorkspaceError('base_hash_mismatch', 'file changed since patch proposal'); const finalCheck = await policy.regularFile(argsObject(call).workspace_id, argsObject(call).path); if (finalCheck.canonical !== prepared.file.canonical || finalCheck.stat.size !== prepared.file.stat.size || finalCheck.stat.mtimeMs !== prepared.file.stat.mtimeMs) throw new WorkspaceError('path_changed', 'file changed before atomic replace'); const temp = `${prepared.file.canonical}.lae-${randomUUID()}.tmp`; await writeFile(temp, prepared.newBytes, { mode: 0o600, flag: 'wx' }); const verify = sha256(await readFile(prepared.file.canonical)); if (verify !== prepared.oldHash) { await unlink(temp).catch(() => {}); throw new WorkspaceError('base_hash_mismatch', 'file changed before atomic replace'); }
    const renameCheck = await policy.regularFile(argsObject(call).workspace_id, argsObject(call).path); if (renameCheck.canonical !== prepared.file.canonical || renameCheck.stat.size !== prepared.file.stat.size || renameCheck.stat.mtimeMs !== prepared.file.stat.mtimeMs || sha256(await readFile(renameCheck.canonical)) !== prepared.oldHash) { await unlink(temp).catch(() => {}); throw new WorkspaceError('path_changed', 'file changed immediately before atomic replace'); }
    try { await rename(temp, renameCheck.canonical); } catch (error) {
      if (!['EEXIST', 'EPERM', 'ENOTEMPTY'].includes(error.code)) { await unlink(temp).catch(() => {}); throw error; }
      const backup = `${renameCheck.canonical}.lae-${randomUUID()}.bak`; try { await rename(renameCheck.canonical, backup); await rename(temp, renameCheck.canonical); await unlink(backup); } catch (inner) { await rename(backup, renameCheck.canonical).catch(() => {}); await unlink(temp).catch(() => {}); throw inner; }
    }
    const written = await readFile(prepared.file.canonical); return result(call, 'ok', JSON.stringify({ applied: true, ...prepared.preview, final_sha256: sha256(written) }));
  };
  const tools = { 'fs.list': { ...filesystemDefinitions['fs.list'], execute: list }, 'fs.read_text': { ...filesystemDefinitions['fs.read_text'], execute: readText }, 'fs.search_text': { ...filesystemDefinitions['fs.search_text'], execute: searchText }, 'fs.write_new': { ...filesystemDefinitions['fs.write_new'], execute: writeNew }, 'fs.apply_patch': { ...filesystemDefinitions['fs.apply_patch'], preview: async call => (await patchPreview(call)).preview, execute: applyPatch } };
  return tools;
}
