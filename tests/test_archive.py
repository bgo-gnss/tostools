"""Tests for `tostools.archive` — the cold-archive helper module.

Each filter helper is pinned independently so future audit verbs that
adopt them can trust the contract. The walker is mocked at the
filesystem level — no real archive access required.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from tostools.archive import (
    ArchiveDay,
    classify_file_format,
    coalesce_brand_runs,
    cold_archive_prepath,
    detect_brand_transitions,
    detect_data_gaps,
    detect_rinex_only_spans,
    walk_station_timeline,
)

# ---------------------------------------------------------------------------
# classify_file_format
# ---------------------------------------------------------------------------


def test_classify_septentrio_raw():
    """`.sbf` filename pattern (Septentrio raw) carries explicit YYYY-MM-DD."""
    c = classify_file_format("SAVI201407080000a.sbf")
    assert c.family == "septentrio"
    assert c.is_raw is True
    assert c.date == date(2014, 7, 8)
    assert c.extension == "sbf"


def test_classify_trimble_netr9_raw():
    """`.T02` → trimble_netr9 family."""
    c = classify_file_format("SAVI201607020000a.T02")
    assert c.family == "trimble_netr9"
    assert c.is_raw is True
    assert c.date == date(2016, 7, 2)
    assert c.extension == "T02"


def test_classify_trimble_other_raw():
    """`.T01` and `.T00` map to NetRS / generic Trimble — distinct from NetR9."""
    assert classify_file_format("ARHO200501010000a.T01").family == "trimble_netrs"
    assert classify_file_format("ARHO200501010000a.T00").family == "trimble_other"
    assert classify_file_format("ARHO199901010000a.dat").family == "trimble_4000"


def test_classify_hatanaka_rinex_brand_neutral():
    """Hatanaka `<sta><doy>0.<yy>D[.Z|.gz]` is brand-neutral (post-conversion)."""
    c = classify_file_format("SAVI1840.16D.Z")
    assert c.family == "rinex"
    assert c.is_raw is False
    # YY=16 → year 2016; DOY=184 → 2016-07-02
    assert c.date == date(2016, 7, 2)


def test_classify_rinex_observation_extension():
    """Lowercase `o` for plain observation also matches the RINEX pattern."""
    c = classify_file_format("SAVI1840.16o")
    assert c.family == "rinex"
    assert c.date == date(2016, 7, 2)


def test_classify_unknown_filename_returns_unknown_family_and_no_date():
    """Filename that matches neither pattern → family='unknown', date=None."""
    c = classify_file_format("random_file.bin")
    assert c.family == "unknown"
    assert c.date is None
    assert c.is_raw is False


def test_classify_century_pivot_for_two_digit_year():
    """YY<80 → 2000s, YY>=80 → 1900s. Pivot 80 chosen because civilian
    GPS observations don't exist before 1980 — anything YY>=80 must be
    a 19xx file (e.g. 1990s data). YY<80 maps into 2000-2079."""
    # 2000s side
    assert classify_file_format("ARHO1230.00D.Z").date == date(2000, 5, 2)
    assert classify_file_format("ARHO1230.16D.Z").date == date(2016, 5, 2)
    # 1900s side (pivot fires at YY>=80)
    assert classify_file_format("ARHO1230.85D.Z").date == date(1985, 5, 3)
    assert classify_file_format("ARHO1230.99D.Z").date == date(1999, 5, 3)


# ---------------------------------------------------------------------------
# walk_station_timeline (mocked filesystem)
# ---------------------------------------------------------------------------


def _make_fake_archive(tmp_path: Path, marker: str, files_per_month: dict) -> Path:
    """Build a fake archive under tmp_path matching the real layout.

    files_per_month: {(year, month_short, leaf): [filename, ...]}
    """
    for (year, mon, leaf), files in files_per_month.items():
        d = tmp_path / str(year) / mon / marker / "15s_24hr" / leaf
        d.mkdir(parents=True, exist_ok=True)
        for fname in files:
            (d / fname).write_text("dummy")
    return tmp_path


def test_walk_station_timeline_chronological_order(tmp_path):
    root = _make_fake_archive(
        tmp_path,
        "SAVI",
        {
            (2007, "sep", "raw"): ["SAVI200709010000a.sbf"],
            (2007, "aug", "raw"): ["SAVI200708150000a.sbf"],
            (2008, "jan", "raw"): ["SAVI200801010000a.sbf"],
        },
    )
    timeline = list(walk_station_timeline("SAVI", root))
    assert [d.obs_date for d in timeline] == [
        date(2007, 8, 15),
        date(2007, 9, 1),
        date(2008, 1, 1),
    ]


def test_walk_station_timeline_prefers_raw_over_rinex_per_day(tmp_path):
    """When both raw and rinex exist for the same day, raw wins so we keep
    the brand signal (rinex is brand-neutral)."""
    root = _make_fake_archive(
        tmp_path,
        "SAVI",
        {
            (2014, "jul", "raw"): ["SAVI201407080000a.sbf"],
            (2014, "jul", "rinex"): ["SAVI1890.14D.Z"],
        },
    )
    timeline = list(walk_station_timeline("SAVI", root))
    assert len(timeline) == 1
    assert timeline[0].family == "septentrio"
    assert timeline[0].is_raw is True


def test_walk_station_timeline_falls_back_to_rinex_when_raw_missing(tmp_path):
    """If raw/ is empty for a day but rinex/ has it, surface the rinex
    entry — date is known even if brand isn't."""
    root = _make_fake_archive(
        tmp_path,
        "SAVI",
        {(2014, "jul", "rinex"): ["SAVI1890.14D.Z"]},
    )
    timeline = list(walk_station_timeline("SAVI", root))
    assert len(timeline) == 1
    assert timeline[0].family == "rinex"


