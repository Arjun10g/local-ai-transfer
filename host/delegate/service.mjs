// Delegated jobs: a coding assistant (through the stdio bridge, lae-mcp.mjs)
// hands this laptop a small read-only text job and polls for the answer.
//
// The model sits between untrusted text and the operator's machine, and the
// answer leaves the laptop for a cloud caller, so:
//   * every job is approved ON THE LAPTOP (a card in the BMO UI) or by an
//     operator grant made on the laptop; the caller's own "allow" counts for
//     nothing;
//   * a job runs in a fresh controller built with read-only tools only, in
//     the controller's restricted `tools: 'delegate'` mode;
//   * the operator's own chat has priority: a job waits while a turn runs,
//     and a new turn stops a running job (which is queued again);
//   * nothing about a job is written to disk or logged; its text lives in
//     memory until it ends (prompt) or for 15 minutes after (answer).
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { ConversationController } from '../agent/controller.mjs';
import { CONTEXT_DEFAULTS, isContextOverflowError, utf8Bytes } from '../agent/context-budget.mjs';
import { ENGINE_MAX_MESSAGE_BYTES } from '../agent/tool-result-cap.mjs';
import { DelegateHttpError, exactKeys, jsonContentType, readJsonBody, sendJson, sendRetry } from './http.mjs';
import { ensureStateDir, loadOrCreateDelegateKey, readDelegateKey, removeHostFile, writeHostFile } from './state.mjs';
import { MAX_CALLER_CHARS, MAX_CONTEXT_CHARS, MAX_TASK_CHARS, answerForCaller, codePoints, composeJobMessage, forDisplay, stripUnsafe } from './text.mjs';

export const DELEGATE_CAPABILITY = 'delegate.read_only_jobs';
// The grant binding the operator sees in the access panel.  It approves
// delegated jobs only; nothing else checks this capability.
export const DELEGATE_GRANT_BINDING = Object.freeze({ capability: DELEGATE_CAPABILITY, provider: 'local_delegate', accountFingerprint: 'local_host', scope: 'delegate', label: 'Coding-assistant jobs (read-only; may read folders marked for delegation; answers go back to the coding assistant)' });

export const MAX_PENDING_JOBS = 3; // awaiting approval + queued
export const MAX_JOB_RECORDS = 20;
export const RESULT_TTL_MS = 15 * 60 * 1000;
export const MAX_WAIT_S = 15;
const MAX_PREEMPTIONS = 3;
const MAX_BUSY_RETRIES = 3;
const STARTS_PER_MINUTE = 6;
const AUTH_FAILURE_LIMIT = 20;
const AUTH_WINDOW_MS = 60000;
// The handshake is unauthenticated, so every request counts, not only bad ones.
export const HANDSHAKE_LIMIT = 30;
const HANDSHAKE_NONCE = /^[A-Za-z0-9_-]{22,64}$/u;
export const HANDSHAKE_CONTEXT = 'bmo-delegate-handshake-v1';

/**
 * Proof that this host holds the delegate key, bound to the caller's nonce
 * and to the port and pid the bridge read from host.json: a stale host.json
 * pointing at some other program on a reused port cannot be answered, so the
 * bridge learns that before it ever sends the key.
 */
export function handshakeProof(key, nonce, port, pid) {
  return createHmac('sha256', Buffer.from(key, 'utf8')).update(Buffer.from(`${HANDSHAKE_CONTEXT}|${nonce}|${port}|${pid}`, 'utf8')).digest('base64url');
}
// task 4,000 + context 16,000 code points at up to 4 UTF-8 bytes each, plus
// the JSON around them.
const MAX_BODY_BYTES = 96 * 1024;
const STOP_GRACE_MS = 30000;
const JOB_ID = /^[A-Za-z0-9_-]{8,64}$/;
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'denied', 'expired']);
const settleWithin = (promise, ms) => new Promise(resolve => { const timer = setTimeout(resolve, ms); timer.unref?.(); promise.then(() => { clearTimeout(timer); resolve(); }, () => { clearTimeout(timer); resolve(); }); });
const opaque = prefix => `${prefix}_${randomBytes(18).toString('base64url')}`;

