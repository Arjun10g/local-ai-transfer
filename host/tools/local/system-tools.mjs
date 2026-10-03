import os from 'node:os';
import { isIP } from 'node:net';
import { spawn } from 'node:child_process';
import { makeToolResult } from '../../agent/tool-envelope.mjs';
import { validateToolArguments } from './argument-validation.mjs';
import { applyOperatorGrantPolicy } from '../../providers/operator-tool-policy.mjs';
import { canonicalWindowsDrivePath } from './windows-filesystem.mjs';

const result = (call, status, text, truncated = false) => makeToolResult({ id: call.id, name: call.name, status, text, truncated });
const safeId = /^[A-Za-z0-9_.-]{1,64}$/;

// Clipboard subprocess boundary (Windows only).  The default-browser
// fallback uses the same absolute System32 resolution for rundll32.exe.
//
// Executable: CreateProcess searches the application directory and the
// current directory before PATH for a bare name, so `powershell.exe` could run
// a planted binary.  The absolute System32 path is derived from SystemRoot,
// validated as a local drive path (no UNC/device/verbatim/relative/`..`), with
// a constant fallback.  An unset or ambiguous value never widens the search.
//
// Encoding: clip.exe interprets stdin in the OEM code page and Get-Clipboard
// output goes through the console code page, both of which mangle non-ASCII.
// Both scripts therefore move raw UTF-8 bytes over the standard streams and
// never touch console encodings.  Scripts are fixed and passed with
// -EncodedCommand so no quoting reaches PowerShell's command-line parser;
// clipboard text only ever travels on stdin/stdout, never on the command line.
//
// Command resolution: cmdlets are module-qualified, -NoProfile skips profile
// scripts, and PSModulePath is pinned to $PSHOME\Modules so a module earlier
// in an inherited PSModulePath cannot shadow Get-/Set-Clipboard.
const WINDOWS_FALLBACK_ROOT = 'C:\\Windows';
const CLIPBOARD_MAX_CHARS = 65536;
const CLIPBOARD_READ_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  '$t = Microsoft.PowerShell.Management\\Get-Clipboard -Raw',
  "if ($null -eq $t) { $t = '' }",
  `if ($t.Length -gt ${CLIPBOARD_MAX_CHARS}) { $t = $t.Substring(0, ${CLIPBOARD_MAX_CHARS}) }`,
  '$b = [System.Text.Encoding]::UTF8.GetBytes([string]$t)',
  '$o = [System.Console]::OpenStandardOutput()',
  '$o.Write($b, 0, $b.Length)',
  '$o.Flush()'
].join('; ');
const CLIPBOARD_WRITE_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  '$i = [System.Console]::OpenStandardInput()',
  '$m = New-Object System.IO.MemoryStream',
  '$i.CopyTo($m)',
  '$t = (New-Object System.Text.UTF8Encoding($false, $true)).GetString($m.ToArray())',
  'Microsoft.PowerShell.Management\\Set-Clipboard -Value $t'
].join('; ');
const encodedCommand = script => Buffer.from(script, 'utf16le').toString('base64');
const POWERSHELL_FLAGS = Object.freeze(['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Restricted', '-EncodedCommand']);
export const CLIPBOARD_READ_ARGS = Object.freeze([...POWERSHELL_FLAGS, encodedCommand(CLIPBOARD_READ_SCRIPT)]);
export const CLIPBOARD_WRITE_ARGS = Object.freeze([...POWERSHELL_FLAGS, encodedCommand(CLIPBOARD_WRITE_SCRIPT)]);

// process.env is case-insensitive on Windows; an injected object is not, so
// every spelling is collected and disagreement falls back to the constant.
function environmentValue(environment, name) {
  const values = new Set(Object.keys(environment ?? {}).filter(key => key.toUpperCase() === name).map(key => environment[key]));
  return values.size === 1 ? [...values][0] : undefined;
}
export function windowsSystemRoot(environment = process.env) {
  try { return canonicalWindowsDrivePath(environmentValue(environment, 'SYSTEMROOT')); } catch { return WINDOWS_FALLBACK_ROOT; }
}
export function windowsPowerShellPath(environment = process.env) { return `${windowsSystemRoot(environment)}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`; }
// Other fixed System32 utilities (taskkill.exe for process-tree cleanup)
// resolve the same way, for the same reason.  Only a plain `name.exe` is
// accepted, so no caller can steer the path out of System32.
export function windowsSystem32Path(name, environment = process.env) {
  if (typeof name !== 'string' || !/^[A-Za-z0-9_-]{1,64}\.exe$/u.test(name)) throw new TypeError('invalid System32 executable name');
  return `${windowsSystemRoot(environment)}\\System32\\${name}`;
}
// app.open runs only an absolute local path: a bare name would be resolved
// by CreateProcess (current directory first) or by PATH, so whatever sits
// there, not what the operator configured, would run.  UNC and `//` paths
// are refused because they reach the network.
export function isAbsoluteLocalExecutable(value) {
  return typeof value === 'string' && value.length >= 2 && value.length <= 1024 && !/[\u0000-\u001f\u007f]/u.test(value) && (/^\/(?!\/)/u.test(value) || /^[A-Za-z]:[\\/]/u.test(value));
}
// A launched application or browser keeps the operator's ordinary
// environment, minus the host's own LAE_* settings (the engine bearer token,
// the journal descriptor): none of that is a launched program's business.
export function launchEnvironment(environment = process.env) {
  return Object.fromEntries(Object.entries(environment ?? {}).filter(([key]) => !/^LAE_/iu.test(key)));
}
// Minimal child environment: profile-location variables are passed only when
// they are plain local drive paths so PowerShell can start; nothing else
// (PATH, PSModulePath, proxies, tokens) is inherited.
export function clipboardEnvironment(environment = process.env) {
  const root = windowsSystemRoot(environment);
  const child = { SystemRoot: root, windir: root, PSModulePath: `${root}\\System32\\WindowsPowerShell\\v1.0\\Modules` };
  for (const name of ['USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP']) { try { child[name] = canonicalWindowsDrivePath(environmentValue(environment, name)); } catch {} }
  return child;
}

