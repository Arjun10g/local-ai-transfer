#!/usr/bin/env python3
"""Validate a redacted Shadeform job receipt without contacting a provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .shadeform import validate_job_manifest, warning_level


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Shadeform budget and cleanup receipt")
    parser.add_argument("job")
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    errors = validate_job_manifest(job)
    if errors:
        print(json.dumps({"status": "FAIL", "errors": errors}, indent=2))
        return 1
    print(json.dumps({"status": "PASS", "warning_levels": {str(n): warning_level(n) for n in (0, 50, 80, 100)}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
