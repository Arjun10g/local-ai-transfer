// The only tools a delegated job's controller is ever built with.
//
// This is the first of two independent layers: the registry handed to the
// job's controller simply does not contain a write, launch, process,
// clipboard, browser or provider tool, and its filesystem tools are built
// over ONLY the folders the operator flagged `"delegate": true`, forced
// read-only.  The second layer is the controller's `tools: 'delegate'` mode,
// which filters and fails closed again even if a registry held more.
import { createLocalToolRegistry } from '../tools/local/index.mjs';

const FILE_TOOLS = Object.freeze(['fs.list', 'fs.read_text', 'fs.search_text']);

/** The flagged folders as read-only workspace entries (string entries cannot be flagged). */
export function delegateWorkspaces(workspaceRoots = []) {
  return workspaceRoots
    .filter(entry => entry && typeof entry === 'object' && !Array.isArray(entry) && entry.delegate === true && entry.read !== false)
    .map(entry => ({ id: entry.id, path: entry.path, read: true, write: false }));
}

export function createDelegateToolRegistry({ workspaceRoots = [], platform } = {}) {
  const workspaces = delegateWorkspaces(workspaceRoots);
  // No applications, no process actions, network disabled, no grant control:
  // nothing here can be granted into doing more.
  const local = createLocalToolRegistry({ workspaces, platform, networkProvider: 'disabled' });
  const registry = { 'system.get_info': local['system.get_info'] };
  for (const name of FILE_TOOLS) if (local[name]) registry[name] = local[name];
  const files = FILE_TOOLS.every(name => registry[name]);
  return { registry, workspaceIds: files ? workspaces.map(entry => entry.id) : [] };
}
