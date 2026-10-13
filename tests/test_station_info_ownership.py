"""Tests for spliced station.info ownership (TOS vs host file).

The behaviours pinned here are the ones that decide whether a splice is SAFE:

* nothing outside TOS's managed region is ever removed — pre-TOS history and
  TOS-gap residue are preserved, because deleting them would destroy real GAMIT
  history that TOS cannot re-render;
* a site absent from TOS is untouched entirely (the campaign-site case);
* an explicit reviewed boundary outranks the TOS-derived seed;
* splicing is deterministic, so the drift guard is an equality check;
* as residue is imported into TOS the splice converges to plain regeneration.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tostools.standards.gamit_station_info import Occupation
from tostools.standards.station_info_ownership import (
    FAR_FUTURE,
    BoundaryEntry,
    BoundaryTable,
    RowTier,
    build_report,
    classify_site,
    compute_managed_from,
    format_rows_csv,
    format_summary,
    splice_site,
)


def occ(
    marker: str,
    start: str,
    stop: str | None,
    *,
    receiver_type: str = "TRIMBLE NETRS",
    receiver_sn: str = "1111111",
    antenna_type: str = "TRM41249.00",
    dome: str = "NONE",
    antenna_height: str = "1.0000",
) -> Occupation:
    """Build an Occupation directly — deliberately NOT via the text parser.

    Formatting coupling here would let a column bug in a fixture masquerade as a
    passing test; the parser has its own tests.
    """
    return Occupation(
        marker=marker,
        station_name="Test Site",
        time_from=datetime.fromisoformat(start),
        time_to=datetime.fromisoformat(stop) if stop else None,
        receiver_type=receiver_type,
        receiver_sn=receiver_sn,
        vers="1.0",
        swver="1.00",
        antenna_type=antenna_type,
        dome=dome,
        antenna_sn="0000",
        antenna_height=antenna_height,
        htcod="DHARP",
    )


# --------------------------------------------------------------------------
# the three tiers
# --------------------------------------------------------------------------


def test_pre_tos_history_is_preserved():
    """History before TOS's earliest session must survive untouched."""
    host = [occ("TEST", "2005-01-01", "2010-01-01"), occ("TEST", "2015-01-01", None)]
    tos = [occ("TEST", "2015-01-01", None)]

    site = classify_site("TEST", host, tos)

    assert len(site.pre_tos_rows) == 1
    assert site.pre_tos_rows[0].occupation.time_from == datetime(2005, 1, 1)
    assert len(site.managed_rows) == 1
    assert site.residue_count == 0

    spliced, adjustments = splice_site(site)
    assert len(spliced) == 2  # preserved pre-TOS row + the TOS row
    assert min(o.time_from for o in spliced) == datetime(2005, 1, 1)


def test_managed_rows_are_superseded_by_tos():
    """Where TOS has a session, TOS's values win — the whole point."""
    host = [occ("TEST", "2015-01-01", None, receiver_sn="OLD")]
    tos = [occ("TEST", "2015-01-01", None, receiver_sn="NEW")]

    site = classify_site("TEST", host, tos)
    assert len(site.managed_rows) == 1

    spliced, _ = splice_site(site)
    assert [o.receiver_sn for o in spliced] == ["NEW"]


def test_tos_gap_is_residue_and_preserved_not_deleted():
    """A hole inside TOS's span must NOT delete the host's row.

    This is the 168-gap / 36,943-station-day failure mode: regenerating from TOS
    would silently drop occupations TOS cannot account for.
    """
    host = [occ("TEST", "2015-01-01", "2019-01-01"), occ("TEST", "2021-01-01", None)]
    tos = [occ("TEST", "2015-01-01", "2019-01-01"), occ("TEST", "2021-01-01", None)]
    # widen the host row so it covers the 2019-2021 hole TOS has no session for
    host = [occ("TEST", "2015-01-01", "2020-06-01"), occ("TEST", "2021-01-01", None)]

    site = classify_site("TEST", host, tos)

    # host row straddles the gap -> split, not deleted
    straddled = site.straddled_rows
    assert len(straddled) == 1
    frags = straddled[0].preserved_fragments
    assert frags, "the part outside the managed region must survive"
    assert frags[0][0] == datetime(2019, 1, 1), "fragment starts where TOS's session ended"

    spliced, adjustments = splice_site(site)
    assert any("SPLIT" in a for a in adjustments), "a split must be reported, never silent"
    assert len(spliced) == 3  # 2 TOS rows + 1 preserved fragment


