# Local Assistant Engine — Windows x64 fixture package

This is a REL-001 package skeleton, not a runnable release. It contains no
model weights, compiler, Node modules, credentials, or installer. The approved
Qwen3.5-9B Q4_K_M model must remain a separately verified local file. Set the
absolute `model_path`, exact `model_size_bytes`, and exact `model_sha256` in
`config.local.json`; `backend_profile` defaults to `cpu` and no model fallback
is permitted. Supply `LAE_ENGINE_TOKEN` through the protected launch
environment before invoking `Start-LocalAssistant.ps1`.

Before any release claim, provide the real `lae-engine-cpu.exe`, run the
allowlist/dependency/secret/weight scanner, generate checksums and SBOM, and
complete native Windows launch and offline acceptance. No Shadeform, Intel,
real-model, or native-Windows evidence is implied by this tree.
