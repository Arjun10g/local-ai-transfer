import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createLocalToolRegistry } from '../../host/tools/local/index.mjs';
import { createExternalToolRegistry } from '../../host/providers/index.mjs';
import { modelToolDefinitions } from '../../host/agent/controller.mjs';

const fixture = JSON.parse(await readFile(new URL('./production_tool_call_eval.json', import.meta.url), 'utf8'));

function configuredDefinitions() {
  const workspace = { id: 'project', path: 'C:/workspace', read: true, write: true };
  const local = createLocalToolRegistry({
    workspaces: [workspace],
    process_actions: { enabled: false, actions: {} },
    networkProvider: 'disabled'
  });
  const external = createExternalToolRegistry({
    workspaceRoots: [workspace],
    config: { browser_actions: { safe_actions: true, action_origins: ['https://example.com'] } }
  });
  return modelToolDefinitions(new Map([...Object.entries(local), ...Object.entries(external)]));
}

test('production fixture exactly matches configured host tool names and schemas', () => {
  assert.equal(fixture.schema, 'local_bmo.tool-call-eval.v1');
  assert.equal(fixture.limits.context_tokens, 8192);
  assert.equal(fixture.limits.max_output_tokens, 64);
  assert.ok(fixture.cases.length <= 64);
  const advertised = configuredDefinitions();
  const fixtureByName = new Map(fixture.tools.map(tool => [tool.function.name, tool.function.parameters]));
  const advertisedByName = new Map(advertised.map(tool => [tool.function.name, tool.function.parameters]));
  assert.equal(fixtureByName.size, 28);
  assert.deepEqual([...fixtureByName.keys()], [...advertisedByName.keys()]);
  for (const [name, schema] of fixtureByName) {
    // The process provider intentionally uses null-prototype maps; compare
    // the JSON contract rather than implementation object prototypes.
    assert.equal(JSON.stringify(schema), JSON.stringify(advertisedByName.get(name)), name);
  }
});

test('production fixture covers every advertised tool and bounded adversarial cases', () => {
  const names = new Set(fixture.tools.map(tool => tool.function.name));
  const covered = new Set(fixture.cases.flatMap(item => item.expected.call ? [item.expected.call.name] : []));
  assert.deepEqual(covered, names);
  assert.ok(fixture.cases.some(item => item.category === 'schema_edge'));
  assert.ok(fixture.cases.some(item => item.category === 'prompt_injection'));
  assert.ok(fixture.cases.some(item => item.expected.no_call === true));
});
