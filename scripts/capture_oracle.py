#!/usr/bin/env python3
"""One-shot capture of legacy gps_metadata output for a station marker.

Run after recording the matching VCR cassette; commits a deterministic
JSON snapshot to tests/_oracle_outputs/<MARKER>_legacy.json that becomes
the byte-equality oracle for future `station_sessions`-style composers.

Workflow:

    # 1. Record cassette (one network round-trip)
    pytest tests/test_composer_oracle.py::test_rhof_legacy_synthesis_matches_snapshot \\
        --record-mode=once

    # 2. Capture snapshot from the same cassette (no network)
    python scripts/capture_oracle.py RHOF

    # 3. Verify replay (no network)
    pytest tests/test_composer_oracle.py --record-mode=none

The cassette path used here mirrors the test's @pytest.mark.vcr default —
edit `_cassette_path` below if either side moves.
"""

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import vcr  # noqa: E402
from _canonicalize import canonicalize  # noqa: E402

from tostools import gps_metadata_qc  # noqa: E402

CASSETTE_PATH = (
    REPO_ROOT
    / "tests"
    / "cassettes"
    / "test_composer_oracle"
    / "test_{marker_slug}_legacy_synthesis_matches_snapshot.yaml"
)
SNAPSHOT_DIR = REPO_ROOT / "tests" / "_oracle_outputs"


def _json_default(obj):
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    raise TypeError(f"Not JSON-serialisable: {type(obj).__name__}")


def _capture(marker: str) -> Path:
    cassette = Path(str(CASSETTE_PATH).format(marker_slug=marker.lower()))
    if not cassette.exists():
        sys.exit(
            f"Cassette not found: {cassette}\n"
            f"Record it first with:\n"
            f"  pytest tests/test_composer_oracle.py::"
            f"test_{marker.lower()}_legacy_synthesis_matches_snapshot "
            f"--record-mode=once"
        )

    my_vcr = vcr.VCR(
        filter_headers=["Authorization", "Cookie", "Set-Cookie"],
        match_on=["method", "scheme", "host", "path", "query"],
        decode_compressed_response=True,
        record_mode="none",
    )

    with my_vcr.use_cassette(str(cassette)):
        result = gps_metadata_qc.gps_metadata(
            marker,
            gps_metadata_qc.URL_REST_TOS,
            loglevel=logging.WARNING,
        )

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = SNAPSHOT_DIR / f"{marker}_legacy.json"
    canonical = canonicalize(result)
    snapshot.write_text(
        json.dumps(canonical, sort_keys=True, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture canonical gps_metadata snapshot from VCR cassette"
    )
    parser.add_argument("marker", help="Station marker (e.g. RHOF)")
    args = parser.parse_args()

    written = _capture(args.marker.upper())
    print(f"Wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
