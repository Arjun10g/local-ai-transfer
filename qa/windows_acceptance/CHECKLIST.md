# Dell target acceptance checklist

Every box needs a receipt or an explicit rejection record. A verbal statement
or screenshot alone is not a READY gate.

- [ ] Transfer approvals, release/model/candidate hashes, malware/DLP results,
  and synthetic live-test destinations are approved.
- [ ] Run as a standard user from local non-reparse paths; perform no install,
  download, build, elevation, policy change, or driver change.
- [ ] Capture exact Windows edition/version/build/architecture.
- [ ] Capture exact CPU name, ProcessorId, topology, and 64-bit width; preserve
  the reported vPro Enterprise tier as a claim until the exact SKU is reviewed.
- [ ] Match Dell board `039NNG` revision `A00` and record redacted BIOS fields.
- [ ] Match 32 GiB DIMM total and 5600 MT/s configured speed.
- [ ] Match one Intel Graphics WMI and display-PNP identity on driver
  `32.0.101.8247`.
- [ ] Bind an approved `vulkaninfo.exe` hash to successful Intel device/API
  enumeration; absence is unknown/unproven, not unsupported.
- [ ] Verify the generated portable release and pinned Node runtime.
- [ ] CPU: exact product backend/model loads, bounded synthetic chat and cancel
  pass, loopback-only auth remains enforced, graceful shutdown has no orphan.
- [ ] Portable lifecycle: current-user named pipes, post-Job launch gate,
  ShellExecute browser handoff, immediate fragment clearing, no query/referrer,
  hostile/replay rejection, bearer-protected API, and abrupt Job cleanup pass.
- [ ] Console/receipt scan finds no bearer, nonce, prompt/response, message,
  mail, token, credential, URL query, or environment dump.
- [ ] Vulkan candidate is attempted by exact binary/device/layer plan with no
  fallback; record `PASS` or `REJECTED_WITH_EVIDENCE`. Do not promote it here.
- [ ] If full access is claimed, opt in with the exact consent phrase and pass
  every Graph/Teams/Outlook/browser/Copilot check using only synthetic accounts,
  approved test pages, and a disposable workspace.
- [ ] Run the offline verifier against the two-file receipt bundle.
- [ ] Sol reviews residual limitations and records the final Phase 8 decision.