def test_walk_station_timeline_empty_root_yields_nothing(tmp_path):
    root = _make_fake_archive(tmp_path, "SAVI", {})
    assert list(walk_station_timeline("SAVI", root)) == []


def test_walk_station_timeline_handles_missing_root_directory(tmp_path):
    """A root that doesn't exist returns an empty iterator (not an exception)."""
    nonexistent = tmp_path / "does_not_exist"
    assert list(walk_station_timeline("SAVI", nonexistent)) == []


def test_walk_station_timeline_marker_uppercased(tmp_path):
    """Lowercased marker arg should still find the uppercase directory."""
    root = _make_fake_archive(
        tmp_path,
        "SAVI",
        {(2020, "jan", "raw"): ["SAVI202001010000a.T02"]},
    )
    timeline = list(walk_station_timeline("savi", root))
    assert len(timeline) == 1


# ---------------------------------------------------------------------------
# detect_brand_transitions
# ---------------------------------------------------------------------------


def _day(d: str, family: str) -> ArchiveDay:
    y, m, dd = d.split("-")
    return ArchiveDay(
        obs_date=date(int(y), int(m), int(dd)),
        family=family,
        file_path=Path(f"/fake/{family}.bin"),
    )


def test_detect_brand_transitions_returns_real_brand_changes():
    """Septentrio → Trimble NetR9 is a real transition; surface it."""
    timeline = [
        _day("2014-07-08", "septentrio"),
        _day("2016-07-02", "trimble_netr9"),
    ]
    transitions = detect_brand_transitions(timeline)
    assert len(transitions) == 1
    t = transitions[0]
    assert t.date_before == date(2014, 7, 8)
    assert t.date_after == date(2016, 7, 2)
    assert t.family_before == "septentrio"
    assert t.family_after == "trimble_netr9"


def test_detect_brand_transitions_ignores_rinex_neighbours():
    """A rinex-family entry between two raw entries should NOT register as a
    'transition' — rinex is brand-neutral (just a format conversion)."""
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2007-12-15", "rinex"),  # brand-neutral, must not split the run
        _day("2008-01-05", "septentrio"),
    ]
    assert detect_brand_transitions(timeline) == []


def test_detect_brand_transitions_no_change_returns_empty():
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2007-10-01", "septentrio"),
        _day("2007-11-01", "septentrio"),
    ]
    assert detect_brand_transitions(timeline) == []


def test_detect_brand_transitions_handles_short_timelines():
    assert detect_brand_transitions([]) == []
    assert detect_brand_transitions([_day("2007-09-01", "septentrio")]) == []


# ---------------------------------------------------------------------------
# detect_data_gaps
# ---------------------------------------------------------------------------


def test_detect_data_gaps_surfaces_gaps_above_threshold():
    """The 2014-07-08 → 2016-07-02 SAVI gap (724 days) must be reported with
    correct delta math."""
    timeline = [
        _day("2014-07-08", "septentrio"),
        _day("2016-07-02", "trimble_netr9"),
    ]
    gaps = detect_data_gaps(timeline, min_days=30)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.last_day_with_data == date(2014, 7, 8)
    assert g.next_day_with_data == date(2016, 7, 2)
    # Gap duration: next - last - 1 (days where no data was collected)
    assert g.duration_days == 724