// Fixed sentences only: a model's or caller's words never become an error.
const ERRORS = Object.freeze({
  operator_denied: 'The operator declined this job on the laptop.',
  approval_expired: 'Nobody approved this job on the laptop in time, so it did not run.',
  job_timeout: 'The job ran past its time limit and was stopped.',
  token_limit: 'The job reached its output limit and was stopped.',
  preempted: 'The operator kept using the local assistant, so the job could not finish. Try again later.',
  engine_busy: 'The local engine stayed busy. Try again later.',
  engine_unavailable: 'The local engine is not available.',
  context_too_large: 'The job does not fit in the local model\'s context window. Send less context.',
  tool_not_offered: 'The local model asked for a tool or folder this job may not use, so the job was stopped.',
  job_failed: 'The job failed on the laptop.',
});
function failureCode(code, error) {
  if (code === 'tool_not_offered' || code === 'workspace_not_offered' || code === 'confirmation_unavailable' || code === 'unknown_tool') return 'tool_not_offered';
  if (code === 'engine_timeout') return 'job_timeout';
  if (code === 'invalid_message_too_large' || isContextOverflowError(error ?? { code })) return 'context_too_large';
  if (['not_ready', 'http_503', 'engine_not_ready', 'engine_client_closed'].includes(code)) return 'engine_unavailable';
  return 'job_failed';
}

function keyMatch(actual, expected) {
  if (typeof actual !== 'string' || typeof expected !== 'string') return false;
  const a = Buffer.from(actual, 'utf8'); const b = Buffer.from(expected, 'utf8');
  return a.length === b.length && timingSafeEqual(a, b);
}

