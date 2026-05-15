"""Composer-oracle byte-equality harness.

Phase 1c sign-off (§9 byte-equality ladder, level 2) requires the new
``station_sessions`` composer to produce byte-equal output to legacy
``gps_metadata_qc.gps_metadata``. The legacy-side test locks the oracle
against a captured VCR cassette + JSON snapshot; the composer-side test
asserts the new chain reproduces the same ``device_history`` slice.

Each test gets its own cassette via the ``@pytest.mark.vcr`` default
path (``tests/cassettes/test_composer_oracle/test_<name>.yaml``).
"""

import json
import logging
from pathlib import Path

import pytest

from tests._canonicalize import canonicalize
from tostools import devices, gps_metadata_qc
from tostools.api.tos_client import TOSClient

TESTS_DIR = Path(__file__).resolve().parent
SNAPSHOTS = TESTS_DIR / "_oracle_outputs"


@pytest.mark.vcr
def test_rhof_legacy_synthesis_matches_snapshot():
    """gps_metadata('RHOF') against cassette must match the committed snapshot.

    The gps_metadata call runs unconditionally so VCR can record the cassette
    on a `--record-mode=once` run when the cassette is absent. The snapshot
    check is the assertion; missing snapshot is a failure with capture
    instructions, not a skip, so CI catches misconfigured fixtures.
    """
    result = gps_metadata_qc.gps_metadata(
        "RHOF",
        gps_metadata_qc.URL_REST_TOS,
        loglevel=logging.WARNING,
    )
    snapshot_path = SNAPSHOTS / "RHOF_legacy.json"
    if not snapshot_path.exists():
        pytest.fail(
            f"Snapshot missing: {snapshot_path}\n"
            f"Capture it from the recorded cassette with:\n"
            f"  python scripts/capture_oracle.py RHOF"
        )
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert canonicalize(result) == expected


@pytest.mark.vcr
def test_rhof_station_sessions_matches_legacy_device_history():
    """station_sessions('RHOF') must byte-equal gps_metadata('RHOF')['device_history'].

    This is the §9 byte-equality gate (level 2) for phase 1c — proves the
    new composer chain reproduces the legacy synthesis output for the
    RHOF reference fixture. Any divergence here means the slicer →
    pivot → device_structure chain has drifted from legacy behaviour.
    """
    client = TOSClient(base_url=gps_metadata_qc.URL_REST_TOS)
    hits = client.search_stations("RHOF", domains="geophysical")
    assert hits, "RHOF not found in TOS — cassette out of date?"
    station_id = hits[0]["id_entity"]

    result = devices.station_sessions(client, station_id)

    snapshot_path = SNAPSHOTS / "RHOF_legacy.json"
    expected_doc = json.loads(snapshot_path.read_text(encoding="utf-8"))
    expected = expected_doc["device_history"]

    assert canonicalize(result) == expected
