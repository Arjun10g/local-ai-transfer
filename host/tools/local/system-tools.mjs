import os from 'node:os';
import { isIP } from 'node:net';
import { spawn } from 'node:child_process';
import { makeToolResult } from '../../agent/tool-envelope.mjs';
import { validateToolArguments } from './argument-validation.mjs';
import { applyOperatorGrantPolicy } from '../../providers/operator-tool-policy.mjs';

const result = (call, status, text, truncated = false) => makeToolResult({ id: call.id, name: call.name, status, text, truncated });
const safeId = /^[A-Za-z0-9_.-]{1,64}$/;

export const systemDefinitions = Object.freeze({
  'system.get_info': { name: 'system.get_info', version: '1.0.0', risk_tier: 'T0', side_effect: 'none', network: false, requires_confirmation: false, timeout_ms: 1000, output_limit: 8192 },
  'clipboard.read': { name: 'clipboard.read', version: '1.0.0', risk_tier: 'T1', side_effect: 'read_sensitive', network: false, requires_confirmation: false, timeout_ms: 3000, output_limit: 65536 },
  'clipboard.write': { name: 'clipboard.write', version: '1.0.0', risk_tier: 'T2', side_effect: 'write_sensitive', network: false, requires_confirmation: true, timeout_ms: 3000, output_limit: 4096 },
  'app.open': { name: 'app.open', version: '1.0.0', risk_tier: 'T1', side_effect: 'launch', network: false, requires_confirmation: true, timeout_ms: 5000, output_limit: 4096 },
  'browser.open_url': { name: 'browser.open_url', version: '1.0.0', risk_tier: 'T1', side_effect: 'external_navigation', network: true, data_egress: 'external_destination', requires_confirmation: true, timeout_ms: 5000, output_limit: 4096 }
});

function spawnBounded(executable, args, { input = '', timeoutMs = 3000, maxOutput = 65536 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] }); let stdout = ''; let stderr = ''; let truncated = false; const timer = setTimeout(() => { child.kill(); reject(Object.assign(new Error('tool_timeout'), { code: 'tool_timeout' })); }, timeoutMs);
    const collect = (target, chunk) => { if (target.length >= maxOutput) { truncated = true; return target; } const text = chunk.toString('utf8'); const room = maxOutput - target.length; if (text.length > room) truncated = true; return target + text.slice(0, room); };
    child.stdout.on('data', chunk => { stdout = collect(stdout, chunk); }); child.stderr.on('data', chunk => { stderr = collect(stderr, chunk); }); child.once('error', error => { clearTimeout(timer); reject(error); }); child.once('close', code => { clearTimeout(timer); if (code === 0) resolve({ stdout, stderr, truncated }); else reject(Object.assign(new Error('tool_process_failed'), { code: 'tool_process_failed', exitCode: code, stderr })); });
    if (input) child.stdin.end(input); else child.stdin.end();
  });
}

function spawnVisible(executable, args) {
  return new Promise((resolve, reject) => { const child = spawn(executable, args, { shell: false, windowsHide: true, stdio: 'ignore' }); child.once('spawn', () => resolve(child)); child.once('error', reject); });
}

function safeUrl(input) {
  if (typeof input !== 'string' || input.length > 2048) throw Object.assign(new Error('invalid_url'), { code: 'invalid_url' });
  let url; try { url = new URL(input); } catch { throw Object.assign(new Error('invalid_url'), { code: 'invalid_url' }); }
  if (url.protocol !== 'https:' || url.username || url.password || url.hostname === 'localhost' || url.hostname.endsWith('.local') || url.hostname.endsWith('.localhost')) throw Object.assign(new Error('unsafe_url'), { code: 'unsafe_url' });
  if (isIP(url.hostname) || /(?:\.internal|\.lan|\.home\.arpa)$/.test(url.hostname)) throw Object.assign(new Error('private_url'), { code: 'private_url' });
  return url;
}

