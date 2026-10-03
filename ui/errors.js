// Plain-English explanations for every error code the BMO host, the native
// engine, and the tools can put in front of the user.  Pure (no DOM, no
// network) so node tests can import it and check coverage against the codes
// the host source actually emits.

const RETRY = 'Try again. If it keeps happening, press Reset or restart BMO.';
const RELAUNCH = 'Close this tab and open BMO again from its shortcut.';
const REPHRASE = 'Try rephrasing your message.';

const CANCELLED = ['Stopped.', ''];
const BUSY = ['The engine is still finishing an earlier request (about a minute).', 'Wait a moment, then send again.'];
const NOT_READY = ['The model is still loading or has stopped.', 'Give it a minute. If this does not clear, restart BMO.'];
const TOO_LONG = ['This conversation has grown too long.', 'Press Reset to start a fresh conversation.'];
const BAD_TOOL = ['The assistant made a bad tool request.', REPHRASE];
const LOST = ['Lost contact with the BMO host.', 'Is its window still open? If you closed it, start BMO again from its shortcut.'];
const DISCONNECTED = ['This page is not connected to the running BMO host (it may have been restarted).', RELAUNCH];
const MALFORMED = ['BMO rejected that request as malformed.', 'Reload the page and try again.'];
const INTERNAL = ['Something went wrong inside BMO.', RETRY];
const ENGINE_GARBLED = ['The engine sent a reply BMO could not use.', RETRY];
const ANSWER_TOO_BIG = ['The answer was too long for BMO to accept.', 'Ask for a shorter answer.'];
const JOURNAL = ['BMO\'s safety log for actions is not available, so it will not run actions that change things.', 'Questions that only read information still work. Restart BMO to retry.'];
const UNVERIFIED = ['BMO cannot confirm whether that action finished.', 'Check the result yourself before asking again.'];
const PROVIDER = ['A connected service (Outlook, Teams, the browser, or Copilot) did not complete the request.', 'Check that it is connected, then try again.'];
const FILE = ['BMO cannot use that file or folder.', 'Check that it is inside a folder BMO is allowed to use.'];
const FILE_CHANGED = ['The file changed after BMO read it.', 'Ask again so BMO re-reads the file first.'];
const URL_BLOCKED = ['That web address is not allowed.', 'Use a public https:// address.'];
const PROCESS = ['The helper program BMO started did not finish properly.', RETRY];
const PAGE_CHANGED = ['The web page changed before BMO could act on it.', 'Ask again so BMO looks at the current page.'];