def test_detect_data_gaps_ignores_gaps_below_threshold():
    """Default min_days=30 — a 25-day gap should NOT be reported."""
    timeline = [
        _day("2020-01-01", "septentrio"),
        _day("2020-01-26", "septentrio"),  # 24-day gap
    ]
    assert detect_data_gaps(timeline) == []


def test_detect_data_gaps_threshold_is_strict():
    """min_days is strict-greater-than (not >=). 30-day gap stays silent;
    31-day gap surfaces."""
    timeline = [
        _day("2020-01-01", "septentrio"),
        _day("2020-01-31", "septentrio"),  # 29-day gap (delta=30 days incl.)
    ]
    assert detect_data_gaps(timeline, min_days=30) == []


# ---------------------------------------------------------------------------
# cold_archive_prepath
# ---------------------------------------------------------------------------


def test_cold_archive_prepath_override_wins():
    """Explicit override beats env/cfg/probe."""
    p = cold_archive_prepath(override="/tmp/explicit_path")
    assert p == Path("/tmp/explicit_path")


def test_cold_archive_prepath_env_var_used(monkeypatch):
    """TOSTOOLS_ARCHIVE_ROOT env var is the next-highest priority."""
    monkeypatch.setenv("TOSTOOLS_ARCHIVE_ROOT", "/tmp/from_env")
    with patch("tostools.archive._find_receivers_cfg", return_value=None):
        with patch("tostools.archive.Path.is_dir", return_value=False):  # disable probe
            assert cold_archive_prepath() == Path("/tmp/from_env")


def test_cold_archive_prepath_reads_from_receivers_cfg(monkeypatch, tmp_path):
    """When no override or env, read [archive_paths] cold_archive_prepath
    from the shared receivers.cfg."""
    monkeypatch.delenv("TOSTOOLS_ARCHIVE_ROOT", raising=False)
    cfg_file = tmp_path / "receivers.cfg"
    cfg_file.write_text(
        "[archive_paths]\ncold_archive_prepath = /custom/archive/path\n"
    )
    with patch("tostools.archive._find_receivers_cfg", return_value=cfg_file):
        assert cold_archive_prepath() == Path("/custom/archive/path")


def test_cold_archive_prepath_falls_back_to_probe_when_cfg_missing(
    monkeypatch, tmp_path
):
    """No override, no env, no cfg → probe known mount points."""
    monkeypatch.delenv("TOSTOOLS_ARCHIVE_ROOT", raising=False)
    fake_probe = tmp_path / "fake_mount"
    fake_probe.mkdir()
    with patch("tostools.archive._find_receivers_cfg", return_value=None):
        with patch("tostools.archive._PROBE_PATHS", (str(fake_probe),)):
            assert cold_archive_prepath() == Path(str(fake_probe))


def test_cold_archive_prepath_raises_when_all_resolution_steps_fail(
    monkeypatch,
):
    """When override/env/cfg/probe all miss, raise with a helpful message
    listing every candidate that was checked."""
    monkeypatch.delenv("TOSTOOLS_ARCHIVE_ROOT", raising=False)
    with patch("tostools.archive._find_receivers_cfg", return_value=None):
        with patch("tostools.archive._PROBE_PATHS", ("/does/not/exist",)):
            with pytest.raises(FileNotFoundError) as exc_info:
                cold_archive_prepath()
    msg = str(exc_info.value)
    assert "cold_archive_prepath unresolved" in msg
    assert "receivers.cfg" in msg


def test_cold_archive_prepath_malformed_cfg_falls_through_to_probe(
    monkeypatch, tmp_path
):
    """Don't surface ConfigParser errors as fatal — fall through to probing."""
    monkeypatch.delenv("TOSTOOLS_ARCHIVE_ROOT", raising=False)
    cfg_file = tmp_path / "receivers.cfg"
    cfg_file.write_text("[archive_paths\nbroken = format")  # unclosed section
    fake_probe = tmp_path / "fallback_mount"
    fake_probe.mkdir()
    with patch("tostools.archive._find_receivers_cfg", return_value=cfg_file):
        with patch("tostools.archive._PROBE_PATHS", (str(fake_probe),)):
            assert cold_archive_prepath() == Path(str(fake_probe))


# ---------------------------------------------------------------------------
# Integration — full SAVI-shape archive walk + analysis
# ---------------------------------------------------------------------------


