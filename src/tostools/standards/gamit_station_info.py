"""Parser for GAMIT SOPAC ``station.info`` files.

``station.info`` is GAMIT's fixed-width station-metadata table: one line per
*occupation* (a continuous span where a given receiver + antenna were installed
at a marker). It carries IGS antenna names, radomes, serials, ARP heights and
``Year DOY`` session start/stop — exactly what TOS needs to reconstruct a
station's historical campaign occupations.

This module is the single, tested reader. The column map mirrors the one in
:func:`tostools.tosGPS._analyze_line_differences` (which only diffed raw lines);
here we slice them into a typed :class:`Occupation`.

Example line (real VOTT campaign occupation)::

    VOTT  Vottur            2012 155 00 00 00  2012 165 00 00 00   0.0000  DHARP   0.0000  -0.0004  TRIMBLE 5700          2.01                   2.01  0220331856            TRM41249.00      NONE   60004115
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

# Fixed-width column spans for the GAMIT SOPAC station.info format.
# (name, start, end): 0-based half-open ``line[start:end]``; ``end = -1`` means
# "to end of line". Boundaries derived from the header label start positions of
# the canonical ``station.info.sopac.apr05`` (each column runs from its own
# label start to the next column's). Note these differ from the SEPT-POLARX5-
# tuned map in tosGPS._analyze_line_differences — the SOPAC file's Ant Ht /
# HtCod / Vers / SwVer / Antenna Type columns sit a few characters apart.
_COLUMNS: tuple[tuple[str, int, int], ...] = (
    ("marker", 0, 7),
    ("station_name", 7, 25),
    ("session_start", 25, 44),
    ("session_stop", 44, 63),
    ("ant_height", 63, 72),
    ("htcod", 72, 79),
    ("ant_n", 79, 88),
    ("ant_e", 88, 97),
    ("receiver_type", 97, 119),
    ("vers", 119, 141),
    ("swver", 141, 148),
    ("receiver_sn", 148, 170),
    ("antenna_type", 170, 187),
    ("dome", 187, 194),
    ("antenna_sn", 194, -1),
)

# Sentinel GAMIT writes for an open-ended (never-closed) session.
_OPEN_SENTINEL_YEAR = 9999


@dataclass(frozen=True)
class Occupation:
    """One station.info occupation (a single closed/open install span).

    Dates are parsed to :class:`datetime`; ``time_to`` is ``None`` when the
    line carried the ``9999 999`` open-ended sentinel. ``antenna_sn`` of
    ``"0000000000"`` / ``"0000"`` (GAMIT's "unknown serial" filler) is
    normalised to an empty string so callers can substitute a synthetic serial.
    """

    marker: str
    station_name: str
    time_from: datetime
    time_to: Optional[datetime]
    receiver_type: str
    receiver_sn: str
    vers: str
    swver: str
    antenna_type: str
    dome: str
    antenna_sn: str
    antenna_height: str
    htcod: str
    #: Marker->ARP horizontal eccentricities (metres). Parsed from the ant_n /
    #: ant_e columns, which were read but previously discarded — they are the
    #: authoritative source for TOS antenna_offset_north / _east, where the
    #: catalog can only offer a 0.0 default. Real ISAK values run to 2.7 mm.
    antenna_north: str = "0.0000"
    antenna_east: str = "0.0000"

    @property
    def is_open(self) -> bool:
        """True when this occupation has no stop date (the 9999 999 sentinel)."""
        return self.time_to is None


#: Where the authoritative GAMIT station.info lives. NOT NFS-mounted anywhere:
#: the laptop mounts okada's /D/GMT/ and /D/GMT/TOT/, but not /D/DATABASE/, and
#: rek-d01 has no /D at all. So the packaged copy is a snapshot taken over ssh,
#: and that is exactly why this resolver reports which copy it used.
CANONICAL_REMOTE = "okada.vedur.is:/D/DATABASE/GAMIT/station.info.sopac.apr05"

#: Local paths probed before falling back to the packaged snapshot. okada
#: exported /D/DATABASE read-only on 2026-08-18, so the laptop now automounts it
#: and `origin=mount` is the normal case — a live read, no ssh hop inside an
#: audit, no snapshot drift.
#:
#: Both entries are cheap stats on a host that lacks them (rek-d01 has no /D and
#: no such mount). On the laptop the first is an autofs trigger, bounded by the
#: fstab's x-systemd.mount-timeout=10 — so off-network this costs ~10 s once,
#: then falls through to the packaged copy rather than failing.
_PROBE_PATHS = (
    "/mnt_data/gps_database/GAMIT/station.info.sopac.apr05",  # laptop NFS mount
    "/D/DATABASE/GAMIT/station.info.sopac.apr05",  # on okada itself
)


@dataclass(frozen=True)
class StationInfoSource:
    """Which station.info was read, and how it was found."""

    path: Path
    origin: str  #: 'override' | 'env' | 'config' | 'mount' | 'packaged'

    @property
    def is_snapshot(self) -> bool:
        """True when this is the frozen in-repo copy rather than a live file."""
        return self.origin == "packaged"

    def describe(self) -> str:
        try:
            from datetime import datetime as _dt

            age = _dt.fromtimestamp(self.path.stat().st_mtime).strftime("%Y-%m-%d")
            size = f", {self.path.stat().st_size // 1024} KB"
        except OSError:
            age, size = "unknown", ""
        base = f"{self.path} (origin={self.origin}, mtime={age}{size})"
        if self.is_snapshot:
            base += f" — SNAPSHOT, may have drifted from {CANONICAL_REMOTE}"
        return base


def resolve_station_info(
    override: Optional[Union[str, Path]] = None,
) -> Optional[StationInfoSource]:
    """Resolve which ``station.info`` to read, mirroring ``cold_archive_prepath``.

    Resolution order (first hit wins):
      1. Explicit ``override`` (CLI ``--station-info``).
      2. Env var ``TOSTOOLS_STATION_INFO``.
      3. ``[paths] gamit_station_info`` in the shared ``receivers.cfg``.
      4. A local mount, if ``/D/DATABASE`` is ever exported.
      5. The packaged snapshot shipped with tostools.

    Returns ``None`` only when even the packaged copy is absent.

    Unlike the archive root this never raises: a station.info is always better
    than none for the audits that consult it. What it does instead is report
    the ORIGIN, so "which station.info am I reading, and is it live?" is always
    answerable. The packaged copy was byte-identical to okada's on 2026-08-09
    (md5 0200c248…, 19,579 lines) — the risk is not that it is wrong today but
    that it drifts silently once GAMIT's copy is updated.
    """
    if override is not None:
        return StationInfoSource(Path(override).expanduser(), "override")

    import os

    env = os.environ.get("TOSTOOLS_STATION_INFO")
    if env:
        return StationInfoSource(Path(env).expanduser(), "env")

    try:
        import configparser

        from ..archive import _find_receivers_cfg

        cfg_path = _find_receivers_cfg()
        if cfg_path is not None:
            parser = configparser.ConfigParser()
            parser.read(cfg_path)
            if parser.has_option("paths", "gamit_station_info"):
                value = parser.get("paths", "gamit_station_info").strip()
                if value:
                    return StationInfoSource(Path(value).expanduser(), "config")
    except Exception:  # noqa: BLE001 — a bad cfg must never block the fallback
        pass

    for cand in _PROBE_PATHS:
        try:
            p = Path(cand).resolve()
            if p.is_file():
                return StationInfoSource(p, "mount")
        except OSError:
            continue

    packaged = Path(__file__).resolve().parents[3] / (
        "data/station_config/station.info.sopac.apr05"
    )
    if packaged.is_file():
        return StationInfoSource(packaged, "packaged")
    return None


#: The resolution chain, in order, spelled out for the unavailability note.
#: Kept beside :func:`resolve_station_info` so the two cannot drift.
RESOLUTION_CHAIN = (
    "--station-info override",
    "TOSTOOLS_STATION_INFO env",
    "receivers.cfg [paths] gamit_station_info",
    "/D/DATABASE mount",
    "packaged snapshot",
)


def resolve_for_audit(
    override: Optional[Union[str, Path]] = None,
) -> tuple[Optional[StationInfoSource], Optional[str]]:
    """Resolve ``station.info`` for the required-attribute audits.

    Returns ``(source, note)`` — **exactly one of them is None**:

    * resolved      -> ``(StationInfoSource, None)``
    * nothing found -> ``(None, note)`` explaining what was tried and what is lost

    Why a note and not just ``None``: an absent ``station.info`` used to be
    SILENT. With no oracle every suggestion in the missing-attributes audit falls
    back to the catalog default, and ``antenna_height`` — which has no catalog
    default — degrades to ``<FILL_VALUE>``. An operator reading the triage file
    cannot then tell "the field record genuinely has no value here" from "the
    oracle was never found". Observed 2026-09-14: the HEID/HRIC/HS02/HUSM/HVEL
    triages were generated on rek-d01, which has no GAMIT mount, so no height
    was suggested for any of them while the audit still reported success.
    """
    src = resolve_station_info(override)
    if src is not None:
        # The override/env/cfg branches of resolve_station_info return a path
        # WITHOUT checking it exists (only the mount/packaged probes do), and
        # load_station_info_occupations swallows OSError -> []. So a typo'd
        # --station-info would degrade exactly as silently as no file at all.
        if Path(src.path).is_file():
            return src, None
        return None, (
            f"station.info resolved to {src.path} (via {src.origin}) but that "
            "path is not a readable file — the field-record oracle is "
            "UNAVAILABLE, so suggested antenna_height/offsets fall back to the "
            "catalog default and antenna_height becomes <FILL_VALUE>."
        )
    tried = ", ".join(RESOLUTION_CHAIN)
    return None, (
        f"no station.info resolved (tried: {tried}) — the field-record oracle "
        "is UNAVAILABLE, so suggested antenna_height/offsets fall back to the "
        "catalog default and antenna_height becomes <FILL_VALUE>. Run this "
        "where station.info is reachable (e.g. the GAMIT mount) or pass "
        "--station-info."
    )


def _slice(line: str, start: int, end: int) -> str:
    if end == -1:
        return line[start:].strip()
    return line[start:end].strip()


def _parse_gamit_datetime(field: str) -> Optional[datetime]:
    """Parse a ``YYYY DOY HH MM SS`` field; return ``None`` for the open sentinel.

    Raises :class:`ValueError` on a malformed (non-sentinel) field so a corrupt
    line is surfaced rather than silently dropped.
    """
    parts = field.split()
    if len(parts) < 2:
        raise ValueError(f"unparseable station.info date field: {field!r}")
    year = int(parts[0])
    if year >= _OPEN_SENTINEL_YEAR:
        return None
    doy = int(parts[1])
    if not 1 <= doy <= 366:
        raise ValueError(f"day-of-year out of range in {field!r}")
    hh = int(parts[2]) if len(parts) > 2 else 0
    mm = int(parts[3]) if len(parts) > 3 else 0
    ss = int(parts[4]) if len(parts) > 4 else 0
    base = datetime.strptime(f"{year} {doy:03d}", "%Y %j")
    # timedelta (not .replace) so GAMIT's hour=24 "end of day" rolls to the next
    # day's 00:00 instead of raising.
    return base + timedelta(hours=hh, minutes=mm, seconds=ss)


def _normalise_serial(raw: str) -> str:
    """GAMIT fills unknown serials with all-zeros; treat that as 'unknown'."""
    stripped = raw.strip()
    if stripped and set(stripped) == {"0"}:
        return ""
    return stripped


def parse_line(line: str) -> Optional[Occupation]:
    """Parse one station.info data line into an :class:`Occupation`.

    Returns ``None`` for comments, headers and lines too short to carry a full
    record (mirrors the skip rule in ``_parse_station_info_file``).
    """
    raw = line.rstrip("\n\r")
    if not raw or raw.lstrip().startswith(("*", "#")) or len(raw) < 50:
        return None

    cols = {name: _slice(raw, start, end) for name, start, end in _COLUMNS}
    marker = cols["marker"]
    # Skip separator/comment rows (e.g. a '----' ruler) — real markers are
    # alphanumeric 4-char codes.
    if not marker or not marker.isalnum():
        return None

    time_from = _parse_gamit_datetime(cols["session_start"])
    if time_from is None:
        raise ValueError(
            f"station.info line for {marker!r} has an open-ended session START "
            f"({cols['session_start']!r}) — a start date is required"
        )

    return Occupation(
        marker=marker,
        station_name=cols["station_name"],
        time_from=time_from,
        time_to=_parse_gamit_datetime(cols["session_stop"]),
        receiver_type=cols["receiver_type"],
        receiver_sn=_normalise_serial(cols["receiver_sn"]),
        vers=cols["vers"],
        swver=cols["swver"],
        antenna_type=cols["antenna_type"],
        dome=cols["dome"] or "NONE",
        antenna_sn=_normalise_serial(cols["antenna_sn"]),
        antenna_height=cols["ant_height"],
        htcod=cols["htcod"],
        antenna_north=cols["ant_n"] or "0.0000",
        antenna_east=cols["ant_e"] or "0.0000",
    )


# Field start columns (0-based) for writing a station.info line. Each field is
# placed left-justified at its start and must fit before the next column — true
# for real SOPAC data. Inverse of :data:`_COLUMNS`.
_WRITE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("marker", 1),
    ("station_name", 7),
    ("session_start", 25),
    ("session_stop", 44),
    ("ant_height", 65),
    ("htcod", 72),
    ("ant_n", 80),
    ("ant_e", 88),
    ("receiver_type", 97),
    ("vers", 119),
    ("swver", 142),
    ("receiver_sn", 148),
    ("antenna_type", 170),
    ("dome", 187),
    ("antenna_sn", 194),
)


def _fmt_gamit_datetime(dt: Optional[datetime]) -> str:
    """Render a datetime as ``YYYY DOY HH MM SS`` (open → 9999 999 sentinel)."""
    if dt is None:
        return "9999 999 00 00 00"
    return dt.strftime("%Y %j %H %M %S")


def format_occupation(occ: Occupation) -> str:
    """Render an :class:`Occupation` back to a station.info data line.

    Inverse of :func:`parse_line`: ``parse_line(format_occupation(occ)) == occ``.
    Used for round-trip verification of a TOS-derived station.info against its
    source. Horizontal offsets round-trip now that Occupation carries them —
    they were previously emitted as a hardcoded ``0.0000``, which silently
    discarded a surveyed measurement.
    """
    fields = {
        "marker": occ.marker,
        "station_name": occ.station_name,
        "session_start": _fmt_gamit_datetime(occ.time_from),
        "session_stop": _fmt_gamit_datetime(occ.time_to),
        "ant_height": occ.antenna_height or "0.0000",
        "htcod": occ.htcod or "DHARP",
        "ant_n": occ.antenna_north or "0.0000",
        "ant_e": occ.antenna_east or "0.0000",
        "receiver_type": occ.receiver_type,
        "vers": occ.vers,
        "swver": occ.swver,
        "receiver_sn": occ.receiver_sn,
        "antenna_type": occ.antenna_type,
        "dome": occ.dome,
        "antenna_sn": occ.antenna_sn,
    }
    width = max(start + len(fields[name]) for name, start in _WRITE_COLUMNS)
    buf = [" "] * width
    for name, start in _WRITE_COLUMNS:
        for i, ch in enumerate(fields[name]):
            buf[start + i] = ch
    return "".join(buf).rstrip()


def parse_station_info(
    source: Union[str, Path, Iterable[str]],
    marker: Optional[str] = None,
    *,
    strict: bool = False,
) -> List[Occupation]:
    """Parse a station.info file (path or line iterable) into occupations.

    Real GAMIT station.info files contain occasional malformed lines (mashed
    day-of-year fields, out-of-range hours, legacy alignment). By default such
    lines are logged and skipped so one bad row elsewhere in the file doesn't
    abort an import targeting a healthy marker. Pass ``strict=True`` to re-raise.

    Args:
        source: Path to a station.info file, or an iterable of its lines.
        marker: When given, return only this 4-char marker's occupations
            (case-insensitive). Otherwise return every occupation in the file.
        strict: Re-raise on a malformed line instead of skipping it.

    Returns:
        Occupations in file order. ``time_from`` is never ``None`` for a valid
        line; an open-ended session has ``time_to is None``.
    """
    if isinstance(source, (str, Path)):
        with open(source, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    else:
        lines = list(source)

    want = marker.upper() if marker else None
    out: List[Occupation] = []
    skipped = 0
    for lineno, line in enumerate(lines, 1):
        try:
            occ = parse_line(line)
        except ValueError as exc:
            if strict:
                raise
            skipped += 1
            logger.warning("station.info line %d skipped: %s", lineno, exc)
            continue
        if occ is None:
            continue
        if want is not None and occ.marker.upper() != want:
            continue
        out.append(occ)
    if skipped:
        logger.info("parse_station_info: skipped %d malformed line(s)", skipped)
    return out
