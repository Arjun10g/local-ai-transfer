import { makeToolResult } from '../agent/tool-envelope.mjs';

export const timeNowDefinition = Object.freeze({
  name: 'time.now', version: '1.0.0', risk_tier: 'T0', side_effect: 'none', network: false,
  description: 'Return local wall-clock time, UTC time, offset, and monotonic metadata.',
  requires_confirmation: false, output_limit: 4096, timeout_ms: 1000
});

export function timeNowTool({ id, name = 'time.now' } = {}) {
  const now = new Date();
  const offsetMinutes = -now.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const abs = Math.abs(offsetMinutes);
  const offset = `${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
  const payload = { local_time: now.toString(), utc_time: now.toISOString(), utc_offset: offset, monotonic_ms: Number(process.hrtime.bigint() / 1000000n) };
  return makeToolResult({ id, name, text: JSON.stringify(payload), durationMs: 0 });
}
