#!/usr/bin/env python3
"""Offline assertion entry point for a pre-created Shadeform job receipt."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.harness.shadeform_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
