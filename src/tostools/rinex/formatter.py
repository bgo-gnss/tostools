"""RINEX header field formatting (fixed-width Fortran layout).

Pure string-formatting helpers for writing RINEX 3.x headers. Each field
has a defined width and column layout per the RINEX spec; callers hand in
Python values (strings, tuples, numbers) and get back correctly-padded
header lines.

This module is the "write" side of the RINEX header story; ``reader.py``
and ``corrector.py`` handle the "read" and "correct against TOS" sides.

Originally lived in ``receivers.rinex.metadata_provider`` — moved here so
the formatting can be shared with any RINEX-producing tool in the
ecosystem, not just receivers.
"""

from typing import Any, Dict, Optional, Tuple

# RINEX header field specifications: (Fortran format, total width).
# Based on the RINEX 3.x spec and tostools.rinex.reader column layouts.
RINEX_FIELD_SPECS: Dict[str, Tuple[str, int]] = {
    "MARKER NAME": ("A60", 60),
    "MARKER NUMBER": ("A20", 20),
    "OBSERVER / AGENCY": ("A20,A40", 60),
    "REC # / TYPE / VERS": ("A20,A20,A20", 60),
    "ANT # / TYPE": ("A20,A20", 40),
    "APPROX POSITION XYZ": ("3F14.4", 42),
    "ANTENNA: DELTA H/E/N": ("3F14.4", 42),
    "INTERVAL": ("F10.3", 10),
}


def format_rinex_field(field_name: str, value: Any) -> Optional[str]:
    """Format a value for a specific RINEX header field.

    Dispatches on ``field_name`` to produce fixed-width Fortran output per
    :data:`RINEX_FIELD_SPECS`. Returns ``None`` when the value is empty or
    ``None`` (so the caller can skip emitting the line entirely).

    Args:
        field_name: RINEX field label, e.g. ``"MARKER NAME"``, ``"ANT # / TYPE"``.
        value: Value to format. Accepts:
            - ``str`` — treated as a single value (split for multi-part fields).
            - ``tuple`` / ``list`` — multi-part value (e.g. ``(observer, agency)``).
            - ``float`` / ``int`` — numeric value.
            - ``None`` — returns ``None``.

    Returns:
        The formatted, padded string, or ``None`` if the value yields
        nothing meaningful to write.

    Examples:
        >>> format_rinex_field("MARKER NAME", "ELDC")[:4]
        'ELDC'
        >>> format_rinex_field("ANT # / TYPE", ("CR6200", "ASH701945C_M    SCIS"))[:6]
        'CR6200'
    """
    if value is None:
        return None

    if field_name == "MARKER NAME":
        v = str(value).strip()
        if not v:
            return None
        return v.upper().ljust(60)[:60]

    elif field_name == "MARKER NUMBER":
        v = str(value).strip()
        if not v:
            return None
        return v.ljust(20)[:20]

    elif field_name == "OBSERVER / AGENCY":
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            obs, agency = str(value[0]).strip(), str(value[1]).strip()
        else:
            parts = str(value).split(None, 1)
            obs = parts[0] if parts else ""
            agency = parts[1] if len(parts) > 1 else ""
        if not obs and not agency:
            return None
        return f"{obs.ljust(20)[:20]}{agency.ljust(40)[:40]}"

    elif field_name == "REC # / TYPE / VERS":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            serial, model, version = (
                str(value[0]).strip(),
                str(value[1]).strip(),
                str(value[2]).strip(),
            )
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            serial, model, version = str(value[0]).strip(), str(value[1]).strip(), ""
        else:
            parts = str(value).split(None, 2)
            serial = parts[0] if len(parts) > 0 else ""
            model = parts[1] if len(parts) > 1 else ""
            version = parts[2] if len(parts) > 2 else ""
        if not serial and not model:
            return None
        return f"{serial.ljust(20)[:20]}{model.ljust(20)[:20]}{version.ljust(20)[:20]}"

    elif field_name == "ANT # / TYPE":
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            serial, ant_type = str(value[0]).strip(), str(value[1]).strip()
        else:
            parts = str(value).split(None, 1)
            serial = parts[0] if parts else ""
            ant_type = parts[1] if len(parts) > 1 else ""
        if not serial:
            return None
        return f"{serial.ljust(20)[:20]}{ant_type.ljust(20)[:20]}"

    elif field_name == "ANTENNA: DELTA H/E/N":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            h, e, n = float(value[0]), float(value[1]), float(value[2])
        elif isinstance(value, (int, float)):
            h, e, n = float(value), 0.0, 0.0
        else:
            try:
                parts = str(value).split()
                h = float(parts[0]) if len(parts) > 0 else 0.0
                e = float(parts[1]) if len(parts) > 1 else 0.0
                n = float(parts[2]) if len(parts) > 2 else 0.0
            except (ValueError, IndexError):
                return None
        return f"{h:14.4f}{e:14.4f}{n:14.4f}"

    elif field_name == "APPROX POSITION XYZ":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            x, y, z = float(value[0]), float(value[1]), float(value[2])
        else:
            return None
        return f"{x:14.4f}{y:14.4f}{z:14.4f}"

    elif field_name == "INTERVAL":
        try:
            return f"{float(value):10.3f}"
        except (ValueError, TypeError):
            return None

    else:
        v = str(value).strip()
        return v if v else None


def format_antenna_type_with_radome(antenna_model: str, radome: str = "NONE") -> str:
    """Format antenna type + radome for the ``ANT # / TYPE`` field.

    IGS convention: 15 chars antenna model + space + 4 chars radome = 20 chars.

    Args:
        antenna_model: Antenna model, e.g. ``"ASH701945C_M"``.
        radome: Radome code, e.g. ``"SCIS"``. Defaults to ``"NONE"``.

    Returns:
        Exactly 20 characters: ``"ANT_MODEL_15CH SCIS"``.
    """
    model = antenna_model.ljust(15)[:15]
    dome = (radome or "NONE").ljust(4)[:4]
    return f"{model} {dome}"
