#!/usr/bin/env python3
"""Repository entry point for the allowlist package builder."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.clean_machine.package_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
