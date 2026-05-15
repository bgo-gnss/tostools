"""Small composable read/write helpers for TOS device entities.

In the style of the legacy ``tostools.tos`` module — each function does
**one** thing on a single concern (lookup, read attribute, fetch
timeline, render). Higher-level workflows
(:func:`tostools.tos.display_device_record`, the ``tos audit apply``
verbs, ``receivers cfg move``) compose these primitives instead of
duplicating them.

Design rules
------------
- Read primitives accept a :class:`TOSClient` (or operate purely on an
  already-fetched history dict). Write primitives accept a
  :class:`TOSWriter`. Cross-layer composites take both — these are
  the only primitives that do.
- Subtype names are the **canonical TOS code** (e.g. ``digitizer``,
  ``gps_clock``, ``gnss_receiver``) — not the GPS-only short aliases
  used by ``audit.SUBTYPE_ALIASES``. Use this module when you need to
  touch the broader fleet (seismic digitisers, weather sensors, etc.).
- Read functions return plain dicts (the TOS payload), so callers can
  pick the fields they care about without a heavy data model.
- Write functions return the TOS response or
  :class:`tostools.api.tos_writer.DryRunResult` in dry-run mode.

See vault note ``1778713245-tostools-devices-design`` for the design
proposal that motivates the API surface here, especially §1b
(division of labour with ``receivers``) and §3 (signatures).

API surface
-----------
Lookup:

* :func:`find_device` — resolve a device by id or (serial, subtype) →
  full history dict.

Attribute helpers (pure, operate on an already-fetched history dict):

* :func:`attribute_periods` — group all periods by ``code``, sorted
  chronologically.
* :func:`open_attribute` — the value of the currently-open period for
  ``code``, or ``None``.
* :func:`attribute_at` — the period covering a given date, or
  ``None``.
* :func:`attribute_at_value` — the ``.value`` of
  :func:`attribute_at`, or ``None``.

Join helpers (pure unless otherwise noted):

* :func:`child_joins` — the children connections of a station, sorted.
* :func:`parent_joins` — the parent connections of a device, sorted.
* :func:`open_joins` — filter either set to those still open.
* :func:`device_timeline` — full chronological join history for one
  device (builds the global join index — slow; cache the result).

Write — joins layer:

* :func:`open_join` — POST a new parent → child join.
* :func:`close_join` — PATCH a join's ``time_to``.
* :func:`fill_join_gap` — POST a closed join for a known historical
  window (cfg-fix backfill).

Write — attributes layer:

* :func:`set_attribute` — POST a new attribute period.
* :func:`end_attribute` — PATCH an existing period to set
  ``date_to``.
* :func:`correct_attribute` — PATCH an existing period in place
  (history-destructive; use sparingly).
* :func:`transition_attribute` — close the open period and open a new
  one on the same date (history-preserving).
* :func:`set_open_attribute` — idempotent set of the open period;
  PATCH if value differs, else POST. History-destructive on PATCH.

Session composers (§3e):

* :func:`device_sessions` — per-join sub-sessions for a station's
  tracked children (replaces ``gps_metadata_qc.get_device_sessions``).
* :func:`station_sessions` — pivot ``device_sessions`` into
  per-station-session rows (replaces
  ``gps_metadata_qc.get_device_history``).
* :func:`station_at` — convenience wrapper returning the
  ``station_sessions`` row covering a given date.

Joins-layer composite (§3f):

* :func:`move_device` — close one open join and open a new one at a
  different parent on the same date (relocation).

Cross-layer composites (§3h):

* :func:`decommission_device` — retire a device: close every open
  parent join + transition status virkt→óvirkt.
* :func:`install_device` — activate a device: open the parent→device
  join + apply status / initial attributes idempotently.
* :func:`replace_device` — swap one device for another at the same
  parent on a given date (decommission_device + install_device).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .api.tos_client import TOSClient
from .api.tos_writer import TOSWriter


def find_device(
    client: TOSClient,
    *,
    serial: Optional[str] = None,
    id_entity: Optional[int] = None,
    subtype: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a device entity by id or by (serial, subtype).

    Two lookup paths:

    * ``id_entity`` (preferred when known): one direct
      ``GET /history/entity/<id>/`` call.
    * ``(serial, subtype)``: ``basic_search`` for the serial, filter to
      exact ``code='serial_number'`` matches, then verify each
      candidate's ``code_entity_subtype`` via the history endpoint.

    Unlike :func:`tostools.audit.audit_device`, ``subtype`` is taken
    verbatim — any TOS-canonical subtype is valid (``digitizer``,
    ``gps_clock``, ``seismometer``, ``thermometer_mercury``, ...). The
    full subtype list lives in vault note ``1778677922-tos-entity-
    subtype-codes`` and is also available via
    ``client._make_request('/entity_subtypes/')``.

    Args:
        client: An unauthenticated :class:`TOSClient`.
        serial: Device serial number; requires ``subtype``.
        id_entity: Device primary key.
        subtype: Required with ``serial``. The canonical TOS code, not
            the GPS-only short alias.

    Returns:
        The full history dict as returned by TOS (``id_entity``,
        ``code_entity_subtype``, ``attributes``, ``children_connections``,
        ``id_entity_parent``, etc.).

    Raises:
        ValueError: insufficient arguments (neither id nor
            serial+subtype).
        LookupError: nothing matched.
    """
    if id_entity is not None:
        history = client.get_entity_history(int(id_entity))
        if not history:
            raise LookupError(f"No entity with id_entity={id_entity}")
        return history
    if not serial:
        raise ValueError("find_device requires either id_entity or serial")
    if not subtype:
        raise ValueError("find_device requires subtype when resolving by serial")
    for hit in client.basic_search(serial):
        if hit.get("code") != "serial_number":
            continue
        if hit.get("distance") not in (0, None):
            continue
        if hit.get("value_varchar") != serial:
            continue
        candidate_id = hit.get("id_lvl_three") or hit.get("id_entity")
        if not candidate_id:
            continue
        history = client.get_entity_history(int(candidate_id))
        if history and history.get("code_entity_subtype") == subtype:
            return history
    raise LookupError(f"No {subtype} with serial {serial!r}")


