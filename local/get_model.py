#!/usr/bin/env python3
"""Fetch the pinned model from a GitHub release and put it back together.

For a machine that cannot reach Hugging Face. The model is published as parts
under 2 GiB (the release-asset limit); this downloads them (resuming a broken
download), checks each part, joins them, and refuses to finish unless the result
has the pinned size and SHA-256 compiled into THIS file, so a tampered manifest
or part cannot produce a model the engine would accept (the engine checks again).

    # public release
    python3 local/get_model.py --base-url https://github.com/OWNER/REPO/releases/download/TAG --out C:\\bmo-transfer
    # parts already downloaded (browser, `gh release download`, USB): no network
    python3 local/get_model.py --folder C:\\Users\\me\\Downloads\\parts --out C:\\bmo-transfer

Needs only Python 3.10+. Writes <out>/Qwen3.5-9B-Q4_K_M.gguf (5,629,109,088 bytes);
needs about 11.5 GB free while joining (parts + result), and --delete-parts then
gives back the parts' 5.6 GB.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

NAME = "Qwen3.5-9B-Q4_K_M.gguf"
SIZE = 5_629_109_088
SHA256 = "c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b"
CHUNK = 8 * 1024 * 1024
MAX_PARTS = 8


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, target: Path, expected_size: int) -> None:
    """Download `url` to `target`, resuming from what is already there."""
    have = target.stat().st_size if target.exists() else 0
    if have > expected_size:
        target.unlink()
        have = 0
    if have == expected_size:
        return
    request = urllib.request.Request(url, headers={"User-Agent": "bmo-get-model"})
    if have:
        request.add_header("Range", f"bytes={have}-")
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        if error.code == 416:  # nothing left to send: the size check below decides
            return
        raise
    with response:
        if have and response.status != 206:  # the server ignored Range: start over
            have = 0
        with target.open("ab" if have else "wb") as out:
            done = shown = have
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if done - shown >= expected_size // 50 or done == expected_size:  # about every 2%
                    shown = done
                    print(f"\r  {target.name}: {done / 1e6:,.0f} / {expected_size / 1e6:,.0f} MB", end="", flush=True)
    print()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--base-url", help="release download URL, without the file name")
    where.add_argument("--folder", type=Path, help="a folder that already holds MANIFEST.json and the parts")
    p.add_argument("--out", required=True, type=Path, help="where to write the model")
    p.add_argument("--delete-parts", action="store_true", help="remove the downloaded parts after a verified join")
    args = p.parse_args(argv)

    final = args.out / NAME
    if final.is_file() and final.stat().st_size == SIZE and sha256_of(final) == SHA256:
        print(f"{final} is already the pinned model.")
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    work = args.folder if args.folder else args.out / "model-parts"
    work.mkdir(parents=True, exist_ok=True)

    manifest_path = work / "MANIFEST.json"
    if args.base_url:
        base = args.base_url.rstrip("/")
        request = urllib.request.Request(f"{base}/MANIFEST.json", headers={"User-Agent": "bmo-get-model"})
        with urllib.request.urlopen(request, timeout=60) as response:
            manifest_path.write_bytes(response.read(1 << 20))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parts = manifest.get("parts")
    # The pinned identity is this file's, not the manifest's.
    if (manifest.get("file") != NAME or manifest.get("size_bytes") != SIZE or manifest.get("sha256") != SHA256
            or not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS
            or sum(part.get("size_bytes", 0) for part in parts) != SIZE):
        sys.exit("MANIFEST.json does not describe the pinned model; refusing")
    for part in parts:
        name = part.get("file", "")
        if name != Path(name).name or not name.startswith(NAME + ".part-"):
            sys.exit(f"unexpected part name {name!r}; refusing")

    for part in parts:
        target = work / part["file"]
        if args.base_url:
            fetch(f"{base}/{part['file']}", target, part["size_bytes"])
        if not target.is_file() or target.stat().st_size != part["size_bytes"]:
            sys.exit(f"{target} is missing or the wrong size ({part['size_bytes']} bytes expected); run again to resume")
        print(f"  checking {part['file']} ...", flush=True)
        if sha256_of(target) != part["sha256"]:
            target.unlink()
            sys.exit(f"{part['file']} is corrupt and was deleted; run again to fetch it afresh")

    free = shutil.disk_usage(args.out).free
    if free < SIZE + (1 << 28):
        sys.exit(f"not enough free disk space to join: need {SIZE / 1e9:.1f} GB, have {free / 1e9:.1f} GB")
    temp = args.out / (NAME + ".joining")
    h = hashlib.sha256()
    print("  joining ...", flush=True)
    with temp.open("wb") as out:
        for part in parts:
            with (work / part["file"]).open("rb") as f:
                for block in iter(lambda: f.read(CHUNK), b""):
                    out.write(block)
                    h.update(block)
    if temp.stat().st_size != SIZE or h.hexdigest() != SHA256:
        temp.unlink()
        sys.exit("joined file does not match the pinned size and SHA-256; deleted")
    os.replace(temp, final)
    print(f"OK: {final} ({SIZE:,} bytes, sha256 {SHA256[:12]}...)")
    if args.delete_parts and args.base_url:
        shutil.rmtree(work, ignore_errors=True)
        print("parts removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
