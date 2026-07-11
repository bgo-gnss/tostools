"""Detect misdated TOS attribute periods (rule 3 from the design doc).

TOS auto-stamps an attribute period's ``date_from`` with the date the value
was *entered* into TOS, not the date it became applicable to the device.
Worked example: ARHO Ashtech has ``serial_number='13831'`` with
``date_from=2014-10-17`` even though the receiver lived at the station from
2002-01-01. The new synthesis chain renders TOS faithfully, so the phantom
date propagates into PrintTOS / sitelog / GAMIT.

Rule 3 (session-anchored) — per ``.interrogate-tos-attribute-dates-audit.md``::

    For each attribute period on a device joined to the audited station,
    find the JOIN WINDOW (session) the period's ``date_from`` falls in and
    flag the period when ``date_from`` is later than that session's
    ``time_from``. The correction aligns ``date_from`` to the session start.

The station-side join is the anchor — the physical "device present here"
date. When an attribute is stamped at the data-entry date but the station's
join carries an earlier ``time_from``, the contradiction surfaces the bug.

**Why the join, not the device's earliest attribute.** An earlier design
anchored to ``min(earliest attribute date_from, earliest join time_from)``.
That mis-fired on devices whose identity attributes predate their join to
*this* station — a receiver manufactured (or living at another station)
before it arrived. Its correctly-dated at-join attributes then read as
"later than the global-earliest" and were "corrected" to a date before the
device ever arrived. Anchoring each period to *its own* join window fixes
this; periods that fall before the first join or inside a gap between joins
are off-station history and are not flagged. Trade-off: a join record that
is itself stamped late (device truly present before its join was recorded)
hides artifacts between the true arrival and the join — that is a
join-date problem for a different audit, not rule 3.

Note constellation toggles (``GPS``/``GLO``/``GAL``…) are ``mutable``: a
gap at a session's start can be a genuine mid-tenure enablement (Galileo
added later) rather than a misdate, and that is undecidable from TOS
metadata alone — cross-check the RINEX archive (``tos audit
constellations``). So constellations stay out of the default (inherent-only)
run; reach them surgically with ``--include GPS`` and review each hit.

All four layers of the DoD now land: detection (2), suppression file (3),
and ``--triage`` emission (4) producing draft ACTION files for the
existing ``tos audit apply`` pipeline. The ``patch-attribute-date`` verb
in :mod:`tostools.tos` consumes those files.

Module surface
--------------
:func:`audit_station_attribute_dates` — main entry point. Takes a
:class:`TOSClient` plus station id/marker, walks every child device's
attribute periods, returns a :class:`StationAttributeDateReport`.

:func:`load_catalog` — read and flatten ``data/attribute_codes.yaml``.

:func:`load_suppressions` — parse the ``SUPPRESS``-style file at
``data/audit_suppressions/attribute_dates.txt`` (Layer 3). Collects
malformed lines instead of raising so an operator can fix every typo
in one cycle.

:func:`format_triage_file` — render a :class:`StationAttributeDateReport`
as an operator-editable action file. ACTION lines are emitted commented
out by default; the operator uncomments the entries to apply.

:func:`classification_for` — resolve a code's classification for a given
device subtype (handles the polymorphic scalar-or-per-subtype-dict shape).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .api.tos_client import TOSClient
from .audit import (
    GPS_DEVICE_SUBTYPES,
    _resolve_station_entity,
    canonical_subtype,
)

# ---------------------------------------------------------------------------
# Catalog + suppressions paths
# ---------------------------------------------------------------------------

# The repo-root data file is the canonical location. Editable installs
# (``pip install -e .``) reach it via ``src/tostools/<this>.py`` →
# parent.parent.parent. Override with the env var (e.g. for CI) or the
# ``--catalog`` CLI flag.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CATALOG_PATH = _REPO_ROOT / "data" / "attribute_codes.yaml"
CATALOG_ENV_VAR = "TOSTOOLS_ATTRIBUTE_CODES_PATH"

# Layer 3 — committed-in-repo suppression file. Operator-edited; one
# ``SUPPRESS <id_entity> <code> <date_from>`` per known-good entry.
# ``date_from`` is normalised to ``YYYY-MM-DD`` on parse so a
# copy-pasted ``2014-10-17 00:00:00`` matches the date-only form the
# audit uses internally.
DEFAULT_SUPPRESSIONS_PATH = (
    _REPO_ROOT / "data" / "audit_suppressions" / "attribute_dates.txt"
)

# (id_entity, code, date_from) — date_from is always 10-char date-only.
SuppressionKey = Tuple[int, str, str]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributeDateViolation:
    """One attribute period whose ``date_from`` violates rule 3.

    The ``(id_entity, code, date_from)`` triple is the natural suppression
    key — Layer 3 (suppression file) will key off this triple.
    """

    id_entity: int
    subtype: str
    serial: Optional[str]
    code: str
    date_from: str
    value: Optional[str]
    earliest_known: str
    # Which anchor determined ``earliest_known``:
    # "join"  — the station-side join is earlier than every attribute date
    # "attribute" — an attribute period is earlier (or tied with the join)
    anchor_source: str


@dataclass
class SuppressionParseError:
    """One malformed line in the suppression file.

    Mirrors :class:`tostools.tos.ParseError` in spirit — collected so the
    operator can fix every typo in one cycle, rather than fix-rerun-fix.
    """

    line_no: int
    message: str
    raw: str


@dataclass(frozen=True)
class SuppressedEntry:
    """A rule-3 hit that was filtered out by the suppression file.

    Carried on the report so ``--verbose`` can show what's being silenced
    — the suppression file is the only audit trail otherwise, and a typo
    in a SUPPRESS line could mask a real violation for months.
    """

    violation: AttributeDateViolation
    suppressions_path: Path
    line_no: int


@dataclass
class StationAttributeDateReport:
    """Result of :func:`audit_station_attribute_dates`."""

    station_id: int
    station_name: Optional[str]
    audited_devices: int = 0
    devices_skipped: int = 0
    unknown_codes: List[str] = field(default_factory=list)
    violations: List[AttributeDateViolation] = field(default_factory=list)
    suppressed: List[SuppressedEntry] = field(default_factory=list)
    suppressions_path: Optional[Path] = None
    suppressions_errors: List[SuppressionParseError] = field(default_factory=list)
    suppressions_disabled: bool = False
    # Layer 5 — per-code filter overrides.
    included_codes: List[str] = field(default_factory=list)
    excluded_codes: List[str] = field(default_factory=list)
    # Codes in ``included_codes`` that didn't match any attribute on any
    # audited device. Surfaces typos / wrong-station mistakes that
    # validation alone can't catch (the code is real, but no device has it).
    included_codes_unmatched: List[str] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        """True when at least one violation survived the suppression filter."""
        return bool(self.violations)

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed)


# ---------------------------------------------------------------------------
# Catalog loading + classification
# ---------------------------------------------------------------------------


SCOPES: Tuple[str, ...] = ("devices", "locations", "stations")


def _resolve_catalog_path(path: Optional[Path]) -> Path:
    if path is not None:
        return path
    env_path = os.environ.get(CATALOG_ENV_VAR)
    return Path(env_path) if env_path else DEFAULT_CATALOG_PATH


def load_catalog_scoped(
    path: Optional[Path] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Load ``attribute_codes.yaml`` preserving the three scopes.

    Returns ``{scope: {code: entry}}`` with ``_scope`` attached to each
    entry for parity with :func:`load_catalog`. Use this when iterating
    rules per-entity-type — the station entity walks ``catalog['stations']``
    and each device walks ``catalog['devices']``, so cross-scope code
    collisions (``subtype``, ``date_start``, ``lat``, …) stay distinct.

    Path resolution: explicit ``path`` arg → env
    ``TOSTOOLS_ATTRIBUTE_CODES_PATH`` → :data:`DEFAULT_CATALOG_PATH`.

    Raises :class:`FileNotFoundError` if no catalog is reachable.
    """
    resolved = _resolve_catalog_path(path)
    with open(resolved, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    scoped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for scope in SCOPES:
        scoped[scope] = {}
        for code, entry in (data.get(scope) or {}).items():
            merged = dict(entry)
            merged["_scope"] = scope
            scoped[scope][code] = merged
    return scoped


def load_catalog(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load ``attribute_codes.yaml`` and flatten the three scopes into a
    single ``{code: entry}`` map.

    Path resolution: explicit ``path`` arg → env ``TOSTOOLS_ATTRIBUTE_CODES_PATH``
    → :data:`DEFAULT_CATALOG_PATH`. The devices/locations/stations scopes
    are merged with devices winning on collision; each entry carries a
    ``_scope`` key so callers can disambiguate when relevant.

    Use :func:`load_catalog_scoped` when the caller must distinguish
    cross-scope collisions (e.g. ``subtype`` exists on both stations and
    devices); the flat view is suitable for Layers 2-5 which only iterate
    device-scope rules.

    Raises :class:`FileNotFoundError` if no catalog is reachable —
    audit cannot run without it.
    """
    scoped = load_catalog_scoped(path)
    flat: Dict[str, Dict[str, Any]] = {}
    for scope in SCOPES:
        for code, entry in scoped[scope].items():
            if code in flat:
                continue
            flat[code] = entry  # already carries _scope
    return flat


def validate_codes_against_catalog(
    codes: Sequence[str],
    catalog: Dict[str, Dict[str, Any]],
    *,
    flag_label: str = "code",
) -> None:
    """Raise ValueError on the first unknown code, with did-you-mean.

    Used by ``--include`` and ``--exclude`` to fail fast on typos.
    ``flag_label`` lets the caller customise the error message
    (``"--include code"``, ``"--exclude code"``, ...).

    Empty input is a no-op — caller validates against an empty sequence
    when the flag wasn't passed.
    """
    import difflib

    known = set(catalog.keys())
    for code in codes:
        if code in known:
            continue
        suggestions = difflib.get_close_matches(code, known, n=3, cutoff=0.6)
        if suggestions:
            hint = f" Did you mean: {', '.join(suggestions)}?"
        else:
            hint = (
                " Run with no --include/--exclude or consult "
                "data/attribute_codes.yaml for the catalog."
            )
        raise ValueError(f"Unknown {flag_label}: {code!r}.{hint}")


def classification_for(entry: Dict[str, Any], subtype: str) -> Optional[str]:
    """Resolve a catalog entry's classification for one device subtype.

    Returns ``"inherent"`` / ``"mutable"`` when the code applies; ``None``
    when:

    * classification is ``"TODO"`` (operator hasn't reviewed yet)
    * the code is a per-subtype dict and ``subtype`` isn't a key
    * the code is scalar but its ``applies_to`` excludes ``subtype``
      (rare — TOS data hygiene issue, but skip rather than misclassify)
    """
    cls = entry.get("classification")
    if cls is None or cls == "TODO":
        return None
    if isinstance(cls, dict):
        resolved = cls.get(subtype)
        return str(resolved) if resolved else None
    applies_to = entry.get("applies_to") or []
    if applies_to and subtype not in applies_to:
        return None
    return str(cls)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _date_only(s: str) -> str:
    """Return the ``YYYY-MM-DD`` prefix of an ISO date or datetime.

    TOS mixes ``"2002-01-01"`` and ``"2002-01-01 00:00:00"`` in the same
    payloads. Comparing the full strings lexically misorders the two —
    ``"2002-01-01" < "2002-01-01 00:00:00"`` — so we normalise to the
    date portion before comparing. Rule 3 fires on gross misdating
    (months / years), so time-of-day precision is intentionally dropped.
    """
    return s[:10]


def _earliest_attribute_date(history: Dict[str, Any]) -> Optional[str]:
    """Return the earliest ``date_from`` across every attribute period.

    None when the device has no attributes with date_from (unusual but
    possible during partial TOS imports). Already date-only normalised so
    the caller can compare against the join anchor directly.
    """
    earliest: Optional[str] = None
    for a in history.get("attributes") or []:
        df = a.get("date_from")
        if not df:
            continue
        df_d = _date_only(str(df))
        if earliest is None or df_d < earliest:
            earliest = df_d
    return earliest


def _station_joins_by_device(
    station_history: Dict[str, Any],
) -> Dict[int, List[Dict[str, Any]]]:
    """Group ``children_connections`` by ``id_entity_child``.

    Devices can have multiple joins to the same station (came back after
    a stint elsewhere); preserve all of them so the caller can pick the
    earliest ``time_from`` as the anchor.
    """
    out: Dict[int, List[Dict[str, Any]]] = {}
    for conn in station_history.get("children_connections") or []:
        cid = conn.get("id_entity_child")
        if cid is None:
            continue
        out.setdefault(int(cid), []).append(conn)
    return out


def _sorted_sessions(
    joins: Sequence[Dict[str, Any]],
) -> List[Tuple[str, Optional[str]]]:
    """Join windows as ``[(time_from, time_to|None), ...]``, ascending.

    Each tuple is a device↔station occupation window, date-only normalised
    (``YYYY-MM-DD``); ``time_to`` is ``None`` for the currently-open join.
    Joins without a ``time_from`` are dropped (nothing to anchor to). The
    :func:`_session_anchor_for` lookup relies on the ascending sort.
    """
    out: List[Tuple[str, Optional[str]]] = []
    for j in joins:
        tf_raw = j.get("time_from")
        if not tf_raw:
            continue
        tf = _date_only(str(tf_raw))
        tt_raw = j.get("time_to")
        tt = _date_only(str(tt_raw)) if tt_raw else None
        out.append((tf, tt))
    out.sort(key=lambda s: s[0])
    return out


def _session_anchor_for(
    df: str, sessions: Sequence[Tuple[str, Optional[str]]]
) -> Optional[str]:
    """The ``time_from`` of the join session that contains ``df``.

    Rule 3 is *session-scoped*: an attribute period is only measured
    against the window the device was actually at this station. Returns the
    containing session's ``time_from`` when ``tf <= df < tt`` (or
    ``tf <= df`` for an open session). Returns ``None`` when ``df`` falls
    **before** the device's first join or **inside a gap** between two
    joins — that is off-station / pre-join history (e.g. a manufacture-
    dated serial, or the device's tenure at a previous station) which must
    NOT be flagged against this station. ``sessions`` must be ascending by
    ``time_from`` (see :func:`_sorted_sessions`).
    """
    for tf, tt in sessions:
        if df < tf:
            # Ascending sort: df precedes this session and every later one.
            return None
        if tt is None or df < tt:
            return tf
        # df >= tt: this closed session ended before df; try the next one.
    return None


def _open_attribute_value(history: Dict[str, Any], code: str) -> Optional[str]:
    """Mirror of :func:`tostools.devices.open_attribute` (avoids the import)."""
    for a in history.get("attributes") or []:
        if a.get("code") != code:
            continue
        if a.get("date_to") is not None:
            continue
        v = a.get("value")
        if v is not None:
            return str(v)
    return None


def _station_display_name(
    station_history: Dict[str, Any], fallback: Optional[str]
) -> Optional[str]:
    """Pull a human label for the station — open ``name`` attribute, else
    fall back to whatever the operator typed."""
    name = _open_attribute_value(station_history, "name")
    return name or fallback


# ---------------------------------------------------------------------------
# Suppression file parsing (Layer 3)
# ---------------------------------------------------------------------------


def load_suppressions(
    path: Optional[Path] = None,
) -> Tuple[Dict[SuppressionKey, int], List[SuppressionParseError], Path]:
    """Parse an ACTION-style suppression file.

    Format: one ``SUPPRESS <id_entity> <code> <date_from>`` per line.
    Comments start with ``#`` and run to end-of-line; blank lines are
    ignored. The date is normalised to ``YYYY-MM-DD`` on parse, so a
    pasted ``2014-10-17 00:00:00`` lines up with the date-only form the
    audit uses internally.

    Returns ``(suppressions, errors, resolved_path)``:

    * ``suppressions`` — ``{(id_entity, code, date_from): line_no}``
      mapping. ``line_no`` is kept so verbose reporting can show which
      file line silenced each entry.
    * ``errors`` — collected malformed lines; the caller decides whether
      to abort or continue with the parsed entries. Mirrors the
      collect-and-report-all pattern from :func:`_parse_action_file`.
    * ``resolved_path`` — the path actually tried (the default location
      or the explicit override). Useful for error / verbose output.

    File-not-found is NOT an error. Returns an empty mapping and empty
    error list. The suppression file is opt-in.
    """
    if path is None:
        path = DEFAULT_SUPPRESSIONS_PATH

    suppressions: Dict[SuppressionKey, int] = {}
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
                        f"expected line to start with 'SUPPRESS' "
                        f"(got {tokens[0]!r})"
                    ),
                    raw=raw,
                )
            )
            continue
        if len(tokens) < 4:
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=(
                        "SUPPRESS line requires 3 arguments: "
                        f"<id_entity> <code> <date_from> (got {len(tokens) - 1})"
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
        # Reject pasted ISO datetimes that the operator split on whitespace
        # by accident — ``SUPPRESS 4773 serial_number 2014-10-17 00:00:00``
        # parses cleanly because tokens[3] = "2014-10-17"; that's
        # intentional. But ``2014-10-17T00:00:00`` is one token; normalise
        # to date-only.
        date_from = _date_only(tokens[3])
        if len(date_from) != 10 or date_from[4] != "-" or date_from[7] != "-":
            errors.append(
                SuppressionParseError(
                    line_no=i,
                    message=(f"date_from must be YYYY-MM-DD (got {tokens[3]!r})"),
                    raw=raw,
                )
            )
            continue
        suppressions[(id_entity, code, date_from)] = i

    return suppressions, errors, path


# ---------------------------------------------------------------------------
# Main audit entry point
# ---------------------------------------------------------------------------


def audit_station_attribute_dates(
    client: TOSClient,
    *,
    name: Optional[str] = None,
    id_entity: Optional[int] = None,
    subtypes: Optional[Sequence[str]] = None,
    include_mutable: bool = False,
    include_codes: Optional[Sequence[str]] = None,
    exclude_codes: Optional[Sequence[str]] = None,
    catalog_path: Optional[Path] = None,
    suppressions_path: Optional[Path] = None,
    use_suppressions: bool = True,
) -> StationAttributeDateReport:
    """Apply rule 3 to every device joined to a station.

    Parameters
    ----------
    client
        Unauthenticated :class:`TOSClient`. No writes; basic_search +
        get_entity_history only.
    name / id_entity
        Station identifier — pass one or the other. ``name`` is the
        4-letter marker (RHOF, ARHO) or full display name; resolution
        delegates to :func:`tostools.audit._resolve_station_entity`,
        which prefers markers and disambiguates Jarðeðlisstöð collisions.
    subtypes
        Device subtypes to audit. Defaults to the GPS quartet
        (:data:`tostools.audit.GPS_DEVICE_SUBTYPES`). Pass canonical or
        short forms (resolved via :func:`canonical_subtype`).
    include_mutable
        When False (default), only ``inherent`` attribute codes are
        checked — firmware bumps and other mutable transitions are
        skipped. Set True to surface every mismatched date_from for
        debugging.
    include_codes
        Per-code override. Any code in this list is audited regardless
        of its classification (mutable, TODO, applies_to mismatch,
        gps_relevance=no). The surgical alternative to
        ``include_mutable``: instead of "audit every mutable",
        ``include_codes=["owner"]`` audits exactly the codes you name.
        Validated against the catalog at call time — unknown codes
        raise :class:`ValueError` with did-you-mean suggestions.
    exclude_codes
        Per-code skip. Any code in this list is dropped before any
        other filter runs — not flagged, not even tracked as
        suppressed. Useful as a station-wide silencer (coarser than
        the per-violation suppression file). On conflict with
        ``include_codes`` for the same code, exclude wins. Same
        catalog validation as ``include_codes``.
    catalog_path
        Override the catalog file location. Defaults to
        :data:`DEFAULT_CATALOG_PATH` or the ``TOSTOOLS_ATTRIBUTE_CODES_PATH``
        env var.
    suppressions_path
        Override the suppression file location. Defaults to
        :data:`DEFAULT_SUPPRESSIONS_PATH`. File-not-found is silent
        (the file is opt-in).
    use_suppressions
        When False, skip the suppression file entirely — every rule-3
        hit lands in ``violations``. Equivalent to ``--no-suppressions``
        on the CLI.

    Returns
    -------
    StationAttributeDateReport
        ``has_violations`` reflects the **filtered** violations list —
        a suppression covering every rule-3 hit produces a clean report.
        The suppressed entries are preserved on ``report.suppressed`` so
        verbose output can show what was silenced (a stale or wrong
        SUPPRESS line is otherwise easy to miss). Unknown attribute
        codes accumulate in ``unknown_codes`` for operator follow-up.

    Raises
    ------
    LookupError
        Station not found.
    ValueError
        Invalid subtype name, neither ``name`` nor ``id_entity`` set,
        or an unknown code passed to ``include_codes`` / ``exclude_codes``.
    FileNotFoundError
        Catalog file missing (suppression file missing is not an error).
    """
    catalog = load_catalog(catalog_path)

    # Layer 5 — validate before any TOS reads. Unknown codes fail fast
    # with did-you-mean suggestions; catches typos at the boundary.
    include_set = set(include_codes or [])
    exclude_set = set(exclude_codes or [])
    if include_set:
        validate_codes_against_catalog(
            sorted(include_set), catalog, flag_label="--include code"
        )
    if exclude_set:
        validate_codes_against_catalog(
            sorted(exclude_set), catalog, flag_label="--exclude code"
        )
    # Exclude wins on conflict — the more conservative choice. Document
    # this contract by removing collisions from include_set up front;
    # the inner loop then only needs to check exclude first.
    include_set -= exclude_set
    # Track which include codes actually matched something so we can
    # warn about silent-no-ops (valid code, no device has it on this
    # station — typo or wrong station, validation can't tell).
    include_matched: set[str] = set()

    if use_suppressions:
        suppressions, supp_errors, supp_path = load_suppressions(suppressions_path)
    else:
        # Empty result; supp_path retains whatever was requested so verbose
        # output can still report "suppressions disabled (would have read X)".
        suppressions = {}
        supp_errors = []
        supp_path = suppressions_path or DEFAULT_SUPPRESSIONS_PATH

    if subtypes:
        wanted = tuple(canonical_subtype(s) for s in subtypes)
    else:
        wanted = GPS_DEVICE_SUBTYPES

    station_history = _resolve_station_entity(client, name=name, id_entity=id_entity)
    station_id = int(station_history["id_entity"])
    station_name = _station_display_name(station_history, name)

    report = StationAttributeDateReport(
        station_id=station_id,
        station_name=station_name,
        suppressions_path=supp_path,
        suppressions_errors=supp_errors,
        suppressions_disabled=not use_suppressions,
        included_codes=sorted(include_set),
        excluded_codes=sorted(exclude_set),
    )
    unknown_codes_seen: set[str] = set()

    joins_by_device = _station_joins_by_device(station_history)

    for device_id, joins in joins_by_device.items():
        history = client.get_entity_history(device_id)
        if not history:
            continue

        dev_subtype = history.get("code_entity_subtype")
        if dev_subtype not in wanted:
            report.devices_skipped += 1
            continue
        report.audited_devices += 1

        # Session anchor (rule 3 is session-scoped): each attribute period is
        # measured against the join window it falls in — NOT the device's
        # global-earliest attribute date. A device that lived at another
        # station, or carries a manufacture-dated identity attribute, before
        # joining THIS station must not have its at-join attributes measured
        # against that pre-join history. That old ``min(earliest_attr,
        # earliest_join)`` anchor produced false positives: a correctly-dated
        # at-join toggle (e.g. a moved receiver's GPS period stamped at the
        # real install) was flagged and "corrected" to a date before the
        # device ever arrived. The station-side join ``time_from`` is the
        # physical "device present here" date and the only sound anchor.
        sessions = _sorted_sessions(joins)
        if not sessions:
            continue

        open_serial = _open_attribute_value(history, "serial_number")

        for attr in history.get("attributes") or []:
            code_raw = attr.get("code")
            if not code_raw:
                continue
            code = str(code_raw)

            # Layer 5 filter order:
            #   1. --exclude wins absolutely — drop before catalog lookup.
            #   2. --include bypasses every catalog-based skip (mutable
            #      gate, TODO, applies_to, gps_relevance) but still
            #      requires the code to exist in the catalog (validation
            #      enforced this above). Track matches so we can warn
            #      about silent-no-ops at the end of the audit.
            #   3. Default path: catalog lookup + classification filter
            #      + gps_relevance filter, same as before Layer 5.
            if code in exclude_set:
                continue

            entry = catalog.get(code)
            if entry is None:
                unknown_codes_seen.add(code)
                continue

            if code in include_set:
                include_matched.add(code)
            else:
                if entry.get("gps_relevance") != "yes":
                    continue
                cls = classification_for(entry, str(dev_subtype))
                if cls is None:
                    continue
                if cls == "mutable" and not include_mutable:
                    continue

            df_raw = attr.get("date_from")
            if not df_raw:
                continue
            df = _date_only(str(df_raw))
            anchor = _session_anchor_for(df, sessions)
            if anchor is None:
                # df precedes the device's first join to this station, or
                # falls inside a gap between joins — off-station / pre-join
                # history, not a rule-3 artifact for this station.
                continue
            if df <= anchor:
                continue

            value_raw = attr.get("value")
            value = str(value_raw) if value_raw is not None else None

            violation = AttributeDateViolation(
                id_entity=device_id,
                subtype=str(dev_subtype),
                serial=open_serial,
                code=code,
                date_from=df,
                value=value,
                earliest_known=anchor,
                # Session-anchored: the join window's time_from is always the
                # reference now (never a device attribute date).
                anchor_source="join",
            )
            supp_line = suppressions.get((device_id, code, df))
            if supp_line is not None:
                report.suppressed.append(
                    SuppressedEntry(
                        violation=violation,
                        suppressions_path=supp_path,
                        line_no=supp_line,
                    )
                )
            else:
                report.violations.append(violation)

    report.unknown_codes = sorted(unknown_codes_seen)
    # Layer 5 — any --include code that never touched a single device's
    # attribute is a silent-no-op signal worth surfacing.
    report.included_codes_unmatched = sorted(include_set - include_matched)
    # Stable sort by (device, code, date_from) for deterministic output.
    report.violations.sort(key=lambda v: (v.id_entity, v.code, v.date_from))
    report.suppressed.sort(
        key=lambda s: (s.violation.id_entity, s.violation.code, s.violation.date_from)
    )
    return report


# ---------------------------------------------------------------------------
# Triage file emission (Layer 4)
# ---------------------------------------------------------------------------


def format_triage_file(
    report: StationAttributeDateReport,
    *,
    audit_command: Optional[str] = None,
    generated_at: Optional[str] = None,
    apply_path: Optional[str] = None,
) -> str:
    """Render *report* as an operator-editable triage file.

    The output is an ACTION-style file consumable by ``tos audit apply``.
    Every violation produces one block:

    * a header comment with the device / violation context
    * a single ACTION line, **commented out by default**, that would
      PATCH the period's ``date_from`` to ``earliest_known`` if applied

    The operator reviews each block, uncomments the ACTION lines that
    should fire, optionally edits the ``new_date_from`` argument, and
    feeds the file back to ``tos audit apply --apply``.

    Parameters
    ----------
    report
        The audit report. Only ``report.violations`` is consulted —
        suppressed entries are intentionally NOT emitted (they're
        already silenced by the suppression file).
    audit_command
        Optional command-line string captured at audit time; rendered
        in the file header so the file is self-documenting. Pass the
        actual argv joined with spaces, or a paraphrase like
        ``"tos audit attribute-dates ARHO"``.
    generated_at
        Optional ISO timestamp. Defaults to ``datetime.utcnow().isoformat()``
        at call time. Pass an explicit value in tests to keep output
        byte-deterministic.
    apply_path
        Optional path string (as the operator would type it — a relative
        path resolves under the gps-tos-corrections repo) rendered into the
        header's concrete ``tos audit apply`` commands. When None the header
        shows a ``<file>`` placeholder.

    Returns
    -------
    str
        Newline-terminated file contents, safe to write directly with
        :meth:`pathlib.Path.write_text`.

    Notes
    -----
    Violations are emitted in the report's natural sort order
    (``id_entity, code, date_from``) — re-running ``--triage`` on the
    same station produces a byte-identical file unless TOS changed,
    so operators can commit the triage file alongside the suppression
    file and audit decisions over time.
    """
    from datetime import datetime, timezone

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines: List[str] = []
    station_label = report.station_name or "<unknown>"
    lines.append("# === tos audit attribute-dates — triage action file ===")
    lines.append(f"# Generated:  {generated_at}")
    lines.append(f"# Station:    {station_label!r} (id_entity={report.station_id})")
    if audit_command:
        lines.append(f"# Audit cmd:  {audit_command}")
    lines.append(f"# Violations: {len(report.violations)}")
    lines.append("#")
    lines.append("# Format: one ACTION per line, '#' for comments.")
    lines.append("#")
    lines.append("#   ACTION <id_entity> patch-attribute-date \\")
    lines.append("#          <code> <old_date_from> <new_date_from>")
    lines.append("#")
    ref = apply_path or "<file>"
    lines.append("# Workflow:")
    lines.append("#   1. Review each block below — verify the suggested")
    lines.append("#      new_date_from is correct. Edit if needed.")
    lines.append("#   2. Uncomment the ACTION line(s) you want to fire.")
    lines.append(f"#   3. tos audit apply {ref}                  # dry-run preview")
    lines.append(
        f"#   4. tos audit apply {ref} --apply --commit  # write to TOS + record"
    )
    lines.append("#      the applied file in the gps-tos-corrections repo (--commit;")
    lines.append("#      best-effort, never pushes — `git push` it yourself).")
    lines.append("#")
    lines.append("# Alternative for known-good entries: copy the SUPPRESS hint into")
    lines.append("# data/audit_suppressions/attribute_dates.txt instead.")
    lines.append("")

    if not report.violations:
        lines.append("# (no violations — nothing to triage)")
        lines.append("")
        return "\n".join(lines)

    # Group by device — keeps each device's violations together while
    # preserving the report's overall (id_entity, code, date_from) sort.
    by_device: Dict[int, List[AttributeDateViolation]] = {}
    device_meta: Dict[int, Any] = {}
    for v in report.violations:
        by_device.setdefault(v.id_entity, []).append(v)
        device_meta[v.id_entity] = (v.subtype, v.serial)

    for did in sorted(by_device):
        subtype, serial = device_meta[did]
        serial_label = f" SN {serial!r}" if serial else ""
        lines.append(f"# --- {subtype} id_entity={did}{serial_label} ---")
        for v in by_device[did]:
            value_part = f"  value={v.value!r}" if v.value is not None else ""
            lines.append(
                f"# violation: {v.code} date_from={v.date_from}"
                f"  (earliest_known={v.earliest_known},"
                f" anchor={v.anchor_source}){value_part}"
            )
            lines.append(
                f"#ACTION {v.id_entity} patch-attribute-date "
                f"{v.code} {v.date_from} {v.earliest_known}"
            )
            lines.append(
                f"# (or suppress: SUPPRESS {v.id_entity} " f"{v.code} {v.date_from})"
            )
            lines.append("")

    return "\n".join(lines)