def attribute_periods(history: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Group every attribute period by ``code`` and sort chronologically.

    Returns ``{code: [period_dict, ...]}``. Within each list, periods
    are ordered by ``date_from`` ascending — closed periods first, then
    the open one (if any) at the end. Each period dict is the raw TOS
    payload (carries ``date_from``, ``date_to``, ``value``,
    ``id_attribute_value``, ...).

    Useful for "show me every status / firmware / model transition for
    this device" — :func:`tostools.tos.display_device_record` renders
    each code's periods as a small table.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for a in history.get("attributes") or []:
        code = str(a.get("code") or "?")
        out.setdefault(code, []).append(a)
    for code in out:
        out[code].sort(key=lambda x: x.get("date_from") or "")
    return out


def open_attribute(history: Dict[str, Any], code: str) -> Optional[str]:
    """Return the value of the currently-open period for ``code``.

    "Open" means ``date_to is None``. Returns ``None`` when no such
    period exists (the attribute was never set, or every period has
    been closed — e.g. a status=óvirkt transition leaves no open
    ``virkt`` period).
    """
    for a in history.get("attributes") or []:
        if a.get("code") != code:
            continue
        if a.get("date_to") is not None:
            continue
        v = a.get("value")
        if v is not None:
            return str(v)
    return None


def device_timeline(
    client: TOSClient,
    id_entity: int,
    *,
    parents: Optional[Any] = None,
):
    """Return the full chronological join history of one device.

    Wrapper around :func:`tostools.history.build_join_index` +
    :meth:`JoinIndex.timeline`. **Slow** — ~110s on the IMO fleet for
    the parent enumeration and walk. Pass a pre-built ``parents`` list
    (from :func:`enumerate_known_parents`) if you're querying multiple
    devices in one session to skip the marker-resolution step.

    Returns a :class:`tostools.history.DeviceTimeline` — read its
    ``joins``, ``open_joins``, ``is_truly_orphan`` properties; or call
    ``timeline.gaps(min_days=...)`` for gap detection.
    """
    from .history import build_join_index, enumerate_known_parents

    if parents is None:
        parents = enumerate_known_parents(client)
    index = build_join_index(client, parents=parents)
    return index.timeline(int(id_entity))


# ---------------------------------------------------------------------------
# Read — attribute helpers (point-in-time, pure)
# ---------------------------------------------------------------------------


def _normalise_iso_for_compare(s: str) -> str:
    """Promote bare ``YYYY-MM-DD`` to ``YYYY-MM-DDT00:00:00`` for lexical
    comparison against TOS-stored datetimes.

    TOS persists every date as a full datetime — :meth:`TOSWriter._tos_date`
    promotes bare dates on write. Operators query with bare dates, so the
    query side has to mirror that promotion or the boundary case at
    midnight flips:
    ``"2026-05-13" < "2026-05-13T00:00:00"`` lexically, which would cause
    a status transition exactly on 2026-05-13 to return the closed period
    instead of the freshly-opened one.

    Strips a trailing ``+HH:MM`` / ``-HH:MM`` / ``Z`` offset for the same
    reason TOS does — defensive, in case a caller passes a tz-aware
    timestamp from elsewhere.
    """
    import re as _re

    s = _re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", s)
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s = f"{s}T00:00:00"
    return s


def attribute_at(
    history: Dict[str, Any], code: str, when: str
) -> Optional[Dict[str, Any]]:
    """Return the attribute period covering ``when`` for ``code``, or None.

    Matching rule: a period covers ``when`` when
    ``date_from <= when`` AND (``date_to is None`` OR ``when < date_to``).

    Comparisons are lexical over ISO date/datetime strings. TOS stores
    every date as a full datetime (``YYYY-MM-DDT00:00:00``) — see
    :meth:`TOSWriter._tos_date`. To avoid a boundary-day flip when the
    caller passes bare ``YYYY-MM-DD``, ``when`` is normalised through
    :func:`_normalise_iso_for_compare` before comparison.

    Args:
        history: An entity history dict (as returned by
            :func:`find_device` or
            :meth:`TOSClient.get_entity_history`).
        code: Attribute code (e.g. ``"status"``, ``"firmware_version"``).
        when: ISO date or datetime string.

    Returns:
        The full period dict (``id_attribute_value``, ``date_from``,
        ``date_to``, ``value``, …) or ``None`` if no period for
        ``code`` covers ``when``.
    """
    if not when:
        raise ValueError("attribute_at requires a non-empty `when`")
    when_n = _normalise_iso_for_compare(when)
    for a in history.get("attributes") or []:
        if a.get("code") != code:
            continue
        df = a.get("date_from") or ""
        if df > when_n:
            continue
        dt = a.get("date_to")
        if dt is not None and dt <= when_n:
            continue
        return a
    return None


def attribute_at_value(history: Dict[str, Any], code: str, when: str) -> Optional[str]:
    """Convenience: the ``.value`` of :func:`attribute_at`, or None.

    Useful when you don't need the surrounding period metadata — just
    "what was the firmware on 2017-03-14?".
    """
    period = attribute_at(history, code, when)
    if period is None:
        return None
    v = period.get("value")
    return None if v is None else str(v)


# ---------------------------------------------------------------------------
# Read — join helpers (pure unless noted)
# ---------------------------------------------------------------------------


def child_joins(history: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the station's child connections, sorted by ``time_from``.

    Use when ``history`` is a station entity; each returned row
    carries ``id_entity_connection``, ``id_entity_child``,
    ``time_from``, ``time_to``. For a device's perspective use
    :func:`parent_joins`.

    Returns an empty list when the entity has no ``children_connections``
    key (e.g. you accidentally passed a device history).
    """
    joins = list(history.get("children_connections") or [])
    joins.sort(key=lambda j: j.get("time_from") or "")
    return joins


def parent_joins(history: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the device's parent connections, sorted by ``time_from``.

    Use when ``history`` is a device entity; tells you which parents
    this device has been attached to and when. For a station's
    perspective use :func:`child_joins`.

    Returns an empty list when the entity has no ``parent_connections``
    key.
    """
    joins = list(history.get("parent_connections") or [])
    joins.sort(key=lambda j: j.get("time_from") or "")
    return joins


def open_joins(history: Dict[str, Any], *, role: str) -> List[Dict[str, Any]]:
    """Filter joins to those with ``time_to is None`` (currently active).

    Args:
        history: An entity history dict.
        role: ``"parent"`` to filter children_connections (the entity
            is the parent), or ``"child"`` to filter parent_connections
            (the entity is the child).

    Returns:
        A list of open join dicts, sorted by ``time_from``.
    """
    if role == "parent":
        joins = child_joins(history)
    elif role == "child":
        joins = parent_joins(history)
    else:
        raise ValueError(f"open_joins: role must be 'parent' or 'child', got {role!r}")
    return [j for j in joins if j.get("time_to") is None]


# ---------------------------------------------------------------------------
# Write — joins layer
# ---------------------------------------------------------------------------


def open_join(
    writer: TOSWriter,
    *,
    parent_id: int,
    child_id: int,
    date_from: str,
) -> Any:
    """Open a new parent → child join starting on ``date_from``.

    Thin wrapper over :meth:`TOSWriter.create_entity_connection`. The
    join is created with ``time_to`` of ``None`` (still active).

    Args:
        writer: An authenticated :class:`TOSWriter`.
        parent_id: Parent entity id (e.g. station's ``id_entity``).
        child_id: Child entity id (e.g. receiver's ``id_entity``).
        date_from: ISO date or datetime — start of the join.

    Returns:
        The TOS response or :class:`DryRunResult`.
    """
    return writer.create_entity_connection(
        id_parent=int(parent_id),
        id_child=int(child_id),
        time_from=date_from,
        time_to=None,
    )


def close_join(
    writer: TOSWriter,
    *,
    id_connection: int,
    date_to: str,
) -> Any:
    """Close an existing join by setting its ``time_to`` to ``date_to``.

    Thin wrapper over
    :meth:`TOSWriter.patch_entity_connection` with ``time_to=...``.

    Args:
        writer: An authenticated :class:`TOSWriter`.
        id_connection: The join's primary key (``id_entity_connection``).
        date_to: ISO date or datetime — end of the join.

    Returns:
        The TOS response or :class:`DryRunResult`.
    """
    return writer.patch_entity_connection(int(id_connection), time_to=date_to)


def fill_join_gap(
    writer: TOSWriter,
    *,
    parent_id: int,
    child_id: int,
    date_from: str,
    date_to: str,
) -> Any:
    """Open a *closed* join for a known historical window.

    The backfill primitive — operator-facing verbs are
    ``receivers cfg fix-gap`` and ``tos audit apply fill-gap``. Unlike
    :func:`open_join`, this requires ``date_to`` because you're
    filling a gap in the timeline, not creating a still-active join.

    Args:
        writer: An authenticated :class:`TOSWriter`.
        parent_id: Backfill parent (often B9 placeholder or a real
            station resolved from cold archive RINEX serials).
        child_id: Device whose timeline gets the gap filled.
        date_from: ISO date or datetime — start of the gap.
        date_to: ISO date or datetime — end of the gap.

    Returns:
        The TOS response or :class:`DryRunResult`.
    """
    if not date_to:
        raise ValueError(
            "fill_join_gap requires date_to — use open_join for " "still-active joins"
        )
    return writer.create_entity_connection(
        id_parent=int(parent_id),
        id_child=int(child_id),
        time_from=date_from,
        time_to=date_to,
    )


# ---------------------------------------------------------------------------
# Write — attributes layer
# ---------------------------------------------------------------------------


def set_attribute(
    writer: TOSWriter,
    *,
    device_id: int,
    code: str,
    value: str,
    date_from: str,
    date_to: Optional[str] = None,
) -> Any:
    """Open a new attribute period — POST only, no history check.

    Does **not** close any prior open period for the same
    ``(device_id, code)``. Use :func:`transition_attribute` for a
    history-preserving change, or :func:`set_open_attribute` for an
    idempotent overwrite of the open period.

    Thin wrapper over :meth:`TOSWriter.add_attribute_value`.
    """
    return writer.add_attribute_value(
        id_entity=int(device_id),
        code=code,
        value=value,
        date_from=date_from,
        date_to=date_to,
    )


def end_attribute(
    writer: TOSWriter,
    *,
    id_attribute_value: int,
    date_to: str,
) -> Any:
    """Close an existing attribute period by setting ``date_to``.

    Targets a specific period by its primary key — typically obtained
    from :func:`attribute_at` or :func:`attribute_periods`. Use this
    when you need to close a period without opening a replacement
    (e.g. retiring an attribute that no longer applies).

    Thin wrapper over :meth:`TOSWriter.patch_attribute_value`.
    """
    return writer.patch_attribute_value(int(id_attribute_value), date_to=date_to)


def correct_attribute(
    writer: TOSWriter,
    *,
    id_attribute_value: int,
    value: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Any:
    """In-place edit of an existing attribute period.

    **History-destructive** — overwrites the period's value or
    boundaries without preserving the prior state. Use sparingly,
    typically only for:

    - Typo fixes (the value was wrong from day one)
    - Boundary corrections (the period's dates were entered wrong)
    - Historical corrections via Pattern 4 (target a specific closed
      period)

    For a genuine state change use :func:`transition_attribute`.

    At least one of ``value``, ``date_from``, ``date_to`` must be
    provided.

    Thin wrapper over :meth:`TOSWriter.patch_attribute_value`.
    """
    return writer.patch_attribute_value(
        int(id_attribute_value),
        value=value,
        date_from=date_from,
        date_to=date_to,
    )


def transition_attribute(
    writer: TOSWriter,
    *,
    device_id: int,
    code: str,
    new_value: str,
    date: str,
) -> Dict[str, Any]:
    """Close the open period for ``code`` on ``date``, open a new one.

    **History-preserving** — the prior period's value is kept;
    a new period starts on ``date`` with ``new_value``. This is the
    Pattern 2 write (instrument change, firmware bump,
    status transition).

    Thin wrapper over
    :meth:`TOSWriter.transition_attribute_value`.

    Returns:
        ``{"closed": <patch_resp>, "opened": <post_resp>}``. ``closed``
        is ``None`` when there was no pre-existing open period.
    """
    return writer.transition_attribute_value(
        id_entity=int(device_id),
        code=code,
        new_value=new_value,
        transition_date=date,
    )


def set_open_attribute(
    writer: TOSWriter,
    *,
    device_id: int,
    code: str,
    value: str,
    date_from: str,
) -> Any:
    """Idempotent set of the open period: PATCH if value differs, else POST.

    **History-destructive on PATCH** — when an open period exists,
    its value is overwritten. Use only for corrections to a
    misentered open value; for genuine state changes use
    :func:`transition_attribute`.

    This is the Pattern 1 write used by ``cfg reconcile --push-tos``
    when ``--no-transition`` is set or for brand-new attributes.
    By default, ``--push-tos`` now routes value *changes* to
    :func:`transition_attribute` (Pattern 2) to preserve history.

    Thin wrapper over :meth:`TOSWriter.upsert_attribute_value`.
    """
    return writer.upsert_attribute_value(
        id_entity=int(device_id),
        code=code,
        value=value,
        date_from=date_from,
    )


# ---------------------------------------------------------------------------
# Read — sub-session synthesis (the device_attribute_history kernel)
# ---------------------------------------------------------------------------

# The GPS-receiver attribute codes the legacy synthesis chain
# (``gps_metadata_qc.device_attribute_history``) tracks. Passing this
# list as ``codes`` to :func:`slice_attributes_by_window` produces
# byte-equivalent output to the legacy kernel — used as the oracle for
# the slice tests.
LEGACY_GPS_ATTRIBUTE_CODES: List[str] = [
    "serial_number",
    "model",
    "date_start",
    "GPS",
    "GLO",
    "firmware_version",
    "software_version",
    "antenna_height",
    "monument_height",
    "antenna_offset_north",
    "antenna_offset_east",
    "antenna_reference_point",
]


def slice_attributes_by_window(
    history: Dict[str, Any],
    window_start: str,
    window_end: Optional[str],
    *,
    codes: Optional[List[str]] = None,
    fine: bool = True,
) -> List[Dict[str, Any]]:
    """Resolve a device's attribute periods across one join window.

    Sub-session synthesis kernel (§3d of the design note). Replaces the
    intricate two-pass algorithm in
    ``gps_metadata_qc.device_attribute_history`` with a clean
    boundary-partition + per-point lookup approach. Produces the same
    output the legacy kernel does when called with
    ``codes=LEGACY_GPS_ATTRIBUTE_CODES`` — exercised by
    ``test_slice_attributes_by_window``.

    Two output modes, controlled by ``fine``:

    - **fine=True** (default): one row per **atomic sub-window** — the
      window is partitioned at every attribute period boundary that
      falls inside it, and one row is emitted per atomic interval.
      Each row carries the value of each code valid throughout that
      interval. This is what site logs, GAMIT ``station.info``, and
      ``tosGPS PrintTOS`` consume: every firmware update produces a
      new row.
    - **fine=False**: one row for the entire window, carrying the
      most-recent value of each code as of ``window_end`` (or the
      currently-open value if ``window_end is None``). This is the
      coarse view used by `tos audit show` and fleet-gap reports.

    The boundary-partition approach: build the set of all attribute
    date_from / date_to values that fall inside the window, plus the
    window edges. Sort, dedupe. The consecutive pairs of these
    boundaries are the atomic sub-windows. For each, look up every
    code via :func:`attribute_at_value` at the sub-window's start.

    Args:
        history: Entity history dict, as returned by
            :func:`find_device` or
            :meth:`TOSClient.get_entity_history`.
        window_start: ISO datetime — start of the join window.
            Required (use ``"0000-01-01T00:00:00"`` for "from
            beginning of time" if you really need that, but normally
            window_start comes from a TOS join's ``time_from`` which
            is always set).
        window_end: ISO datetime — end of the join window, or
            ``None`` for currently-active.
        codes: Attribute codes to include in each row. ``None``
            (default) uses every code present in
            ``history['attributes']``. Pass
            :data:`LEGACY_GPS_ATTRIBUTE_CODES` for legacy-equivalent
            output shape.
        fine: See above. Default ``True``.

    Returns:
        A list of row dicts. Each row carries:

        - ``id_entity``, ``code_entity_subtype`` (from history)
        - ``date_from``, ``date_to`` (atomic sub-window bounds, or
          window edges if coarse)
        - One key per code, value ``None`` when no attribute period
          for that code covers the sub-window's start

        Rows are ordered by ``date_from`` ascending; the open row (if
        ``window_end is None``) is last.

    Raises:
        ValueError: ``window_start`` is empty or None.
    """
    if not window_start:
        raise ValueError("slice_attributes_by_window requires window_start")

    attrs = history.get("attributes") or []
    id_entity = history.get("id_entity")
    code_entity_subtype = history.get("code_entity_subtype")

    if codes is None:
        codes = sorted({a["code"] for a in attrs if a.get("code")})

    universal = {
        "id_entity": id_entity,
        "code_entity_subtype": code_entity_subtype,
    }

    def _row_at(start: str, end: Optional[str]) -> Dict[str, Any]:
        row: Dict[str, Any] = dict.fromkeys(codes)
        for code in codes:
            v = attribute_at_value(history, code, start)
            if v is not None:
                row[code] = v
        row.update(universal)
        row["date_from"] = start
        row["date_to"] = end
        return row

    if not fine:
        # Coarse: one row covering the whole window, values queried at
        # window_end (or just-before-infinity for an open window so any
        # currently-open period covers it).
        when = window_end or "9999-12-31T23:59:59"
        row: Dict[str, Any] = dict.fromkeys(codes)
        for code in codes:
            v = attribute_at_value(history, code, when)
            if v is not None:
                row[code] = v
        row.update(universal)
        row["date_from"] = window_start
        row["date_to"] = window_end
        return [row]

    # Fine mode: partition the window at attribute boundaries.
    boundaries = {window_start}
    if window_end:
        boundaries.add(window_end)

    for a in attrs:
        df = a.get("date_from")
        dt = a.get("date_to")
        # Skip zero-duration periods
        if df and dt and df >= dt:
            continue
        # Add date_from if it falls inside [window_start, window_end)
        if df:
            if df >= window_start and (window_end is None or df < window_end):
                boundaries.add(df)
        # Add date_to if it falls inside (window_start, window_end]
        if dt:
            if dt > window_start and (window_end is None or dt <= window_end):
                boundaries.add(dt)

    sorted_bounds = sorted(boundaries)

    rows: List[Dict[str, Any]] = []
    # Closed atomic sub-windows from consecutive boundaries
    for i in range(len(sorted_bounds) - 1):
        rows.append(_row_at(sorted_bounds[i], sorted_bounds[i + 1]))

    # Open final sub-window when window is open-ended
    if window_end is None and sorted_bounds:
        rows.append(_row_at(sorted_bounds[-1], None))

    return rows


# ---------------------------------------------------------------------------
# Session composers (§3e)
#
# These compose the read primitives above into the shapes consumed by site
# logs, GAMIT station.info, and tosGPS PrintTOS. They replace the legacy
# synthesis chain (gps_metadata_qc.get_device_sessions +
# get_device_history), but until phase 4 lands the adapter, callers should
# continue to use gps_metadata_qc.gps_metadata for any user-facing path.
# ---------------------------------------------------------------------------


DEFAULT_GPS_SUBTYPES: Tuple[str, ...] = (
    "gnss_receiver",
    "antenna",
    "radome",
    "monument",
)


def device_sessions(
    client: TOSClient,
    station_history: Dict[str, Any],
    *,
    subtypes: Sequence[str] = DEFAULT_GPS_SUBTYPES,
    fine: bool = True,
) -> List[Dict[str, Any]]:
    """All per-join sub-sessions for a station's tracked children.

    Requires a **station** history (one with ``children_connections``).
    Passing a device's history (which has ``parent_connections``) yields
    an empty list — use :func:`parent_joins` instead for the device side.

    For each child connection whose subtype is in ``subtypes`` the
    function fetches the child's full history (one ``GET`` per child) and
    runs :func:`slice_attributes_by_window` over the join window using
    :data:`LEGACY_GPS_ATTRIBUTE_CODES`. Zero-duration joins (``time_from
    == time_to``) are skipped, matching legacy behaviour.

    Returns a flat list, sorted by sub-session ``date_from`` ascending.
    Each row is the join's ``children_connections`` dict augmented with a
    ``"device"`` key holding the resolved attribute slice row (carrying
    ``id_entity`` + ``code_entity_subtype`` + ``date_from`` / ``date_to``
    + one key per legacy code). Replaces
    ``gps_metadata_qc.get_device_sessions``; ``fine=True`` matches the
    legacy output exactly.
    """
    sessions: List[Dict[str, Any]] = []

    subtypes_set = set(subtypes)
    children = station_history.get("children_connections") or []

    for connection in children:
        if connection.get("time_from") == connection.get("time_to"):
            continue

        id_child = connection.get("id_entity_child")
        if id_child is None:
            continue

        device = client.get_entity_history(id_child)
        if device is None:
            continue

        if device.get("code_entity_subtype") not in subtypes_set:
            continue

        slices = slice_attributes_by_window(
            device,
            connection["time_from"],
            connection.get("time_to"),
            codes=LEGACY_GPS_ATTRIBUTE_CODES,
            fine=fine,
        )

        for row in slices:
            entry = dict(connection)
            entry["device"] = row
            sessions.append(entry)

    sessions.sort(key=lambda s: s["device"]["date_from"])
    return sessions


def _device_structure(row: Dict[str, Any]) -> Dict[str, Any]:
    """Per-subtype field extraction for the station-session pivot.

    Mirrors ``gps_metadata_qc.device_structure``. Inlined here so the new
    composer chain has no runtime dependency on the soon-to-be-deprecated
    legacy module. Float coercion (``None`` → ``0.0``) follows the legacy
    contract; consumers (site log, GAMIT export) rely on the numeric
    columns never being ``None``.
    """
    subtype = row.get("code_entity_subtype")

    if subtype == "gnss_receiver":
        return {
            "model": row.get("model"),
            "serial_number": row.get("serial_number"),
            "firmware_version": row.get("firmware_version"),
            "software_version": row.get("software_version"),
        }

    if subtype == "antenna":
        antenna_height = row.get("antenna_height")
        antenna_height = float(antenna_height) if antenna_height is not None else 0.0
        offset_north = row.get("antenna_offset_north")
        offset_north = float(offset_north) if offset_north is not None else 0.0
        offset_east = row.get("antenna_offset_east")
        offset_east = float(offset_east) if offset_east is not None else 0.0
        return {
            "model": row.get("model"),
            "serial_number": row.get("serial_number"),
            "antenna_height": antenna_height,
            "antenna_offset_east": offset_east,
            "antenna_offset_north": offset_north,
            "antenna_reference_point": row.get("antenna_reference_point"),
        }

    if subtype == "radome":
        return {
            "model": row.get("model"),
            "serial_number": row.get("serial_number"),
        }

    if subtype == "monument":
        monument_height = row.get("monument_height") or row.get("antenna_height")
        monument_height = float(monument_height) if monument_height is not None else 0.0
        offset_north = row.get("antenna_offset_north")
        offset_north = float(offset_north) if offset_north is not None else 0.0
        offset_east = row.get("antenna_offset_east")
        offset_east = float(offset_east) if offset_east is not None else 0.0
        return {
            "serial_number": row.get("serial_number"),
            "monument_height": monument_height,
            "monument_offset_north": offset_north,
            "monument_offset_east": offset_east,
        }

    return {}


def station_sessions(
    client: TOSClient,
    station_id: int,
    *,
    subtypes: Sequence[str] = DEFAULT_GPS_SUBTYPES,
) -> List[Dict[str, Any]]:
    """Per-station-session rows for one station.

    Pivots :func:`device_sessions` into the shape consumed by
    ``print_station_info``, ``site_log``, and the GAMIT
    ``station.info`` writer. Each row carries::

        {
            "time_from": datetime,
            "time_to":   datetime | None,
            "gnss_receiver": {model, serial_number, firmware_version, software_version},
            "antenna":       {model, serial_number, antenna_height, antenna_offset_east,
                              antenna_offset_north, antenna_reference_point},
            "radome":        {model, serial_number},
            "monument":      {serial_number, monument_height,
                              monument_offset_north, monument_offset_east},
        }

    Replaces ``gps_metadata_qc.get_device_history``. The pivot mirrors
    legacy semantics exactly: build the sorted set of unique sub-session
    starts and the sorted set of unique closed-period ends, pair them up,
    and for each ``(start, end)`` interval claim each subtype's slot from
    whichever sub-session covers the interval (``date_from <= start`` and
    either ``date_to >= end`` or the period is open).
    """
    station_history = client.get_entity_history(station_id)
    if station_history is None:
        return []

    sessions = device_sessions(client, station_history, subtypes=subtypes, fine=True)

    starts = iter(sorted({s["device"]["date_from"] for s in sessions}))
    ends = iter(
        sorted(
            {
                s["device"]["date_to"]
                for s in sessions
                if s["device"].get("date_to") is not None
            }
        )
    )

    pivoted: List[Dict[str, Any]] = []
    for start in starts:
        try:
            end = next(ends)
        except StopIteration:
            end = None

        record: Dict[str, Any] = {
            "time_from": (
                datetime.strptime(start, "%Y-%m-%dT%H:%M:%S") if start else None
            ),
            "time_to": (datetime.strptime(end, "%Y-%m-%dT%H:%M:%S") if end else None),
        }

        for session in sessions:
            dev = session["device"]
            df = dev.get("date_from")
            dt = dev.get("date_to")

            if end is not None:
                if df is None or df > start:
                    continue
                if dt is None or dt >= end:
                    record[dev["code_entity_subtype"]] = _device_structure(dev)
            else:
                if dt is None:
                    record[dev["code_entity_subtype"]] = _device_structure(dev)

        pivoted.append(record)

    return pivoted


def station_at(
    client: TOSClient,
    station_id: int,
    when: str,
    *,
    subtypes: Sequence[str] = DEFAULT_GPS_SUBTYPES,
) -> Optional[Dict[str, Any]]:
    """Return the :func:`station_sessions` row covering ``when``, or None.

    ``when`` is an ISO date or datetime string (bare ``YYYY-MM-DD`` is
    promoted to midnight via :func:`_normalise_iso_for_compare`). Matching
    rule: a row covers ``when`` when its ``time_from <= when`` and either
    ``time_to`` is ``None`` or ``when < time_to``.
    """
    when_n = _normalise_iso_for_compare(when)
    for row in station_sessions(client, station_id, subtypes=subtypes):
        tf = row.get("time_from")
        tt = row.get("time_to")
        tf_iso = tf.isoformat() if tf is not None else "0000-01-01T00:00:00"
        if tf_iso > when_n:
            continue
        if tt is not None and tt.isoformat() <= when_n:
            continue
        return row
    return None


# ---------------------------------------------------------------------------
# Joins-layer composite (§3f)
# ---------------------------------------------------------------------------


def move_device(
    writer: TOSWriter,
    *,
    id_connection: int,
    child_id: int,
    to_parent_id: int,
    date: str,
) -> Dict[str, Any]:
    """Relocate a device by closing one open join and opening a new one.

    Pure joins-layer write: 1 PATCH (close old join) + 1 POST (open new
    join at ``to_parent_id``). No reads — the caller is responsible for
    resolving ``id_connection`` (typically via
    :func:`parent_joins` on the device's history, or a receivers
    ``cfg move`` lookup).

    Args:
        writer: An authenticated :class:`TOSWriter`.
        id_connection: The currently-open parent join to close.
        child_id: The device being moved (used to open the new join).
        to_parent_id: The new parent's ``id_entity``.
        date: ISO date or datetime — applies to both the close and the
            new join's ``date_from``.

    Returns:
        ``{"closed": <patch_resp>, "opened": <post_resp>}``.
    """
    closed = close_join(writer, id_connection=id_connection, date_to=date)
    opened = open_join(
        writer, parent_id=to_parent_id, child_id=child_id, date_from=date
    )
    return {"closed": closed, "opened": opened}


# ---------------------------------------------------------------------------
# Cross-layer composites (§3h)
# ---------------------------------------------------------------------------


def decommission_device(
    writer: TOSWriter,
    client: TOSClient,
    *,
    device_id: int,
    date: str,
) -> Dict[str, Any]:
    """Retire a device on ``date``.

    Three steps:

    1. Fetch the device's history (1 GET via :func:`find_device`).
    2. Close every open parent join on ``date`` (M PATCHes; the common
       case is M=1, but multiple opens can exist on a misconfigured
       device — close them all).
    3. Transition the ``status`` attribute virkt→óvirkt on ``date``
       (1 PATCH + 1 POST via :func:`transition_attribute`).

    Returns:
        ``{
            "closed_joins": [<patch_resp>, ...],
            "status_transition": {"closed": ..., "opened": ...},
        }``

    Failure semantics: writes are not transactional. If join close
    succeeds and the status transition fails, the join is already
    closed on the server — re-run after fixing the underlying problem.
    The idempotent paths (already-closed joins, already-óvirkt status)
    will silently succeed. Already implemented inline in
    ``tos._dispatch_decommission``; this primitive extracts the
    orchestration so the REPL, the ``receivers`` package, and a future
    ``tos device retire`` verb can share it.
    """
    history = find_device(client, id_entity=device_id)
    opens = open_joins(history, role="child")

    closed: List[Any] = []
    for join in opens:
        id_conn = join.get("id_entity_connection")
        if id_conn is None:
            continue
        resp = close_join(writer, id_connection=int(id_conn), date_to=date)
        closed.append(resp)

    status_resp = transition_attribute(
        writer,
        device_id=device_id,
        code="status",
        new_value="óvirkt",
        date=date,
    )

    return {"closed_joins": closed, "status_transition": status_resp}


def install_device(
    writer: TOSWriter,
    client: TOSClient,
    *,
    parent_id: int,
    device_id: int,
    date: str,
    initial_attributes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Activate a device on ``date`` under ``parent_id``.

    Steps:

    1. Fetch the device's history (1 GET via :func:`find_device`).
    2. Open the parent→device join from ``date`` (1 POST via
       :func:`open_join`).
    3. Reconcile the ``status`` attribute to ``virkt``:

       - Open period with value ``"virkt"`` already exists → no-op.
       - No open period → :func:`set_attribute` to open one starting
         on ``date``.
       - Open period with any other value (typically ``"óvirkt"`` —
         the reactivation path) → :func:`transition_attribute`,
         preserving the prior retirement record.

    4. For each ``(code, value)`` in ``initial_attributes`` (if given),
       apply the same idempotent reconciliation pattern as status —
       no-op when already matching, set when never set, transition
       when changing.

    Returns:
        ``{
            "opened_join": <post_resp>,
            "status": <reconcile_outcome or None>,
            "attributes": {<code>: <reconcile_outcome>, ...},
        }``

    The reconcile outcome is ``"noop"`` (already matched), the
    :func:`set_attribute` POST response (no prior period), or the
    :func:`transition_attribute` ``{"closed", "opened"}`` dict.
    ``status`` is ``None`` when no status reconciliation was needed —
    a freshly installed device with no prior status periods is handled
    via the set-attribute branch and surfaces a non-``None`` value.
    """
    history = find_device(client, id_entity=device_id)

    opened_join = open_join(
        writer,
        parent_id=parent_id,
        child_id=device_id,
        date_from=date,
    )

    status_outcome = _reconcile_open_attribute(
        writer, history, device_id=device_id, code="status", target="virkt", date=date
    )

    attr_outcomes: Dict[str, Any] = {}
    for code, value in (initial_attributes or {}).items():
        attr_outcomes[code] = _reconcile_open_attribute(
            writer, history, device_id=device_id, code=code, target=value, date=date
        )

    return {
        "opened_join": opened_join,
        "status": status_outcome,
        "attributes": attr_outcomes,
    }


def _reconcile_open_attribute(
    writer: TOSWriter,
    history: Dict[str, Any],
    *,
    device_id: int,
    code: str,
    target: str,
    date: str,
) -> Any:
    """Idempotent reconciliation helper used by :func:`install_device`.

    Branches on the device's current open value for ``code``:

    - matches ``target``  → ``"noop"`` (no write)
    - no open period      → :func:`set_attribute` (POST)
    - other open value    → :func:`transition_attribute` (PATCH + POST)
    """
    current = open_attribute(history, code)
    if current == target:
        return "noop"
    if current is None:
        return set_attribute(
            writer,
            device_id=device_id,
            code=code,
            value=target,
            date_from=date,
        )
    return transition_attribute(
        writer,
        device_id=device_id,
        code=code,
        new_value=target,
        date=date,
    )


def replace_device(
    writer: TOSWriter,
    client: TOSClient,
    *,
    parent_id: int,
    out_device_id: int,
    in_device_id: int,
    date: str,
    initial_attributes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Swap one device for another at the same parent on ``date``.

    Two-step composite — :func:`decommission_device` for the outgoing
    device followed by :func:`install_device` for the incoming one.
    Operator use case: receiver swap during a site visit.

    ``initial_attributes`` is forwarded to :func:`install_device` and
    lets the operator seed the incoming device's metadata (firmware,
    GPS/GLO config, ...) in the same call.

    Returns:
        ``{
            "decommissioned": <decommission_device result>,
            "installed":      <install_device result>,
        }``
    """
    decommission = decommission_device(
        writer, client, device_id=out_device_id, date=date
    )
    install = install_device(
        writer,
        client,
        parent_id=parent_id,
        device_id=in_device_id,
        date=date,
        initial_attributes=initial_attributes,
    )
    return {"decommissioned": decommission, "installed": install}
