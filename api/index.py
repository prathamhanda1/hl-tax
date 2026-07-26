"""
api/index.py — Vercel serverless entry point.

Vercel turns every file under `api/` into a serverless function and, for
Python, serves the module-level ASGI app named `app`. So this file is a
three-line adapter: point Python at the repo root, force the writable cache
onto /tmp, and re-export the FastAPI app that `webapp/server.py` already
builds. No routing, no logic, no duplication of the server.

WHY /tmp. A Vercel function's filesystem is read-only except for /tmp. The
fetch layer is the project's only writer (it caches raw API pulls), so
HL_TAX_DATA_DIR is set before `config` is imported and the cache lands
somewhere writable. /tmp is per-instance and evaporates when the instance is
recycled, which makes it a genuine cache and never a source of truth — exactly
what the fetch layer already assumes.
"""

import os
import sys
from pathlib import Path

# The repo root is this file's parent's parent; add it so `import config`,
# `import webapp…` resolve the same way they do locally.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must be set BEFORE config is imported (it reads this at module import time).
os.environ.setdefault("HL_TAX_DATA_DIR", "/tmp/hl-tax-data")

from webapp.server import app  # noqa: E402  (path setup must precede this)

# Vercel's Python runtime looks for a module-level ASGI callable named `app`.
__all__ = ["app"]
