"""Cross-check TOS constellation toggles against what the receiver records.

TOS carries ``GPS``/``GLO``/``GAL``/``BDS``/``QZSS``/``SBAS``/``IRN`` toggles per
gnss_receiver, but they usually sit empty and nothing verifies them. This audit
reads the constellation set actually recorded in the archive (RINEX-3 header) and
compares it to the current receiver's open TOS toggles.

Direction of trust — the RINEX-2 under-report caveat drives it:

* **data shows a system, TOS doesn't say ``true``** → propose ``set_true``. This
  is safe even from an R2 reading: if the data records the system it is on.
* **TOS says ``true`` but the data doesn't show it** → only a ``review`` finding,
  and only when the reading is a *reliable* R3 header — an R2 header can
  under-report, so absence there proves nothing.

Emits an apply-ready triage (``add-attribute-period <code> true <date> open``)
for the ``set_true`` findings, dated at the receiver's open-join install date.
Read-only; the live-receiver query (authoritative for PolaRX5) is the preferred
confirmation and is noted, not performed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .api.tos_client import TOSClient
from .archive import classify_file_format, cold_archive_prepath
from .audit import _resolve_station_entity
from .audit_attribute_dates import (
    _date_only,
    _open_attribute_value,
    _station_display_name,
    _station_joins_by_device,
)
from .constellation import (
    TOS_CONSTELLATION_CODES,
    ConstellationReading,
    read_constellations,
)
from .constellation_history import (
    DEFAULT_MIN_SEGMENT_DAYS,
    list_rinex_in_period,
    segment_by_constellation,
    system_first_seen,
)
from .rinex.reader import find_most_recent_rinex


@dataclass(frozen=True)
class ConstellationFinding:
    """One constellation code's data-vs-TOS verdict for a receiver."""

    code: str  # GPS/GLO/GAL/...
    observed: bool  # the archive reading records this system
    tos_value: Optional[str]  # open TOS value ("true"/"false"/None)
    action: str  # "set_true" | "review" | "ok"


@dataclass
class StationConstellationReport:
    """Result of :func:`audit_station_constellations`."""

    station_id: int
    station_name: Optional[str]
    receiver_id: Optional[int] = None
    receiver_serial: Optional[str] = None
    receiver_install: Optional[str] = None
    reading: Optional[ConstellationReading] = None
    findings: List[ConstellationFinding] = field(default_factory=list)
    note: Optional[str] = None

    @property
    def set_true(self) -> List[ConstellationFinding]:
        return [f for f in self.findings if f.action == "set_true"]

    @property
    def reviews(self) -> List[ConstellationFinding]:
        return [f for f in self.findings if f.action == "review"]

    @property
    def has_findings(self) -> bool:
        return bool(self.set_true or self.reviews)


def _open_receiver(station_history: dict, client: TOSClient):
    """Return ``(receiver_history, install_date)`` for the station's open
    gnss_receiver, or ``(None, None)`` when there isn't exactly one."""
    joins_by_device = _station_joins_by_device(station_history)
    for device_id, joins in joins_by_device.items():
        open_joins = [j for j in joins if j.get("time_to") is None]
        if not open_joins:
            continue
        history = client.get_entity_history(device_id)
        if not history or history.get("code_entity_subtype") != "gnss_receiver":
            continue
        dates = [
            _date_only(str(j["time_from"])) for j in open_joins if j.get("time_from")
        ]
        install = min(dates) if dates else None
        return history, install
    return None, None


