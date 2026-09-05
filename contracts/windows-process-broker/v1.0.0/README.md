# Windows process broker contract v1.0.0 (inactive)

Status: **NOT_READY**. This contract and its static fixtures describe a future
inherited-pipe boundary. They do not activate a capability. There is no
process-creation implementation, build target, package entry, host integration,
launcher integration, registry entry, non-empty trust anchor, or Windows
runtime evidence.

Messages use a four-byte unsigned big-endian length followed by one UTF-8 JSON
object. Request frames are at most 65,536 bytes and response frames at most
1,048,576 bytes. Duplicate keys, trailing data, unknown keys, invalid types,
control characters in arguments, excessive nesting, partial-frame stalls, and
out-of-range lengths or deadlines fail closed. Reads, response writes, I/O
cancellation, and worker joins have explicit bounds in the inactive source.

An invocation selects a manifest-pinned logical `action_id`. It cannot select a
manifest, executable, raw argv, cwd, environment, output path, or shell. Product
policy is stricter than JSON Schema alone and is enforced by the C++ parser:

- every launch action binds an immutable executable identity/class, cwd,
  action-policy ID, confinement profile, and action-specific argv template;
- interpreters, shells, generic script hosts, and common LOLBins are denied;
- every parameter is required, typed, bounded, used in its declared placement,
  and free of NUL/newline/control characters in manifest literals and finite
  values; canonical numeric values are range checked;
- process argv parameters require finite `allowed_values`;
- application argv is entirely fixed by the manifest;
- browser input is one bounded `https` URL;
- Copilot prompt content is bounded stdin only; and
- clipboard actions use manifest limits no larger than 64 KiB/five seconds.

The fixture manifest is `mode: fixture` and carries the explicit
`unproven-disabled` confinement marker. Product parsing rejects that marker and
the fixture mode. Even a valid product document could not start this source
because the compiled trust anchor is empty and the supervisor-containment and
launch-confinement activation constants are false.

The broker proposal rejects recently reused IDs with a bounded 1,024-entry
in-memory cache. This is not restart-safe replay protection. A durable host
action journal is mandatory before activation, including duplicate-action
blocking and unknown-after-dispatch recovery. The protocol's `accepted` status
must never be interpreted as completion.

Receipts omit paths, argv/argument values, environment, output content,
clipboard text, Copilot prompts, URLs, secrets, and token material. In this
inactive revision `process_created` is always `false`; receipts do not claim Job
assignment, confinement, reaping, navigation, or provider acknowledgement.

The Python tests are static contract checks only. They do not prove C++17
compilation, Windows behavior, process containment, security-token properties,
clipboard behavior, or runtime readiness. A later activation change requires
remote Windows build/test evidence and a new security review.