def test_site_absent_from_tos_is_untouched():
    """Campaign sites not yet in TOS: empty managed region -> nothing removed."""
    host = [occ("GSK1", "2007-05-01", "2007-06-01"), occ("GSK1", "2008-05-01", "2008-06-01")]

    site = classify_site("GSK1", host, tos_rows=[])

    assert not site.in_tos
    assert site.managed_from is None
    assert site.residue_count == 2  # everything is backlog for this site
    assert len(site.managed_rows) == 0

    spliced, adjustments = splice_site(site)
    assert len(spliced) == 2, "no TOS rows to add, and nothing may be dropped"
    assert adjustments == []
    assert {o.time_from for o in spliced} == {
        datetime(2007, 5, 1),
        datetime(2008, 5, 1),
    }


# --------------------------------------------------------------------------
# boundaries
# --------------------------------------------------------------------------


def test_boundary_seed_is_tos_earliest_and_marked_proposed():
    host = [occ("TEST", "2010-01-01", None)]
    tos = [occ("TEST", "2015-01-01", None), occ("TEST", "2018-01-01", None)]

    site = classify_site("TEST", host, tos)

    assert site.tos_earliest == datetime(2015, 1, 1)
    assert site.managed_from == datetime(2015, 1, 1)
    assert site.boundary_is_proposed is True


def test_reviewed_boundary_overrides_the_seed():
    """A reviewed decision outranks TOS's own earliest session (KAST/HRSC class)."""
    host = [occ("KAST", "2010-01-01", None)]
    tos = [occ("KAST", "2023-10-01", None)]

    # TOS says 10-01; review decided the real start is later
    reviewed = BoundaryEntry("KAST", datetime(2023, 10, 24), source="reviewed:2026-10-13")
    site = classify_site("KAST", host, tos, boundary=reviewed)

    assert site.managed_from == datetime(2023, 10, 24)
    assert site.boundary_is_proposed is False


def test_explicit_null_boundary_means_fully_unmanaged():
    """A blank boundary is a deliberate 'do not touch yet' decision."""
    host = [occ("TEST", "2010-01-01", None)]
    tos = [occ("TEST", "2015-01-01", None)]

    entry = BoundaryEntry("TEST", None, source="reviewed:2026-10-13", note="disputed")
    site = classify_site("TEST", host, tos, boundary=entry)

    assert site.managed_from is None
    spliced, adjustments = splice_site(site)
    assert len(spliced) == 1 and spliced[0].time_from == datetime(2010, 1, 1)
    # the TOS row must NOT be injected: with no managed region the host owns it all
    assert all(o.receiver_sn == "1111111" for o in spliced)
    assert any("NOT applied" in a for a in adjustments)


def test_boundary_floor_clips_an_early_tos_session():
    """A TOS session before the boundary must be CLIPPED to it, not re-injected.

    Regression: an earlier version emitted TOS rows verbatim, re-introducing the
    very rows the boundary was set to protect.
    """
    host = [occ("TEST", "2000-01-01", None, receiver_sn="HOSTSN")]
    tos = [occ("TEST", "2005-01-01", None, receiver_sn="TOSSN")]

    entry = BoundaryEntry("TEST", datetime(2010, 1, 1), source="reviewed")
    site = classify_site("TEST", host, tos, boundary=entry)

    # managed region is floored at 2010, so 2000-2010 survives
    assert len(site.managed_rows) == 0
    assert site.straddled_rows, "the host row is split at the 2010 boundary"

    spliced, adjustments = splice_site(site)
    assert len(spliced) == 2

    preserved, incoming = spliced[0], spliced[1]
    assert preserved.receiver_sn == "HOSTSN"
    assert preserved.time_from == datetime(2000, 1, 1)
    assert preserved.time_to == datetime(2010, 1, 1), "trimmed exactly at the boundary"

    assert incoming.receiver_sn == "TOSSN"
    assert incoming.time_from == datetime(2010, 1, 1), (
        "the TOS row must start AT the boundary, not at its own earlier 2005 start"
    )
    assert any("clipped" in a for a in adjustments), "a clip must be reported"


# --------------------------------------------------------------------------
# determinism / convergence
# --------------------------------------------------------------------------


