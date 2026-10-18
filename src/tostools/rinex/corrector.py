"""
RINEX header correction using TOS metadata or station config.

This module provides high-level functions for correcting RINEX headers:
- For recent dates: Uses station.cfg (fast, no network dependency)
- For historical dates: Queries TOS database

Architecture:
    Daily operations use station.cfg to avoid constantly querying TOS.
    Historical reprocessing queries TOS when config_valid_from is missing
    or observation date is before the config validity period.
"""

import gzip
import logging
import math
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..gps_metadata_qc import URL_REST_TOS, gps_metadata
from ..utils.logging import get_logger
from .domes import domes_or_skip
from .reader import get_rinex_labels, read_rinex_header

# Sentinel correction value meaning "remove this header line if present, and do
# not insert one". Used for MARKER NUMBER when the station has no IERS DOMES: the
# policy is to strip the field, not fall back to the 4-char station id.
STRIP_LINE = None


def _is_placeholder_serial(serial: str) -> bool:
    """Check if a serial number is a TOS placeholder/assigned value.

    TOS assigns placeholder serial numbers when the actual serial is unknown.
    These typically follow patterns like:
    - "antenna-{station}-{date}" (e.g., "antenna-thob-20200128")
    - "receiver-{station}-{date}"
    - "Unknown"

    Args:
        serial: Serial number string to check

    Returns:
        True if this appears to be a placeholder, False if it's a real serial
    """
    if not serial:
        return True

    serial_lower = serial.lower().strip()

    # Check for common placeholder patterns
    if serial_lower == "unknown":
        return True
    if serial_lower.startswith("antenna-"):
        return True
    if serial_lower.startswith("receiver-"):
        return True
    if serial_lower.startswith("radome-"):
        return True

    return False


#: Largest APPROX POSITION rewrite this corrector will ever apply, in metres.
#: Mirrors ``validator._MAX_POSITION_CORRECTION_M`` — the two paths must agree,
#: or one will rewrite what the other refuses.
MAX_POSITION_CORRECTION_M = 1000.0


def _parse_xyz_triplet(value):
    """Parse a RINEX ``APPROX POSITION XYZ`` field → ``(x, y, z)`` or None."""
    try:
        parts = [float(v) for v in str(value or "").split()[:3]]
    except (TypeError, ValueError):
        return None
    return tuple(parts) if len(parts) == 3 else None


