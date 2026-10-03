// What the user is shown when asked to approve a side-effecting tool call.
//
// The confirmation card used to carry only the call id and tool name, so the
// user approved blind.  This builds a DISPLAY COPY of the arguments: per tool,
// a fixed set of bounded fields, sanitized for display and screened for
// credential-shaped text.  It never returns whole file contents, it never
// mutates the call, and execution always uses the original arguments.
//
// Screening masks only the credential-shaped VALUE and keeps the text around
// it.  Hiding a whole value because some part of it looked like a secret
// blinded the card: `password=x` placed in front of a hostile payload made
// the user approve text they could not see.
import { utf8Bytes } from './context-budget.mjs';

export const EXCERPT_CHARS = 200;
// Long values show their end too, so something appended after the head is
// still on the card.
export const EXCERPT_TAIL_CHARS = 200;
const FIELD_CHARS = 512;
const ARGV_ITEMS = 32;
const ARGV_ITEM_CHARS = 256;
// Screening a value is linear, but a 2 MiB replacement is the largest
// argument any local tool accepts; beyond that the value is not shown at all.
const SCREEN_MAX_CHARS = 2 * 1024 * 1024;
export const REDACTED = '[redacted: looks like a credential]';
export const MASK = '[redacted]';
export const DISPLAY_DIFF_CHARS = 8192;
const DISPLAY_DIFF_LINES = 400;

