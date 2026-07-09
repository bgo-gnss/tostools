"""Tests for constellation extraction + the data-vs-TOS cross-check."""

from __future__ import annotations

from unittest.mock import MagicMock

from tostools.constellation import systems_from_header

R3_HEADER = """\
     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE
sbf2rin-16.2.0                          20260601 002717 UTC PGM / RUN BY / DATE
NYLA00ISL                                                   MARKER NAME
G   11 C1C C1L C1W C2L C2W C5Q L1C L1L L2L L2W L5Q       SYS / # / OBS TYPES
R   10 C1C C1P C2C C2P C3Q L1C L1P L2C L2P L3Q           SYS / # / OBS TYPES
E   10 C1C C5Q C6X C7Q C8Q L1C L5Q L6X L7Q L8Q           SYS / # / OBS TYPES
                                                            END OF HEADER
"""

# GPS line with >13 obs types → wraps onto a continuation line whose first
# column is blank; the continuation must NOT be miscounted as a new system.
R3_WITH_CONTINUATION = """\
     3.05           OBSERVATION DATA    M                   RINEX VERSION / TYPE
G   16 C1C L1C D1C S1C C1W C2W L2W D2W S2W C2L L2L D2L    SYS / # / OBS TYPES
       S2L C5Q L5Q S5Q                                     SYS / # / OBS TYPES
C    8 C2I L2I D2I S2I C7I L7I D7I S7I                     SYS / # / OBS TYPES
                                                            END OF HEADER
"""

R2_MIXED_HEADER = """\
     2.11           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE
     5    C1    L1    L2    P2    P1                        # / TYPES OF OBSERV
                                                            END OF HEADER
"""

R2_GPS_ONLY_HEADER = """\
     2.11           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE
     4    C1    L1    L2    P2                              # / TYPES OF OBSERV
                                                            END OF HEADER
"""


def test_r3_header_systems_reliable():
    r = systems_from_header(R3_HEADER)
    assert r.version == 3.04
    assert r.reliable is True
    assert r.systems == frozenset({"GPS", "GLO", "GAL"})


def test_r3_continuation_line_not_double_counted():
    r = systems_from_header(R3_WITH_CONTINUATION)
    assert r.reliable is True
    # G (with a wrapped continuation) and C — the blank-first-col line is skipped.
    assert r.systems == frozenset({"GPS", "BDS"})


def test_r2_mixed_is_unreliable_and_underreports():
    """R2 'M (MIXED)' can't enumerate the set from the header → unreliable,
    empty best-effort (the caller must confirm vs raw / live receiver)."""
    r = systems_from_header(R2_MIXED_HEADER)
    assert r.version == 2.11
    assert r.reliable is False
    assert r.systems == frozenset()


def test_r2_gps_only_best_effort():
    r = systems_from_header(R2_GPS_ONLY_HEADER)
    assert r.reliable is False
    assert r.systems == frozenset({"GPS"})


# ---------------------------------------------------------------------------
# Cross-check logic (mocked client + archive)
# ---------------------------------------------------------------------------


def _mock_station_with_open_receiver(rx_attrs):
    """Station history dict with one open gnss_receiver child (id 200)."""
    return {
        "id_entity": 100,
        "code_entity_subtype": "geophysical",
        "attributes": [{"code": "marker", "value": "tst1", "date_from": "2010-01-01"}],
        "children_connections": [
            {"id_entity_child": 200, "time_from": "2020-01-01", "time_to": None}
        ],
    }, {
        "id_entity": 200,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [
            {"code": "serial_number", "value": "SN9", "date_from": "2020-01-01"},
            *rx_attrs,
        ],
        "children_connections": [],
    }


def _audit_with(monkeypatch, rx_attrs, reading):
    import tostools.audit_constellations as mod

    station, receiver = _mock_station_with_open_receiver(rx_attrs)
    client = MagicMock()
    client.get_entity_history.side_effect = lambda i: (
        station if int(i) == 100 else receiver
    )
    monkeypatch.setattr(mod, "_resolve_station_entity", lambda *a, **k: station)
    monkeypatch.setattr(mod, "cold_archive_prepath", lambda: "/fake")
    monkeypatch.setattr(mod, "find_most_recent_rinex", lambda *a, **k: "/fake/x.rnx")
    monkeypatch.setattr(mod, "read_constellations", lambda p: reading)
    return mod.audit_station_constellations(client, name="TST1")


def test_data_shows_system_tos_silent_proposes_set_true(monkeypatch):
    from tostools.constellation import ConstellationReading

    reading = ConstellationReading(
        version=3.04, systems=frozenset({"GPS", "GLO", "GAL"}), reliable=True
    )
    report = _audit_with(monkeypatch, rx_attrs=[], reading=reading)
    assert {f.code for f in report.set_true} == {"GPS", "GLO", "GAL"}
    assert not report.reviews
    assert report.receiver_install == "2020-01-01"


def test_tos_claims_system_data_lacks_flags_review_only_when_reliable(monkeypatch):
    from tostools.constellation import ConstellationReading

    rx_attrs = [
        {"code": "GPS", "value": "true", "date_from": "2020-01-01"},
        {"code": "BDS", "value": "true", "date_from": "2020-01-01"},  # not in data
    ]
    reading = ConstellationReading(
        version=3.04, systems=frozenset({"GPS"}), reliable=True
    )
    report = _audit_with(monkeypatch, rx_attrs=rx_attrs, reading=reading)
    assert {f.code for f in report.reviews} == {"BDS"}
    assert not report.set_true  # GPS already true and observed


def test_r2_absence_does_not_flag_review(monkeypatch):
    """An R2 (unreliable) reading must NOT raise 'review' for a TOS-claimed
    system it can't see — R2 under-reports."""
    from tostools.constellation import ConstellationReading

    rx_attrs = [{"code": "GLO", "value": "true", "date_from": "2020-01-01"}]
    reading = ConstellationReading(
        version=2.11, systems=frozenset({"GPS"}), reliable=False
    )
    report = _audit_with(monkeypatch, rx_attrs=rx_attrs, reading=reading)
    assert not report.reviews  # GLO absence from R2 proves nothing
    assert {f.code for f in report.set_true} == {"GPS"}  # data shows GPS, TOS silent
