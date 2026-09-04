import { randomUUID } from 'node:crypto';

export const ASSISTANT_EVENTS_VERSION = '0.1.0';
export const EVENT_NAMES = Object.freeze([
  'message.started', 'message.delta', 'message.completed', 'reasoning.started', 'reasoning.completed',
  'tool.proposed', 'tool.confirmation_required', 'tool.started', 'tool.output', 'tool.completed',
  'tool.failed', 'request.cancelled', 'request.failed', 'metrics.snapshot'
]);
const ID = /^[A-Za-z0-9_-]{8,96}$/;

export class EventError extends Error {
  constructor(code, message) { super(message); this.name = 'EventError'; this.code = code; }
}

export function makeEvent({ event, requestId, sessionId, sequence, data = {}, eventId = randomUUID().replaceAll('-', '') }) {
  if (!EVENT_NAMES.includes(event)) throw new EventError('unknown_event', `unknown event: ${event}`);
  if (!ID.test(requestId) || !ID.test(sessionId) || !ID.test(eventId)) throw new EventError('invalid_event_id', 'invalid event identifier');
  if (!Number.isInteger(sequence) || sequence < 0 || sequence > 10000) throw new EventError('invalid_sequence', 'invalid event sequence');
  if (!data || typeof data !== 'object' || Array.isArray(data) || Object.keys(data).length > 32) throw new EventError('invalid_event_data', 'event data must be a bounded object');
  return { version: ASSISTANT_EVENTS_VERSION, event_id: eventId, event, request_id: requestId, session_id: sessionId, sequence, timestamp: new Date().toISOString(), data: structuredClone(data) };
}

export function validateEvent(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new EventError('invalid_event', 'event must be an object');
  const required = ['version', 'event_id', 'event', 'request_id', 'session_id', 'sequence', 'timestamp', 'data'];
  for (const key of Object.keys(value)) if (!required.includes(key)) throw new EventError('unknown_field', `unknown event field: ${key}`);
  for (const key of required) if (!(key in value)) throw new EventError('missing_field', `missing event field: ${key}`);
  if (value.version !== ASSISTANT_EVENTS_VERSION || !EVENT_NAMES.includes(value.event)) throw new EventError('invalid_event', 'unsupported event version/name');
  if (!ID.test(value.event_id) || !ID.test(value.request_id) || !ID.test(value.session_id)) throw new EventError('invalid_event_id', 'invalid event identifier');
  if (!Number.isInteger(value.sequence) || value.sequence < 0 || value.sequence > 10000) throw new EventError('invalid_sequence', 'invalid event sequence');
  if (typeof value.timestamp !== 'string' || Number.isNaN(Date.parse(value.timestamp))) throw new EventError('invalid_timestamp', 'invalid timestamp');
  if (!value.data || typeof value.data !== 'object' || Array.isArray(value.data) || Object.keys(value.data).length > 32) throw new EventError('invalid_event_data', 'event data must be a bounded object');
  return structuredClone(value);
}

export function sseFrame(event) { const valid = validateEvent(event); return `event: ${valid.event}\ndata: ${JSON.stringify(valid)}\n\n`; }
