import { access, lstat, realpath } from 'node:fs/promises';
import { constants } from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';

const RESERVED = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;
const WINDOWS_DRIVE = /^[A-Za-z]:/;

export class WorkspaceError extends Error {
  constructor(code, message) { super(message); this.name = 'WorkspaceError'; this.code = code; }
}

export function validateRelativePath(input, { allowEmpty = false } = {}) {
  if (typeof input !== 'string' || input.length > 1024 || input.includes('\0')) throw new WorkspaceError('invalid_path', 'path is invalid');
  if (!input && allowEmpty) return '';
  if (!input || isAbsolute(input) || WINDOWS_DRIVE.test(input) || input.startsWith('\\') || input.startsWith('/')) throw new WorkspaceError('invalid_path', 'path must be relative');
  const parts = input.replaceAll('\\', '/').split('/');
  if (parts.some(part => !part || part === '.' || part === '..' || part.includes(':') || RESERVED.test(part))) throw new WorkspaceError('invalid_path', 'path contains a forbidden segment');
  return parts.join('/');
}

function contained(root, target) {
  const rel = relative(root, target); return rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

export class WorkspacePolicy {
  constructor(workspaces = []) {
    this.workspaces = new Map();
    for (const item of workspaces) {
      const entry = typeof item === 'string' ? { id: `workspace-${this.workspaces.size}`, path: item, read: true, write: true } : item;
      if (!entry || typeof entry.id !== 'string' || !entry.id || typeof entry.path !== 'string') throw new WorkspaceError('invalid_workspace', 'workspace requires id and path');
      if (this.workspaces.has(entry.id)) throw new WorkspaceError('duplicate_workspace', 'workspace id is duplicated');
      this.workspaces.set(entry.id, { id: entry.id, path: resolve(entry.path), read: entry.read !== false, write: entry.write === true });
    }
  }
  get(id) { const workspace = this.workspaces.get(id); if (!workspace) throw new WorkspaceError('unknown_workspace', 'workspace is not configured'); return workspace; }
  async root(id, { write = false } = {}) {
    const workspace = this.get(id); if (write && !workspace.write) throw new WorkspaceError('write_not_allowed', 'workspace is read-only'); if (!write && !workspace.read) throw new WorkspaceError('read_not_allowed', 'workspace is not readable');
    let canonical; try { canonical = await realpath(workspace.path); } catch { throw new WorkspaceError('workspace_unavailable', 'workspace root is unavailable'); }
    return { ...workspace, canonical };
  }
  async resolve(id, input, { write = false, mustExist = false, allowEmpty = false } = {}) {
    const root = await this.root(id, { write }); const path = validateRelativePath(input, { allowEmpty }); const candidate = resolve(root.canonical, path);
    if (!contained(root.canonical, candidate)) throw new WorkspaceError('path_escape', 'path escapes workspace');
    let canonical = candidate;
    try { canonical = await realpath(candidate); } catch (error) { if (mustExist || !write) throw new WorkspaceError('not_found', 'path does not exist'); }
    if (!contained(root.canonical, canonical)) throw new WorkspaceError('path_escape', 'resolved path escapes workspace');
    if (mustExist) { try { await access(canonical, constants.F_OK); } catch { throw new WorkspaceError('not_found', 'path does not exist'); } }
    return { root, path, candidate, canonical };
  }
  async parent(id, input) {
    const path = validateRelativePath(input); const parentPath = dirname(path) === '.' ? '' : dirname(path).replaceAll('\\', '/');
    const parent = parentPath ? await this.resolve(id, parentPath, { write: true, mustExist: true }) : await this.root(id, { write: true }).then(root => ({ root, path: '', candidate: root.canonical, canonical: root.canonical }));
    return { ...parent, name: path.split('/').at(-1), relative: path };
  }
  async regularFile(id, input, { write = false } = {}) {
    const resolved = await this.resolve(id, input, { write, mustExist: true }); let stat; try { stat = await lstat(resolved.canonical); } catch { throw new WorkspaceError('not_found', 'path does not exist'); }
    if (!stat.isFile()) throw new WorkspaceError('not_regular_file', 'only regular files are supported');
    return { ...resolved, stat };
  }
}
