import { randomBytes } from 'node:crypto';

const PROFILES = new Set(['always_ask', 'ask_before_writes', 'review_important_actions', 'full_access']);
const SAFE_VALUE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const MAX_GRANTS = 128;
export const MAX_GRANT_DURATION_MS = 8 * 60 * 60 * 1000;

function safeValue(value) { return typeof value === 'string' && SAFE_VALUE.test(value); }

export class OperatorGrantStore {
  constructor({ now = () => Date.now() } = {}) { this.now = now; this.grants = new Map(); this.listeners = new Map(); this.expiryTimers = new Map(); }
  grant({ capability, provider, accountFingerprint, scope = 'account', profile = 'full_access', expiresAt = this.now() + 3600000 } = {}) {
    const now = this.now();
    if (![capability, provider, accountFingerprint, scope].every(safeValue) || !PROFILES.has(profile) || !Number.isSafeInteger(expiresAt) || expiresAt <= now || expiresAt - now > MAX_GRANT_DURATION_MS) throw new TypeError('invalid operator grant');
    if (!this.grants.has(capability) && this.grants.size >= MAX_GRANTS) throw new TypeError('operator grant limit exceeded');
    const previous = this.grants.get(capability);
    if (previous) for (const listener of this.listeners.get(capability) ?? []) listener();
    clearTimeout(this.expiryTimers.get(capability));
    const record = Object.freeze({ capability, provider, account_fingerprint: accountFingerprint, scope, profile, created_at: now, expires_at: expiresAt, generation: randomBytes(16).toString('hex') });
    this.grants.set(capability, record);
    const timer = setTimeout(() => { if (this.grants.get(capability)?.generation === record.generation) this.revoke(capability); }, Math.max(1, expiresAt - now));
    timer.unref?.(); this.expiryTimers.set(capability, timer); return record;
  }
  revoke(capability) { clearTimeout(this.expiryTimers.get(capability)); this.expiryTimers.delete(capability); const deleted = this.grants.delete(capability); if (deleted) for (const listener of this.listeners.get(capability) ?? []) listener(); return deleted; }
  revokeAll() { const capabilities = [...this.grants.keys()]; for (const capability of capabilities) this.revoke(capability); return capabilities.length; }
  subscribe(capability, listener) { if (typeof listener !== 'function') throw new TypeError('grant listener is required'); const listeners = this.listeners.get(capability) ?? new Set(); listeners.add(listener); this.listeners.set(capability, listeners); return () => { listeners.delete(listener); if (!listeners.size) this.listeners.delete(capability); }; }
  get(capability) { const grant = this.grants.get(capability); if (!grant) return null; if (grant.expires_at <= this.now()) { this.revoke(capability); return null; } return grant; }
  matches(capability, binding = {}) {
    const grant = this.get(capability); return Boolean(grant && grant.provider === binding.provider && grant.account_fingerprint === binding.accountFingerprint && grant.scope === binding.scope);
  }
}

export class OperatorGrantControl {
  constructor({ store, bindings = [] } = {}) {
    if (!(store instanceof OperatorGrantStore) || !Array.isArray(bindings) || bindings.length > MAX_GRANTS) throw new TypeError('invalid operator grant control');
    this.store = store; this.bindings = new Map();
    for (const binding of bindings) {
      if (!binding || typeof binding !== 'object' || Array.isArray(binding) || ![binding.capability, binding.provider, binding.accountFingerprint, binding.scope].every(safeValue) || typeof binding.label !== 'string' || binding.label.length < 1 || binding.label.length > 120 || this.bindings.has(binding.capability)) throw new TypeError('invalid operator grant binding');
      this.bindings.set(binding.capability, Object.freeze({ capability: binding.capability, provider: binding.provider, accountFingerprint: binding.accountFingerprint, scope: binding.scope, label: binding.label }));
    }
  }
  binding(capability) { return this.bindings.get(capability) ?? null; }
  grantFor(capability) { const binding = this.binding(capability); if (!binding) return null; return this.store.matches(capability, binding) ? this.store.get(capability) : null; }
  view(binding) { const grant = this.grantFor(binding.capability); return { capability: binding.capability, provider: binding.provider, scope: binding.scope, label: binding.label, granted: Boolean(grant), expires_at: grant?.expires_at ?? null, profile: grant?.profile ?? null }; }
  list() { return [...this.bindings.values()].map(binding => this.view(binding)).sort((a, b) => a.capability.localeCompare(b.capability)); }
  grant(capability, durationMs) {
    const binding = this.binding(capability); if (!binding) return null;
    if (!Number.isInteger(durationMs) || durationMs < 60000 || durationMs > MAX_GRANT_DURATION_MS) throw new TypeError('invalid operator grant duration');
    this.store.grant({ ...binding, expiresAt: this.store.now() + durationMs, profile: 'full_access' }); return this.view(binding);
  }
  revoke(capability) { const binding = this.binding(capability); if (!binding) return null; this.store.revoke(capability); return this.view(binding); }
  revokeAll() { const revoked = this.store.revokeAll(); return { revoked }; }
}

export function buildOperatorGrantBindings(config = {}) {
  const bindings = [];
  const graph = config.providers?.microsoft_graph;
  if (graph?.enabled === true && graph.permission_profile === 'full_access' && graph.account_fingerprint && graph.account_fingerprint !== 'unknown') {
    for (const [capability, label] of [['microsoft.graph.mail', 'Outlook mail for the configured account'], ['microsoft.graph.teams', 'Teams chats for the configured account']]) bindings.push({ capability, provider: 'microsoft_graph', accountFingerprint: graph.account_fingerprint, scope: graph.scope ?? 'account', label });
  }
  for (const [index, workspace] of (config.workspace_roots ?? []).entries()) {
    const entry = typeof workspace === 'string' ? { id: `workspace-${index}`, write: true } : workspace;
    if (entry.write === true) bindings.push({ capability: `local.filesystem:${entry.id}`, provider: 'local_filesystem', accountFingerprint: 'local_host', scope: entry.id, label: `Write files in workspace ${entry.id}` });
  }
  for (const id of Object.keys(config.applications ?? {})) bindings.push({ capability: `local.application:${id}`, provider: 'local_application', accountFingerprint: 'local_host', scope: id, label: `Open allowlisted application ${id}` });
  if (config.process_actions?.enabled === true) for (const id of Object.keys(config.process_actions.actions ?? {})) bindings.push({ capability: `local.process:${id}`, provider: 'local_process', accountFingerprint: 'local_host', scope: id, label: `Run allowlisted process action ${id}` });
  bindings.push({ capability: 'local.clipboard', provider: 'local_clipboard', accountFingerprint: 'local_host', scope: 'clipboard', label: 'Write the local clipboard' });
  return bindings;
}

export const PERMISSION_PROFILES = Object.freeze([...PROFILES]);
