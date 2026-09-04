function revoked() { return Object.assign(new Error('operator grant is missing, changed, or expired'), { code: 'provider_permission_revoked' }); }

export function applyOperatorGrantPolicy(tool, { grantControl, capabilityForCall } = {}) {
  if (!tool || typeof tool !== 'object' || typeof tool.execute !== 'function' || !grantControl || typeof capabilityForCall !== 'function') return tool;
  if (!['T1', 'T2'].includes(tool.risk_tier)) return tool;
  const originalConfirmation = tool.confirmationRequired;
  const baseConfirmation = async (call, context) => typeof originalConfirmation === 'function' ? Boolean(await originalConfirmation(call, context)) : Boolean(tool.requires_confirmation);
  return {
    ...tool,
    confirmationRequired: async (call, context) => grantControl.grantFor(capabilityForCall(call)) ? false : baseConfirmation(call, context),
    authorize: async call => {
      const grant = grantControl.grantFor(capabilityForCall(call));
      return grant ? { kind: 'operator_grant', generation: grant.generation } : { kind: 'policy' };
    },
    execute: async call => {
      if (call.authorization?.kind !== 'operator_grant') return tool.execute(call);
      const capability = capabilityForCall(call); const grant = grantControl.grantFor(capability);
      if (!grant || grant.generation !== call.authorization.generation) throw revoked();
      const controller = new AbortController(); const relay = () => controller.abort(); call.signal?.addEventListener('abort', relay, { once: true }); const unsubscribe = grantControl.store.subscribe(capability, relay);
      try {
        const value = await tool.execute({ ...call, signal: controller.signal });
        const current = grantControl.grantFor(capability);
        if (controller.signal.aborted || !current || current.generation !== grant.generation) throw revoked();
        return value;
      } finally { call.signal?.removeEventListener('abort', relay); unsubscribe(); }
    }
  };
}