export function createSystemTools({ platform = process.platform, applications = {}, browserExecutable, networkProvider = 'disabled', grantControl } = {}) {
  const info = async call => { validateToolArguments('system.get_info', call.arguments); return result(call, 'ok', JSON.stringify({ platform, arch: process.arch, node: process.version, cpus: os.cpus().length, memory_bytes: { total: os.totalmem(), free: os.freemem() }, uptime_seconds: Math.floor(os.uptime()) })); };
  const clipboardRead = async call => { validateToolArguments('clipboard.read', call.arguments); if (platform !== 'win32') return result(call, 'failed', JSON.stringify({ code: 'platform_unsupported', message: 'clipboard integration requires Windows' })); try { const output = await spawnBounded('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Get-Clipboard -Raw'], { maxOutput: 65536 }); return result(call, 'ok', JSON.stringify({ text: output.stdout, sensitive: true, truncated: output.truncated }), output.truncated); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'clipboard_unavailable' })); } };
  const clipboardWrite = async call => { const { text } = validateToolArguments('clipboard.write', call.arguments); if (platform !== 'win32') return result(call, 'failed', JSON.stringify({ code: 'platform_unsupported', message: 'clipboard integration requires Windows' })); try { await spawnBounded('clip.exe', [], { input: text, maxOutput: 1024 }); return result(call, 'ok', JSON.stringify({ written: true, bytes: Buffer.byteLength(text), sensitive: true })); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'clipboard_unavailable' })); } };
  const appOpen = async call => { const { app_id: id } = validateToolArguments('app.open', call.arguments); if (!safeId.test(id) || !applications[id]) throw Object.assign(new Error('unknown_app'), { code: 'unknown_app' }); const entry = applications[id]; if (!safeId.test(entry.executable_id ?? id) || typeof entry.executable !== 'string' || !Array.isArray(entry.args) || entry.args.some(arg => typeof arg !== 'string')) throw new Error('invalid_app_allowlist'); try { const launched = await spawnVisible(entry.executable, entry.args); return result(call, 'ok', JSON.stringify({ opened: true, app_id: id, executable_id: entry.executable_id ?? id, pid: launched.pid })); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'app_open_failed' })); } };
  const browserPreview = async call => { const { url: rawUrl } = validateToolArguments('browser.open_url', call.arguments); const url = safeUrl(rawUrl); return { destination: url.href, provider: networkProvider, data_egress: 'external_navigation' }; };
  const browserOpen = async call => { const { url: rawUrl } = validateToolArguments('browser.open_url', call.arguments); const url = safeUrl(rawUrl); if (networkProvider !== 'browser_open') return result(call, 'failed', JSON.stringify({ code: 'provider_disabled', destination: url.href, provider: networkProvider })); const executable = browserExecutable ?? (platform === 'win32' ? 'rundll32.exe' : platform === 'darwin' ? 'open' : 'xdg-open'); const args = platform === 'win32' ? ['url.dll,FileProtocolHandler', url.href] : [url.href]; try { const launched = await spawnVisible(executable, args); return result(call, 'ok', JSON.stringify({ opened: true, scheme: url.protocol, host: url.hostname, pid: launched.pid })); } catch (error) { return result(call, 'failed', JSON.stringify({ code: error.code ?? 'browser_open_failed' })); } };
  const tools = { 'system.get_info': { ...systemDefinitions['system.get_info'], execute: info }, 'clipboard.read': { ...systemDefinitions['clipboard.read'], execute: clipboardRead }, 'clipboard.write': { ...systemDefinitions['clipboard.write'], execute: clipboardWrite }, 'app.open': { ...systemDefinitions['app.open'], execute: appOpen }, 'browser.open_url': { ...systemDefinitions['browser.open_url'], preview: browserPreview, execute: browserOpen } };
  tools['clipboard.write'] = applyOperatorGrantPolicy(tools['clipboard.write'], { grantControl, capabilityForCall: () => 'local.clipboard' });
  tools['app.open'] = applyOperatorGrantPolicy(tools['app.open'], { grantControl, capabilityForCall: call => `local.application:${call.arguments.app_id}` });
  return tools;
}
