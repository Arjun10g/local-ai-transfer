#!/usr/bin/env python3
"""Project approved Shadeform dotenv keys from a mixed donor file.

This command is offline and one-way: it never contacts Shadeform, invokes a
provider, or prints assignment values.  The donor may contain unrelated valid
assignments; only the exact Local BMO mutation-key allowlist is selected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import shadeform_lifecycle as lifecycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="mixed donor dotenv path")
    parser.add_argument(
        "--destination", type=Path, required=True,
        help=(
            "new dotenv path beneath an existing effective-user-owned directory "
            "with no group/world permission bits (mode 0700 recommended for projection)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        report = lifecycle.project_mutation_env(
            args.source, args.destination,
        )
    except (OSError, UnicodeError, ValueError, lifecycle.ShadeformError) as exc:
        print(json.dumps({"status": "refused", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
