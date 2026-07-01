"""``tos audit rinex-timeline`` — the empirical receiver/firmware/antenna timeline.

Reads RINEX headers across the cold archive and returns the segmented history of
one metadata field — the *empirical truth* (what was actually observed) that
``cfg onboard-station`` and the firmware-chain emitter consume, and that
``verify-from-rinex`` cross-checks TOS against.

Three fields, all from the same archive walk:

* ``firmware`` — every ``REC # / TYPE / VERS`` change (segments break on firmware
  bumps). Source: :func:`receiver_timeline.build_receiver_timeline`.
* ``receiver`` — the *physical receiver* timeline: firmware-only boundaries are
  coalesced (:func:`receiver_timeline.coalesce_receiver_units`) so one unit is one
  segment, dated to its install. This is NOT a re-display of ``firmware``.
* ``antenna`` — the ``ANT # / TYPE`` + ``ANTENNA: DELTA H/E/N`` timeline (type,
  radome, serial, ARP height). Source: :func:`antenna_timeline.build_antenna_timeline`.

Archive root resolves exactly as ``verify-from-rinex`` does — via
:func:`tostools.archive.cold_archive_prepath` (``--archive-root`` → env
``TOSTOOLS_ARCHIVE_ROOT`` → ``receivers.cfg`` → mount probe). Read-only, no TOS.

Known limitation — header serial truncation. Segment identity keys on the
normalized ``(type, serial, ...)``, but some receivers write a truncated serial
in RINEX (Trimble 4700: ``20147817`` in one era, ``7817`` in another). We do NOT
coalesce those — unlike the blank≡NONE radome equivalence (a safe IGS
convention), treating two serial spellings as one unit is a heuristic guess that
could mask a genuine dual-receiver case. So ``--field receiver`` shows such
formatting drift as **distinct segments by design**, and
``current_receiver_install_date`` can report a too-recent install date when a
station's *current* receiver's serial was truncated mid-tenure. The consumer
that must resolve this is ``firmware-chain`` (it keys the firmware chain on
serial); this verb faithfully reports what the headers say.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional

from . import antenna_timeline as ant_mod
from . import receiver_timeline as rec_mod
from .archive import cold_archive_prepath

FIELDS = ("receiver", "firmware", "antenna")


@dataclass
class RinexTimelineReport:
    station: str
    field: str
    rate: str
    archive_root: str
    rows: List[Dict[str, Any]] = dc_field(default_factory=list)
    unit_install_date: Optional[str] = None
    """For ``receiver`` / ``antenna``: the install date of the CURRENT unit
    (walks back across firmware / height-only boundaries). None for firmware."""

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "station": self.station,
            "field": self.field,
            "rate": self.rate,
            "archive_root": self.archive_root,
            "n_segments": len(self.rows),
            "current_unit_install_date": self.unit_install_date,
            "segments": self.rows,
        }


def _iso(d) -> str:
    return d.isoformat()


def run_rinex_timeline(
    station: str,
    field: str,
    *,
    root=None,
    rate: str = "15s_24hr",
) -> RinexTimelineReport:
    """Build the empirical timeline of one metadata field for ``station``.

    Args:
        station: Four-character marker (case-insensitive).
        field: One of :data:`FIELDS` (``receiver`` / ``firmware`` / ``antenna``).
        root: Archive-root override; resolved via
            :func:`tostools.archive.cold_archive_prepath` when ``None``.
        rate: Archive rate subdir (default ``15s_24hr``).

    Raises:
        ValueError: unknown ``field``.
    """
    if field not in FIELDS:
        raise ValueError(f"unknown field {field!r}; choose from {', '.join(FIELDS)}")

    resolved_root = cold_archive_prepath(override=root)
    report = RinexTimelineReport(
        station=station.upper(),
        field=field,
        rate=rate,
        archive_root=str(resolved_root),
    )

    if field in ("receiver", "firmware"):
        timeline = rec_mod.build_receiver_timeline(station, resolved_root, rate=rate)
        if field == "receiver":
            units = rec_mod.coalesce_receiver_units(timeline)
            report.rows = [
                {
                    "date_from": _iso(s.start),
                    "last_seen": _iso(s.end),
                    "rec_type": s.header.rtype,
                    "rec_serial": s.header.serial,
                }
                for s in units
            ]
            install = rec_mod.current_receiver_install_date(timeline)
            report.unit_install_date = _iso(install) if install else None
        else:  # firmware
            report.rows = [
                {
                    "date_from": _iso(s.start),
                    "last_seen": _iso(s.end),
                    "firmware": s.header.firmware,
                    "rec_type": s.header.rtype,
                    "rec_serial": s.header.serial,
                }
                for s in timeline
            ]
        return report

    # antenna
    timeline = ant_mod.build_antenna_timeline(station, resolved_root, rate=rate)
    report.rows = [
        {
            "date_from": _iso(s.start),
            "last_seen": _iso(s.end),
            "antenna_type": s.header.atype,
            "radome": s.header.radome,
            "ant_serial": s.header.serial,
            "delta_h": s.header.delta_h,
            "delta_e": s.header.delta_e,
            "delta_n": s.header.delta_n,
        }
        for s in timeline
    ]
    install = ant_mod.current_antenna_install_date(timeline)
    report.unit_install_date = _iso(install) if install else None
    return report


def format_report(report: RinexTimelineReport) -> str:
    """Render a :class:`RinexTimelineReport` as a human-readable table."""
    lines: List[str] = []
    lines.append(
        f"{report.station} {report.field} timeline "
        f"({len(report.rows)} segment{'' if len(report.rows) == 1 else 's'}, "
        f"rate={report.rate})"
    )
    if not report.rows:
        lines.append(f"  no archived RINEX headers found under {report.archive_root}")
        return "\n".join(lines)

    for r in report.rows:
        span = f"{r['date_from']} → {r['last_seen']}"
        if report.field == "firmware":
            detail = (
                f"fw={r['firmware'] or '?'}  "
                f"({r['rec_type'] or '?'} sn={r['rec_serial'] or '?'})"
            )
        elif report.field == "receiver":
            detail = f"{r['rec_type'] or '?'}  sn={r['rec_serial'] or '?'}"
        else:  # antenna
            h = "?" if r["delta_h"] is None else f"{r['delta_h']:.4f}"
            detail = (
                f"{r['antenna_type'] or '?'}/{r['radome'] or 'NONE'}  "
                f"sn={r['ant_serial'] or '?'}  h={h}"
            )
        lines.append(f"  {span}  {detail}")

    if report.unit_install_date:
        noun = "receiver" if report.field == "receiver" else "antenna"
        lines.append(f"  current {noun} unit installed: {report.unit_install_date}")
    lines.append(f"  archive: {report.archive_root}")
    return "\n".join(lines)
