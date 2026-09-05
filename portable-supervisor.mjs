#!/usr/bin/env node
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export const PORTABLE_LAUNCH_STATE = 'NOT_READY-identity-pinned-native-launcher-unavailable';

// Pathname checks cannot bind node.exe, this module, or the engine image that
// CreateProcess consumes. Product launch remains absent until a reviewed
// native launcher holds deny-write/delete handles through process creation,
// assigns the complete process tree to a Job, and passes Windows acceptance.
export function refusePortableLaunch() {
  throw new Error(PORTABLE_LAUNCH_STATE);
}

const isMain = process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (isMain) refusePortableLaunch();