const EXACT = new Map(Object.entries({
  // Client-side conditions (no host round trip).
  network_error: LOST,
  stream_interrupted: ['The connection to BMO dropped while it was answering.', 'Check that the BMO window is still open, then try again.'],
  no_credential: ['This tab is not connected to BMO.', 'Use the tab BMO opened when it started (reloading that tab is fine), or restart BMO.'],
  session_expired: DISCONNECTED,
  message_too_long: ['That message is too long (the limit is 32 KB of text, roughly 30,000 English characters).', 'Shorten it or split it into parts.'],
  // Host HTTP refusals.
  unauthorized: DISCONNECTED,
  invalid_bootstrap: DISCONNECTED,
  bootstrap_unavailable: ['This launch link has already been used or has expired.', 'Use the tab BMO opened when it started (reloading that tab is fine), or restart BMO.'],
  auth_rate_limited: ['Too many failed connection attempts.', 'Wait a minute, then open BMO again from its shortcut.'],
  forbidden: ['The BMO host refused this page.', 'Open BMO from its own shortcut so the address starts with http://127.0.0.1.'],
  body_too_large: ['That message is too long for BMO to accept.', 'Shorten it or split it into parts.'],
  invalid_json: ['BMO could not read that request (it may be too long).', 'Shorten the message and try again.'],
  request_timeout: ['The request took too long to reach BMO.', 'Try again.'],
  unsupported_content_type: MALFORMED,
  invalid_request_body: MALFORMED,
  invalid_request_id: MALFORMED,
  invalid_chat_request: MALFORMED,
  invalid_message: ['BMO could not accept that message (it may be empty or too long).', 'Shorten it and try again.'],
  invalid_message_too_large: ['That message is too long (the limit is 32 KB of text, roughly 30,000 English characters).', 'Shorten it or split it into parts.'],
  invalid_tools_mode: MALFORMED,
  invalid_mode: MALFORMED,
  invalid_session_id: MALFORMED,
  invalid_messages: MALFORMED,
  invalid_tools: MALFORMED,
  invalid_text: MALFORMED,
  not_found: ['BMO could not find that (it may already have finished).', ''],
  unknown_capability: ['That permission is not configured on this laptop.', ''],
  session_limit: ['Too many conversations are open in BMO.', 'Close other BMO tabs, or press Reset.'],
  host_shutdown_failed: ['BMO did not shut down cleanly.', 'Restart BMO from its shortcut.'],
  request_failed: INTERNAL,
  internal_error: INTERNAL,
  engine_error: INTERNAL,
  // Generation state.
  busy: BUSY,
  session_busy: ['BMO is still working on the last message, so the conversation cannot be reset yet.', 'Press Stop, wait for it to finish, then press Reset.'],
  cancelled: CANCELLED,
  request_cancelled: CANCELLED,
  not_ready: NOT_READY,
  engine_not_ready: NOT_READY,
  http_503: NOT_READY,
  engine_timeout: ['That took too long.', 'Try a shorter message or press Reset.'],
  engine_client_closed: ['BMO is shutting down.', 'Start BMO again from its shortcut.'],
  engine_identity_mismatch: ['The running engine is not the model BMO was set up for.', 'Restart BMO with its launcher.'],
  invalid_request: TOO_LONG,
  engine_request_too_large: TOO_LONG,
  request_too_large: TOO_LONG,
  context_overflow: TOO_LONG,
  context_limit_exceeded: TOO_LONG,
  response_too_large: ANSWER_TOO_BIG,
  output_limit: ANSWER_TOO_BIG,
  // Tool-call envelope and tool runtime.
  malformed_tool_call: BAD_TOOL,
  invalid_tool_call: BAD_TOOL,
  invalid_tool_arguments: BAD_TOOL,
  invalid_arguments: BAD_TOOL,
  invalid_tool_name: BAD_TOOL,
  unknown_tool: BAD_TOOL,
  unknown_field: BAD_TOOL,
  missing_field: BAD_TOOL,
  mixed_tool_call_output: BAD_TOOL,
  tool_call_too_large: BAD_TOOL,
  arguments_too_large: BAD_TOOL,
  duplicate_json_key: BAD_TOOL,
  invalid_call_id: BAD_TOOL,
  invalid_envelope: BAD_TOOL,
  invalid_tool_stream: BAD_TOOL,
  tool_call_not_offered: ['The assistant tried to use a tool while tools were switched off.', 'Turn tools on, or rephrase the question.'],
  confirmation_expired: ['Nobody answered the confirmation in time, so the action was not run.', 'Ask again if you still want it done.'],
  tool_call_limit_exceeded: ['The assistant used too many tools in a row and was stopped.', 'Ask for one thing at a time.'],
  tool_timeout: ['A tool took too long and was stopped.', 'Try again, or ask for something smaller.'],
  invalid_tool_timeout: INTERNAL,
  invalid_tool_result: ['A tool returned something BMO could not check, so it was discarded.', 'Try again.'],
  invalid_result_content: ['A tool returned something BMO could not check, so it was discarded.', 'Try again.'],
  invalid_result_status: ['A tool returned something BMO could not check, so it was discarded.', 'Try again.'],
  invalid_result_metadata: ['A tool returned something BMO could not check, so it was discarded.', 'Try again.'],
  tool_result_mismatch: ['A tool returned something BMO could not check, so it was discarded.', 'Try again.'],
  native_supervisor_unavailable: JOURNAL,
  action_reconciliation_unavailable: JOURNAL,
  journal_unavailable: JOURNAL,
  action_completion_unverified: UNVERIFIED,
  provider_action_reconciling: UNVERIFIED,
  provider_read_unverified: ['BMO could not verify what the connected service returned, so it was not used.', 'Try again.'],
  provider_unconfigured: ['That service is not set up on this laptop.', ''],
  provider_disabled: ['That service is switched off in BMO\'s settings.', ''],
  provider_timeout: ['The connected service took too long to answer.', 'Try again in a moment.'],
  provider_offline: ['The connected service could not be reached.', 'Check the internet connection, then try again.'],
  provider_unauthorized: ['The connected service needs you to sign in again.', 'Use Connect Outlook / Teams, then try again.'],
  provider_permission_revoked: ['Access to that service was revoked.', 'Grant access again if you want BMO to use it.'],
  provider_permission_insufficient: ['BMO does not have permission for that in the connected service.', 'Grant the matching access, then try again.'],
  // Local files, web addresses, apps, and helper processes.
  already_exists: ['That file already exists. BMO only creates new files.', 'Ask for a different file name.'],
  base_hash_mismatch: FILE_CHANGED,
  path_changed: FILE_CHANGED,
  file_too_large: ['That file is too large for BMO to open.', 'Ask about a smaller file.'],
  not_text: ['That file is not plain text, so BMO cannot read it.', ''],
  unsafe_url: URL_BLOCKED,
  private_url: URL_BLOCKED,
  invalid_url: URL_BLOCKED,
  unknown_app: ['That app is not on BMO\'s allowed list.', ''],
  platform_unsupported: ['That tool does not work on this computer.', ''],
  // Conversation memory (host/agent/memory-note.mjs); shown only in metrics.
  memory_empty_note: ['BMO could not summarise the earlier conversation, so older parts were dropped instead.', ''],
  memory_tool_call_output: ['BMO could not summarise the earlier conversation, so older parts were dropped instead.', ''],
  // Delegated jobs from a coding assistant (host/delegate), as the approval
  // card and the delegate bridge report them.
  approval_expired: ['Nobody approved that coding-assistant job in time, so it did not run.', 'The coding assistant can ask again.'],
  operator_denied: ['You declined that coding-assistant job.', ''],
  job_not_found: ['That coding-assistant job is no longer known to BMO (it may have finished or expired).', ''],
  job_not_pending: ['That coding-assistant job was already answered or stopped.', ''],
  queue_full: ['Too many coding-assistant jobs are waiting.', 'Approve, deny, or wait for the waiting ones first.'],
  task_too_large: ['That coding-assistant job is too long for BMO.', 'Ask the coding assistant to send a shorter task.'],
  job_too_large: ['That coding-assistant job is too long for BMO.', 'Ask the coding assistant to send less context.'],
  job_timeout: ['A coding-assistant job ran past its time limit and was stopped.', ''],
  token_limit: ['A coding-assistant job reached its output limit and was stopped.', ''],
  preempted: ['A coding-assistant job was stopped so you could use BMO.', ''],
  engine_busy: BUSY,
  engine_unavailable: NOT_READY,
  rate_limited: ['Too many coding-assistant jobs were started in the last minute.', 'Wait a minute.'],
  tool_not_offered: ['The assistant asked for a tool it was not allowed to use in this job, so it was stopped.', ''],
  confirmation_unavailable: ['That job cannot ask for confirmation, so the action was refused.', ''],
  invalid_delegate_scope: INTERNAL,
  delegate_key_invalid: ['The coding-assistant key file is damaged.', 'Make a new one: python local\\bmo_local.py delegate-key --rotate'],
  delegate_key_unsafe: ['The coding-assistant key file is not private to you, so BMO will not use it.', 'Make a new one: python local\\bmo_local.py delegate-key --rotate'],
  state_dir_invalid: ['BMO cannot find its settings folder for coding-assistant jobs.', 'Restart BMO with its launcher.'],
  state_dir_unsafe: ['BMO\'s settings folder for coding-assistant jobs is not private to you.', 'Check the folder\'s owner and permissions, then restart BMO.'],
  host_file_invalid: INTERNAL,
  // The delegate bridge's own codes (host/mcp), shown to the coding assistant.
  bmo_not_running: ['BMO is not running, or coding-assistant jobs are switched off.', 'Start BMO with coding-assistant jobs enabled.'],
  no_key: ['The coding assistant has no BMO key configured.', 'Show the key on the laptop and paste it into the coding assistant\'s settings.'],
  host_unreachable: LOST,
  // The handshake proof did not match: whatever answers on that port is not this BMO.
  host_unverified: ['The program on BMO\'s port is not BMO, so the key was not sent to it.', 'Restart BMO, then try again.'],
  host_timeout: ['BMO did not answer in time.', 'Check that BMO is still running, then try again.'],
  host_protocol_error: INTERNAL,
  invalid_job_id: MALFORMED,
  aborted: CANCELLED,
  timeout: ['That took too long.', 'Try again.'],
}));

