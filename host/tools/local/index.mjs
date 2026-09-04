import { WorkspacePolicy } from './workspace-policy.mjs';
import { createFilesystemTools } from './filesystem.mjs';
import { createSystemTools } from './system-tools.mjs';
import { timeNowDefinition, timeNowTool } from '../time-now.mjs';

export function createLocalToolRegistry({ workspaces = [], applications = {}, browserExecutable, platform, networkProvider = 'disabled' } = {}) {
  const policy = new WorkspacePolicy(workspaces); const filesystem = workspaces.length ? createFilesystemTools(policy) : {};
  const system = createSystemTools({ applications, browserExecutable, platform, networkProvider });
  return { 'time.now': { ...timeNowDefinition, execute: ({ id, arguments: args }) => timeNowTool({ id, arguments: args }) }, ...system, ...filesystem };
}

export { WorkspacePolicy } from './workspace-policy.mjs';
export { createFilesystemTools } from './filesystem.mjs';
export { createSystemTools } from './system-tools.mjs';
