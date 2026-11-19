"""
Griffin Hub application entry (package mode).

For the single-file / AppImage path, prefer griffinhub_legacy.py or the
bundled copy under usr/src.  This module exists so `python -m griffinhub`
works when the package is on PYTHONPATH.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    # Prefer the full legacy module (complete UI) if present next to the package
    # or as griffinhub_legacy in the artifacts root.
    candidates = [
        Path(__file__).resolve().parent.parent / "griffinhub_legacy.py",
        Path(__file__).resolve().parent / "griffinhub_legacy.py",
        Path(__file__).resolve().parents[1] / "griffinhub.py",
    ]
    for c in candidates:
        if c.exists():
            # Execute as __main__ so CLI + GUI entry points work
            sys.argv[0] = str(c)
            runpy.run_path(str(c), run_name="__main__")
            return

    print(
        "Griffin Hub: full application module not found.\n"
        "Expected griffinhub_legacy.py alongside the package.\n"
        "Run: python3 griffinhub_legacy.py",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
