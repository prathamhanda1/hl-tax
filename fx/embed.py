"""
fx/embed.py — ship the FX series where a serverless bundler can actually find it.

WHY THIS EXISTS. `fx/data/usdinr_reference.csv` is the series, and it is the
single source of truth. But Vercel's Python builder does not copy the project
directory into the lambda: it traces `import` statements from the entrypoint and
bundles the `.py` files it reaches. A CSV is imported by nothing, so it was
silently dropped and every request came back as

    FX coverage error … FX series file not found:
    /var/task/fx/data/usdinr_reference.csv

which reads like a stale-data problem and is not one — the file was absent, so no
`as_of` and no staleness ceiling could have helped. `includeFiles` is the
documented cure, but it is a `functions` property; under the legacy `builds`
config this project uses it is accepted and ignored (verified against the live
deployment: `/api/meta` kept reporting `fx_coverage_end: null`).

So the series also ships as a MODULE, `fx/_series_embedded.py`, which is a `.py`
file reached by a real import and therefore bundled by any Python packer that
works at all. This is deployment plumbing, not a second source of truth:

  * The CSV stays canonical. `fetch_fx` writes it, and regenerates the module
    from it in the same breath, so a refresh cannot update one and not the other.
  * The module is PLAIN CSV TEXT in a triple-quoted string, not base64 — an
    append-only refresh then shows up as appended lines in `git diff`, and the
    rates stay readable to anyone auditing where a tax number came from.
  * It carries the SHA-256 of the CSV bytes it was generated from, and
    `check()` (exercised by the test suite) fails loudly if the two drift.

The reader in `fx/fx.py` prefers the CSV and falls back to the module only when
the file is missing. Locally the CSV is always there and this code never runs;
on Vercel the fallback is the whole ballgame.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import config

# The import that does the actual work. It is TOP-LEVEL and unconditional on
# purpose: a bundler that traces imports only sees what it can read statically,
# so hiding this inside the function that needs it would defeat the one job this
# module has. The guard is for the moment before the file has ever been
# generated (a fresh clone, mid-reseed) — not a reason to make it lazy.
try:
    from fx import _series_embedded as _embedded
except Exception:                                  # noqa: BLE001
    _embedded = None

# The generated module. Underscore-prefixed so Vercel never mistakes it for a
# function entrypoint, and so it reads as generated rather than hand-edited.
EMBED_FILE = Path(__file__).resolve().parent / "_series_embedded.py"

_HEADER = '''\
"""
fx/_series_embedded.py — GENERATED. DO NOT EDIT BY HAND.

A verbatim copy of `fx/data/usdinr_reference.csv`, carried as a Python module so
that serverless bundlers which trace imports (Vercel's Python builder) ship the
FX series into the lambda. The CSV remains the source of truth; regenerate with

    python -m fx.fetch_fx --update      (or --reseed, or --embed to only rebuild)

`fx.embed.check()` asserts this file matches the CSV, and the test suite runs it,
so a hand-edit or a forgotten regeneration fails the build rather than quietly
serving stale rates.
"""

# SHA-256 of the CSV bytes this was generated from.
CSV_SHA256 = "{sha}"

CSV_TEXT = """\\
'''

_FOOTER = '"""\n'


def _normalize(raw: bytes) -> str:
    """CSV text with newlines normalized to \\n.

    The CSV is written with `newline=""` and so lands as CRLF on Windows and LF
    elsewhere. Embedding the bytes verbatim would make the generated module —
    and its checksum — differ by platform, turning `check()` into a machine-
    dependent coin flip. csv.reader treats either ending identically, so
    normalizing here costs nothing and makes the artifact reproducible.
    """
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate(csv_path: Path | None = None) -> Path:
    """Rewrite `_series_embedded.py` from the CSV. Returns the module path."""
    csv_path = Path(csv_path or config.FX_SERIES_FILE)
    text = _normalize(csv_path.read_bytes())

    # The payload sits in a triple-quoted string, so anything that could end or
    # escape that string would produce a module whose contents are not the CSV.
    # The schema (ISO dates, decimals, ASCII source/administrator names) can
    # never contain either, so this is a generator-time assertion, not a case to
    # handle: if it ever trips, the format changed and this file must change too.
    if '"""' in text or "\\" in text:
        raise ValueError(
            f"{csv_path} contains a quote-triple or a backslash; the embedded "
            f"module cannot carry it verbatim. Change the generator, do not "
            f"escape the data."
        )
    if not text.endswith("\n"):
        text += "\n"

    EMBED_FILE.write_text(
        _HEADER.format(sha=_sha_of(text)) + text + _FOOTER,
        encoding="utf-8",
        newline="\n",
    )

    # Refresh the cached module so a generate() and a read_embedded() in the
    # same interpreter agree — `--reseed` then a lookup, or the test suite.
    global _embedded
    try:
        import importlib                           # noqa: PLC0415
        if _embedded is None:
            from fx import _series_embedded        # noqa: PLC0415
            _embedded = _series_embedded
        else:
            _embedded = importlib.reload(_embedded)
    except Exception:                              # noqa: BLE001
        _embedded = None

    return EMBED_FILE


def read_embedded() -> str | None:
    """The embedded CSV text, or None if the module was never generated.

    A missing fallback returns None rather than raising: it must surface as the
    caller's own "series file not found" error, not as an ImportError from a
    file the reader has never heard of.
    """
    return getattr(_embedded, "CSV_TEXT", None)


def check(csv_path: Path | None = None) -> None:
    """Raise if the embedded module is missing or has drifted from the CSV."""
    csv_path = Path(csv_path or config.FX_SERIES_FILE)
    if not csv_path.exists():
        raise FileNotFoundError(f"no CSV to check against: {csv_path}")

    embedded = read_embedded()
    if embedded is None:
        raise AssertionError(
            f"{EMBED_FILE.name} is missing or unimportable. The FX series would "
            f"not reach a serverless bundle. Regenerate: python -m fx.fetch_fx --embed"
        )

    expected = _normalize(csv_path.read_bytes())
    if embedded != expected:
        raise AssertionError(
            f"{EMBED_FILE.name} has drifted from {csv_path.name} "
            f"(embedded sha {_sha_of(embedded)[:12]}, csv sha "
            f"{_sha_of(expected)[:12]}). Regenerate: python -m fx.fetch_fx --embed"
        )
