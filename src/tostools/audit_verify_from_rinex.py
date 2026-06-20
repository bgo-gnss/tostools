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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api.tos_client import TOSClient
from .receiver_timeline import ReceiverHeader

logger = logging.getLogger(__name__)

# IGS-normalized receiver type → ``cfg replace-receiver --new-type`` token.
# Keys are the value of ``ReceiverHeader.key[0]`` (i.e. ``_norm_type`` output,
# which runs ``to_igs_receiver`` first). Only the families the receivers verb
# actually probes are mapped — see the ``--new-type`` choices in
# ``receivers/src/receivers/cli/cfg.py``. Anything unmapped renders a
# ``<TYPE?>`` placeholder for the operator to fill (the suggestion is a
# review-first commented command, never auto-run).
_REPLACE_TYPE_TOKEN = {
    "SEPT POLARX5": "polarx5",
    "TRIMBLE NETR9": "netr9",
    "TRIMBLE NETRS": "netrs",
    "TRIMBLE NETR5": "netr5",
}

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
class CurrentReceiverVerdict:
    """RINEX current-install vs TOS's currently-open ``gnss_receiver`` join.

    The receiver-LEVEL counterpart to :class:`TOSReceiverVerdict` (which is
    brand-level). Where the brand audit only sees the file-extension family —
    and goes blind on the rinex-only spans where many current receivers live —
    this reads the actual ``REC # / TYPE / VERS`` header via
    :mod:`tostools.receiver_timeline`, so it catches the case that motivated
    todo #40: TOS still holds a long-retired open receiver (OLKE's TRIMBLE 4700
    from 2000) while the archive shows the PolaRX5 that's really been deployed
    since 2017.

    ``suggested_command`` carries an operator-pasteable **``receivers cfg
    replace-receiver``** shell command (Pattern-2 device swap, dated to the
    RINEX install proxy) — NOT a ``tos audit apply`` ACTION line. It is
    populated only for actionable disagreements (``type_mismatch`` /
    ``serial_mismatch``); ``None`` for clean / informational / firmware-only
    statuses. RINEX dates are a strong proxy, never gospel: suggest, review,
    run manually.

    Status values:
      * ``ok`` — RINEX and TOS open join agree on type and serial
      * ``type_mismatch`` — different receiver type (the OLKE case)
      * ``serial_mismatch`` — same type, different (both-known) serial
      * ``firmware_drift`` — type+serial agree, firmware differs
        (informational; recording it is todo #39, no replace-receiver)
      * ``no_open_join`` — no open ``gnss_receiver`` join in TOS
      * ``no_rinex_receiver`` — archive header carries no usable identity
    """

    status: str
    detail: str
    rinex_type: Optional[str] = None
    rinex_serial: Optional[str] = None
    rinex_firmware: Optional[str] = None
    rinex_install_date: Optional[str] = None
    tos_type: Optional[str] = None
    tos_serial: Optional[str] = None
    tos_firmware: Optional[str] = None
    tos_time_from: Optional[str] = None
    suggested_command: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        """True iff this verdict carries a replace-receiver suggestion."""
        return self.suggested_command is not None


def _replace_receiver_command(
    station: str,
    header: ReceiverHeader,
    install_date: Optional[str],
) -> str:
    """Build the dated ``receivers cfg replace-receiver`` suggestion line.

    The IGS-normalized type drives ``--new-type``; unmapped families render a
    ``<TYPE?>`` placeholder (the operator fills it before running). Serial /
    firmware / date are included when known so the line is as close to
    runnable as the archive allows — but it stays commented in the triage file.
    """
    norm_type = header.key[0]
    token = _REPLACE_TYPE_TOKEN.get(norm_type or "", "<TYPE?>")
    parts = [
        "receivers cfg replace-receiver",
        f"--station {station.upper()}",
        f"--new-type {token}",
    ]
    if header.serial:
        parts.append(f"--new-serial {header.serial}")
    if norm_type:
        parts.append(f'--new-model "{norm_type}"')
    if header.firmware:
        parts.append(f"--new-firmware {header.firmware}")
    if install_date:
        parts.append(f"--date {install_date}")
    return " ".join(parts)


