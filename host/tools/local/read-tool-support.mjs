// Shared behaviour of the read-only filesystem tools (POSIX filesystem.mjs
// and windows-filesystem.mjs): cancellation, recoverable-error results and
// search budgets.  Keeping them in one place keeps the two implementations'
// model-facing contract identical.
import { makeToolResult } from '../../agent/tool-envelope.mjs';
import { WorkspaceError } from './workspace-policy.mjs';

// Cancellation.  The controller aborts call.signal on timeout or user
// cancel, but cannot stop a promise it no longer awaits; without this a
// timed-out search keeps walking the disk in the background and up to
// maxToolCalls of them can stack.  Every filesystem step races the signal so
// the walk stops at the next step even if one primitive never settles.
export const cancelledError = () => Object.assign(new Error('cancelled'), { code: 'cancelled' });
export const isCancelled = error => error?.code === 'cancelled';
export function throwIfAborted(signal) { if (signal?.aborted) throw cancelledError(); }
export async function abortable(promise, signal, onLateResult) {
  if (!signal) return promise;
  throwIfAborted(signal);
  let release;
  const aborted = new Promise((_, reject) => { release = () => reject(cancelledError()); signal.addEventListener('abort', release, { once: true }); });
  try { return await Promise.race([promise, aborted]); }
  catch (error) {
    // A handle that opens after we stopped waiting must still be closed.
    if (isCancelled(error) && onLateResult) promise.then(onLateResult, () => {});
    throw error;
  } finally { signal.removeEventListener('abort', release); }
}

// Outcomes the model can act on (a wrong path or id, a non-text file, a link
// it should not try to follow) become an ordinary failed tool result with a
// fixed, path-free message.  Anything else — path_escape, path_changed, an
// unusable root, cancellation, argument errors, platform refusals — still
// throws: those are sandbox invariant violations or controller-level
// failures, and the turn must stop rather than let the model probe around
// them.
export const RECOVERABLE_READ_ERRORS = Object.freeze({
  invalid_path: 'path is not a valid workspace-relative path; use plain names separated by /',
  not_found: 'no such file or directory in the workspace',
  not_directory: 'path is not a directory',
  not_regular_file: 'path is not a regular file',
  file_too_large: 'file is larger than the readable limit',
  not_text: 'file is not UTF-8 text',
  reparse_point_rejected: 'path goes through a symlink or junction, which is never followed',
  hard_link_rejected: 'file has more than one hard link and is not read',
  unknown_workspace: 'workspace_id is not configured',
  read_not_allowed: 'workspace is not readable',
  max_bytes_too_small: 'max_bytes is too small to hold the next character'
});
export const recoverableReadTool = execute => async call => {
  try { return await execute(call); } catch (error) {
    if (error instanceof WorkspaceError && Object.hasOwn(RECOVERABLE_READ_ERRORS, error.code)) return makeToolResult({ id: call.id, name: call.name, status: 'failed', text: JSON.stringify({ code: error.code, message: RECOVERABLE_READ_ERRORS[error.code] }) });
    throw error;
  }
};

// Search budgets on top of the file/match/depth limits: directories and
// entries examined are bounded too, so a wide tree of empty folders cannot
// keep the walk busy for the whole timeout, and no directory is read whole.
export const SEARCH_BUDGETS = Object.freeze({ directories: 256, entries: 10000, entriesPerDirectory: 2000 });
export const DIRECTORY_BATCH = 32;

export async function readAt(handle, maxBytes, position, signal) {
  const bytes = Buffer.alloc(maxBytes); let offset = 0;
  while (offset < maxBytes) { const read = await abortable(handle.read(bytes, offset, maxBytes - offset, position + offset), signal); if (!read.bytesRead) break; offset += read.bytesRead; }
  return bytes.subarray(0, offset);
}
