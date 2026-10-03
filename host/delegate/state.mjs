// The per-user state directory the delegate bridge reads to find this host.
//
// Two files live there and nothing else is ever written by the host:
//   host.json     {version, port, pid, started_at}: where the host listens.
//                 No secret: the port alone opens nothing.
//   delegate-key  32 random bytes, base64url: the only credential the
//                 delegate routes accept.  Separate from the per-launch UI
//                 bearer and the engine token, and stable across restarts, so
//                 the operator pastes it into the Copilot client once.
//
// The directory is the security boundary for the key: it must belong to this
// user and be closed to everyone else.  On POSIX that is checked (owner,
// mode, not a symlink).  On Windows Node cannot read an ACL; the default
// location is under %LOCALAPPDATA%, whose inherited ACL is per-user, and the
// only thing checked is that the directory is a real directory and not a
// junction or symlink that could redirect it somewhere shared.
import { randomBytes } from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import { chmod, link, lstat, mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';

export const HOST_FILE = 'host.json';
export const KEY_FILE = 'delegate-key';
export const HOST_FILE_VERSION = 1;
export const DELEGATE_KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export class DelegateStateError extends Error {
  constructor(code, message) { super(message); this.name = 'DelegateStateError'; this.code = code; }
}

/**
 * Where the state lives for this user.  `BMO_STATE_DIR` overrides (tests, a
 * portable install); it must be absolute so it cannot silently follow the
 * working directory.  A relative XDG_STATE_HOME is ignored, as the XDG spec
 * requires.
 */
export function resolveStateDir({ env = process.env, platform = process.platform, home = homedir() } = {}) {
  const override = env.BMO_STATE_DIR;
  if (override !== undefined && override !== '') {
    if (!isAbsolute(override)) throw new DelegateStateError('state_dir_invalid', 'BMO_STATE_DIR must be an absolute path');
    return override;
  }
  if (platform === 'win32') {
    const local = env.LOCALAPPDATA;
    if (typeof local !== 'string' || !/^[A-Za-z]:[\\/]/u.test(local)) throw new DelegateStateError('state_dir_invalid', 'LOCALAPPDATA is not set to a local drive path');
    return `${local.replace(/[\\/]+$/u, '')}\\BMO`;
  }
  if (typeof home !== 'string' || !isAbsolute(home)) throw new DelegateStateError('state_dir_invalid', 'home directory is unavailable');
  if (platform === 'darwin') return join(home, 'Library', 'Application Support', 'BMO');
  const xdg = env.XDG_STATE_HOME;
  return join(typeof xdg === 'string' && isAbsolute(xdg) ? xdg : join(home, '.local', 'state'), 'bmo');
}

/**
 * Create the directory (0700) or verify an existing one.  Returns what could
 * and could not be verified, so status output never claims an ACL check that
 * did not happen.
 */
export async function ensureStateDir(dir, { platform = process.platform } = {}) {
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const info = await lstat(dir);
  if (info.isSymbolicLink() || !info.isDirectory()) throw new DelegateStateError('state_dir_unsafe', 'the BMO state directory is not a plain directory');
  if (platform === 'win32') return { owner_verified: false, mode_verified: false };
  if (typeof process.getuid === 'function' && info.uid !== process.getuid()) throw new DelegateStateError('state_dir_unsafe', 'the BMO state directory belongs to another user');
  // Ours but too open (an old install, a hand-made folder): close it rather
  // than refuse, since only the owner could have opened it.
  if ((info.mode & 0o077) !== 0) await chmod(dir, 0o700);
  return { owner_verified: typeof process.getuid === 'function', mode_verified: true };
}

// Refuse a key file another user could read or that is not a plain file.
async function checkPrivateFile(path, platform) {
  const info = await lstat(path);
  if (info.isSymbolicLink() || !info.isFile()) throw new DelegateStateError('delegate_key_unsafe', 'the delegate key is not a plain file');
  if (platform === 'win32') return;
  if (typeof process.getuid === 'function' && info.uid !== process.getuid()) throw new DelegateStateError('delegate_key_unsafe', 'the delegate key belongs to another user');
  if ((info.mode & 0o077) !== 0) await chmod(path, 0o600);
}

// Write a private file through a temporary name in the same directory, so a
// reader sees either the old content or the new, never half of either.
async function writePrivateTemp(dir, base, text) {
  const temp = join(dir, `${base}.${randomBytes(8).toString('hex')}.tmp`);
  let handle;
  try {
    handle = await open(temp, fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL, 0o600);
    await handle.writeFile(text, 'utf8'); await handle.sync();
  } catch (error) { await handle?.close().catch(() => {}); handle = null; await unlink(temp).catch(() => {}); throw error; }
  await handle.close();
  return temp;
}

export function newDelegateKey() { return randomBytes(32).toString('base64url'); }

/**
 * The stored key, creating it on first use.  Creation never replaces a key
 * that appeared meanwhile (a second launcher racing this one): the temporary
 * file is hard-linked into place, which fails if the name exists.
 */
export async function loadOrCreateDelegateKey(dir, { platform = process.platform } = {}) {
  const path = join(dir, KEY_FILE);
  const existing = await readDelegateKey(dir, { platform });
  if (existing) return existing;
  const key = newDelegateKey();
  const temp = await writePrivateTemp(dir, KEY_FILE, `${key}\n`);
  try { await link(temp, path); }
  catch (error) { if (error?.code !== 'EEXIST') throw error; }
  finally { await unlink(temp).catch(() => {}); }
  const stored = await readDelegateKey(dir, { platform });
  if (!stored) throw new DelegateStateError('delegate_key_invalid', 'the delegate key file is unreadable');
  return stored;
}

/** Replace the key.  Whatever held the old one stops working at once. */
export async function rotateDelegateKey(dir, { platform = process.platform } = {}) {
  await ensureStateDir(dir, { platform });
  const key = newDelegateKey();
  const temp = await writePrivateTemp(dir, KEY_FILE, `${key}\n`);
  try { await rename(temp, join(dir, KEY_FILE)); } catch (error) { await unlink(temp).catch(() => {}); throw error; }
  return key;
}

/**
 * The current key, or null when there is none.  A malformed or unsafe file
 * is an error, not "no key": the operator must rotate it deliberately.
 */
export async function readDelegateKey(dir, { platform = process.platform } = {}) {
  const path = join(dir, KEY_FILE);
  let text;
  try { await checkPrivateFile(path, platform); text = await readFile(path, 'utf8'); }
  catch (error) { if (error?.code === 'ENOENT') return null; throw error; }
  const key = text.trim();
  if (!DELEGATE_KEY_PATTERN.test(key)) throw new DelegateStateError('delegate_key_invalid', 'the delegate key file is malformed; rotate it');
  return key;
}

/** Atomic, 0600, overwrites a stale file left by a crashed host. */
export async function writeHostFile(dir, { port, pid = process.pid, startedAt = new Date() } = {}) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new DelegateStateError('host_file_invalid', 'port is invalid');
  const record = { version: HOST_FILE_VERSION, port, pid, started_at: startedAt.toISOString() };
  const temp = await writePrivateTemp(dir, HOST_FILE, `${JSON.stringify(record)}\n`);
  try { await rename(temp, join(dir, HOST_FILE)); } catch (error) { await unlink(temp).catch(() => {}); throw error; }
  return record;
}

/**
 * Remove host.json on clean shutdown, but only the one this process wrote: a
 * second host started later owns the file now and must keep it.
 */
export async function removeHostFile(dir, { pid = process.pid, port } = {}) {
  const path = join(dir, HOST_FILE);
  let record;
  try { record = JSON.parse(await readFile(path, 'utf8')); } catch { return false; }
  if (record?.pid !== pid || (port !== undefined && record?.port !== port)) return false;
  try { await unlink(path); return true; } catch { return false; }
}
