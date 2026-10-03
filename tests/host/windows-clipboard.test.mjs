// Clipboard subprocess boundary, exercised on POSIX through an injected
// spawn.  These tests pin what Node hands to CreateProcess (absolute image
// path, argv, environment, stdin bytes) and how output is bounded/decoded.
// Whether Windows PowerShell 5.1 actually behaves as the fixed scripts assume
// (raw stdio streams, Set-Clipboard with UTF-8 text, start-up with the
// minimal environment) is NOT verified here.
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { CLIPBOARD_READ_ARGS, CLIPBOARD_WRITE_ARGS, clipboardEnvironment, createSystemTools, windowsPowerShellPath, windowsSystemRoot } from '../../host/tools/local/system-tools.mjs';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';

const call = (name, arguments_) => ({ id: 'call_clip01', name, arguments: arguments_ });
const payload = output => JSON.parse(output.content[0].text);
const decodeScript = args => Buffer.from(args.at(-1), 'base64').toString('utf16le');

function fakeSpawn({ stdoutChunks = [], exitCode = 0, stderr = '', stdinError = false } = {}) {
  const launches = [];
  const spawn = (executable, args, options) => {
    const child = new EventEmitter(); child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.stdin = new PassThrough(); child.kill = () => {};
    const stdin = []; child.stdin.on('data', chunk => stdin.push(chunk));
    launches.push({ executable, args, options, stdin: () => Buffer.concat(stdin) });
    setImmediate(() => {
      if (stdinError) child.stdin.emit('error', Object.assign(new Error('write EPIPE'), { code: 'EPIPE' }));
      for (const chunk of stdoutChunks) child.stdout.write(chunk);
      if (stderr) child.stderr.write(stderr);
      setImmediate(() => child.emit('close', exitCode));
    });
    return child;
  };
  return { spawn, launches };
}

