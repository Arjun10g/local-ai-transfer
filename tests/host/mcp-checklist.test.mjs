// Coverage audit for the MCP bridge: every conformance-checklist id in
// docs/research/COPILOT_MCP_COMPATIBILITY.md §8 must be named in a test title, or be listed
// below with the reason it cannot be tested offline. Also checks the client assets in
// docs/copilot/ against the real tool surface, so the docs cannot drift from the code.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { TOOL_NAMES } from '../../host/mcp/tools.mjs';

const ROOT = new URL('../../', import.meta.url);
const read = relative => readFile(fileURLToPath(new URL(relative, ROOT)), 'utf8');

// Items whose subject is a client UI or a transport this bridge deliberately does not build.
const NOT_TESTABLE_OFFLINE = Object.freeze({
  VSIDE1: 'Visual Studio tool enablement and re-trust prompts are client UI; covered by the manual steps in docs/copilot/README.md.',
  HTTP1: 'No HTTP transport is built: the bridge is stdio-only (doc §0.3), so there is no listener to probe.',
});

// Bridge requirements beyond the research checklist: the host identity handshake added after
// review (the key must never reach a listener that has not proved it is BMO).
const BRIDGE_REQUIREMENTS = Object.freeze({
  HS1: 'Handshake precedes every keyed request, carries only a fresh nonce, no key; success cached.',
  HS2: 'Proof binds key, nonce, port and pid: wrong key/pid/port and replayed proofs are rejected.',
  HS3: 'Missing/malformed/oversized proof, non-200 or timeout => unverified, key not sent.',
  HS4: 'Re-verify when host.json identity changes or a connection fails; concurrent calls share one handshake.',
  HS5: 'Proof failures are cached for at most ~5 s.',
});

async function checklistIds() {
  const doc = await read('docs/research/COPILOT_MCP_COMPATIBILITY.md');
  const section = doc.slice(doc.indexOf('## 8.'), doc.indexOf('## 9.'));
  return [...new Set([...section.matchAll(/\[ \] ([A-Z]+\d+)/g)].map(match => match[1]))];
}

async function testTitles() {
  const dir = fileURLToPath(new URL('tests/host/', ROOT));
  const files = (await readdir(dir)).filter(name => /^mcp-.*\.test\.mjs$/.test(name));
  const titles = [];
  for (const file of files) {
    const source = await readFile(`${dir}${file}`, 'utf8');
    for (const match of source.matchAll(/\btest\((['"`])(.*?)\1/g)) titles.push(match[2]);
  }
  return titles;
}

test('every conformance-checklist item is covered by a test title or explicitly out of offline scope', async () => {
  const ids = await checklistIds();
  assert.equal(ids.length, 30, `expected the 30 checklist items, found ${ids.length}: ${ids.join(' ')}`);
  const titles = await testTitles();
  const uncovered = [...ids, ...Object.keys(BRIDGE_REQUIREMENTS)].filter(id => !titles.some(title => title.includes(`[${id}]`)) && !NOT_TESTABLE_OFFLINE[id]);
  assert.deepEqual(uncovered, []);
  for (const id of Object.keys(NOT_TESTABLE_OFFLINE)) assert.ok(ids.includes(id), `stale exclusion ${id}`);
  // No typos: every bracketed id in a title must be a real checklist id.
  const used = new Set(titles.flatMap(title => [...title.matchAll(/\[([A-Z]+\d+)\]/g)].map(match => match[1])));
  for (const id of used) assert.ok(ids.includes(id) || Object.hasOwn(BRIDGE_REQUIREMENTS, id), `unknown checklist id [${id}] in a test title`);
});

test('[C8] VS Code client config: server "bmo", stdio, absolute node and script paths, key only via a password input', async () => {
  const config = JSON.parse(await read('docs/copilot/vscode-mcp.json'));
  assert.deepEqual(Object.keys(config.servers), ['bmo']);
  const server = config.servers.bmo;
  assert.equal(server.type, 'stdio');
  assert.match(server.command, /^[A-Za-z]:\\.+node\.exe$/);
  assert.equal(server.args.length, 1);
  assert.match(server.args[0], /^[A-Za-z]:\\.+\\lae-mcp\.mjs$/);
  const [input] = config.inputs;
  assert.equal(input.type, 'promptString');
  assert.equal(input.password, true);
  assert.equal(typeof input.description, 'string');
  assert.deepEqual(server.env, { BMO_DELEGATE_KEY: `\${input:${input.id}}` });
});

test('[C8] Copilot CLI config: server "bmo", local type, env placeholder (no literal key), bounded timeout', async () => {
  const config = JSON.parse(await read('docs/copilot/copilot-cli-mcp.json'));
  assert.deepEqual(Object.keys(config.mcpServers), ['bmo']);
  const server = config.mcpServers.bmo;
  assert.ok(['local', 'stdio'].includes(server.type));
  assert.match(server.command, /^[A-Za-z]:\\.+node\.exe$/);
  assert.match(server.args[0], /\\lae-mcp\.mjs$/);
  assert.deepEqual(server.env, { BMO_DELEGATE_KEY: '${BMO_DELEGATE_KEY}' });
  assert.deepEqual(server.tools, ['*']);
  assert.ok(server.timeout >= 20_000);
});

test('client docs name only real tools, the custom agent is scoped to bmo tools, and the README covers every tool', async () => {
  const files = ['README.md', 'copilot-instructions-snippet.md', 'agents-md-snippet.md', 'agents/bmo-delegate.agent.md', 'vscode-mcp.json', 'copilot-cli-mcp.json'];
  for (const file of files) {
    const text = await read(`docs/copilot/${file}`);
    for (const [name] of text.matchAll(/\bbmo_[a-z_]+/g)) if (!['bmo_not_running', 'bmo_local'].includes(name)) assert.ok(TOOL_NAMES.includes(name), `${file} mentions unknown tool ${name}`);
  }
  const agent = await read('docs/copilot/agents/bmo-delegate.agent.md');
  const frontmatter = agent.split('---')[1];
  assert.match(frontmatter, /^description: .+/m);
  assert.match(frontmatter, /^tools: \['bmo\/\*'\]$/m);
  assert.ok(!/mcp-servers/.test(frontmatter), 'mcp-servers is ignored by IDE custom agents');
  const readme = await read('docs/copilot/README.md');
  for (const name of TOOL_NAMES) assert.ok(readme.includes(`\`${name}\``), name);
  for (const step of ['Start-BMO.ps1 -Mode app -EnableDelegation', 'Start-BMO.ps1 -ShowDelegateKey', 'delegate-key', '-RotateDelegateKey', 'preempted', 'queue_full', 'Approve the first delegated job on the laptop', 'cloud agent']) assert.ok(readme.includes(step), step);
});
