import { randomBytes } from 'node:crypto';

const PROFILES = new Set(['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access']);

export class OperatorGrantStore {
  constructor({ now = () => Date.now() } = {}) { this.now = now; this.grants = new Map(); this.listeners = new Map(); }
  grant({ capability, provider, accountFingerprint, scope = 'account', profile = 'full_access', expiresAt = this.now() + 3600000 } = {}) {
    if (![capability, provider, accountFingerprint, scope].every(value => typeof value === 'string' && value.length > 0) || !PROFILES.has(profile) || !Number.isFinite(expiresAt) || expiresAt <= this.now()) throw new TypeError('invalid operator grant');
    const previous = this.grants.get(capability);
    if (previous) for (const listener of this.listeners.get(capability) ?? []) listener();
    const record = Object.freeze({ capability, provider, account_fingerprint: accountFingerprint, scope, profile, created_at: this.now(), expires_at: expiresAt, generation: randomBytes(16).toString('hex') });
    this.grants.set(capability, record); return record;
  }
  revoke(capability) { const deleted = this.grants.delete(capability); if (deleted) for (const listener of this.listeners.get(capability) ?? []) listener(); return deleted; }
  subscribe(capability, listener) { if (typeof listener !== 'function') throw new TypeError('grant listener is required'); const listeners = this.listeners.get(capability) ?? new Set(); listeners.add(listener); this.listeners.set(capability, listeners); return () => { listeners.delete(listener); if (!listeners.size) this.listeners.delete(capability); }; }
  get(capability) { const grant = this.grants.get(capability); if (!grant || grant.expires_at <= this.now()) { const deleted = this.grants.delete(capability); if (deleted) for (const listener of this.listeners.get(capability) ?? []) listener(); return null; } return grant; }
  matches(capability, binding = {}) {
    const grant = this.get(capability); return Boolean(grant && grant.provider === binding.provider && grant.account_fingerprint === binding.accountFingerprint && grant.scope === binding.scope);
  }
}

export const PERMISSION_PROFILES = Object.freeze([...PROFILES]);
