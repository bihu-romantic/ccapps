#!/usr/bin/env python3
"""Compatibility wrapper — prefer scripts/xlsx_to_ccpl_json.py."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    target = Path(__file__).with_name("xlsx_to_ccpl_json.py")
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
