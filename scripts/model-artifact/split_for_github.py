#!/usr/bin/env python3
"""Split the pinned model into GitHub-release-sized parts, with checksums.

GitHub release assets must be under 2 GiB each (a git repository file under
100 MB, and Git LFS free-tier files under 2 GiB, are the other routes and do not
fit a 5.6 GB file either). The product model is split into parts of at most
1,900,000,000 bytes: `Qwen3.5-9B-Q4_K_M.gguf.part-001`, `-002`, `-003`.

Writes into --out: the parts, SHA256SUMS.txt (parts and the whole file),
MANIFEST.json, and the upstream LICENSE/NOTICE when given. It then reassembles
the parts in memory-bounded chunks and refuses to finish unless the result is
byte-identical to the input. `local/get_model.py` is the receiving side.

    python3 scripts/model-artifact/split_for_github.py \
        --model artifacts/qwen35-9b/Qwen3.5-9B-Q4_K_M.gguf --out /path/to/release-dir \
        [--license LICENSE]
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

NAME = "Qwen3.5-9B-Q4_K_M.gguf"
EXPECTED_SIZE = 5_629_109_088
EXPECTED_SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
PART_BYTES = 1_900_000_000  # < 2 GiB (2,147,483,648), the release-asset limit
CHUNK = 8 * 1024 * 1024


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--license", type=Path, help="the upstream Apache-2.0 LICENSE file to ship beside the parts")
    p.add_argument("--part-bytes", type=int, default=PART_BYTES)
    args = p.parse_args(argv)
    if not 1_000_000 <= args.part_bytes < 2 * 1024**3:
        sys.exit("--part-bytes must be between 1 MB and just under 2 GiB")
    model = args.model
    if model.name != NAME or not model.is_file() or model.stat().st_size != EXPECTED_SIZE:
        sys.exit(f"{model} is not the pinned {NAME} ({EXPECTED_SIZE} bytes)")
    print("hashing the model ...", flush=True)
    whole = sha256_of(model)
    if whole != EXPECTED_SHA256:
        sys.exit(f"model sha256 {whole} is not the pinned {EXPECTED_SHA256}")
    args.out.mkdir(parents=True, exist_ok=True)
    parts = []
    with model.open("rb") as src:
        index = 0
        while True:
            index += 1
            target = args.out / f"{NAME}.part-{index:03d}"
            written, h = 0, hashlib.sha256()
            with target.open("wb") as dst:
                while written < args.part_bytes:
                    block = src.read(min(CHUNK, args.part_bytes - written))
                    if not block:
                        break
                    dst.write(block)
                    h.update(block)
                    written += len(block)
            if written == 0:
                target.unlink()
                break
            parts.append({"file": target.name, "size_bytes": written, "sha256": h.hexdigest()})
            print(f"  {target.name}: {written} bytes", flush=True)
    if sum(part["size_bytes"] for part in parts) != EXPECTED_SIZE:
        sys.exit("parts do not add up to the model size")
    if args.license:
        shutil.copyfile(args.license, args.out / "LICENSE")
    (args.out / "NOTICE.txt").write_text(
        "Qwen3.5-9B-Q4_K_M.gguf is a Q4_K_M GGUF quantization (text-only: no vision projector, no MTP head) of\n"
        "Qwen/Qwen3.5-9B (revision c202236235762e1c871ad0ccb60c8ee5ba337b9a), (c) the Qwen team, licensed under the\n"
        "Apache License 2.0 (see LICENSE). The weights were converted and quantized; no other change was made.\n"
        "This repository is not affiliated with or endorsed by the Qwen team.\n", encoding="utf-8")
    manifest = {"schema": "local_bmo.github-model-release.v1", "file": NAME, "size_bytes": EXPECTED_SIZE,
                "sha256": EXPECTED_SHA256, "part_bytes": args.part_bytes, "parts": parts,
                "license": "Apache-2.0", "source": "Qwen/Qwen3.5-9B@c202236235762e1c871ad0ccb60c8ee5ba337b9a"}
    (args.out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (args.out / "SHA256SUMS.txt").write_text(
        "".join(f"{part['sha256']}  {part['file']}\n" for part in parts) + f"{EXPECTED_SHA256}  {NAME}\n", encoding="utf-8")
    # Prove the receiving side's job: join the parts and compare with the input.
    print("verifying reassembly ...", flush=True)
    h = hashlib.sha256()
    for part in parts:
        with (args.out / part["file"]).open("rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
    if h.hexdigest() != EXPECTED_SHA256:
        sys.exit("reassembled parts do not match the model")
    print(f"OK: {len(parts)} parts, reassembly sha256 matches {EXPECTED_SHA256[:12]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