def test_savi_shape_integration(tmp_path):
    """End-to-end: fake archive shaped like SAVI's actual state
    (POLARX2 era → 2-year gap → NETR9 era) produces the expected
    transitions + gaps. This is the verb's main use case in one test."""
    files = {
        # POLARX2 era (Septentrio .sbf)
        (2010, "jun", "raw"): ["SAVI201006010000a.sbf"],
        (2014, "jul", "raw"): ["SAVI201407080000a.sbf"],
        # 2-year gap — no SAVI dirs at all
        # NETR9 era (Trimble .T02)
        (2016, "jul", "raw"): ["SAVI201607020000a.T02"],
        (2020, "jan", "raw"): ["SAVI202001010000a.T02"],
    }
    root = _make_fake_archive(tmp_path, "SAVI", files)

    timeline = list(walk_station_timeline("SAVI", root))
    assert [d.obs_date for d in timeline] == [
        date(2010, 6, 1),
        date(2014, 7, 8),
        date(2016, 7, 2),
        date(2020, 1, 1),
    ]
    assert [d.family for d in timeline] == [
        "septentrio",
        "septentrio",
        "trimble_netr9",
        "trimble_netr9",
    ]

    transitions = detect_brand_transitions(timeline)
    assert len(transitions) == 1
    assert transitions[0].family_before == "septentrio"
    assert transitions[0].family_after == "trimble_netr9"
    assert transitions[0].date_before == date(2014, 7, 8)
    assert transitions[0].date_after == date(2016, 7, 2)

    gaps = detect_data_gaps(timeline, min_days=30)
    # Three gaps surface from the sparse fixture (every consecutive pair
    # has >30 days between them). The cross-brand gap is the SAVI-style
    # marker we care about most.
    assert len(gaps) == 3
    big_gap = next(g for g in gaps if g.last_day_with_data == date(2014, 7, 8))
    assert big_gap.next_day_with_data == date(2016, 7, 2)
    assert big_gap.duration_days == 724


# ---------------------------------------------------------------------------
# coalesce_brand_runs — rinex absorbed into surrounding brand
# ---------------------------------------------------------------------------


def test_coalesce_rinex_between_same_brand_is_absorbed():
    """Septentrio → rinex → septentrio collapses to one septentrio run
    with the rinex day count preserved on the run."""
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2007-12-15", "rinex"),
        _day("2008-01-05", "septentrio"),
    ]
    runs = coalesce_brand_runs(timeline)
    assert len(runs) == 1
    r = runs[0]
    assert r.family == "septentrio"
    assert r.start == date(2007, 9, 1)
    assert r.end == date(2008, 1, 5)
    assert r.rinex_only_days == 1
    assert r.ambiguous is False


def test_coalesce_rinex_between_different_brands_marked_ambiguous():
    """Septentrio → rinex → trimble: rinex can't be attributed to either
    side, surfaces as its own ambiguous span."""
    timeline = [
        _day("2014-07-08", "septentrio"),
        _day("2015-01-01", "rinex"),
        _day("2016-07-02", "trimble_netr9"),
    ]
    runs = coalesce_brand_runs(timeline)
    assert len(runs) == 3
    assert runs[0].family == "septentrio"
    assert runs[0].ambiguous is False
    assert runs[1].family == "rinex"
    assert runs[1].ambiguous is True
    assert runs[2].family == "trimble_netr9"
    assert runs[2].ambiguous is False


def test_coalesce_leading_rinex_surfaces_as_ambiguous():
    """Rinex at the very start (no brand before it) can't be attributed
    — surface as ambiguous so the operator decides."""
    timeline = [
        _day("2007-01-01", "rinex"),
        _day("2007-09-01", "septentrio"),
    ]
    runs = coalesce_brand_runs(timeline)
    assert len(runs) == 2
    assert runs[0].family == "rinex"
    assert runs[0].ambiguous is True
    assert runs[1].family == "septentrio"


def test_coalesce_trailing_rinex_surfaces_as_ambiguous():
    """Same for rinex at the very end of the timeline."""
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2020-01-01", "rinex"),
    ]
    runs = coalesce_brand_runs(timeline)
    assert len(runs) == 2
    assert runs[0].family == "septentrio"
    assert runs[1].family == "rinex"
    assert runs[1].ambiguous is True