def audit_station_constellations(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    archive_root: Optional[Union[str, Path]] = None,
) -> StationConstellationReport:
    """Compare the current receiver's TOS constellation toggles to the archive.

    Read-only. Resolves the station's open gnss_receiver, reads the most recent
    daily RINEX header for the constellation set, and produces per-code findings.
    Raises ``LookupError`` if the station isn't found.
    """
    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_name = _station_display_name(station_history, name)
    report = StationConstellationReport(
        station_id=station_id, station_name=station_name
    )

    rx, install = _open_receiver(station_history, client)
    if rx is None:
        report.note = "no open gnss_receiver at station"
        return report
    report.receiver_id = int(rx["id_entity"])
    report.receiver_serial = _open_attribute_value(rx, "serial_number")
    report.receiver_install = install

    root = Path(archive_root) if archive_root else cold_archive_prepath()
    if root is None:
        report.note = "no archive root resolved"
        return report
    marker = _open_attribute_value(station_history, "marker") or (name or "")
    rinex_path = find_most_recent_rinex(marker, base_dir=root)
    if rinex_path is None:
        report.note = f"no archived RINEX under {root}"
        return report
    reading = read_constellations(rinex_path)
    if reading is None:
        report.note = f"could not read RINEX header: {rinex_path}"
        return report
    report.reading = reading

    # The constellation set must belong to the CURRENT receiver. The most recent
    # RINEX is normally its data, but a down / just-installed station can leave
    # the newest archived day sitting in a PREVIOUS receiver's tenure — reading
    # that would misattribute the old receiver's systems. Gate on the RINEX date
    # being on/after the current receiver's install; otherwise no cross-check.
    rinex_date = classify_file_format(Path(rinex_path).name).date
    if install and rinex_date and str(rinex_date) < install:
        report.note = (
            f"most recent RINEX {rinex_date} predates current receiver install "
            f"{install} — no data yet for the current receiver (no cross-check)"
        )
        return report

    for code in TOS_CONSTELLATION_CODES:
        observed = code in reading.systems
        tos_value = _open_attribute_value(rx, code)
        tos_true = (tos_value or "").lower() == "true"
        if observed and not tos_true:
            # Data proves the system is on — valid even from an R2 reading.
            action = "set_true"
        elif tos_true and not observed and reading.reliable:
            # TOS claims a system the reliable R3 header doesn't record.
            action = "review"
        else:
            action = "ok"
        report.findings.append(
            ConstellationFinding(
                code=code, observed=observed, tos_value=tos_value, action=action
            )
        )
    return report


def format_triage(report: StationConstellationReport) -> List[str]:
    """Apply-ready ACTION lines (commented) for the ``set_true`` findings."""
    lines: List[str] = []
    if not report.set_true or report.receiver_id is None:
        return lines
    date_from = report.receiver_install or "<FILL_DATE>"
    caveat = (
        ""
        if (report.reading and report.reading.reliable)
        else " (R2 reading — confirm vs raw/live receiver)"
    )
    lines.append(
        f"# {report.station_name} constellations — data records systems TOS omits{caveat}"
    )
    lines.append("# ACTIONS COMMENTED — verify then uncomment.")
    for f in report.set_true:
        lines.append(
            f"# ACTION {report.receiver_id} add-attribute-period {f.code} true {date_from} open"
        )
    return lines


# --------------------------------------------------------------------------- #
# --history: reconstruct constellations across ALL receiver device periods
# --------------------------------------------------------------------------- #


@dataclass
class ReceiverPeriodConstellations:
    """Reconstructed constellations for one gnss_receiver device period."""

    device_id: Optional[int]
    serial: Optional[str]
    model: Optional[str]
    date_from: Optional[date]  # join install (inclusive)
    date_to: Optional[date]  # join removal (exclusive); None = open
    first_seen: Dict[str, date] = field(default_factory=dict)  # code → first date
    reliable: bool = False  # all reconstructed segments were R3
    missing: List[Tuple[str, date]] = field(default_factory=list)  # (code, from)
    n_files: int = 0
    note: Optional[str] = None


@dataclass
class StationConstellationHistoryReport:
    """Result of :func:`audit_station_constellations_history`."""

    station_id: int
    station_name: Optional[str]
    marker: str
    periods: List[ReceiverPeriodConstellations] = field(default_factory=list)
    note: Optional[str] = None

    @property
    def has_actions(self) -> bool:
        return any(p.missing for p in self.periods)


def _to_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(_date_only(str(value)))
    except ValueError:
        return None


