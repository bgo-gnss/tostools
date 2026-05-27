"""Cross-check TOS state against the cold RINEX archive.

This module is the data-collection half of ``tos audit verify-from-rinex``
and ``tos station show --with-archive``. The CLI handler in
:mod:`tostools.tos` reshapes the report into rich tables; the
``format_triage_file`` helper below renders the same data into a
commented ``ACTION``-style block consumable by ``tos audit apply``
and by the combined ``tos station triage`` flow.

Concept: each archived day under ``<archive>/<YYYY>/<mon>/<STATION>/``
carries a file-extension signal that identifies the receiver-brand
family that wrote it. By walking the timeline and comparing against
TOS's child-receiver joins we surface:

  * brand transitions (real hardware changes)
  * multi-day data gaps
  * windows where only RINEX files exist (raw missing)
  * TOS joins that disagree with the archive (verdicts like
    ``join_too_wide``, ``late_start``, ``early_end``)

Outputs ``StationRinexReport`` — same data the CLI handler and
``station_triage`` orchestrator consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api.tos_client import TOSClient

# Model-substring → expected brand-family map. Keys are matched
# case-insensitively against the device's open ``model`` attribute;
# first matching rule wins. Add a rule when a new receiver model
# shows up.
MODEL_TO_FAMILY = (
    ("netr9", "trimble_netr9"),
    ("netrs", "trimble_netrs"),
    ("trimble 4000", "trimble_4000"),
    ("polarx", "septentrio"),
    ("sept", "septentrio"),
)


def infer_expected_family(model: Optional[str]) -> Optional[str]:
    """Map a TOS-stored model string to the archive's brand-family code.

    Returns ``None`` when no rule matches (e.g. ``ASHTECH UZ-12`` — no
    .sbf-style raw extension is mapped for ASHTECH; verdict logic
    treats the model as 'unmapped' rather than 'wrong').
    """
    if not model:
        return None
    m = model.lower()
    for needle, family in MODEL_TO_FAMILY:
        if needle in m:
            return family
    return None


def classify_tos_join_against_archive(
    time_from: str,
    time_to: Optional[str],
    expected_family: Optional[str],
    timeline,
) -> Dict[str, Any]:
    """Compare a TOS join window against the archive's brand timeline.

    Returns a dict with ``status`` and human-readable ``detail``; when
    the verdict implies a fixable ACTION (``join_too_wide`` is the
    canonical case — the bulk-load placeholder dates), also includes
    ``suggested_action_args`` so the caller can render the operator-
    targeted suggestion.

    Status values:
      * ``no_archive_coverage`` — no archived days in window
      * ``unmapped_model`` — TOS model doesn't map to a known family
      * ``rinex_only`` — only RINEX (format-neutral) days in window
      * ``ok`` — only the expected family present in the window
      * ``late_start`` / ``early_end`` / ``join_too_wide`` /
        ``wrong_brand`` — various levels of disagreement, with
        ``suggested_action_args`` populated when fixable.
    """
    tf = time_from[:10] if time_from else ""
    tt = time_to[:10] if time_to else None

    in_window = [
        d
        for d in timeline
        if (not tf or str(d.obs_date) >= tf) and (tt is None or str(d.obs_date) < tt)
    ]
    if not in_window:
        return {
            "status": "no_archive_coverage",
            "detail": "no archived data in window",
        }
    if expected_family is None:
        archive_families = sorted({d.family for d in in_window if d.is_raw})
        return {
            "status": "unmapped_model",
            "detail": (
                f"model not in MODEL_TO_FAMILY map; archive shows "
                f"{','.join(archive_families) or 'rinex-only'}"
            ),
        }
    raw_days = [d for d in in_window if d.is_raw]
    if not raw_days:
        return {
            "status": "rinex_only",
            "detail": "only RINEX days in window; brand undetermined",
        }
    expected_days = [d for d in raw_days if d.family == expected_family]
    other_days = [d for d in raw_days if d.family != expected_family]

    if not expected_days:
        other_families = sorted({d.family for d in other_days})
        return {
            "status": "wrong_brand",
            "detail": (
                f"expected {expected_family}; archive shows "
                f"{','.join(other_families)} throughout"
            ),
        }
    if not other_days:
        return {
            "status": "ok",
            "detail": f"archive {expected_family} throughout",
        }
    # Both present — the SAVI-style "join too wide" case.
    first_expected = expected_days[0].obs_date
    last_expected = expected_days[-1].obs_date
    first_other = other_days[0].obs_date
    last_other = other_days[-1].obs_date

    if first_other < first_expected and last_other < first_expected:
        other_families = sorted({d.family for d in other_days})
        return {
            "status": "late_start",
            "detail": (
                f"expected {expected_family} starts {first_expected} "
                f"(archive shows {','.join(other_families)} before that)"
            ),
            "suggested_action_args": ("time_from", str(first_expected)),
        }
    if first_other > last_expected and last_other > last_expected:
        other_families = sorted({d.family for d in other_days})
        return {
            "status": "early_end",
            "detail": (
                f"expected {expected_family} ends {last_expected} "
                f"(archive shows {','.join(other_families)} after that)"
            ),
            "suggested_action_args": ("time_to", str(last_expected)),
        }
    other_families = sorted({d.family for d in other_days})
    return {
        "status": "join_too_wide",
        "detail": (
            f"join window contains both {expected_family} and "
            f"{','.join(other_families)}; expected family first appears "
            f"{first_expected}"
        ),
        "suggested_action_args": ("time_from", str(first_expected)),
    }


@dataclass
class TOSReceiverVerdict:
    """One TOS receiver join classified against the archive timeline.

    Fields mirror the per-row data the CLI handler / triage renderer
    consume. ``suggested_action`` carries the operator-pasteable
    ``ACTION ... patch-join-date ...`` line for fixable verdicts;
    ``None`` when the verdict is clean or non-actionable.
    """

    id_entity: int
    serial: Optional[str]
    model: Optional[str]
    time_from: str
    time_to: Optional[str]
    id_connection: Optional[int]
    expected_family: Optional[str]
    status: str
    detail: str
    suggested_action: Optional[str] = None


@dataclass
class StationRinexReport:
    """Aggregated archive-vs-TOS findings for one station.

    Returned by :func:`audit_station_verify_from_rinex` and consumed
    by both the CLI rendering path and the
    :mod:`tostools.station_triage` composer.

    ``has_findings`` is the pass/fail signal for the
    ``tos station verify --with-archive`` oracle: True iff any brand
    transition, data gap, or actionable receiver verdict is present.
    """

    station: str
    station_id: Optional[int]
    archive_root: Path
    timeline_count: int
    first_day: Optional[str]
    last_day: Optional[str]
    brand_runs: List[Any] = field(default_factory=list)
    brand_transitions: List[Any] = field(default_factory=list)
    data_gaps: List[Any] = field(default_factory=list)
    rinex_only_spans: List[Any] = field(default_factory=list)
    receivers: List[TOSReceiverVerdict] = field(default_factory=list)
    suggested_actions: List[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        """True if the report surfaces any operator-actionable signal.

        Brand transitions and data gaps are always findings (they
        always need a human read). Receiver verdicts contribute only
        when they carry a ``suggested_action`` (``join_too_wide``,
        ``late_start``, ``early_end``) — clean and informational
        statuses don't count.
        """
        if self.brand_transitions or self.data_gaps:
            return True
        return any(r.suggested_action for r in self.receivers)


def audit_station_verify_from_rinex(
    client: TOSClient,
    station: str,
    *,
    archive_root: Optional[Path] = None,
    min_gap_days: float = 30.0,
) -> StationRinexReport:
    """Cross-check one station's TOS state against the cold RINEX archive.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. No writes.
    station
        Station marker (e.g. ``"HEDI"``) or display name.
    archive_root
        Optional override of the archive root (env / config fallbacks
        documented in :func:`tostools.archive.cold_archive_prepath`).
    min_gap_days
        Minimum gap duration to flag (default 30; below ~7 the report
        fills with date-rounding noise).

    Returns
    -------
    StationRinexReport
        Empty timeline when the station has no archived data — callers
        can branch on ``timeline_count == 0`` before rendering.
    Raises
    ------
    FileNotFoundError
        Archive root cannot be resolved.
    """
    from . import archive as archive_mod
    from .devices import open_attribute

    # Defer import-failure surfacing to the caller — archive root
    # resolution can fail on offline / no-mount workflows and we
    # want the operator to see the candidate-paths message.
    resolved_root = archive_mod.cold_archive_prepath(override=archive_root)

    timeline = list(archive_mod.walk_station_timeline(station, resolved_root))

    # Empty timeline short-circuits the report — caller decides
    # whether to treat as error or informational.
    if not timeline:
        return StationRinexReport(
            station=station,
            station_id=None,
            archive_root=resolved_root,
            timeline_count=0,
            first_day=None,
            last_day=None,
        )

    transitions = archive_mod.detect_brand_transitions(timeline)
    gaps = archive_mod.detect_data_gaps(timeline, min_days=int(min_gap_days))
    brand_runs = archive_mod.coalesce_brand_runs(timeline)
    rinex_only_spans = archive_mod.detect_rinex_only_spans(timeline)

    # Resolve station + walk its child receivers — same primitives as
    # `tos device list`. Resolution failures yield an empty receivers
    # list rather than raising; the operator still sees the archive
    # side of the picture.
    parent_id = _resolve_station_id(client, station)
    receivers: List[TOSReceiverVerdict] = []
    if parent_id is not None:
        parent = client.get_entity_history(parent_id)
        if parent:
            for conn in parent.get("children_connections") or []:
                child_id_raw = conn.get("id_entity_child")
                if child_id_raw is None:
                    continue
                try:
                    child_id = int(child_id_raw)
                except (TypeError, ValueError):
                    continue
                child = client.get_entity_history(child_id) or {}
                if child.get("code_entity_subtype") != "gnss_receiver":
                    continue
                tf = (conn.get("time_from") or "")[:10]
                tt_raw = conn.get("time_to")
                tt = tt_raw[:10] if tt_raw else None
                expected_family = infer_expected_family(open_attribute(child, "model"))
                verdict = classify_tos_join_against_archive(
                    tf, tt, expected_family, timeline
                )
                sug = verdict.get("suggested_action_args")
                id_conn = conn.get("id_entity_connection") or conn.get("id")
                suggested_action = None
                if sug and id_conn is not None:
                    field_name, new_date = sug
                    was = tf if field_name == "time_from" else (tt or "open")
                    suggested_action = (
                        f"ACTION {child_id} patch-join-date "
                        f"{id_conn} {field_name} {new_date}  # was {was}"
                    )
                receivers.append(
                    TOSReceiverVerdict(
                        id_entity=child_id,
                        serial=open_attribute(child, "serial_number"),
                        model=open_attribute(child, "model"),
                        time_from=tf,
                        time_to=tt,
                        id_connection=int(id_conn) if id_conn is not None else None,
                        expected_family=expected_family,
                        status=verdict["status"],
                        detail=verdict["detail"],
                        suggested_action=suggested_action,
                    )
                )
            receivers.sort(key=lambda r: r.time_from or "")

    suggested_actions = [r.suggested_action for r in receivers if r.suggested_action]

    return StationRinexReport(
        station=station,
        station_id=parent_id,
        archive_root=resolved_root,
        timeline_count=len(timeline),
        first_day=str(timeline[0].obs_date),
        last_day=str(timeline[-1].obs_date),
        brand_runs=brand_runs,
        brand_transitions=transitions,
        data_gaps=gaps,
        rinex_only_spans=rinex_only_spans,
        receivers=receivers,
        suggested_actions=suggested_actions,
    )


def _resolve_station_id(client: TOSClient, station: str) -> Optional[int]:
    """Resolve a station marker → id via basic_search.

    Tiny wrapper that mirrors ``_resolve_parent_id`` in tos.py but
    lives here so this module stays import-graph-tidy (no circular
    dep on tos.py). Same matching contract: marker, exact, type
    ``stöð``.
    """
    if not station:
        return None
    needle = station.lower()
    for hit in client.basic_search(needle):
        if hit.get("code") != "marker":
            continue
        if hit.get("distance") != 0:
            continue
        if (hit.get("value_varchar") or "").lower() != needle:
            continue
        if hit.get("type_lvl_two") != "stöð":
            continue
        entity_id = hit.get("id_entity") or hit.get("id_lvl_two")
        if entity_id:
            return int(entity_id)
    return None


def format_triage_file(
    report: StationRinexReport,
    *,
    audit_command: str = "tos audit verify-from-rinex",
    generated_at: Optional[str] = None,
) -> str:
    """Render a :class:`StationRinexReport` as a commented ACTION block.

    Mirrors the format used by :func:`audit_attribute_dates.format_triage_file`
    and :func:`audit_missing_attributes.format_triage_file` — header
    line, command preamble, one commented ``ACTION`` per actionable
    finding. Operator opts in by uncommenting.

    Surfaces:
      * brand transitions as informational comments (humans decide
        whether to insert new TOS joins; no auto-ACTION).
      * data gaps as informational comments.
      * actionable receiver verdicts as ``# ACTION ... patch-join-date``
        lines — already constructed in the report's ``suggested_actions``.
    """
    import datetime as dt

    if generated_at is None:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        generated_at = now.isoformat(timespec="seconds") + "Z"

    lines: List[str] = [
        f"# Section: archive verification — {report.station}",
        f"# Audit command: {audit_command}",
        f"# Generated: {generated_at}",
        f"# Archive root: {report.archive_root}",
        f"# Timeline: {report.timeline_count} archived day(s) "
        f"({report.first_day} → {report.last_day})",
        "#",
    ]

    if report.brand_transitions:
        lines.append(
            f"# Brand transitions ({len(report.brand_transitions)} — "
            "real hardware changes per file-extension signal):"
        )
        for t in report.brand_transitions:
            lines.append(
                f"#   {t.date_before} ({t.family_before}) → "
                f"{t.date_after} ({t.family_after})"
            )
        lines.append("#")

    if report.data_gaps:
        lines.append(f"# Data gaps ({len(report.data_gaps)} ≥30d — dormant periods):")
        for g in report.data_gaps:
            lines.append(
                f"#   {g.last_day_with_data} → {g.next_day_with_data}  "
                f"({g.duration_days}d)"
            )
        lines.append("#")

    if report.rinex_only_spans:
        lines.append(
            f"# RINEX-only spans ({len(report.rinex_only_spans)} — raw missing):"
        )
        for s in report.rinex_only_spans:
            lines.append(f"#   {s.start} → {s.end}  ({s.days}d)")
        lines.append("#")

    if report.suggested_actions:
        lines.append(
            f"# Suggested ACTION lines ({len(report.suggested_actions)} — "
            "uncomment to apply):"
        )
        for action in report.suggested_actions:
            lines.append(f"#{action}")
        lines.append("#")
    else:
        lines.append(
            "# No actionable TOS-vs-archive discrepancies found "
            "(all receiver joins clean or informational)."
        )

    return "\n".join(lines)