def test_coalesce_savi_pattern_two_clean_brand_runs():
    """The SAVI live-archive shape: 3 septentrio segments interleaved
    with 2 rinex-only gaps, then a brand change to trimble. Coalesces
    to 2 brand runs (septentrio with 2 rinex stretches absorbed; then
    trimble)."""
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2007-12-15", "rinex"),
        _day("2008-01-05", "septentrio"),
        _day("2009-06-01", "rinex"),
        _day("2010-08-25", "septentrio"),
        _day("2014-07-08", "septentrio"),
        _day("2016-07-02", "trimble_netr9"),
    ]
    runs = coalesce_brand_runs(timeline)
    assert [r.family for r in runs] == ["septentrio", "trimble_netr9"]
    assert runs[0].rinex_only_days == 2
    assert runs[0].start == date(2007, 9, 1)
    assert runs[0].end == date(2014, 7, 8)
    assert runs[1].start == date(2016, 7, 2)
    assert runs[1].rinex_only_days == 0


def test_coalesce_empty_timeline_returns_empty():
    assert coalesce_brand_runs([]) == []


# ---------------------------------------------------------------------------
# detect_rinex_only_spans
# ---------------------------------------------------------------------------


def test_detect_rinex_only_spans_groups_adjacent_rinex_days():
    """Two adjacent rinex days collapse into one span."""
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2007-12-15", "rinex"),
        _day("2007-12-16", "rinex"),
        _day("2007-12-17", "rinex"),
        _day("2008-01-05", "septentrio"),
    ]
    spans = detect_rinex_only_spans(timeline)
    assert len(spans) == 1
    assert spans[0].start == date(2007, 12, 15)
    assert spans[0].end == date(2007, 12, 17)
    assert spans[0].days == 3


def test_detect_rinex_only_spans_handles_multiple_spans():
    """Separated rinex stretches surface as separate spans."""
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2007-12-15", "rinex"),
        _day("2008-01-05", "septentrio"),
        _day("2009-06-01", "rinex"),
        _day("2009-06-02", "rinex"),
        _day("2010-08-25", "septentrio"),
    ]
    spans = detect_rinex_only_spans(timeline)
    assert len(spans) == 2
    assert spans[0].days == 1
    assert spans[1].days == 2


def test_detect_rinex_only_spans_no_rinex_returns_empty():
    timeline = [
        _day("2007-09-01", "septentrio"),
        _day("2014-07-08", "septentrio"),
    ]
    assert detect_rinex_only_spans(timeline) == []


def test_detect_rinex_only_spans_only_rinex_returns_one_span():
    """A timeline of only rinex days → one span covering the whole range."""
    timeline = [
        _day("2007-01-01", "rinex"),
        _day("2007-01-02", "rinex"),
        _day("2007-01-03", "rinex"),
    ]
    spans = detect_rinex_only_spans(timeline)
    assert len(spans) == 1
    assert spans[0].days == 3


# ---------------------------------------------------------------------------
# _classify_tos_join_against_archive + _infer_expected_family
# (Live in tos.py, but tested here alongside the archive helpers they consume)
# ---------------------------------------------------------------------------


def test_infer_expected_family_recognised_models():
    from tostools.tos import _infer_expected_family

    assert _infer_expected_family("TRIMBLE NETR9") == "trimble_netr9"
    assert _infer_expected_family("TRIMBLE NETRS") == "trimble_netrs"
    assert _infer_expected_family("SEPT POLARX2") == "septentrio"
    assert _infer_expected_family("SEPT POLARX5") == "septentrio"
    # Case insensitive
    assert _infer_expected_family("trimble netr9") == "trimble_netr9"


def test_infer_expected_family_unrecognised_returns_none():
    """Unmapped models → None, so verdict treats them as 'unmapped_model'
    (informational), not 'wrong_brand' (actionable). Important: ASHTECH
    UZ-12 has no .sbf-style mapping today; don't falsely flag it."""
    from tostools.tos import _infer_expected_family

    assert _infer_expected_family("ASHTECH UZ-12") is None
    assert _infer_expected_family("LEICA GR10") is None
    assert _infer_expected_family(None) is None
    assert _infer_expected_family("") is None


