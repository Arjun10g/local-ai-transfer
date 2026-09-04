import { open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createHash } from 'node:crypto';
import { WorkspacePolicy } from '../tools/local/workspace-policy.mjs';
import { ProviderToolError } from './provider-common.mjs';

const MAX_BYTES = 57344;
const MAX_FILE_BYTES = 32768;

function hash(value) { return createHash('sha256').update(value).digest('hex'); }
function utf8(value) { return Buffer.byteLength(value, 'utf8'); }

/** Read only explicitly selected regular files through WorkspacePolicy.
 * The returned labels are host-authored and are included in the preview digest;
 * no directory discovery or ambient environment is performed.
 */
export async function readWorkspaceContext(policy, workspaceId, paths) {
  if (!(policy instanceof WorkspacePolicy)) throw new ProviderToolError('copilot_policy_denied');
  if (!Array.isArray(paths) || paths.length > 8) throw new ProviderToolError('invalid_tool_arguments');
  const files = [];
  let total = 0;
  for (const path of paths) {
    const selected = await policy.regularFile(workspaceId, path);
    if (selected.stat.size > MAX_FILE_BYTES) throw new ProviderToolError('provider_response_too_large');
    let data; let handle;
    try {
      const flags = constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0);
      handle = await open(selected.canonical, flags);
      const before = await handle.stat();
      if (!before.isFile() || before.size !== selected.stat.size || before.mtimeMs !== selected.stat.mtimeMs) throw new ProviderToolError('copilot_policy_denied', 'selected context changed while opening');
      data = await handle.readFile();
      const after = await handle.stat();
      if (!after.isFile() || after.size !== before.size || after.mtimeMs !== before.mtimeMs) throw new ProviderToolError('copilot_policy_denied', 'selected context changed while reading');
    } catch (error) { if (error?.code === 'copilot_policy_denied') throw error; throw new ProviderToolError('copilot_policy_denied'); } finally { await handle?.close().catch(() => {}); }
    const checked = await policy.regularFile(workspaceId, path);
    if (checked.canonical !== selected.canonical || checked.stat.size !== selected.stat.size || checked.stat.mtimeMs !== selected.stat.mtimeMs) throw new ProviderToolError('copilot_policy_denied', 'selected context changed after reading');
    if (data.length > MAX_FILE_BYTES) throw new ProviderToolError('provider_response_too_large');
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(data); } catch { throw new ProviderToolError('copilot_policy_denied', 'selected file is not UTF-8'); }
    const label = `${workspaceId}/${path}`;
    const rendered = `--- ${label} ---\n${text}\n`;
    const bytes = utf8(rendered);
    if (total + bytes > MAX_BYTES) throw new ProviderToolError('provider_response_too_large');
    total += bytes;
    files.push({ path: label, bytes: data.length, digest: hash(data), text, rendered });
  }
  return { text: files.map(file => file.rendered).join(''), bytes: total, files: files.map(({ path, bytes, digest }) => ({ path, bytes, digest })) };
}

export function createWorkspaceContextReader(workspaces) {
  const policy = workspaces instanceof WorkspacePolicy ? workspaces : new WorkspacePolicy(workspaces ?? []);
  return (workspaceId, paths) => readWorkspaceContext(policy, workspaceId, paths);
}
