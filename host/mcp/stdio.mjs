// stdio transport for the BMO MCP bridge: framing, stdout discipline, and shutdown.
//
// Spec (2026-07-28 stdio, verified): one JSON-RPC message per line, no embedded newlines,
// stdout carries ONLY MCP messages, logs may go to stderr, and the server SHOULD exit
// promptly when stdin closes (the primary, and on Windows the only, graceful signal).

import { HostClient, readHostInfo, defaultStateDir } from './host-client.mjs';
import { createLogger } from './logger.mjs';
import { McpServer } from './server.mjs';
import { ToolService } from './tools.mjs';

export const MAX_MESSAGE_BYTES = 1024 * 1024;
// The key goes into an HTTP header, so only visible ASCII is acceptable; the length floor
// rejects obvious placeholders like "changeme".
const KEY_SHAPE = /^[\x21-\x7E]{16,512}$/;

/**
 * Split a byte stream into lines without ever buffering more than `maxBytes` of one line.
 * Works on Buffers so a multi-byte UTF-8 character split across chunks is decoded intact.
 */
export class LineSplitter {
  constructor({ maxBytes = MAX_MESSAGE_BYTES, onLine, onOversize }) {
    this.maxBytes = maxBytes; this.onLine = onLine; this.onOversize = onOversize;
    this.parts = []; this.size = 0; this.discarding = false;
  }

  push(chunk) {
    let start = 0;
    let newline;
    while ((newline = chunk.indexOf(10, start)) !== -1) {
      const piece = chunk.subarray(start, newline);
      start = newline + 1;
      if (this.discarding) { this.#reset(); continue; } // tail of an over-long line, already reported
      if (this.size + piece.length > this.maxBytes) { this.#reset(); this.onOversize(); continue; }
      const line = this.parts.length ? Buffer.concat([...this.parts, piece]) : piece;
      this.#reset();
      this.#emit(line);
    }
    if (start >= chunk.length || this.discarding) return;
    const rest = chunk.subarray(start);
    if (this.size + rest.length > this.maxBytes) { this.#reset(); this.discarding = true; this.onOversize(); return; }
    this.parts.push(Buffer.from(rest)); this.size += rest.length;
  }

  /** EOF: a final message without a trailing newline is still a message. */
  end() {
    if (!this.discarding && this.size > 0) { const line = Buffer.concat(this.parts); this.#reset(); this.#emit(line); }
    this.#reset();
  }

  #emit(buffer) {
    // Windows clients may send CRLF; a UTF-8 BOM may precede the first message.
    const text = buffer.toString('utf8').replace(/^﻿/, '').replace(/\r$/, '');
    if (text.trim()) this.onLine(text);
  }

  #reset() { this.parts = []; this.size = 0; this.discarding = false; }
}

/** Read and validate the delegate key from the environment, then remove it from process.env. */
export function takeDelegateKey(env = process.env) {
  const raw = env.BMO_DELEGATE_KEY;
  // Removing it limits exposure to anything that later dumps or inherits the environment.
  delete env.BMO_DELEGATE_KEY;
  const key = typeof raw === 'string' ? raw.trim() : '';
  // An unexpanded client placeholder ("${input:...}", "${BMO_DELEGATE_KEY}") means the client
  // did not substitute the secret; treat it as "not configured" instead of sending it and
  // getting an opaque 401.
  if (key.includes('${')) return '';
  return KEY_SHAPE.test(key) ? key : '';
}

/**
 * Wire an McpServer to byte streams. Everything is injectable so tests can run the real
 * transport in-process; lae-mcp.mjs passes the real process streams.
 */
export function startStdioBridge({
  stdin = process.stdin,
  stdout = process.stdout,
  writeErr = text => process.stderr.write(text),
  env = process.env,
  exit = code => process.exit(code),
  hostClient,
  discover,
  limits,
  budgets,
  limiter,
  logLevel,
} = {}) {
  const key = takeDelegateKey(env);
  const logger = createLogger({ write: writeErr, level: logLevel ?? env.BMO_MCP_LOG ?? 'info', secrets: [key] });
  const client = hostClient ?? new HostClient({ key, discover: discover ?? (() => readHostInfo({ stateDir: defaultStateDir({ env }) })) });
  const tools = new ToolService({ hostClient: client, secrets: [key], budgets, limiter, logger });
  let stdoutOpen = true;
  const send = message => {
    if (!stdoutOpen) return;
    // JSON.stringify escapes every control character, so the line can never contain a raw
    // newline; this is the only place in the bridge that writes to stdout.
    try { stdout.write(`${JSON.stringify(message)}\n`); } catch { stdoutOpen = false; }
  };
  const server = new McpServer({ tools, send, logger, limits });
  const splitter = new LineSplitter({ onLine: line => { server.handleLine(line).catch(() => {}); }, onOversize: () => server.oversized() });

  let exiting = false;
  const stop = async (reason, code = 0) => {
    if (exiting) return; exiting = true;
    logger.info('shutdown', { reason });
    await server.shutdown();
    stdoutOpen = false;
    // Pipe writes are asynchronous on macOS; give already-queued responses a moment to
    // reach the client before exiting, without letting a stuck reader delay exit.
    if (stdout.writableLength > 0) await new Promise(resolve => { const timer = setTimeout(resolve, 250); stdout.once?.('drain', () => { clearTimeout(timer); resolve(); }); });
    exit(code);
  };

  stdin.on('data', chunk => splitter.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
  stdin.on('end', () => { splitter.end(); stop('stdin_eof'); });
  stdin.on('close', () => stop('stdin_closed'));
  stdin.on('error', () => stop('stdin_error'));
  stdout.on?.('error', () => { stdoutOpen = false; stop('stdout_error'); });
  logger.info('ready', { key_configured: key.length > 0 });
  return { server, stop, logger };
}
