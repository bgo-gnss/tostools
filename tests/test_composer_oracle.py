"""Composer-oracle byte-equality harness.

Phase 1c sign-off (§9 byte-equality ladder, level 2) requires the new
``station_sessions`` composer to produce byte-equal output to legacy
``gps_metadata_qc.gps_metadata`` for stations where legacy is known
correct (RHOF, AKUR, VMEY, SKRO — clean attribute periods, no
overlapping device installations).

For stations where legacy is known *buggy* (AUST severe, REYK + HOFN
mild — see ``docs/architecture/synthesis-legacy-divergence.md``) we
instead lock the *new* composer output against its own captured
snapshot. That prevents accidental regressions to legacy semantics
without forcing the new code to reproduce known wrong output.

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


@pytest.mark.vcr
def test_rhof_gps_metadata_via_devices_matches_legacy_snapshot():
    """gps_metadata_via_devices('RHOF') must byte-equal legacy gps_metadata('RHOF').

    Locks the phase-4 adapter against the same RHOF snapshot as the
    legacy chain. This is the integration-level gate: the adapter
    not only produces the right device_history list but also wraps
    it in the legacy station-shaped dict (top-level marker, name,
    lat, lon, altitude, contact, ...). Any divergence here means
    either the adapter dropped a field or the station-metadata
    fetch path drifted from legacy.
    """
    result = gps_metadata_qc.gps_metadata_via_devices(
        "RHOF",
        gps_metadata_qc.URL_REST_TOS,
        loglevel=logging.WARNING,
    )
    snapshot_path = SNAPSHOTS / "RHOF_legacy.json"
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert canonicalize(result) == expected


@pytest.mark.vcr
def test_aust_station_sessions_locked():
    """station_sessions('AUST') must match its captured new-behaviour snapshot.

    AUST is the canonical "legacy is wrong" fixture: 17 of legacy's 24
    sessions emerge with ``time_to < time_from`` because the legacy
    slicer under-emits sub-windows when attribute periods misalign, and
    the pivot's independent-iter zip then pairs unrelated boundaries.
    The new chain produces 26 well-ordered sessions; this test locks
    that output so future refactors can't accidentally regress to the
    legacy pair-based behaviour. See
    ``docs/architecture/synthesis-legacy-divergence.md`` for the full
    write-up of the two legacy bugs and the new-behaviour contract.
    """
    client = TOSClient(base_url=gps_metadata_qc.URL_REST_TOS)
    hits = client.search_stations("AUST", domains="geophysical")
    assert hits, "AUST not found in TOS — cassette out of date?"
    station_id = hits[0]["id_entity"]

    result = devices.station_sessions(client, station_id)

    snapshot_path = SNAPSHOTS / "AUST_new.json"
    if not snapshot_path.exists():
        pytest.fail(
            f"Snapshot missing: {snapshot_path}\n"
            f"Capture it from the recorded cassette with:\n"
            f"  python scripts/capture_oracle.py AUST --source new"
        )
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert canonicalize(result) == expected
