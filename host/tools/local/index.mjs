import { WorkspacePolicy } from './workspace-policy.mjs';
import { createFilesystemTools } from './filesystem.mjs';
import { createSystemTools } from './system-tools.mjs';
import { timeNowDefinition, timeNowTool } from '../time-now.mjs';
import { createProcessRunTools, ProcessRunProvider } from './process-run.mjs';

export function createLocalToolRegistry({ workspaces = [], applications = {}, process_actions: processActions = {}, processEnvironment = {}, browserExecutable, platform, networkProvider = 'disabled', grantControl } = {}) {
  const policy = new WorkspacePolicy(workspaces); const filesystem = workspaces.length ? createFilesystemTools(policy, { platform, grantControl }) : {};
  const system = createSystemTools({ applications, browserExecutable, platform, networkProvider, grantControl });
  const process = createProcessRunTools(new ProcessRunProvider({ enabled: processActions.enabled === true, actions: processActions.actions ?? {}, workspacePolicy: policy, grantControl, platform, environment: processEnvironment }));
  return { 'time.now': { ...timeNowDefinition, execute: ({ id, arguments: args }) => timeNowTool({ id, arguments: args }) }, ...system, ...filesystem, 'process.run_allowlisted': process };
}

export { WorkspacePolicy } from './workspace-policy.mjs';
export { createFilesystemTools } from './filesystem.mjs';
export { createSystemTools } from './system-tools.mjs';
export { createProcessRunTools, ProcessRunProvider } from './process-run.mjs';
