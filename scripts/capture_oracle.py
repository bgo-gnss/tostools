#!/usr/bin/env python3
"""One-shot capture of synthesis output for a station marker.

Run after recording the matching VCR cassette; commits a deterministic
JSON snapshot to ``tests/_oracle_outputs/<MARKER>_<source>.json`` that
becomes the byte-equality oracle for future test runs.

Two ``--source`` modes:

* ``legacy`` (default) — calls ``gps_metadata_qc.gps_metadata`` and
  writes ``<MARKER>_legacy.json``. Used by the §9 byte-equality gate
  (level 2) to lock the historical synthesis output for stations where
  legacy is known correct (RHOF, AKUR, VMEY, SKRO).
* ``new`` — calls ``devices.station_sessions`` and writes
  ``<MARKER>_new.json``. Used to lock new-composer behaviour for
  stations where legacy is known buggy (AUST, REYK, HOFN). See
  ``docs/architecture/synthesis-legacy-divergence.md``.

Workflow::

    # 1. Record cassette (one network round-trip)
    pytest tests/test_composer_oracle.py::test_aust_station_sessions_locked \\
        --record-mode=once

    # 2. Capture snapshot from the same cassette (no network)
    python scripts/capture_oracle.py AUST --source new

    # 3. Verify replay (no network)
    pytest tests/test_composer_oracle.py --record-mode=none

The cassette path used here mirrors each test's @pytest.mark.vcr default —
edit ``_CASSETTES`` below if either side moves.
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

from tostools import devices, gps_metadata_qc  # noqa: E402
from tostools.api.tos_client import TOSClient  # noqa: E402

_CASSETTES = {
    "legacy": REPO_ROOT
    / "tests"
    / "cassettes"
    / "test_composer_oracle"
    / "test_{marker_slug}_legacy_synthesis_matches_snapshot.yaml",
    "new": REPO_ROOT
    / "tests"
    / "cassettes"
    / "test_composer_oracle"
    / "test_{marker_slug}_station_sessions_locked.yaml",
}
SNAPSHOT_DIR = REPO_ROOT / "tests" / "_oracle_outputs"


def _json_default(obj):
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    raise TypeError(f"Not JSON-serialisable: {type(obj).__name__}")


def _run_legacy(marker: str):
    return gps_metadata_qc.gps_metadata(
        marker,
        gps_metadata_qc.URL_REST_TOS,
        loglevel=logging.WARNING,
    )


def _run_new(marker: str):
    client = TOSClient(base_url=gps_metadata_qc.URL_REST_TOS)
    hits = client.search_stations(marker, domains="geophysical")
    if not hits:
        sys.exit(f"{marker} not found in TOS via cassette — re-record?")
    station_id = hits[0]["id_entity"]
    return devices.station_sessions(client, station_id)


_RUNNERS = {"legacy": _run_legacy, "new": _run_new}


def _capture(marker: str, source: str) -> Path:
    template = _CASSETTES[source]
    cassette = Path(str(template).format(marker_slug=marker.lower()))
    if not cassette.exists():
        sys.exit(
            f"Cassette not found: {cassette}\n"
            f"Record it first with:\n"
            f"  pytest tests/test_composer_oracle.py "
            f"-k {marker.lower()} --record-mode=once"
        )

    my_vcr = vcr.VCR(
        filter_headers=["Authorization", "Cookie", "Set-Cookie"],
        match_on=["method", "scheme", "host", "path", "query"],
        decode_compressed_response=True,
        record_mode="none",
    )

    with my_vcr.use_cassette(str(cassette)):
        result = _RUNNERS[source](marker)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = SNAPSHOT_DIR / f"{marker}_{source}.json"
    canonical = canonicalize(result)
    snapshot.write_text(
        json.dumps(canonical, sort_keys=True, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture canonical synthesis snapshot from VCR cassette"
    )
    parser.add_argument("marker", help="Station marker (e.g. RHOF, AUST)")
    parser.add_argument(
        "--source",
        choices=sorted(_RUNNERS),
        default="legacy",
        help=(
            "legacy = gps_metadata_qc.gps_metadata (default); "
            "new = devices.station_sessions"
        ),
    )
    args = parser.parse_args()

    written = _capture(args.marker.upper(), args.source)
    print(f"Wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
