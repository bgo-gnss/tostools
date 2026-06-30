"""device_attribute_history must not crash on the same-date_from / mixed-date_to
pattern (a firmware update closes the old period while serial stays open).

Regression for the DYNC bug: ``sorted(sub_sessions)`` compared (date_from, date_to)
tuples and, when two shared a date_from, fell through to comparing date_to where one
was None -> ``str < None`` -> TypeError. The TOS client swallowed it and fell back
to a flattened single-period dict, silently degrading the per-period device history.
Both the active (legacy) path and the non-legacy module are covered.
"""

from __future__ import annotations

import logging

import pytest

from tostools.gps_metadata_qc import device_attribute_history as dah_current
from tostools.legacy.gps_metadata_qc import device_attribute_history as dah_legacy


def _attr(code, value, date_from, date_to):
    return {"code": code, "value": value, "date_from": date_from, "date_to": date_to}


def _device():
    # Real DYNC gnss_receiver (id 16300) data: firmware 5.2.0 -> 5.3.0 -> 5.6.0 and
    # software 5.2 -> 5.3 over distinct periods while serial/model/status stay open,
    # and the session_start (receiver install 2019-06-01) differs from the attribute
    # date_from (2019-05-14). After the function's internal period-modelling this
    # leaves (date_from, date_to) tuples that share a date_from with one open and one
    # closed date_to -> sorted() compares str < None -> TypeError (the DYNC crash).
    return {
        "id_entity": 16300,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            _attr("firmware_version", "5.6.0", "2026-05-18T00:00:00", None),
            _attr("serial_number", "3047795", "2019-05-14T00:00:00", None),
            _attr("http_port", "8060", "2019-05-14T00:00:00", None),
            _attr("owner", "Jarðeðlismælihópur", "2019-05-14T00:00:00", None),
            _attr("software_version", "5.3", "2019-10-17T00:00:00", None),
            _attr("date_start", "2019-05-14T00:00:00", "2019-05-14T00:00:00", None),
            _attr(
                "software_version",
                "5.2",
                "2019-05-14T00:00:00",
                "2019-10-17T00:00:00",
            ),
            _attr("model", "SEPT POLARX5", "2019-05-14T00:00:00", None),
            _attr(
                "firmware_version",
                "5.2.0",
                "2019-05-14T00:00:00",
                "2019-10-17T00:00:00",
            ),
            _attr(
                "firmware_version",
                "5.3.0",
                "2019-10-17T00:00:00",
                "2026-05-18T00:00:00",
            ),
            _attr("status", "virkt", "2019-05-14T00:00:00", None),
        ],
    }


@pytest.mark.parametrize("dah", [dah_legacy, dah_current], ids=["legacy", "current"])
def test_no_crash_and_periods_built(dah):
    # session_start = receiver install, distinct from the attribute date_from.
    conns = dah(_device(), "2019-06-01T11:16:00", None, logging.CRITICAL)
    assert conns, "expected at least one connection period"
    # The open period must carry the latest firmware (5.6.0), not be lost to a
    # flattened fallback.
    firmwares = {c.get("firmware_version") for c in conns}
    assert "5.6.0" in firmwares
