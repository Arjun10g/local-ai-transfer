#!/usr/bin/env python3
"""Development entry point for the planner shipped in the Windows package."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    planner = Path(__file__).resolve().parents[1] / "release" / "windows" / "windows_backend_plan.py"
    runpy.run_path(str(planner), run_name="__main__")
