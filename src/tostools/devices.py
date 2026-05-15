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
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
