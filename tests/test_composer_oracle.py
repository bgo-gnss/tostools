"""Composer-oracle byte-equality harness.

Phase 1c sign-off (§9 byte-equality ladder, level 2) requires the future
`station_sessions` composer to produce byte-equal output to legacy
`gps_metadata_qc.gps_metadata`. This test locks the *legacy* side of that
comparison against a captured VCR cassette + JSON snapshot, so the
composer-side test can land later and assert against a stable reference.

Add new station markers as sibling tests; each gets its own cassette via
the `@pytest.mark.vcr` default path
(`tests/cassettes/test_composer_oracle/test_<name>.yaml`).
"""

import json
import logging
from pathlib import Path

import pytest

from tests._canonicalize import canonicalize
from tostools import gps_metadata_qc

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
