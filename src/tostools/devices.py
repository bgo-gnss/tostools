"""Small composable read/write helpers for TOS device entities.

In the style of the legacy ``tostools.tos`` module — each function does
**one** thing on a single concern (lookup, read attribute, fetch
timeline, render). Higher-level workflows
(:func:`tostools.tos.display_device_record`, the ``tos audit apply``
verbs, ``receivers cfg move``) compose these primitives instead of
duplicating them.

Design rules
------------
- Functions accept a :class:`TOSClient` (read) or :class:`TOSWriter`
  (write) — never both, never implicit.
- Subtype names are the **canonical TOS code** (e.g. ``digitizer``,
  ``gps_clock``, ``gnss_receiver``) — not the GPS-only short aliases
  used by ``audit.SUBTYPE_ALIASES``. Use this module when you need to
  touch the broader fleet (seismic digitisers, weather sensors, etc.).
- Read functions return plain dicts (the TOS payload), so callers can
  pick the fields they care about without a heavy data model.

API surface
-----------
Lookup:

* :func:`find_device` — resolve a device by id or (serial, subtype) →
  full history dict.

Attribute helpers:

* :func:`attribute_periods` — group all periods by ``code``, sorted
  chronologically. Useful for "show me every transition for this
  device's status / firmware / model".
* :func:`open_attribute` — the value of the currently-open period for
  ``code``, or ``None``.

Join helpers:

* :func:`device_timeline` — full chronological join history for one
  device (builds the global join index — slow; cache the result).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .api.tos_client import TOSClient


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