def classify_current_receiver(
    station: str,
    rinex_header: Optional[ReceiverHeader],
    rinex_install_date: Optional[str],
    tos_type: Optional[str],
    tos_serial: Optional[str],
    tos_firmware: Optional[str],
    tos_time_from: Optional[str],
) -> CurrentReceiverVerdict:
    """Compare the archive's current receiver against TOS's open join.

    Pure function — no archive walk, no TOS call — so it is unit-testable in
    isolation. Both sides are normalized through the SAME
    :class:`~tostools.receiver_timeline.ReceiverHeader` key (type via
    ``to_igs_receiver``, firmware ``5.50``≡``5.5.0``, placeholder serials →
    unknown): a disagreement is a real one, not header-vs-TOS spelling noise.
    With most of the fleet already drifting this normalization parity is the
    line between useful triage and a wall of false positives.

    ``rinex_install_date`` is the ``start`` of the archive's current segment
    (already an ISO ``YYYY-MM-DD`` string) — the install-date proxy that dates
    the suggested ``replace-receiver``.
    """
    if rinex_header is None or not rinex_header.is_known:
        return CurrentReceiverVerdict(
            status="no_rinex_receiver",
            detail="archive header carries no usable receiver identity",
            rinex_install_date=rinex_install_date,
        )

    rtype, rserial, rfw = rinex_header.key
    common = dict(
        rinex_type=rinex_header.rtype,
        rinex_serial=rinex_header.serial,
        rinex_firmware=rinex_header.firmware,
        rinex_install_date=rinex_install_date,
        tos_type=tos_type,
        tos_serial=tos_serial,
        tos_firmware=tos_firmware,
        tos_time_from=tos_time_from,
    )

    tos_header = ReceiverHeader(
        serial=tos_serial, rtype=tos_type, firmware=tos_firmware
    )
    if not tos_header.is_known and not tos_time_from:
        return CurrentReceiverVerdict(
            status="no_open_join",
            detail=(
                "no open gnss_receiver join in TOS; archive shows "
                f"{rinex_header.rtype or '?'} since {rinex_install_date or '?'}"
            ),
            **common,
        )
    ttype, tserial, tfw = tos_header.key

    if rtype != ttype:
        cmd = _replace_receiver_command(station, rinex_header, rinex_install_date)
        return CurrentReceiverVerdict(
            status="type_mismatch",
            detail=(
                f"TOS open receiver {tos_type or '?'} "
                f"(since {tos_time_from or '?'}) but archive shows "
                f"{rinex_header.rtype or '?'} since {rinex_install_date or '?'}"
            ),
            suggested_command=cmd,
            **common,
        )

    if rserial is not None and tserial is not None and rserial != tserial:
        cmd = _replace_receiver_command(station, rinex_header, rinex_install_date)
        return CurrentReceiverVerdict(
            status="serial_mismatch",
            detail=(
                f"same type ({rinex_header.rtype or '?'}) but TOS serial "
                f"{tos_serial or '?'} ≠ archive serial {rinex_header.serial or '?'} "
                f"(archive since {rinex_install_date or '?'})"
            ),
            suggested_command=cmd,
            **common,
        )

    if rfw is not None and tfw is not None and rfw != tfw:
        return CurrentReceiverVerdict(
            status="firmware_drift",
            detail=(
                f"type+serial agree; firmware TOS {tos_firmware or '?'} ≠ "
                f"archive {rinex_header.firmware or '?'} "
                "(recording the change is todo #39 — no replace-receiver)"
            ),
            **common,
        )

    return CurrentReceiverVerdict(
        status="ok",
        detail=f"TOS open receiver matches archive ({rinex_header.rtype or '?'})",
        **common,
    )


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
    current_receiver: Optional[CurrentReceiverVerdict] = None

    @property
    def has_findings(self) -> bool:
        """True if the report surfaces any operator-actionable signal.

        Brand transitions and data gaps are always findings (they
        always need a human read). Receiver verdicts contribute only
        when they carry a ``suggested_action`` (``join_too_wide``,
        ``late_start``, ``early_end``) — clean and informational
        statuses don't count. The receiver-level current-install
        verdict contributes when it carries a ``replace-receiver``
        suggestion (``type_mismatch`` / ``serial_mismatch``).
        """
        if self.brand_transitions or self.data_gaps:
            return True
        if self.current_receiver is not None and self.current_receiver.is_actionable:
            return True
        return any(r.suggested_action for r in self.receivers)

    @property
    def finding_count(self) -> int:
        """Total findings across all surfaces.

        ``has_findings`` is the boolean oracle; this is the integer
        count for headers, fleet summaries, and the rinex slice of
        ``StationTriageReport.total_findings``. Definition pinned in
        one place so the three call sites (station header, fleet
        per-station row, single-station verify summary) cannot drift.
        """
        current = (
            1
            if self.current_receiver is not None and self.current_receiver.is_actionable
            else 0
        )
        return (
            len(self.brand_transitions)
            + len(self.data_gaps)
            + len(self.suggested_actions)
            + current
        )


