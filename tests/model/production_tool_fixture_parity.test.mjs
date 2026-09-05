import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createLocalToolDefinitionCatalog } from '../../host/tools/local/index.mjs';
import { createExternalToolDefinitionCatalog } from '../../host/providers/index.mjs';
import { ConversationController, modelToolDefinitions } from '../../host/agent/controller.mjs';

const fixture = JSON.parse(await readFile(new URL('./production_tool_call_eval.json', import.meta.url), 'utf8'));

function catalogDefinitions() {
  const local = createLocalToolDefinitionCatalog();
  const external = createExternalToolDefinitionCatalog();
  return modelToolDefinitions(new Map([...Object.entries(local), ...Object.entries(external)]));
}

test('schema catalogs are pure definitions and are rejected as execution registries', () => {
  const catalog = { ...createLocalToolDefinitionCatalog(), ...createExternalToolDefinitionCatalog() };
  const hookNames = ['execute', 'preview', 'authorize', 'confirmationRequired', 'confirmation', 'confirm'];
  for (const [name, definition] of Object.entries(catalog)) {
    for (const hook of hookNames) assert.notEqual(typeof definition[hook], 'function', `${name}: ${hook} must not be executable`);
  }
  assert.throws(
    () => new ConversationController({ engine: { async *generate() {} }, toolRegistry: catalog }),
    /must provide its matching name and execute function/u
  );
});

test('production evaluation fixture exactly matches the explicit definition catalog', () => {
  assert.equal(fixture.schema, 'local_bmo.tool-call-eval.v1');
  assert.equal(fixture.limits.context_tokens, 8192);
  assert.equal(fixture.limits.max_output_tokens, 256);
  assert.ok(fixture.cases.length <= 64);
  const advertised = catalogDefinitions();
  const fixtureByName = new Map(fixture.tools.map(tool => [tool.function.name, tool.function]));
  const advertisedByName = new Map(advertised.map(tool => [tool.function.name, tool.function]));
  assert.equal(fixtureByName.size, 28);
  assert.deepEqual([...fixtureByName.keys()], [...advertisedByName.keys()]);
  for (const [name, fixtureFunction] of fixtureByName) {
    // The process provider intentionally uses null-prototype maps; compare
    // the JSON contract rather than implementation object prototypes.
    const advertisedFunction = advertisedByName.get(name);
    assert.equal(fixtureFunction.description, advertisedFunction.description, `${name}: description`);
    assert.equal(JSON.stringify(fixtureFunction.parameters), JSON.stringify(advertisedFunction.parameters), name);
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
