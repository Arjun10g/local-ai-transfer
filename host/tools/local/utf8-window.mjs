// UTF-8-safe byte windows for the bounded read tools (POSIX and Windows).
//
// fs.read_text reads at most max_bytes from offset_bytes.  Either edge can
// land inside a multi-byte character, and the strict decoder would then
// report a perfectly valid non-ASCII file as not_text.  A window that does
// not start at byte 0 skips leading continuation bytes; a window that does
// not end at EOF backs off to the last complete character.  An incomplete
// sequence at real EOF is left in place so genuinely invalid text still fails.
const isContinuation = byte => (byte & 0xc0) === 0x80;
function sequenceLength(lead) { return lead < 0x80 ? 1 : (lead & 0xe0) === 0xc0 ? 2 : (lead & 0xf0) === 0xe0 ? 3 : (lead & 0xf8) === 0xf0 ? 4 : 1; }

// Returns { start, bytes }: `start` is how many leading bytes were skipped,
// so callers report offset + start as the real start of the returned text.
export function utf8Window(bytes, { atStart, atEnd }) {
  let start = 0; if (!atStart) while (start < Math.min(3, bytes.length) && isContinuation(bytes[start])) start++;
  let end = bytes.length;
  if (!atEnd) {
    let lead = end - 1; while (lead >= start && lead > end - 4 && isContinuation(bytes[lead])) lead--;
    if (lead >= start && lead + sequenceLength(bytes[lead]) > end) end = lead;
  }
  return { start, bytes: bytes.subarray(start, end) };
}