// Families keep the table finite while still giving every emitted code a
// specific sentence; first match wins, so narrower patterns come first.
const FAMILIES = [
  [/^http_(?:401|403)$/, DISCONNECTED],
  [/^http_409$/, BUSY],
  [/^http_413$/, ['That message is too long for BMO to accept.', 'Shorten it or split it into parts.']],
  [/^http_429$/, ['Too many failed connection attempts.', 'Wait a minute, then open BMO again from its shortcut.']],
  [/^http_4\d\d$/, MALFORMED],
  [/^http_5\d\d$/, INTERNAL],
  [/^(?:engine_(?:response|stream)_too_large|engine_stream_(?:line_too_large|event_limit))$/, ANSWER_TOO_BIG],
  [/^(?:engine_|invalid_engine_)/, ENGINE_GARBLED],
  [/^json_/, BAD_TOOL],
  [/^action_journal_|^action_/, JOURNAL],
  [/^copilot_/, ['Copilot is not available for that request.', '']],
  [/^provider_/, PROVIDER],
  [/^browser_/, PAGE_CHANGED],
  [/^(?:path_|invalid_path$|invalid_workspace$|unknown_workspace$|duplicate_workspace$|workspace_|not_directory$|not_regular_file$|read_not_allowed$|write_not_allowed$|reparse_point_rejected$|hard_link_rejected$)/, FILE],
  [/^(?:process_|tool_process_)/, PROCESS],
  [/^(?:invalid_event|unknown_event$|invalid_sequence$|invalid_timestamp$)/, INTERNAL],
];

const CODE = /^[a-z][a-z0-9_]{0,95}$/;

/**
 * Turn a host/engine/tool error code (or an HTTP status when no code came
 * back) into `{ code, message, action, known }`.  `known` is false only for
 * codes this table has never heard of; the caller still gets usable text.
 */
export function describeError(code, { status } = {}) {
  let normalized = typeof code === 'string' && CODE.test(code) ? code : null;
  if (!normalized && Number.isInteger(status) && status >= 100 && status <= 599) normalized = `http_${status}`;
  if (!normalized) normalized = 'request_failed';
  let entry = EXACT.get(normalized);
  if (!entry) entry = FAMILIES.find(([pattern]) => pattern.test(normalized))?.[1];
  const known = entry !== undefined;
  const [message, action] = entry ?? ['Something unexpected went wrong.', RETRY];
  return { code: normalized, message, action, known };
}

/** True when the code means "the user stopped it", which is not a failure. */
export function isCancellation(code) { return code === 'cancelled' || code === 'request_cancelled'; }
