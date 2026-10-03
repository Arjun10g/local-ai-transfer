import { WorkspacePolicy } from './workspace-policy.mjs';
import { createFilesystemTools, filesystemDefinitions } from './filesystem.mjs';
import { createSystemTools, systemDefinitions } from './system-tools.mjs';
import { timeNowDefinition, timeNowTool } from '../time-now.mjs';
import { createProcessRunTools, ProcessRunProvider, processDefinition } from './process-run.mjs';
import { WINDOWS_FILESYSTEM_STATUS } from './platform-safety.mjs';
import { createWindowsReadOnlyFilesystemTools, WindowsWorkspacePolicy } from './windows-filesystem.mjs';

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

function filesystemCapability(advertised, reason, notReady) {
  return Object.freeze(notReady ? { ...WINDOWS_FILESYSTEM_STATUS, advertised, reason } : { advertised, reason });
}

// Windows serves only the read-only trio through the separate check-then-
// verify implementation; the profile marker keeps status output honest that
// this is not the POSIX descriptor-safe path.
function windowsReadCapability(advertised, readable, usable) {
  return Object.freeze({ advertised, reason: usable.size ? 'workspace_configured' : readable.size ? 'workspace_path_invalid' : 'workspace_unconfigured', profile: 'windows_read_only' });
}

// Schema/evaluation catalog only. Production must use
// createLocalToolRegistry(), which applies the configured-capability filter.
export function createLocalToolDefinitionCatalog() {
  return { [timeNowDefinition.name]: timeNowDefinition, ...systemDefinitions, ...filesystemDefinitions, [processDefinition.name]: processDefinition };
}

export function createLocalToolRegistry({ workspaces = [], applications = {}, process_actions: processActions = {}, processEnvironment = {}, browserExecutable, platform, networkProvider = 'disabled', grantControl } = {}) {
  const effectivePlatform = platform ?? process.platform;
  const policy = new WorkspacePolicy(workspaces); const { readable, writable } = workspaceSets(workspaces); const patchable = new Set([...writable].filter(id => readable.has(id)));
  // On win32 the POSIX implementation stays fail-closed (no O_NOFOLLOW) and
  // writes stay unavailable; reads use the Windows policy, and only for
  // workspaces the operator configured with a local drive path.  There is
  // never a default workspace.
  const windows = effectivePlatform === 'win32';
  const filesystemSupported = !windows;
  const windowsPolicy = windows && workspaces.length ? new WindowsWorkspacePolicy(workspaces) : null;
  const windowsReadable = new Set(windowsPolicy ? [...readable].filter(id => windowsPolicy.isUsable(id)) : []);
  const readableForTools = windows ? windowsReadable : readable;
  const filesystem = !workspaces.length ? {} : windows ? createWindowsReadOnlyFilesystemTools(windowsPolicy) : createFilesystemTools(policy, { platform: effectivePlatform, grantControl });
  const system = createSystemTools({ applications, browserExecutable, platform: effectivePlatform, networkProvider, grantControl });
  const actionEntries = Object.values(processActions.actions ?? {});
  const processConfigured = effectivePlatform !== 'win32' && processActions.enabled === true && actionEntries.length > 0 && actionEntries.every(action => writable.has(action.workspace_id));
  const processProvider = new ProcessRunProvider({ enabled: processConfigured, actions: processActions.actions ?? {}, workspacePolicy: policy, grantControl, platform: effectivePlatform, environment: processEnvironment });
  const registry = {
    'time.now': { ...timeNowDefinition, execute: ({ id, arguments: args }) => timeNowTool({ id, arguments: args }) },
    'system.get_info': system['system.get_info'],
    ...(effectivePlatform !== 'win32' && Object.keys(applications).length ? { 'app.open': system['app.open'] } : {}),
    ...(effectivePlatform !== 'win32' && networkProvider === 'browser_open' ? { 'browser.open_url': system['browser.open_url'] } : {}),
    ...(readableForTools.size ? Object.fromEntries(['fs.list', 'fs.read_text', 'fs.search_text'].filter(name => filesystem[name]).map(name => [name, filesystem[name]])) : {}),
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
    'fs.list': windows ? windowsReadCapability(Boolean(filesystem['fs.list'] && windowsReadable.size), readable, windowsReadable) : filesystemCapability(Boolean(filesystem['fs.list'] && readable.size), readable.size ? 'workspace_configured' : 'workspace_unconfigured', false),
    'fs.read_text': windows ? windowsReadCapability(Boolean(filesystem['fs.read_text'] && windowsReadable.size), readable, windowsReadable) : filesystemCapability(Boolean(filesystem['fs.read_text'] && readable.size), readable.size ? 'workspace_configured' : 'workspace_unconfigured', false),
    'fs.search_text': windows ? windowsReadCapability(Boolean(filesystem['fs.search_text'] && windowsReadable.size), readable, windowsReadable) : filesystemCapability(Boolean(filesystem['fs.search_text'] && readable.size), readable.size ? 'workspace_configured' : 'workspace_unconfigured', false),
    'fs.write_new': filesystemCapability(Boolean(filesystem['fs.write_new'] && writable.size), !filesystemSupported ? WINDOWS_FILESYSTEM_STATUS.reason : writable.size ? 'writable_workspace_configured' : 'writable_workspace_unconfigured', !filesystemSupported),
    'fs.apply_patch': filesystemCapability(Boolean(filesystem['fs.apply_patch'] && patchable.size), !filesystemSupported ? WINDOWS_FILESYSTEM_STATUS.reason : patchable.size ? 'readable_writable_workspace_configured' : 'readable_writable_workspace_unconfigured', !filesystemSupported),
    'process.run_allowlisted': Object.freeze({ advertised: processConfigured, reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary' : processConfigured ? 'actions_configured' : processActions.enabled === true ? 'actions_or_writable_workspace_unconfigured' : 'provider_disabled' })
  };
  Object.defineProperty(registry, 'capabilitySnapshot', { enumerable: false, value: freezeCapabilitySnapshot(effectivePlatform, states), writable: false, configurable: false });
  return registry;
}

export { WorkspacePolicy } from './workspace-policy.mjs';
export { createFilesystemTools } from './filesystem.mjs';
export { createWindowsReadOnlyFilesystemTools, WindowsWorkspacePolicy } from './windows-filesystem.mjs';
export { createSystemTools } from './system-tools.mjs';
export { createProcessRunTools, ProcessRunProvider } from './process-run.mjs';