def audit_station_verify_from_rinex(
    client: TOSClient,
    station: str,
    *,
    archive_root: Optional[Path] = None,
    min_gap_days: float = 30.0,
    check_current_receiver: bool = True,
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
    check_current_receiver
        When True (default) also build the RINEX-header receiver timeline
        and compare its current install against TOS's open receiver join
        (todo #40). This reads a few dozen RINEX headers — set False for the
        brand-only audit (e.g. archive-offline workflows or fleet sweeps
        where the header reads are too costly).

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
    # Fields of the currently-OPEN gnss_receiver join (time_to is None) — the
    # receiver-level current-install check below compares the archive's current
    # receiver against this. Latest open join wins if (anomalously) more than one.
    open_join: Optional[Dict[str, Optional[str]]] = None
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
                if tt is None and (
                    open_join is None or tf > (open_join["time_from"] or "")
                ):
                    open_join = {
                        "model": open_attribute(child, "model"),
                        "serial_number": open_attribute(child, "serial_number"),
                        "firmware_version": open_attribute(child, "firmware_version"),
                        "time_from": tf,
                    }
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

    # Receiver-LEVEL current-install check (todo #40): read the archive's
    # current receiver from RINEX headers and compare against TOS's open join.
    # Guarded — a header/archive read failure degrades to no verdict, never
    # sinks the brand-level report.
    current_receiver: Optional[CurrentReceiverVerdict] = None
    if check_current_receiver:
        try:
            from .receiver_timeline import (
                build_receiver_timeline,
                current_install,
                current_receiver_install_date,
            )

            rcv_timeline = build_receiver_timeline(station, root=resolved_root)
            cur = current_install(rcv_timeline)
            if cur is not None:
                # Install date of the receiver UNIT (back past firmware bumps),
                # not the latest firmware segment's start.
                install_date = current_receiver_install_date(rcv_timeline)
                current_receiver = classify_current_receiver(
                    station,
                    cur.header,
                    str(install_date) if install_date is not None else None,
                    open_join["model"] if open_join else None,
                    open_join["serial_number"] if open_join else None,
                    open_join["firmware_version"] if open_join else None,
                    open_join["time_from"] if open_join else None,
                )
        except Exception as exc:  # noqa: BLE001 — archive/header read is best-effort
            logger.warning("current-receiver check failed on %s: %s", station, exc)

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
        current_receiver=current_receiver,
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

    cr = report.current_receiver
    if cr is not None and cr.is_actionable:
        lines.append(
            f"# Current receiver ({cr.status} — archive header vs TOS open join):"
        )
        lines.append(f"#   {cr.detail}")
        lines.append(
            "#   Suggested fix — run MANUALLY in the receivers package after "
            "reviewing the date (RINEX install date is a proxy, not gospel):"
        )
        lines.append(f"#       {cr.suggested_command}")
        lines.append("#")

    if report.suggested_actions:
        lines.append(
            f"# Suggested ACTION lines ({len(report.suggested_actions)} — "
            "uncomment to apply):"
        )
        for action in report.suggested_actions:
            lines.append(f"#{action}")
        lines.append("#")
    elif cr is None or not cr.is_actionable:
        lines.append(
            "# No actionable TOS-vs-archive discrepancies found "
            "(all receiver joins clean or informational)."
        )

    return "\n".join(lines)
