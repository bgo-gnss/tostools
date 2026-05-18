"""pytest fixtures and VCR configuration for tostools tests.

The VCR config drives `pytest-recording` cassettes used by the composer-oracle
byte-equality harness (`test_composer_oracle.py`). Cassettes live under
`tests/cassettes/<test_module>/<test_func>.yaml` and capture every HTTP
exchange both `gps_metadata_qc.gps_metadata` and `TOSClient`-based code make
against the TOS REST API.
"""

import json
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent


def _match_json_body(r1: Any, r2: Any) -> bool:
    """Body matcher that compares JSON payloads as parsed dicts, not bytes.

    Falls back to byte-exact comparison for non-JSON bodies (empty / form-
    encoded). Prevents cassettes going stale when a future TOSClient refactor
    reorders keys in a POST body.
    """
    b1 = r1.body
    b2 = r2.body
    if not b1 and not b2:
        return True
    if not b1 or not b2:
        return False
    try:
        return json.loads(b1) == json.loads(b2)
    except (ValueError, TypeError):
        return b1 == b2


@pytest.fixture(scope="session")
def vcr_config() -> dict:
    """Global VCR config consumed by `pytest-recording`."""
    return {
        "filter_headers": ["Authorization", "Cookie", "Set-Cookie"],
        "match_on": ["method", "scheme", "host", "path", "query", "json_body"],
        "decode_compressed_response": True,
    }


def _has_pytest_recording() -> bool:
    try:
        import pytest_recording  # noqa: F401

        return True
    except ImportError:
        return False


if _has_pytest_recording():
    # pytest-recording instantiates a fresh VCR for every test and fires the
    # `pytest_recording_configure(config, vcr)` hook just before the cassette
    # context opens. Hooking here is the supported way to register custom
    # matchers without monkey-patching the global default VCR.
    def pytest_recording_configure(config, vcr):  # noqa: D401
        """Register the `json_body` matcher on the per-test VCR instance."""
        vcr.register_matcher("json_body", _match_json_body)