def test_classify_no_archive_coverage():
    """TOS window with no archived days → no_archive_coverage."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [_day("2010-01-01", "septentrio")]
    verdict = _classify_tos_join_against_archive(
        time_from="2020-01-01",
        time_to="2020-12-31",
        expected_family="trimble_netr9",
        timeline=timeline,
    )
    assert verdict["status"] == "no_archive_coverage"


def test_classify_unmapped_model_surfaces_informational():
    """When the TOS model has no family mapping (ASHTECH), verdict is
    informational — neither green nor red. The operator can decide."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [_day("2007-09-01", "septentrio")]
    verdict = _classify_tos_join_against_archive(
        time_from="2007-08-08",
        time_to="2007-12-31",
        expected_family=None,
        timeline=timeline,
    )
    assert verdict["status"] == "unmapped_model"
    assert "septentrio" in verdict["detail"]


def test_classify_rinex_only_when_only_format_neutral_days():
    """Window contains only RINEX (format-neutral) days — brand can't
    be confirmed from filenames alone. Don't flag as wrong/right."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [_day("2010-01-01", "rinex"), _day("2010-06-01", "rinex")]
    verdict = _classify_tos_join_against_archive(
        time_from="2010-01-01",
        time_to="2010-12-31",
        expected_family="trimble_netr9",
        timeline=timeline,
    )
    assert verdict["status"] == "rinex_only"


def test_classify_ok_when_expected_family_throughout():
    """Window contains only the expected family → ok."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [
        _day("2020-01-01", "trimble_netr9"),
        _day("2020-06-01", "trimble_netr9"),
        _day("2020-12-31", "trimble_netr9"),
    ]
    verdict = _classify_tos_join_against_archive(
        time_from="2020-01-01",
        time_to="2021-01-01",
        expected_family="trimble_netr9",
        timeline=timeline,
    )
    assert verdict["status"] == "ok"


def test_classify_late_start_suggests_narrowing_time_from():
    """SAVI 4830 case: TOS says NETR9 2007-09-07 → 2026-05-22, but
    archive shows septentrio before 2016-07-02. Suggest patching
    time_from to 2016-07-02."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [
        _day("2008-01-01", "septentrio"),
        _day("2012-06-01", "septentrio"),
        _day("2014-07-08", "septentrio"),
        _day("2016-07-02", "trimble_netr9"),
        _day("2020-01-01", "trimble_netr9"),
        _day("2025-12-31", "trimble_netr9"),
    ]
    verdict = _classify_tos_join_against_archive(
        time_from="2007-09-07",
        time_to="2026-05-22",
        expected_family="trimble_netr9",
        timeline=timeline,
    )
    assert verdict["status"] == "late_start"
    assert verdict["suggested_action_args"] == ("time_from", "2016-07-02")
    assert "trimble_netr9" in verdict["detail"]
    assert "septentrio" in verdict["detail"]


def test_classify_early_end_suggests_narrowing_time_to():
    """Mirror case: TOS window extends past when the expected brand
    actually ended. Suggest patching time_to backward."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [
        _day("2008-01-01", "septentrio"),
        _day("2014-07-08", "septentrio"),
        _day("2016-07-02", "trimble_netr9"),
        _day("2020-01-01", "trimble_netr9"),
    ]
    verdict = _classify_tos_join_against_archive(
        time_from="2007-01-01",
        time_to="2021-01-01",
        expected_family="septentrio",
        timeline=timeline,
    )
    assert verdict["status"] == "early_end"
    assert verdict["suggested_action_args"] == ("time_to", "2014-07-08")


def test_classify_wrong_brand_when_only_other_family_present():
    """Window has raw days but none match the expected family."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [
        _day("2020-01-01", "trimble_netr9"),
        _day("2020-06-01", "trimble_netr9"),
    ]
    verdict = _classify_tos_join_against_archive(
        time_from="2020-01-01",
        time_to="2021-01-01",
        expected_family="septentrio",
        timeline=timeline,
    )
    assert verdict["status"] == "wrong_brand"
    assert "trimble_netr9" in verdict["detail"]


def test_classify_join_too_wide_interleaved():
    """Interleaved expected + other (rare; typically detection-then-coalesce
    catches it as late_start/early_end). Surface as join_too_wide with
    suggestion to narrow to first expected day."""
    from tostools.tos import _classify_tos_join_against_archive

    timeline = [
        _day("2020-01-01", "trimble_netr9"),
        _day("2020-06-01", "septentrio"),
        _day("2020-12-01", "trimble_netr9"),
    ]
    verdict = _classify_tos_join_against_archive(
        time_from="2020-01-01",
        time_to="2021-01-01",
        expected_family="trimble_netr9",
        timeline=timeline,
    )
    assert verdict["status"] == "join_too_wide"
    assert verdict["suggested_action_args"] == ("time_from", "2020-01-01")
