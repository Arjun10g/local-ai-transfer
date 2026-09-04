#!/usr/bin/env python3
"""CLI for the cross-platform static portable package scan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .package import scan_tree


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan an allowlisted Windows package tree")
    parser.add_argument("root")
    parser.add_argument("--require-runtime", action="store_true")
    args = parser.parse_args()
    result = scan_tree(Path(args.root), require_runtime=args.require_runtime)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
