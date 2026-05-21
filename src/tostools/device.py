"""Helpers for the ``tos device add`` CLI — warehouse intake of GNSS hardware.

The CLI in ``tostools.tos._device_main`` is the user-facing entrypoint; the
helpers here exist so the validation, attribute shaping, and IGS lookup logic
can be unit-tested without spinning up argparse or hitting the TOS API.

A "device" in TOS is an entity with ``code_entity_subtype`` of
``gnss_receiver``, ``antenna``, ``radome``, or ``monument``. Warehouse intake
creates the entity with the required attributes (serial, model, owner,
location) and then layers optional attributes (firmware, comment, galvos) via
:meth:`tostools.api.tos_writer.TOSWriter.upsert_attribute_value`.

Extending to seismic / non-GPS devices
--------------------------------------
The required/optional attribute codes and the dry-run flow are intentionally
domain-agnostic — adding a seismometer or digitizer subtype is a two-line
change:

1. Append the new ``code_entity_subtype`` to :data:`VALID_SUBTYPES`.
2. Add a case in :func:`validate_model`. If the new subtype has no IGS lookup
   (most non-GPS instruments don't), fall through to the ``monument`` branch's
   pass-through behaviour. If it does have a standard model list, add a lookup
   table in :mod:`tostools.standards` and a dispatch branch here.

The CLI itself (``_device_main`` in ``tostools.tos``) needs no change beyond
broadening its ``--subtype`` argparse ``choices``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .standards.igs_equipment import (
    ANTENNA_IGS,
    RADOME_IGS,
    RECEIVER_IGS,
    to_igs_antenna,
    to_igs_radome,
    to_igs_receiver,
)

VALID_SUBTYPES: Tuple[str, ...] = (
    "gnss_receiver",
    "antenna",
    "radome",
    "monument",
)

REQUIRED_ATTR_CODES: Tuple[str, ...] = (
    "serial_number",
    "model",
    "owner",
    "status",
    "date_start",
)

# Order here drives the iteration order in :func:`iter_optional_attributes`
# and therefore the order of ``upsert_attribute_value`` calls in the CLI.
OPTIONAL_ATTR_CODES: Tuple[str, ...] = (
    "firmware_version",
    "comment",
    "galvos",
)

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def normalize_date_start(raw: str) -> str:
    """Normalise ``--date-start`` to ``YYYY-MM-DDTHH:MM:SS``.

    Accepts a calendar date (``YYYY-MM-DD``, expanded to midnight) or a full
    ISO-8601 datetime without timezone. Anything else raises ``ValueError``.
    ``TOSWriter._tos_date`` will still strip a trailing ``Z`` / ``+HH:MM`` from
    the wire payload if one slips through.
    """
    if not raw:
        raise ValueError("date_start must be a non-empty string")
    if _DATE_ONLY_RE.match(raw):
        candidate = f"{raw}T00:00:00"
    elif _DATETIME_RE.match(raw):
        candidate = raw
    else:
        raise ValueError(
            f"date_start must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, got {raw!r}"
        )
    # Calendar sanity check (catches 2026-02-30 etc.).
    datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%S")
    return candidate


def _format_known_models(table: Dict[str, str], header: str) -> str:
    """Render an IGS lookup table as ``IGS_NAME (aliases: a, b, c)`` lines."""
    aliases_by_value: Dict[str, List[str]] = {}
    for alias, igs in table.items():
        aliases_by_value.setdefault(igs, []).append(alias)
    lines = [header]
    for igs in sorted(aliases_by_value):
        extras = [a for a in aliases_by_value[igs] if a != igs]
        if extras:
            lines.append(f"  {igs}    (aliases: {', '.join(sorted(extras))})")
        else:
            lines.append(f"  {igs}")
    return "\n".join(lines)


def validate_model(subtype: str, raw: str) -> str:
    """Return the IGS-standard model string for *raw*, or raise ``ValueError``.

    For ``gnss_receiver`` and ``antenna`` the IGS lookup is authoritative: an
    unknown model is rejected and the error message lists the known IGS values
    (with accepted aliases) so the user can either fix the typo or extend
    :mod:`tostools.standards.igs_equipment`. ``radome`` defers to the IGS table
    but the table maps unknown radomes to ``"NONE"`` rather than raising.
    ``monument`` has no IGS table — the raw string is returned unchanged after
    a non-empty check.
    """
    if not raw:
        raise ValueError(f"--model is required for subtype {subtype!r}")

    if subtype == "gnss_receiver":
        result = to_igs_receiver(raw)
        if result is None:
            raise ValueError(
                _format_known_models(
                    RECEIVER_IGS,
                    f"Unknown gnss_receiver model: {raw!r}.\n"
                    "Known IGS receiver names (accepted aliases in parentheses):",
                )
                + "\n\nAdd the alias in tostools/standards/igs_equipment.py "
                "if your model is missing."
            )
        return result

    if subtype == "antenna":
        result = to_igs_antenna(raw)
        if result is None:
            raise ValueError(
                _format_known_models(
                    ANTENNA_IGS,
                    f"Unknown antenna model: {raw!r}.\n"
                    "Known IGS antenna names (accepted aliases in parentheses):",
                )
                + "\n\nAdd the alias in tostools/standards/igs_equipment.py "
                "if your model is missing."
            )
        return result

    if subtype == "radome":
        # to_igs_radome silently coerces unknown values to "NONE" — we
        # surface the available codes when the caller passed something that
        # was not an exact alias, so the coercion never happens behind a
        # warehouse-intake user's back.
        if raw not in RADOME_IGS and raw.upper() not in {k.upper() for k in RADOME_IGS}:
            raise ValueError(
                _format_known_models(
                    RADOME_IGS,
                    f"Unknown radome model: {raw!r}.\n"
                    "Known IGS radome codes (accepted aliases in parentheses):",
                )
                + "\n\nUse 'NONE' for no radome, or add the alias in "
                "tostools/standards/igs_equipment.py."
            )
        return to_igs_radome(raw) or "NONE"

    if subtype == "monument":
        # No IGS table for monuments — accept the raw string.
        return raw

    raise ValueError(
        f"Unknown subtype {subtype!r}. Valid subtypes: {', '.join(VALID_SUBTYPES)}"
    )


def build_required_attributes(
    serial: str,
    model: str,
    owner: str,
    date_start: str,
) -> List[Dict[str, Optional[str]]]:
    """Build the attribute list passed to :meth:`TOSWriter.create_device`.

    Each attribute carries an explicit ``date_to: None`` — the TOS ``/entities``
    endpoint treats the field as required-present even when the period is open.

    Returns the canonical warehouse-device attribute set verified against
    existing open children of B9 - Kjallari - Jörð (id_entity=4) on
    2026-05-21:

    - ``serial_number``, ``model``, ``owner`` — device identity
    - ``status`` = ``"virkt"`` (active) — canonical "device is alive" marker
    - ``date_start`` — separate from the per-attribute ``date_from`` field;
      stored as its own attribute_value row by TOS

    Note: ``location`` was previously written here as a free-text attribute
    on the device. It has been removed — TOS represents "device at
    location" via an ``entity_connection`` row joining the device entity
    to the location entity, not via a string attribute on the device.
    Callers must call :meth:`TOSWriter.create_entity_connection` after
    :meth:`TOSWriter.create_device` to record the placement; see
    :meth:`TOSWriter.connect_device_to_location` for the resolve + connect
    convenience wrapper.
    """
    return [
        {
            "code": "serial_number",
            "value": serial,
            "date_from": date_start,
            "date_to": None,
        },
        {
            "code": "model",
            "value": model,
            "date_from": date_start,
            "date_to": None,
        },
        {
            "code": "owner",
            "value": owner,
            "date_from": date_start,
            "date_to": None,
        },
        {
            "code": "status",
            "value": "virkt",
            "date_from": date_start,
            "date_to": None,
        },
        {
            "code": "date_start",
            "value": date_start,
            "date_from": date_start,
            "date_to": None,
        },
    ]


def iter_optional_attributes(
    firmware: Optional[str] = None,
    comment: Optional[str] = None,
    galvos: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Return ``(code, value)`` pairs for the supplied optional attributes.

    Empty / ``None`` inputs are dropped so the caller can iterate the result
    directly and call ``upsert_attribute_value`` once per pair. Order matches
    :data:`OPTIONAL_ATTR_CODES`.
    """
    by_code: Dict[str, Optional[str]] = {
        "firmware_version": firmware,
        "comment": comment,
        "galvos": galvos,
    }
    pairs: List[Tuple[str, str]] = []
    for code in OPTIONAL_ATTR_CODES:
        value = by_code[code]
        if value:
            pairs.append((code, value))
    return pairs
