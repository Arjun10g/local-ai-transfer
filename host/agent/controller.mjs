import { randomUUID } from 'node:crypto';
import { makeEvent } from './assistant-events.mjs';
import { makeToolResult, parseToolCall, EnvelopeError } from './tool-envelope.mjs';
import { timeNowDefinition, timeNowTool } from '../tools/time-now.mjs';

export const STATES = Object.freeze(['IDLE', 'BUILDING_PROMPT', 'INFERENCING', 'TOOL_PROPOSED', 'WAITING_CONFIRMATION', 'TOOL_RUNNING', 'CONTINUING_MODEL', 'COMPLETED', 'CANCELLED', 'FAILED']);
const opaque = prefix => `${prefix}_${randomUUID().replaceAll('-', '')}`;
const CANCELLED_CONFIRMATION = Symbol('cancelled-confirmation');
const sessionIdPattern = /^[A-Za-z0-9_-]{8,96}$/;

export class ConversationController {
  constructor({ engine, maxToolCalls = 8, confirmationTimeoutMs = 30000, maxSessions = 4, maxHistoryMessages = 64, maxHistoryBytes = 262144, toolRegistry } = {}) {
    if (!engine?.generate) throw new TypeError('engine.generate is required');
    if (!Number.isInteger(maxSessions) || maxSessions < 1) throw new TypeError('maxSessions must be positive');
    if (!Number.isInteger(maxHistoryMessages) || maxHistoryMessages < 1 || !Number.isInteger(maxHistoryBytes) || maxHistoryBytes < 1024) throw new TypeError('history limits are invalid');
    this.engine = engine; this.maxToolCalls = maxToolCalls; this.confirmationTimeoutMs = confirmationTimeoutMs; this.maxSessions = maxSessions; this.maxHistoryMessages = maxHistoryMessages; this.maxHistoryBytes = maxHistoryBytes; this.clock = 0;
    this.sessions = new Map(); this.active = null; this.pending = new Map();
    this.tools = new Map([[timeNowDefinition.name, { ...timeNowDefinition, execute: ({ id }) => timeNowTool({ id }) }], ...(toolRegistry ? Object.entries(toolRegistry) : [])]);
  }
  createSession(sessionId = opaque('ses')) {
    if (!sessionIdPattern.test(sessionId)) throw new Error('invalid session id');
    if (this.sessions.has(sessionId)) return this._touch(this.sessions.get(sessionId));
    if (this.sessions.size >= this.maxSessions) {
      const candidates = [...this.sessions.values()].filter(s => s.id !== this.active?.sessionId);
      if (!candidates.length) throw Object.assign(new Error('session_limit'), { code: 'session_limit' });
      candidates.sort((a, b) => a.last_used - b.last_used || a.id.localeCompare(b.id));
      this.sessions.delete(candidates[0].id);
    }
    const session = { id: sessionId, state: 'IDLE', history: [], history_bytes: 0, created_at: new Date().toISOString(), last_request_id: null, last_used: ++this.clock };
    this.sessions.set(sessionId, session); return session;
  }
  _touch(session) { session.last_used = ++this.clock; return session; }
  getSession(sessionId) { return this._touch(this.sessions.get(sessionId) ?? this.createSession(sessionId)); }
  _appendHistory(session, message) {
    session.history.push(message); session.history_bytes += Buffer.byteLength(JSON.stringify(message), 'utf8');
    while (session.history.length > this.maxHistoryMessages || session.history_bytes > this.maxHistoryBytes) { const removed = session.history.shift(); session.history_bytes -= Buffer.byteLength(JSON.stringify(removed), 'utf8'); }
  }
  resetSession(sessionId) { const session = this.sessions.get(sessionId); if (!session) return false; if (session.state !== 'IDLE' && session.state !== 'COMPLETED' && session.state !== 'FAILED' && session.state !== 'CANCELLED') throw new Error('session_busy'); session.history = []; session.history_bytes = 0; session.state = 'IDLE'; return true; }
  state(sessionId) { return this.getSession(sessionId).state; }
  _cancelPending(requestId) { const active = this.active; if (!active || active.requestId !== requestId || !active.confirmationId) return false; const item = this.pending.get(active.confirmationId); if (!item) return false; this.pending.delete(active.confirmationId); active.confirmationId = null; item.resolve(CANCELLED_CONFIRMATION); return true; }
  cancel(requestId) { if (this.active?.requestId !== requestId) return false; this.active.controller.abort(); this._cancelPending(requestId); this.engine.cancel?.(requestId); return true; }
  confirm(confirmationId, approved, { requestId, callId } = {}) { const item = this.pending.get(confirmationId); if (!item || typeof approved !== 'boolean') return false; if ((requestId && requestId !== item.requestId) || (callId && callId !== item.callId)) return false; this.pending.delete(confirmationId); item.resolve(approved); return true; }
  emitFactory(requestId, sessionId, onEvent) { let sequence = 0; return (event, data) => { const output = makeEvent({ event, requestId, sessionId, sequence: sequence++, data }); onEvent?.(output); return output; }; }
  async runTurn({ sessionId, message, mode = 'normal', requestId = opaque('req'), signal, onEvent } = {}) {
    if (typeof message !== 'string' || !message.trim() || message.length > 32768) throw new Error('invalid_message');
    if (!/^[A-Za-z0-9_-]{8,96}$/.test(requestId)) throw Object.assign(new Error('invalid_request_id'), { code: 'invalid_request_id' });
    const session = this.getSession(sessionId); if (this.active) throw Object.assign(new Error('another_generation_active'), { code: 'busy' });
    if (!['normal', 'deep'].includes(mode)) throw new Error('invalid_mode');
    const controller = new AbortController();
    const relayAbort = () => { controller.abort(); this._cancelPending(requestId); }; signal?.addEventListener('abort', relayAbort, { once: true });
    this.active = { requestId, sessionId: session.id, controller, confirmationId: null };
    const emit = this.emitFactory(requestId, session.id, onEvent);
    session.last_request_id = requestId; this._appendHistory(session, { role: 'user', content: message }); session.state = 'BUILDING_PROMPT';
    let text = ''; let calls = 0;
    try {
      emit('message.started', { mode, state: session.state });
      while (true) {
        if (controller.signal.aborted) throw Object.assign(new Error('cancelled'), { code: 'cancelled' });
        session.state = calls ? 'CONTINUING_MODEL' : 'INFERENCING'; emit('message.started', { mode, state: session.state, continuation: calls > 0 });
        let callText = ''; let gotCall = false; let usage;
        for await (const frame of this.engine.generate({ requestId, sessionId: session.id, messages: session.history, mode, signal: controller.signal })) {
          if (frame.kind === 'text_delta') { text += frame.text; emit('message.delta', { text: frame.text }); }
          else if (frame.kind === 'tool_call_chunk') { gotCall = true; callText += frame.text; if (Buffer.byteLength(callText) > 32768) throw new EnvelopeError('tool_call_too_large', 'tool call exceeds limit'); }
          else if (frame.kind === 'done') usage = frame.usage;
        }
        if (!gotCall) { this._appendHistory(session, { role: 'assistant', content: text }); session.state = 'COMPLETED'; emit('message.completed', { text, finish_reason: 'stop', usage: usage ?? { prompt_tokens: 0, completion_tokens: text.length }, state: session.state }); emit('metrics.snapshot', { tool_calls: calls, history_messages: session.history.length, history_bytes: session.history_bytes }); return { requestId, sessionId: session.id, state: session.state, text }; }
        calls++; if (calls > this.maxToolCalls) throw Object.assign(new Error('tool_call_limit_exceeded'), { code: 'tool_call_limit_exceeded' });
        const call = parseToolCall(callText); session.state = 'TOOL_PROPOSED'; emit('tool.proposed', { call });
        const tool = this.tools.get(call.name); if (!tool) throw Object.assign(new Error('unknown_tool'), { code: 'unknown_tool' });
        let approved = true;
        if (tool.requires_confirmation) {
          session.state = 'WAITING_CONFIRMATION'; const confirmationId = opaque('cnf');
          this.active.confirmationId = confirmationId; emit('tool.confirmation_required', { confirmation_id: confirmationId, call, risk_tier: tool.risk_tier, expires_in_ms: this.confirmationTimeoutMs });
          approved = await new Promise(resolve => { const timer = setTimeout(() => { this.pending.delete(confirmationId); resolve(false); }, this.confirmationTimeoutMs); this.pending.set(confirmationId, { resolve: answer => { clearTimeout(timer); resolve(answer); }, requestId, sessionId: session.id, callId: call.id }); });
          this.active.confirmationId = null;
          if (approved === CANCELLED_CONFIRMATION) throw Object.assign(new Error('cancelled'), { code: 'cancelled' });
        }
        session.state = 'TOOL_RUNNING'; emit('tool.started', { call, approved });
        let result;
        if (!approved) result = makeToolResult({ id: call.id, name: call.name, status: 'denied', text: 'User denied this action.' });
        else {
          if (controller.signal.aborted) throw Object.assign(new Error('cancelled'), { code: 'cancelled' });
          result = await tool.execute({ ...call, signal: controller.signal });
        }
        if (!result || result.id !== call.id) throw new Error('tool_result_mismatch');
        emit('tool.completed', { result }); this._appendHistory(session, { role: 'tool', name: call.name, tool_call_id: call.id, content: result.content[0]?.text ?? '' });
        session.state = 'CONTINUING_MODEL'; text = '';
      }
    } catch (error) {
      const cancelled = error?.code === 'cancelled' || controller.signal.aborted;
      session.state = cancelled ? 'CANCELLED' : 'FAILED';
      emit(cancelled ? 'request.cancelled' : 'request.failed', { code: cancelled ? 'cancelled' : (error.code ?? 'request_failed'), message: cancelled ? 'Request cancelled.' : 'Request failed.' });
      return { requestId, sessionId: session.id, state: session.state, error: cancelled ? 'cancelled' : (error.code ?? 'request_failed') };
    } finally { signal?.removeEventListener('abort', relayAbort); if (this.active?.requestId === requestId) this.active = null; }
  }
}
