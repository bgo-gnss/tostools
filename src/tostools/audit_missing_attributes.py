"""Detect missing required TOS attributes (Layer 6 of the audit work).

The Layers 2-5 audit (``audit_attribute_dates``) checks the dates of attributes
that *exist*. This module checks the *presence* of attributes that *should*
exist — surfacing REYK-style gaps where the station entity is missing its
``date_start``, or where a device is missing a required ``serial_number``.

Rule — per ``.interrogate-tos-audit-missing-attributes.md``::

    For each entity in scope (the station + its open child devices), iterate
    the catalog rules for that entity's scope. Flag every code where
    ``entity.code_entity_subtype ∈ entry['gps_required_for']`` AND the entity
    has no open attribute period for that code.

The walker fans out from one station:

* The station entity itself is audited against ``catalog['stations']`` —
  station subtype is always ``geophysical`` for real GPS sites.
* Each open child device (``time_to`` is None) is audited against
  ``catalog['devices']`` — restricted to the GPS quartet
  (gnss_receiver, antenna, radome, monument). Monument-specific catalog
  entries are reached naturally via ``applies_to: [monument]``.

Iterating *only* the entity's natural catalog scope prevents the cross-scope
collision pattern (``subtype``, ``date_start``, ``lat``, …) from shadowing
station-level rules behind device-level ones. See
:func:`tostools.audit_attribute_dates.load_catalog_scoped`.

Suppression-file integration and CLI wiring land in the next step (Layer 6
task #10); this module exposes the walker + data model only.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .api.tos_client import TOSClient
from .audit import GPS_DEVICE_SUBTYPES, _resolve_station_entity
from .audit_attribute_dates import (
    SuppressionParseError,
    _date_only,
    _open_attribute_value,
    _station_display_name,
    _station_joins_by_device,
    load_catalog_scoped,
)
from .data_files import data_path

# (id_entity, code) — "missing" has no date anchor, so the suppression key
# is shorter than the attribute-dates audit's 3-tuple.
MissingSuppressionKey = Tuple[int, str]

# Layer 3 — committed-in-repo suppression file for missing-attributes.
# Format: one ``SUPPRESS <id_entity> <code>`` per known-good gap.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MISSING_SUPPRESSIONS_PATH = data_path(
    "audit_suppressions", "missing_attributes.txt"
)

# Placeholders rendered in triage output for codes without catalog defaults
# or when a sensible date_from anchor isn't available.
FILL_VALUE_PLACEHOLDER = "<FILL_VALUE>"
FILL_DATE_PLACEHOLDER = "<FILL_DATE>"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingAttributeViolation:
    """One catalog code an entity is required to have but doesn't.

    Carries enough context for triage emission:
    ``suggested_value`` pre-fills the ACTION line when the catalog has a
    ``default_value`` (e.g. ``subtype`` → ``"GPS stöð"``); ``suggested_date_from``
    pre-fills the date when the entity is a device (we use its earliest open
    join's ``time_from``). Stations don't carry a sensible date hint — the
    operator picks the value when they uncomment the line.
    """

    id_entity: int
    subtype: str
    name: Optional[str]
    code: str
    scope: str  # "stations" or "devices" — which catalog scope the rule came from
    suggested_value: Optional[str]
    suggested_date_from: Optional[str]
    # "required" (gps_required_for → a real violation) or "recommended"
    # (gps_recommended_for → a logged reminder, not a violation; a safe default
    # is applied at dissemination/sitelog — e.g. antenna azimuth → 0.0).
    severity: str = "required"
    # Set when the device is NOT currently installed here: the value describes
    # one finished occupation, so it must be written as a CLOSED period. An
    # open period would assert this station's geometry as the device's
    # forever-truth — wrong for campaign kit that went on to other marks.
    suggested_date_to: Optional[str] = None
    # Human-readable provenance for suggested_value, rendered into the triage
    # so the operator can see whether a number was derived or taken raw.
    value_basis: Optional[str] = None


@dataclass(frozen=True)
class SuppressedMissing:
    """A missing-attributes hit that was filtered by the suppression file."""

    violation: MissingAttributeViolation
    suppressions_path: Path
    line_no: int


@dataclass(frozen=True)
class StaleOpenAttribute:
    """An installation-scoped attribute still open after the device left.

    The inverse defect to a missing attribute: the value is present, but its
    period never got closed when the device was removed, so TOS still claims
    this station's geometry describes a device that is now in a warehouse or at
    another mark.

    ISAK antenna 4527 is the worked example — removed 2026-07-30T12:00, its
    join correctly closed and ``status``/``comment`` correctly updated by
    ``cfg replace-antenna``, but ``antenna_height`` (id 113122) left open since
    2002-01-09. Only codes in :data:`_INSTALL_SCOPED_CODES` qualify: ``status``,
    ``comment`` and ``owner`` are also mutable but describe the device wherever
    it now is, so they stay open by design.
    """

    id_entity: int
    subtype: str
    name: Optional[str]
    code: str
    value: Optional[str]
    date_from: str
    #: Date the device left this station — the ``date_to`` the period wants.
    removal_date: str


@dataclass
class StationMissingAttributesReport:
    """Result of :func:`audit_station_missing_attributes`."""

    station_id: int
    station_name: Optional[str]
    audited_entities: int = 0
    devices_skipped: int = 0
    violations: List[MissingAttributeViolation] = field(default_factory=list)
    stale_open: List[StaleOpenAttribute] = field(default_factory=list)
    suppressed: List[SuppressedMissing] = field(default_factory=list)
    suppressions_path: Optional[Path] = None
    suppressions_errors: List[SuppressionParseError] = field(default_factory=list)
    suppressions_disabled: bool = False
    #: Which station.info backed this audit, and — when the field-record oracle
    #: could not be resolved — why. A non-None ``station_info_note`` means every
    #: suggested height/eccentricity degraded to the catalog default, so the
    #: triage file must say so instead of silently looking authoritative.
    station_info_path: Optional[Path] = None
    station_info_note: Optional[str] = None

    @property
    def station_info_degraded(self) -> bool:
        """True when the station.info oracle was unavailable for this audit."""
        return self.station_info_note is not None

    @property
    def hard_violations(self) -> List[MissingAttributeViolation]:
        """Required-tier misses only — the ones that count as real violations."""
        return [v for v in self.violations if v.severity == "required"]

    @property
    def recommended_missing(self) -> List[MissingAttributeViolation]:
        """Recommended-tier misses — logged reminders, not violations."""
        return [v for v in self.violations if v.severity == "recommended"]

    @property
    def has_violations(self) -> bool:
        # Only hard (required) misses fail the audit; recommended are reminders.
        return bool(self.hard_violations)

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed)


# ---------------------------------------------------------------------------
# Suppression file parsing (Layer 3 — 2-tuple key)
# ---------------------------------------------------------------------------


def load_missing_suppressions(
    path: Optional[Path] = None,
) -> Tuple[Dict[MissingSuppressionKey, int], List[SuppressionParseError], Path]:
    """Parse a SUPPRESS-style file for the missing-attributes audit.

    Format: one ``SUPPRESS <id_entity> <code>`` per line. Comments start
    with ``#`` and run to end-of-line; blank lines are ignored. The key
    is a 2-tuple — there's no ``date_from`` anchor since "missing" has
    no date. Mirrors :func:`tostools.audit_attribute_dates.load_suppressions`
    in spirit; the shorter shape is the only material difference.

    Returns ``(suppressions, errors, resolved_path)``:

    * ``suppressions`` — ``{(id_entity, code): line_no}`` mapping. Line
      numbers are kept so verbose output can show which file line
      silenced each entry.
    * ``errors`` — collected malformed lines; the caller decides whether
      to abort or continue with the parsed entries.
    * ``resolved_path`` — the path actually tried.

    File-not-found is NOT an error. Returns an empty mapping. The
    suppression file is opt-in.
    """
    if path is None:
        path = DEFAULT_MISSING_SUPPRESSIONS_PATH

    suppressions: Dict[MissingSuppressionKey, int] = {}
    errors: List[SuppressionParseError] = []

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return suppressions, errors, path

    for i, line in enumerate(text.splitlines(), 1):
        raw = line
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue

        tokens = line.split()
        if tokens[0] != "SUPPRESS":
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=(
                        f"expected line to start with 'SUPPRESS' (got {tokens[0]!r})"
                    ),
                    raw=raw,
                )
            )
            continue
        if len(tokens) < 3:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=(
                        "SUPPRESS line requires 2 arguments: "
                        f"<id_entity> <code> (got {len(tokens) - 1})"
                    ),
                    raw=raw,
                )
            )
            continue
        try:
            id_entity = int(tokens[1])
        except ValueError:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=f"id_entity must be int, got {tokens[1]!r}",
                    raw=raw,
                )
            )
            continue
        code = tokens[2]
        suppressions[(id_entity, code)] = i

    return suppressions, errors, path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_display_label(history: Dict[str, Any]) -> Optional[str]:
    """Pull a human label for a device — open serial_number, then open model."""
    for code in ("serial_number", "model"):
        v = _open_attribute_value(history, code)
        if v:
            return v
    return None


#: Antennas installed on/after this date are assumed true-north aligned
#: (azimuth 0.0), so a closed-join antenna's missing azimuth can safely default
#: to 0.0. Before this, campaign setups were oriented arbitrarily and cannot be
#: reconstructed after the fact.
_AZIMUTH_NORTH_ASSUMABLE_SINCE = "2012-01-01"


def _azimuth_assumable_north(
    code: str, entity_subtype: str, install_date: Optional[str]
) -> bool:
    """True when a closed-join antenna's missing ``azimuth`` is safely 0.0."""
    if code != "azimuth" or entity_subtype != "antenna":
        return False
    if not install_date:
        return False
    return _date_only(str(install_date)) >= _AZIMUTH_NORTH_ASSUMABLE_SINCE


def _required_codes_in_scope(
    scope_rules: Dict[str, Dict[str, Any]],
    entity_subtype: str,
    *,
    installed: bool = True,
    install_date: Optional[str] = None,
) -> List[Tuple[str, Dict[str, Any], str]]:
    """Return ``[(code, entry, severity)]`` for rules that apply to
    ``entity_subtype``, where ``severity`` is:

    * ``"required"`` — ``entity_subtype ∈ gps_required_for`` (a real violation).
    * ``"recommended"`` — ``entity_subtype ∈ gps_recommended_for`` (a logged
      reminder; a safe default is applied downstream).

    ``gps_required_when_installed`` is a third tier: required, but only while
    the device is actually installed here (``installed=True``). It exists for
    values that describe a live installation and cannot be reconstructed for a
    historical one. Antenna ``azimuth`` is the case that motivated it —
    promoting it to plain ``gps_required_for`` made it a violation on every
    historical occupation in the fleet (423 of 2,535 on the 2026-08-03 survey),
    all of which would only ever be filled with the catalog default. Nobody can
    survey the orientation of a 1995 campaign setup after the fact; asking is
    noise, and noise is how a check gets ignored.

    ``gps_required_for`` wins if a code lists the subtype in several. Filtering
    on ``gps_relevance == 'yes'`` keeps "no"/"maybe" entries out until classified.
    """
    out: List[Tuple[str, Dict[str, Any], str]] = []
    for code, entry in scope_rules.items():
        if entry.get("gps_relevance") != "yes":
            continue
        required = entry.get("gps_required_for") or []
        when_installed = entry.get("gps_required_when_installed") or []
        recommended = entry.get("gps_recommended_for") or []
        if entity_subtype in required:
            out.append((code, entry, "required"))
        elif entity_subtype in when_installed:
            if installed or _azimuth_assumable_north(
                code, entity_subtype, install_date
            ):
                out.append((code, entry, "required"))
            # Not installed → not asked for at all, except antenna azimuth
            # installed after the true-north cutoff (see
            # _azimuth_assumable_north). Deliberately not demoted to
            # "recommended": a reminder nobody can act on is the same noise in
            # a quieter voice.
        elif entity_subtype in recommended:
            out.append((code, entry, "recommended"))
    return out


def _last_removal(joins: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Latest ``time_to`` across ``joins`` (date-only), or ``None``."""
    ends = [
        _date_only(str(j["time_to"]))
        for j in joins
        if j.get("time_to") is not None and j.get("time_to")
    ]
    return max(ends) if ends else None


def _served_elsewhere_after(
    client: TOSClient,
    device_id: int,
    station_joins: Sequence[Dict[str, Any]],
) -> bool:
    """True if the device was installed at a DIFFERENT parent after leaving here.

    Distinguishes portable campaign equipment from a genuinely retired device.
    Early GPS occupations reused the same antennas across many marks: ISAK
    antenna 4520 has eight parent joins in 2001 and was installed at another
    station three days after it left here. The monument stayed put; the kit
    toured. So a closed join at this station says nothing about the device's
    condition, and labelling it "decommissioned" — or inferring a status from
    this tenure — would be wrong.

    Failures are swallowed: an unavailable parent history must degrade to
    "unknown, treat as retired" rather than break a read-only audit.
    """
    last_here = _last_removal(station_joins)
    if last_here is None:
        return False
    try:
        parents = client.get_parent_history(device_id) or []
    except Exception:  # noqa: BLE001 - never fail the audit on a lookup
        return False

    station_parents = {
        j.get("id_entity_parent") for j in station_joins if j.get("id_entity_parent")
    }
    for p in parents:
        start = p.get("time_from")
        if not start:
            continue
        if _date_only(str(start)) > last_here:
            return True
        # A later join to a DIFFERENT parent that started earlier but is still
        # open also counts as "went on serving".
        if (
            p.get("time_to") is None
            and p.get("id_entity_parent") not in station_parents
        ):
            return True
    return False


#: TOS attribute code -> the Occupation field that authoritatively supplies it.
#: GAMIT station.info is the field record of what was actually installed, so it
#: beats a catalog default: the catalog can only offer 0.0 for the antenna
#: eccentricities, while station.info carries the surveyed value (ISAK's run to
#: 2.7 mm), and it is the origin of the DHARP height code.
_STATION_INFO_FIELDS = {
    "antenna_height": "antenna_height",
    "antenna_reference_point": "htcod",
    "antenna_offset_north": "antenna_north",
    "antenna_offset_east": "antenna_east",
}


def load_station_info_occupations(path, marker: str):
    """Parse ``station.info`` and return this marker's occupations, or []."""
    from .standards.gamit_station_info import parse_station_info

    try:
        occs = parse_station_info(str(path))
    except (OSError, ValueError):
        return []
    return [o for o in occs if (o.marker or "").upper() == marker.upper()]


def _occupation_for(occupations, serial: Optional[str], date_from: Optional[str]):
    """Best occupation for a device, matched on serial then install date.

    Serial is the strong key — a campaign antenna appears at many markers, but
    within one marker its serial identifies it. Date picks the right OCCUPATION
    of that unit, which matters because the same antenna can be re-installed
    with a different height (ISAK antenna 190269 has two occupations, 1.0047
    then 1.0358, so picking the wrong one records a real measurement against
    the wrong period).

    Selection, given a date *inside* some occupation's span:

      1. the occupation whose ``[time_from, time_to)`` **covers** the date
         (latest such, in case spans ever nest);
      2. else the latest occupation **starting on or before** the date;
      3. else (no date, or the date precedes every occupation) the earliest.

    Step 2 is the one that used to be missing. The previous implementation
    matched on an EXACT ``time_from`` and otherwise fell back to
    ``candidates[0]`` — the earliest occupation — so any window beginning
    part-way through an occupation silently got the wrong era's height. On a
    long-history station, where every receiver era adds an occupation and the
    TOS join windows rarely land exactly on one, that is the common case, not
    the edge case: HVOL's 2019/2021/2026 windows all resolved to the 1999
    occupation (1.0443) instead of the covering one (0.9780).
    """
    if not occupations:
        return None
    serial = (serial or "").strip()
    candidates = [
        o for o in occupations if serial and (o.antenna_sn or "").strip() == serial
    ]
    if not candidates:
        return None

    def _start(o):
        return _date_only(str(o.time_from)) if o.time_from else None

    want = _date_only(str(date_from)) if date_from else None
    if want:
        covering = [
            o
            for o in candidates
            if _start(o)
            and _start(o) <= want
            and (o.time_to is None or want < _date_only(str(o.time_to)))
        ]
        if covering:
            return max(covering, key=_start)
        earlier = [o for o in candidates if _start(o) and _start(o) <= want]
        if earlier:
            return max(earlier, key=_start)
    return min(candidates, key=lambda o: _start(o) or "9999-99-99")


#: Catalog codes whose ``default_value`` asserts a CURRENT operational state,
#: and so must not be proposed for a decommissioned device. ``status``
#: defaults to "virkt" (active) — correct for an installed device, false for a
#: retired one. Offsets/azimuth are historical facts and stay defaulted.
_CURRENT_STATE_CODES = frozenset({"status"})

#: A monument's ``model`` (construction type) has NO default — it is REQUIRED
#: user input. It is published to IGS site log §3.1 "Foundation", and the fleet
#: mode ("GPS stál-fjórfótur") is only ~2/3 of known marks across four+ types
#: (drill-braced, pillar, pin, pole), so auto-filling it silently publishes a
#: wrong §3.1. The audit therefore emits <FILL_VALUE> for it, forcing the
#: operator to supply the actual mark type (see MONUMENT_MODEL_TO_IGS in
#: legacy/gps_metadata_functions.py for the §3.1 translation).

#: Attribute codes that describe ONE INSTALLATION rather than the device.
#: They must be written as closed periods when the device is no longer here,
#: and an open one after removal is the :class:`StaleOpenAttribute` defect.
#:
#: Deliberately a fixed list, not "every ``classification: mutable`` code":
#: ``status``, ``comment`` and ``owner`` are mutable too, but they describe the
#: device wherever it now is and stay open by design.
#: ``antenna_reference_point`` is excluded because the catalog classifies it
#: ``inherent`` (the DHARP convention travels with the antenna model).
INSTALL_SCOPED_CODES = frozenset(
    {
        "antenna_height",
        "antenna_offset_north",
        "antenna_offset_east",
        "azimuth",
    }
)

#: Private alias kept for readability inside this module. The public name is
#: what ``receivers`` imports so the write path closes exactly the codes this
#: audit reports — one definition, so detector and writer can never disagree.
_INSTALL_SCOPED_CODES = INSTALL_SCOPED_CODES

#: Antenna geometry code -> the monument code carrying the same axis.
#:
#: GAMIT ``station.info`` records the STATION COMPOSITE (``Ant Ht`` = monument
#: height + ARP delta). TOS splits it: the monument entity holds the bulk, the
#: antenna entity holds only the delta above it. Suggesting the composite for
#: the antenna is a ~1 m error — see ISAK 4520, whose correct second-occupation
#: value is 0.0311 against a station.info height of 1.0358 (monument 1.0047).
_MONUMENT_BASELINE_CODES = {
    "antenna_height": "monument_height",
    "antenna_offset_north": "antenna_offset_north",
    "antenna_offset_east": "antenna_offset_east",
}


def _format_decimal(value: "Decimal") -> str:
    """Render a derived number without float noise or a bare integer.

    ``1.0358 - 1.0047`` in binary floating point is 0.031099999999999906;
    quantising first keeps the 4-decimal survey precision TOS stores. Trailing
    zeros go, but at least one decimal stays so a zero delta reads as "0.0"
    (matching what ``cfg replace-antenna`` writes) rather than "0".
    """
    text = f"{value:.4f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def _period_covers_window(
    history: Dict[str, Any],
    code: str,
    window_from: Optional[str],
    window_to: Optional[str],
) -> bool:
    """True if some existing period already records ``code`` over the window.

    The "is it missing?" test elsewhere asks only whether an OPEN period
    exists, which is the right question for a device still installed. For a
    finished occupation it is the wrong one twice over: the value is recorded
    as a CLOSED period (correctly — see D1), and once the close-on-removal
    check has ended a stale open period the code would read as missing again
    and get re-proposed. Applying that writes a second period for a window
    that already has one, and the exact-match idempotency guard does not catch
    it because TOS stores values as strings — the '0.000' already on ISAK
    antenna 4527 is not equal to the '0.0' a fresh suggestion produces.

    Overlap, not equality: any period intersecting the occupation means the
    window is described, even when the boundaries were recorded slightly
    differently from the join dates.
    """
    for attribute in history.get("attributes") or []:
        if attribute.get("code") != code:
            continue
        date_from_raw = attribute.get("date_from")
        if not date_from_raw:
            continue
        period_from = _date_only(str(date_from_raw))
        date_to_raw = attribute.get("date_to")
        period_to = _date_only(str(date_to_raw)) if date_to_raw else None
        starts_before_window_ends = window_to is None or period_from < window_to
        ends_after_window_starts = (
            period_to is None or window_from is None or period_to > window_from
        )
        if starts_before_window_ends and ends_after_window_starts:
            return True
    return False


def _is_unrecorded_monument_height(code: str, value: str) -> bool:
    """True when a monument height of zero means "never measured".

    Only ``monument_height`` qualifies. A monument IS a pillar or a bolted
    tripod — it always has a height — so 0 is a placeholder, not a survey.
    ISAK's three campaign-era monuments (5132/5133/5134) all read ``'0.000'``.
    A zero ``antenna_offset_*`` by contrast is the normal, real answer.
    """
    if code != "monument_height":
        return False
    try:
        return Decimal(value) == 0
    except InvalidOperation:
        return False


def _covering_join(joins: Sequence[Dict[str, Any]], window_from: str) -> bool:
    """True if any join in ``joins`` was active on ``window_from``."""
    for j in joins:
        tf = j.get("time_from")
        if not tf:
            continue
        start = _date_only(str(tf))
        end_raw = j.get("time_to")
        end = _date_only(str(end_raw)) if end_raw else None
        if start <= window_from and (end is None or window_from < end):
            return True
    return False


def _monument_baseline(
    client: TOSClient,
    joins_by_device: Dict[int, List[Dict[str, Any]]],
    window_from: str,
) -> Optional[Dict[str, str]]:
    """Monument values in force at ``window_from``, as ``{code: value}``.

    Returns ``None`` when no monument covers the window at all, versus ``{}``
    or a partial dict when one covers it but has no open value for some code.
    The caller reports those differently: "there was no monument to split
    against" is a fact about the site, "the monument exists but this axis was
    never recorded" is a gap someone can go and fill.

    Lookup failures degrade to ``{}`` — a read-only audit must not die because
    one device history is unavailable.

    Devices are visited in id order so a station that (wrongly) has two
    monuments covering one window still yields a stable answer run to run.
    """
    for device_id in sorted(joins_by_device):
        joins = joins_by_device[device_id]
        if not _covering_join(joins, window_from):
            continue
        try:
            history = client.get_entity_history(device_id)
        except Exception:  # noqa: BLE001 - never fail the audit on a lookup
            continue
        if not history or (history.get("code_entity_subtype") or "") != "monument":
            continue
        out: Dict[str, str] = {}
        for monument_code in set(_MONUMENT_BASELINE_CODES.values()):
            value = _open_attribute_value(history, monument_code)
            if value is not None:
                out[monument_code] = value
        return out
    return None


def _audit_entity(
    *,
    report: StationMissingAttributesReport,
    scope_rules: Dict[str, Dict[str, Any]],
    history: Dict[str, Any],
    entity_id: int,
    entity_subtype: str,
    entity_label: Optional[str],
    scope_name: str,
    suggested_date_from: Optional[str] = None,
    suggested_date_to: Optional[str] = None,
    decommissioned: bool = False,
    occupation=None,
    monument_baseline: Optional[Dict[str, str]] = None,
    seen_codes: Optional[set] = None,
) -> None:
    """Walk one entity (station, device, or monument).

    Increments ``report.audited_entities``, appends to ``report.violations``.
    The caller passes the correct catalog scope (``catalog['stations']`` for
    a station entity, ``catalog['devices']`` for a device/monument) so
    cross-scope code collisions stay distinct.

    ``decommissioned`` suppresses catalog defaults that assert a CURRENT
    operational state — see :data:`_CURRENT_STATE_CODES`.

    ``suggested_date_to`` marks the occupation as finished, so the triage emits
    a closed ``add-attribute-period`` instead of an open ``add-attribute``.

    ``monument_baseline`` de-composites the antenna geometry: station.info
    carries monument + ARP, TOS wants the ARP alone. See
    :data:`_MONUMENT_BASELINE_CODES`.

    ``seen_codes`` lets a caller auditing several occupation windows of the same
    device keep ``report.audited_entities`` honest across the repeats.
    """
    if seen_codes is None:
        report.audited_entities += 1
    # An occupation with an end date is finished, so the device is not
    # installed here now — that is what gates gps_required_when_installed.
    for code, entry, severity in _required_codes_in_scope(
        scope_rules,
        entity_subtype,
        installed=suggested_date_to is None,
        install_date=suggested_date_from,
    ):
        if _open_attribute_value(history, code) is not None:
            continue
        if suggested_date_to is not None and _period_covers_window(
            history, code, suggested_date_from, suggested_date_to
        ):
            # Finished occupation whose value is already on file as a closed
            # period. Re-proposing it would duplicate the row.
            continue
        default = entry.get("default_value")
        suggested_value = str(default) if default is not None else None
        value_basis: Optional[str] = None
        # Lowest-tier fallback, applied only when the catalog offers nothing.
        # station.info still overrides it below — the ordering is deliberate:
        # field record > catalog default.
        # NOTE: no fleet-mode fallback for a monument's `model` — it is REQUIRED
        # user input and emits <FILL_VALUE> (see the module-level note above).
        # GAMIT station.info wins over a catalog default: it is the field
        # record of what was actually installed, not a fleet-wide guess.
        if occupation is not None and code in _STATION_INFO_FIELDS:
            observed = getattr(occupation, _STATION_INFO_FIELDS[code], None)
            if observed not in (None, ""):
                suggested_value = str(observed).strip()
                value_basis = "station.info"
                baseline_code = _MONUMENT_BASELINE_CODES.get(code)
                if baseline_code is not None:
                    # station.info gives the STATION COMPOSITE; TOS wants only
                    # the antenna's delta above the monument. Without this the
                    # suggestion is the monument height itself — ~1 m wrong.
                    baseline = (
                        None
                        if monument_baseline is None
                        else monument_baseline.get(baseline_code)
                    )
                    if monument_baseline is None:
                        value_basis = (
                            "station.info composite — no monument covers this "
                            "window, so this IS the whole height"
                        )
                    elif baseline is None:
                        value_basis = (
                            f"station.info COMPOSITE — the covering monument has "
                            f"no open {baseline_code}, so the split is unknown"
                        )
                    elif _is_unrecorded_monument_height(baseline_code, baseline):
                        # A monument with height 0 is physically meaningless —
                        # it means nobody recorded it (ISAK's three campaign-era
                        # monuments all read '0.000'). Subtracting it would turn
                        # the composite into a delta that looks derived and
                        # isn't. Say the split is unknown instead of faking it.
                        # NB the asymmetry: a zero antenna_offset_* IS a real
                        # measurement, so only the height gets this treatment.
                        value_basis = (
                            f"station.info COMPOSITE — {baseline_code} is "
                            f"{baseline!r} (unrecorded), so the monument/ARP "
                            "split is unknown. Fill the monument height first, "
                            "or record this as the whole height knowingly."
                        )
                    else:
                        try:
                            delta = Decimal(suggested_value) - Decimal(baseline)
                        except InvalidOperation:
                            value_basis = (
                                f"station.info composite (could not subtract "
                                f"{baseline_code}={baseline!r})"
                            )
                        else:
                            suggested_value = _format_decimal(delta)
                            value_basis = (
                                f"station.info {observed} - {baseline_code} "
                                f"{baseline} (monument-relative)"
                            )
        if decommissioned and code in _CURRENT_STATE_CODES:
            # The catalog default describes a device in service ("virkt" =
            # active). On a retired device that is not merely unhelpful, it is
            # false — and it would be applied in bulk from the triage file.
            # Force a human decision instead of proposing a wrong value.
            suggested_value = None
            value_basis = None
        if seen_codes is not None:
            seen_codes.add(code)
        report.violations.append(
            MissingAttributeViolation(
                id_entity=entity_id,
                subtype=entity_subtype,
                name=entity_label,
                code=code,
                scope=scope_name,
                suggested_value=suggested_value,
                suggested_date_from=suggested_date_from,
                suggested_date_to=suggested_date_to,
                value_basis=value_basis,
                severity=severity,
            )
        )


def _collect_stale_open(
    *,
    report: StationMissingAttributesReport,
    history: Dict[str, Any],
    entity_id: int,
    entity_subtype: str,
    entity_label: Optional[str],
    removal_date: str,
) -> None:
    """Flag installation-scoped attributes still open after the device left.

    Called only for devices with no open join at this station. The join is
    closed, so TOS already knows the device is gone — but an open
    ``antenna_height`` still asserts this mark's geometry for it, and every
    consumer that reads "the current value" gets a stale answer.
    """
    for attribute in history.get("attributes") or []:
        code = attribute.get("code")
        if code not in _INSTALL_SCOPED_CODES:
            continue
        if attribute.get("date_to") is not None:
            continue
        date_from_raw = attribute.get("date_from")
        if not date_from_raw:
            continue
        date_from = _date_only(str(date_from_raw))
        if date_from >= removal_date:
            # Opened on or after the device left here — it belongs to a later
            # installation, not to this station's tenure. The `>=` matters:
            # a same-day move (which is what move_device writes — close and
            # open on one date) leaves the NEXT site's attributes starting
            # exactly on this removal date. Closing those would end the new
            # installation's record at the moment it began.
            continue
        value = attribute.get("value")
        report.stale_open.append(
            StaleOpenAttribute(
                id_entity=entity_id,
                subtype=entity_subtype,
                name=entity_label,
                code=code,
                value=None if value is None else str(value),
                date_from=date_from,
                removal_date=removal_date,
            )
        )


# ---------------------------------------------------------------------------
# Main audit entry point
# ---------------------------------------------------------------------------


def audit_station_missing_attributes(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    subtypes: Optional[Sequence[str]] = None,
    catalog_path: Optional[Path] = None,
    suppressions_path: Optional[Path] = None,
    use_suppressions: bool = True,
    include_closed: bool = False,
    station_info_path: Optional[Path] = None,
    station_info_note: Optional[str] = None,
) -> StationMissingAttributesReport:
    """Walk a station + its child devices and flag missing required attributes.

    By default only devices with an OPEN join are audited. ``include_closed``
    widens the walk to every device ever joined to the station —
    decommissioned antennas, receivers and monuments included. Their metadata
    is not a current *operational* gap, which is why they are skipped by
    default, but it is still published: a retired antenna appears in IGS
    site-log section 4 and in the header of every RINEX file recorded while it
    was installed, so an absent serial or reference point there is as wrong as
    on the open one.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. No writes; ``basic_search`` +
        ``get_entity_history`` only.
    name / id_entity
        Station identifier — pass one or the other. Resolution delegates
        to :func:`tostools.audit._resolve_station_entity`, which prefers
        markers and disambiguates ``geophysical`` station collisions.
    subtypes
        Device subtypes to audit. Defaults to
        :data:`tostools.audit.GPS_DEVICE_SUBTYPES`
        (gnss_receiver, antenna, radome, monument).
    catalog_path
        Override the catalog file location. Defaults to the canonical
        repo path / ``TOSTOOLS_ATTRIBUTE_CODES_PATH`` env var.
    suppressions_path
        Override the suppression file location. Defaults to
        :data:`DEFAULT_MISSING_SUPPRESSIONS_PATH`. File-not-found is silent.
    use_suppressions
        When False, skip the suppression file entirely — every missing
        hit lands in ``violations``. Equivalent to ``--no-suppressions``.

    Returns
    -------
    StationMissingAttributesReport
        ``has_violations`` reflects the **filtered** violations list —
        suppressed entries are preserved on ``report.suppressed`` so
        verbose output can show what was silenced.

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Neither ``name`` nor ``id_entity`` set.
    FileNotFoundError
        Catalog file missing (suppression file missing is not an error).
    """
    scoped = load_catalog_scoped(catalog_path)
    stations_rules = scoped.get("stations") or {}
    devices_rules = scoped.get("devices") or {}

    wanted = tuple(subtypes) if subtypes else GPS_DEVICE_SUBTYPES

    if use_suppressions:
        suppressions, supp_errors, supp_path = load_missing_suppressions(
            suppressions_path
        )
    else:
        suppressions = {}
        supp_errors = []
        supp_path = suppressions_path or DEFAULT_MISSING_SUPPRESSIONS_PATH

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_subtype = station_history.get("code_entity_subtype") or "geophysical"
    station_name = _station_display_name(station_history, name)

    report = StationMissingAttributesReport(
        station_id=station_id,
        station_name=station_name,
        suppressions_path=supp_path,
        suppressions_errors=supp_errors,
        suppressions_disabled=not use_suppressions,
        station_info_path=station_info_path,
        station_info_note=station_info_note,
    )

    # 1. Station entity itself — iterate stations scope.
    _audit_entity(
        report=report,
        scope_rules=stations_rules,
        history=station_history,
        entity_id=station_id,
        entity_subtype=station_subtype,
        entity_label=station_name,
        scope_name="stations",
        suggested_date_from=None,
    )

    # 2. Each child device — iterate devices scope. Closed joins (time_to set)
    #    are skipped by default: a removed device's missing attributes aren't a
    #    current operational gap. `include_closed` opts into them, because they
    #    are still published via site logs and historical RINEX headers.
    occupations = (
        load_station_info_occupations(station_info_path, name or station_name or "")
        if station_info_path
        else []
    )
    # Defence in depth for library callers: the CLI resolves station.info and
    # passes a note, but a direct caller handing over a path that does not exist
    # would otherwise get the same silent degradation (the loader swallows
    # OSError -> []). NB this fires only on "the oracle is not there" — a valid
    # station.info that simply has no occupation for this marker is a legitimate
    # result, not a degraded audit.
    if station_info_path is not None:
        report.station_info_path = Path(station_info_path)
        if not Path(station_info_path).is_file() and report.station_info_note is None:
            report.station_info_note = (
                f"station.info {station_info_path} is not a readable file — the "
                "field-record oracle is UNAVAILABLE, so suggested "
                "heights/offsets are catalog defaults."
            )

    joins_by_device = _station_joins_by_device(station_history)
    for device_id, joins in joins_by_device.items():
        open_joins = [j for j in joins if j.get("time_to") is None]
        audited_joins = joins if include_closed else open_joins
        if not audited_joins:
            continue

        history = client.get_entity_history(device_id)
        if not history:
            continue

        dev_subtype = history.get("code_entity_subtype") or ""
        if dev_subtype not in wanted:
            report.devices_skipped += 1
            continue

        device_label = _device_display_label(history)
        retired = False
        if include_closed and not open_joins:
            if _served_elsewhere_after(client, device_id, joins):
                # Not retired: it left this station and was joined somewhere
                # else afterwards — another mark (campaign kit: ISAK antenna
                # 4520 has 8 parent joins in 2001 and was at another station
                # three days after leaving here) or a warehouse (4527, retired
                # to B9 in the 2026 swap). Either way its condition cannot be
                # inferred from THIS tenure, and "decommissioned" is wrong.
                # The wording states only what was checked.
                device_label = f"{device_label} [later joined elsewhere]"
            else:
                # No later join anywhere: removed and not seen since, so its
                # present condition is genuinely unknown.
                last = _last_removal(joins)
                device_label = (
                    f"{device_label} [not installed since {last or 'unknown'}]"
                )
                retired = True
        join_dates = [
            _date_only(str(j["time_from"])) for j in audited_joins if j.get("time_from")
        ]
        suggested_date = min(join_dates) if join_dates else None

        if include_closed and not open_joins:
            # The device is history here, so its values belong to finished
            # occupations. Audit each occupation window separately rather than
            # once across min(from)..max(to): ISAK antenna 4520 sat here twice
            # in 2001 with DIFFERENT heights (1.0047 then 1.0358) and was at
            # Austmannsbunga in between, so one span covering both would be
            # wrong at each end and would swallow the trip elsewhere.
            windows = sorted(
                (
                    _date_only(str(j["time_from"])),
                    _occupation_end_date(j),
                )
                for j in audited_joins
                if j.get("time_from")
            )
            seen: set = set()
            report.audited_entities += 1
            for window_from, window_to in windows:
                _audit_entity(
                    report=report,
                    scope_rules=devices_rules,
                    history=history,
                    entity_id=device_id,
                    entity_subtype=dev_subtype,
                    entity_label=device_label,
                    scope_name="devices",
                    suggested_date_from=window_from,
                    suggested_date_to=window_to,
                    decommissioned=retired,
                    occupation=_occupation_for(
                        occupations,
                        _open_attribute_value(history, "serial_number"),
                        window_from,
                    ),
                    monument_baseline=_monument_baseline(
                        client, joins_by_device, window_from
                    ),
                    seen_codes=seen,
                )
            last_removal = _last_removal(joins)
            if last_removal:
                _collect_stale_open(
                    report=report,
                    history=history,
                    entity_id=device_id,
                    entity_subtype=dev_subtype,
                    entity_label=device_label,
                    removal_date=last_removal,
                )
            continue

        _audit_entity(
            report=report,
            scope_rules=devices_rules,
            history=history,
            entity_id=device_id,
            entity_subtype=dev_subtype,
            entity_label=device_label,
            scope_name="devices",
            suggested_date_from=suggested_date,
            # Only a device with no later service anywhere has an unknown
            # state. Campaign kit that moved on is still fine, so leave its
            # catalog defaults intact rather than forcing needless hand-entry.
            decommissioned=retired,
            occupation=_occupation_for(
                occupations,
                _open_attribute_value(history, "serial_number"),
                suggested_date,
            ),
            # A currently-installed antenna needs de-compositing too: the
            # station.info height is monument + ARP whichever way the join runs.
            monument_baseline=(
                _monument_baseline(client, joins_by_device, suggested_date)
                if suggested_date
                else None
            ),
        )

    # Apply suppressions — partition violations into kept vs suppressed.
    if suppressions:
        kept: List[MissingAttributeViolation] = []
        for v in report.violations:
            key = (v.id_entity, v.code)
            line_no = suppressions.get(key)
            if line_no is not None:
                report.suppressed.append(
                    SuppressedMissing(
                        violation=v,
                        suppressions_path=supp_path,
                        line_no=line_no,
                    )
                )
            else:
                kept.append(v)
        report.violations = kept

    return report


# ---------------------------------------------------------------------------
# Triage file emission (Layer 4)
# ---------------------------------------------------------------------------


def _quote_value(value: Optional[str]) -> str:
    """Render a value for the ACTION line — shlex-quote when needed.

    ``None`` becomes the ``<FILL_VALUE>`` placeholder so the operator
    has to fill it in before applying. Values with spaces or shell
    metacharacters get single-quoted (e.g. ``GPS stöð`` → ``'GPS stöð'``),
    matching the shlex-split parsing the apply verb (task #11) will use.
    """
    if value is None:
        return FILL_VALUE_PLACEHOLDER
    return shlex.quote(value)


def _wrap_note(note: str, width: int = 62) -> List[str]:
    """Wrap an unavailability note for the triage file's warning banner."""
    import textwrap

    return textwrap.wrap(note, width=width) or [""]


def format_triage_file(
    report: StationMissingAttributesReport,
    *,
    audit_command: Optional[str] = None,
    generated_at: Optional[str] = None,
    apply_path: Optional[Path] = None,
) -> str:
    """Render a missing-attributes report as an operator-editable
    action file.

    Each violation becomes a commented ``#ACTION <id> add-attribute
    <code> <value> <date_from>`` line. The operator reviews, fills in
    ``<FILL_VALUE>`` / ``<FILL_DATE>`` placeholders where present,
    uncomments the lines they want to apply, then feeds the file into
    ``tos audit apply`` (which dispatches to the ``add-attribute`` verb
    once Layer 6 task #11 lands).

    Parameters
    ----------
    report
        The audit report. Only ``report.violations`` is consulted —
        suppressed entries are intentionally NOT emitted.
    audit_command
        Optional command-line string captured at audit time; rendered
        in the header so the file is self-documenting.
    generated_at
        Optional ISO timestamp. Defaults to ``datetime.utcnow()`` at
        call time. Pass an explicit value in tests to keep output
        byte-deterministic.

    Returns
    -------
    str
        Newline-terminated file contents, safe to write directly with
        :meth:`pathlib.Path.write_text`.

    Notes
    -----
    Violations are grouped by entity (station first, then devices) so
    the operator can read the file linearly and decide per-entity what
    to fill in.
    """
    from datetime import datetime, timezone

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines: List[str] = []
    station_label = report.station_name or "<unknown>"
    lines.append("# === tos audit missing-attributes — triage action file ===")
    lines.append(f"# Generated:  {generated_at}")
    lines.append(f"# Station:    {station_label!r} (id_entity={report.station_id})")
    if audit_command:
        lines.append(f"# Audit cmd:  {audit_command}")
    lines.append(f"# Violations: {len(report.violations)}")
    if report.stale_open:
        lines.append(f"# Open-after-removal: {len(report.stale_open)}")
    if report.station_info_path is not None:
        lines.append(f"# station.info: {report.station_info_path}")
    if report.station_info_degraded:
        # Loud on purpose: without the oracle every suggested height/offset in
        # this file is a catalog default, and antenna_height is a bare
        # <FILL_VALUE>. Silently emitting that read as "checked, nothing known".
        lines.append("#")
        lines.append("# " + "!" * 66)
        lines.append(
            "# !! WARNING — DEGRADED AUDIT: station.info UNAVAILABLE "
            "(field record NOT read)."
        )
        for _chunk in _wrap_note(str(report.station_info_note)):
            lines.append(f"# !! {_chunk}")
        lines.append("# !! Suggested heights/eccentricities are CATALOG DEFAULTS, not")
        lines.append("# !! field-record values. Re-generate before trusting them.")
        lines.append("# " + "!" * 66)
    lines.append("#")
    lines.append("# Format: one ACTION per line, '#' for comments.")
    lines.append("#")
    lines.append("#   ACTION <id_entity> add-attribute \\")
    lines.append("#          <code> <value> <date_from>                  (open period)")
    lines.append("#   ACTION <id_entity> add-attribute-period \\")
    lines.append(
        "#          <code> <value> <date_from> <date_to>        (finished occupation)"
    )
    lines.append("#   ACTION <id_entity> patch-attribute-date-to \\")
    lines.append(
        "#          <code> <date_from> <date_to>                (close a leftover)"
    )
    lines.append("#")
    lines.append("# A device that has LEFT this station gets closed periods: an open")
    lines.append("# one would assert this mark's geometry as its forever-truth, which")
    lines.append("# is wrong for campaign kit that went on to other marks.")
    lines.append("#")
    lines.append(
        "# Antenna heights/offsets are monument-RELATIVE. station.info records"
    )
    lines.append(
        "# the station composite (monument + ARP); the value below already has"
    )
    lines.append("# the monument subtracted where one covers the window.")
    lines.append("#")
    lines.append("# Workflow:")
    lines.append("#   1. Review each block below. Replace any")
    lines.append(
        f"#      {FILL_VALUE_PLACEHOLDER} / {FILL_DATE_PLACEHOLDER}"
        " placeholders with the value/date you"
    )
    lines.append("#      know is correct for the entity.")
    lines.append("#   2. Uncomment the ACTION line(s) you want to fire.")
    # Bake in the real destination when it is known: reconstructing the path
    # by hand is the step that gets fumbled, and a <file> placeholder still has
    # to be edited before it can be used.
    _f = str(apply_path) if apply_path is not None else "<file>"
    lines.append(f"#   3. tos audit apply {_f}          # dry-run preview")
    lines.append(f"#   4. tos audit apply {_f} --apply  # commit writes")
    lines.append("#")
    lines.append("# Alternative for known-good gaps: copy the SUPPRESS hint into")
    lines.append("# data/audit_suppressions/missing_attributes.txt instead.")
    lines.append("")

    if not report.violations:
        if not report.stale_open:
            lines.append("# (no violations — nothing to triage)")
            lines.append("")
            return "\n".join(lines)
        # No gaps to fill, but there are periods to close. Falling through to
        # the stale-open block matters: once a device's attributes are all
        # filled, leftover open periods are the ONLY thing left to report, and
        # an early return here would print "nothing to triage" over them.
        lines.append("# (no missing attributes — see the close section below)")

    # Group by entity for readability. Station entity always comes first
    # if present; devices follow in id_entity order.
    station_vios: List[MissingAttributeViolation] = []
    by_device: Dict[int, List[MissingAttributeViolation]] = {}
    entity_meta: Dict[int, Tuple[str, Optional[str]]] = {}
    for v in report.violations:
        if v.id_entity == report.station_id:
            station_vios.append(v)
        else:
            by_device.setdefault(v.id_entity, []).append(v)
        entity_meta[v.id_entity] = (v.subtype, v.name)

    def _emit_entity_block(
        entity_id: int, entity_vios: List[MissingAttributeViolation]
    ) -> None:
        subtype, label = entity_meta[entity_id]
        label_part = f" {label!r}" if label else ""
        lines.append(f"# --- {subtype} id_entity={entity_id}{label_part} ---")
        for v in entity_vios:
            value_token = _quote_value(v.suggested_value)
            date_token = v.suggested_date_from or FILL_DATE_PLACEHOLDER
            suggested_note = ""
            if v.suggested_value is not None:
                suggested_note = f"  (default: {v.suggested_value!r})"
            elif v.suggested_date_from is not None:
                suggested_note = f"  (date hint: {v.suggested_date_from})"
            lines.append(f"# missing: {v.code}{suggested_note}")
            if v.value_basis:
                lines.append(f"#   value from: {v.value_basis}")
            if v.suggested_date_to is not None:
                # A finished occupation: closed period, never an open one.
                lines.append(
                    f"#ACTION {v.id_entity} add-attribute-period "
                    f"{v.code} {value_token} {date_token} {v.suggested_date_to}"
                )
            else:
                lines.append(
                    f"#ACTION {v.id_entity} add-attribute "
                    f"{v.code} {value_token} {date_token}"
                )
            lines.append(f"# (or suppress: SUPPRESS {v.id_entity} {v.code})")
            lines.append("")

    if station_vios:
        _emit_entity_block(report.station_id, station_vios)

    for did in sorted(by_device):
        _emit_entity_block(did, by_device[did])

    _emit_stale_open_block(report, lines)

    return "\n".join(lines)


def _emit_stale_open_block(
    report: StationMissingAttributesReport, lines: List[str]
) -> None:
    """Append the close-the-loose-ends section, if there is anything to close.

    The inverse of the blocks above: those add a missing value, these end a
    value that outlived its installation. Emitted last so the file reads
    "fill the gaps, then close the leftovers", and kept in the same file so one
    ``tos audit apply`` run settles a device completely.
    """
    if not report.stale_open:
        return

    lines.append("")
    lines.append("# " + "=" * 72)
    lines.append("# INSTALLATION-SCOPED ATTRIBUTES STILL OPEN AFTER REMOVAL")
    lines.append("#")
    lines.append("# These devices are no longer joined to this station, but the")
    lines.append("# attributes below still have no date_to — so TOS reports this")
    lines.append("# mark's geometry as their CURRENT value. Closing them at the")
    lines.append("# removal date preserves the history without the false claim.")
    lines.append("#")
    lines.append("# status / comment / owner are deliberately NOT listed: they")
    lines.append("# describe the device wherever it is now, so they stay open.")
    lines.append("# " + "=" * 72)
    lines.append("")

    by_entity: Dict[int, List[StaleOpenAttribute]] = {}
    for stale in report.stale_open:
        by_entity.setdefault(stale.id_entity, []).append(stale)

    for entity_id in sorted(by_entity):
        entries = by_entity[entity_id]
        label = entries[0].name
        label_part = f" {label!r}" if label else ""
        lines.append(
            f"# --- {entries[0].subtype} id_entity={entity_id}{label_part} ---"
        )
        for stale in entries:
            lines.append(
                f"# open since {stale.date_from}"
                f"{'' if stale.value is None else f'  value={stale.value!r}'}"
                f"  — device removed {stale.removal_date}"
            )
            lines.append(
                f"#ACTION {stale.id_entity} patch-attribute-date-to "
                f"{stale.code} {stale.date_from} {stale.removal_date}"
            )
            lines.append("")


def _occupation_end_date(join: Dict[str, Any]) -> Optional[str]:
    """Exclusive-end occupation date for a join.

    ``_period_covers_window`` treats ``window_to`` as the half-open end of
    the occupation ([from, to)). ``date_only(time_to)`` is that end only when
    the removal happened at midnight; a sub-day occupation (e.g. HAMR antenna
    4540: 1995-09-01T00:00 -> 1995-09-01T16:30, the first half of a split
    session) ends *inside* its calendar day, so the exclusive end is the NEXT
    day. Without the bump the window is the empty [09-01, 09-01) and no fill
    starting on 09-01 can ever satisfy it — the audit re-flags a covered
    attribute forever.
    """
    raw_to = join.get("time_to")
    if not raw_to:
        return None
    to = str(raw_to)
    day = _date_only(to)
    if to[len(day) :].strip() in ("", "T00:00:00", "00:00:00", "00:00"):
        return day  # midnight end: [from, day) is the correct half-open span
    # mid-day end: the occupation occupies part of `day`, so [from, day+1)
    from datetime import date as _date
    from datetime import timedelta as _td

    y, m, d = (int(x) for x in day.split("-"))
    return (_date(y, m, d) + _td(days=1)).isoformat()
