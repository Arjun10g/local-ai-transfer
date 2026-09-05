import { WorkspacePolicy } from './workspace-policy.mjs';
import { createFilesystemTools, filesystemDefinitions } from './filesystem.mjs';
import { createSystemTools, systemDefinitions } from './system-tools.mjs';
import { timeNowDefinition, timeNowTool } from '../time-now.mjs';
import { createProcessRunTools, ProcessRunProvider, processDefinition } from './process-run.mjs';

const LOCAL_TOOL_NAMES = Object.freeze([
  'time.now', 'system.get_info', 'clipboard.read', 'clipboard.write', 'app.open', 'browser.open_url',
  'fs.list', 'fs.read_text', 'fs.search_text', 'fs.write_new', 'fs.apply_patch', 'process.run_allowlisted'
]);

function freezeCapabilitySnapshot(platform, states) {
  const tools = Object.fromEntries(LOCAL_TOOL_NAMES.map(name => [name, Object.freeze(states[name] ?? { advertised: false, reason: 'unconfigured' })]));
  return Object.freeze({ basis: 'immutable_startup_configuration', platform, tools: Object.freeze(tools) });
}

function workspaceSets(workspaces) {
  const readable = new Set(); const writable = new Set();
  workspaces.forEach((item, index) => {
    const entry = typeof item === 'string' ? { id: `workspace-${index}`, read: true, write: true } : item;
    if (entry?.read !== false) readable.add(entry.id);
    if (entry?.write === true || typeof item === 'string') writable.add(entry.id);
  });
  return { readable, writable };
}

// Schema/evaluation catalog only. Production must use
// createLocalToolRegistry(), which applies the configured-capability filter.
export function createLocalToolDefinitionCatalog() {
  return { [timeNowDefinition.name]: timeNowDefinition, ...systemDefinitions, ...filesystemDefinitions, [processDefinition.name]: createProcessRunTools() };
}

export function createLocalToolRegistry({ workspaces = [], applications = {}, process_actions: processActions = {}, processEnvironment = {}, browserExecutable, platform, networkProvider = 'disabled', grantControl } = {}) {
  const effectivePlatform = platform ?? process.platform;
  const policy = new WorkspacePolicy(workspaces); const { readable, writable } = workspaceSets(workspaces); const patchable = new Set([...writable].filter(id => readable.has(id)));
  const filesystemSupported = effectivePlatform !== 'win32';
  const filesystem = workspaces.length && filesystemSupported ? createFilesystemTools(policy, { platform: effectivePlatform, grantControl }) : {};
  const system = createSystemTools({ applications, browserExecutable, platform: effectivePlatform, networkProvider, grantControl });
  const actionEntries = Object.values(processActions.actions ?? {});
  const processConfigured = effectivePlatform !== 'win32' && processActions.enabled === true && actionEntries.length > 0 && actionEntries.every(action => writable.has(action.workspace_id));
  const processProvider = new ProcessRunProvider({ enabled: processConfigured, actions: processActions.actions ?? {}, workspacePolicy: policy, grantControl, platform: effectivePlatform, environment: processEnvironment });
  const registry = {
    'time.now': { ...timeNowDefinition, execute: ({ id, arguments: args }) => timeNowTool({ id, arguments: args }) },
    'system.get_info': system['system.get_info'],
    ...(effectivePlatform !== 'win32' && Object.keys(applications).length ? { 'app.open': system['app.open'] } : {}),
    ...(effectivePlatform !== 'win32' && networkProvider === 'browser_open' ? { 'browser.open_url': system['browser.open_url'] } : {}),
    ...(readable.size ? Object.fromEntries(['fs.list', 'fs.read_text', 'fs.search_text'].filter(name => filesystem[name]).map(name => [name, filesystem[name]])) : {}),
    ...(writable.size && filesystem['fs.write_new'] ? { 'fs.write_new': filesystem['fs.write_new'] } : {}),
    ...(patchable.size && filesystem['fs.apply_patch'] ? { 'fs.apply_patch': filesystem['fs.apply_patch'] } : {}),
    ...(processConfigured ? { 'process.run_allowlisted': createProcessRunTools(processProvider) } : {})
  };
  const states = {
    'time.now': Object.freeze({ advertised: true, reason: 'always_available' }),
    'system.get_info': Object.freeze({ advertised: true, reason: 'always_available' }),
    'clipboard.read': Object.freeze({ advertised: false, reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary' : 'platform_unsupported' }),
    'clipboard.write': Object.freeze({ advertised: false, reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary' : 'platform_unsupported' }),
    'app.open': Object.freeze({ advertised: effectivePlatform !== 'win32' && Object.keys(applications).length > 0, reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary' : Object.keys(applications).length ? 'allowlist_configured' : 'allowlist_absent' }),
    'browser.open_url': Object.freeze({ advertised: effectivePlatform !== 'win32' && networkProvider === 'browser_open', reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary' : networkProvider === 'browser_open' ? 'provider_configured' : 'provider_disabled' }),
    'fs.list': Object.freeze({ advertised: Boolean(filesystem['fs.list'] && readable.size), reason: !filesystemSupported ? 'platform_unsupported' : readable.size ? 'workspace_configured' : 'workspace_unconfigured' }),
    'fs.read_text': Object.freeze({ advertised: Boolean(filesystem['fs.read_text'] && readable.size), reason: !filesystemSupported ? 'platform_unsupported' : readable.size ? 'workspace_configured' : 'workspace_unconfigured' }),
    'fs.search_text': Object.freeze({ advertised: Boolean(filesystem['fs.search_text'] && readable.size), reason: !filesystemSupported ? 'platform_unsupported' : readable.size ? 'workspace_configured' : 'workspace_unconfigured' }),
    'fs.write_new': Object.freeze({ advertised: Boolean(filesystem['fs.write_new'] && writable.size), reason: !filesystemSupported ? 'platform_unsupported' : writable.size ? 'writable_workspace_configured' : 'writable_workspace_unconfigured' }),
    'fs.apply_patch': Object.freeze({ advertised: Boolean(filesystem['fs.apply_patch'] && patchable.size), reason: !filesystemSupported ? 'platform_unsupported' : patchable.size ? 'readable_writable_workspace_configured' : 'readable_writable_workspace_unconfigured' }),
    'process.run_allowlisted': Object.freeze({ advertised: processConfigured, reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary' : processConfigured ? 'actions_configured' : processActions.enabled === true ? 'actions_or_writable_workspace_unconfigured' : 'provider_disabled' })
  };
  Object.defineProperty(registry, 'capabilitySnapshot', { enumerable: false, value: freezeCapabilitySnapshot(effectivePlatform, states), writable: false, configurable: false });
  return registry;
}

export { WorkspacePolicy } from './workspace-policy.mjs';
export { createFilesystemTools } from './filesystem.mjs';
export { createSystemTools } from './system-tools.mjs';
export { createProcessRunTools, ProcessRunProvider } from './process-run.mjs';
