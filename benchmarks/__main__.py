"""``python -m benchmarks``: the same entry point as ``python benchmarks/run.py``."""

from __future__ import annotations

import sys

from benchmarks.run import main

if __name__ == "__main__":
    sys.exit(main())