def test_splice_is_deterministic():
    """The drift guard is equality, so splicing must be reproducible."""
    host = [occ("TEST", "2005-01-01", "2020-06-01"), occ("TEST", "2021-01-01", None)]
    tos = [occ("TEST", "2015-01-01", "2019-01-01"), occ("TEST", "2021-01-01", None)]

    a, adj_a = splice_site(classify_site("TEST", host, tos))
    b, adj_b = splice_site(classify_site("TEST", host, tos))

    assert [o.time_from for o in a] == [o.time_from for o in b]
    assert adj_a == adj_b


def test_converges_to_plain_regeneration_once_residue_is_imported():
    """As TOS's union covers the host, the splice becomes regeneration."""
    host = [occ("TEST", "2005-01-01", "2020-06-01", receiver_sn="OLD")]
    # after importing the residue, TOS covers the whole host span
    tos = [
        occ("TEST", "2005-01-01", "2015-01-01", receiver_sn="NEW"),
        occ("TEST", "2015-01-01", None, receiver_sn="NEW"),
    ]

    site = classify_site("TEST", host, tos)
    assert site.residue_count == 0
    assert len(site.managed_rows) == 1

    spliced, _ = splice_site(site)
    assert spliced == tos, "with no residue the output IS the TOS render"
    assert all(o.receiver_sn == "NEW" for o in spliced)


def test_open_ended_session_is_not_truncated_by_far_future():
    host = [occ("TEST", "2015-01-01", None)]
    tos = [occ("TEST", "2015-01-01", None)]

    site = classify_site("TEST", host, tos)
    assert len(site.managed_rows) == 1
    assert site.straddled_rows == []

    spliced, _ = splice_site(site)
    assert spliced[0].time_to is None, "the open sentinel must survive the splice"


# --------------------------------------------------------------------------
# boundary table I/O
# --------------------------------------------------------------------------


def test_boundary_table_round_trip(tmp_path):
    host = {"TEST": [occ("TEST", "2010-01-01", None)], "GSK1": [occ("GSK1", "2007-05-01", None)]}
    tos = {"TEST": [occ("TEST", "2015-01-01", None)]}
    sites = build_report(host, tos)

    path = tmp_path / "managed_from.csv"
    BoundaryTable().save(path, sites)
    table = BoundaryTable.load(path)

    assert table.get("TEST").managed_from == datetime(2015, 1, 1)
    assert table.get("TEST").source == "tos-seed"
    # absent from TOS -> recorded, but with no boundary
    assert table.get("GSK1").managed_from is None
    assert table.get("GSK1").source == "absent-from-tos"


def test_boundary_table_save_never_overwrites_a_reviewed_decision(tmp_path):
    """Re-seeding must not clobber review — the KAST/HRSC hazard."""
    path = tmp_path / "managed_from.csv"
    path.write_text("marker,managed_from,source,note\nKAST,2023-10-24,reviewed,deliberate\n")

    table = BoundaryTable.load(path)
    sites = build_report({"KAST": [occ("KAST", "2010-01-01", None)]},
                         {"KAST": [occ("KAST", "2023-10-01", None)]},
                         boundaries=table)
    table.save(path, sites)

    reloaded = BoundaryTable.load(path)
    assert reloaded.get("KAST").managed_from == datetime(2023, 10, 24)
    assert reloaded.get("KAST").source == "reviewed"


def test_boundary_table_rejects_a_bad_date(tmp_path):
    path = tmp_path / "managed_from.csv"
    path.write_text("marker,managed_from,source,note\nTEST,not-a-date,x,\n")
    with pytest.raises(ValueError, match="unrecognised date"):
        BoundaryTable.load(path)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def test_report_formatting_includes_the_headline_totals():
    host = {
        "TEST": [occ("TEST", "2005-01-01", "2015-01-01"), occ("TEST", "2015-01-01", None)],
        "GSK1": [occ("GSK1", "2007-05-01", None)],
    }
    tos = {"TEST": [occ("TEST", "2015-01-01", None)]}

    out = format_summary(build_report(host, tos))

    assert "RESIDUE (backlog)" in out
    assert "sites absent from TOS    : 1" in out
    assert "GSK1" in out


def test_rows_csv_emits_one_row_per_preserved_fragment():
    host = [occ("TEST", "2005-01-01", "2020-06-01")]
    tos = [occ("TEST", "2015-01-01", "2019-01-01"), occ("TEST", "2021-01-01", None)]

    csv_text = format_rows_csv(build_report({"TEST": host}, {"TEST": tos}))
    lines = [ln for ln in csv_text.strip().splitlines()]

    assert lines[0].startswith("marker,tier,host_from")
    assert len(lines) == 3  # header + 2 preserved fragments of the split row
    assert "straddled" in csv_text
