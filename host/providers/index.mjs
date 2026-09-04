import { createMicrosoftGraphTools, MicrosoftGraphProvider, graphDefinitions } from './microsoft-graph.mjs';
import { createCopilotTool, CopilotCliProvider, copilotDefinition } from './copilot-cli.mjs';
import { OperatorGrantStore, PERMISSION_PROFILES } from './operator-grants.mjs';

export function createExternalToolRegistry({ graph, copilot, config = {} } = {}) {
  const graphConfig = config.microsoft_graph ?? {};
  const copilotConfig = config.copilot ?? {};
  const graphProvider = graph instanceof MicrosoftGraphProvider ? graph : new MicrosoftGraphProvider({ ...graphConfig, ...(graph ?? {}) });
  const copilotProvider = copilot instanceof CopilotCliProvider ? copilot : new CopilotCliProvider({ ...copilotConfig, ...(copilot ?? {}) });
  return { ...createMicrosoftGraphTools(graphProvider), [copilotDefinition.name]: createCopilotTool(copilotProvider) };
}

export { CopilotCliProvider, MicrosoftGraphProvider, OperatorGrantStore, PERMISSION_PROFILES, copilotDefinition, graphDefinitions };
