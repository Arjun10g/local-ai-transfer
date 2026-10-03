// Small HTTP helpers for the delegate routes.  They mirror the host's own
// (host/server/host-server.mjs) so the delegate surface answers with the
// same headers and the same strict-JSON parser, without reaching into the
// host's private helpers.
import { parseStrictJson } from '../agent/tool-envelope.mjs';

const SECURITY_HEADERS = Object.freeze({ 'content-security-policy': "default-src 'none'; frame-ancestors 'none'", 'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer' });

export function sendJson(res, status, value, extraHeaders = {}) {
  if (res.destroyed || res.writableEnded) return;
  const body = JSON.stringify(value);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body), 'cache-control': 'no-store', ...SECURITY_HEADERS, ...extraHeaders });
  res.end(body);
}

/** 409/429 carry how long to wait, in the body and as Retry-After. */
export function sendRetry(res, status, error, retryAfterS) {
  const seconds = Math.max(1, Math.ceil(retryAfterS));
  sendJson(res, status, { error, retry_after_s: seconds }, { 'retry-after': String(seconds) });
}

export const jsonContentType = req => /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|utf8))?$/i.test(req.headers['content-type'] ?? '');

export class DelegateHttpError extends Error {
  constructor(status, code) { super(code); this.name = 'DelegateHttpError'; this.status = status; this.code = code; }
}

export async function readJsonBody(req, { maxBytes, timeoutMs, maxString }) {
  const declared = Number(req.headers['content-length']);
  if (Number.isSafeInteger(declared) && declared > maxBytes) throw new DelegateHttpError(413, 'body_too_large');
  let total = 0; const chunks = [];
  req.setTimeout(timeoutMs, () => req.destroy(new DelegateHttpError(408, 'request_timeout')));
  try {
    for await (const chunk of req) { total += chunk.length; if (total > maxBytes) throw new DelegateHttpError(413, 'body_too_large'); chunks.push(chunk); }
  } finally { req.setTimeout(0); }
  if (!total) return {};
  try { return parseStrictJson(Buffer.concat(chunks).toString('utf8'), { maxBytes, maxDepth: 4, maxString, maxArray: 16, maxObject: 16 }); }
  catch { throw new DelegateHttpError(400, 'invalid_json'); }
}

/** Exactly these keys: unknown or missing ones are refused, never ignored. */
export function exactKeys(input, allowed, required = []) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new DelegateHttpError(400, 'invalid_request_body');
  if (Object.keys(input).some(key => !allowed.includes(key)) || required.some(key => !Object.hasOwn(input, key))) throw new DelegateHttpError(400, 'invalid_request_body');
  return input;
}
