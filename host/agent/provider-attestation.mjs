// Host-only provider completion channel. Attestations are deliberately kept
// outside the serializable tool envelope so model/provider echoes cannot forge
// completion by copying journal metadata into JSON.
const attestations = new WeakMap();

export function attachProviderAttestation(result, attestation) {
  if (!result || typeof result !== 'object' || !attestation || typeof attestation !== 'object') throw new TypeError('provider attestation target is invalid');
  attestations.set(result, Object.freeze({ ...attestation }));
  return result;
}

export function readProviderAttestation(result) {
  return result && typeof result === 'object' ? attestations.get(result) ?? null : null;
}