export const systemDefinitions = Object.freeze({
  'system.get_info': { name: 'system.get_info', version: '1.0.0', risk_tier: 'T0', side_effect: 'none', network: false, requires_confirmation: false, timeout_ms: 1000, output_limit: 8192 },
  'clipboard.read': { name: 'clipboard.read', version: '1.0.0', risk_tier: 'T1', side_effect: 'read_sensitive', network: false, requires_confirmation: false, timeout_ms: 3000, output_limit: 65536 },
  'clipboard.write': { name: 'clipboard.write', version: '1.0.0', risk_tier: 'T2', side_effect: 'write_sensitive', network: false, requires_confirmation: true, timeout_ms: 3000, output_limit: 4096 },
  'app.open': { name: 'app.open', version: '1.0.0', risk_tier: 'T1', side_effect: 'launch', network: false, requires_confirmation: true, timeout_ms: 5000, output_limit: 4096 },
  'browser.open_url': { name: 'browser.open_url', version: '1.0.0', risk_tier: 'T1', side_effect: 'external_navigation', network: true, data_egress: 'external_destination', requires_confirmation: true, timeout_ms: 5000, output_limit: 4096 }
});

// Bytes, not characters, are bounded; decoding happens once at the end with
// a streaming decoder so a multi-byte character split across chunks (or cut
// by the bound) is dropped rather than turned into mojibake.
function spawnBounded(executable, args, { input, timeoutMs = 3000, maxOutput = 65536, env, spawnImpl = spawn } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawnImpl(executable, args, { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], ...(env ? { env } : {}) }); const chunks = []; let bytes = 0; let truncated = false; let stderrBytes = 0; const timer = setTimeout(() => { child.kill(); reject(Object.assign(new Error('tool_timeout'), { code: 'tool_timeout' })); }, timeoutMs);
    child.stdout.on('data', chunk => { const room = maxOutput - bytes; if (chunk.length > room) truncated = true; if (room > 0) { const kept = chunk.subarray(0, room); chunks.push(kept); bytes += kept.length; } });
    // stderr is drained (so the child cannot block on a full pipe) but never
    // returned: it can echo clipboard content in error text.
    child.stderr.on('data', chunk => { stderrBytes += chunk.length; });
    // An early child exit makes the stdin write fail with EPIPE; without a
    // listener that error would be thrown as an uncaught exception.
    child.stdin.on('error', () => {});
    child.once('error', error => { clearTimeout(timer); reject(error); }); child.once('close', code => { clearTimeout(timer); if (code === 0) resolve({ stdout: new TextDecoder('utf-8').decode(Buffer.concat(chunks), { stream: true }), truncated, stderrBytes }); else reject(Object.assign(new Error('tool_process_failed'), { code: 'tool_process_failed', exitCode: code })); });
    if (input !== undefined) child.stdin.end(input); else child.stdin.end();
  });
}

function spawnVisible(executable, args, spawnImpl = spawn, env = launchEnvironment()) {
  return new Promise((resolve, reject) => { const child = spawnImpl(executable, args, { shell: false, windowsHide: true, stdio: 'ignore', env }); child.once('spawn', () => resolve(child)); child.once('error', reject); });
}

function safeUrl(input) {
  if (typeof input !== 'string' || input.length > 2048) throw Object.assign(new Error('invalid_url'), { code: 'invalid_url' });
  let url; try { url = new URL(input); } catch { throw Object.assign(new Error('invalid_url'), { code: 'invalid_url' }); }
  if (url.protocol !== 'https:' || url.username || url.password || url.hostname === 'localhost' || url.hostname.endsWith('.local') || url.hostname.endsWith('.localhost')) throw Object.assign(new Error('unsafe_url'), { code: 'unsafe_url' });
  if (isIP(url.hostname) || /(?:\.internal|\.lan|\.home\.arpa)$/.test(url.hostname)) throw Object.assign(new Error('private_url'), { code: 'private_url' });
  return url;
}