test('clipboard.read launches PowerShell by absolute System32 path with fixed encoded script and minimal environment', async () => {
  const secretText = '密码 ünïcödé 🔑 line\r\nend';
  const bytes = Buffer.from(secretText, 'utf8');
  // Split inside a 4-byte emoji and a 3-byte CJK character.
  const { spawn, launches } = fakeSpawn({ stdoutChunks: [bytes.subarray(0, 2), bytes.subarray(2, 17), bytes.subarray(17)] });
  const environment = { SystemRoot: 'C:\\WINDOWS', PATH: 'C:\\planted;C:\\Windows', PSModulePath: 'C:\\Users\\Op\\evil-modules', USERPROFILE: 'C:\\Users\\Op', TEMP: '\\\\server\\share\\tmp', GITHUB_TOKEN: 'ghp_secret' };
  const tools = createSystemTools({ platform: 'win32', environment, spawn });
  const result = payload(await tools['clipboard.read'].execute(call('clipboard.read', {})));
  assert.equal(result.text, secretText); assert.equal(result.sensitive, true); assert.equal(result.truncated, false);
  const [launch] = launches;
  assert.equal(launch.executable, 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
  assert.deepEqual(launch.args, [...CLIPBOARD_READ_ARGS]);
  assert.deepEqual(launch.args.slice(0, -1), ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Restricted', '-EncodedCommand']);
  assert.equal(launch.options.shell, false); assert.equal(launch.options.windowsHide, true);
  assert.deepEqual(launch.options.env, { SystemRoot: 'C:\\WINDOWS', windir: 'C:\\WINDOWS', PSModulePath: 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\Modules', USERPROFILE: 'C:\\Users\\Op' });
  const script = decodeScript(launch.args);
  assert.match(script, /Microsoft\.PowerShell\.Management\\Get-Clipboard -Raw/u);
  assert.match(script, /OpenStandardOutput/u); assert.match(script, /UTF8\.GetBytes/u);
  assert.doesNotMatch(script, /clip\.exe|Out-String|Write-Output/u);
});

test('hostile or ambiguous SystemRoot values never produce a bare or non-local executable path', () => {
  for (const value of [undefined, '', 'Windows', '.\\Windows', 'C:Windows', '\\\\attacker\\share\\Windows', '//attacker/share', '\\\\?\\C:\\Windows', '\\\\.\\C:\\Windows', 'C:\\Windows\\..\\Users\\Op\\Downloads', 'C:\\Windows:ads', 'C:\\Windows.', 'C:\\', 'C:\\Win\u0000dows', 42]) {
    assert.equal(windowsSystemRoot({ SystemRoot: value }), 'C:\\Windows', String(value));
    assert.equal(windowsPowerShellPath({ SystemRoot: value }), 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
  }
  assert.equal(windowsSystemRoot({ SystemRoot: 'D:\\Win', SYSTEMROOT: 'C:\\Users\\Op\\fake' }), 'C:\\Windows', 'conflicting spellings fall back');
  assert.equal(windowsSystemRoot({ SYSTEMROOT: 'd:/Windows/' }), 'D:\\Windows');
  assert.equal(clipboardEnvironment({ SystemRoot: '\\\\attacker\\share' }).PSModulePath, 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules');
});

test('clipboard.write sends exact UTF-8 bytes on stdin and never on the command line', async () => {
  const { spawn, launches } = fakeSpawn();
  const text = 'pässwörd → 秘密 🔐 "quoted" $(Invoke-Evil) `n';
  const tools = createSystemTools({ platform: 'win32', environment: { SystemRoot: 'C:\\Windows' }, spawn });
  const result = payload(await tools['clipboard.write'].execute(call('clipboard.write', { text })));
  assert.deepEqual(result, { written: true, bytes: Buffer.byteLength(text), sensitive: true });
  const [launch] = launches;
  assert.equal(launch.executable, 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
  assert.deepEqual(launch.args, [...CLIPBOARD_WRITE_ARGS]);
  assert.ok(launch.stdin().equals(Buffer.from(text, 'utf8')));
  assert.equal(launch.args.some(arg => arg.includes('Invoke-Evil') || arg.includes('pässwörd')), false);
  const script = decodeScript(launch.args);
  assert.match(script, /OpenStandardInput/u); assert.match(script, /UTF8Encoding\(\$false, \$true\)/u); assert.match(script, /Microsoft\.PowerShell\.Management\\Set-Clipboard -Value \$t/u);
});

test('output is byte-bounded without splitting a character, and failures never echo stderr', async () => {
  const big = Buffer.from('€'.repeat(30000), 'utf8'); // 90000 bytes, 3 per char
  const { spawn } = fakeSpawn({ stdoutChunks: [big.subarray(0, 40000), big.subarray(40000)] });
  const read = await createSystemTools({ platform: 'win32', environment: {}, spawn })['clipboard.read'].execute(call('clipboard.read', {}));
  const result = payload(read);
  assert.equal(result.truncated, true); assert.equal(read.metadata.truncated, true);
  assert.ok(Buffer.byteLength(result.text, 'utf8') <= 65536); assert.equal(result.text, '€'.repeat(21845)); assert.equal(result.text.includes('\uFFFD'), false);
  const failing = fakeSpawn({ exitCode: 1, stderr: 'Set-Clipboard : hunter2 could not be written' });
  const failed = payload(await createSystemTools({ platform: 'win32', environment: {}, spawn: failing.spawn })['clipboard.write'].execute(call('clipboard.write', { text: 'hunter2' })));
  assert.deepEqual(failed, { code: 'tool_process_failed' });
});

test('an early child exit (EPIPE on stdin) is contained instead of crashing the host', async () => {
  const { spawn } = fakeSpawn({ exitCode: 1, stdinError: true });
  const failed = payload(await createSystemTools({ platform: 'win32', environment: {}, spawn })['clipboard.write'].execute(call('clipboard.write', { text: 'x' })));
  assert.equal(failed.code, 'tool_process_failed');
});

test('non-Windows stays offline without spawning, and the win32 registry still withholds clipboard tools', async () => {
  const { spawn, launches } = fakeSpawn();
  const tools = createSystemTools({ platform: 'linux', spawn });
  assert.equal(payload(await tools['clipboard.read'].execute(call('clipboard.read', {}))).code, 'platform_unsupported');
  assert.equal(payload(await tools['clipboard.write'].execute(call('clipboard.write', { text: 'x' }))).code, 'platform_unsupported');
  assert.equal(launches.length, 0);
  // Registration is a separate policy decision (see report): clipboard.read
  // is T1 read_sensitive with requires_confirmation false, so registering it
  // would let the model pull the clipboard into context without the operator
  // seeing a confirmation; clipboard.write is durable and needs the journal.
  const registry = createLocalToolRegistry({ platform: 'win32', workspaces: [{ id: 'project', path: 'C:\\Users\\Op\\Project' }] });
  for (const name of ['clipboard.read', 'clipboard.write']) { assert.equal(Object.hasOwn(registry, name), false); assert.equal(registry.capabilitySnapshot.tools[name].reason, 'unsafe_subprocess_boundary'); }
});

test('the Windows default-browser fallback uses an absolute System32 rundll32 path, never a bare name', async () => {
  const launches = [];
  const spawn = (executable, args, options) => { launches.push({ executable, args, options }); const child = new EventEmitter(); setImmediate(() => child.emit('spawn')); return child; };
  const tools = createSystemTools({ platform: 'win32', networkProvider: 'browser_open', environment: { SystemRoot: '.\\planted' }, spawn });
  assert.equal(payload(await tools['browser.open_url'].execute(call('browser.open_url', { url: 'https://example.com/path' }))).opened, true);
  assert.deepEqual(launches.map(launch => [launch.executable, launch.args, launch.options.shell]), [['C:\\Windows\\System32\\rundll32.exe', ['url.dll,FileProtocolHandler', 'https://example.com/path'], false]]);
});