// The same credential vocabulary the remote-eval receipt screen uses
// (scripts/j1m_runner.py `_CREDENTIAL_NAME`, `_BEARER_VALUE`,
// `_URL_CREDENTIAL_QUERY`, `_URL_USERINFO`, `_CREDENTIAL_HEADER`), plus the
// common provider key shapes, so the two screens agree on what a secret
// looks like.  A display screen may over-redact; it must not under-redact.
const CREDENTIAL_NAME = /(?:^|[_.-])(?:access[_-]?token|account[_-]?key|api[_-]?key|auth(?:orization)?|bearer|client[_-]?secret|credential|cookie|hf[_-]?token|pass|passwd|password|pwd|secret|shared[_-]?access[_-]?(?:key|signature)|token|x[_-]api[_-]?key)(?:$|[_.-])/iu;
// camelCase names (`dbPassword`, `AccountKey`) are split at the case change
// so the delimiter-anchored vocabulary sees `db_Password`, `Account_Key`.
const credentialName = name => CREDENTIAL_NAME.test(name.replace(/([a-z0-9])([A-Z])/gu, '$1_$2'));
const ASSIGNMENT_NAME = /(?<![A-Za-z0-9])['"]?([A-Za-z][A-Za-z0-9_.-]{0,127})['"]?\s*[:=]/gu;
const BEARER = /\bbearer\s+[^\s,;]+/giu;
const URL_QUERY_CREDENTIAL = /[?&#](?:access[_-]?token|api[_-]?key|auth(?:orization)?|credential|password|passwd|secret|sig|signature|token|x[-_]api[-_]key|x[-_]amz[-_]credential)=[^&#\s]*/giu;
const URL_USERINFO = /\b[a-z][a-z0-9+.-]*:\/\/[^/@\s]+@/giu;
const CREDENTIAL_HEADER = /^\s*(?:-+h\s*)?(authorization|proxy-authorization|x[-_]api[-_]key|api[-_]key|cookie)\s*:/iu;
const HEADER_SCHEME = /^(?:basic|bearer|digest|negotiate|ntlm|token)\s+/iu;
const PEM_BEGIN = /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/u;
const PEM_END = /-----END [A-Z0-9 ]*PRIVATE KEY-----/u;
const TOKEN_SHAPES = [
  /\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/gu,
  /\bsk-[A-Za-z0-9_-]{16,}/gu,
  // Stripe secret and restricted keys.
  /\b[rs]k_(?:live|test)_[A-Za-z0-9]{10,}/gu,
  // Google API keys.
  /\bAIza[0-9A-Za-z_-]{35}/gu,
  /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/gu,
  /\bgithub_pat_[A-Za-z0-9_]{20,}/gu,
  /\bhf_[A-Za-z0-9]{20,}/gu,
  /\bAKIA[0-9A-Z]{16}\b/gu,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/gu,
];
// The host's own bearer token and bootstrap nonce: 32 random bytes as
// base64url, 43 characters.  At least two of upper/lower/digit, so a long
// snake_case identifier is not taken for one (a 40-hex git sha is too short).
const BASE64URL_43 = /(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])/gu;
const mixedClasses = text => [/[A-Z]/u, /[a-z]/u, /[0-9]/u].filter(pattern => pattern.test(text)).length >= 2;

// Look-alike letters fold to the Latin letter they imitate (NFKC does not
// fold Cyrillic or Greek), and invisible format characters are skipped, so
// `pаssword=` (Cyrillic а) or `pass\u200Bword=` cannot slip past the names.
const CONFUSABLES = new Map(Object.entries({
  а: 'a', в: 'b', е: 'e', і: 'i', ј: 'j', к: 'k', м: 'm', н: 'h', о: 'o', р: 'p', с: 'c', т: 't', у: 'y', х: 'x', ѕ: 's', ԁ: 'd', ԛ: 'q', ԝ: 'w',
  А: 'A', В: 'B', Е: 'E', І: 'I', Ј: 'J', К: 'K', М: 'M', Н: 'H', О: 'O', Р: 'P', С: 'C', Т: 'T', У: 'Y', Х: 'X', Ѕ: 'S',
  α: 'a', ε: 'e', ι: 'i', κ: 'k', ν: 'v', ο: 'o', ρ: 'p', τ: 't', υ: 'u', χ: 'x',
  Α: 'A', Β: 'B', Ε: 'E', Ζ: 'Z', Η: 'H', Ι: 'I', Κ: 'K', Μ: 'M', Ν: 'N', Ο: 'O', Ρ: 'P', Τ: 'T', Υ: 'Y', Χ: 'X',
}));
const FORMAT_CHARACTER = /\p{Cf}/u;
// The matching copy of one line, with each of its characters' offsets in the
// original so a span found in the copy masks the right original text.
function skeleton(line) {
  let text = ''; const starts = []; const ends = []; let offset = 0;
  for (const character of line) {
    const next = offset + character.length;
    if (!FORMAT_CHARACTER.test(character)) {
      const folded = CONFUSABLES.get(character) ?? character.normalize('NFKC');
      for (const unit of folded) { text += unit; for (let i = 0; i < unit.length; i += 1) { starts.push(offset); ends.push(next); } }
    }
    offset = next;
  }
  return { text, starts, ends };
}

// Spans (in skeleton offsets) holding a credential VALUE on one line.
function credentialSpans(text) {
  const spans = [];
  const header = CREDENTIAL_HEADER.exec(text);
  if (header) {
    let start = header.index + header[0].length; while (text[start] === ' ' || text[start] === '\t') start += 1;
    // A cookie header is all session values; other headers are a scheme
    // word plus one credential token, and anything after stays visible.
    if (/cookie/iu.test(header[1])) { if (start < text.length) spans.push([start, text.length]); }
    else { const scheme = HEADER_SCHEME.exec(text.slice(start)); const from = start + (scheme ? scheme[0].length : 0); const token = /^\S+/u.exec(text.slice(from)); if (token) spans.push([from, from + token[0].length]); }
  }
  for (const match of text.matchAll(BEARER)) { const value = /\S+$/u.exec(match[0]); spans.push([match.index + value.index, match.index + match[0].length]); }
  for (const match of text.matchAll(URL_QUERY_CREDENTIAL)) { const start = match.index + match[0].indexOf('=') + 1; if (start < match.index + match[0].length) spans.push([start, match.index + match[0].length]); }
  for (const match of text.matchAll(URL_USERINFO)) spans.push([match.index + match[0].indexOf('//') + 2, match.index + match[0].length - 1]);
  for (const pattern of TOKEN_SHAPES) for (const match of text.matchAll(pattern)) spans.push([match.index, match.index + match[0].length]);
  for (const match of text.matchAll(BASE64URL_43)) if (mixedClasses(match[0])) spans.push([match.index, match.index + match[0].length]);
  // `name = value` / `name: value` with a credential name: a quoted value
  // through its closing quote, otherwise one whitespace-free token, so a
  // command later on the same line is never hidden with it.
  for (const match of text.matchAll(ASSIGNMENT_NAME)) {
    // The header's own name was handled above, scheme word kept visible.
    if (!credentialName(match[1]) || (header && match.index < header.index + header[0].length)) continue;
    let start = match.index + match[0].length; while (text[start] === ' ' || text[start] === '\t') start += 1;
    if (start >= text.length) continue;
    const quote = text[start];
    if (quote === '"' || quote === "'") { const close = text.indexOf(quote, start + 1); spans.push([start, close === -1 ? text.length : close + 1]); continue; }
    // `Authorization: Bearer X` written inline: the scheme word stays
    // readable and the token after it is the value.
    const scheme = HEADER_SCHEME.exec(text.slice(start)); if (scheme && /^\S/u.test(text.slice(start + scheme[0].length))) start += scheme[0].length;
    // Inside a URL query the value ends at the next parameter.
    spans.push([start, start + (/[?&#]/u.test(text[match.index - 1] ?? '') ? /^[^\s&#]*/u : /^\S+/u).exec(text.slice(start))[0].length]);
  }
  return spans.filter(([start, end]) => end > start).sort((left, right) => left[0] - right[0]);
}
// A leading `- ` / `+ ` diff marker survives a whole-line mask.
const lineMarker = line => /^[-+] /u.exec(line)?.[0] ?? '';
function maskLine(line) {
  const view = skeleton(line); const spans = credentialSpans(view.text);
  if (!spans.length) return line;
  let out = ''; let cursor = 0;
  for (const [start, end] of spans) {
    const from = view.starts[start]; const to = view.ends[end - 1];
    if (to <= cursor) continue;
    out += line.slice(cursor, Math.max(cursor, from)) + (from >= cursor ? MASK : ''); cursor = to;
  }
  return out + line.slice(cursor);
}

// Masking screen for text the user must still be able to read: each
// credential-shaped value becomes MASK and everything else stays.  A private
// key block is masked from its header through its footer.  Display only:
// the tool always works on the real text.
export function maskCredentialText(text) {
  if (typeof text !== 'string' || !text) return { text: typeof text === 'string' ? text : '', masked: false };
  if (text.length > SCREEN_MAX_CHARS) return { text: MASK, masked: true };
  let inPem = false;
  const lines = text.split('\n').map(line => {
    if (inPem || PEM_BEGIN.test(skeleton(line).text)) { inPem = !PEM_END.test(skeleton(line).text); return lineMarker(line) + MASK + (line.endsWith('\r') ? '\r' : ''); }
    return maskLine(line);
  });
  const masked = lines.join('\n');
  return { text: masked, masked: masked !== text };
}
export function looksLikeCredential(value) {
  if (typeof value !== 'string' || !value) return false;
  return value.length > SCREEN_MAX_CHARS || maskCredentialText(value).masked;
}

// Controls and invisible formatting characters (bidi overrides, zero-width
// joiners) could make the card show something other than what will run.
const UNSAFE_DISPLAY = /[\p{Cc}\p{Cf}\u2028\u2029]/gu;
const sanitize = text => text.replace(UNSAFE_DISPLAY, character => (character === '\n' || character === '\t' ? character : '\uFFFD'));
const screen = value => sanitize(maskCredentialText(value).text);
// Code-point slicing, so an emoji is never split into a lone surrogate.
function clip(text, max) {
  const points = Array.from(text);
  return points.length > max ? { text: points.slice(0, max).join(''), clipped: true } : { text, clipped: false };
}
function field(value, max = FIELD_CHARS) {
  if (typeof value !== 'string') return null;
  if (value.length > SCREEN_MAX_CHARS) return REDACTED;
  const { text, clipped } = clip(screen(value), max);
  return text + (clipped ? '…' : '');
}
// `{label}_excerpt` is the first EXCERPT_CHARS characters of the screened
// value and `{label}_excerpt_tail` its last EXCERPT_TAIL_CHARS (null when the
// head already shows everything), with `{label}_chars` the full length, so
// text appended after the head is still seen.  `{label}_masked` says some
// value was replaced by MASK; `{label}_redacted` (the whole value hidden) is
// now true only for a value too large to screen.
function excerpt(label, value) {
  if (typeof value !== 'string') return {};
  if (value.length > SCREEN_MAX_CHARS) return { [`${label}_excerpt`]: null, [`${label}_excerpt_tail`]: null, [`${label}_redacted`]: true, [`${label}_masked`]: false, [`${label}_excerpt_truncated`]: false, [`${label}_chars`]: value.length };
  const masked = maskCredentialText(value); const points = Array.from(sanitize(masked.text));
  const truncated = points.length > EXCERPT_CHARS;
  const tail = truncated ? points.slice(Math.max(EXCERPT_CHARS, points.length - EXCERPT_TAIL_CHARS)).join('') : null;
  return { [`${label}_excerpt`]: points.slice(0, EXCERPT_CHARS).join(''), [`${label}_excerpt_tail`]: tail, [`${label}_redacted`]: false, [`${label}_masked`]: masked.masked, [`${label}_excerpt_truncated`]: truncated, [`${label}_chars`]: Array.from(value).length };
}
// Origin and path are what the user decides on; credential-named query
// parameters keep their names but lose their values.
function displayUrl(raw) {
  if (typeof raw !== 'string') return null;
  let url; try { url = new URL(raw); } catch { return field(raw); }
  url.username = ''; url.password = '';
  for (const key of [...url.searchParams.keys()]) if (credentialName(key) || looksLikeCredential(url.searchParams.get(key) ?? '')) url.searchParams.set(key, '[redacted]');
  if (url.hash && looksLikeCredential(url.hash)) url.hash = '[redacted]';
  const { text, clipped } = clip(url.href.replaceAll('%5Bredacted%5D', '[redacted]'), FIELD_CHARS);
  return sanitize(text) + (clipped ? '…' : '');
}
function argv(values) {
  if (!Array.isArray(values)) return null;
  return values.slice(0, ARGV_ITEMS).map(value => field(String(value), ARGV_ITEM_CHARS));
}
function parameters(values) {
  if (!values || typeof values !== 'object' || Array.isArray(values)) return null;
  const entries = Object.entries(values).slice(0, ARGV_ITEMS);
  return Object.fromEntries(entries.map(([key, value]) => {
    const name = field(key, 64);
    // The parameter's own name says the whole value is the secret.
    if (credentialName(key)) return [name, REDACTED];
    return [name, typeof value === 'string' ? field(value, ARGV_ITEM_CHARS) : (typeof value === 'number' || typeof value === 'boolean' ? value : null)];
  }));
}

// The fs.apply_patch preview diff as the UI receives it.  The tool emits
// `--- P\n+++ P\n- OLD\n+ NEW` (cut at 8,192 characters): only the first line
// of OLD and of NEW carries a marker, so classifying lines by their leading
// character let a replacement line starting with `-` or `@@` pass for a
// removal or a hunk header.  The split between OLD and NEW is recovered from
// structure instead: NEW is the call's own replacement text and OLD is
// exactly `old_bytes` UTF-8 bytes.  When that cannot be established every
// line is context, never a guess.  Each line is credential-masked and
// sanitized, and the legacy `diff` string is rebuilt with one marker per line
// (`- `, `+ `, two spaces for context, `--- `/`+++ ` headers) so a renderer
// keyed on the first character also classifies correctly.
function splitPatchDiff(diff, path, oldBytes, replacement) {
  if (typeof path !== 'string' || typeof replacement !== 'string' || !Number.isSafeInteger(oldBytes)) return null;
  const head = `--- ${path}\n+++ ${path}\n- `;
  if (!diff.startsWith(head)) return null;
  const body = diff.slice(head.length); const joined = `\n+ ${replacement}`;
  let bytes = 0; let index = 0;
  while (index < body.length && bytes < oldBytes) { const point = body.codePointAt(index); const width = point > 0xffff ? 2 : 1; bytes += utf8Bytes(body.slice(index, index + width)); index += width; }
  if (bytes > oldBytes) return null;
  // The body ended inside OLD: only a cut diff may do that.
  if (index === body.length) return diff.length >= DISPLAY_DIFF_CHARS ? { old: body, added: null } : null;
  const rest = body.slice(index);
  if (!joined.startsWith(rest)) return null;
  return { old: body.slice(0, index), added: rest.length >= 3 ? rest.slice(3) : '' };
}
export function displayDiff(diff, { path, oldBytes, replacement } = {}) {
  if (typeof diff !== 'string') return null;
  const parts = splitPatchDiff(diff, path, oldBytes, replacement);
  const lines = [];
  const add = (kind, text) => { for (const line of maskCredentialText(text).text.split('\n')) lines.push({ kind, text: sanitize(line.endsWith('\r') ? line.slice(0, -1) : line) }); };
  if (parts) {
    lines.push({ kind: 'header', text: sanitize(`--- ${path}`) }, { kind: 'header', text: sanitize(`+++ ${path}`) });
    add('removed', parts.old); if (parts.added !== null) add('added', parts.added);
  } else add('context', diff);
  const MARKERS = { header: '', removed: '- ', added: '+ ', context: '  ' };
  const kept = []; let chars = 0; let truncated = diff.length >= DISPLAY_DIFF_CHARS;
  for (const line of lines) {
    const rendered = MARKERS[line.kind] + line.text;
    if (kept.length >= DISPLAY_DIFF_LINES || chars + rendered.length + 1 > DISPLAY_DIFF_CHARS) { truncated = true; break; }
    kept.push(line); chars += rendered.length + 1;
  }
  return { diff: kept.map(line => MARKERS[line.kind] + line.text).join('\n'), diff_lines: kept, diff_structured: parts !== null, diff_truncated: truncated, diff_redacted: lines.some(line => line.text.includes(MASK)) };
}

// Returns null for a tool with no summary: the event then simply omits it.
export function summarizeToolArguments(name, args, preview) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return null;
  const view = preview && typeof preview === 'object' && !Array.isArray(preview) ? preview : {};
  switch (name) {
    case 'fs.write_new':
      return { kind: 'file_create', workspace_id: field(args.workspace_id, 64), path: field(args.path), content_bytes: utf8Bytes(args.content), ...excerpt('content', args.content) };
    case 'fs.apply_patch': {
      const replacement = args.replacement ?? args.patch;
      return {
        kind: 'file_replace', workspace_id: field(args.workspace_id, 64), path: field(args.path),
        old_bytes: Number.isSafeInteger(view.old_bytes) ? view.old_bytes : null, new_bytes: utf8Bytes(replacement),
        changed: typeof view.changed === 'boolean' ? view.changed : null, ...excerpt('replacement', replacement),
      };
    }
    case 'process.run_allowlisted':
      return {
        kind: 'process_run', action_id: field(args.action_id, 64), executable: field(view.executable),
        argv: argv(view.argv), cwd: field(view.cwd), parameters: parameters(args.parameters),
      };
    case 'browser.open_url':
      return { kind: 'open_url', url: displayUrl(args.url) };
    case 'app.open':
      return { kind: 'open_app', app_id: field(args.app_id, 64) };
    case 'clipboard.write':
      return { kind: 'clipboard_write', text_bytes: utf8Bytes(args.text), ...excerpt('text', args.text) };
    default:
      return null;
  }
}
