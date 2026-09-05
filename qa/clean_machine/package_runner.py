#!/usr/bin/env python3
"""Fail-closed placeholder for the Windows portable package builder."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

NODE_VERSION = "24.20.0"
NODE_EXE_SHA256 = "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"
NODE_LICENSE_SHA256 = "5888dbb9a1d2b18f2c3e6c5f6af1b39de658372b402a0577b002777f14c62ace"
PACKAGE_BUILD_BLOCKER = "secure-handle-relative-package-builder-unavailable"
PACKAGE_READINESS = "NOT_READY"


def build_package(source: Path, engine: Path, node: Path, node_license: Path, output: Path) -> dict[str, object]:
    """Refuse before resolving, opening, hashing, creating, or copying a path."""

    del source, engine, node, node_license, output
    return {
        "status": "FAIL",
        "readiness": PACKAGE_READINESS,
        "stage": "safety-unavailable",
        "findings": [PACKAGE_BUILD_BLOCKER],
        "output_created": False,
        "native_windows_launch": "REFUSED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--node-license", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = build_package(Path(args.source), Path(args.engine), Path(args.node), Path(args.node_license), Path(args.output))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
