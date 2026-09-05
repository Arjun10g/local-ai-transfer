# Inert Win32 clipboard source

This directory is a source-review boundary for a future direct Win32 clipboard
broker. It is not compiled, imported, packaged, registered, or executable.
Production availability is false.

The public source interface exposes bounded `clipboard.read`, confirmed
`clipboard.write`, and a read-only lost-acknowledgement query. The immutable
activation check is before request or operating-system access. There is no
PowerShell, `clip.exe`, shell, child process, network, or model surface.

The latent path is intentionally conservative:

- exact active-console `WinSta0\Default` session/desktop only, with independent
  station/input handles, security-descriptor fingerprints, and active-input
  proof retained and revalidated around acquisition, mutation, and publication;
- opaque operation/session/expiry/digest/nonce/MAC capability with no issuer;
- nonconstructible, key-owning authority that recomputes the exact HMAC and
  compares the bound nonce/MAC in constant time;
- one five-second monotonic deadline and supervisor cancellation event;
- bounded retrying `OpenClipboard`, `CF_UNICODETEXT` only, strict scalar and
  UTF-8 conversion, and 64 KiB returned/input content cap;
- explicit all-format replacement confirmation and exact preview sequence;
- durable dispatch before `EmptyClipboard`, noncancellable commit, ownership
  transfer only after successful `SetClipboardData`, then durable typed
  outcome;
- pre-hash strict UTF-8/size/NUL checks and fail-stop cleanup on unlock, free,
  or clipboard-close ambiguity;
- sequence-bound, no-replay reconciliation whose ambiguous result is manual;
- content absent from receipts and errors.

The source cannot be activated by changing configuration or environment.
Independent audit plus reviewed implementation of every immutable-false gate,
remote Windows build/analysis, package integration review, and target
acceptance are mandatory.