def correct_rinex_from_tos(
    rinex_file: Path,
    station_id: str,
    observation_date: Optional[datetime] = None,
    output_file: Optional[Path] = None,
    station_config: Optional[Dict[str, Any]] = None,
    loglevel: int = logging.INFO,
    only_fields: Optional[set] = None,
    extra_corrections: Optional[Dict[str, Any]] = None,
    tos_metadata_cache: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """
    Correct RINEX header using TOS metadata or station config.

    Logic:
    1. If station_config provided with valid config_valid_from:
       - If observation_date >= config_valid_from: use station_config
       - Otherwise: query TOS
    2. If station_config not provided or no config_valid_from: query TOS
    3. Apply corrections using fortranformat for proper RINEX formatting
    4. Write corrected file

    Args:
        rinex_file: Path to RINEX file to correct
        station_id: 4-character station identifier
        observation_date: Date of observation (auto-detected from file if None)
        output_file: Output path (default: overwrite in place)
        station_config: Station configuration dict from gps_parser.
                       Should contain 'rinex' section with config_valid_from.
        loglevel: Logging level
        only_fields: Optional set of RINEX header labels (e.g. ``{"ANTENNA: DELTA H/E/N"}``)
                     to restrict the rewrite to. When None (default) all known fields
                     are corrected. When set, only corrections whose label is in the
                     set are applied — used by ``receivers rinex --fix-headers`` to
                     touch only the fields that actually differ from TOS.

    Returns:
        Path to corrected file, or None if correction failed

    Example:
        >>> from tostools.rinex.corrector import correct_rinex_from_tos
        >>> # With station config (daily operations - no TOS query)
        >>> correct_rinex_from_tos(
        ...     Path("THOB0160.26o"),
        ...     "THOB",
        ...     station_config=station_cfg,
        ... )
        >>> # Without config (historical - queries TOS)
        >>> correct_rinex_from_tos(Path("THOB0010.20o"), "THOB")
    """
    logger = get_logger(__name__, loglevel)
    rinex_file = Path(rinex_file)

    if not rinex_file.exists():
        logger.error(f"RINEX file not found: {rinex_file}")
        return None

    # Auto-detect observation date from filename if not provided
    if observation_date is None:
        observation_date = _extract_date_from_rinex(rinex_file)
        if observation_date is None:
            logger.warning(f"Could not extract date from {rinex_file.name}")

    corrections = resolve_corrections(
        rinex_file,
        station_id,
        observation_date,
        station_config=station_config,
        loglevel=loglevel,
        only_fields=only_fields,
        extra_corrections=extra_corrections,
        tos_metadata_cache=tos_metadata_cache,
    )
    if not corrections:
        return rinex_file  # Return original file unchanged

    # Apply corrections
    corrected_file = _apply_corrections(rinex_file, corrections, output_file, logger)

    return corrected_file


#: Header label whose value TOS owns outright, on both the config and TOS paths.
_ANTENNA_DELTA = "ANTENNA: DELTA H/E/N"


def _overlay_tos_antenna_delta(
    corrections: Dict[str, Any],
    station_id: str,
    observation_date: Optional[datetime],
    loglevel: int,
    logger: logging.Logger,
    tos_metadata_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Re-source ``ANTENNA: DELTA H/E/N`` from TOS on the ``stations.cfg`` path.

    stations.cfg is structurally incapable of producing this field. It carries a
    single ``antenna_height`` composite and NO eccentricity at all, so the config
    branch hardcoded ``[height, 0.0, 0.0]`` — correct only for the stations whose
    true eccentricity happens to be zero. TOS splits the mark→ARP vector across a
    monument and an antenna entity and is the only source that holds all three
    axes, which is why the IGS site log reads TOS and why a config-path header
    silently disagreed with the site log it is supposed to match.

    Ownership, stated once: config owns the descriptive fields (marker, observer,
    agency); TOS owns device geometry. Only the geometry field moves here — the
    antenna and receiver IDENTITY fields keep their config-branch guards (notably
    the placeholder-serial guard) rather than being re-routed in the same change.

    On a TOS miss the config value is LEFT IN PLACE with a warning. The config
    path exists so a daily conversion does not hard-depend on the network; making
    it abort on a TOS blip would stop production for the whole fleet to avoid an
    error the file already has today. Degraded, never worse than the status quo.
    """
    try:
        tos_corrections = _get_corrections_from_tos(
            station_id, observation_date, loglevel, tos_metadata_cache
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see docstring
        # tostools' station lookup does sys.exit(1) on a connection error, which
        # is a SystemExit and would otherwise sail past `except Exception` and
        # kill the run. The config path must survive an unreachable TOS.
        logger.warning(
            "%s: TOS unreachable while re-sourcing %s (%s) — keeping the "
            "stations.cfg value, which cannot carry an eccentricity",
            station_id,
            _ANTENNA_DELTA,
            exc,
        )
        return corrections

    delta = tos_corrections.get(_ANTENNA_DELTA) if tos_corrections else None
    if delta is None:
        logger.warning(
            "%s: TOS returned no %s for %s — keeping the stations.cfg value",
            station_id,
            _ANTENNA_DELTA,
            observation_date.date() if observation_date else "unknown date",
        )
        return corrections

    previous = corrections.get(_ANTENNA_DELTA)
    if previous is not None and list(previous) != list(delta):
        logger.info(
            "%s: %s re-sourced from TOS: %s -> %s",
            station_id,
            _ANTENNA_DELTA,
            previous,
            delta,
        )
    corrections[_ANTENNA_DELTA] = delta
    return corrections


def resolve_corrections(
    rinex_file: Path,
    station_id: str,
    observation_date: Optional[datetime] = None,
    *,
    station_config: Optional[Dict[str, Any]] = None,
    loglevel: int = logging.INFO,
    only_fields: Optional[set] = None,
    extra_corrections: Optional[Dict[str, Any]] = None,
    tos_metadata_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide WHICH header fields change and to WHAT value — nothing is written.

    This is the single source of truth for the correction values, extracted so a
    dry-run preview and the real write cannot diverge. They did: ``--fix-headers``
    previewed the value :func:`compare_rinex_to_tos` proposed while the write used
    the value built here, and for a TOS placeholder antenna serial the two differ
    (blank vs ``0000``). A preview that does not match the action is worthless on
    the one run where it matters — the review before rewriting archived files.

    Returns the label→values mapping :func:`_apply_corrections` would apply, after
    the config/TOS source choice, the ``extra_corrections`` merge, the
    ``only_fields`` filter and the position guard. Empty means "nothing to write".
    A value of :data:`STRIP_LINE` means the label's line is REMOVED.
    """
    logger = get_logger(__name__, loglevel)
    rinex_file = Path(rinex_file)

    # Determine whether to use config or TOS
    use_config = False
    if station_config:
        rinex_cfg = station_config.get("rinex", {})
        valid_from_str = rinex_cfg.get("config_valid_from", "")
        if valid_from_str:
            try:
                config_valid_from = datetime.strptime(valid_from_str, "%Y-%m-%d")
                if (
                    observation_date
                    and observation_date.date() >= config_valid_from.date()
                ):
                    use_config = True
                    logger.debug(
                        f"Using station.cfg for {station_id} "
                        f"(date {observation_date.date()} >= {config_valid_from.date()})"
                    )
            except ValueError:
                logger.warning(f"Invalid config_valid_from format: {valid_from_str}")

    # Get metadata from appropriate source
    if use_config and station_config:
        corrections = _get_corrections_from_config(station_id, station_config, logger)
        # stations.cfg cannot express an antenna eccentricity; TOS owns the
        # geometry on every path. See _overlay_tos_antenna_delta.
        corrections = _overlay_tos_antenna_delta(
            corrections,
            station_id,
            observation_date,
            loglevel,
            logger,
            tos_metadata_cache=tos_metadata_cache,
        )
    else:
        logger.debug(f"Querying TOS for {station_id} metadata")
        corrections = _get_corrections_from_tos(
            station_id,
            observation_date,
            loglevel,
            tos_metadata_cache=tos_metadata_cache,
        )

    # Caller-supplied corrections (e.g. the receivers-resolved OBSERVER / AGENCY,
    # which needs agencies.yaml the corrector can't reach). Merged over the TOS/
    # config corrections, before the only_fields filter, so a field the corrector
    # cannot build itself can still be written in the same single pass.
    if extra_corrections:
        corrections = {**corrections, **extra_corrections}

    if not corrections:
        logger.warning(f"No corrections available for {station_id}")
        return {}

    # Field-selective mode: keep only the requested labels (used by
    # `receivers rinex --fix-headers` to touch only discrepant fields).
    if only_fields is not None:
        corrections = {
            label: values
            for label, values in corrections.items()
            if label in only_fields
        }
        if not corrections:
            logger.debug(
                "No corrections after only_fields filter for %s — leaving file unchanged",
                station_id,
            )
            return {}

    # Refuse a km-scale APPROX POSITION rewrite. An a-priori position is
    # legitimately out by metres — a hand-entered header, a stale seed, the
    # receiver's own solution. Out by KILOMETRES means the file was recorded
    # somewhere else, and overwriting it launders foreign data into looking
    # authentic under this station's marker, coordinates and DOMES.
    #
    # This is the path that did it: ISAK's receiver was taken off the mark for a
    # campaign survey in Aug 2016 and set-header moved 14 days of it up to 245 km
    # onto ISAK. The EPOS QC gate then compared the REWRITTEN header against TOS,
    # matched, and published every one. Guarded here rather than in the two
    # correction builders so the config path is covered as well.
    return _guard_position_correction(rinex_file, corrections, logger, loglevel)


def render_correction(label: str, values: Any) -> Optional[str]:
    """The 60-char header field the write will emit for ``label``.

    ``None`` means the line is REMOVED — either the :data:`STRIP_LINE` sentinel
    (e.g. a MARKER NUMBER with no real DOMES) or a label this formatter cannot
    render. Callers previewing a change must distinguish "removed" from "written
    as empty"; they are different edits.
    """
    if values is STRIP_LINE:
        return None
    if not isinstance(values, (list, tuple)):
        values = [values]
    return _format_rinex_data(label, list(values))


def _guard_position_correction(rinex_file, corrections, logger, loglevel):
    """Drop an APPROX POSITION correction that moves the file kilometres.

    Returns ``corrections`` unchanged when there is nothing to guard. Never
    raises: a header it cannot read simply isn't second-guessed.
    """
    proposed = corrections.get("APPROX POSITION XYZ")
    if not proposed:
        return corrections
    try:
        header = read_rinex_header(rinex_file, loglevel=loglevel)
        from .reader import extract_header_info

        info = extract_header_info(header, loglevel=loglevel) if header else {}
        current = _parse_xyz_triplet((info or {}).get("APPROX POSITION XYZ"))
        target = _parse_xyz_triplet(" ".join(str(v) for v in proposed))
    except Exception as exc:  # noqa: BLE001 - guard must never break the fix
        logger.debug("position guard could not read %s: %s", rinex_file, exc)
        return corrections
    if current is None or target is None:
        return corrections
    # (0,0,0) is the "position never filled" placeholder — the converter wrote
    # the origin because the raw carried no position metadata. That is a MISSING
    # position, not "recorded elsewhere": allow the TOS fill rather than refuse.
    # (Trimble raw for 2010-2012 at FIM2 hit exactly this: 260 files with a
    # zero position, ~6362 km from the station, that the guard was refusing.)
    if math.dist(current, (0.0, 0.0, 0.0)) < 1.0:
        return corrections
    distance = math.dist(current, target)
    if distance <= MAX_POSITION_CORRECTION_M:
        return corrections
    logger.error(
        "REFUSING to rewrite APPROX POSITION for %s: the header is %.0f m from "
        "the TOS position. That is not a header error — the file was recorded "
        "elsewhere. Investigate; do not rewrite.",
        Path(rinex_file).name,
        distance,
    )
    return {k: v for k, v in corrections.items() if k != "APPROX POSITION XYZ"}


def _get_corrections_from_config(
    station_id: str,
    station_config: Dict[str, Any],
    logger: logging.Logger,
) -> Dict[str, Any]:
    """Extract RINEX corrections from station.cfg configuration."""
    corrections = {}

    rinex_cfg = station_config.get("rinex", {})
    antenna_cfg = station_config.get("antenna", {})

    # MARKER NAME
    marker_name = rinex_cfg.get("marker_name", station_id.upper())
    if marker_name:
        corrections["MARKER NAME"] = [marker_name]

    # MARKER NUMBER ← IERS DOMES only. cfg's rinex_marker_number holds a real
    # DOMES for registered stations and the 4-char id for the rest; the guard
    # keeps only a real DOMES and strips the line otherwise (no id fallback).
    marker_number = domes_or_skip(rinex_cfg.get("marker_number", ""))
    if marker_number:
        corrections["MARKER NUMBER"] = [marker_number]
    else:
        corrections["MARKER NUMBER"] = STRIP_LINE

    # OBSERVER / AGENCY
    observer = rinex_cfg.get("observer", "")
    agency = rinex_cfg.get("agency", "")
    if observer or agency:
        corrections["OBSERVER / AGENCY"] = [observer or "", agency or ""]

    # ANT # / TYPE (serial + type with radome)
    ant_serial = antenna_cfg.get("serial", "")
    ant_type = antenna_cfg.get("type", "")
    ant_radome = antenna_cfg.get("radome", "NONE")
    # Same guard the TOS branch already applies. This branch wrote whatever
    # stations.cfg held, and `cfg sync-from-tos` mirrors antenna_serial FROM
    # TOS — so TOS's synthetic placeholders reached cfg and then the header.
    # 26 stations carry one today. At 21 chars `antenna-thob-20200128` does not
    # fit the A20 field: it truncated to `antenna-thob-2020012`, filling the
    # column exactly so the serial abuts the antenna type with no separator.
    # Measured 2026-08-19 — 19 stations had a malformed ANT # / TYPE in their
    # most recent daily file, and a fresh conversion on rek-d01 still produced
    # one, because the earlier fixes guarded the metadata_provider and
    # validator paths and never this one.
    had_serial = bool(ant_serial)
    if _is_placeholder_serial(ant_serial):
        ant_serial = "0000"
    # Emit on serial OR type, and never on neither. Gating on the serial alone
    # would drop the antenna TYPE and RADOME along with a suppressed serial —
    # a worse header than a blank serial field (metadata_provider makes the
    # same call). Gating on nothing would stamp a bare "0000" onto a station
    # with no antenna configured at all.
    if had_serial or ant_type:
        # Format: serial (20) + type with radome (20)
        ant_type_full = f"{ant_type:<15} {ant_radome:<4}" if ant_type else ""
        corrections["ANT # / TYPE"] = [ant_serial, ant_type_full]

    # ANTENNA: DELTA H/E/N — PLACEHOLDER ONLY, overwritten from TOS.
    #
    # stations.cfg has no eccentricity fields, so this branch can only ever
    # produce a zeroed E/N. resolve_corrections() re-sources the whole triple
    # from TOS via _overlay_tos_antenna_delta(); the value built here survives
    # solely as the fallback for an unreachable TOS. Do NOT "fix" the zeros by
    # inventing cfg keys — the mark->ARP vector is split across the TOS monument
    # and antenna entities and a single cfg number cannot represent it.
    ant_height = float(antenna_cfg.get("height", 0) or 0)
    corrections["ANTENNA: DELTA H/E/N"] = [ant_height, 0.0, 0.0]

    logger.debug(f"Corrections from config: {list(corrections.keys())}")
    return corrections


def _get_corrections_from_tos(
    station_id: str,
    observation_date: Optional[datetime],
    loglevel: int,
    tos_metadata_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Get RINEX corrections from TOS database.

    TOS returns station data with device_history containing sessions:
    - time_from, time_to: Session time period
    - gnss_receiver: {model, serial_number, firmware_version}
    - antenna: {model, serial_number, antenna_height}
    - radome: {model}
    - monument: {monument_height}

    ``tos_metadata_cache`` (optional, caller-owned) memoizes the date-independent
    ``gps_metadata(station_id)`` fetch across many dates: a batch that corrects
    every day of a station (re-rinex, fleet sweeps) makes 1 TOS call per station
    instead of 1 per file. Only a non-empty result is cached — an empty/miss
    re-fetches next time (never freeze a soft miss for the whole run).
    """
    logger = get_logger(__name__, loglevel)

    try:
        sid = station_id.upper()
        # Cache holds the whole station payload (device_history + station meta),
        # which does not depend on observation_date — the session is resolved
        # locally below. Cache the fetch, not the per-date resolution.
        if tos_metadata_cache is not None and sid in tos_metadata_cache:
            station_data = tos_metadata_cache[sid]
        else:
            station_data = gps_metadata(station_id, URL_REST_TOS, loglevel=loglevel)
            if station_data and tos_metadata_cache is not None:
                tos_metadata_cache[sid] = station_data

        if not station_data:
            logger.warning(f"No TOS data found for {station_id}")
            return {}

        # Get device history (list of sessions)
        device_history = station_data.get("device_history", [])
        if not device_history:
            logger.warning(f"No device history found in TOS for {station_id}")
            return {}

        # Find session covering observation date
        session = None
        for s in device_history:
            time_from = s.get("time_from")
            time_to = s.get("time_to")

            # Handle datetime objects or strings
            if isinstance(time_from, str):
                try:
                    time_from = datetime.fromisoformat(time_from.replace("Z", "+00:00"))
                except ValueError:
                    time_from = None
            if isinstance(time_to, str) and time_to:
                try:
                    time_to = datetime.fromisoformat(time_to.replace("Z", "+00:00"))
                except ValueError:
                    time_to = None

            # TOS sessions are half-open: time_from <= observation_date < time_to.
            if observation_date:
                obs_date = (
                    observation_date.replace(tzinfo=None)
                    if observation_date.tzinfo
                    else observation_date
                )
                if time_from:
                    tf = (
                        time_from.replace(tzinfo=None)
                        if hasattr(time_from, "tzinfo") and time_from.tzinfo
                        else time_from
                    )
                    if obs_date < tf:
                        continue
                if time_to:
                    tt = (
                        time_to.replace(tzinfo=None)
                        if hasattr(time_to, "tzinfo") and time_to.tzinfo
                        else time_to
                    )
                    if obs_date >= tt:
                        continue
            session = s
            break

        if session is None and device_history:
            # Use most recent session as fallback
            session = device_history[-1]
            logger.debug(f"Using most recent TOS session for {station_id}")

        if session is None:
            return {}

        logger.info(
            f"Found TOS session for {station_id}: {session.get('time_from')} - {session.get('time_to')}"
        )

        # Extract corrections from TOS session
        # TOS uses keys like 'gnss_receiver', 'antenna', 'radome', 'monument'
        corrections = {}

        # MARKER NAME
        corrections["MARKER NAME"] = [station_id.upper()]

        # MARKER NUMBER ← IERS DOMES only. When the station has no DOMES the line
        # is stripped, never filled with the 4-char marker/id (MARKER NAME already
        # carries that). Matches the domes check in compare_rinex_to_tos, so a
        # --fix-headers run removes a stale id instead of re-injecting one.
        domes = domes_or_skip(station_data.get("iers_domes_number"))
        if domes:
            corrections["MARKER NUMBER"] = [domes]
        else:
            corrections["MARKER NUMBER"] = STRIP_LINE

        # REC # / TYPE / VERS - from gnss_receiver
        receiver = session.get("gnss_receiver", {})
        if receiver:
            corrections["REC # / TYPE / VERS"] = [
                receiver.get("serial_number", ""),
                receiver.get("model", ""),
                receiver.get("firmware_version", ""),
            ]

        # ANT # / TYPE
        antenna = session.get("antenna", {})
        radome = session.get("radome", {})
        if antenna:
            ant_model = antenna.get("model", "")
            radome_model = radome.get("model", "NONE") if radome else "NONE"
            ant_type_full = f"{ant_model:<15} {radome_model:<4}" if ant_model else ""
            # Use "0000" if serial is a TOS placeholder
            ant_serial = antenna.get("serial_number", "")
            if _is_placeholder_serial(ant_serial):
                ant_serial = "0000"
            corrections["ANT # / TYPE"] = [ant_serial, ant_type_full]

        # ANTENNA: DELTA H/E/N — a composite on ALL THREE axes.
        #
        # TOS splits the mark→ARP vector across two entities: the monument
        # (mark → monument top) and the antenna (monument top → ARP). The
        # published value is their sum. This function always summed the UP
        # axis and, until 2026-08-31, took EAST/NORTH from the antenna entity
        # alone — so any station whose offset lives on the MONUMENT record
        # published a zeroed eccentricity while its IGS site log, which does
        # sum all three (legacy/gps_metadata_functions.py:196-204), published
        # the real one. ISAK is the canary: antenna E/N are 0.0/0.0, monument
        # E/N are 0.0001/0.0002, header read 0.0000 against a site log of
        # 0.0002. Fleet-wide the split is genuinely used, so read both.
        monument = session.get("monument", {})
        ant_height = float(antenna.get("antenna_height", 0) or 0) if antenna else 0.0
        mon_height = float(monument.get("monument_height", 0) or 0) if monument else 0.0
        ant_east = float(antenna.get("antenna_offset_east", 0) or 0) if antenna else 0.0
        ant_north = (
            float(antenna.get("antenna_offset_north", 0) or 0) if antenna else 0.0
        )
        mon_east = (
            float(monument.get("monument_offset_east", 0) or 0) if monument else 0.0
        )
        mon_north = (
            float(monument.get("monument_offset_north", 0) or 0) if monument else 0.0
        )
        corrections["ANTENNA: DELTA H/E/N"] = [
            ant_height + mon_height,
            ant_east + mon_east,
            ant_north + mon_north,
        ]

        # APPROX POSITION XYZ ← TOS surveyed lat/lon/altitude → ECEF.
        lat = station_data.get("lat")
        lon = station_data.get("lon")
        alt = station_data.get("altitude")
        if lat is not None and lon is not None and alt is not None:
            try:
                from ..gps_metadata_qc import wgs84toitrf08

                x, y, z = wgs84toitrf08.transform(float(lat), float(lon), float(alt))
                corrections["APPROX POSITION XYZ"] = [x, y, z]
            except Exception as exc:  # noqa: BLE001 - transform optional
                logger.debug("APPROX POSITION XYZ transform failed: %s", exc)

        logger.debug(f"Corrections from TOS: {list(corrections.keys())}")
        return corrections

    except Exception as e:
        logger.error(f"TOS query failed for {station_id}: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        return {}


def _format_rinex_data(label: str, values: list) -> Optional[str]:
    """Format RINEX header data fields with correct column widths.

    RINEX uses fixed-width format with left-justified strings.
    This function returns the 60-character data portion of a header line.

    Args:
        label: RINEX header label (e.g., "MARKER NAME")
        values: List of values for this field

    Returns:
        60-character formatted string, or None if formatting fails
    """
    if label == "MARKER NAME":
        # A60: Single 60-char field, left-justified
        v = str(values[0]) if values else ""
        return v.upper().ljust(60)[:60]

    elif label == "MARKER NUMBER":
        # A20 + 40X: 20-char field + 40 spaces
        v = str(values[0]) if values else ""
        return v.ljust(20)[:20] + " " * 40

    elif label == "OBSERVER / AGENCY":
        # A20 + A20 + 20X: observer(20) + agency(20) + 20 spaces
        obs = str(values[0]).ljust(20)[:20] if len(values) > 0 else " " * 20
        agency = str(values[1]).ljust(40)[:40] if len(values) > 1 else " " * 40
        return f"{obs}{agency}"

    elif label == "REC # / TYPE / VERS":
        # A20 + A20 + A20: serial(20) + type(20) + version(20)
        serial = str(values[0]).ljust(20)[:20] if len(values) > 0 else " " * 20
        rtype = str(values[1]).ljust(20)[:20] if len(values) > 1 else " " * 20
        vers = str(values[2]).ljust(20)[:20] if len(values) > 2 else " " * 20
        return f"{serial}{rtype}{vers}"

    elif label == "ANT # / TYPE":
        # A20 + A20 + 20X: serial(20) + type(20) + 20 spaces
        serial = str(values[0]).ljust(20)[:20] if len(values) > 0 else " " * 20
        atype = str(values[1]).ljust(20)[:20] if len(values) > 1 else " " * 20
        return f"{serial}{atype}" + " " * 20

    elif label == "ANTENNA: DELTA H/E/N":
        # 3F14.4 + 18X: three 14-char floats + 18 spaces
        h = float(values[0]) if len(values) > 0 else 0.0
        e = float(values[1]) if len(values) > 1 else 0.0
        n = float(values[2]) if len(values) > 2 else 0.0
        return f"{h:14.4f}{e:14.4f}{n:14.4f}" + " " * 18

    elif label == "APPROX POSITION XYZ":
        # 3F14.4 + 18X: three 14-char floats + 18 spaces
        x = float(values[0]) if len(values) > 0 else 0.0
        y = float(values[1]) if len(values) > 1 else 0.0
        z = float(values[2]) if len(values) > 2 else 0.0
        return f"{x:14.4f}{y:14.4f}{z:14.4f}" + " " * 18

    else:
        # Unknown field - try to format as string
        v = str(values[0]) if values else ""
        return v.ljust(60)[:60]


def _insert_header_record(
    header_content: str,
    label: str,
    new_line: str,
    logger: logging.Logger,
) -> str:
    """Insert a header record that is absent from the file.

    Positional insert that preserves the existing line endings (string splice,
    not splitlines/join): ``MARKER NUMBER`` goes immediately after the
    ``MARKER NAME`` line (RINEX convention); any other label goes just before
    ``END OF HEADER``. Returns the content unchanged if no anchor is found —
    a malformed header is never corrupted further.
    """
    if label == "MARKER NUMBER":
        anchor = re.compile(r"^.*MARKER NAME.*$", re.M)
        m = anchor.search(header_content)
        if m:
            # After the anchor line's content, before its newline.
            pos = m.end()
            return header_content[:pos] + "\n" + new_line + header_content[pos:]

    eoh = re.compile(r"^.*END OF HEADER.*$", re.M)
    m = eoh.search(header_content)
    if m:
        # Before the END OF HEADER line.
        pos = m.start()
        return header_content[:pos] + new_line + "\n" + header_content[pos:]

    logger.debug("No insert anchor for %s; header left unchanged", label)
    return header_content


def _apply_corrections(
    rinex_file: Path,
    corrections: Dict[str, Any],
    output_file: Optional[Path],
    logger: logging.Logger,
) -> Path:
    """Apply corrections to RINEX file header."""
    # Read the RINEX file
    rheader = read_rinex_header(rinex_file)
    if not rheader or "header" not in rheader:
        logger.error(f"Failed to read RINEX header from {rinex_file}")
        return rinex_file

    header_content: str = str(rheader["header"])
    rinex_labels, _ = get_rinex_labels()

    # Apply each correction
    for label, values in corrections.items():
        if label not in rinex_labels:
            logger.debug(f"Unknown label {label}, skipping")
            continue

        # STRIP_LINE sentinel: remove the label's line if present, insert nothing.
        # Used for MARKER NUMBER on a station with no IERS DOMES (skip the field).
        if values is STRIP_LINE:
            strip_re = re.compile(rf"^.*{re.escape(label)}.*$\n?", re.M)
            if strip_re.search(header_content):
                header_content = strip_re.sub("", header_content)
                logger.debug(f"Stripped {label} line (no value under policy)")
            continue

        # Create the corrected line using proper RINEX formatting
        try:
            # Format data part based on field type (columns 1-60)
            data_part = _format_rinex_data(label, values)
            if data_part is None:
                logger.debug(f"Could not format {label}, skipping")
                continue

            # RINEX format: 60 chars data + 20 chars label
            new_line = f"{data_part}{label}"

            # Replace the existing record, or INSERT it when the label line is
            # absent. Insert matters for MARKER NUMBER: pre-DOMES-era headers
            # (e.g. RHOF 2000-2011) carry no MARKER NUMBER line at all, so a
            # replace-only corrector would silently no-op and never write the
            # DOMES. RINEX places MARKER NUMBER right after MARKER NAME; any
            # other absent label goes just before END OF HEADER.
            pattern = rf"(^.*{re.escape(label)}.*$)"
            mstring = re.compile(pattern, re.M)
            if mstring.search(header_content):
                header_content = re.sub(mstring, new_line, header_content)
                logger.debug(f"Applied correction to {label}")
            else:
                header_content = _insert_header_record(
                    header_content, label, new_line, logger
                )
        except Exception as e:
            logger.warning(f"Failed to apply correction for {label}: {e}")

    # Determine output path
    if output_file is None:
        output_file = rinex_file

    # Write corrected file
    _write_rinex_file(rinex_file, header_content, output_file, logger)

    return output_file


def _write_rinex_file(
    original_file: Path,
    new_header: str,
    output_file: Path,
    logger: logging.Logger,
) -> None:
    """Write RINEX file with corrected header.

    The CRINEX data section (after END OF HEADER) is binary for Hatanaka-
    compressed files; decoding it as text corrupts it. So we work with BYTES
    for reading and the header/data split, decode only the header portion,
    replace it, and re-encode — keeping the data section bit-identical."""
    import subprocess

    # Determine original format
    is_gzipped = original_file.suffix == ".gz"
    is_z_compressed = original_file.suffix == ".Z"

    # Read the FULL content as BYTES (never decode the data section for
    # CRINEX/Hatanaka files where it may be binary).
    if is_gzipped:
        with gzip.open(original_file, "rb") as f:
            content_bytes = f.read()
    elif is_z_compressed:
        proc = subprocess.run(
            ["zcat", str(original_file)], capture_output=True, check=True
        )
        content_bytes = proc.stdout
    else:
        content_bytes = original_file.read_bytes()

    # Find END OF HEADER as bytes, split header bytes from data bytes.
    end_marker_b = b"END OF HEADER"
    idx = content_bytes.find(end_marker_b)
    if idx >= 0:
        # header_bytes = everything up to and including END OF HEADER + trailing \n
        eoh_end = idx + len(end_marker_b)
        nl = content_bytes.find(b"\n", eoh_end)
        header_end = nl + 1 if nl >= 0 else eoh_end
        data_bytes = content_bytes[header_end:]
        # ``new_header`` (from read_rinex_header) ends at "END OF HEADER" with NO
        # trailing newline, while ``data_bytes`` starts at the first epoch line.
        # Concatenating as-is glues them ("END OF HEADER> 2026 ..."), which breaks
        # the RINEX3 record structure and makes downstream RNX2CRX fail ("ERROR
        # when reading line N"). Guarantee exactly one newline at the boundary.
        header_str = new_header if new_header.endswith("\n") else new_header + "\n"
        new_content_bytes = header_str.encode("utf-8") + data_bytes
    else:
        logger.warning("END OF HEADER not found (bytes), replacing entire content")
        new_content_bytes = new_header.encode("utf-8")

    # Write output — re-compress in the SAME format as the original so the
    # corrected file is byte-compatible with the archive convention.
    temp_file = output_file.with_suffix(".tmp")
    try:
        if output_file.suffix == ".gz":
            with gzip.open(temp_file, "wb") as f:
                f.write(new_content_bytes)
        elif output_file.suffix == ".Z":
            # Unix-compress (LZW). Write plain bytes to a temp file, run
            # compress.  ``compress foo.plain`` produces ``foo.plain.Z`` —
            # NOT a simple suffix-replacement of .plain with .Z — so read the
            # actual compress output filename rather than assuming.
            import subprocess

            plain = temp_file.with_name(temp_file.name + ".plain")
            plain.write_bytes(new_content_bytes)
            subprocess.run(
                ["compress", "-f", str(plain)],
                check=True,
                capture_output=True,
            )
            compressed = plain.with_name(plain.name + ".Z")
            temp_file.write_bytes(compressed.read_bytes())
            compressed.unlink(missing_ok=True)
            plain.unlink(missing_ok=True)
        else:
            temp_file.write_bytes(new_content_bytes)

        # Atomic replace
        shutil.move(temp_file, output_file)
        logger.info(f"Wrote corrected RINEX to {output_file}")

    except Exception as e:
        logger.error(f"Failed to write {output_file}: {e}")
        if temp_file.exists():
            temp_file.unlink()
        raise


def _extract_date_from_rinex(rinex_file: Path) -> Optional[datetime]:
    """Extract observation date from RINEX filename."""
    from gtimes.timefunc import datefRinex

    try:
        # Remove compression extensions
        name = rinex_file.name
        for ext in [".gz", ".Z"]:
            if name.endswith(ext):
                name = name[: -len(ext)]

        dates = datefRinex([name])
        if dates and dates[0]:
            return dates[0]
    except Exception:
        pass

    return None
