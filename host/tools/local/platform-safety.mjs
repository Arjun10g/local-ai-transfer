// Platform boundaries that are deliberately fail-closed until a reviewed
// handle-relative Windows implementation exists.  This module has no path,
// process, or environment access so it is safe to use during composition.
export const WINDOWS_FILESYSTEM_ERROR = 'platform_path_safety_unavailable';
export const WINDOWS_FILESYSTEM_MESSAGE = 'filesystem tools require descriptor-safe path operations; Windows support is fail-closed until handle-relative reparse-safe primitives are available';

export const WINDOWS_FILESYSTEM_STATUS = Object.freeze({
  advertised: false,
  status: 'NOT_READY',
  reason: WINDOWS_FILESYSTEM_ERROR
});

export function filesystemSafetyError() {
  const error = new Error(WINDOWS_FILESYSTEM_MESSAGE);
  error.code = WINDOWS_FILESYSTEM_ERROR;
  return error;
}

export function assertFilesystemPlatformSafe(platform) {
  if (platform === 'win32') throw filesystemSafetyError();
}
