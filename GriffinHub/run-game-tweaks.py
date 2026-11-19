#!/usr/bin/env python3
"""Launch Griffin Game Tweaks from a source checkout."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from griffin.ui import run_app  # noqa: E402

if __name__ == "__main__":
    run_app()