export class DelegateService {
  /**
   * @param engine          the shared engine client (one generation at a time)
   * @param userController  the operator's ConversationController (its turns have priority)
   * @param toolRegistry    the read-only registry from createDelegateToolRegistry()
   * @param workspaceIds    the flagged folder ids those tools serve
   */
  constructor({ engine, userController, toolRegistry = {}, workspaceIds = [], stateDir, grantControl = null, options = {}, contextTokens = CONTEXT_DEFAULTS.contextTokens, maxOutputTokens, requestTimeoutMs = 30000, platform = process.platform, now = () => Date.now() } = {}) {
    if (!engine?.generate) throw new TypeError('engine.generate is required');
    if (typeof stateDir !== 'string' || !stateDir) throw new TypeError('stateDir is required');
    this.engine = engine; this.userController = userController ?? null; this.toolRegistry = toolRegistry; this.workspaceIds = Object.freeze([...workspaceIds]);
    this.stateDir = stateDir; this.grantControl = grantControl; this.platform = platform; this.now = now;
    this.approvalTimeoutMs = options.approval_timeout_ms ?? 120000; this.maxRuntimeMs = options.max_runtime_ms ?? 600000;
    this.maxJobTokens = options.max_output_tokens ?? 2048; this.maxToolCalls = options.max_tool_calls ?? 4;
    // Not a config key: fixed at 15 minutes in production, shortened by tests.
    this.resultTtlMs = options.result_ttl_ms ?? RESULT_TTL_MS;
    this.contextTokens = contextTokens; this.maxOutputTokens = maxOutputTokens ?? engine.maxTokens ?? CONTEXT_DEFAULTS.maxOutputTokens; this.requestTimeoutMs = requestTimeoutMs;
    this.jobs = new Map(); this.queue = []; this.running = null; this.starting = false; this.interactive = 0; this.retryTimer = null;
    this.port = null; this.secrets = []; this.started = false; this.closed = false; this.hostRecord = null;
    this.authFailures = { count: 0, until: 0 }; this.startTimes = []; this.handshakes = { count: 0, until: 0 };
    // A revoked grant withdraws the approval of the jobs it approved.
    this.unsubscribe = grantControl?.store?.subscribe?.(DELEGATE_CAPABILITY, () => this.#grantChanged()) ?? null;
  }

  // ---- lifecycle ---------------------------------------------------------

  /** Called by HostServer.listen once the port is known. */
  async start({ port, secrets = [] } = {}) {
    if (this.started) return;
    await ensureStateDir(this.stateDir, { platform: this.platform });
    await loadOrCreateDelegateKey(this.stateDir, { platform: this.platform });
    this.port = port; this.secrets = secrets.filter(value => typeof value === 'string');
    this.hostRecord = await writeHostFile(this.stateDir, { port });
    this.started = true;
  }

  /** Called by HostServer.close: stop every job, release pollers, remove host.json. */
  async close() {
    if (this.closed) return;
    this.closed = true; this.unsubscribe?.(); clearTimeout(this.retryTimer);
    const running = this.running;
    for (const job of [...this.jobs.values()]) if (!TERMINAL.has(job.status)) this.#cancel(job, 'shutdown');
    if (running) await settleWithin(running.done, STOP_GRACE_MS);
    for (const job of this.jobs.values()) { clearTimeout(job.evictTimer); this.#notify(job); }
    if (this.hostRecord) await removeHostFile(this.stateDir, { port: this.hostRecord.port });
  }

  // ---- priority for the operator's own chat ------------------------------

  /**
   * The chat route calls this before a user turn.  A running job is stopped
   * (and queued again), so the operator never waits behind a job; no job
   * starts until the returned release() is called.
   */
  async interactiveBegin() {
    this.interactive += 1; let released = false;
    const release = () => { if (released) return; released = true; this.interactive -= 1; this.#kick(); };
    const current = this.running;
    if (current) {
      current.reason ??= 'preempted'; this.#stopRun(current);
      await settleWithin(current.done, STOP_GRACE_MS);
      await this.#engineIdle(STOP_GRACE_MS);
    }
    return release;
  }

  // The engine notices a cancel mid-prefill only at its next abort check and
  // stays BUSY until then; /readyz says ready while BUSY, so the lifecycle is
  // what is watched.  Without a ready() probe (the fixture) there is nothing
  // to wait for.
  async #engineIdle(timeoutMs) {
    if (typeof this.engine.ready !== 'function') return;
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      let lifecycle;
      try { lifecycle = (await this.engine.ready({ timeoutMs: 2000 }))?.lifecycle; } catch { return; /* the turn reports a busy engine itself */ }
      if (lifecycle !== 'BUSY') return;
      await new Promise(resolve => { const timer = setTimeout(resolve, 50); timer.unref?.(); });
    }
  }

  // ---- views --------------------------------------------------------------

  approvalMode() { try { return this.grantControl?.grantFor?.(DELEGATE_CAPABILITY) ? 'granted' : 'per_job'; } catch { return 'per_job'; } }
  pendingCount() { let n = 0; for (const job of this.jobs.values()) if (job.status === 'awaiting_approval' || job.status === 'queued') n += 1; return n; }
  statusSummary() { return { enabled: true, queue: this.pendingCount(), approval: this.approvalMode() }; }
  #busy() { return Boolean(this.running) || Boolean(this.userController?.active); }

  /** What the caller may see: never the task, the context, or the approver. */
  project(job) {
    const end = TERMINAL.has(job.status) ? job.finishedAt : this.now();
    const out = { job_id: job.id, status: job.status };
    if (!TERMINAL.has(job.status) && job.phase) out.phase = job.phase;
    out.elapsed_s = Math.max(0, Math.floor((end - job.createdAt) / 1000));
    if (job.status === 'completed') out.answer = job.answer;
    if (job.error && ['failed', 'denied', 'expired'].includes(job.status)) out.error = { code: job.error, message: ERRORS[job.error] ?? ERRORS.job_failed };
    out.truncated = job.truncated === true;
    return out;
  }

  /** The approval card: caller-chosen text masked, made visible, bounded. */
  card(job) {
    const task = forDisplay(job.task ?? '', MAX_TASK_CHARS);
    return {
      job_id: job.id,
      caller: { client: forDisplay(job.caller.client, MAX_CALLER_CHARS).text, name: forDisplay(job.caller.name, MAX_CALLER_CHARS).text },
      task: task.text, task_masked: task.masked, task_chars: job.taskChars, context_chars: job.contextChars,
      // files_requested without allow_files: the caller asked, but no folder
      // is marked for delegation, so the job will not read any file.
      allow_files: job.workspaces.length > 0, files_requested: job.filesRequested, workspaces: [...job.workspaces],
      expires_in_ms: Math.max(0, job.approvalExpiresAt - this.now()),
    };
  }

  operatorView() {
    const pending = [...this.jobs.values()].filter(job => job.status === 'awaiting_approval').sort((a, b) => a.createdAt - b.createdAt).map(job => this.card(job));
    const running = this.running ? { job_id: this.running.job.id, caller: { client: forDisplay(this.running.job.caller.client, MAX_CALLER_CHARS).text, name: forDisplay(this.running.job.caller.name, MAX_CALLER_CHARS).text } } : null;
    return { enabled: true, approval: this.approvalMode(), queue: this.pendingCount(), running, pending };
  }

  // ---- job lifecycle ------------------------------------------------------

  #notify(job) { job.version += 1; for (const wake of job.waiters) wake(); job.waiters.clear(); }
  #set(job, status, fields = {}) {
    Object.assign(job, fields); job.status = status;
    if (TERMINAL.has(status)) {
      clearTimeout(job.approvalTimer); job.finishedAt = this.now(); job.phase = null;
      // The prompt is no longer needed by anything; only the answer (if
      // any) is kept, and only until the record expires.
      job.task = null; job.context = null;
      this.queue = this.queue.filter(id => id !== job.id);
      job.evictTimer = setTimeout(() => this.#evict(job), this.resultTtlMs); job.evictTimer.unref?.();
    }
    this.#notify(job);
  }
  #evict(job) { clearTimeout(job.evictTimer); clearTimeout(job.approvalTimer); job.answer = null; this.jobs.delete(job.id); this.#notify(job); }
  #fail(job, code) { this.#set(job, 'failed', { error: ERRORS[code] ? code : 'job_failed' }); }

  submit({ task, context = '', allowFiles = false, caller }) {
    // Room for one more record: the oldest finished job goes first.
    if (this.jobs.size >= MAX_JOB_RECORDS) {
      const oldest = [...this.jobs.values()].filter(job => TERMINAL.has(job.status)).sort((a, b) => a.finishedAt - b.finishedAt)[0];
      if (!oldest) throw new DelegateHttpError(409, 'queue_full');
      this.#evict(oldest);
    }
    const workspaces = allowFiles ? [...this.workspaceIds] : [];
    const job = {
      id: opaque('job'), status: 'awaiting_approval', phase: 'waiting_for_operator', createdAt: this.now(), finishedAt: null,
      caller: { client: caller.client, name: caller.name }, task, context, taskChars: codePoints(task), contextChars: codePoints(context), workspaces, filesRequested: allowFiles === true,
      approvedBy: null, approvalExpiresAt: this.now() + this.approvalTimeoutMs, approvalTimer: null, answer: null, truncated: false, error: null,
      preemptions: 0, busyRetries: 0, notBefore: 0, version: 0, waiters: new Set(), evictTimer: null,
    };
    this.jobs.set(job.id, job);
    if (this.approvalMode() === 'granted') this.#approve(job, 'grant');
    else { job.approvalTimer = setTimeout(() => { if (job.status === 'awaiting_approval') this.#set(job, 'expired', { error: 'approval_expired' }); }, this.approvalTimeoutMs); job.approvalTimer.unref?.(); }
    return job;
  }

  #approve(job, by) {
    clearTimeout(job.approvalTimer); job.approvedBy = by;
    this.queue.push(job.id); this.#set(job, 'queued', { phase: 'waiting_for_engine' }); this.#kick();
  }

  /** The operator's answer from the card.  Returns accepted / expired / not_pending / unknown. */
  decide(jobId, approved) {
    const job = this.jobs.get(jobId);
    if (!job) return 'unknown';
    if (job.status === 'expired') return 'expired';
    if (job.status !== 'awaiting_approval') return 'not_pending';
    if (approved) this.#approve(job, 'operator'); else this.#set(job, 'denied', { error: 'operator_denied' });
    return 'accepted';
  }

  cancel(jobId) { const job = this.jobs.get(jobId); if (!job) return null; if (!TERMINAL.has(job.status)) this.#cancel(job, 'cancelled'); return job; }
  /** "Stop & revoke all" in the UI: every job, whatever its state. */
  stopAll() { let n = 0; for (const job of [...this.jobs.values()]) if (!TERMINAL.has(job.status)) { this.#cancel(job, 'cancelled'); n += 1; } return n; }

  // A running job is reported cancelled at once; its generation is stopped
  // and the slot frees when the engine call returns.
  #cancel(job, reason) {
    if (this.running?.job === job) { this.running.reason = reason; this.#stopRun(this.running); }
    this.#set(job, 'cancelled');
  }
  #grantChanged() {
    if (this.approvalMode() === 'granted') return;
    for (const job of [...this.jobs.values()]) if (job.approvedBy === 'grant' && !TERMINAL.has(job.status)) this.#cancel(job, 'grant_revoked');
  }

  // ---- scheduling ---------------------------------------------------------

  #engineFree() { return this.interactive === 0 && !this.userController?.active; }
  #retryLater(ms = 250) { if (this.retryTimer || this.closed) return; this.retryTimer = setTimeout(() => { this.retryTimer = null; this.#kick(); }, ms); this.retryTimer.unref?.(); }
  #kick() { void this.#pump().catch(() => {}); }
  async #pump() {
    if (this.closed || this.running || this.starting || !this.queue.length) return;
    if (!this.#engineFree()) return this.#retryLater();
    // A memory summary of the operator's conversation may still be running
    // on the engine after their turn ended; it has priority too.
    this.starting = true;
    try { await this.userController?.memoryIdle?.(); } catch { /* best effort */ } finally { this.starting = false; }
    if (this.closed || this.running || !this.queue.length) return;
    if (!this.#engineFree()) return this.#retryLater();
    const job = this.jobs.get(this.queue[0]);
    if (!job || job.status !== 'queued') { this.queue.shift(); return this.#kick(); }
    // A job the engine just refused as busy backs off before trying again.
    if (job.notBefore > this.now()) return this.#retryLater(job.notBefore - this.now());
    this.queue.shift();
    this.#run(job);
  }

  #stopRun(run) {
    run.abort.abort();
    try { run.controller?.cancel(run.requestId); } catch { /* the abort above already stops the turn */ }
    try { this.engine.cancel?.(run.requestId); } catch { /* idem */ }
  }

  #run(job) {
    const run = { job, abort: new AbortController(), reason: null, requestId: opaque('dlgreq'), sessionId: opaque('dlg'), controller: null, done: null };
    this.running = run;
    this.#set(job, 'running', { phase: 'starting' });
    run.done = this.#execute(job, run).catch(() => { if (!TERMINAL.has(job.status)) this.#fail(job, 'job_failed'); }).finally(() => { if (this.running === run) this.running = null; this.#kick(); });
  }

  async #execute(job, run) {
    const timer = setTimeout(() => { run.reason ??= 'timeout'; this.#stopRun(run); }, this.maxRuntimeMs); timer.unref?.();
    const engine = this.engine; const cap = this.maxJobTokens; let spent = 0;
    // The engine's per-call max_tokens bounds each reply; this bounds the
    // job's total across its tool loop.  Streamed text is estimated as it
    // arrives (so a runaway reply stops early); the engine's own count, when
    // it reports one, replaces the estimate at the end of each call.
    const jobEngine = {
      get maxTokens() { return engine.maxTokens; },
      cancel: id => engine.cancel?.(id),
      async *generate(args) {
        let call = 0;
        for await (const frame of engine.generate(args)) {
          if ((frame?.kind === 'text_delta' || frame?.kind === 'tool_call_chunk') && typeof frame.text === 'string') call += Math.ceil(utf8Bytes(frame.text) / 4);
          if (frame?.kind === 'done' && Number.isSafeInteger(frame.usage?.completion_tokens)) call = frame.usage.completion_tokens;
          if (spent + call > cap) { run.reason ??= 'token_limit'; run.abort.abort(); throw Object.assign(new Error('token_limit'), { code: 'cancelled' }); }
          yield frame;
        }
        spent += call;
      },
    };
    let result;
    try {
      // A fresh controller per job: no memory of the operator's conversation
      // or of any other job, discarded when the job ends.
      const controller = new ConversationController({ engine: jobEngine, toolRegistry: this.toolRegistry, maxToolCalls: this.maxToolCalls, maxSessions: 1, contextTokens: this.contextTokens, maxOutputTokens: this.maxOutputTokens });
      run.controller = controller;
      const message = composeJobMessage({ task: job.task, context: job.context, workspaces: job.workspaces });
      result = await controller.runTurn({ sessionId: run.sessionId, message, mode: 'normal', tools: 'delegate', delegateScope: { workspaces: job.workspaces }, requestId: run.requestId, signal: run.abort.signal, onEvent: event => this.#phase(job, event) });
    } catch (error) { result = { state: 'FAILED', error: error?.code ?? 'job_failed', cause: error }; }
    finally {
      clearTimeout(timer); run.controller?.sessions?.clear(); run.controller = null;
      // Free the engine's session slot (it keeps four and evicts the oldest,
      // which would otherwise be the operator's own conversation).
      try { await engine.deleteSession?.(run.sessionId); } catch { /* the engine evicts it eventually */ }
    }
    this.#settle(job, run, result);
  }

  #settle(job, run, result) {
    if (TERMINAL.has(job.status)) return;
    const reason = run.reason;
    if (reason === 'cancelled' || reason === 'shutdown' || reason === 'grant_revoked' || this.closed) return this.#set(job, 'cancelled');
    if (reason === 'preempted') {
      if (job.preemptions >= MAX_PREEMPTIONS) return this.#fail(job, 'preempted');
      job.preemptions += 1; this.queue.unshift(job.id); return this.#set(job, 'queued', { phase: 'waiting_for_operator_chat' });
    }
    if (reason === 'timeout') return this.#fail(job, 'job_timeout');
    if (reason === 'token_limit') return this.#fail(job, 'token_limit');
    if (result?.state === 'COMPLETED' && typeof result.text === 'string') {
      const { answer, truncated } = answerForCaller(result.text, { secrets: [...this.secrets, this.#cachedKey].filter(Boolean) });
      return this.#set(job, 'completed', { answer, truncated });
    }
    // The engine refused a second generation (something else got there
    // first): not the job's fault, so it waits its turn again.
    if (result?.error === 'busy' && job.busyRetries < MAX_BUSY_RETRIES) {
      job.busyRetries += 1; job.notBefore = this.now() + 1000; this.queue.unshift(job.id); this.#set(job, 'queued', { phase: 'waiting_for_engine' }); return undefined;
    }
    if (result?.error === 'busy') return this.#fail(job, 'engine_busy');
    return this.#fail(job, failureCode(result?.error, result?.cause));
  }

  // Phase only; no event text is read or kept.
  #phase(job, event) {
    let phase = null;
    if (event?.event === 'message.started') phase = 'generating';
    else if (event?.event === 'tool.started') phase = typeof event.data?.call?.name === 'string' && event.data.call.name.startsWith('fs.') ? 'reading_files' : 'using_tool';
    if (phase && job.phase !== phase && job.status === 'running') { job.phase = phase; this.#notify(job); }
  }

  #waitForChange(job, version, ms, res) {
    if (job.version !== version || TERMINAL.has(job.status) || ms <= 0 || this.closed) return Promise.resolve();
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); job.waiters.delete(done); res.off('close', done); resolve(); };
      const timer = setTimeout(done, ms); timer.unref?.();
      job.waiters.add(done); res.on('close', done);
    });
  }

  // ---- HTTP: the bridge's routes (/api/delegate/*) -------------------------

  #cachedKey = null;
  async #currentKey() {
    // Read on every request so `delegate-key --rotate` takes effect at once,
    // without a restart; any problem with the file means no key at all.
    try { this.#cachedKey = await readDelegateKey(this.stateDir, { platform: this.platform }); } catch { this.#cachedKey = null; }
    return this.#cachedKey;
  }
  #authLimited() { return this.authFailures.until > this.now() && this.authFailures.count >= AUTH_FAILURE_LIMIT; }
  #authFailed() { const now = this.now(); if (this.authFailures.until <= now) this.authFailures = { count: 1, until: now + AUTH_WINDOW_MS }; else this.authFailures.count += 1; }

  /**
   * Entry point for any path under /api/delegate/.  The checks run in this
   * order: a browser (any Origin) and a rebinding host name are refused
   * before the key is even read; the failed-key counter is separate from the
   * UI's, so a guessing caller cannot lock the operator out of the UI.
   */
  async handleBridge(req, res, url) {
    if (req.headers.origin !== undefined) return sendJson(res, 403, { error: 'forbidden' });
    const host = String(req.headers.host ?? '').toLowerCase();
    if (!this.port || (host !== `127.0.0.1:${this.port}` && host !== `localhost:${this.port}`)) return sendJson(res, 403, { error: 'forbidden' });
    // Unauthenticated by design and answered before the key check: it proves
    // the host to the bridge, never the bridge to the host.
    if (url.pathname === '/api/delegate/handshake') return this.#handshake(req, res, url);
    if (this.#authLimited()) return sendRetry(res, 429, 'auth_rate_limited', (this.authFailures.until - this.now()) / 1000);
    const match = /^Bearer ([A-Za-z0-9_-]{1,128})$/u.exec(req.headers.authorization ?? '');
    const key = await this.#currentKey();
    if (!match || !key || !keyMatch(match[1], key)) {
      this.#authFailed();
      if (this.#authLimited()) return sendRetry(res, 429, 'auth_rate_limited', (this.authFailures.until - this.now()) / 1000);
      return sendJson(res, 401, { error: 'unauthorized' });
    }
    this.authFailures = { count: 0, until: 0 };
    try { return await this.#routeBridge(req, res, url); }
    catch (error) {
      if (res.headersSent) { res.end(); return undefined; }
      if (error instanceof DelegateHttpError) return sendJson(res, error.status, { error: error.code });
      return sendJson(res, 500, { error: 'request_failed' });
    }
  }

  async #handshake(req, res, url) {
    if (req.method !== 'POST' || url.search) return sendJson(res, 404, { error: 'not_found' });
    // Its own budget, separate from the key-failure counter: it cannot lock
    // out a valid bridge's key checks, and it cannot be used to pump HMACs.
    const now = this.now();
    if (this.handshakes.until <= now) this.handshakes = { count: 0, until: now + AUTH_WINDOW_MS };
    this.handshakes.count += 1;
    if (this.handshakes.count > HANDSHAKE_LIMIT) return sendRetry(res, 429, 'rate_limited', (this.handshakes.until - now) / 1000);
    // The key is never sent here: a bridge that did would defeat the point.
    if (req.headers.authorization !== undefined) return sendJson(res, 400, { error: 'invalid_request' });
    try {
      if (!jsonContentType(req)) return sendJson(res, 415, { error: 'unsupported_content_type' });
      const input = exactKeys(await readJsonBody(req, { maxBytes: 256, timeoutMs: this.requestTimeoutMs, maxString: 64 }), ['nonce'], ['nonce']);
      if (typeof input.nonce !== 'string' || !HANDSHAKE_NONCE.test(input.nonce)) throw new DelegateHttpError(400, 'invalid_request_body');
      const key = await this.#currentKey();
      if (!key) return sendJson(res, 503, { error: 'not_ready' });
      // The port this request actually arrived on, and this process: exactly
      // what host.json claims.  No logging of nonce or proof anywhere.
      return sendJson(res, 200, { proof: handshakeProof(key, input.nonce, req.socket.localPort, process.pid) });
    } catch (error) {
      if (res.headersSent) { res.end(); return undefined; }
      if (error instanceof DelegateHttpError) return sendJson(res, error.status, { error: error.code });
      return sendJson(res, 500, { error: 'request_failed' });
    }
  }

  async #routeBridge(req, res, url) {
    const path = url.pathname;
    if (req.method === 'GET' && path === '/api/delegate/health' && !url.search) return this.#health(res);
    if (req.method === 'POST' && path === '/api/delegate/jobs' && !url.search) return this.#create(req, res);
    const status = /^\/api\/delegate\/jobs\/([^/]+)$/u.exec(path);
    if (req.method === 'GET' && status) return this.#status(req, res, url, status[1]);
    const cancel = /^\/api\/delegate\/jobs\/([^/]+)\/cancel$/u.exec(path);
    if (req.method === 'POST' && cancel && !url.search) {
      if (!jsonContentType(req)) return sendJson(res, 415, { error: 'unsupported_content_type' });
      exactKeys(await readJsonBody(req, { maxBytes: 1024, timeoutMs: this.requestTimeoutMs, maxString: 64 }), []);
      const job = JOB_ID.test(cancel[1]) ? this.cancel(cancel[1]) : null;
      return job ? sendJson(res, 200, { job_id: job.id, status: job.status }) : sendJson(res, 404, { error: 'job_not_found' });
    }
    return sendJson(res, 404, { error: 'not_found' });
  }

  async #health(res) {
    let ready = false;
    try { const health = await Promise.race([Promise.resolve(this.engine.health?.()), new Promise(resolve => { const timer = setTimeout(() => resolve(null), 5000); timer.unref?.(); })]); ready = health?.ready === true; } catch { ready = false; }
    return sendJson(res, 200, { ready: ready && !this.closed, engine: ready ? 'ready' : 'not_ready', queue: this.pendingCount(), busy: this.#busy(), approval: this.approvalMode() });
  }

  async #create(req, res) {
    if (!jsonContentType(req)) return sendJson(res, 415, { error: 'unsupported_content_type' });
    const input = exactKeys(await readJsonBody(req, { maxBytes: MAX_BODY_BYTES, timeoutMs: this.requestTimeoutMs, maxString: 2 * MAX_CONTEXT_CHARS }), ['task', 'context', 'allow_files', 'caller'], ['task', 'caller']);
    const caller = exactKeys(input.caller, ['client', 'name'], ['client', 'name']);
    for (const value of [caller.client, caller.name]) if (typeof value !== 'string' || !value.trim() || codePoints(value) > MAX_CALLER_CHARS) throw new DelegateHttpError(400, 'invalid_request_body');
    if (typeof input.task !== 'string' || (input.context !== undefined && typeof input.context !== 'string') || (input.allow_files !== undefined && typeof input.allow_files !== 'boolean')) throw new DelegateHttpError(400, 'invalid_request_body');
    if (codePoints(input.task) > MAX_TASK_CHARS) throw new DelegateHttpError(413, 'task_too_large');
    if (input.context !== undefined && codePoints(input.context) > MAX_CONTEXT_CHARS) throw new DelegateHttpError(413, 'context_too_large');
    const task = stripUnsafe(input.task); const context = stripUnsafe(input.context ?? '');
    if (!task.trim()) throw new DelegateHttpError(400, 'invalid_request_body');
    // The engine bounds one message in UTF-8 bytes; refuse now rather than
    // after the operator has approved a job that cannot run.
    if (utf8Bytes(composeJobMessage({ task, context, workspaces: input.allow_files === true ? this.workspaceIds : [] })) > ENGINE_MAX_MESSAGE_BYTES) throw new DelegateHttpError(413, 'job_too_large');
    const now = this.now(); this.startTimes = this.startTimes.filter(at => at > now - 60000);
    if (this.startTimes.length >= STARTS_PER_MINUTE) return sendRetry(res, 429, 'rate_limited', (this.startTimes[0] + 60000 - now) / 1000);
    if (this.pendingCount() >= MAX_PENDING_JOBS) return sendRetry(res, 409, 'queue_full', 30);
    this.startTimes.push(now);
    const job = this.submit({ task, context, allowFiles: input.allow_files === true, caller: { client: stripUnsafe(caller.client), name: stripUnsafe(caller.name) } });
    return sendJson(res, 202, { job_id: job.id, status: job.status });
  }

  async #status(req, res, url, id) {
    const keys = [...url.searchParams.keys()];
    if (keys.some(key => key !== 'wait') || keys.length > 1) return sendJson(res, 400, { error: 'invalid_request' });
    const raw = url.searchParams.get('wait');
    if (raw !== null && !/^(?:[0-9]|1[0-5])$/u.test(raw)) return sendJson(res, 400, { error: 'invalid_request' });
    const job = JOB_ID.test(id) ? this.jobs.get(id) : undefined;
    if (!job) return sendJson(res, 404, { error: 'job_not_found' });
    // Long poll: answer at once if anything changed, else when it does.
    await this.#waitForChange(job, job.version, Number(raw ?? 0) * 1000, res);
    if (!this.jobs.has(id)) return sendJson(res, 404, { error: 'job_not_found' });
    return sendJson(res, 200, this.project(job));
  }

  // ---- HTTP: the operator's routes (/api/delegation*, UI bearer) ----------

  async handleOperator(req, res, url) {
    const path = url.pathname;
    try {
      if (req.method === 'GET' && path === '/api/delegation' && !url.search) return sendJson(res, 200, this.operatorView());
      const decision = /^\/api\/delegation\/jobs\/([A-Za-z0-9_-]{8,64})\/decision$/u.exec(path);
      if (req.method === 'POST' && decision && !url.search) {
        if (!jsonContentType(req)) return sendJson(res, 415, { error: 'unsupported_content_type' });
        const input = exactKeys(await readJsonBody(req, { maxBytes: 1024, timeoutMs: this.requestTimeoutMs, maxString: 64 }), ['approved'], ['approved']);
        if (typeof input.approved !== 'boolean') throw new DelegateHttpError(400, 'invalid_request_body');
        const outcome = this.decide(decision[1], input.approved);
        if (outcome === 'accepted') return sendJson(res, 200, { accepted: true, status: this.jobs.get(decision[1])?.status ?? 'cancelled' });
        if (outcome === 'expired') return sendJson(res, 410, { accepted: false, error: 'approval_expired' });
        if (outcome === 'not_pending') return sendJson(res, 409, { accepted: false, error: 'job_not_pending' });
        return sendJson(res, 404, { accepted: false, error: 'job_not_found' });
      }
      if (req.method === 'POST' && path === '/api/delegation/stop' && !url.search) {
        if (!jsonContentType(req)) return sendJson(res, 415, { error: 'unsupported_content_type' });
        exactKeys(await readJsonBody(req, { maxBytes: 1024, timeoutMs: this.requestTimeoutMs, maxString: 64 }), []);
        return sendJson(res, 200, { cancelled: this.stopAll() });
      }
      return sendJson(res, 404, { error: 'not_found' });
    } catch (error) {
      if (res.headersSent) { res.end(); return undefined; }
      if (error instanceof DelegateHttpError) return sendJson(res, error.status, { error: error.code });
      return sendJson(res, 500, { error: 'request_failed' });
    }
  }
}
