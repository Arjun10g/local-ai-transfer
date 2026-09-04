import { createMicrosoftGraphTools, MicrosoftGraphProvider, graphDefinitions } from './microsoft-graph.mjs';
import { createCopilotTool, CopilotCliProvider, copilotDefinition } from './copilot-cli.mjs';
import { OperatorGrantStore, PERMISSION_PROFILES } from './operator-grants.mjs';

export function createExternalToolRegistry({ graph, copilot } = {}) {
  const graphProvider = graph instanceof MicrosoftGraphProvider ? graph : new MicrosoftGraphProvider(graph);
  const copilotProvider = copilot instanceof CopilotCliProvider ? copilot : new CopilotCliProvider(copilot);
  return { ...createMicrosoftGraphTools(graphProvider), [copilotDefinition.name]: createCopilotTool(copilotProvider) };
}

export { CopilotCliProvider, MicrosoftGraphProvider, OperatorGrantStore, PERMISSION_PROFILES, copilotDefinition, graphDefinitions };
