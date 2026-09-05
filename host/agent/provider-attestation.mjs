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

// Envelope validation intentionally returns a structured clone. Preserve the
// identity-bound attestation only from the original host object; serialized
// fields can never create or alter this association.
export function transferProviderAttestation(source, target) {
  const attestation = readProviderAttestation(source);
  if (!attestation || !target || typeof target !== 'object') return target;
  attestations.set(target, attestation);
  return target;
}