export function createSystemTools({ platform = process.platform, applications = {}, browserExecutable, networkProvider = 'disabled', grantControl, environment = process.env, spawn: spawnImpl = spawn } = {}) {
  const info = async call => { validateToolArguments('system.get_info', call.arguments); return result(call, 'ok', JSON.stringify({ platform, arch: process.arch, node: process.version, cpus: os.cpus().length, memory_bytes: { total: os.totalmem(), free: os.freemem() }, uptime_seconds: Math.floor(os.uptime()) })); };
  const clipboardRead = async call => { validateToolArguments('clipboard.read', call.arguments); if (platform !== 'win32') return result(call, 'failed', JSON.stringify({ code: 'platform_unsupported', message: 'clipboard integration requires Windows' })); try { const output = await spawnBounded(windowsPowerShellPath(environment), CLIPBOARD_READ_ARGS, { maxOutput: 65536, env: clipboardEnvironment(environment), spawnImpl }); return result(call, 'ok', JSON.stringify({ text: output.stdout, sensitive: true, truncated: output.truncated }), output.truncated); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'clipboard_unavailable' })); } };
  const clipboardWrite = async call => { const { text } = validateToolArguments('clipboard.write', call.arguments); if (platform !== 'win32') return result(call, 'failed', JSON.stringify({ code: 'platform_unsupported', message: 'clipboard integration requires Windows' })); try { await spawnBounded(windowsPowerShellPath(environment), CLIPBOARD_WRITE_ARGS, { input: Buffer.from(text, 'utf8'), maxOutput: 1024, env: clipboardEnvironment(environment), spawnImpl }); return result(call, 'ok', JSON.stringify({ written: true, bytes: Buffer.byteLength(text), sensitive: true })); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'clipboard_unavailable' })); } };
  const appOpen = async call => { const { app_id: id } = validateToolArguments('app.open', call.arguments); if (!safeId.test(id) || !applications[id]) throw Object.assign(new Error('unknown_app'), { code: 'unknown_app' }); const entry = applications[id]; if (!safeId.test(entry.executable_id ?? id) || !isAbsoluteLocalExecutable(entry.executable) || !Array.isArray(entry.args) || entry.args.some(arg => typeof arg !== 'string')) throw new Error('invalid_app_allowlist'); try { const launched = await spawnVisible(entry.executable, entry.args, spawnImpl, launchEnvironment(environment)); return result(call, 'ok', JSON.stringify({ opened: true, app_id: id, executable_id: entry.executable_id ?? id, pid: launched.pid })); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'app_open_failed' })); } };
  const browserPreview = async call => { const { url: rawUrl } = validateToolArguments('browser.open_url', call.arguments); const url = safeUrl(rawUrl); return { destination: url.href, provider: networkProvider, data_egress: 'external_navigation' }; };
  const browserOpen = async call => { const { url: rawUrl } = validateToolArguments('browser.open_url', call.arguments); const url = safeUrl(rawUrl); if (networkProvider !== 'browser_open') return result(call, 'failed', JSON.stringify({ code: 'provider_disabled', destination: url.href, provider: networkProvider })); const executable = browserExecutable ?? (platform === 'win32' ? `${windowsSystemRoot(environment)}\\System32\\rundll32.exe` : platform === 'darwin' ? 'open' : 'xdg-open'); const args = platform === 'win32' ? ['url.dll,FileProtocolHandler', url.href] : [url.href]; try { const launched = await spawnVisible(executable, args, spawnImpl, launchEnvironment(environment)); return result(call, 'ok', JSON.stringify({ opened: true, scheme: url.protocol, host: url.hostname, pid: launched.pid })); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'browser_open_failed' })); } };
  const tools = { 'system.get_info': { ...systemDefinitions['system.get_info'], execute: info }, 'clipboard.read': { ...systemDefinitions['clipboard.read'], execute: clipboardRead }, 'clipboard.write': { ...systemDefinitions['clipboard.write'], execute: clipboardWrite }, 'app.open': { ...systemDefinitions['app.open'], execute: appOpen }, 'browser.open_url': { ...systemDefinitions['browser.open_url'], preview: browserPreview, execute: browserOpen } };
  tools['clipboard.write'] = applyOperatorGrantPolicy(tools['clipboard.write'], { grantControl, capabilityForCall: () => 'local.clipboard' });
  tools['app.open'] = applyOperatorGrantPolicy(tools['app.open'], { grantControl, capabilityForCall: call => `local.application:${call.arguments.app_id}` });
  return tools;
}