def _code_true_in_period(
    history: Dict[str, Any],
    code: str,
    period_from: Optional[date],
    period_to: Optional[date],
) -> bool:
    """True if ``code`` is already recorded ``true`` on the device for a span
    overlapping ``[period_from, period_to)``. Period-aware (a decommissioned
    receiver's toggles are closed, so the open-only reader would miss them)."""
    for a in history.get("attributes") or []:
        if a.get("code") != code:
            continue
        if str(a.get("value") or "").lower() != "true":
            continue
        a_from = _to_date(a.get("date_from"))
        a_to = _to_date(a.get("date_to"))
        left_ok = a_to is None or period_from is None or a_to > period_from
        right_ok = period_to is None or a_from is None or a_from < period_to
        if left_ok and right_ok:
            return True
    return False


def audit_station_constellations_history(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    archive_root: Optional[Union[str, Path]] = None,
    min_segment_days: int = DEFAULT_MIN_SEGMENT_DAYS,
) -> StationConstellationHistoryReport:
    """Reconstruct constellations for EVERY receiver period and flag TOS gaps.

    Read-only. For each gnss_receiver ever joined to the station, reconstructs
    the satellite systems from the archived RINEX over each join period (first/
    last + binary search) and proposes ``add-attribute-period`` for any system
    the data records that TOS does not already carry ``true`` for that span.
    """
    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_name = _station_display_name(station_history, name)
    marker = _open_attribute_value(station_history, "marker") or (name or "")
    report = StationConstellationHistoryReport(
        station_id=station_id, station_name=station_name, marker=marker
    )

    root = Path(archive_root) if archive_root else cold_archive_prepath()
    if root is None:
        report.note = "no archive root resolved"
        return report

    for device_id, joins in _station_joins_by_device(station_history).items():
        history = client.get_entity_history(device_id)
        if not history or history.get("code_entity_subtype") != "gnss_receiver":
            continue
        serial = _open_attribute_value(history, "serial_number")
        model = _open_attribute_value(history, "model")
        for join in joins:
            df = _to_date(join.get("time_from"))
            dt = _to_date(join.get("time_to"))
            files = list_rinex_in_period(root, marker, df, dt)
            segments = segment_by_constellation(files)
            first_seen = system_first_seen(segments, min_segment_days=min_segment_days)
            reliable = bool(segments) and all(s.reliable for s in segments)
            missing = [
                (code, first_seen[code])
                for code in TOS_CONSTELLATION_CODES
                if code in first_seen
                and not _code_true_in_period(history, code, df, dt)
            ]
            missing.sort(key=lambda cd: (cd[1], cd[0]))
            report.periods.append(
                ReceiverPeriodConstellations(
                    device_id=int(device_id) if device_id is not None else None,
                    serial=serial,
                    model=model,
                    date_from=df,
                    date_to=dt,
                    first_seen=first_seen,
                    reliable=reliable,
                    missing=missing,
                    n_files=len(files),
                    note=None if files else "no archived RINEX in period",
                )
            )

    report.periods.sort(key=lambda p: (p.date_from or date.min))
    return report


def format_history_triage(report: StationConstellationHistoryReport) -> List[str]:
    """Apply-ready (commented) ACTION lines for the per-period gaps."""
    lines: List[str] = []
    if not report.has_actions:
        return lines
    lines.append(
        f"# {report.station_name} constellation history — systems the archive "
        "records that TOS omits, per receiver period"
    )
    lines.append("# ACTIONS COMMENTED — verify then uncomment.")
    for p in report.periods:
        if not p.missing:
            continue
        end = p.date_to.isoformat() if p.date_to else "open"
        caveat = "" if p.reliable else "   # R2/best-effort — confirm vs raw"
        lines.append(f"# --- {p.model} {p.serial}  [{p.date_from} -> {end}]{caveat}")
        if p.device_id is None:
            lines.append("#     (no device id resolved — skipped)")
            continue
        for code, cdate in p.missing:
            lines.append(
                f"# ACTION {p.device_id} add-attribute-period {code} true "
                f"{cdate.isoformat()} {end}"
            )
    return lines
