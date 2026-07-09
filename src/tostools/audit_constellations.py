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
from pathlib import Path
from typing import List, Optional, Union

from .api.tos_client import TOSClient
from .archive import cold_archive_prepath
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
