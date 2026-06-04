"""tos — CLI for TOS station-metadata QC + write workflows.

Subcommand surface only; the legacy flat-arg form (Tryggvi original)
was removed in v0.7. XML / SC3 / FDSN export has been retired.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

KNOWN_SUBCOMMANDS = {
    "owners",
    "device",
    "audit",
    "station",
    "contact",
    "fleet",
    "visit",
}


def _owners_main(argv):
    """Handle `tos owners ...` subcommands."""
    from .api.tos_client import TOSClient
    from .owners import KNOWN_OWNERS, OwnersCache

    p = argparse.ArgumentParser(
        prog="tos owners",
        description="Manage the recognized TOS device-owner allow-list.",
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List recognized owner labels.")
    p_list.add_argument(
        "--refresh",
        action="store_true",
        help="Probe TOS to verify each owner is still in use; rewrites the cache.",
    )
    p_list.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_list.add_argument(
        "--cache-path",
        help="Override the cache file path (default: ~/.config/tostools/owners.yaml).",
    )
    p_list.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_list.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    if args.action != "list":
        p.error(f"unknown action: {args.action}")
        return 2

    cache_path = args.cache_path
    cache = OwnersCache(cache_path) if cache_path else OwnersCache()

    if args.refresh:
        scheme = "https" if args.port == 443 else "http"
        base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
        client = TOSClient(base_url=base_url)
        result = cache.refresh(client)
        owners = result.in_use
        missing = result.missing
    else:
        owners = cache.load()
        missing = []

    if args.json:
        import json as _json

        payload = {
            "owners": owners,
            "missing": missing,
            "cache_path": str(cache.cache_path),
            "seed": list(KNOWN_OWNERS),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for o in owners:
            print(o)
        if missing:
            print(
                "\nMissing from TOS (not found via basic_search):",
                file=sys.stderr,
            )
            for o in missing:
                print(f"  - {o}", file=sys.stderr)
    return 0


def add_device_filter_arguments(parser, *, with_date: bool = True) -> None:
    """Add the standard device-filter argument set to a subparser.

    Reusable across `tos device list`, future `tos device search`, audit
    verbs that produce device tables, etc. Match semantics are documented
    on :func:`apply_device_filters`.

    Adds: ``--subtype``, ``--model``, ``--status``, ``--serial``. When
    ``with_date=True`` (default), also adds ``--date`` for point-in-time
    membership filtering — callers that have no time-bounded data
    (pure-attribute listings) can opt out.
    """
    parser.add_argument(
        "--subtype",
        help=(
            "Filter to a single TOS subtype (gnss_receiver, antenna, "
            "radome, monument, modem_gsm, sim_card, ...). Exact match."
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "Filter by device model — case-insensitive substring "
            "(e.g. 'NETR9' matches 'TRIMBLE NETR9')."
        ),
    )
    parser.add_argument(
        "--status",
        help=(
            "Filter by current status — exact match against the open "
            "status attribute value (e.g. virkt, bilað, óvirkt)."
        ),
    )
    parser.add_argument(
        "--serial",
        help="Filter by serial — substring match, case-sensitive.",
    )
    if with_date:
        parser.add_argument(
            "--date",
            help=(
                "Filter to devices present at the parent on this date "
                "(YYYY-MM-DD). Match rule: time_from <= date "
                "AND (time_to IS NULL OR time_to > date). For listing "
                "verbs that default to open-only joins, --date implicitly "
                "enables --all scanning."
            ),
        )


def add_attribute_filter_arguments(parser) -> None:
    """Add the standard attribute-filter argument set to a subparser.

    Reusable across `tos device show`, future fleet-wide attribute
    inspection verbs (`tos audit attribute-dates`, ...). Match semantics
    are documented on :func:`apply_attribute_filters`.

    Adds: ``--code`` (repeatable), ``--value``, ``--on-date``,
    ``--suspicious``.
    """
    parser.add_argument(
        "--code",
        action="append",
        dest="codes",
        help=(
            "Filter to one or more attribute codes (e.g. --code "
            "serial_number --code model). Repeatable; OR'd. Exact match."
        ),
    )
    parser.add_argument(
        "--value",
        help=(
            "Filter by attribute value — case-insensitive substring "
            "(e.g. '--value NETR9' matches 'TRIMBLE NETR9')."
        ),
    )
    parser.add_argument(
        "--on-date",
        dest="on_date",
        help=(
            "Filter to attribute periods active on this date "
            "(YYYY-MM-DD). Match rule: date_from <= date "
            "AND (date_to IS NULL OR date_to > date)."
        ),
    )
    parser.add_argument(
        "--suspicious",
        action="store_true",
        help=(
            "Filter to attribute periods opening on 2014-10-17 — the "
            "fleet-wide metadata-cleanup-artifact pattern (model / "
            "serial / etc. silently dated to the bulk-load date). See "
            "memory project_2014_10_17_metadata_cleanup_artifacts."
        ),
    )


def apply_attribute_filters(
    periods: List[Dict[str, Any]],
    args,
) -> List[Dict[str, Any]]:
    """Filter attribute-period rows by the standard CLI attribute-filter set.

    Reads ``args.codes`` (list), ``args.value``, ``args.on_date``,
    ``args.suspicious`` (any missing attribute is treated as "no
    constraint"). Filters are AND'd; preserves input order.

    Expected period shape: TOS attribute-value dict with ``code``,
    ``value``, ``date_from``, ``date_to`` (the rows from
    ``writer.get_attribute_values`` / ``client.get_entity_history``).

    Match semantics:
      - ``codes``: exact match against ``period['code']`` (any one of the
        listed codes — OR'd within the filter, AND'd with others)
      - ``value``: case-insensitive substring against
        ``str(period['value'])``
      - ``on_date``: period active on date —
        ``date_from <= date < date_to`` (or ``date_to`` is None). Same
        date-prefix lex compare as :func:`apply_device_filters`.
      - ``suspicious``: period's ``date_from[:10] == '2014-10-17'``
    """
    codes = getattr(args, "codes", None) or None
    value_needle = getattr(args, "value", None)
    on_date_raw = getattr(args, "on_date", None)
    on_date = on_date_raw[:10] if on_date_raw else None
    suspicious_only = bool(getattr(args, "suspicious", False))

    code_set = set(codes) if codes else None
    value_lower = value_needle.lower() if value_needle else None

    out: List[Dict[str, Any]] = []
    for p in periods:
        if code_set and p.get("code") not in code_set:
            continue
        if value_lower is not None:
            value = p.get("value")
            if value is None or value_lower not in str(value).lower():
                continue
        if on_date:
            df = (p.get("date_from") or "")[:10]
            dt_raw = p.get("date_to")
            dt = dt_raw[:10] if dt_raw else None
            if df and df > on_date:
                continue
            if dt is not None and dt <= on_date:
                continue
        if suspicious_only:
            if (p.get("date_from") or "")[:10] != _CLEANUP_ARTIFACT_DATE:
                continue
        out.append(p)
    return out


def apply_device_filters(
    rows: List[Dict[str, Any]],
    args,
) -> List[Dict[str, Any]]:
    """Filter enriched device rows by the standard CLI filter set.

    Reads ``args.subtype``, ``args.model``, ``args.status``, ``args.serial``,
    ``args.date`` (any missing attribute is treated as "no constraint").
    Filters are AND'd; preserves input order.

    Expected row shape: ``subtype``, ``model``, ``status``, ``serial``,
    ``time_from``, ``time_to`` (the row dicts emitted by
    :func:`_device_list_main` and similar producers).

    Match semantics:
      - ``subtype``: exact match against ``row['subtype']``
      - ``model``: case-insensitive substring against ``row['model']``
      - ``status``: exact match against ``row['status']``
      - ``serial``: case-sensitive substring against ``row['serial']``
      - ``date``: row's join straddles the date —
        ``row['time_from'] <= date < row['time_to']`` (or
        ``time_to`` is None). Date-only prefixes (YYYY-MM-DD) are
        compared lexicographically; TOS's full-datetime values compare
        correctly because year-first.
    """
    subtype = getattr(args, "subtype", None)
    model = getattr(args, "model", None)
    status = getattr(args, "status", None)
    serial = getattr(args, "serial", None)
    date_raw = getattr(args, "date", None)
    on_date = date_raw[:10] if date_raw else None

    out: List[Dict[str, Any]] = []
    for row in rows:
        if subtype and row.get("subtype") != subtype:
            continue
        if model and model.lower() not in (row.get("model") or "").lower():
            continue
        if status and row.get("status") != status:
            continue
        if serial and serial not in (row.get("serial") or ""):
            continue
        if on_date:
            tf = (row.get("time_from") or "")[:10]
            tt_raw = row.get("time_to")
            tt = tt_raw[:10] if tt_raw else None
            if tf and tf > on_date:
                continue
            if tt is not None and tt <= on_date:
                continue
        out.append(row)
    return out


#: Allowed maintenance-reason codes — match
#: :attr:`TOSWriter.MAINTENANCE_REASON_CODES` exactly so the read-side
#: filter and the (eventual) write-side validator stay in lock-step.
MAINTENANCE_REASON_CODES = frozenset(
    {"change", "repairs", "inspection", "improvements", "other"}
)

#: Display strings TOS uses on the list endpoint for each reason boolean.
#: The list endpoint flattens ``reason_change=true,reason_repairs=true``
#: into a single ``"reason"`` field with the Icelandic display strings
#: comma-joined ("Breyting, Viðgerð"). The detail endpoint returns the
#: raw booleans. ``apply_visit_filters`` uses this map to translate the
#: filter operator's English codes into the strings TOS actually emits.
#: ``change`` / ``repairs`` / ``improvements`` were empirically verified
#: against live data 2026-05-30; ``inspection`` / ``other`` are unused
#: in the GPS fleet today (best-guess translations — adjust if TOS
#: reveals them).
MAINTENANCE_REASON_DISPLAY = {
    "change": "Breyting",
    "repairs": "Viðgerð",
    "inspection": "Skoðun",
    "improvements": "Endurbætur",
    "other": "Annað",
}


def add_visit_filter_arguments(parser) -> None:
    """Add the standard vitjun-filter argument set to a subparser.

    Reusable across ``tos visit list``, future ``tos visit show``
    multi-match, and the visit-coverage audit. Match semantics are
    documented on :func:`apply_visit_filters`.

    Adds: ``--type {on_site,remote}``, ``--reason`` (repeatable),
    ``--since DATE``, ``--participants SUBSTR``, ``--open``,
    ``--completed`` (mutually exclusive).
    """
    parser.add_argument(
        "--type",
        dest="visit_type",
        choices=["on_site", "remote"],
        default=None,
        help=(
            "Filter to one visit type: on_site (Staðarvitjun) or remote "
            "(Fjarvitjun)."
        ),
    )
    parser.add_argument(
        "--reason",
        action="append",
        dest="reasons",
        choices=sorted(MAINTENANCE_REASON_CODES),
        help=(
            "Filter to one or more reason codes (e.g. --reason change "
            "--reason repairs). Repeatable; OR'd within the filter."
        ),
    )
    parser.add_argument(
        "--since",
        dest="since",
        default=None,
        help=(
            "Filter to visits with start_time >= this date (YYYY-MM-DD). "
            "Compared as a date-prefix lex compare against start_time."
        ),
    )
    parser.add_argument(
        "--participants",
        dest="participants",
        default=None,
        help=(
            "Filter by participants/participants_names — case-insensitive "
            "substring (e.g. '--participants bgo' matches "
            "'bgo@vedur.is' OR 'Benedikt Ófeigsson')."
        ),
    )
    status_group = parser.add_mutually_exclusive_group()
    status_group.add_argument(
        "--open",
        dest="open_only",
        action="store_true",
        help="Only visits with completed=False (long-running / unresolved).",
    )
    status_group.add_argument(
        "--completed",
        dest="completed_only",
        action="store_true",
        help="Only visits with completed=True (closed records).",
    )


def apply_visit_filters(
    rows: List[Dict[str, Any]],
    args,
) -> List[Dict[str, Any]]:
    """Filter vitjun rows by the standard CLI visit-filter set.

    Reads ``args.visit_type``, ``args.reasons`` (list), ``args.since``,
    ``args.participants``, ``args.open_only``, ``args.completed_only``
    (any missing attribute is treated as "no constraint"). Filters are
    AND'd; preserves input order.

    Expected row shape: TOS vitjun dict as returned by
    :meth:`TOSClient.list_maintenance_visits` —
    ``maintenance_type``, ``start_time``, ``reason`` (string with
    embedded codes; see below), ``participants`` /
    ``participants_names``, ``completed``.

    Match semantics:
      - ``visit_type``: exact match against ``row['maintenance_type']``
      - ``reasons``: TOS's list endpoint flattens the per-code
        booleans into a single ``reason`` field with Icelandic display
        strings ("Breyting, Viðgerð"). The filter translates the
        operator's English codes via :data:`MAINTENANCE_REASON_DISPLAY`
        and matches if any requested display string appears in the row
        (OR within the filter, AND against other filters).
      - ``since``: ``row['start_time'][:10] >= since[:10]`` (lex
        compare; YYYY-MM-DD ordering)
      - ``participants``: case-insensitive substring against
        ``participants_names`` OR ``participants`` (whichever has a
        value; participants_names is the human-resolved form)
      - ``open_only``: ``row['completed']`` is falsy
      - ``completed_only``: ``row['completed']`` is truthy
    """
    visit_type = getattr(args, "visit_type", None)
    reasons = getattr(args, "reasons", None) or None
    since_raw = getattr(args, "since", None)
    since = since_raw[:10] if since_raw else None
    participants_needle = getattr(args, "participants", None)
    open_only = bool(getattr(args, "open_only", False))
    completed_only = bool(getattr(args, "completed_only", False))

    # Translate operator codes → TOS display strings. Skip unknown
    # codes silently — argparse choices= already constrains input.
    reason_displays = (
        [
            MAINTENANCE_REASON_DISPLAY[c]
            for c in reasons
            if c in MAINTENANCE_REASON_DISPLAY
        ]
        if reasons
        else None
    )
    participants_lower = participants_needle.lower() if participants_needle else None

    out: List[Dict[str, Any]] = []
    for row in rows:
        if visit_type and row.get("maintenance_type") != visit_type:
            continue
        if reason_displays:
            raw = str(row.get("reason") or "")
            # Comma-joined display strings; match if any operator-
            # requested display appears as a substring of the field.
            if not any(d in raw for d in reason_displays):
                continue
        if since:
            start = (row.get("start_time") or "")[:10]
            if start and start < since:
                continue
            if not start:
                # Row with no start_time can't be filtered by --since;
                # exclude rather than silently keep it.
                continue
        if participants_lower is not None:
            names = str(row.get("participants_names") or "")
            emails = str(row.get("participants") or "")
            if (
                participants_lower not in names.lower()
                and participants_lower not in emails.lower()
            ):
                continue
        if open_only and row.get("completed"):
            continue
        if completed_only and not row.get("completed"):
            continue
        out.append(row)
    return out


def _resolve_parent_id(
    client,
    *,
    station_marker: Optional[str] = None,
    location_name: Optional[str] = None,
) -> Optional[int]:
    """Resolve a parent entity id from a station marker or a location name.

    Read-only helper used by ``tos device list``. Uses
    :meth:`TOSClient.basic_search` directly (rather than the
    TOSWriter wrappers ``find_station_by_marker`` /
    ``find_location_by_name``) to keep the read CLI off the writer
    surface — same convention as ``tos device show``. See memory note
    ``project_tos_client_writer_read_duplication`` for the eventual
    consolidation plan.

    Returns the parent's ``id_entity`` or ``None`` if no exact match.
    """
    if station_marker:
        needle = station_marker.lower()
        for hit in client.basic_search(needle):
            if hit.get("code") != "marker":
                continue
            if hit.get("distance") != 0:
                continue
            if (hit.get("value_varchar") or "").lower() != needle:
                continue
            if hit.get("type_lvl_two") != "stöð":
                continue
            entity_id = hit.get("id_entity") or hit.get("id_lvl_two")
            if entity_id:
                return int(entity_id)
        return None
    if location_name:
        for hit in client.basic_search(location_name):
            if hit.get("code") != "name":
                continue
            if hit.get("distance") != 0:
                continue
            if hit.get("value_varchar") != location_name:
                continue
            entity_id = hit.get("id_entity") or hit.get("id_lvl_two")
            if entity_id:
                return int(entity_id)
        return None
    return None


def _device_list_main(args) -> int:
    """Handle ``tos device list`` — list devices joined to a parent.

    Resolves the parent entity from ``--station`` (marker) or
    ``--location`` (name), reads its ``children_connections``, and
    prints a table of currently-joined devices. Mirrors the TOS web UI's
    per-station device panel: ``id_entity, serial, model, subtype,
    status, since`` plus ``id_connection`` for use in subsequent ACTION
    lines.

    Defaults to **open** joins only (devices presently at the parent).
    ``--all`` includes closed joins for full-history inspection.

    Each child's serial / model / subtype / status comes from a
    follow-up ``get_entity_history(child_id)`` call. One HTTP per
    distinct device; cheap for a station with <10 children.
    """
    import json as _json

    from .api.tos_client import TOSClient
    from .devices import open_attribute

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    parent_id = _resolve_parent_id(
        client,
        station_marker=args.station,
        location_name=args.location,
    )
    if parent_id is None:
        needle = args.station or args.location
        kind = "station marker" if args.station else "location name"
        print(f"No parent entity found for {kind} {needle!r}", file=sys.stderr)
        return 1

    parent = client.get_entity_history(parent_id)
    if not parent:
        print(
            f"Parent id_entity={parent_id} returned no history payload",
            file=sys.stderr,
        )
        return 1

    parent_name = open_attribute(parent, "name") or open_attribute(parent, "marker")
    children = parent.get("children_connections") or []

    # --date implies --all: closed joins must be scanned to know what was
    # at the parent on a past date. Open-vs-all filtering happens here
    # (no child fetch needed); all other filters happen after enrichment
    # via apply_device_filters.
    include_closed = args.all or args.date is not None
    if not include_closed:
        children = [c for c in children if c.get("time_to") is None]

    rows: List[Dict[str, Any]] = []
    for conn in children:
        child_id_raw = conn.get("id_entity_child")
        if child_id_raw is None:
            continue
        try:
            child_id = int(child_id_raw)
        except (TypeError, ValueError):
            continue
        child = client.get_entity_history(child_id) or {}
        rows.append(
            {
                "id_entity": child_id,
                "serial": open_attribute(child, "serial_number") or "?",
                "model": open_attribute(child, "model") or "?",
                "subtype": child.get("code_entity_subtype") or "?",
                "status": open_attribute(child, "status") or "—",
                "time_from": conn.get("time_from") or "?",
                "time_to": conn.get("time_to"),
                "id_connection": conn.get("id_entity_connection") or conn.get("id"),
            }
        )

    rows = apply_device_filters(rows, args)

    active_filters = {
        "subtype": args.subtype,
        "model": args.model,
        "status": args.status,
        "serial": args.serial,
        "on_date": args.date,
    }
    active_filters = {k: v for k, v in active_filters.items() if v}

    if args.json:
        payload = {
            "parent_id_entity": parent_id,
            "parent_name": parent_name,
            "include_closed": include_closed,
            "filters": active_filters,
            "devices": rows,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    parent_label = parent_name or f"id_entity={parent_id}"
    if args.date:
        scope = f"as-of {args.date}"
    elif include_closed:
        scope = "all"
    else:
        scope = "open"
    filter_suffix = ""
    if active_filters:
        bits = [f"{k}={v!r}" for k, v in active_filters.items()]
        filter_suffix = f"  [filters: {', '.join(bits)}]"
    print(
        f"Devices at {parent_label} (id_entity={parent_id}) — {len(rows)} {scope} "
        f"join(s):{filter_suffix}"
    )
    if not rows:
        print("  (no matching children_connections)")
        return 0

    # Render via Rich so the subtype / date / id / status cells get
    # the same color treatment as `tos station show`. Caller already
    # printed the "Devices at ... — N <scope> join(s)" header line
    # above (tests assert on that substring), so the table itself
    # has no title — just the column box.
    #
    # ``no_wrap=True`` on text columns prevents Rich from splitting
    # "TRIMBLE NETR9" across two lines when the terminal is narrow;
    # horizontal overflow is preferable to wrapping for grep / tail
    # workflows. Matches the pre-Rich plain-text behavior.
    from rich.console import Console
    from rich.table import Table

    table = Table(box=None, show_edge=False, pad_edge=False)
    table.add_column("id", justify="right")
    table.add_column("serial", no_wrap=True)
    table.add_column("model", no_wrap=True)
    table.add_column("subtype", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("since", no_wrap=True)
    table.add_column("until", no_wrap=True)
    table.add_column("conn", justify="right")
    for r in rows:
        tf = str(r["time_from"])[:19]
        tt = str(r["time_to"])[:19] if r["time_to"] else "—"
        table.add_row(
            _color_id(r["id_entity"]),
            str(r["serial"]),
            str(r["model"]),
            _color_subtype(r["subtype"]),
            _color_status(r["status"]),
            _color_date(tf) if tf else "?",
            _color_date(tt) if r["time_to"] else "—",
            _color_id(r["id_connection"]) if r["id_connection"] is not None else "?",
        )
    # Width override matches the legacy plain-print overflow
    # behavior: don't squeeze columns just because the terminal is
    # narrow — let the user pipe to `less -S` or widen the window.
    # Cap at 240 to keep tests deterministic across environments.
    Console(width=240).print(table)
    return 0


# ----------------------------------------------------------------------
# `tos device show` rendering helpers
# ----------------------------------------------------------------------
#
# Suspicion-coloring rules surface known TOS data-quality smells without
# requiring the operator to re-derive them:
#
#  - "2014-10-17" date_from on any attribute / join is the fleet-wide
#    metadata-cleanup-artifact pattern (see memory
#    project_2014_10_17_metadata_cleanup_artifacts). Yellow.
#  - status == bilað / óvirkt is operationally relevant. Red.
#  - closed periods (date_to set) are dimmed so the open / current
#    periods stand out at a glance.
#  - id_attribute_value and id_connection are cyan — they're what the
#    operator copies into the next ACTION line in a triage file.

_CLEANUP_ARTIFACT_DATE = "2014-10-17"
_SUSPICIOUS_STATUSES = ("bilað", "óvirkt")

# Per-subtype color for the device-table subtype column. Aligned with
# the legacy `tosGPS PrintTOS` palette in `io/rich_formatters.py`:
# receivers green, antennas/radomes red, monuments yellow. Anything
# else (digitizer, sim_card, ...) stays uncolored. Kept separate from
# the more general `_color_status` / `_color_date` family because
# subtype is identity, not condition.
_SUBTYPE_COLOR = {
    "gnss_receiver": "green",
    "antenna": "red",
    "radome": "red",
    "monument": "yellow",
}

# Default color for attribute-value cells in the station-level
# attribute tables (`tos station show`). Matches the legacy
# `Property | Value` formatter (Value column = cyan).
_STATION_VALUE_COLOR = "cyan"

# Canonical display order for the GPS quartet — receiver first because it
# carries firmware history (the most frequent drill-down target), then
# antenna / radome / monument matching the IGS site-log convention.
# Anything outside this set (digitizer, sim_card, ...) sorts after.
_DEVICE_SUBTYPE_DISPLAY_ORDER = ("gnss_receiver", "antenna", "radome", "monument")

# Canonical display order for station-level attribute rows. Identity
# fields first (subtype / marker / name), then location triple
# (lon / lat / altitude), then operational metadata. Everything
# unlisted sorts alphabetically after this prefix — preserves a
# predictable top-of-table even as new attribute codes are added.
_STATION_ATTRIBUTE_DISPLAY_ORDER = (
    "subtype",
    "marker",
    "name",
    "iers_domes_number",
    "lon",
    "lat",
    "altitude",
    "operational_class",
    "in_network_epos",
)


def _ordered_codes(codes: List[str], priority: tuple) -> List[str]:
    """Sort ``codes`` by ``priority`` first, alphabetical for the rest.

    ``priority`` is a tuple of canonical codes that should appear in
    that order at the top. Any code in ``codes`` not listed in
    ``priority`` is appended afterward, sorted alphabetically.
    """
    in_priority = [c for c in priority if c in codes]
    extras = sorted(c for c in codes if c not in priority)
    return in_priority + extras


def _device_row_sort_key(row: Dict[str, Any]) -> tuple:
    """Sort key for joined-device rows: (subtype priority, date_from).

    Used by `tos station show` to order both the open and closed
    joined-device tables consistently. Subtype priority follows
    :data:`_DEVICE_SUBTYPE_DISPLAY_ORDER`; date_from sorts chronologically
    within each subtype group so a station's history reads top-to-bottom.
    """
    subtype = row.get("subtype") or ""
    try:
        priority = _DEVICE_SUBTYPE_DISPLAY_ORDER.index(subtype)
    except ValueError:
        priority = len(_DEVICE_SUBTYPE_DISPLAY_ORDER)
    date_from = row.get("time_from") or ""
    return (priority, date_from)


def _color_subtype(subtype: Optional[str]) -> str:
    """Wrap a device subtype in its canonical color (see ``_SUBTYPE_COLOR``).

    Falls through uncoloured for unknown subtypes so unfamiliar entity
    kinds don't silently get repainted.
    """
    if not subtype:
        return "?"
    style = _SUBTYPE_COLOR.get(subtype)
    if style is None:
        return str(subtype)
    return f"[{style}]{subtype}[/{style}]"


def _color_value(value: Any, code: str) -> str:
    """Render an attribute-value cell with the station-table coloring.

    Status uses the existing red treatment for ``bilað`` / ``óvirkt``;
    everything else is wrapped in :data:`_STATION_VALUE_COLOR` so the
    Value column pops visually in `tos station show`. Used by the
    station path only — device-show keeps plain values for now.
    """
    if code == "status":
        return _color_status(value)
    if value is None:
        return "—"
    return f"[{_STATION_VALUE_COLOR}]{value}[/{_STATION_VALUE_COLOR}]"


def _color_date(date_str: Optional[str]) -> str:
    """Wrap a date in rich markup.

    Yellow for the fleet-wide 2014-10-17 cleanup-artifact backdate
    (operationally suspicious). Plain blue for everything else,
    matching the legacy `tosGPS PrintTOS` From/To column style so
    dates pop consistently across both views. Empty / None renders
    as an em-dash placeholder.
    """
    if not date_str:
        return "—"
    if str(date_str)[:10] == _CLEANUP_ARTIFACT_DATE:
        return f"[yellow]{date_str}[/yellow]"
    return f"[blue]{date_str}[/blue]"


def _color_status(value: Optional[str]) -> str:
    """Wrap a status value in red if it's bilað/óvirkt."""
    if value is None:
        return "—"
    if str(value) in _SUSPICIOUS_STATUSES:
        return f"[red]{value}[/red]"
    return str(value)


def _color_id(value: Any) -> str:
    """Wrap an id in cyan — visually distinguishes copy-into-ACTION-line values."""
    if value is None:
        return "?"
    return f"[cyan]{value}[/cyan]"


def _color_id_with_recency(value: Any, highlight_since: Optional[int]) -> str:
    """Like :func:`_color_id` but flag values above ``highlight_since``.

    Renders a leading ★ and switches the cell to bold magenta when the
    id is above the threshold. Used by ``tos device show
    --highlight-since`` to surface attribute_value / id_connection rows
    that were written recently (likely retrospective back-fills, since
    TOS exposes no created_at field — see CLAUDE.md "Retrospective
    writes" section).
    """
    if value is None:
        return "?"
    if highlight_since is not None:
        try:
            if int(value) > int(highlight_since):
                return f"[bold magenta]★ {value}[/bold magenta]"
        except (TypeError, ValueError):
            pass
    return f"[cyan]{value}[/cyan]"


def _render_show_header(console, history: Dict[str, Any]) -> None:
    """One-line device summary: id, subtype, open serial/model/status."""
    from .devices import open_attribute

    did = history.get("id_entity")
    subtype = history.get("code_entity_subtype") or "?"
    serial = open_attribute(history, "serial_number") or "?"
    model = open_attribute(history, "model") or "?"
    status = open_attribute(history, "status")
    console.print(
        f"Device id={_color_id(did)}  subtype={subtype}  "
        f"SN [bold]{serial}[/bold]  model [bold]{model}[/bold]  "
        f"status {_color_status(status)}"
    )


def _render_show_open_attributes(
    console,
    history: Dict[str, Any],
    args=None,
    *,
    priority_codes: Optional[tuple] = None,
    colorize_values: bool = False,
) -> None:
    """Render the currently-open attribute periods only (--attributes view).

    Mirrors the TOS web UI 'Eiginleikar' panel. Highlights yellow when an
    open period's date_from is the cleanup-artifact date 2014-10-17.

    When ``args`` carries attribute filters (see
    :func:`add_attribute_filter_arguments`), only matching periods are
    shown. Filters AND'd with the implicit "open only" constraint.

    ``priority_codes`` reorders the rows so the listed codes appear
    first in that order, with everything else alphabetical afterwards.
    Used by ``tos station show`` to surface the station-identity
    triple (marker / name / location) above arbitrary metadata.
    Default ``None`` preserves the legacy alphabetical-only ordering
    for ``tos device show``.

    ``colorize_values=True`` wraps each value cell (status excepted)
    in :data:`_STATION_VALUE_COLOR` so the Value column pops. Off by
    default — only ``tos station show`` opts in.
    """
    from rich.table import Table

    from .devices import attribute_periods

    by_code = attribute_periods(history)
    if priority_codes is not None:
        ordered_codes = _ordered_codes(list(by_code), priority_codes)
    else:
        ordered_codes = sorted(by_code)
    open_rows = []
    for code in ordered_codes:
        for p in by_code[code]:
            if p.get("date_to") is None:
                open_rows.append((code, p))

    if args is not None:
        filtered = apply_attribute_filters([p for _, p in open_rows], args)
        keep_ids = {id(p) for p in filtered}
        open_rows = [(code, p) for code, p in open_rows if id(p) in keep_ids]

    highlight_since = (
        getattr(args, "highlight_since", None) if args is not None else None
    )

    table = Table(title="Current attributes (open periods only)")
    table.add_column("code")
    table.add_column("value")
    table.add_column("date_from")
    table.add_column("id_attribute_value", justify="right")
    for code, p in open_rows:
        value = p.get("value")
        if colorize_values:
            rendered_value = _color_value(value, code)
        else:
            rendered_value = (
                _color_status(value)
                if code == "status"
                else (str(value) if value is not None else "—")
            )
        table.add_row(
            code,
            rendered_value,
            _color_date(p.get("date_from")),
            _color_id_with_recency(p.get("id_attribute_value"), highlight_since),
        )
    console.print(table)


def _render_show_attribute_history(
    console,
    history: Dict[str, Any],
    args=None,
    *,
    closed_only: bool = False,
    priority_codes: Optional[tuple] = None,
    colorize_values: bool = False,
) -> None:
    """Render the attribute history (--attributes-history view).

    Mirrors the TOS web UI 'Saga eiginda tækis' panel. By default shows
    all periods (open + closed), with closed rows dimmed so the
    currently-open ones stand out. Pass ``closed_only=True`` to drop
    open rows entirely — used by ``tos station show --all`` where the
    open periods are already shown in the Current-attributes table
    immediately above.

    When ``args`` carries attribute filters (see
    :func:`add_attribute_filter_arguments`), only matching periods are
    shown.

    ``colorize_values=True`` opts the Value column into the station
    coloring (see :func:`_color_value`).
    """
    from rich.table import Table

    from .devices import attribute_periods

    by_code = attribute_periods(history)
    if args is not None:
        # Per-code filter, preserving the chronological sort
        # attribute_periods built. Drop codes that lose all periods so
        # the table doesn't show empty per-code groupings.
        filtered_by_code: Dict[str, List[Dict[str, Any]]] = {}
        for code, periods in by_code.items():
            kept = apply_attribute_filters(periods, args)
            if kept:
                filtered_by_code[code] = kept
        by_code = filtered_by_code
    highlight_since = (
        getattr(args, "highlight_since", None) if args is not None else None
    )

    title = (
        "Attribute history (closed periods only)"
        if closed_only
        else "Attribute history (all periods)"
    )
    table = Table(title=title)
    table.add_column("code")
    table.add_column("value")
    table.add_column("date_from")
    table.add_column("date_to")
    table.add_column("type")
    table.add_column("id_attribute_value", justify="right")

    if priority_codes is not None:
        ordered_codes = _ordered_codes(list(by_code), priority_codes)
    else:
        ordered_codes = sorted(by_code)

    rows_added = 0
    for code in ordered_codes:
        for p in by_code[code]:
            is_closed = p.get("date_to") is not None
            if closed_only and not is_closed:
                continue
            value = p.get("value")
            if colorize_values:
                rendered_value = _color_value(value, code)
            else:
                rendered_value = (
                    _color_status(value)
                    if code == "status"
                    else (str(value) if value is not None else "—")
                )
            datatype = p.get("attribute_datatype_code") or "?"

            cells = [
                code,
                rendered_value,
                _color_date(p.get("date_from")),
                _color_date(p.get("date_to")) if p.get("date_to") else "open",
                datatype,
                _color_id_with_recency(p.get("id_attribute_value"), highlight_since),
            ]
            if is_closed:
                # Dim the whole row by wrapping each cell. Keep the
                # color markup intact (rich nests styles cleanly).
                cells = [f"[dim]{c}[/dim]" for c in cells]
            table.add_row(*cells)
            rows_added += 1

    if rows_added == 0 and closed_only:
        # Nothing to show — print a one-liner instead of an empty table.
        console.print(f"{title}: (none)")
        return
    console.print(table)


def _render_show_parent_history(
    console,
    client,
    parent_history: List[Dict[str, Any]],
    args=None,
) -> None:
    """Render the parent (location/station) history (--list view).

    Mirrors the TOS web UI 'Saga staðsetningar tækis' panel. Resolves
    parent names via on-demand get_entity_history, cached per id.
    Highlights cleanup-artifact dates yellow; dims closed joins.
    """
    from rich.table import Table

    if not parent_history:
        console.print(
            "[dim]Parent history: (no parent connections — device is orphan or "
            "never joined to a parent)[/dim]"
        )
        return

    parent_names: Dict[int, str] = {}

    def _parent_name(pid: int) -> str:
        cached = parent_names.get(pid)
        if cached is not None:
            return cached
        try:
            parent_entity = client.get_entity_history(pid)
        except Exception:  # noqa: BLE001
            parent_entity = None
        name: Optional[str] = None
        if parent_entity:
            for a in parent_entity.get("attributes") or []:
                if a.get("code") in ("name", "marker") and a.get("date_to") is None:
                    name = a.get("value")
                    break
        resolved = name or "?"
        parent_names[pid] = resolved
        return resolved

    highlight_since = (
        getattr(args, "highlight_since", None) if args is not None else None
    )

    table = Table(title=f"Parent history ({len(parent_history)} join(s))")
    table.add_column("#", justify="right")
    table.add_column("state")
    table.add_column("time_from")
    table.add_column("time_to")
    table.add_column("parent")
    table.add_column("name")
    table.add_column("id_connection", justify="right")

    for i, j in enumerate(parent_history, 1):
        is_open = j.get("time_to") is None
        pid = j.get("id_entity_parent")
        pname = _parent_name(int(pid)) if pid is not None else "?"
        conn_id = j.get("id")

        cells = [
            str(i),
            "[green]open[/green]" if is_open else "closed",
            _color_date(j.get("time_from")),
            _color_date(j.get("time_to")) if j.get("time_to") else "—",
            str(pid) if pid is not None else "?",
            pname,
            _color_id_with_recency(conn_id, highlight_since),
        ]
        if not is_open:
            cells = [f"[dim]{c}[/dim]" for c in cells]
        table.add_row(*cells)
    console.print(table)


def _device_show_main(args) -> int:
    """Handle ``tos device show`` — read-only device inspection.

    Resolves a device by ``id_entity`` or ``(--serial, --subtype)`` and
    renders one or more sections. Defaults to all three; flag-restricted
    via ``--list``, ``--attributes``, ``--attributes-history`` (mutually
    exclusive).

    Sections:
      - **Header** — id, subtype, currently-open serial / model / status.
        Always printed in pretty mode (unless a flag suppresses it; see
        below).
      - **Current attributes** (``--attributes``) — currently-open
        attribute periods. Mirrors the TOS web UI 'Eiginleikar' panel.
      - **Attribute history** (``--attributes-history``) — full open +
        closed periods. Mirrors 'Saga eiginda tækis'.
      - **Parent history** (``--list``) — every join, open and closed,
        with parent names resolved via on-demand
        :meth:`TOSClient.get_entity_history` (cached per parent id).
        Mirrors 'Saga staðsetningar tækis'.

    Suspicion coloring:
      - **yellow** date_from / date_to matching ``2014-10-17`` (the
        fleet-wide metadata-cleanup-artifact pattern)
      - **red** status value ``bilað`` / ``óvirkt``
      - **dim** closed periods (date_to set) so open / current periods
        stand out
      - **cyan** id_attribute_value / id_connection — the values the
        operator copies into ACTION lines

    ``--json`` emits the raw entity history + parent_history payload as
    a single JSON object, bypassing the pretty-print path. Section flags
    are ignored when ``--json`` is set.
    """
    import json as _json

    from rich.console import Console

    from .api.tos_client import TOSClient
    from .devices import find_device

    if args.serial is None and args.id_entity is None:
        print(
            "tos device show requires either id_entity or --serial",
            file=sys.stderr,
        )
        return 2
    if args.serial is not None and args.subtype is None:
        print(
            "tos device show --serial requires --subtype to disambiguate",
            file=sys.stderr,
        )
        return 2

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    try:
        history = find_device(
            client,
            serial=args.serial,
            id_entity=args.id_entity,
            subtype=args.subtype,
        )
    except (LookupError, ValueError) as e:
        print(f"Device lookup failed: {e}", file=sys.stderr)
        return 1

    did = int(history["id_entity"])
    parent_history = client.get_parent_history(did)

    no_visits = bool(getattr(args, "no_visits", False))
    # Always fetch for the JSON path; for the pretty path skip the
    # HTTP when the operator opted out via --no-visits or picked an
    # explicit section flag that doesn't include visits.
    show_all_sections = not (
        args.section_list or args.section_attributes or args.section_attributes_history
    )
    want_visits = args.json or (show_all_sections and not no_visits)
    visits: List[Dict[str, Any]] = []
    if want_visits:
        try:
            visits = client.list_maintenance_visits(did) or []
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: list_maintenance_visits({did}) failed: {exc}",
                file=sys.stderr,
            )
            visits = []

    if args.json:
        payload = {
            "id_entity": did,
            "history": history,
            "parent_history": parent_history,
            "visits": visits,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console = Console()

    if show_all_sections or args.section_list or args.section_attributes:
        _render_show_header(console, history)

    if show_all_sections or args.section_attributes:
        console.print()
        _render_show_open_attributes(console, history, args)

    if show_all_sections or args.section_attributes_history:
        console.print()
        _render_show_attribute_history(console, history, args)

    if show_all_sections or args.section_list:
        console.print()
        _render_show_parent_history(console, client, parent_history, args)

    if show_all_sections and not no_visits:
        visible_visits, hidden = _trim_visits_for_show(visits, show_all=False)
        console.print()
        console.print(
            f"Recent vitjanir — {len(visible_visits)} record(s) for device {did}"
        )
        if hidden:
            console.print(
                f"  [dim]({hidden} more closed — use "
                f"`tos visit list --device {did}` to see them)[/dim]"
            )
        _render_visits_table(console, visible_visits, title="")
        if visible_visits:
            sample_id = visible_visits[0].get("id")
            if sample_id:
                console.print(
                    f"[dim]Drill: tos visit show {sample_id}  |  "
                    f"tos visit list --device {did}[/dim]"
                )

    return 0


def _substitute_id_in_triage(
    path: Path, placeholder: str, id_entity: int
) -> Dict[str, Any]:
    """Substitute ``<placeholder>`` with ``id_entity`` in a triage file in-place.

    Used by ``tos device add --triage PATH --placeholder TOKEN`` to drop a
    freshly-created entity's id into a waiting triage file, eliminating
    the copy-paste step between ``device add`` and ``audit apply``.

    Args:
        path: Triage file to update in-place. Read + write text, UTF-8.
        placeholder: Token name (without angle brackets). The actual
            match string is ``<TOKEN>``.
        id_entity: The new entity's id, substituted as ``str(id_entity)``.

    Returns:
        Dict with ``token`` (the angle-bracketed match string), ``count``
        (number of substitutions; 0 if the placeholder wasn't present),
        and ``written`` (True iff the file was modified — False on
        count==0, no write performed).

    Raises:
        OSError on read/write failure — caller surfaces to stderr.
    """
    token = f"<{placeholder}>"
    content = path.read_text(encoding="utf-8")
    count = content.count(token)
    if count == 0:
        return {"token": token, "count": 0, "written": False}
    path.write_text(content.replace(token, str(id_entity)), encoding="utf-8")
    return {"token": token, "count": count, "written": True}


def _device_main(argv):
    """Handle ``tos device ...`` subcommands.

    Step 3 of the device-warehouse interface — adds a brand-new device entity
    (gnss_receiver, antenna, radome, monument) to TOS with strict input
    validation, owner allow-list checking, IGS model normalisation, and a
    duplicate-serial guard (bypassable with ``--force``). Defaults to dry-run.
    """
    from . import device as device_helpers
    from .api.tos_writer import TOSWriter
    from .owners import OwnersCache

    p = argparse.ArgumentParser(
        prog="tos device",
        description="Manage TOS device entities (warehouse intake).",
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Add a new device entity to TOS.")
    p_add.add_argument(
        "--subtype",
        required=True,
        choices=device_helpers.VALID_SUBTYPES,
        help="Device subtype.",
    )
    p_add.add_argument("--serial", required=True, help="Device serial number.")
    p_add.add_argument(
        "--model",
        required=True,
        help="Equipment model. Normalised to IGS rcvr_ant.tab format.",
    )
    p_add.add_argument(
        "--owner",
        required=True,
        help="Owner label; must match an entry in the OwnersCache.",
    )
    p_add.add_argument("--location", required=True, help="Physical location.")
    p_add.add_argument(
        "--date-start",
        required=True,
        help="Start date for all attribute values (YYYY-MM-DD or "
        "YYYY-MM-DDTHH:MM:SS).",
    )
    p_add.add_argument("--firmware", help="Optional firmware_version attribute.")
    p_add.add_argument("--comment", help="Optional free-form comment attribute.")
    p_add.add_argument(
        "--galvos", help="Optional galvos (inventory/registration) number."
    )
    p_add.add_argument(
        "--force",
        action="store_true",
        help="Bypass the duplicate-serial guard from create_device.",
    )
    p_add.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Commit the writes. Without this flag, payloads are logged only.",
    )
    p_add.add_argument(
        "--owners-cache",
        help="Override the owners cache path "
        "(default: ~/.config/tostools/owners.yaml).",
    )
    p_add.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_add.add_argument("--port", type=int, default=443)
    p_add.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON summary instead of plain text.",
    )
    p_add.add_argument(
        "--triage",
        type=Path,
        default=None,
        help=(
            "After successful device creation, substitute the returned "
            "id_entity into a triage file in-place. Requires --placeholder. "
            "No-op in dry-run (no real id_entity returned). Example: "
            "--triage savi.txt --placeholder POLARX2_3102_ID will replace "
            "every '<POLARX2_3102_ID>' in savi.txt with the new id."
        ),
    )
    p_add.add_argument(
        "--placeholder",
        help=(
            "Token name to substitute in the --triage file. The actual "
            "match is the angle-bracketed form '<TOKEN>'. Required when "
            "--triage is given."
        ),
    )

    p_show = sub.add_parser(
        "show",
        help=(
            "Display everything TOS knows about one device: current "
            "attribute values, full attribute history, full parent "
            "(location/station) history."
        ),
    )
    p_show.add_argument(
        "id_entity",
        nargs="?",
        type=int,
        help=(
            "Device id_entity. May also be supplied as --id (matches "
            "the `tos audit show` calling convention). Mutually "
            "exclusive with --serial."
        ),
    )
    # Separate dest so argparse doesn't clobber `--id N` with the
    # positional's default=None during parsing. Merged into id_entity
    # in the dispatch handler below.
    p_show.add_argument(
        "--id",
        dest="id_flag",
        type=int,
        default=None,
        help=(
            "Alternative to the positional id_entity. Equivalent — "
            "`tos device show 16099` and `tos device show --id 16099` "
            "do the same thing. Provided so the same `--id N` syntax "
            "works across `tos audit show`, `tos device show`, and "
            "the drill-hint output of `tos station show`."
        ),
    )
    p_show.add_argument(
        "--serial",
        help=(
            "Look up by serial_number instead of id_entity. Requires "
            "--subtype to disambiguate."
        ),
    )
    p_show.add_argument(
        "--subtype",
        help=(
            "Required with --serial. TOS subtype code (gnss_receiver, "
            "antenna, radome, monument, ...)."
        ),
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", type=int, default=443)
    p_show.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw entity history + parent_history as JSON.",
    )
    section_group = p_show.add_mutually_exclusive_group()
    section_group.add_argument(
        "--list",
        dest="section_list",
        action="store_true",
        help=(
            "Print only the parent (location/station) history section. "
            "Mirrors the TOS web UI 'Saga staðsetningar tækis' panel."
        ),
    )
    section_group.add_argument(
        "--attributes",
        dest="section_attributes",
        action="store_true",
        help=(
            "Print only the currently-open attributes table. Mirrors the "
            "TOS web UI 'Eiginleikar' panel."
        ),
    )
    section_group.add_argument(
        "--attributes-history",
        dest="section_attributes_history",
        action="store_true",
        help=(
            "Print only the full attribute history (open + closed periods, "
            "with date_to and datatype columns). Mirrors the TOS web UI "
            "'Saga eiginda tækis' panel."
        ),
    )
    p_show.add_argument(
        "--highlight-since",
        type=int,
        default=None,
        metavar="ID_AV",
        help=(
            "Flag attribute_value / id_connection rows whose id is above "
            "this threshold with a bold-magenta ★ marker. TOS exposes no "
            "created_at, so the id_attribute_value sequence is the only "
            "soft signal of write-recency. Useful for spotting "
            "after-the-fact back-fills (which date_from alone cannot "
            "reveal). Reasonable thresholds: the 2014-10-17 fleet "
            "bulk-load sits at id_av ≈ 32000-35000; per-session writes "
            "land 5-10k higher each year. See CLAUDE.md 'Retrospective "
            "writes' section + memory project_tos_retrospective_writes_provenance_gap."
        ),
    )
    p_show.add_argument(
        "--no-visits",
        dest="no_visits",
        action="store_true",
        help=(
            "Suppress the Recent vitjanir section. Default-on; this device's "
            "vitjanir surface alongside the attribute / parent-history panels."
        ),
    )
    add_attribute_filter_arguments(p_show)

    p_list = sub.add_parser(
        "list",
        help=(
            "List devices currently joined to a station (by marker) or "
            "to a location (by name, e.g. warehouse). Mirrors the TOS web "
            "UI's per-station device panel."
        ),
    )
    parent_group = p_list.add_mutually_exclusive_group(required=True)
    parent_group.add_argument(
        "--station",
        help="Station marker (e.g. SAVI). Case-insensitive.",
    )
    parent_group.add_argument(
        "--location",
        help=(
            "Location name (e.g. 'B9 - Kjallari - Jörð' for the bench "
            "warehouse). Exact match, case-sensitive."
        ),
    )
    p_list.add_argument(
        "--all",
        action="store_true",
        help=(
            "Include closed (historical) joins. Default shows only "
            "currently-open joins (devices presently at the parent). "
            "Implicitly enabled by --date."
        ),
    )
    add_device_filter_arguments(p_list)
    p_list.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_list.add_argument("--port", type=int, default=443)
    p_list.add_argument(
        "--json",
        action="store_true",
        help="Emit the device rows as JSON instead of a rendered table.",
    )

    args = p.parse_args(argv)
    if args.action == "show":
        # Merge --id (id_flag) into id_entity so the downstream lookup
        # logic stays single-source. --id wins when both forms appear,
        # matching argparse's last-wins convention for repeated flags.
        if getattr(args, "id_flag", None) is not None:
            args.id_entity = args.id_flag
        return _device_show_main(args)
    if args.action == "list":
        return _device_list_main(args)
    if args.action != "add":
        p.error(f"unknown action: {args.action}")
        return 2

    # ---- Input validation ------------------------------------------------
    try:
        date_start = device_helpers.normalize_date_start(args.date_start)
    except ValueError as e:
        print(f"Invalid --date-start: {e}", file=sys.stderr)
        return 2

    cache = OwnersCache(args.owners_cache) if args.owners_cache else OwnersCache()
    known_owners = cache.load()
    if args.owner not in known_owners:
        print(
            f"Unknown owner: {args.owner!r}. "
            f"Run 'tos owners list' to see allowed values, or "
            f"'tos owners list --refresh' if you recently added one in TOS.",
            file=sys.stderr,
        )
        return 2

    try:
        igs_model = device_helpers.validate_model(args.subtype, args.model)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    required = device_helpers.build_required_attributes(
        serial=args.serial,
        model=igs_model,
        owner=args.owner,
        date_start=date_start,
    )
    optional = device_helpers.iter_optional_attributes(
        firmware=args.firmware,
        comment=args.comment,
        galvos=args.galvos,
    )

    # ---- Writer setup ----------------------------------------------------
    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    # ---- Create entity ---------------------------------------------------
    try:
        response = writer.create_device(args.subtype, required, force=args.force)
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg and not args.force:
            print(f"{msg}\nPass --force to add the duplicate anyway.", file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 1

    # In dry-run, response is a DryRunResult (no id_entity). In live mode,
    # response is the TOS API dict containing id_entity for the new entity.
    id_entity = None
    if isinstance(response, dict):
        id_entity = response.get("id_entity")

    # ---- Location join (parent area entity → child device) --------------
    # Replaces the old "location as a free-text attribute on the device"
    # approach; physical placement in TOS is conveyed via entity_connection.
    if dry_run or id_entity is None:
        if not args.json:
            print(
                f"DRY RUN: would resolve location {args.location!r} → entity_id "
                f"and create entity_connection(parent=<area>, "
                f"child={id_entity if id_entity is not None else '<new>'}, "
                f"time_from={date_start})"
            )
        connection_response = {"location": args.location, "dry_run": True}
    else:
        try:
            connection_response = writer.connect_device_to_location(
                id_device=id_entity,
                location_name=args.location,
                date_start=date_start,
            )
        except ValueError as e:
            print(
                f"Device created (id_entity={id_entity}) but location join "
                f"failed: {e}",
                file=sys.stderr,
            )
            return 1

    # ---- Optional attributes --------------------------------------------
    upsert_responses = []
    for code, value in optional:
        if dry_run or id_entity is None:
            print(
                f"DRY RUN: would upsert {code}={value!r} "
                f"from {date_start} on id_entity="
                f"{id_entity if id_entity is not None else '<new entity>'}"
            )
            upsert_responses.append({"code": code, "value": value, "dry_run": True})
        else:
            r = writer.upsert_attribute_value(
                id_entity, code=code, value=value, date_from=date_start
            )
            upsert_responses.append({"code": code, "value": value, "response": r})

    # ---- Summary ---------------------------------------------------------
    if args.json:
        import json as _json

        payload = {
            "subtype": args.subtype,
            "serial": args.serial,
            "model": igs_model,
            "owner": args.owner,
            "location": args.location,
            "date_start": date_start,
            "id_entity": id_entity,
            "dry_run": dry_run,
            "required_attributes": required,
            "optional_attributes": [{"code": c, "value": v} for c, v in optional],
            "upsert_results": upsert_responses,
            "location_connection": connection_response,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        suffix = " (dry-run)" if dry_run else ""
        id_str = id_entity if id_entity is not None else "<would be assigned>"
        print(
            f"Created {args.subtype} serial={args.serial} "
            f"id_entity={id_str}{suffix}"
        )
        if not dry_run and isinstance(connection_response, dict):
            conn_id = connection_response.get("id_connection")
            if conn_id is not None:
                print(
                    f"Connected to location {args.location!r} "
                    f"(connection id={conn_id})"
                )

    # ---- Triage-file substitution -------------------------------------------
    # Operator UX: paste the just-created id_entity into a waiting triage
    # file in one shot, so the device-add → triage-edit handoff is
    # deterministic instead of a copy-paste step. Two modes:
    #   1. --triage PATH --placeholder TOKEN  → auto-substitute in-place
    #   2. neither flag  → print a sed hint the operator can run themselves
    if args.triage is not None or args.placeholder is not None:
        if args.triage is None or args.placeholder is None:
            print(
                "--triage and --placeholder must be used together",
                file=sys.stderr,
            )
            return 2
    if dry_run or id_entity is None:
        if args.triage is not None and not args.json:
            print(
                f"Triage update skipped: dry-run / no real id_entity "
                f"returned (use --no-dry-run to substitute "
                f"<{args.placeholder}> in {args.triage}).",
                file=sys.stderr,
            )
    elif args.triage is not None and args.placeholder is not None:
        try:
            result = _substitute_id_in_triage(args.triage, args.placeholder, id_entity)
        except OSError as e:
            print(
                f"Could not read triage file {args.triage}: {e}",
                file=sys.stderr,
            )
            return 1
        if result["count"] == 0:
            print(
                f"Triage update: placeholder {result['token']!r} not found "
                f"in {args.triage} (no changes written).",
                file=sys.stderr,
            )
        elif not args.json:
            n = result["count"]
            print(
                f"Updated {args.triage}: {result['token']} → {id_entity} "
                f"({n} replacement{'s' if n != 1 else ''})"
            )
    elif id_entity is not None and not args.json:
        # Hint mode (always-on) — operator didn't pass --triage; nudge
        # them with a sed line they can paste if they're working with a
        # waiting triage file.
        print(
            f"\nTip: to drop this id into a triage file, run:\n"
            f"  sed -i 's/<TOKEN>/{id_entity}/g' <triage-file>\n"
            f"(or pass --triage <file> --placeholder TOKEN to do it in one "
            f"shot next time)"
        )
    return 0


def _audit_verify_from_rinex_main(args, client) -> int:
    """Handle ``tos audit verify-from-rinex --station X``.

    Thin CLI handler: delegates data collection to
    :func:`audit_verify_from_rinex.audit_station_verify_from_rinex`,
    then renders the report either as rich tables or as JSON.

    Exit codes:
      0 = clean (no brand transitions, no gaps, no actionable verdicts)
      1 = discrepancies surfaced (caller should review)
      2 = lookup / usage error (archive root missing, no timeline)
    """
    import json as _json

    from . import audit_verify_from_rinex as avfr_mod

    try:
        report = avfr_mod.audit_station_verify_from_rinex(
            client,
            args.station,
            archive_root=args.archive_root,
            min_gap_days=args.min_gap_days,
        )
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    if report.timeline_count == 0:
        print(
            f"No archived data for station {args.station!r} under "
            f"{report.archive_root}",
            file=sys.stderr,
        )
        return 2

    if args.json:
        payload = {
            "station": report.station,
            "archive_root": str(report.archive_root),
            "timeline_count": report.timeline_count,
            "first": report.first_day,
            "last": report.last_day,
            "brand_runs": [
                {
                    "family": r.family,
                    "from": r.start.isoformat(),
                    "to": r.end.isoformat(),
                    "days": r.days,
                    "rinex_only_days": r.rinex_only_days,
                    "ambiguous": r.ambiguous,
                }
                for r in report.brand_runs
            ],
            "brand_transitions": [
                {
                    "date_before": t.date_before.isoformat(),
                    "date_after": t.date_after.isoformat(),
                    "family_before": t.family_before,
                    "family_after": t.family_after,
                }
                for t in report.brand_transitions
            ],
            "data_gaps": [
                {
                    "from": g.last_day_with_data.isoformat(),
                    "to": g.next_day_with_data.isoformat(),
                    "duration_days": g.duration_days,
                }
                for g in report.data_gaps
            ],
            "rinex_only_spans": [
                {
                    "from": s.start.isoformat(),
                    "to": s.end.isoformat(),
                    "days": s.days,
                }
                for s in report.rinex_only_spans
            ],
            "tos_receivers": [
                {
                    "id_entity": r.id_entity,
                    "serial": r.serial,
                    "model": r.model,
                    "time_from": r.time_from,
                    "time_to": r.time_to,
                    "id_connection": r.id_connection,
                    "expected_family": r.expected_family,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in report.receivers
            ],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 1 if report.has_findings else 0

    _print_rinex_audit_report(report, min_gap_days=args.min_gap_days)
    return 1 if report.has_findings else 0


def _print_rinex_audit_report(report, *, min_gap_days: float) -> None:
    """Render a :class:`StationRinexReport` as rich tables.

    Mirrors the byte-stable output of the pre-Phase-3 inline
    implementation: archive header, brand timeline table, brand
    transitions, data gaps, RINEX-only spans, TOS receiver joins
    cross-reference, and the bottom "Suggested ACTION lines" block.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(
        f"Station [bold]{report.station}[/bold] vs archive at "
        f"[cyan]{report.archive_root}[/cyan]"
    )
    console.print(
        f"  archived days: [bold]{report.timeline_count}[/bold]  |  "
        f"first: {report.first_day}  |  last: {report.last_day}"
    )

    if report.brand_runs:
        console.print()
        t_runs = Table(
            title=(
                "Archive brand timeline "
                "(rinex-only days absorbed into surrounding brand)"
            )
        )
        t_runs.add_column("family")
        t_runs.add_column("from")
        t_runs.add_column("to")
        t_runs.add_column("days", justify="right")
        t_runs.add_column("rinex-only inside", justify="right")
        for r in report.brand_runs:
            family_label = (
                f"[yellow]{r.family} (ambiguous)[/yellow]" if r.ambiguous else r.family
            )
            rinex_cell = str(r.rinex_only_days) if r.rinex_only_days else ""
            t_runs.add_row(
                family_label,
                str(r.start),
                str(r.end),
                str(r.days),
                rinex_cell,
            )
        console.print(t_runs)

    if report.brand_transitions:
        console.print()
        t_trans = Table(title="Brand transitions (real receiver swaps per archive)")
        t_trans.add_column("date_before")
        t_trans.add_column("family_before")
        t_trans.add_column("date_after")
        t_trans.add_column("family_after")
        for t in report.brand_transitions:
            t_trans.add_row(
                str(t.date_before),
                t.family_before,
                f"[yellow]{t.date_after}[/yellow]",
                f"[yellow]{t.family_after}[/yellow]",
            )
        console.print(t_trans)

    if report.data_gaps:
        console.print()
        t_gaps = Table(title=f"Data gaps ≥{min_gap_days} days")
        t_gaps.add_column("last day with data")
        t_gaps.add_column("next day with data")
        t_gaps.add_column("duration (days)", justify="right")
        for g in report.data_gaps:
            t_gaps.add_row(
                str(g.last_day_with_data),
                str(g.next_day_with_data),
                str(g.duration_days),
            )
        console.print(t_gaps)

    if report.rinex_only_spans:
        console.print()
        t_ronly = Table(
            title="RINEX-only spans (raw missing — possible data-loss windows)"
        )
        t_ronly.add_column("from")
        t_ronly.add_column("to")
        t_ronly.add_column("days", justify="right")
        for s in report.rinex_only_spans:
            t_ronly.add_row(str(s.start), str(s.end), str(s.days))
        console.print(t_ronly)

    if report.receivers:
        console.print()
        t_tos = Table(title="TOS receiver joins (all, incl. closed)")
        t_tos.add_column("id_entity", justify="right")
        t_tos.add_column("serial")
        t_tos.add_column("model")
        t_tos.add_column("time_from")
        t_tos.add_column("time_to")
        t_tos.add_column("verdict")
        _VERDICT_STYLE = {
            "ok": "green",
            "no_archive_coverage": "yellow",
            "unmapped_model": "dim",
            "rinex_only": "yellow",
            "late_start": "red",
            "early_end": "red",
            "join_too_wide": "red",
            "wrong_brand": "red",
        }
        for r in report.receivers:
            style = _VERDICT_STYLE.get(r.status, "white")
            verdict_text = f"[{style}]{r.status}[/{style}]: {r.detail}"
            t_tos.add_row(
                str(r.id_entity),
                str(r.serial or "?"),
                str(r.model or "?"),
                r.time_from or "?",
                r.time_to or "—",
                verdict_text,
            )
        console.print(t_tos)

        if report.suggested_actions:
            console.print()
            console.print(
                "[bold]Suggested ACTION lines for triage[/bold] "
                "(paste into a triage file then `tos audit apply`):"
            )
            for line in report.suggested_actions:
                console.print(f"  {line}")


def _station_main(argv):
    """Handle ``tos station <verb> <STN>`` — top-level station orchestration.

    Verbs:

      ``triage``  Run all audits + emit a single combined ACTION-style
                  triage file consumable by ``tos audit apply``. All
                  ACTION lines are commented out by default — operator
                  opts in via uncomment + edit.

      ``verify``  Re-run all audits as a pass/fail oracle. Exits 0 if
                  clean (no findings), 1 if findings remain, 2 if any
                  audit failed. Closes the ``apply → verify`` loop.

      ``show``    Display the station's current TOS state: identity,
                  open attribute periods, currently-joined child
                  devices. ``--all`` adds attribute history + closed
                  joins; ``--device`` delegates to ``tos device list``.

    Exit codes: 0 on success / clean, 1 on findings / lookup miss,
    2 on audit failure or usage error.
    """
    from pathlib import Path

    p = argparse.ArgumentParser(
        prog="tos station",
        description=(
            "Top-level station orchestration. Aggregates the individual "
            "audit verbs into single-command workflows.\n\n"
            "Verbs:\n"
            "  triage <STATION>    Run all audits + emit a combined "
            "triage file (commented ACTIONs)\n"
            "  verify <STATION>    Re-run audits; exit 0 clean / 1 "
            "findings / 2 failure\n"
            "  show <STATION>      Identity + open attributes + "
            "joined devices (--all adds history)\n\n"
            "Example workflow:\n"
            "  tos station triage HEDI                # generates "
            "data/triage/hedi/hedi_audit_<DATE>.txt\n"
            "  $EDITOR <file>                         # uncomment / fill "
            "<FILL> placeholders\n"
            "  tos audit apply <file>                 # dry-run\n"
            "  tos audit apply <file> --apply         # commit\n"
            "  tos station verify HEDI                # confirm clean"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", required=True)

    p_tri = sub.add_parser(
        "triage",
        help=(
            "Run all audits on a station + emit a single combined "
            "triage file. Suggested ACTION lines are commented out by "
            "default — operator opts in."
        ),
    )
    p_tri.add_argument("station", help="Station marker (e.g. HEDI) or name.")
    p_tri.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output path for the triage file. Default: "
            "data/triage/<station>/<station>_audit_<YYYYMMDD>.txt "
            "(per-station subdirectory under the repo's `data/` "
            "convention)."
        ),
    )
    p_tri.add_argument(
        "--stdout",
        action="store_true",
        help="Print the triage file to stdout instead of writing to disk.",
    )
    _add_archive_arguments(p_tri)
    _add_coverage_arguments(p_tri)

    p_ver = sub.add_parser(
        "verify",
        help=(
            "Re-run all audits as a pass/fail oracle. Exits 0 clean, "
            "1 findings remain, 2 audit failure."
        ),
        description=(
            "Run every available audit (missing-attributes, "
            "attribute-dates) against the station and aggregate the "
            "pass/fail signal. Unlike `tos station triage`, this writes "
            "nothing to disk — it's the verify half of the "
            "apply → verify loop.\n\n"
            "Exit codes:\n"
            "  0  clean   — every audit ran and reported no violations\n"
            "  1  findings — at least one audit has surviving violations\n"
            "  2  failure  — at least one audit raised (e.g. TOS\n"
            "                lookup error, malformed catalog). Distinct\n"
            "                from `findings` so cron / CI can tell\n"
            "                'station needs work' from 'oracle broken'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ver.add_argument("station", help="Station marker (e.g. HEDI) or name.")
    p_ver.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help=(
            "Override the suppression file path. Each audit consults its "
            "own filename (attribute_dates.txt / missing_attributes.txt) "
            "in this directory if given."
        ),
    )
    p_ver.add_argument(
        "--no-suppressions",
        action="store_true",
        help=(
            "Bypass the per-audit SUPPRESS files entirely. Every rule "
            "hit is reported — useful to verify what a stale SUPPRESS "
            "line is hiding."
        ),
    )
    p_ver.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Override the attribute-codes catalog path.",
    )
    p_ver.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_ver.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Pass --verbose through to the per-audit pretty-printers "
            "(SUPPRESS hints, anchor sources, silenced entries)."
        ),
    )
    _add_archive_arguments(p_ver)
    _add_coverage_arguments(p_ver)

    p_show = sub.add_parser(
        "show",
        help=(
            "Show a station's current TOS state: identity, open attribute "
            "periods, currently-joined child devices."
        ),
        description=(
            "Read-only station inspection. Modes:\n\n"
            "  (default)    identity + open attribute periods + "
            "currently-joined children + contacts\n"
            "  --all        adds closed attribute periods, a separate "
            "Past-devices table (closed joins), and a Device-attribute-"
            "history table (closed periods on currently-joined devices — "
            "firmware upgrades, status flips, etc.)\n"
            "  --attributes only the attribute periods (open by default; "
            "open + closed with --all). Suppresses joined-devices, "
            "contacts, and the drill hint.\n"
            "  --device     delegate to `tos device list --station "
            "<STN>` — convenience pass-through"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_show.add_argument("station", help="Station marker (e.g. HEDI) or name.")
    p_show.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help=(
            "Include attribute history (closed periods), closed joins, "
            "and per-device closed attribute history. Without this flag "
            "only currently-open state is shown."
        ),
    )
    # --attributes and --device are mutually exclusive view modes:
    # one prints only attributes, the other delegates entirely to
    # `tos device list`.
    mode_group = p_show.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--attributes",
        dest="attributes_only",
        action="store_true",
        help=(
            "Only show the attribute periods (open by default; open + "
            "closed with --all). Suppresses joined-devices, contacts, "
            "and the drill hint. Mirrors `tos device show --attributes`."
        ),
    )
    mode_group.add_argument(
        "--device",
        dest="device_mode",
        action="store_true",
        help=(
            "Delegate to `tos device list --station <STN>`. With --all, "
            "passes --all through (closed joins included). Other show "
            "flags are ignored in this mode."
        ),
    )
    p_show.add_argument(
        "--no-visits",
        dest="no_visits",
        action="store_true",
        help=(
            "Suppress the Recent vitjanir section (aggregated from the "
            "station + currently-joined devices). Default-on."
        ),
    )
    p_show.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    if args.verb == "triage":
        return _station_triage_main(args)
    if args.verb == "verify":
        return _station_verify_main(args)
    if args.verb == "show":
        return _station_show_main(args)

    return 2


def _station_triage_main(args) -> int:
    """Generate and write a combined triage file for one station."""
    from .api.tos_client import TOSClient
    from .station_triage import (
        default_triage_path,
        format_station_triage,
        generate_station_triage,
    )

    client = TOSClient()
    report = generate_station_triage(
        args.station,
        client=client,
        with_archive=getattr(args, "with_archive", False),
        archive_root=getattr(args, "archive_root", None),
        min_gap_days=getattr(args, "archive_min_gap_days", 30.0),
        with_coverage=getattr(args, "with_coverage", False),
        coverage_since=getattr(args, "coverage_since", None),
        coverage_window_days=getattr(args, "coverage_window_days", 7),
    )
    rendered = format_station_triage(report)

    if args.stdout:
        print(rendered, end="")
        return 0

    out_path = args.out or default_triage_path(args.station)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")

    print(
        f"Wrote triage file: {out_path}\n"
        f"  station_id: {report.station_id}\n"
        f"  findings:   {report.total_findings}\n"
        f"\n"
        f"Next:\n"
        f"  $EDITOR {out_path}\n"
        f"  tos audit apply {out_path}            # dry-run\n"
        f"  tos audit apply {out_path} --apply    # commit"
    )
    return 0


def _station_verify_main(args) -> int:
    """Run every audit against a station; exit 0 clean / 1 findings / 2 failure.

    Reuses :func:`generate_station_triage` as the aggregator — the same
    sub-report objects (with ``notes`` for failures) feed both the
    triage renderer and this oracle. Output reuses the per-audit
    pretty-printers in pretty mode and the per-audit ``_to_dict``
    serializers in JSON mode.
    """
    import json as _json

    from .api.tos_client import TOSClient
    from .station_triage import generate_station_triage

    client = TOSClient()
    report = generate_station_triage(
        args.station,
        client=client,
        use_suppressions=not args.no_suppressions,
        suppressions_path=args.suppressions,
        catalog_path=args.catalog,
        with_archive=getattr(args, "with_archive", False),
        archive_root=getattr(args, "archive_root", None),
        min_gap_days=getattr(args, "archive_min_gap_days", 30.0),
        with_coverage=getattr(args, "with_coverage", False),
        coverage_since=getattr(args, "coverage_since", None),
        coverage_window_days=getattr(args, "coverage_window_days", 7),
    )

    # Status + exit code from the canonical oracle definitions —
    # same mapping as `tos fleet status` so the two verbs cannot
    # drift on what "clean" / "findings" / "failed" mean.
    from .station_triage import (
        STATUS_EXIT_CODE,
        STATUS_MARK,
        classify_station_triage,
    )

    status = classify_station_triage(report)
    exit_code = STATUS_EXIT_CODE[status]

    if args.json:
        audits_payload: Dict[str, Any] = {
            "missing_attributes": (
                _missing_attributes_report_to_dict(report.missing)
                if report.missing is not None
                else None
            ),
            "attribute_dates": (
                _attribute_date_report_to_dict(report.dates)
                if report.dates is not None
                else None
            ),
        }
        if getattr(args, "with_archive", False):
            # Only surface the slot when the operator opted in — keeps
            # the default payload shape unchanged.
            audits_payload["verify_from_rinex"] = (
                _rinex_report_to_dict(report.rinex)
                if report.rinex is not None
                else None
            )
        payload: Dict[str, Any] = {
            "station": report.station,
            "station_id": report.station_id,
            "status": status,
            "exit_code": exit_code,
            "audits": audits_payload,
            "notes": list(report.notes),
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code

    # ---- Pretty text output ----
    marker = STATUS_MARK[status]
    summary_bits: List[str] = []
    if report.missing is not None:
        summary_bits.append(
            f"missing-attributes: {len(report.missing.violations)} violation(s)"
        )
    else:
        summary_bits.append("missing-attributes: (failed)")
    if report.dates is not None:
        summary_bits.append(
            f"attribute-dates: {len(report.dates.violations)} violation(s)"
        )
    else:
        summary_bits.append("attribute-dates: (failed)")
    if getattr(args, "with_archive", False):
        if report.rinex is not None:
            summary_bits.append(
                f"verify-from-rinex: {report.rinex.finding_count} finding(s)"
            )
        else:
            summary_bits.append("verify-from-rinex: (failed)")

    print(
        f"VERIFY {report.station} (id_entity={report.station_id}): "
        f"{marker} {status} — {report.total_findings} finding(s)"
    )
    for bit in summary_bits:
        print(f"  {bit}")

    if report.notes:
        print()
        print("Audit failures:")
        for note in report.notes:
            print(f"  ‽ {note}")

    if report.missing is not None and report.missing.violations:
        print()
        _print_missing_attributes_report(report.missing, verbose=args.verbose)
    if report.dates is not None and report.dates.violations:
        print()
        _print_attribute_date_report(report.dates, verbose=args.verbose)
    if report.rinex is not None and report.rinex.has_findings:
        print()
        _print_rinex_audit_report(
            report.rinex,
            min_gap_days=getattr(args, "archive_min_gap_days", 30.0),
        )

    return exit_code


def _rinex_report_to_dict(report) -> Dict[str, Any]:
    """JSON-serializable view of a :class:`StationRinexReport`.

    Mirrors the shape ``tos audit verify-from-rinex --json`` emits, so
    consumers of ``tos station verify --with-archive --json`` see a
    familiar payload nested under ``audits.verify_from_rinex``.
    """
    return {
        "station": report.station,
        "station_id": report.station_id,
        "archive_root": str(report.archive_root),
        "timeline_count": report.timeline_count,
        "first": report.first_day,
        "last": report.last_day,
        "brand_transitions": [
            {
                "date_before": t.date_before.isoformat(),
                "date_after": t.date_after.isoformat(),
                "family_before": t.family_before,
                "family_after": t.family_after,
            }
            for t in report.brand_transitions
        ],
        "data_gaps": [
            {
                "from": g.last_day_with_data.isoformat(),
                "to": g.next_day_with_data.isoformat(),
                "duration_days": g.duration_days,
            }
            for g in report.data_gaps
        ],
        "tos_receivers": [
            {
                "id_entity": r.id_entity,
                "serial": r.serial,
                "model": r.model,
                "time_from": r.time_from,
                "time_to": r.time_to,
                "status": r.status,
                "detail": r.detail,
            }
            for r in report.receivers
        ],
        "suggested_actions": list(report.suggested_actions),
    }


def _station_show_main(args) -> int:
    """Display a station's current TOS state.

    Default view: identity + open attribute periods + currently-joined
    children. ``--all`` extends to attribute history + closed joins.
    ``--device`` short-circuits to ``tos device list --station <STN>``.
    """
    import json as _json
    from types import SimpleNamespace

    from rich.console import Console

    from .api.tos_client import TOSClient
    from .devices import open_attribute

    # --device delegates straight to the device-list handler. Build the
    # namespace it expects (matches the parser in `tos device list`).
    if args.device_mode:
        delegated = SimpleNamespace(
            station=args.station,
            location=None,
            all=args.show_all,
            date=None,
            subtype=None,
            model=None,
            status=None,
            serial=None,
            json=args.json,
            server=args.server,
            port=args.port,
        )
        return _device_list_main(delegated)

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    station_id = _resolve_parent_id(client, station_marker=args.station)
    if station_id is None:
        print(
            f"No station found for marker {args.station!r}",
            file=sys.stderr,
        )
        return 1

    history = client.get_entity_history(station_id)
    if not history:
        print(
            f"Station id_entity={station_id} returned no history payload",
            file=sys.stderr,
        )
        return 1

    station_name = open_attribute(history, "name")
    marker = open_attribute(history, "marker")
    include_closed = args.show_all
    attributes_only = getattr(args, "attributes_only", False)

    # In attributes-only mode we skip the child-device enumeration
    # entirely (one HTTP per child × <up to 10 children>) since
    # nothing downstream uses it. Saves real time on a slow link.
    children_rows: List[Dict[str, Any]] = []
    child_histories: Dict[int, Dict[str, Any]] = {}
    open_rows: List[Dict[str, Any]] = []
    closed_rows: List[Dict[str, Any]] = []
    contacts: List[Dict[str, Any]] = []

    if not attributes_only:
        children_conns = history.get("children_connections") or []
        if not include_closed:
            active_conns = [c for c in children_conns if c.get("time_to") is None]
        else:
            active_conns = children_conns

        for conn in active_conns:
            child_id_raw = conn.get("id_entity_child")
            if child_id_raw is None:
                continue
            try:
                child_id = int(child_id_raw)
            except (TypeError, ValueError):
                continue
            child = client.get_entity_history(child_id) or {}
            child_histories[child_id] = child
            children_rows.append(
                {
                    "id_entity": child_id,
                    "subtype": child.get("code_entity_subtype") or "?",
                    "serial": open_attribute(child, "serial_number") or "?",
                    "model": open_attribute(child, "model") or "?",
                    "status": open_attribute(child, "status") or "—",
                    "time_from": conn.get("time_from") or "?",
                    "time_to": conn.get("time_to"),
                    "id_connection": conn.get("id_entity_connection") or conn.get("id"),
                }
            )

        open_rows = sorted(
            (r for r in children_rows if r["time_to"] is None),
            key=_device_row_sort_key,
        )
        closed_rows = sorted(
            (r for r in children_rows if r["time_to"] is not None),
            key=_device_row_sort_key,
        )

        # Station contacts (owner / operator / point-of-contact). The
        # endpoint returns an empty list when none are mapped, which the
        # renderer surfaces as a single-line "(none)" placeholder.
        try:
            contacts = client.get_contacts(station_id) or []
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: get_contacts({station_id}) failed: {exc}",
                file=sys.stderr,
            )
            contacts = []

    # Aggregate vitjanir from the station + currently-joined devices.
    # Forward-compatible with Phase C (lifecycle tracker): when device-
    # attached vitjanir start landing on GPS receivers / antennas, they
    # surface here automatically without an additional roundtrip beyond
    # the per-child loop already done above. Each row gets a
    # ``__source_label`` field so the renderer can show attribution
    # ("station HEDI" vs "device 4830 (gnss_receiver)").
    aggregated_visits: List[Dict[str, Any]] = []
    no_visits = bool(getattr(args, "no_visits", False))
    if not attributes_only and not no_visits:
        try:
            station_visits = client.list_maintenance_visits(station_id) or []
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: list_maintenance_visits({station_id}) failed: {exc}",
                file=sys.stderr,
            )
            station_visits = []
        station_label_compact = marker or station_name or f"id={station_id}"
        for v in station_visits:
            v_copy = dict(v)
            v_copy["__source_label"] = f"station {station_label_compact}"
            v_copy["__source_kind"] = "station"
            v_copy["__source_id"] = station_id
            aggregated_visits.append(v_copy)
        # Currently-joined children only — closed joins' visits are out
        # of scope for the "what's going on at this station now" view.
        for row in open_rows:
            child_id = row["id_entity"]
            try:
                child_visits = client.list_maintenance_visits(child_id) or []
            except Exception as exc:  # noqa: BLE001
                print(
                    f"warning: list_maintenance_visits({child_id}) failed: {exc}",
                    file=sys.stderr,
                )
                child_visits = []
            child_label = f"device {child_id} ({row['subtype']})"
            for v in child_visits:
                v_copy = dict(v)
                v_copy["__source_label"] = child_label
                v_copy["__source_kind"] = "device"
                v_copy["__source_id"] = child_id
                aggregated_visits.append(v_copy)

    if args.json:
        payload = {
            "id_entity": station_id,
            "marker": marker,
            "name": station_name,
            "subtype": history.get("code_entity_subtype"),
            "include_closed": include_closed,
            "attributes_only": attributes_only,
            "history": history if include_closed else None,
            "children": children_rows,
            "children_open": open_rows,
            "children_closed": closed_rows if include_closed else [],
            "contacts": contacts,
            "visits": aggregated_visits,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console = Console()
    _render_station_show_header(console, history, station_id)

    console.print()
    _render_show_open_attributes(
        console,
        history,
        args=None,
        priority_codes=_STATION_ATTRIBUTE_DISPLAY_ORDER,
        colorize_values=True,
    )

    if include_closed:
        console.print()
        _render_show_attribute_history(
            console,
            history,
            args=None,
            closed_only=True,
            priority_codes=_STATION_ATTRIBUTE_DISPLAY_ORDER,
            colorize_values=True,
        )

    # --attributes short-circuits everything below: no devices, no
    # contacts, no drill hint. Operator opted into a focused view.
    if attributes_only:
        return 0

    parent_label = marker or station_name or str(station_id)

    console.print()
    _render_joined_devices_table(
        console,
        open_rows,
        title=f"Joined devices — {len(open_rows)} open join(s) at {parent_label}",
        empty_note="(no currently-open joins)",
    )

    if include_closed:
        console.print()
        _render_joined_devices_table(
            console,
            closed_rows,
            title=(
                f"Past devices — {len(closed_rows)} closed join(s) at "
                f"{parent_label}"
            ),
            empty_note="(no closed joins)",
        )

        # Per-currently-joined-device closed attribute periods.
        # Surfaces firmware bumps, status transitions, and any other
        # attribute history that affected the station while a given
        # device was deployed — the operational "what changed?" view.
        console.print()
        _render_device_attribute_history(console, open_rows, child_histories)

    console.print()
    _render_station_contacts(console, contacts)

    if not no_visits:
        visible_visits, hidden = _trim_visits_for_show(
            aggregated_visits, show_all=include_closed
        )
        console.print()
        console.print(
            f"Recent vitjanir — {len(visible_visits)} record(s) "
            f"(aggregated from station + {len(open_rows)} joined device(s))"
        )
        if hidden:
            console.print(
                f"  [dim]({hidden} more closed — use --all to see them)[/dim]"
            )
        _render_visits_table(
            console,
            visible_visits,
            title="",
            source_label_col="source",
        )
        if aggregated_visits:
            sample_id = visible_visits[0].get("id") if visible_visits else None
            if sample_id:
                console.print(
                    f"[dim]Drill: tos visit show {sample_id}  |  "
                    f"tos visit list --station {args.station}[/dim]"
                )

    console.print()
    _render_show_drill_hint(
        console,
        station=args.station,
        open_rows=open_rows,
        contacts=contacts,
    )

    return 0


def _render_station_contacts(
    console,
    contacts: List[Dict[str, Any]],
) -> None:
    """Render station contacts (owner / operator / point-of-contact).

    Sources rows from :meth:`TOSClient.get_contacts` (one HTTP per
    station). Shape per row: ``role``, ``role_is``, ``name``,
    ``organization``, ``phone_primary``, ``address``,
    ``per_time_from`` / ``per_time_to``. We surface the bare minimum
    operationally useful subset; the JSON payload still carries the
    full row dict for automation.

    The role column is colour-coded by ``role`` so owner / operator /
    contact pop visually — useful in IGS site-log handover context
    where the owner agency drives the publication permissions.
    """
    from rich.table import Table

    title = f"Contacts — {len(contacts)} record(s)"
    if not contacts:
        console.print(title)
        console.print("  (no contacts mapped)")
        return

    # Stable display order: owner first, then operator, then
    # everything else. Within each role group, sort by
    # per_time_from ascending so the contact timeline reads
    # chronologically.
    role_priority = {"owner": 0, "operator": 1}

    def _key(row: Dict[str, Any]) -> tuple:
        return (
            role_priority.get((row.get("role") or "").lower(), 99),
            row.get("per_time_from") or "",
        )

    ordered = sorted(contacts, key=_key)
    role_style = {"owner": "bold cyan", "operator": "bold green"}

    table = Table(title=title)
    table.add_column("id", justify="right")
    table.add_column("role")
    table.add_column("name / organization")
    table.add_column("phone")
    table.add_column("address")
    table.add_column("since")
    table.add_column("until")
    for c in ordered:
        role = (c.get("role") or "").lower()
        role_label = c.get("role_is") or c.get("role") or "?"
        style = role_style.get(role)
        role_cell = f"[{style}]{role_label}[/{style}]" if style else role_label
        name = c.get("name") or c.get("organization") or "?"
        phone = c.get("phone_primary") or "—"
        address = c.get("address") or "—"
        since = (c.get("per_time_from") or "")[:10] or "?"
        until_raw = c.get("per_time_to")
        until = until_raw[:10] if until_raw else "—"
        # id_contact is the canonical entity id (drilling target via
        # /entity/<id>); id_contact_entity_relationship is the join
        # row's id. Surface id_contact since it's the more useful
        # value for further inspection.
        table.add_row(
            _color_id(c.get("id_contact")),
            role_cell,
            name,
            phone,
            address,
            since,
            until,
        )
    console.print(table)


def _render_joined_devices_table(
    console,
    rows: List[Dict[str, Any]],
    *,
    title: str,
    empty_note: str,
) -> None:
    """Render one joined-devices table. Shared by open / closed sections."""
    from rich.table import Table

    if not rows:
        console.print(title)
        console.print(f"  {empty_note}")
        return

    table = Table(title=title)
    table.add_column("id", justify="right")
    table.add_column("subtype")
    table.add_column("serial")
    table.add_column("model")
    table.add_column("status")
    table.add_column("since")
    table.add_column("until")
    table.add_column("conn", justify="right")
    for r in rows:
        tf = str(r["time_from"])[:19]
        tt = str(r["time_to"])[:19] if r["time_to"] else "—"
        table.add_row(
            _color_id(r["id_entity"]),
            _color_subtype(r["subtype"]),
            str(r["serial"]),
            str(r["model"]),
            _color_status(r["status"]),
            _color_date(tf),
            _color_date(tt) if r["time_to"] else "—",
            _color_id(r["id_connection"]),
        )
    console.print(table)


def _render_device_attribute_history(
    console,
    open_rows: List[Dict[str, Any]],
    child_histories: Dict[int, Dict[str, Any]],
) -> None:
    """Render closed attribute periods for currently-joined devices.

    Surfaces operationally relevant device-side history (firmware
    upgrades, status transitions, identity rewrites) for each of the
    station's currently-joined children. Combined into one table sorted
    by device id then date_from so a firmware-bump sequence reads
    chronologically.
    """
    from rich.table import Table

    from .devices import attribute_periods

    table = Table(
        title=(
            "Device attribute history " "(closed periods on currently-joined devices)"
        )
    )
    table.add_column("id", justify="right")
    table.add_column("subtype")
    table.add_column("code")
    table.add_column("value")
    table.add_column("date_from")
    table.add_column("date_to")
    table.add_column("id_attribute_value", justify="right")

    rows_added = 0
    for child in open_rows:
        child_id = child["id_entity"]
        history = child_histories.get(child_id)
        if not history:
            continue
        by_code = attribute_periods(history)
        # Flatten then filter to closed periods only; sort by date_from
        # so per-code transitions (firmware_version 4.85 → 5.10 → 6.00)
        # read chronologically.
        closed: List[tuple[str, Dict[str, Any]]] = []
        for code, periods in by_code.items():
            for p in periods:
                if p.get("date_to") is not None:
                    closed.append((code, p))
        closed.sort(key=lambda kp: (kp[1].get("date_from") or "", kp[0]))
        for code, p in closed:
            value = p.get("value")
            rendered_value = (
                _color_status(value)
                if code == "status"
                else (str(value) if value is not None else "—")
            )
            table.add_row(
                _color_id(child_id),
                _color_subtype(child["subtype"]),
                code,
                rendered_value,
                _color_date(p.get("date_from")),
                _color_date(p.get("date_to")),
                _color_id(p.get("id_attribute_value")),
            )
            rows_added += 1

    if rows_added == 0:
        console.print(
            "Device attribute history (closed periods on currently-joined "
            "devices): (none)"
        )
        return
    console.print(table)


def _render_show_drill_hint(
    console,
    *,
    station: str,
    open_rows: List[Dict[str, Any]],
    contacts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Print a tail block suggesting how to drill into a child device.

    Picks one representative device id per subtype seen in
    ``open_rows`` so the suggested ``tos device show --id N`` lines
    are copy-pasteable against the very station that was just
    rendered. Order: gnss_receiver, antenna, monument — typically the
    GPS quartet the operator cares about most.

    ``contacts`` (optional) supplies the rows from the Contacts
    section. When at least one contact is present, the hint includes
    a ``tos contact show --id N`` line referencing the first contact's
    ``id_contact`` (a different namespace from id_entity).
    """
    # Map subtype → first open device row of that subtype (preserves
    # encounter order from open_rows). Lets us pick one example per
    # subtype without dragging the whole list into the hint.
    by_subtype: Dict[str, Dict[str, Any]] = {}
    for r in open_rows:
        by_subtype.setdefault(r["subtype"], r)

    ordered: List[Dict[str, Any]] = []
    for sub in _DEVICE_SUBTYPE_DISPLAY_ORDER:
        if sub in by_subtype:
            ordered.append(by_subtype[sub])
    # Anything else (digitizer, sim_card, ...) tacked on after the
    # GPS quartet, in encounter order.
    for sub, row in by_subtype.items():
        if sub not in _DEVICE_SUBTYPE_DISPLAY_ORDER:
            ordered.append(row)

    console.print("[bold]Drill deeper:[/bold]")
    if ordered:
        example = ordered[0]
        example_id = example["id_entity"]
        example_subtype = example["subtype"]
        example_label = f"  # {example_subtype} {example['serial']}"
        console.print(f"  tos device show --id {example_id}{example_label}")
        console.print(
            f"  tos device show --id {example_id} --attributes-history "
            "  # firmware + status transitions"
        )
        console.print(
            f"  tos audit timeline {example_id}"
            "                   # complete join chronology"
        )
        if len(ordered) > 1:
            other_ids = ", ".join(str(r["id_entity"]) for r in ordered[1:])
            console.print(f"  # other open devices: {other_ids}")
    if contacts:
        first_id_contact = contacts[0].get("id_contact")
        if first_id_contact is not None:
            console.print(
                f"  tos contact show --id {first_id_contact}"
                "             # contact entity (different namespace from id_entity)"
            )
    console.print(
        f"  tos station verify {station}"
        "                # re-run audits as pass/fail oracle"
    )
    console.print(
        f"  tos station triage {station}"
        "                # emit combined triage file (commented ACTIONs)"
    )


def _render_station_show_header(
    console, history: Dict[str, Any], station_id: int
) -> None:
    """One-line station identity summary mirroring `_render_show_header`."""
    from .devices import open_attribute

    subtype = history.get("code_entity_subtype") or "?"
    marker = open_attribute(history, "marker") or "?"
    name = open_attribute(history, "name") or "?"
    status = open_attribute(history, "status")
    console.print(
        f"Station id={_color_id(station_id)}  subtype={subtype}  "
        f"marker [bold]{marker}[/bold]  name [bold]{name}[/bold]  "
        f"status {_color_status(status)}"
    )


def _compile_text_pattern(pattern: str):
    """Compile a user-supplied filter string into a regex matcher.

    Three input styles, detected in this order:

    1. Contains glob meta (``*`` / ``?``) but no other regex specials
       → translated via :func:`fnmatch.translate` so users can type
       ``*veður*`` without escaping anything.
    2. Compiles as a regex → used directly. Lets advanced operators
       write things like ``veður|vega`` or ``\\bIMO\\b``.
    3. Compile failure → escaped to a literal substring search.

    Always case-insensitive (TOS names mix Icelandic + English; ergo
    operators rarely care about case). Returns a compiled
    ``re.Pattern`` whose ``.search`` is the match predicate.
    """
    import fnmatch
    import re

    # Glob style — `*` and `?` are unambiguously globs when no other
    # regex metacharacters appear.
    has_glob = "*" in pattern or "?" in pattern
    other_regex_meta = set(".+|()[]{}^$\\") & set(pattern)
    if has_glob and not other_regex_meta:
        # fnmatch.translate emits a regex anchored with `\Z`; strip
        # the anchors so .search() can match anywhere.
        regex = fnmatch.translate(pattern)
        # Translated form looks like "(?s:.*veður.*)\\Z" — drop the
        # trailing anchor so we get substring semantics consistent
        # with the plain-text path.
        regex = regex.replace(r"\Z", "").replace(r"\\Z", "")
        return re.compile(regex, re.IGNORECASE)

    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


def _add_archive_arguments(parser) -> None:
    """Attach the cold-archive opt-in flags to a subparser.

    Shared between ``tos station triage`` and ``tos station verify``
    so both surface the verify-from-rinex audit identically. Off by
    default since the cold-archive mount isn't always available
    (offline / laptop workflows). When ``--with-archive`` is set,
    archive-root resolution falls back through:
      ``--archive-root`` flag → ``$TOSTOOLS_ARCHIVE_ROOT`` →
      ``receivers.cfg`` [archive_paths] → mount probe → error.
    """
    parser.add_argument(
        "--with-archive",
        action="store_true",
        help=(
            "Also run `tos audit verify-from-rinex` against the cold "
            "RINEX archive. Surfaces brand transitions, data gaps, and "
            "TOS-vs-archive join disagreements. Off by default — archive "
            "access isn't always available."
        ),
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "Override the cold-archive root path. Defaults to the "
            "$TOSTOOLS_ARCHIVE_ROOT env var, then the [archive_paths] "
            "section of receivers.cfg, then a mount probe."
        ),
    )
    parser.add_argument(
        "--archive-min-gap-days",
        type=float,
        default=30.0,
        metavar="DAYS",
        help=(
            "Minimum gap duration to flag (default: 30). Below ~7 the "
            "report fills with date-rounding noise."
        ),
    )


def _add_coverage_arguments(parser) -> None:
    """Attach the visit-coverage opt-in flags to a subparser.

    Shared between ``tos station triage`` / ``tos station verify`` /
    ``tos fleet`` so the audit surfaces identically across all three.
    Off by default — pre-vitjun-era stations have huge gaps and the
    first run would overwhelm operators before they've used
    ``add-visit`` enough to establish a baseline.
    """
    parser.add_argument(
        "--with-coverage",
        action="store_true",
        help=(
            "Also run `tos audit visit-coverage` — flag equipment-change "
            "events with no vitjun within ±N days. Off by default. "
            "Phase D of the vitjanir CLI expansion; expect a noisy first "
            "run on pre-vitjun-era stations."
        ),
    )
    parser.add_argument(
        "--coverage-since",
        default=None,
        metavar="DATE",
        help=(
            "Earliest event date the visit-coverage audit considers "
            "(YYYY-MM-DD). Defaults to today minus 2 years."
        ),
    )
    parser.add_argument(
        "--coverage-window-days",
        dest="coverage_window_days",
        type=int,
        default=7,
        metavar="DAYS",
        help=(
            "±N-day window around each event for vitjun coverage "
            "(default: 7). Wider = fewer false positives, more silent "
            "drift."
        ),
    )


def _add_contact_filter_arguments(parser) -> None:
    """Attach the standard ``--name`` / ``--email`` filter flags to a
    subparser.

    Shared between ``tos contact list`` and ``tos contact show`` so
    pattern semantics stay identical: plain text = case-insensitive
    substring, ``*`` / ``?`` = glob, full regex also accepted. Rows
    missing the filtered field are excluded when that filter is set.
    """
    parser.add_argument(
        "--name",
        default=None,
        metavar="PATTERN",
        help=(
            "Filter by name / organization. Plain text = case-"
            "insensitive substring (`veður` matches `Veðurstofa "
            "Íslands`). Glob characters `*` and `?` are honoured "
            "(`*stof*` matches the same). Full regex syntax is "
            "accepted (`veður|vega`, `\\bIMO\\b`)."
        ),
    )
    parser.add_argument(
        "--email",
        default=None,
        metavar="PATTERN",
        help=(
            "Filter by email. Same matching rules as --name: "
            "substring / glob / regex, case-insensitive. Rows with "
            "no email value are excluded when this filter is set."
        ),
    )


def _filter_contacts(
    contacts: List[Dict[str, Any]],
    *,
    name: Optional[str] = None,
    email: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Apply --name / --email filters to a contact row list.

    Filters are AND'd. ``name`` matches against ``name`` or
    ``organization`` (whichever has content). ``email`` matches
    against the ``email`` field. Rows missing the relevant field are
    excluded when that filter is set — "no value" can't match a
    pattern. Order preserved.
    """
    name_re = _compile_text_pattern(name) if name else None
    email_re = _compile_text_pattern(email) if email else None
    out: List[Dict[str, Any]] = []
    for c in contacts:
        if name_re is not None:
            candidate = c.get("name") or c.get("organization") or ""
            if not name_re.search(str(candidate)):
                continue
        if email_re is not None:
            value = c.get("email") or ""
            if not email_re.search(str(value)):
                continue
        out.append(c)
    return out


def _contact_main(argv):
    """Handle ``tos contact <verb>`` subcommands.

    Contacts live in their own id namespace (``id_contact``), distinct
    from device / station entities (``id_entity``). The Contacts table
    in ``tos station show`` surfaces ``id_contact`` values, and this
    subcommand is the canonical drill-down for them.

    Verbs:

      ``show --id N``        Fetch one contact by id_contact. Wraps
                             ``GET /contact/{id_contact}/``.
      ``list --station S``   Mirror the Contacts section from
                             ``tos station show`` (current contacts
                             for one station).

    Exit codes: 0 on success, 1 on lookup miss, 2 on usage error.
    """
    import json as _json

    from .api.tos_client import TOSClient

    p = argparse.ArgumentParser(
        prog="tos contact",
        description=(
            "Inspect TOS contact records. Contacts are entities in "
            "their own right (id_contact namespace), distinct from "
            "stations / devices (id_entity namespace) — the cyan "
            "`id` column in `tos station show`'s Contacts table is "
            "the value to pass here.\n\n"
            "Verbs:\n"
            "  show --id N         One contact record (owner / "
            "operator / point-of-contact).\n"
            "  list --station S    Contacts currently mapped to "
            "station S — same data as the embedded Contacts section "
            "in `tos station show`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", required=True)

    p_show = sub.add_parser(
        "show",
        help=(
            "Display one contact in detail. Look up by --id, or by "
            "--name / --email filters (must resolve to a unique match)."
        ),
        description=(
            "Detail view for a single contact. One of --id, --name, "
            "or --email is required. With filters:\n\n"
            "  * 1 match  → full detail, same as --id\n"
            "  * many     → compact list of matches + 'use --id N' hint\n"
            "  * 0        → exit 1\n\n"
            "Filter pattern semantics match `tos contact list`: "
            "plain text = case-insensitive substring; `*` / `?` are "
            "glob wildcards; full regex syntax is also accepted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_show.add_argument(
        "--id",
        dest="id_contact",
        type=int,
        default=None,
        help=(
            "Contact's id_contact (cyan column in `tos station show`). "
            "Mutually exclusive with --name / --email — id is the "
            "exact-match path."
        ),
    )
    _add_contact_filter_arguments(p_show)
    p_show.add_argument(
        "--json", action="store_true", help="Emit raw JSON instead of pretty."
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", type=int, default=443)

    p_list = sub.add_parser(
        "list",
        help="List contacts. With --station: contacts for that station only.",
        description=(
            "List contact records. Two modes:\n\n"
            "  (default)        every contact in TOS — compact "
            "id / name / phone / email table.\n"
            "  --station S      contacts mapped to station S only "
            "(same data as the embedded Contacts section in "
            "`tos station show`).\n\n"
            "Both modes accept --name / --email filters and --json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_list.add_argument(
        "--station",
        default=None,
        help=(
            "Optional station marker (e.g. HEDI) or display name. "
            "When omitted, every contact in TOS is listed."
        ),
    )
    _add_contact_filter_arguments(p_list)
    p_list.add_argument(
        "--json", action="store_true", help="Emit raw JSON instead of pretty."
    )
    p_list.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_list.add_argument("--port", type=int, default=443)

    # ---- Write verbs (dry-run default; mirror tos visit add) -----------
    p_patch = sub.add_parser(
        "patch-relationship",
        help="Correct a contact↔station relationship's period or role.",
        description=(
            "Edit the time_from / time_to / role of one contact↔station "
            "relationship row (id_contact_entity_relationship). Primary "
            "use: backdate a time_from that is a TOS-migration artifact "
            "(the relationship row was created when the contact was "
            "loaded into the new TOS, not when ownership actually "
            "started).\n\n"
            "Dry-run by default — the payload is logged but not sent. "
            "--no-dry-run commits (needs TOS credentials).\n\n"
            "The id_rel is the 'id' from the raw relationship row — get "
            "it from `tos contact list --station S --json` "
            "(id_contact_entity_relationship field)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_patch.add_argument(
        "id_rel",
        type=int,
        help="The relationship row id (id_contact_entity_relationship).",
    )
    p_patch.add_argument(
        "--time-from",
        dest="time_from",
        default=None,
        help="New time_from (YYYY-MM-DD). The migration-artifact fix.",
    )
    p_patch.add_argument(
        "--time-to",
        dest="time_to",
        default=None,
        help="New time_to (YYYY-MM-DD), or empty to leave open.",
    )
    p_patch.add_argument(
        "--role",
        default=None,
        help="New role string (owner / operator / ...).",
    )
    p_patch.add_argument(
        "--no-dry-run",
        dest="no_dry_run",
        action="store_true",
        help="Commit the write. Default: dry-run (payload logged only).",
    )
    p_patch.add_argument("--json", action="store_true", help="Structured output.")
    p_patch.add_argument("--server", default="vi-api.vedur.is")
    p_patch.add_argument("--port", type=int, default=443)

    p_assign = sub.add_parser(
        "assign",
        help="Assign a contact to a station (open a new relationship).",
        description=(
            "Create a new contact↔station relationship (POST "
            "/contact_joins). Dry-run by default; --no-dry-run commits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_assign.add_argument(
        "--station", required=True, help="Station marker (e.g. HEDI) or name."
    )
    p_assign.add_argument(
        "--contact",
        dest="id_contact",
        type=int,
        required=True,
        help="The contact entity id (from `tos contact list`).",
    )
    p_assign.add_argument(
        "--role",
        required=True,
        help="Role string (owner / operator / ...).",
    )
    p_assign.add_argument(
        "--from",
        dest="time_from",
        required=True,
        help="Relationship start (YYYY-MM-DD).",
    )
    p_assign.add_argument(
        "--no-dry-run",
        dest="no_dry_run",
        action="store_true",
        help="Commit the write. Default: dry-run.",
    )
    p_assign.add_argument("--json", action="store_true", help="Structured output.")
    p_assign.add_argument("--server", default="vi-api.vedur.is")
    p_assign.add_argument("--port", type=int, default=443)

    p_remove = sub.add_parser(
        "remove",
        help="Delete a contact↔station relationship (destructive).",
        description=(
            "Permanently delete one relationship row (DELETE "
            "/admin_contact_entity_relationship_row/{id}). Erases "
            "history — to END a valid relationship prefer "
            "`patch-relationship <id> --time-to <date>`. Dry-run by "
            "default; --no-dry-run commits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_remove.add_argument(
        "id_rel",
        type=int,
        help="The relationship row id (id_contact_entity_relationship).",
    )
    p_remove.add_argument(
        "--no-dry-run",
        dest="no_dry_run",
        action="store_true",
        help="Commit the delete. Default: dry-run.",
    )
    p_remove.add_argument("--json", action="store_true", help="Structured output.")
    p_remove.add_argument("--server", default="vi-api.vedur.is")
    p_remove.add_argument("--port", type=int, default=443)

    def _add_contact_entity_field_args(parser) -> None:
        """Shared writable-field flags for `create` / `patch-entity`."""
        parser.add_argument("--organization", default=None)
        parser.add_argument("--job-title", dest="job_title", default=None)
        parser.add_argument("--phone", dest="phone_primary", default=None)
        parser.add_argument("--phone2", dest="phone_secondary", default=None)
        parser.add_argument("--phone3", dest="phone_tertiary", default=None)
        parser.add_argument("--email", default=None)
        parser.add_argument("--address", default=None)
        parser.add_argument("--comment", default=None)
        parser.add_argument(
            "--start-date", dest="start_date", default=None, help="YYYY-MM-DD"
        )
        parser.add_argument(
            "--end-date", dest="end_date", default=None, help="YYYY-MM-DD (deactivate)"
        )
        parser.add_argument(
            "--ssid", default=None, help="Kennitala / org registration number."
        )

    p_create = sub.add_parser(
        "create",
        help="Create a new contact entity (person / organisation).",
        description=(
            "Create a new contact in TOS (POST /contacts). Returns the "
            "new id_contact, which you then map to a station with "
            "`tos contact assign`. Dry-run by default; --no-dry-run "
            "commits.\n\n"
            "NOTE: there is no contact-delete endpoint — a created "
            "contact cannot be removed, only deactivated via "
            "--end-date. The POST body is inferred from the GET entity "
            "shape; verify the first real creation with `tos contact "
            "show --id <new_id>`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_create.add_argument("--name", required=True, help="Contact name (required).")
    _add_contact_entity_field_args(p_create)
    p_create.add_argument(
        "--no-dry-run",
        dest="no_dry_run",
        action="store_true",
        help="Commit the create. Default: dry-run.",
    )
    p_create.add_argument("--json", action="store_true", help="Structured output.")
    p_create.add_argument("--server", default="vi-api.vedur.is")
    p_create.add_argument("--port", type=int, default=443)

    p_patch_entity = sub.add_parser(
        "patch-entity",
        help="Edit a contact entity's details (FLEET-GLOBAL — affects all stations).",
        description=(
            "Edit a contact entity in place (PUT /contact/{id}/). "
            "GET-merge-PUT: unchanged fields are preserved.\n\n"
            "⚠ FLEET-GLOBAL: one contact serves many stations, so a "
            "phone/address/name change propagates everywhere it's "
            "mapped. This is NOT a per-station correction (for that, "
            "use patch-relationship). Dry-run by default; --no-dry-run "
            "commits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_patch_entity.add_argument("id_contact", type=int, help="The contact entity id.")
    p_patch_entity.add_argument("--name", default=None, help="New name.")
    _add_contact_entity_field_args(p_patch_entity)
    p_patch_entity.add_argument(
        "--no-dry-run",
        dest="no_dry_run",
        action="store_true",
        help="Commit the edit. Default: dry-run.",
    )
    p_patch_entity.add_argument(
        "--json", action="store_true", help="Structured output."
    )
    p_patch_entity.add_argument("--server", default="vi-api.vedur.is")
    p_patch_entity.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    if args.verb == "show":
        # Three input styles: --id (exact), --name / --email (filtered).
        # Reject the empty case explicitly so the operator sees a
        # clearer message than argparse's generic "required" error.
        if args.id_contact is None and args.name is None and args.email is None:
            print(
                "tos contact show: one of --id, --name, --email is required",
                file=sys.stderr,
            )
            return 2

        # --id path: single exact lookup via /contact/{id}/. Filters
        # are ignored when --id is given (id is the unambiguous key).
        if args.id_contact is not None:
            contact = client.get_contact(args.id_contact)
            if not contact:
                print(
                    f"No contact found for id_contact={args.id_contact}",
                    file=sys.stderr,
                )
                return 1
            if args.json:
                print(_json.dumps(contact, ensure_ascii=False, indent=2))
                return 0
            _render_contact_record(contact)
            return 0

        # Filter path: pull the full fleet list, apply name/email.
        # Resolve by count:
        #   1 match  → render full detail (same as --id)
        #   many     → render compact list + hint to drill via --id
        #   0        → exit 1 with the clearest message we can manage
        all_contacts = client.list_all_contacts() or []
        matches = _filter_contacts(all_contacts, name=args.name, email=args.email)
        if not matches:
            criteria = []
            if args.name:
                criteria.append(f"--name {args.name!r}")
            if args.email:
                criteria.append(f"--email {args.email!r}")
            print(
                f"No contact matches: {' '.join(criteria)}",
                file=sys.stderr,
            )
            return 1
        if len(matches) == 1:
            contact = matches[0]
            if args.json:
                print(_json.dumps(contact, ensure_ascii=False, indent=2))
                return 0
            _render_contact_record(contact)
            return 0

        # Multiple matches — surface the candidate list so the
        # operator can pick by id. Same compact renderer as `list`.
        if args.json:
            print(_json.dumps({"contacts": matches}, ensure_ascii=False, indent=2))
            return 0
        from rich.console import Console

        console = Console()
        _render_all_contacts_table(console, matches)
        first_id = matches[0].get("id") or matches[0].get("id_contact")
        console.print()
        console.print(
            f"[yellow]{len(matches)} matches — narrow the filter or pick "
            f"one with[/yellow]:  tos contact show --id {first_id}"
        )
        return 0

    if args.verb == "list":
        # Two modes: --station S (per-station relationships) vs no
        # filter (every contact in TOS). Different endpoints, different
        # row shapes — the renderers below diverge accordingly.
        # --name / --email are AND'd onto either mode.
        if args.station is None:
            contacts = client.list_all_contacts() or []
            contacts = _filter_contacts(contacts, name=args.name, email=args.email)
            if args.json:
                print(_json.dumps({"contacts": contacts}, ensure_ascii=False, indent=2))
                return 0
            from rich.console import Console

            console = Console()
            _render_all_contacts_table(console, contacts)
            return 0

        station_id = _resolve_parent_id(client, station_marker=args.station)
        if station_id is None:
            print(
                f"No station found for marker {args.station!r}",
                file=sys.stderr,
            )
            return 1
        contacts = client.get_contacts(station_id) or []
        contacts = _filter_contacts(contacts, name=args.name, email=args.email)
        if args.json:
            payload = {
                "station": args.station,
                "id_entity": station_id,
                "contacts": contacts,
            }
            print(_json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        from rich.console import Console

        console = Console()
        _render_station_contacts(console, contacts)
        return 0

    if args.verb in (
        "patch-relationship",
        "assign",
        "remove",
        "create",
        "patch-entity",
    ):
        return _contact_write_main(args, base_url)

    return 2


def _contact_write_main(args, base_url: str) -> int:
    """Handle the contact write verbs (relationship + entity).

    Split out from :func:`_contact_main` so the read path stays on the
    unauthenticated client. All verbs are dry-run by default
    (``--no-dry-run`` commits), mirroring ``tos visit add`` /
    ``tos device add``.
    """
    import json as _json

    from .api.tos_writer import TOSWriter

    dry_run = not args.no_dry_run
    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    # ---- Contact-entity verbs (create / patch-entity) ------------------
    _ENTITY_FIELDS = (
        "organization",
        "job_title",
        "phone_primary",
        "phone_secondary",
        "phone_tertiary",
        "email",
        "address",
        "comment",
        "start_date",
        "end_date",
        "ssid",
    )

    if args.verb == "create":
        fields = {
            f: getattr(args, f) for f in _ENTITY_FIELDS if getattr(args, f) is not None
        }
        try:
            result = writer.create_contact(name=args.name, **fields)
        except ValueError as exc:
            print(f"create failed: {exc}", file=sys.stderr)
            return 1
        new_id = result.get("id") if isinstance(result, dict) else None
        suffix = " (dry-run)" if dry_run else ""
        if args.json:
            print(
                _json.dumps(
                    {
                        "verb": "create",
                        "name": args.name,
                        "fields": fields,
                        "dry_run": dry_run,
                        "id_contact": new_id,
                        "result": str(result),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            id_str = new_id if new_id else "<would be assigned>"
            print(f"Created contact id_contact={id_str} name={args.name!r}{suffix}")
            if not dry_run and isinstance(new_id, int):
                print(
                    f"Next: tos contact assign --station S --contact {new_id} "
                    "--role owner --from DATE"
                )
        return 0

    if args.verb == "patch-entity":
        fields = {}
        if args.name is not None:
            fields["name"] = args.name
        for f in _ENTITY_FIELDS:
            v = getattr(args, f)
            if v is not None:
                fields[f] = v
        if not fields:
            print(
                "tos contact patch-entity: at least one field flag is required",
                file=sys.stderr,
            )
            return 2
        try:
            result = writer.patch_contact(args.id_contact, **fields)
        except ValueError as exc:
            print(f"patch-entity failed: {exc}", file=sys.stderr)
            return 1
        suffix = " (dry-run)" if dry_run else ""
        if args.json:
            print(
                _json.dumps(
                    {
                        "verb": "patch-entity",
                        "id_contact": args.id_contact,
                        "changes": fields,
                        "dry_run": dry_run,
                        "result": str(result),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            changed = ", ".join(f"{k}={v}" for k, v in fields.items())
            print(
                f"Patched contact {args.id_contact} (FLEET-GLOBAL): "
                f"{changed}{suffix}"
            )
        return 0

    if args.verb == "assign":
        # Resolve the station marker → id_entity (the relationship's
        # entity side). The contact id is supplied directly.
        from .api.tos_client import TOSClient

        client = TOSClient(base_url=base_url)
        id_entity = _resolve_parent_id(client, station_marker=args.station)
        if id_entity is None:
            print(f"No station found for marker {args.station!r}", file=sys.stderr)
            return 1
        try:
            result = writer.create_contact_relationship(
                args.id_contact,
                id_entity,
                args.role,
                args.time_from,
            )
        except ValueError as exc:
            print(f"assign failed: {exc}", file=sys.stderr)
            return 1
        suffix = " (dry-run)" if dry_run else ""
        if args.json:
            print(
                _json.dumps(
                    {
                        "verb": "assign",
                        "id_contact": args.id_contact,
                        "id_entity": id_entity,
                        "role": args.role,
                        "time_from": args.time_from,
                        "dry_run": dry_run,
                        "result": str(result),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                f"Assigned contact {args.id_contact} → station {args.station} "
                f"(id_entity={id_entity}) role={args.role!r} "
                f"from {args.time_from}{suffix}"
            )
        return 0

    if args.verb == "patch-relationship":
        if args.time_from is None and args.time_to is None and args.role is None:
            print(
                "tos contact patch-relationship: at least one of "
                "--time-from / --time-to / --role is required",
                file=sys.stderr,
            )
            return 2
        kwargs: Dict[str, Any] = {}
        if args.time_from is not None:
            kwargs["time_from"] = args.time_from
        if args.time_to is not None:
            kwargs["time_to"] = args.time_to
        if args.role is not None:
            kwargs["role"] = args.role
        try:
            result = writer.patch_contact_relationship(args.id_rel, **kwargs)
        except ValueError as exc:
            print(f"patch-relationship failed: {exc}", file=sys.stderr)
            return 1
        suffix = " (dry-run)" if dry_run else ""
        if args.json:
            print(
                _json.dumps(
                    {
                        "verb": "patch-relationship",
                        "id_rel": args.id_rel,
                        "changes": kwargs,
                        "dry_run": dry_run,
                        "result": str(result),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            changes = ", ".join(f"{k}={v}" for k, v in kwargs.items())
            print(f"Patched relationship {args.id_rel}: {changes}{suffix}")
        return 0

    if args.verb == "remove":
        try:
            result = writer.delete_contact_relationship(args.id_rel)
        except ValueError as exc:
            print(f"remove failed: {exc}", file=sys.stderr)
            return 1
        suffix = " (dry-run)" if dry_run else ""
        if args.json:
            print(
                _json.dumps(
                    {
                        "verb": "remove",
                        "id_rel": args.id_rel,
                        "dry_run": dry_run,
                        "result": str(result),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"Removed relationship {args.id_rel}{suffix}")
        return 0

    return 2


def _render_all_contacts_table(
    console,
    contacts: List[Dict[str, Any]],
) -> None:
    """Render the full TOS contact list (the `list` verb sans
    ``--station``).

    Different shape from :func:`_render_station_contacts`: rows here
    come from ``/contact/`` directly (the contact entity, not a
    relationship), so there is no role / per-station period to show.
    Compact columns — id, name/organization, phone, email, start_date,
    end_date — sized for fleet inspection.

    Rows are sorted by id_contact ascending so the listing is
    deterministic; operators usually pipe to grep / less anyway.
    """
    from rich.table import Table

    title = f"All TOS contacts — {len(contacts)} record(s)"
    if not contacts:
        console.print(title)
        console.print("  (no contacts returned)")
        return

    rows = sorted(contacts, key=lambda c: c.get("id") or c.get("id_contact") or 0)

    table = Table(title=title)
    table.add_column("id", justify="right")
    table.add_column("name / organization")
    table.add_column("phone")
    table.add_column("email")
    table.add_column("start_date")
    table.add_column("end_date")
    for c in rows:
        id_contact = c.get("id") or c.get("id_contact")
        name = c.get("name") or c.get("organization") or "?"
        phone = c.get("phone_primary") or "—"
        email = c.get("email") or "—"
        start = (c.get("start_date") or "")[:10] or "?"
        end_raw = c.get("end_date")
        end = end_raw[:10] if end_raw else "—"
        table.add_row(
            _color_id(id_contact),
            name,
            phone,
            email,
            _color_date(start) if start != "?" else "?",
            _color_date(end) if end != "—" else "—",
        )
    console.print(table)


def _render_contact_record(contact: Dict[str, Any]) -> None:
    """Pretty-print one contact record (the `show` verb's output).

    The ``/contact/{id}/`` endpoint returns the *contact entity* itself
    — name / address / phones / email — without per-station role
    (role lives on the ``entity_contacts/{id_entity}/`` relationship
    rows). Rendered as one (field, value) pair per row so long
    values (address, comment) read clearly.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    # The endpoint exposes the contact's id as `id` (not `id_contact`).
    id_contact = contact.get("id") or contact.get("id_contact")
    name = contact.get("name") or contact.get("organization") or "?"
    console.print(f"Contact id={_color_id(id_contact)}  name [bold]{name}[/bold]")

    # Field list matches the actual `/contact/{id}/` payload schema.
    # Role / role_is are intentionally omitted — they're per-station
    # relationship data, exposed by `tos contact list --station S`.
    fields = [
        ("organization", contact.get("organization")),
        ("name", contact.get("name")),
        ("job_title", contact.get("job_title")),
        ("phone_primary", contact.get("phone_primary")),
        ("phone_secondary", contact.get("phone_secondary")),
        ("phone_tertiary", contact.get("phone_tertiary")),
        ("email", contact.get("email")),
        ("address", contact.get("address")),
        ("comment", contact.get("comment")),
        ("start_date", contact.get("start_date")),
        ("end_date", contact.get("end_date")),
        ("ssid", contact.get("ssid")),
    ]
    table = Table(title="Contact attributes")
    table.add_column("field")
    table.add_column("value")
    for label, value in fields:
        if value in (None, ""):
            rendered = "—"
        else:
            rendered = f"[{_STATION_VALUE_COLOR}]{value}[/{_STATION_VALUE_COLOR}]"
        table.add_row(label, rendered)
    console.print(table)


def _visit_main(argv):
    """Handle ``tos visit <verb>`` subcommands.

    Vitjun (visit / maintenance) records live on an entity, alongside
    its attribute-value periods and entity-connection joins. The schema
    is generic on ``id_entity`` — both stations and devices can have
    them, though in current GPS data every vitjun is station-attached
    (device-attached vitjanir today are exclusively on meteorological
    sensors).

    Verbs:

      ``list --station S | --device <id> | --entity <id> [filters]``
                            Listing for one entity. Standard filter set
                            (--type, --reason, --since, --participants,
                            --open / --completed).
      ``show <id_maintenance>``
                            One vitjun's full detail, including the
                            ``maintenance_attribute_values`` rows.
      ``add --station S | --device <id> | --entity <id>
              --start DATE [--end DATE] [--type {on_site,remote}]
              [--participants EMAIL ...] [--reason CODE ...]
              [--work TEXT] [--comment TEXT] [--remaining TEXT]
              [--no-completed] [--no-dry-run]``
                            Create a new vitjun. Dry-run by default;
                            ``--no-dry-run`` commits.

    Exit codes: 0 success, 1 lookup miss / no records / writer error,
    2 usage error.
    """
    import json as _json

    from .api.tos_client import TOSClient
    from .api.tos_writer import TOSWriter

    p = argparse.ArgumentParser(
        prog="tos visit",
        description=(
            "Inspect TOS vitjun (visit / maintenance) records. Vitjanir "
            "are entity-attached temporal records (id_maintenance "
            "namespace) — every visit hangs off an id_entity (station "
            "or device).\n\n"
            "Verbs:\n"
            "  list --station S        Vitjanir attached to station S.\n"
            "  list --device <id>      Vitjanir attached to a device by id_entity.\n"
            "  list --entity <id>      Escape hatch — any entity by id_entity.\n"
            "  show <id_maintenance>   One vitjun's full detail.\n\n"
            "List accepts the standard visit-filter set: --type, "
            "--reason (repeatable), --since DATE, --participants SUBSTR, "
            "--open / --completed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", required=True)

    p_list = sub.add_parser(
        "list",
        help="List vitjanir attached to one entity (station / device).",
        description=(
            "List vitjun records for one entity. Exactly one of "
            "--station, --device, --entity is required.\n\n"
            "Default sort: start_time descending (most recent first). "
            "Use --json for machine-readable output, or pass the "
            "standard visit-filter set to narrow the listing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target_group = p_list.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--station",
        default=None,
        help="Station marker (e.g. HEDI) or display name. Primary affordance.",
    )
    target_group.add_argument(
        "--device",
        dest="device_id",
        type=int,
        default=None,
        help=(
            "Device id_entity. Useful for meteorological sensors and "
            "(eventually) device-lifecycle vitjanir on GPS receivers."
        ),
    )
    target_group.add_argument(
        "--entity",
        dest="entity_id",
        type=int,
        default=None,
        help=(
            "Escape hatch — any id_entity directly. Bypasses station/"
            "device naming entirely."
        ),
    )
    add_visit_filter_arguments(p_list)
    p_list.add_argument(
        "--json", action="store_true", help="Emit raw JSON instead of pretty."
    )
    p_list.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_list.add_argument("--port", type=int, default=443)

    p_show = sub.add_parser(
        "show",
        help="Show one vitjun's full detail by id_maintenance.",
        description=(
            "Display all fields for one vitjun, including the "
            "maintenance_attribute_values rows (with each row's "
            "id_maintenance_attribute_value — needed by the writer for "
            "updates). Use the id from `tos visit list`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_show.add_argument(
        "id_maintenance",
        type=int,
        help="The vitjun's id_maintenance (column 'id' in `tos visit list`).",
    )
    p_show.add_argument(
        "--json", action="store_true", help="Emit raw JSON instead of pretty."
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", type=int, default=443)

    p_add = sub.add_parser(
        "add",
        help="Create a new vitjun on an entity (station / device).",
        description=(
            "Create a new vitjun (visit / maintenance record). Three-call "
            "flow: POST /maintenances/id_entity/<id> → GET to discover "
            "auto-seeded attribute-value rows → PUT to fill them in.\n\n"
            "Dry-run by default — payloads are logged but not sent. Pass "
            "--no-dry-run to commit (requires TOS credentials: "
            "TOS_USERNAME/TOS_PASSWORD env vars, or the [tos] section in "
            "~/.config/database.cfg, or an interactive prompt).\n\n"
            "Target: --station S (resolves marker → id_entity), "
            "--device <id> (direct id_entity, semantically a device), "
            "--entity <id> (escape hatch — any id_entity)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_target_group = p_add.add_mutually_exclusive_group(required=True)
    add_target_group.add_argument(
        "--station",
        default=None,
        help="Station marker (e.g. HEDI) or display name. Primary affordance.",
    )
    add_target_group.add_argument(
        "--device",
        dest="device_id",
        type=int,
        default=None,
        help="Device id_entity. Device-attached vitjun (lifecycle tracker use).",
    )
    add_target_group.add_argument(
        "--entity",
        dest="entity_id",
        type=int,
        default=None,
        help="Escape hatch — any id_entity directly.",
    )
    p_add.add_argument(
        "--start",
        required=True,
        help=(
            "Visit start. ISO datetime or YYYY-MM-DD (promoted to "
            "midnight). Example: --start 2026-05-30."
        ),
    )
    p_add.add_argument(
        "--end",
        default=None,
        help=(
            "Visit end. Defaults to --start (instantaneous visit). "
            "Same date / datetime format as --start."
        ),
    )
    p_add.add_argument(
        "--type",
        dest="visit_type",
        choices=["on_site", "remote"],
        default="on_site",
        help="on_site (Staðarvitjun, default) or remote (Fjarvitjun).",
    )
    p_add.add_argument(
        "--participants",
        action="append",
        dest="participants",
        default=None,
        metavar="EMAIL",
        help=(
            "Participant email (e.g. bgo@vedur.is). Repeatable; joined "
            "comma-separated for TOS. TOS resolves to "
            "participants_names on read."
        ),
    )
    p_add.add_argument(
        "--reason",
        action="append",
        dest="reasons",
        choices=sorted(MAINTENANCE_REASON_CODES),
        help=(
            "Reason code (change / repairs / inspection / improvements "
            "/ other). Repeatable; multiple reasons can be true on one "
            "vitjun."
        ),
    )
    p_add.add_argument(
        "--work",
        default=None,
        help='Free-text "Framkvæmt" / "Vinna" description.',
    )
    p_add.add_argument(
        "--comment",
        default=None,
        help='Free-text "Athugasemdir".',
    )
    p_add.add_argument(
        "--remaining",
        default=None,
        help='Free-text "Útistandandi" outstanding work.',
    )
    p_add.add_argument(
        "--no-completed",
        dest="no_completed",
        action="store_true",
        help=(
            "Mark the visit as open (completed=False). Use for long-"
            "running repairs / ongoing investigations. Default: closed."
        ),
    )
    p_add.add_argument(
        "--no-dry-run",
        dest="no_dry_run",
        action="store_true",
        help=(
            "Actually commit the writes. Without this flag, payloads "
            "are logged only (dry-run, default — matches `tos device add`)."
        ),
    )
    p_add.add_argument(
        "--json", action="store_true", help="Emit structured JSON summary."
    )
    p_add.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_add.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    if args.verb == "list":
        # Resolve the target entity. Three input styles:
        #   --station S      → marker / display-name resolution
        #   --device <id>    → direct id_entity (no resolution)
        #   --entity <id>    → direct id_entity (no resolution)
        # Device vs entity is semantic only — both feed the same
        # endpoint. The split exists so `--device` reads naturally on
        # the operator's eye for the eventual lifecycle-tracker use case.
        if args.station is not None:
            id_entity = _resolve_parent_id(client, station_marker=args.station)
            if id_entity is None:
                print(
                    f"No station found for marker {args.station!r}",
                    file=sys.stderr,
                )
                return 1
            target_label = args.station
            target_kind = "station"
        elif args.device_id is not None:
            id_entity = args.device_id
            target_label = f"device {id_entity}"
            target_kind = "device"
        else:
            id_entity = args.entity_id
            target_label = f"entity {id_entity}"
            target_kind = "entity"

        rows = client.list_maintenance_visits(id_entity)
        rows = apply_visit_filters(rows, args)
        # Most recent first — operators usually want the latest visit
        # at the top, drilling backwards in time.
        rows = sorted(rows, key=lambda r: r.get("start_time") or "", reverse=True)

        if args.json:
            payload = {
                "target_kind": target_kind,
                "target_label": target_label,
                "id_entity": id_entity,
                "visits": rows,
            }
            print(_json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        from rich.console import Console

        console = Console()
        _render_visits_table(
            console,
            rows,
            title=f"Vitjanir — {len(rows)} record(s) for {target_label}",
        )
        if rows:
            console.print()
            sample_id = rows[0].get("id")
            console.print(f"[dim]Drill: tos visit show {sample_id}[/dim]")
        return 0

    if args.verb == "show":
        visit = client.get_maintenance_visit(args.id_maintenance)
        if not visit:
            print(
                f"No vitjun found for id_maintenance={args.id_maintenance}",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(_json.dumps(visit, ensure_ascii=False, indent=2))
            return 0
        from rich.console import Console

        console = Console()
        _render_visit_detail(console, visit)
        return 0

    if args.verb == "add":
        # Resolve target via the same three-way affordance as `list`.
        if args.station is not None:
            id_entity = _resolve_parent_id(client, station_marker=args.station)
            if id_entity is None:
                print(
                    f"No station found for marker {args.station!r}",
                    file=sys.stderr,
                )
                return 1
            target_label = f"station {args.station}"
        elif args.device_id is not None:
            id_entity = args.device_id
            target_label = f"device {id_entity}"
        else:
            id_entity = args.entity_id
            target_label = f"entity {id_entity}"

        # Writer setup. Dry-run by default — matches `tos device add`.
        dry_run = not args.no_dry_run
        writer = TOSWriter(base_url=base_url, dry_run=dry_run)

        # Participants: TOS expects comma-joined emails. CLI accepts
        # repeatable --participants for ergonomics.
        participants_str = ",".join(args.participants) if args.participants else ""

        # Dry-run preview before invoking the writer — surfaces what
        # would be POSTed even when the writer's own log is muted.
        if dry_run and not args.json:
            print(
                f"DRY RUN: would add vitjun on {target_label} "
                f"(id_entity={id_entity})"
            )
            print(f"         start_time      = {args.start}")
            print(f"         end_time        = {args.end or args.start}")
            print(f"         maintenance_type= {args.visit_type}")
            print(
                f"         reasons         = "
                f"{','.join(args.reasons) if args.reasons else '(none)'}"
            )
            print(f"         participants    = {participants_str or '(none)'}")
            print(f"         work            = {args.work or '(none)'}")
            print(f"         comment         = {args.comment or '(none)'}")
            print(f"         remaining       = {args.remaining or '(none)'}")
            print(f"         completed       = {not args.no_completed}")
            print()

        # Writer validates reason codes + maintenance_type + dates;
        # surface ValueError as exit 1 with the writer's message.
        try:
            result = writer.add_maintenance_visit(
                id_entity,
                start_time=args.start,
                end_time=args.end,
                maintenance_type=args.visit_type,
                participants=participants_str,
                reasons=args.reasons,
                work=args.work,
                comment=args.comment,
                remaining=args.remaining,
                completed=not args.no_completed,
            )
        except ValueError as exc:
            print(f"add_maintenance_visit failed: {exc}", file=sys.stderr)
            return 1

        new_id = result.get("id_maintenance")
        if args.json:
            payload = {
                "id_entity": id_entity,
                "target": target_label,
                "dry_run": dry_run,
                "id_maintenance": new_id,
                "params": {
                    "start_time": args.start,
                    "end_time": args.end or args.start,
                    "maintenance_type": args.visit_type,
                    "participants": participants_str,
                    "reasons": args.reasons or [],
                    "work": args.work,
                    "comment": args.comment,
                    "remaining": args.remaining,
                    "completed": not args.no_completed,
                },
                "result": result,
            }
            print(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0

        suffix = " (dry-run)" if dry_run else ""
        id_str = new_id if new_id else "<would be assigned>"
        print(f"Created vitjun id_maintenance={id_str} on {target_label}{suffix}")
        if not dry_run and isinstance(new_id, int):
            print(f"Drill: tos visit show {new_id}")
        return 0

    return 2


def _render_visits_table(
    console,
    visits: List[Dict[str, Any]],
    *,
    title: str,
    source_label_col: Optional[str] = None,
) -> None:
    """Render a vitjun list table.

    Columns: id, start_time, type, reasons, participants, completed,
    work-summary. ``end_time`` is suppressed by default — most visits
    are instantaneous (``end_time == start_time``) and the column adds
    visual noise. Drill via ``tos visit show <id>`` for full detail.

    When ``source_label_col`` is given, a second column with that
    header is inserted between ``id`` and ``start``; each row's value
    comes from the per-row ``__source_label`` key (added by the
    aggregator in ``_station_show_main``). Used to show "station HEDI"
    vs "device 4830 (gnss_receiver)" attribution in the station-show
    aggregated view.
    """
    from rich.table import Table

    if not visits:
        console.print(title)
        console.print("  (no vitjanir on file)")
        return

    table = Table(title=title)
    table.add_column("id", justify="right")
    if source_label_col is not None:
        table.add_column(source_label_col)
    table.add_column("start")
    table.add_column("type")
    table.add_column("reasons")
    table.add_column("participants")
    table.add_column("done")
    table.add_column("work (first line)")

    for v in visits:
        vid = v.get("id")
        start = (v.get("start_time") or "")[:10]
        vtype = v.get("maintenance_type") or "—"
        type_color = "green" if vtype == "on_site" else "blue"
        vtype_cell = f"[{type_color}]{vtype}[/{type_color}]"
        reasons = (v.get("reason") or "").replace(",", ", ") or "—"
        participants = v.get("participants_names") or v.get("participants") or "—"
        completed = v.get("completed")
        if completed is True:
            done_cell = "[green]yes[/green]"
        elif completed is False:
            done_cell = "[yellow]open[/yellow]"
        else:
            done_cell = "—"
        work = (v.get("work") or "").splitlines()
        work_first = (work[0] if work else "") or "—"
        if len(work_first) > 60:
            work_first = work_first[:57] + "..."
        row_cells = [str(vid) if vid is not None else "?"]
        if source_label_col is not None:
            row_cells.append(str(v.get("__source_label") or "?"))
        row_cells.extend(
            [start, vtype_cell, reasons, str(participants), done_cell, work_first]
        )
        table.add_row(*row_cells)
    console.print(table)


def _trim_visits_for_show(
    visits: List[Dict[str, Any]],
    *,
    show_all: bool,
    max_closed: int = 3,
) -> "tuple[List[Dict[str, Any]], int]":
    """Trim a visit list for the per-entity 'Recent visits' section.

    Default view (``show_all=False``): every open visit + the
    ``max_closed`` most-recent closed visits. ``show_all=True``:
    every visit, no trim. Returns ``(visible_rows, hidden_count)``
    so the caller can hint at how many more visits exist.

    Sort within each group: start_time descending (most-recent first).
    The composed return list preserves "open first, then closed
    newest-first" — same operator-eye ordering as `tos visit list`.
    """
    by_recency = sorted(visits, key=lambda v: v.get("start_time") or "", reverse=True)
    if show_all:
        return by_recency, 0
    open_rows = [v for v in by_recency if not v.get("completed")]
    closed_rows = [v for v in by_recency if v.get("completed")]
    visible_closed = closed_rows[:max_closed]
    hidden = len(closed_rows) - len(visible_closed)
    return open_rows + visible_closed, hidden


def _render_visit_detail(console, visit: Dict[str, Any]) -> None:
    """Render one vitjun's full detail (the `show` verb output).

    Header (id / type / dates / completed / participants / reasons) +
    a free-text panel (work / comment / remaining) + the
    ``maintenance_attribute_values`` rows with each row's
    ``id_maintenance_attribute_value`` (needed by the writer for
    updates).

    The detail endpoint doesn't carry ``id_entity`` or a flat
    ``reason`` / ``work`` / ``comment`` / ``remaining`` field —
    everything except the header columns lives in
    ``maintenance_attribute_values``. We source from there.
    """
    from rich.table import Table

    vid = visit.get("id_maintenance") or visit.get("id")
    vtype = visit.get("maintenance_type") or "—"
    start = visit.get("start_time") or "—"
    end = visit.get("end_time") or "—"
    completed = visit.get("completed")
    completed_label = (
        "yes" if completed is True else "open" if completed is False else "—"
    )
    completed_color = (
        "green" if completed is True else "yellow" if completed is False else ""
    )

    # Build a code → (id_av, value) lookup from the attribute_value rows.
    # This is the only place ``work`` / ``comment`` / ``remaining`` /
    # ``reason_*`` actually live in the detail payload.
    av_rows: List[Dict[str, Any]] = visit.get("maintenance_attribute_values") or []
    by_code: Dict[str, Dict[str, Any]] = {
        (av.get("code") or ""): av for av in av_rows if av.get("code")
    }

    # Active reason codes — booleans on the attribute_value rows.
    active_reasons = [
        code[len("reason_") :]
        for code, av in by_code.items()
        if code.startswith("reason_") and str(av.get("value")).lower() == "true"
    ]
    reasons_display = (
        ", ".join(MAINTENANCE_REASON_DISPLAY.get(c, c) for c in active_reasons) or "—"
    )

    header = Table(title=f"Vitjun {vid}", show_header=False)
    header.add_column("field", style="bold")
    header.add_column("value")
    header.add_row("id_maintenance", str(vid) if vid is not None else "?")
    header.add_row(
        "maintenance_type",
        f"[{'green' if vtype == 'on_site' else 'blue'}]{vtype}[/]",
    )
    header.add_row("start_time", str(start))
    header.add_row("end_time", str(end))
    header.add_row(
        "completed",
        (
            f"[{completed_color}]{completed_label}[/]"
            if completed_color
            else completed_label
        ),
    )
    header.add_row(
        "participants",
        str(visit.get("participants_names") or visit.get("participants") or "—"),
    )
    header.add_row("reasons", reasons_display)
    console.print(header)

    # Free-text fields, sourced from attribute_value rows. Always render
    # so the operator sees which slots exist on a vitjun at a glance,
    # even when empty.
    text_table = Table(title="Notes", show_header=False)
    text_table.add_column("field", style="bold")
    text_table.add_column("text")
    for code in ("work", "comment", "remaining"):
        av = by_code.get(code) or {}
        val = av.get("value") or "—"
        text_table.add_row(code, str(val))
    console.print()
    console.print(text_table)

    # Raw attribute_value rows — same shape the writer needs for
    # updates. Always shown (low-cost; the writer-to-be will copy
    # id_maintenance_attribute_value values from here).
    if av_rows:
        av_table = Table(title=f"maintenance_attribute_values — {len(av_rows)} row(s)")
        av_table.add_column("id_av", justify="right")
        av_table.add_column("code")
        av_table.add_column("value")
        for av in av_rows:
            av_table.add_row(
                str(av.get("id_maintenance_attribute_value") or "?"),
                str(av.get("code") or "?"),
                str(av.get("value") or "—"),
            )
        console.print()
        console.print(av_table)


def _fleet_main(argv):
    """Handle ``tos fleet <verb>`` subcommands.

    Fleet-wide orchestrators that loop the single-station verbs across
    every GNSS station in ``stations.cfg``. Phase 4 of the
    [[station_triage_orchestrator]] sequence — see CLAUDE.md.

    Verbs:

      ``triage``  Generate per-station triage files across the fleet.
                  Clean stations are skipped by default (no findings,
                  no file). Files land under
                  ``data/triage/<station>/<station>_audit_<DATE>.txt``.

      ``status``  Run the verify oracle in bulk and emit a fleet
                  summary table. No disk writes. Exit code mirrors
                  ``tos station verify``: 0 clean fleet, 1 any
                  findings, 2 any audit failure.

    Both verbs share the same filter set:

      ``--include STN1 STN2``  whitelist station markers
      ``--exclude STN3``       blacklist station markers
      ``--limit N``            stop after N stations (test helper)
      ``--with-archive``       also run verify-from-rinex (slow at
                               fleet scale — 173 archive walks)

    A fleet run is sequential and takes 5-15 minutes on a warm cache.
    Progress is printed to stderr; per-station summaries go to stdout.
    """
    import argparse as _argparse
    import json as _json
    import sys as _sys

    p = _argparse.ArgumentParser(
        prog="tos fleet",
        description=(
            "Fleet-wide orchestrators for GPS/GNSS metadata QC.\n\n"
            "Loop the per-station audit verbs (`tos station verify` /\n"
            "`tos station triage`) across every GNSS station listed in\n"
            "`stations.cfg` (~173 stations on the IMO network) and\n"
            "aggregate the results into one report.\n\n"
            "WHEN TO USE\n"
            "  Daily health check       → `tos fleet status` (read-only)\n"
            "  Cron / CI gate           → `tos fleet status --json`\n"
            "  Bulk metadata cleanup    → `tos fleet triage` → edit →\n"
            "                             `tos audit apply`\n"
            "  Spot check a subset      → `--include STN1 STN2 …`\n"
            "  Archive cross-check      → `--with-archive` (slow at\n"
            "                             fleet scale; warn auto-emits)\n\n"
            "VERBS\n"
            "  status   Bulk verify oracle — runs every audit against\n"
            "           every station; emits a fleet dashboard table\n"
            "           sorted by findings desc. No disk writes. Exit\n"
            "           code:  0 = all clean, 1 = any findings,\n"
            "           2 = any audit raised. Same exit semantics as\n"
            "           single-station `tos station verify`, so cron /\n"
            "           CI can tell 'fleet needs work' from\n"
            "           'oracle broken'.\n\n"
            "  triage   Generate per-station ACTION-style triage files\n"
            "           under `data/triage/<STN>/<STN>_audit_<DATE>.txt`.\n"
            "           Clean stations are skipped by default — the\n"
            "           operator only sees files for stations needing\n"
            "           attention. The generated file is the input to\n"
            "           `tos audit apply` (dry-run first, then `--apply`).\n\n"
            "FLEET OPERATOR WORKFLOW\n"
            "  1. `tos fleet status`               # is anything broken?\n"
            "  2. `tos fleet triage --include X`   # produce triage files\n"
            "                                      # for stations of\n"
            "                                      # interest\n"
            "  3. `$EDITOR data/triage/x/*.txt`    # review + uncomment\n"
            "                                      # ACTION lines\n"
            "  4. `tos audit apply <file>`         # dry-run\n"
            "  5. `tos audit apply <file> --apply` # commit\n"
            "  6. `tos fleet status --include X`   # confirm clean\n"
            "  7. `git commit data/triage/x/*.txt` # provenance\n\n"
            "PERFORMANCE\n"
            "  Sequential by design. A narrow run (`--include HEDI SAVI`)\n"
            "  takes a few seconds. A full fleet sweep is 5-15 min on a\n"
            "  warm cache; `--with-archive` adds ~10s per station for\n"
            "  the cold-archive walk (30+ min for a full fleet sweep —\n"
            "  consider `--limit N` for first runs).\n\n"
            "  Progress is printed to stderr; the aggregate table /\n"
            "  JSON payload goes to stdout, so `tos fleet status --json\n"
            "  > today.json` works cleanly.\n\n"
            "OUTPUT TABLE COLUMNS  (text mode)\n"
            "  mark   ✓ clean / ✗ findings / ‽ audit raised\n"
            "  STN    station marker (4-letter)\n"
            "  id     id_entity in TOS\n"
            "  find   total findings (sum of all audits)\n"
            "  miss   missing required attributes\n"
            "  date   suspicious attribute dates (e.g. 2014-10-17\n"
            "         cleanup-artifact backdates)\n"
            "  rinex  archive cross-check findings (with --with-archive)\n"
            "  notes  first failure note if status=failed\n\n"
            "EXAMPLES\n"
            "  tos fleet status\n"
            "  tos fleet status --include HEDI SAVI\n"
            "  tos fleet status --show-clean --json > fleet.json\n"
            "  tos fleet status --no-suppressions   # surface what\n"
            "                                       # SUPPRESS files\n"
            "                                       # hide\n"
            "  tos fleet triage --include HEDI\n"
            "  tos fleet triage --exclude OLKE SVIN  # skip known-broken\n"
            "  tos fleet triage --limit 5           # smoke test\n"
            "  tos fleet status --with-archive --include HOFN\n\n"
            "SEE ALSO\n"
            "  tos station verify <STN>   single-station oracle\n"
            "  tos station triage <STN>   single-station triage file\n"
            "  tos station show <STN>     current-state inspection\n"
            "  tos audit apply <file>     consume a triage file"
        ),
        formatter_class=_argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", required=True)

    def _add_common_filters(sp):
        sp.add_argument(
            "--include",
            nargs="+",
            default=None,
            metavar="STN",
            help=(
                "Restrict the run to these 4-letter station markers "
                "(case-insensitive, space-separated, e.g. `--include "
                "HEDI SAVI`). Applied BEFORE marker-to-id resolution, "
                "so a 2-station run makes ~2 HTTP calls, not 173. "
                "Pairs well with `--with-archive` to keep the slow "
                "path bounded."
            ),
        )
        sp.add_argument(
            "--exclude",
            nargs="+",
            default=None,
            metavar="STN",
            help=(
                "Skip these station markers (case-insensitive, space-"
                "separated). Useful for stations with known-broken "
                "audit state that you don't want polluting the fleet "
                "summary during routine runs."
            ),
        )
        sp.add_argument(
            "--limit",
            type=int,
            default=None,
            metavar="N",
            help=(
                "Stop after N stations (applied AFTER include/exclude "
                "filters). Use for smoke-testing the pipeline without "
                "waiting for the full fleet — e.g. `--limit 5` runs "
                "in a few seconds."
            ),
        )
        sp.add_argument(
            "--stations-cfg",
            type=Path,
            default=None,
            metavar="PATH",
            help=(
                "Override the stations.cfg path. Section names in the "
                "cfg are the marker list — one section per station. "
                "Defaults to $GPS_CONFIG_PATH/stations.cfg → "
                "~/.config/gpsconfig/stations.cfg. Useful for running "
                "against a snapshot or staging cfg."
            ),
        )
        sp.add_argument(
            "--catalog",
            type=Path,
            default=None,
            help=(
                "Override the attribute-codes catalog path "
                "(data/attribute_codes.yaml). Audits use the catalog "
                "to know which attributes are required + their "
                "defaults. Override for testing against an updated "
                "catalog without committing it."
            ),
        )
        sp.add_argument(
            "--no-suppressions",
            action="store_true",
            help=(
                "Bypass per-audit SUPPRESS files entirely. SUPPRESS "
                "lines silence known-acceptable violations (e.g. a "
                "specific station's missing antenna_height that was "
                "manually verified). Pass this to find out what stale "
                "SUPPRESS lines are hiding — fleet-wide audit hygiene."
            ),
        )
        sp.add_argument(
            "--suppressions",
            type=Path,
            default=None,
            help=(
                "Override the suppression file directory. Each audit "
                "consults its own filename (attribute_dates.txt / "
                "missing_attributes.txt) in this dir."
            ),
        )
        _add_archive_arguments(sp)
        _add_coverage_arguments(sp)
        sp.add_argument(
            "--json",
            action="store_true",
            help=(
                "Emit machine-readable JSON instead of the text table. "
                "Shape: {run_kind, generated_at, totals, exit_code, "
                "results: [{station, station_id, status, "
                "findings_count, ...}, ...]}. Designed for `jq` and "
                "dashboard ingestion."
            ),
        )

    p_tri = sub.add_parser(
        "triage",
        help=(
            "Generate per-station triage files across the fleet. "
            "Clean stations are skipped by default."
        ),
        description=(
            "Generate combined-audit ACTION-style triage files for "
            "every GNSS station in stations.cfg.\n\n"
            "Each triage file aggregates every audit's findings for "
            "ONE station: missing required attributes, suspicious "
            "attribute dates, and (with `--with-archive`) RINEX vs TOS "
            "discrepancies. Each finding emits a SUGGESTED, "
            "COMMENTED-OUT ACTION line. The operator reviews + "
            "uncomments lines they agree with, fills any `<FILL_VALUE>` "
            "placeholders, then runs `tos audit apply <file>` to "
            "commit. Nothing is written to TOS by this command.\n\n"
            "BY DEFAULT only stations with at least one finding (or an "
            "audit failure) produce a file — the operator's working "
            "directory stays clean of empty inventory files. Pass "
            "`--include-clean` to write a file for every station "
            "regardless (full inventory; useful for git-tracked "
            "snapshot work).\n\n"
            "OUTPUT LAYOUT\n"
            "  data/triage/<stn>/<stn>_audit_<YYYYMMDD>.txt\n"
            "    one subdirectory per station, one dated file per\n"
            "    run-day. Same-day re-runs overwrite that day's file;\n"
            "    tomorrow produces a new dated file alongside (the\n"
            "    YYYYMMDD slug is the only thing that changes).\n\n"
            "  Override the root with `--out-dir DIR`; the per-station\n"
            "  subdirectory structure is preserved under whatever\n"
            "  root you pick.\n\n"
            "TYPICAL WORKFLOW\n"
            "  tos fleet triage --include HEDI SAVI\n"
            "  $EDITOR data/triage/hedi/hedi_audit_20260528.txt\n"
            "  tos audit apply data/triage/hedi/hedi_audit_20260528.txt\n"
            "  tos audit apply data/triage/hedi/hedi_audit_20260528.txt \\\n"
            "      --apply                       # commit to TOS\n"
            "  tos station verify HEDI           # confirm clean\n"
            "  git add data/triage/hedi/         # provenance trail\n\n"
            "PROVENANCE NOTE\n"
            "  TOS attribute_value rows have date_from/date_to but no\n"
            "  created_at. Back-fills written today and dated to 2007\n"
            "  are indistinguishable in TOS from contemporaneous\n"
            "  records. Triage files committed to git ARE the audit\n"
            "  trail for retrospective writes — that's why we keep\n"
            "  them in the repo even after `tos audit apply` lands."
        ),
        formatter_class=_argparse.RawDescriptionHelpFormatter,
    )
    p_tri.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Override the triage output root directory. Defaults to "
            "./data/triage/ (relative to CWD). Per-station "
            "subdirectories `<STN>/` are created under whatever root "
            "is chosen. Useful for sending a sweep to /tmp/ when "
            "iterating, or to a snapshot dir when reviewing."
        ),
    )
    p_tri.add_argument(
        "--include-clean",
        action="store_true",
        help=(
            "Also write triage files for clean stations (which would "
            "otherwise be skipped). Yields a complete fleet inventory "
            "— useful for snapshot work, fleet-wide diff review, or "
            "any time you want a file for every station regardless of "
            "findings. Default skips clean stations to keep the "
            "working directory uncluttered for routine runs."
        ),
    )
    _add_common_filters(p_tri)

    p_sta = sub.add_parser(
        "status",
        help=(
            "Bulk verify across the fleet. Exit 0 clean / 1 findings "
            "/ 2 audit failure. No disk writes."
        ),
        description=(
            "Run every audit against every GNSS station and emit an "
            "aggregate fleet dashboard. Read-only — no files written, "
            "no TOS state mutated.\n\n"
            "Equivalent to `tos station verify` looped across the "
            "fleet and tallied. Useful as a daily health check, a "
            "cron-job gate, or the verify half of the\n"
            "`tos audit apply → verify` loop after a bulk fix.\n\n"
            "EXIT CODES (cron / CI friendly)\n"
            "  0   all stations clean — fleet is healthy\n"
            "  1   at least one station has findings — bulk metadata\n"
            "      cleanup is needed (run `tos fleet triage` next)\n"
            "  2   at least one station's audit raised an exception\n"
            "      (TOS lookup error, malformed catalog, transient\n"
            "      HTTP failure). Distinct from 'findings' so cron /\n"
            "      CI can distinguish 'fleet needs work' from\n"
            "      'oracle broken'. ``--no-suppressions`` also tends\n"
            "      to bump some stations into exit 1.\n\n"
            "OUTPUT MODES\n"
            "  default text         dashboard table (clean rows\n"
            "                       suppressed) on stdout, progress on\n"
            "                       stderr — pipe-friendly\n"
            "  --show-clean         include clean rows too (full fleet\n"
            "                       table, useful for snapshots)\n"
            "  --json               machine-readable: totals dict +\n"
            "                       per-station rows + exit_code.\n"
            "                       Perfect for `jq` / dashboards.\n\n"
            "FILTERS\n"
            "  --include STN1 STN2  spot-check named stations only\n"
            "                       (fast: skips marker resolution for\n"
            "                       everything else)\n"
            "  --exclude STN3       skip known-broken stations\n"
            "  --limit N            stop after N stations (smoke test)\n"
            "  --with-archive       also run RINEX archive cross-check\n"
            "                       (slow at fleet scale — auto-warns)\n\n"
            "EXAMPLES\n"
            "  tos fleet status\n"
            "      # full fleet health check, ~5-15 min\n\n"
            "  tos fleet status --include HEDI SAVI\n"
            "      # spot check two stations, ~5 sec\n\n"
            "  tos fleet status --json | jq '.totals'\n"
            "      # cron-job summary\n\n"
            "  tos fleet status --json | jq '.results[] | "
            'select(.status=="findings") | .station\'\n'
            "      # list stations needing attention\n\n"
            "  tos fleet status --no-suppressions\n"
            "      # surface what stale SUPPRESS files are hiding"
        ),
        formatter_class=_argparse.RawDescriptionHelpFormatter,
    )
    p_sta.add_argument(
        "--show-clean",
        action="store_true",
        help=(
            "Include clean stations in the output table. Default "
            "suppresses them since they carry no actionable signal "
            "— most fleet runs surface only the 5-20 stations that "
            "need attention. Use this for a full inventory snapshot, "
            "compliance reports, or to sanity-check that the audits "
            "actually ran against the stations you expected."
        ),
    )
    _add_common_filters(p_sta)

    p_cd = sub.add_parser(
        "contact-dates",
        help=(
            "Sweep `tos audit contact-dates` across the fleet. Flags "
            "contact↔station relationships with a TOS-migration date "
            "(non-midnight per_time_from)."
        ),
        description=(
            "Run the contact-dates audit against every GNSS station and "
            "aggregate the migration-artifact relationships fleet-wide. "
            "Read-only — no TOS state mutated.\n\n"
            "Migration bulk-loads gave each contact↔station relationship "
            "a time_from set to the load instant (a non-midnight "
            "clock time) rather than the real ownership-start date. "
            "This sweep surfaces them all.\n\n"
            "Use --triage to emit ONE combined action file. Owner-role "
            "relationships are emitted UNCOMMENTED (backdating to "
            "`start`/founding is always correct — the owner owned the "
            "station from founding); non-owner roles (data_owner / "
            "operator / observer) are COMMENTED for review (they may "
            "have a genuinely recent start date).\n\n"
            "One-time cleanup — once swept it stays clean (no new "
            "migration), so this is not in the recurring verify oracle."
        ),
        epilog=(
            "Examples:\n"
            "  tos fleet contact-dates\n"
            "  tos fleet contact-dates --triage data/triage/contact_dates_fleet.txt\n"
            "  tos fleet contact-dates --json | jq '.totals'\n"
        ),
        formatter_class=_argparse.RawDescriptionHelpFormatter,
    )
    p_cd.add_argument(
        "--triage",
        dest="triage_path",
        type=Path,
        default=None,
        help=(
            "Emit one combined triage file (owner-role uncommented, "
            "non-owner commented). Apply with `tos audit apply`."
        ),
    )
    p_cd.add_argument(
        "--include",
        nargs="+",
        default=None,
        metavar="STN",
        help="Restrict to these markers (case-insensitive).",
    )
    p_cd.add_argument(
        "--exclude",
        nargs="+",
        default=None,
        metavar="STN",
        help="Skip these markers.",
    )
    p_cd.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N stations (smoke test).",
    )
    p_cd.add_argument(
        "--stations-cfg",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override the stations.cfg path.",
    )
    p_cd.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help="Override the contact_dates.txt suppression path.",
    )
    p_cd.add_argument(
        "--no-suppressions",
        action="store_true",
        help="Bypass the suppression file.",
    )
    p_cd.add_argument("--json", action="store_true", help="Machine-readable JSON.")

    args = p.parse_args(argv)

    from .api.tos_client import TOSClient
    from .fleet_ops import (
        fleet_summary_to_dict,
        format_fleet_summary,
        run_fleet_triage,
        run_fleet_verify,
    )
    from .station_triage import STATUS_MARK

    client = TOSClient()

    # contact-dates has its own summary type + render path — handle it
    # before the shared status/triage machinery below.
    if args.verb == "contact-dates":
        from .fleet_ops import (
            fleet_contact_dates_to_dict,
            format_fleet_contact_dates_report,
            format_fleet_contact_dates_triage,
            run_fleet_contact_dates,
        )

        def _cd_enum(idx, total):
            if total and (idx == total or idx % 20 == 0 or idx == 1):
                print(
                    f"resolving stations.cfg markers… {idx}/{total}",
                    file=_sys.stderr,
                )

        def _cd_progress(idx, total, st):
            if idx % 25 == 0 or idx == total:
                print(f"  audited {idx}/{total}…", file=_sys.stderr)

        try:
            cd_summary = run_fleet_contact_dates(
                client,
                use_suppressions=not bool(args.no_suppressions),
                suppressions_path=args.suppressions,
                station_cfg_path=str(args.stations_cfg) if args.stations_cfg else None,
                include=args.include,
                exclude=args.exclude,
                limit=args.limit,
                progress=_cd_progress,
                enumerate_progress=_cd_enum,
            )
        except RuntimeError as exc:
            print(f"tos fleet: {exc}", file=_sys.stderr)
            return 2

        if args.triage_path:
            content = format_fleet_contact_dates_triage(cd_summary)
            args.triage_path.write_text(content, encoding="utf-8")
            print(
                f"wrote combined triage file: {args.triage_path} "
                f"({cd_summary.total_violations} violation(s))",
                file=_sys.stderr,
            )
        if args.json:
            print(
                _json.dumps(
                    fleet_contact_dates_to_dict(cd_summary),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(format_fleet_contact_dates_report(cd_summary), end="")
        # Exit 1 if any migration-artifact dates surfaced, else 0.
        return 1 if cd_summary.total_violations else 0

    # Stderr progress reporter — keeps stdout clean for JSON / piping.
    def _progress(idx, total, result):
        mark = STATUS_MARK.get(result.status, "?")
        print(
            f"[{idx:3d}/{total}] {mark} {result.station:<10} "
            f"{result.status:<8}  {result.findings_count} finding(s)",
            file=_sys.stderr,
        )

    def _enum_progress(idx, total):
        # The marker-resolution loop dominates wall-clock for the first
        # ~100s of a cold run. Heartbeat at every 20 markers so the
        # operator can tell it's alive without flooding.
        if total and (idx == total or idx % 20 == 0 or idx == 1):
            print(
                f"resolving stations.cfg markers… {idx}/{total}",
                file=_sys.stderr,
            )

    use_suppressions: bool = not bool(args.no_suppressions)
    with_archive: bool = bool(getattr(args, "with_archive", False))
    min_gap_days: float = float(getattr(args, "archive_min_gap_days", 30.0))
    archive_root = getattr(args, "archive_root", None)
    station_cfg_path = str(args.stations_cfg) if args.stations_cfg else None

    # Surface the slow-path hazard up front. --with-archive triggers a
    # cold-archive walk for every station; on a full 173-station sweep
    # that's 30+ minutes of I/O before the first finding lands. Warn
    # unless the operator has narrowed scope with --include / --limit.
    if with_archive and not args.include and not args.limit:
        print(
            "warning: --with-archive on a full fleet sweep walks the cold "
            "archive for ~173 stations (30+ min). Consider --include / "
            "--limit to narrow scope.",
            file=_sys.stderr,
        )

    try:
        if args.verb == "triage":
            summary = run_fleet_triage(
                client,
                out_dir=args.out_dir,
                include_clean=args.include_clean,
                use_suppressions=use_suppressions,
                suppressions_path=args.suppressions,
                catalog_path=args.catalog,
                with_archive=with_archive,
                archive_root=archive_root,
                min_gap_days=min_gap_days,
                with_coverage=getattr(args, "with_coverage", False),
                coverage_since=getattr(args, "coverage_since", None),
                coverage_window_days=getattr(args, "coverage_window_days", 7),
                station_cfg_path=station_cfg_path,
                include=args.include,
                exclude=args.exclude,
                limit=args.limit,
                progress=_progress,
                enumerate_progress=_enum_progress,
            )
        elif args.verb == "status":
            summary = run_fleet_verify(
                client,
                use_suppressions=use_suppressions,
                suppressions_path=args.suppressions,
                catalog_path=args.catalog,
                with_archive=with_archive,
                archive_root=archive_root,
                min_gap_days=min_gap_days,
                with_coverage=getattr(args, "with_coverage", False),
                coverage_since=getattr(args, "coverage_since", None),
                coverage_window_days=getattr(args, "coverage_window_days", 7),
                station_cfg_path=station_cfg_path,
                include=args.include,
                exclude=args.exclude,
                limit=args.limit,
                progress=_progress,
                enumerate_progress=_enum_progress,
            )
        else:
            return 2
    except RuntimeError as exc:
        # enumerate_fleet_stations raises when zero stations resolve —
        # surface that as a clean usage error rather than a stack trace.
        print(f"tos fleet: {exc}", file=_sys.stderr)
        return 2

    if args.json:
        print(_json.dumps(fleet_summary_to_dict(summary), ensure_ascii=False, indent=2))
    else:
        show_clean = (
            getattr(args, "show_clean", False) if args.verb == "status" else False
        )
        print(format_fleet_summary(summary, show_clean=show_clean), end="")

    if args.verb == "status":
        # Verify-oracle exit code: 0 clean / 1 findings / 2 failed.
        return summary.exit_code()
    # Triage is a build-output verb; exit 0 unless the enumeration step
    # blew up (handled above). Per-station failures land in the summary
    # for the operator to triage, not as a non-zero exit.
    return 0


def _audit_main(argv):
    """Handle ``tos audit <kind>`` subcommands.

    Step 1 of the device-warehouse implementation order — read-only invariant
    checks for devices (I1) and stations (I2) per the design doc at
    ``2.Areas/VI_GPS_Library/1778592216-device-warehouse-design.md``.

    Exit codes: 0 = clean, 1 = invariant violation detected, 2 = usage error
    or entity not found. Completeness warnings on a station are advisory and
    do **not** affect the exit code.
    """
    import json as _json
    from pathlib import Path

    from . import audit as audit_mod
    from .api.tos_client import TOSClient

    p = argparse.ArgumentParser(
        prog="tos audit",
        description=(
            "Verify TOS device-warehouse invariants and patch corrections. "
            "Most verbs are read-only (device, station, orphans, "
            "fleet-gaps, timeline, show, attribute-dates) — no credentials "
            "needed. `apply` is the exception: it consumes an operator-"
            "edited ACTION file and writes back to TOS, so it requires "
            "credentials via $TOS_USERNAME/$TOS_PASSWORD, the [tos] "
            "section in database.cfg, or interactive prompt. `apply` "
            "defaults to dry-run; pass --apply to commit writes."
        ),
        epilog=(
            "WHAT TOS TRACKS\n"
            "  Every GPS device (receiver, antenna, radome, monument) is its\n"
            "  own entity in TOS. The location of a device — 'this receiver\n"
            "  is plugged into station X' — is NOT stored as an attribute. It\n"
            "  is a parent-child *join* record with a date range:\n"
            "    time_from = day the device was attached\n"
            "    time_to   = day it was removed (NULL for the current state)\n"
            "  When a device moves, the old join should close (time_to is\n"
            "  set) and a new one open. B9-Jörð (id_entity=4) is the virtual\n"
            "  'warehouse' station: a device that is not deployed in the\n"
            "  field should be joined to B9.\n"
            "\n"
            "INVARIANTS THIS COMMAND CHECKS (TOS does not enforce them)\n"
            "  I1  Every device has exactly one open join at any moment.\n"
            "      Violations:\n"
            "        I1 no-parent : id_entity_parent on the device is null.\n"
            "        I1 orphan    : the device's last join was closed but no\n"
            "                       replacement was opened — the device is\n"
            "                       'in limbo'.\n"
            "        I1 multi-open: more than one open join to the same\n"
            "                       parent (internally inconsistent).\n"
            "  I2  Every station has at most one open join per device\n"
            "      subtype (no two active receivers, etc., at one station).\n"
            "  Completeness (advisory, never blocks):\n"
            "      A full GPS station has one open receiver + antenna +\n"
            "      monument. Partial sets are legal but flagged.\n"
            "\n"
            "WHY THIS MATTERS\n"
            "  Wrong joins propagate into RINEX metadata, dashboards, GAMIT,\n"
            "  IGS site logs. Audit catches inconsistencies before they leak\n"
            "  downstream.\n"
            "\n"
            "Examples:\n"
            "\n"
            "  # ---- I1 / I2 invariant checks ----------------------------\n"
            "  tos audit device --serial 3235768 --subtype receiver\n"
            "  tos audit device --id 21489\n"
            "  tos audit device --id 21489 --verbose\n"
            "  tos audit station RHOF\n"
            "  tos audit station --id 4               # B9 - Kjallari - Jörð\n"
            "  tos audit orphans --subtype receiver\n"
            "  tos audit orphans --subtype receiver --verbose\n"
            "  tos audit orphans --subtype receiver --model POLARX5 --json\n"
            "\n"
            "  # ---- Drill-downs ----------------------------------------\n"
            "  tos audit show --id 4773                # full record + joins\n"
            "  tos audit show --id 4773 --no-joins     # attributes only (fast)\n"
            "  tos audit timeline 4773                 # complete join chronology\n"
            "  tos audit timeline 4773 4501 4547       # multiple devices, one index build\n"
            "  tos audit fleet-gaps --min-days 365     # high-confidence gaps tail\n"
            "\n"
            "  # ---- Attribute-date misdating (rule 3) ------------------\n"
            "  tos audit attribute-dates RHOF                       # default inherent-only\n"
            "  tos audit attribute-dates RHOF --verbose             # SUPPRESS hints + silenced entries\n"
            "  tos audit attribute-dates RHOF --include owner       # add one mutable code\n"
            "  tos audit attribute-dates RHOF --include owner,firmware_version\n"
            "  tos audit attribute-dates RHOF --exclude serial_number\n"
            "  tos audit attribute-dates RHOF --include-mutable     # sledgehammer (all mutable)\n"
            "  tos audit attribute-dates RHOF --triage trial.txt    # emit ACTION file\n"
            "  tos audit attribute-dates RHOF --no-suppressions     # bypass committed SUPPRESSes\n"
            "  tos audit attribute-dates RHOF --json                # machine-readable\n"
            "\n"
            "  # ---- Missing required attributes (Layer 6) ---------------\n"
            "  tos audit missing-attributes HAUC                    # station + open devices + monument\n"
            "  tos audit missing-attributes HAUC --verbose          # show SUPPRESS hints + silenced entries\n"
            "  tos audit missing-attributes HAUC --triage trial.txt # emit ACTION file (add-attribute lines)\n"
            "  tos audit missing-attributes HAUC --no-suppressions  # bypass committed SUPPRESSes\n"
            "  tos audit missing-attributes HAUC --json             # machine-readable\n"
            "\n"
            "  # ---- Visit coverage (Phase D — equipment changes vs vitjanir) ----\n"
            "  tos audit visit-coverage HAUC                        # default (last 2y, ±7d)\n"
            "  tos audit visit-coverage HAUC --since 2020-01-01     # widen historical scope\n"
            "  tos audit visit-coverage HAUC --coverage-window-days 14  # widen tolerance\n"
            "  tos audit visit-coverage HAUC --triage hauc_cov.txt  # emit add-visit ACTIONs\n"
            "  tos audit visit-coverage HAUC --no-suppressions      # bypass SUPPRESS file\n"
            "\n"
            "  # ---- Contact dates (migration-artifact per_time_from) ----\n"
            "  tos audit contact-dates RHOF                         # flag non-midnight relationship dates\n"
            "  tos audit contact-dates RHOF --triage rhof_cts.txt   # emit patch-contact-relationship ACTIONs\n"
            "\n"
            "  # ---- Apply triage files (writes; needs credentials) -----\n"
            "  tos audit apply trial.txt                            # dry-run preview (default)\n"
            "  tos audit apply trial.txt --apply                    # commit writes\n"
            "  tos audit apply trial.txt --json                     # structured report\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="kind", required=True)

    p_dev = sub.add_parser(
        "device",
        help="Audit one device (invariant I1).",
        description=(
            "Verify that one device has exactly one open join to its current "
            "parent. Exits 0 on I1 OK, 1 on I1 violation, 2 on lookup or "
            "usage error."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit device --serial 3235768 --subtype receiver\n"
            "  tos audit device --id 21489\n"
            "  tos audit device --id 21489 --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dev_target = p_dev.add_mutually_exclusive_group(required=True)
    dev_target.add_argument(
        "--serial", help="Device serial number; requires --subtype."
    )
    dev_target.add_argument(
        "--id", dest="id_entity", type=int, help="Device id_entity."
    )
    p_dev.add_argument(
        "--subtype",
        choices=sorted(set(audit_mod.SUBTYPE_ALIASES)),
        help="Device subtype (short or canonical). Required with --serial.",
    )
    p_dev.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_dev.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_dev.add_argument("--port", type=int, default=443)
    p_dev.add_argument(
        "--verbose",
        action="store_true",
        help="On violations, add a plain-English block explaining what it "
        "means, the expected state, and how to fix it.",
    )

    p_st = sub.add_parser(
        "station",
        help="Audit one station (real station: I2; warehouse: inventory).",
        description=(
            "Subtype-aware. For a real physical station (code_entity_subtype = "
            "'geophysical' — Jarðeðlisstöð such as RHOF), verify I2 (at most "
            "one open join per device subtype) and emit non-blocking "
            "completeness warnings when expected subtypes are missing. "
            "For a warehouse-style entity (Lager, such as B9 - Kjallari - "
            "Jörð, id_entity=4), I2 does not apply — render an inventory "
            "listing instead. Exits 0 on I2 OK (or warehouse), 1 on I2 "
            "violation, 2 on lookup or usage error.\n\n"
            "The positional argument matches either the station's marker "
            "(short id, like 'RHOF') or its display name ('Raufarhöfn'). "
            "Markers are tried first."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit station RHOF                 # marker lookup\n"
            "  tos audit station Raufarhöfn           # display-name lookup\n"
            "  tos audit station --id 4               # B9 - Kjallari - Jörð (warehouse)\n"
            "  tos audit station RHOF --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    st_target = p_st.add_mutually_exclusive_group(required=True)
    st_target.add_argument(
        "name", nargs="?", help="Station marker (e.g. RHOF) or display name."
    )
    st_target.add_argument(
        "--id", dest="id_entity", type=int, help="Station id_entity."
    )
    p_st.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_st.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_st.add_argument("--port", type=int, default=443)
    p_st.add_argument(
        "--verbose",
        action="store_true",
        help="On violations, add a plain-English block explaining what it "
        "means, the expected state, and how to fix it.",
    )

    p_orph = sub.add_parser(
        "orphans",
        help="List I1-orphan devices across the fleet (scan-by-model).",
        description=(
            "Enumerate devices of a given subtype via basic_search on a list "
            "of model strings, audit each, and report those with I1 "
            "violations (closed-without-replacement orphans, multi-open "
            "joins, or no current parent). For gnss_receivers the default "
            "list covers the enumerable fleet (~322 devices across modern + "
            "legacy models) as discovered via a TOS probe on 2026-05-12.\n\n"
            "Known limitation: TOS basic_search mis-indexes hyphen-and-digit "
            "patterns, so ASHTECH Z-XII3 receivers cannot be enumerated by "
            "any model search. Use `tos audit device --id <n>` for those, "
            "or wait for `cfg fix` (todo #5) to enumerate by join graph "
            "instead.\n\n"
            "Exits 0 when no violations are found, 1 when at least one "
            "violation is reported, 2 on usage error."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit orphans --subtype receiver\n"
            "  tos audit orphans --subtype receiver --model POLARX5 --model NetR9\n"
            "  tos audit orphans --subtype receiver --json\n"
            "\n"
            "Default models per subtype (used when --model is not given):\n"
            + "\n".join(
                f"  {sub}: {', '.join(models)}"
                for sub, models in audit_mod.DEFAULT_ORPHAN_SCAN_MODELS.items()
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_orph.add_argument(
        "--subtype",
        required=True,
        choices=sorted(set(audit_mod.SUBTYPE_ALIASES)),
        help="Device subtype to scan (short or canonical).",
    )
    p_orph.add_argument(
        "--model",
        action="append",
        dest="models",
        help=(
            "Model string passed to basic_search (repeatable). When omitted, "
            "uses the subtype's DEFAULT_ORPHAN_SCAN_MODELS list."
        ),
    )
    p_orph.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_orph.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_orph.add_argument("--port", type=int, default=443)
    p_orph.add_argument(
        "--verbose",
        action="store_true",
        help="Add a plain-English preamble explaining what an I1 orphan is "
        "and how to fix one.",
    )

    p_fleet = sub.add_parser(
        "fleet-gaps",
        help="Report devices whose join history has unrecorded coverage gaps.",
        description=(
            "Walk every known parent's children_connections, build the global "
            "join index, and surface devices whose timeline contains gaps "
            "longer than --min-days. A gap is the time between the close of "
            "one join and the open of the next; when such a stretch exists, "
            "the device was somewhere TOS does not record (typically: sat at "
            "B9 after pickup, or was sent for repair, but the move was never "
            "entered).\n\n"
            "This is a *report*, not an invariant gate — exit 0 always, "
            "even when gaps are reported. Use --json to feed the output into "
            "downstream tooling, or follow up on individual rows with "
            "`tos audit device --id <n>`.\n\n"
            "Empirical baseline (2026-05-12 fleet probe): with --min-days 30, "
            "the IMO fleet surfaces ~50 devices and ~115 gaps; with "
            "--min-days 365, ~40 devices in the high-confidence tail. Below "
            "~7 days the result set is dominated by date-rounding artifacts."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit fleet-gaps                                # ≥30d gaps + orphans\n"
            "  tos audit fleet-gaps --min-days 365                 # high-confidence tail\n"
            "  tos audit fleet-gaps --subtype receiver --json      # GNSS receivers, JSON\n"
            "  tos audit fleet-gaps --top 10 --no-orphans          # top 10 longest gaps\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_fleet.add_argument(
        "--min-days",
        type=float,
        default=30.0,
        help="Minimum gap duration in days (default: 30). Below ~7 the "
        "report fills with date-rounding noise; ≥365 isolates the "
        "high-confidence tail.",
    )
    p_fleet.add_argument(
        "--subtype",
        choices=sorted(set(audit_mod.SUBTYPE_ALIASES)),
        help="Filter to one device subtype. Requires per-device enrichment "
        "(implicit unless --no-enrich is set).",
    )
    p_fleet.add_argument(
        "--top",
        type=int,
        default=None,
        help="Show only the N rows with the longest gaps (rows are already "
        "sorted by max gap descending).",
    )
    p_fleet.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip per-device subtype/serial/model lookup. Faster, but the "
        "output only carries id_entity. Incompatible with --subtype.",
    )
    p_fleet.add_argument(
        "--no-orphans",
        action="store_true",
        help="Suppress the truly-orphan section (devices with closed joins "
        "but none open). Default reports both gaps and orphans.",
    )
    p_fleet.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_fleet.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_fleet.add_argument("--port", type=int, default=443)
    p_fleet.add_argument(
        "--verbose",
        action="store_true",
        help="Print a per-gap detail block for every row, not just the "
        "single longest gap. Independent of --json.",
    )
    p_fleet.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the parent-walk progress line on stderr.",
    )
    p_fleet.add_argument(
        "--with-timelines",
        action="store_true",
        help="Embed each device's complete join history under its row. "
        "Reuses the same index walk (no extra cost). Use this when "
        "drilling down from a fleet-gap row needs the surrounding "
        "context — equivalent to running `timeline` for every "
        "surfaced device in a single invocation.",
    )

    p_tl = sub.add_parser(
        "timeline",
        help="Print one or more devices' complete join history (drill-down).",
        description=(
            "Walk the global join index once, then dump every join — open "
            "or closed — for each requested device id, in chronological "
            "order. Gaps between adjacent joins are annotated inline. This "
            "is the drill-down companion to `fleet-gaps`: use that to find "
            "interesting devices, then pass their id_entity values here to "
            "see what TOS actually has on file.\n\n"
            "Pass multiple ids in a single invocation so the ~110s index "
            "build is amortised — `timeline 16321 4926 16576 19712` is one "
            "build, four lookups. Default `--min-gap-days=0` surfaces every "
            "gap (timeline view normally wants the full picture, unlike "
            "fleet-gaps which filters noise)."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit timeline 19969                             # one device, full history\n"
            "  tos audit timeline 16321 4926 16576 19712            # the fleet-gaps top 4\n"
            "  tos audit timeline 16581 --json                      # structured output\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_tl.add_argument(
        "ids",
        nargs="+",
        type=int,
        help="One or more device id_entity values to report on.",
    )
    p_tl.add_argument(
        "--min-gap-days",
        type=float,
        default=0.0,
        help="Threshold for gap annotations. Default 0 (every gap shown). "
        "Raise to suppress short date-rounding artifacts.",
    )
    p_tl.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip per-device subtype/serial/model lookup. The join history "
        "is still complete; only the header line loses metadata.",
    )
    p_tl.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_tl.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_tl.add_argument("--port", type=int, default=443)
    p_tl.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress the parent-walk progress lines on stderr.",
    )

    p_apply = sub.add_parser(
        "apply",
        help="Apply ACTION lines from an operator-edited triage file.",
        description=(
            "Read an action file and apply each ACTION line as a TOS write. "
            "Default is dry-run: every action is logged but no HTTP write "
            "goes out. Pass --apply to commit.\n\n"
            "File format — one ACTION per line, '#' for comments:\n"
            "  ACTION <id_entity> <verb> [args...]\n\n"
            "Verbs:\n"
            "  change-subtype <code>     PUT /admin_entity_row/<id>/ "
            "with id_entity_subtype=<resolved-int>\n"
            "  decommission <date>       Close the device's open join + "
            "transition status to óvirkt on <date>\n"
            "  move <to_parent_id> <date>\n"
            "                            Close the device's open join + "
            "open a new join at <to_parent_id> on <date>\n"
            "  fill-gap <parent_id> <date_from> <date_to>\n"
            "                            POST a closed join for a known "
            "historical window (cfg-fix backfill)\n"
            "  patch-attribute-date <code> <old_date_from> <new_date_from>\n"
            "                            PATCH /attribute_value/<id> "
            "date_from — consumes triage files from\n"
            "                            `tos audit attribute-dates --triage`\n"
            "  add-attribute <code> <value> <date_from>\n"
            "                            POST /attribute_values — add a new open "
            "period to fill\n"
            "                            a required-attribute gap. Consumes triage "
            "files from\n"
            "                            `tos audit missing-attributes --triage`. "
            "Quote values\n"
            "                            with spaces, e.g. `'GPS stöð'`.\n"
            "  delete-join <id_connection>\n"
            "                            DELETE /admin_entity_connection_row/<id> "
            "— permanently\n"
            "                            remove a join row. Destructive / "
            "history-erasing. Use\n"
            "                            ONLY on known-bad rows (e.g. "
            "SOPAC-convention\n"
            "                            split-monument workaround joins). "
            "Admin-only.\n"
            "  delete-attribute-value <id_attribute_value>\n"
            "                            DELETE /admin_attribute_value_row/<id> "
            "— permanently\n"
            "                            remove an attribute_value row. "
            "Destructive. Use on\n"
            "                            wrong-scope id_attribute FKs, "
            "duplicates, orphans.\n"
            "                            Admin-only.\n"
            "  defer                      no-op placeholder (review next run)\n\n"
            "Validation: each ACTION line is parsed before any HTTP call. "
            "If any line is malformed, nothing is sent. Otherwise actions "
            "run in file order; a single failed write logs the error and "
            "continues to the next."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit apply triage.txt              # dry-run (default)\n"
            "  tos audit apply triage.txt --apply      # commit writes\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_apply.add_argument(
        "action_file",
        help="Path to the action file (text, one ACTION per line).",
    )
    p_apply.add_argument(
        "--apply",
        action="store_true",
        help="Commit writes. Without this flag, payloads are logged only "
        "(safe default).",
    )
    p_apply.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_apply.add_argument("--port", type=int, default=443)
    p_apply.add_argument(
        "--json", action="store_true", help="Emit a structured JSON summary."
    )

    p_show = sub.add_parser(
        "show",
        help="Display a device's full record — attributes + (optional) join chronology.",
        description=(
            "Print the complete TOS record for one device: header, every "
            "attribute period (status / firmware / model / ...) grouped by "
            "code, and (when --no-joins is not set) the full chronological "
            "join history. Useful for verifying what TOS knows about a "
            "device after a write, or as input to deciding fill-gap / "
            "decommission actions.\n\n"
            "Accepts the device by id (preferred when known) or by "
            "(serial, subtype). The subtype is the canonical TOS code "
            "(``digitizer``, ``gps_clock``, ``gnss_receiver``, ...) — see "
            "vault note ``1778677922-tos-entity-subtype-codes`` for the "
            "full list. Unlike ``audit device --id N``, this verb is "
            "subtype-agnostic and works on the broader fleet."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit show --id 19712                # full record + joins (slow)\n"
            "  tos audit show --id 19712 --no-joins     # attributes only (fast)\n"
            "  tos audit show --serial G2584 --subtype digitizer --no-joins\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    show_target = p_show.add_mutually_exclusive_group(required=True)
    show_target.add_argument(
        "--id", dest="id_entity", type=int, help="Device id_entity."
    )
    show_target.add_argument(
        "--serial", help="Device serial number; requires --subtype."
    )
    p_show.add_argument(
        "--subtype",
        help="Canonical TOS subtype code (digitizer, gps_clock, ...). "
        "Required with --serial.",
    )
    p_show.add_argument(
        "--no-joins",
        action="store_true",
        help="Skip the join chronology (the ~110s index build). Attribute "
        "periods are still printed in full.",
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", type=int, default=443)

    p_attr = sub.add_parser(
        "attribute-dates",
        help="Flag TOS attribute periods misdated by data-entry stamp (rule 3).",
        description=(
            "Detect attribute periods whose `date_from` is later than the "
            "device's earliest known signal. TOS auto-stamps a period's "
            "date_from with the date the value was entered, not the date it "
            "became applicable — so retroactive data entry produces phantom "
            "transition dates that propagate into PrintTOS / sitelog / "
            "GAMIT. The discriminator is the station-side join time_from: "
            "when every attribute on a device is stamped at the entry date "
            "but the station's join carries a much earlier time_from, that "
            "contradiction surfaces the bug.\n\n"
            "Rule 3 fires when ``period.date_from > min(earliest attribute "
            "date_from, earliest station-side join time_from)``. By default "
            "only inherent codes (per data/attribute_codes.yaml) are "
            "checked — firmware bumps and other mutable transitions are "
            "skipped. Pass --include-mutable to widen.\n\n"
            "Exits 0 when no violations found, 1 when at least one is, "
            "2 on lookup / usage error. The (id_entity, code, date_from) "
            "triple in each violation is the natural suppression key for "
            "Layer 3 (data/audit_suppressions/attribute_dates.txt) — not "
            "implemented in this layer."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit attribute-dates ARHO\n"
            "  tos audit attribute-dates ARHO --verbose\n"
            "  tos audit attribute-dates RHOF --include-mutable --json\n"
            "  tos audit attribute-dates --id 1234 --subtypes antenna monument\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_attr.add_argument(
        "name", nargs="?", help="Station marker (e.g. ARHO) or display name."
    )
    p_attr.add_argument("--id", dest="id_entity", type=int, help="Station id_entity.")
    p_attr.add_argument(
        "--subtypes",
        nargs="+",
        help=(
            "Device subtypes to audit (short or canonical). Default: "
            "gnss_receiver, antenna, radome, monument."
        ),
    )
    p_attr.add_argument(
        "--include-mutable",
        action="store_true",
        help="Also check mutable codes (firmware bumps, status transitions, "
        "etc.). Default is inherent-only.",
    )
    p_attr.add_argument(
        "--include",
        action="append",
        default=[],
        dest="include",
        metavar="CODE[,CODE...]",
        help="Audit these codes regardless of their catalog classification "
        "(mutable, TODO, applies_to mismatch, gps_relevance=no all "
        "bypassed). Surgical alternative to --include-mutable. Repeatable, "
        "and each value may be a comma-separated list. Unknown codes "
        "raise an error with did-you-mean suggestions.",
    )
    p_attr.add_argument(
        "--exclude",
        action="append",
        default=[],
        dest="exclude",
        metavar="CODE[,CODE...]",
        help="Drop these codes entirely — not flagged, not even tracked as "
        "suppressed. Station-wide silencer (coarser than the per-violation "
        "SUPPRESS file). On conflict with --include, --exclude wins.",
    )
    p_attr.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Override the catalog YAML path. Defaults to repo "
        "data/attribute_codes.yaml or $TOSTOOLS_ATTRIBUTE_CODES_PATH.",
    )
    p_attr.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help="Override the suppression file path. Defaults to "
        "data/audit_suppressions/attribute_dates.txt. File-not-found is "
        "silent (the file is opt-in).",
    )
    p_attr.add_argument(
        "--no-suppressions",
        action="store_true",
        help="Bypass the suppression file entirely; every rule-3 hit is "
        "reported. Useful to verify what a stale SUPPRESS line is hiding.",
    )
    p_attr.add_argument(
        "--triage",
        dest="triage_path",
        type=Path,
        default=None,
        help="Emit a draft ACTION file at this path. One commented "
        "`ACTION ... patch-attribute-date ...` line per violation, with "
        "earliest_known as the suggested new date_from. Feeds into "
        "`tos audit apply <file>` (dry-run by default).",
    )
    p_attr.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_attr.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra context: anchor source per violation and any "
        "unknown attribute codes seen in TOS but missing from the catalog.",
    )
    p_attr.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_attr.add_argument("--port", type=int, default=443)

    p_missing = sub.add_parser(
        "missing-attributes",
        help="Flag required TOS attributes that have no open period.",
        description=(
            "Walk a station + its open child devices + monument and flag "
            "every catalog code where the entity's subtype is listed in "
            "``gps_required_for`` but the entity has no open attribute "
            "period for it. Complements `attribute-dates` (which checks "
            "the dates of attributes that *exist*); this verb checks the "
            "presence of attributes that *should* exist.\n\n"
            "Rule: for each entity in scope (station + open GPS-quartet "
            "children — gnss_receiver, antenna, radome, monument), iterate "
            "the catalog rules for that entity's scope. Flag every code "
            "where ``entity.code_entity_subtype ∈ entry['gps_required_for']`` "
            "AND the entity has no open attribute period for that code. "
            "Filters: ``gps_relevance == 'yes'`` gates above "
            "``gps_required_for`` — TODO / maybe / no entries are silently "
            "skipped until the operator classifies them.\n\n"
            "Exits 0 when no violations found (or all were suppressed), "
            "1 when at least one violation survives, 2 on lookup / usage "
            "error. The ``(id_entity, code)`` 2-tuple in each violation "
            "is the natural suppression key for Layer 3 "
            "(data/audit_suppressions/missing_attributes.txt)."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit missing-attributes HAUC\n"
            "  tos audit missing-attributes HAUC --verbose\n"
            "  tos audit missing-attributes HAUC --triage hauc_missing.txt\n"
            "  tos audit missing-attributes --id 1234 --subtypes antenna monument\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_missing.add_argument(
        "name", nargs="?", help="Station marker (e.g. HAUC) or display name."
    )
    p_missing.add_argument(
        "--id", dest="id_entity", type=int, help="Station id_entity."
    )
    p_missing.add_argument(
        "--subtypes",
        nargs="+",
        help=(
            "Device subtypes to audit (short or canonical). Default: "
            "gnss_receiver, antenna, radome, monument."
        ),
    )
    p_missing.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Override the catalog YAML path. Defaults to repo "
        "data/attribute_codes.yaml or $TOSTOOLS_ATTRIBUTE_CODES_PATH.",
    )
    p_missing.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help="Override the suppression file path. Defaults to "
        "data/audit_suppressions/missing_attributes.txt. File-not-found "
        "is silent (the file is opt-in).",
    )
    p_missing.add_argument(
        "--no-suppressions",
        action="store_true",
        help="Bypass the suppression file entirely; every missing-attribute "
        "hit is reported. Useful to verify what a stale SUPPRESS line is hiding.",
    )
    p_missing.add_argument(
        "--triage",
        dest="triage_path",
        type=Path,
        default=None,
        help="Emit a draft ACTION file at this path. One commented "
        "`ACTION ... add-attribute ...` line per violation, with the "
        "catalog's default_value pre-filled when present (otherwise "
        "<FILL_VALUE>) and the device's earliest open-join time_from "
        "as the date hint (otherwise <FILL_DATE>).",
    )
    p_missing.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_missing.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra context: SUPPRESS hint per violation and any "
        "entries that were silenced by the suppression file.",
    )
    p_missing.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_missing.add_argument("--port", type=int, default=443)

    p_coverage = sub.add_parser(
        "visit-coverage",
        help=(
            "Flag equipment-change events with no vitjun within ±N days. "
            "Phase D of the vitjanir CLI expansion."
        ),
        description=(
            "Cross-reference a station's join history against its "
            "vitjun history. For each join-open event in the --since "
            "window (default last 2 years, skips the 2014-10-17 "
            "cleanup-artifact pattern), check whether any vitjun on "
            "the station has start_time within ±N days of the event. "
            "Uncovered events become violations.\n\n"
            "v1 scope is intentionally narrow: opens only (not closes "
            "or attribute writes), station-attached vitjanir only "
            "(device-attached coverage will land when GPS-device "
            "vitjanir start appearing — empirically zero today).\n\n"
            "Use --triage to emit a draft action file with one "
            "commented `add-visit` ACTION per violation."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit visit-coverage HEDI\n"
            "  tos audit visit-coverage HEDI --since 2020-01-01\n"
            "  tos audit visit-coverage HEDI --coverage-window-days 14\n"
            "  tos audit visit-coverage HEDI --triage hedi_coverage.txt\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_coverage.add_argument(
        "name", nargs="?", help="Station marker (e.g. HEDI) or display name."
    )
    p_coverage.add_argument(
        "--id", dest="id_entity", type=int, help="Station id_entity."
    )
    p_coverage.add_argument(
        "--since",
        default=None,
        help=(
            "Earliest event date to audit (YYYY-MM-DD). Default: today "
            "minus 2 years. Older join-opens are silently skipped."
        ),
    )
    p_coverage.add_argument(
        "--coverage-window-days",
        dest="coverage_window_days",
        type=int,
        default=7,
        help=(
            "Half-width of the coverage window in days. ±N days around "
            "each event. Default 7."
        ),
    )
    p_coverage.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help=(
            "Override the suppression file path. Defaults to "
            "data/audit_suppressions/visit_coverage.txt. File-not-found "
            "is silent (the file is opt-in)."
        ),
    )
    p_coverage.add_argument(
        "--no-suppressions",
        action="store_true",
        help=(
            "Bypass the suppression file entirely. Useful to see what a "
            "stale SUPPRESS line is hiding."
        ),
    )
    p_coverage.add_argument(
        "--triage",
        dest="triage_path",
        type=Path,
        default=None,
        help=(
            "Emit a draft ACTION file at this path. One commented "
            '`ACTION ... add-visit change <event_date> "<FILL_WORK>"` '
            "line per violation."
        ),
    )
    p_coverage.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_coverage.add_argument(
        "--verbose",
        action="store_true",
        help="Show suppressed entries with file:lineno references.",
    )
    p_coverage.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_coverage.add_argument("--port", type=int, default=443)

    p_contact_dates = sub.add_parser(
        "contact-dates",
        help=(
            "Flag contact↔station relationships with a TOS-migration "
            "date (non-midnight per_time_from)."
        ),
        description=(
            "When TOS contacts were bulk-loaded into the new system, "
            "each contact↔station relationship got a time_from set to "
            "the moment of the load, not the real ownership-start date. "
            "The signal: a non-midnight time-of-day (genuine dates are "
            "recorded at T00:00:00; migration bulk-loads carry a real "
            "clock time, identical within each batch — e.g. 26 "
            "relationships all at 2025-02-04T15:32:38).\n\n"
            "Flags every relationship whose per_time_from has a "
            "non-midnight time component. Use --triage to emit "
            "patch-contact-relationship ACTIONs that backdate each to "
            "the station's earliest_known (the `start` token)."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit contact-dates RHOF\n"
            "  tos audit contact-dates RHOF --triage rhof_contacts.txt\n"
            "  tos audit contact-dates RHOF --no-suppressions --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_contact_dates.add_argument(
        "name", nargs="?", help="Station marker (e.g. RHOF) or display name."
    )
    p_contact_dates.add_argument(
        "--id", dest="id_entity", type=int, help="Station id_entity."
    )
    p_contact_dates.add_argument(
        "--suppressions",
        type=Path,
        default=None,
        help=(
            "Override the suppression file path. Defaults to "
            "data/audit_suppressions/contact_dates.txt. File-not-found "
            "is silent (the file is opt-in)."
        ),
    )
    p_contact_dates.add_argument(
        "--no-suppressions",
        action="store_true",
        help="Bypass the suppression file entirely.",
    )
    p_contact_dates.add_argument(
        "--triage",
        dest="triage_path",
        type=Path,
        default=None,
        help=(
            "Emit a draft ACTION file at this path. One commented "
            "`ACTION <station> patch-contact-relationship <id_rel> "
            "time_from start` line per violation."
        ),
    )
    p_contact_dates.add_argument(
        "--json", action="store_true", help="Emit JSON instead of plain text."
    )
    p_contact_dates.add_argument(
        "--verbose",
        action="store_true",
        help="Show suppressed entries with file:lineno references.",
    )
    p_contact_dates.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_contact_dates.add_argument("--port", type=int, default=443)

    p_verify = sub.add_parser(
        "verify-from-rinex",
        help=(
            "Cross-check TOS state against the cold RINEX archive. Detects "
            "data gaps, receiver-brand transitions, and TOS-claimed dates "
            "that don't match what's archived."
        ),
        description=(
            "Walks ``<archive>/<YYYY>/<mon>/<STATION>/15s_24hr/{raw,rinex}/`` "
            "for a station, classifies each archived file by receiver-brand "
            "family (`.sbf` → septentrio, `.T02` → trimble_netr9, etc.), and "
            "compares the resulting timeline against TOS's child-device "
            "joins. Surfaces brand transitions, multi-day gaps, and "
            "discrepancies between TOS-claimed join start dates and the "
            "earliest archived day for each brand.\n\n"
            "Archive root is resolved in order: --archive-root → env "
            "TOSTOOLS_ARCHIVE_ROOT → ``receivers.cfg [archive_paths] "
            "cold_archive_prepath`` (shared with the receivers package) → "
            "probe ``/mnt/rawgpsdata`` then ``/mnt_data/rawgpsdata``. Pin "
            "the path by adding ``cold_archive_prepath`` to the shared cfg."
        ),
        epilog=(
            "Examples:\n"
            "  tos audit verify-from-rinex --station SAVI\n"
            "  tos audit verify-from-rinex --station SAVI --json\n"
            "  tos audit verify-from-rinex --station SAVI "
            "--archive-root /mnt_data/rawgpsdata\n"
            "  tos audit verify-from-rinex --station SAVI --min-gap-days 90\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_verify.add_argument(
        "--station",
        required=True,
        help="Station marker (e.g. SAVI). Case-insensitive.",
    )
    p_verify.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "Override the resolved archive root. Default: env "
            "TOSTOOLS_ARCHIVE_ROOT, then receivers.cfg, then probed mount."
        ),
    )
    p_verify.add_argument(
        "--min-gap-days",
        type=int,
        default=30,
        help="Minimum gap to surface in the report (default: 30 days).",
    )
    p_verify.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of pretty text.",
    )
    p_verify.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_verify.add_argument("--port", type=int, default=443)

    args = p.parse_args(argv)

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    client = TOSClient(base_url=base_url)

    if args.kind == "device":
        if args.serial and not args.subtype:
            print("--subtype is required when using --serial", file=sys.stderr)
            return 2
        try:
            report = audit_mod.audit_device(
                client,
                serial=args.serial,
                id_entity=args.id_entity,
                subtype=args.subtype,
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(
                _json.dumps(
                    _device_report_to_dict(report), ensure_ascii=False, indent=2
                )
            )
        else:
            _print_device_report(report, verbose=args.verbose)
        return 0 if report.invariant_I1_ok else 1

    if args.kind == "orphans":
        try:
            scan = audit_mod.list_orphan_devices(
                client, subtype=args.subtype, models=args.models
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(_json.dumps(_orphan_scan_to_dict(scan), ensure_ascii=False, indent=2))
        else:
            _print_orphan_scan(scan, verbose=args.verbose)
        return 1 if scan.orphan_reports else 0

    if args.kind == "station":
        try:
            report = audit_mod.audit_station(
                client, name=args.name, id_entity=args.id_entity
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(
                _json.dumps(
                    _station_report_to_dict(report), ensure_ascii=False, indent=2
                )
            )
        else:
            _print_station_report(report, verbose=args.verbose)
        return 0 if report.invariant_I2_ok else 1

    if args.kind == "fleet-gaps":
        from . import history as history_mod

        if args.subtype and args.no_enrich:
            print(
                "--subtype requires per-device enrichment; drop --no-enrich.",
                file=sys.stderr,
            )
            return 2
        canonical_subtype = (
            audit_mod.canonical_subtype(args.subtype) if args.subtype else None
        )
        if args.no_progress or args.json or not sys.stderr.isatty():
            walk_progress = None
            enumerate_progress = None
        else:
            sys.stderr.write(
                "Resolving station markers (one basic_search per marker, "
                "~100s for the IMO fleet)...\n"
            )
            sys.stderr.flush()
            enumerate_progress = _stderr_progress("markers")
            walk_progress = _stderr_progress("parents")
        try:
            report = history_mod.scan_fleet_gaps(
                client,
                min_days=args.min_days,
                include_orphans=not args.no_orphans,
                enrich=not args.no_enrich,
                subtype=canonical_subtype,
                progress=walk_progress,
                enumerate_progress=enumerate_progress,
                with_timelines=args.with_timelines,
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.json:
            print(
                _json.dumps(
                    _fleet_gap_report_to_dict(report, top=args.top),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_fleet_gap_report(report, top=args.top, verbose=args.verbose)
        return 0

    if args.kind == "apply":
        return _apply_main(args)

    if args.kind == "show":
        if args.serial and not args.subtype:
            print("--subtype is required when using --serial", file=sys.stderr)
            return 2
        try:
            display_device_record(
                client,
                serial=args.serial,
                id_entity=args.id_entity,
                subtype=args.subtype,
                with_joins=not args.no_joins,
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0

    if args.kind == "timeline":
        from . import history as history_mod

        if args.no_progress or args.json or not sys.stderr.isatty():
            walk_progress = None
            enumerate_progress = None
        else:
            sys.stderr.write(
                "Resolving station markers (one basic_search per marker, "
                "~100s for the IMO fleet)...\n"
            )
            sys.stderr.flush()
            enumerate_progress = _stderr_progress("markers")
            walk_progress = _stderr_progress("parents")
        report = history_mod.get_device_timelines(
            client,
            args.ids,
            min_gap_days=args.min_gap_days,
            enrich=not args.no_enrich,
            progress=walk_progress,
            enumerate_progress=enumerate_progress,
        )
        if args.json:
            print(
                _json.dumps(
                    _timelines_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_timelines_report(report)
        return 0

    if args.kind == "attribute-dates":
        from . import audit_attribute_dates as add_mod

        # Flatten the repeatable + comma-separated --include / --exclude
        # forms into plain lists. argparse handles `--include a --include b`
        # via action='append'; we add comma-splitting on top so
        # `--include a,b` is equivalent. Why post-process here instead of
        # in argparse: type=callable would split a single value but lose
        # the per-flag composition with append.
        def _flatten_codes(values):
            out = []
            for v in values or []:
                out.extend(c.strip() for c in v.split(",") if c.strip())
            return out

        include_codes = _flatten_codes(args.include)
        exclude_codes = _flatten_codes(args.exclude)

        try:
            report = add_mod.audit_station_attribute_dates(
                client,
                name=args.name,
                id_entity=args.id_entity,
                subtypes=args.subtypes,
                include_mutable=args.include_mutable,
                include_codes=include_codes or None,
                exclude_codes=exclude_codes or None,
                catalog_path=args.catalog,
                suppressions_path=args.suppressions,
                use_suppressions=not args.no_suppressions,
            )
        except (LookupError, ValueError, FileNotFoundError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if report.included_codes_unmatched:
            print(
                "note: --include matched 0 attributes on this station for: "
                f"{', '.join(report.included_codes_unmatched)} "
                "(typo? wrong station? wrong subtype filter?)",
                file=sys.stderr,
            )
        if report.suppressions_errors:
            print(
                f"warning: {len(report.suppressions_errors)} malformed line(s) "
                f"in {report.suppressions_path}:",
                file=sys.stderr,
            )
            for err in report.suppressions_errors:
                print(
                    f"  line {err.line_no}: {err.message}",
                    file=sys.stderr,
                )
        if args.triage_path:
            audit_cmd = "tos audit " + " ".join(argv)
            content = add_mod.format_triage_file(report, audit_command=audit_cmd)
            args.triage_path.write_text(content, encoding="utf-8")
            print(
                f"wrote triage file: {args.triage_path} "
                f"({len(report.violations)} violation(s))",
                file=sys.stderr,
            )
        if args.json:
            print(
                _json.dumps(
                    _attribute_date_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_attribute_date_report(report, verbose=args.verbose)
        return 1 if report.has_violations else 0

    if args.kind == "missing-attributes":
        from . import audit_missing_attributes as ama_mod

        try:
            report = ama_mod.audit_station_missing_attributes(
                client,
                name=args.name,
                id_entity=args.id_entity,
                subtypes=args.subtypes,
                catalog_path=args.catalog,
                suppressions_path=args.suppressions,
                use_suppressions=not args.no_suppressions,
            )
        except (LookupError, ValueError, FileNotFoundError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if report.suppressions_errors:
            print(
                f"warning: {len(report.suppressions_errors)} malformed line(s) "
                f"in {report.suppressions_path}:",
                file=sys.stderr,
            )
            for err in report.suppressions_errors:
                print(f"  line {err.line_no}: {err.message}", file=sys.stderr)
        if args.triage_path:
            audit_cmd = "tos audit " + " ".join(argv) if argv else "tos audit"
            content = ama_mod.format_triage_file(report, audit_command=audit_cmd)
            args.triage_path.write_text(content, encoding="utf-8")
            print(
                f"wrote triage file: {args.triage_path} "
                f"({len(report.violations)} violation(s))",
                file=sys.stderr,
            )
        if args.json:
            print(
                _json.dumps(
                    _missing_attributes_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_missing_attributes_report(report, verbose=args.verbose)
        return 1 if report.has_violations else 0

    if args.kind == "visit-coverage":
        from . import audit_visit_coverage as avc_mod

        try:
            report = avc_mod.audit_station_visit_coverage(
                client,
                name=args.name,
                id_entity=args.id_entity,
                since=args.since,
                coverage_window_days=args.coverage_window_days,
                suppressions_path=args.suppressions,
                use_suppressions=not args.no_suppressions,
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if report.suppressions_errors:
            print(
                f"warning: {len(report.suppressions_errors)} malformed line(s) "
                f"in {report.suppressions_path}:",
                file=sys.stderr,
            )
            for err in report.suppressions_errors:
                print(f"  line {err.line_no}: {err.message}", file=sys.stderr)
        if args.triage_path:
            audit_cmd = "tos audit " + " ".join(argv) if argv else "tos audit"
            content = avc_mod.format_triage_file(report, audit_command=audit_cmd)
            args.triage_path.write_text(content, encoding="utf-8")
            print(
                f"wrote triage file: {args.triage_path} "
                f"({len(report.violations)} violation(s))",
                file=sys.stderr,
            )
        if args.json:
            print(
                _json.dumps(
                    _visit_coverage_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_visit_coverage_report(report, verbose=args.verbose)
        return 1 if report.has_violations else 0

    if args.kind == "contact-dates":
        from . import audit_contact_dates as acd_mod

        try:
            report = acd_mod.audit_station_contact_dates(
                client,
                name=args.name,
                id_entity=args.id_entity,
                suppressions_path=args.suppressions,
                use_suppressions=not args.no_suppressions,
            )
        except (LookupError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        if report.suppressions_errors:
            print(
                f"warning: {len(report.suppressions_errors)} malformed line(s) "
                f"in {report.suppressions_path}:",
                file=sys.stderr,
            )
            for err in report.suppressions_errors:
                print(f"  line {err.line_no}: {err.message}", file=sys.stderr)
        if args.triage_path:
            audit_cmd = "tos audit " + " ".join(argv) if argv else "tos audit"
            content = acd_mod.format_triage_file(report, audit_command=audit_cmd)
            args.triage_path.write_text(content, encoding="utf-8")
            print(
                f"wrote triage file: {args.triage_path} "
                f"({len(report.violations)} violation(s))",
                file=sys.stderr,
            )
        if args.json:
            print(
                _json.dumps(
                    _contact_dates_report_to_dict(report),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_contact_dates_report(report, verbose=args.verbose)
        return 1 if report.has_violations else 0

    if args.kind == "verify-from-rinex":
        return _audit_verify_from_rinex_main(args, client)

    p.error(f"unknown kind: {args.kind}")
    return 2


# ---------------------------------------------------------------------------
# Action-file parser + runner (operator-edited triage workflow)
# ---------------------------------------------------------------------------


@dataclass
class ParsedAction:
    """One action parsed from a triage file."""

    line_no: int
    id_entity: int
    verb: str
    args: List[str]
    raw: str


@dataclass
class ParseError:
    """One error encountered while parsing an action file."""

    line_no: int
    message: str
    raw: str


_SUPPORTED_VERBS = (
    "add-attribute",
    "add-visit",
    "assign-contact",
    "change-subtype",
    "create-join",
    "decommission",
    "defer",
    "delete-attribute-value",
    "delete-contact-relationship",
    "delete-join",
    "fill-gap",
    "move",
    "patch-attribute-date",
    "patch-attribute-value",
    "patch-contact-relationship",
    "patch-join-date",
)


def _parse_action_file(text: str) -> tuple[List[ParsedAction], List[ParseError]]:
    """Parse a triage action file into (actions, errors).

    Format: one ACTION per line. Comments (``#`` to end-of-line) and blank
    lines are ignored. Each non-blank line must match::

        ACTION <id_entity> <verb> [args...]

    Token splitting uses :func:`shlex.split` so values containing spaces
    can be quoted (e.g. ``add-attribute subtype 'GPS stöð' 2010-01-01``).
    For verbs that take bare tokens (patch-attribute-date, change-subtype,
    …), shlex.split behaves identically to ``str.split()``.

    Returns both lists so the runner can report every malformed line at
    once instead of bailing on the first error.
    """
    import shlex

    actions: List[ParsedAction] = []
    errors: List[ParseError] = []
    for i, line in enumerate(text.splitlines(), 1):
        raw = line
        # Strip comments and surrounding whitespace.
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            errors.append(
                ParseError(
                    line_no=i,
                    message=f"malformed quoting: {exc}",
                    raw=raw,
                )
            )
            continue
        if tokens[0] != "ACTION":
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "expected line to start with 'ACTION' " f"(got {tokens[0]!r})"
                    ),
                    raw=raw,
                )
            )
            continue
        if len(tokens) < 3:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "ACTION line needs at least: ACTION <id> <verb> "
                        f"(got {len(tokens)} tokens)"
                    ),
                    raw=raw,
                )
            )
            continue
        try:
            id_entity = int(tokens[1])
        except ValueError:
            errors.append(
                ParseError(
                    line_no=i,
                    message=f"id_entity must be int, got {tokens[1]!r}",
                    raw=raw,
                )
            )
            continue
        verb = tokens[2]
        if verb not in _SUPPORTED_VERBS:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        f"unknown verb {verb!r}; supported: "
                        f"{', '.join(_SUPPORTED_VERBS)}"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "change-subtype" and len(tokens) != 4:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "change-subtype requires exactly one argument: "
                        "the new subtype code"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "defer" and len(tokens) != 3:
            errors.append(
                ParseError(
                    line_no=i,
                    message="defer takes no arguments",
                    raw=raw,
                )
            )
            continue
        if verb == "decommission" and len(tokens) != 4:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "decommission requires exactly one argument: the "
                        "retirement date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "move" and len(tokens) != 5:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "move requires exactly two arguments: " "<to_parent_id> <date>"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "fill-gap" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "fill-gap requires exactly three arguments: "
                        "<parent_id> <date_from> <date_to>"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "patch-attribute-date" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "patch-attribute-date requires exactly three "
                        "arguments: <code> <old_date_from> <new_date_from>"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "add-attribute" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "add-attribute requires exactly three arguments: "
                        "<code> <value> <date_from> "
                        "(quote the value if it contains spaces, e.g. "
                        "'GPS stöð')"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "patch-attribute-value" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "patch-attribute-value requires exactly three "
                        "arguments: <code> <date_from_match> <new_value> "
                        "(quote the value if it contains spaces)"
                    ),
                    raw=raw,
                )
            )
            continue
        if verb == "patch-join-date" and len(tokens) != 6:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "patch-join-date requires exactly three arguments: "
                        "<id_connection> <field> <new_date> "
                        "(field is time_from or time_to)"
                    ),
                    raw=raw,
                )
            )
            continue
        # create-join accepts either 2 args (open join) or 3 args
        # (closed historical join). Token count: ACTION <id> <verb>
        # <parent_id> <date_from> [<date_to>] → 5 or 6 tokens.
        if verb == "create-join" and len(tokens) not in (5, 6):
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "create-join requires 2 or 3 arguments: "
                        "<parent_id> <date_from> [<date_to>] "
                        "(omit date_to for an open join; provide it for "
                        "a closed historical join — alt to fill-gap)"
                    ),
                    raw=raw,
                )
            )
            continue
        # delete-join takes exactly one arg (the connection id to drop).
        # Token count: ACTION <id> delete-join <id_connection> → 4 tokens.
        if verb == "delete-join" and len(tokens) != 4:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "delete-join requires exactly one argument: "
                        "<id_connection> (the join row to permanently "
                        "remove). Use only on known-bad rows — see verb "
                        "docstring."
                    ),
                    raw=raw,
                )
            )
            continue
        # delete-attribute-value: 4 tokens (ACTION <id> verb <id_av>).
        if verb == "delete-attribute-value" and len(tokens) != 4:
            errors.append(
                ParseError(
                    line_no=i,
                    message=(
                        "delete-attribute-value requires exactly one "
                        "argument: <id_attribute_value> (the row to "
                        "permanently remove). Destructive — use only "
                        "on known-bad rows (wrong-scope id_attribute "
                        "FKs, duplicates, orphans)."
                    ),
                    raw=raw,
                )
            )
            continue
        actions.append(
            ParsedAction(
                line_no=i,
                id_entity=id_entity,
                verb=verb,
                args=tokens[3:],
                raw=raw,
            )
        )
    return actions, errors


@dataclass
class ActionResult:
    """Outcome of one action execution."""

    action: ParsedAction
    status: str  # "ok" | "deferred" | "failed"
    detail: str


_DATE_TOKEN_NOW = "now"
_DATE_TOKEN_START = "start"


def _resolve_date_token(
    raw: str, id_entity: int, writer
) -> tuple[Optional[str], Optional[str]]:
    """Resolve `now` / `start` date tokens to ``YYYY-MM-DD``.

    Lets triage files reference dates symbolically — useful when the
    same date_from would be hand-copied across many ACTIONs (the
    station's earliest_known), or when the operator wants to stamp
    "today" without typing the calendar date.

    Returns ``(resolved_date, error)``. Exactly one is non-None:

    * On success: ``(yyyy-mm-dd, None)``.
    * On token-unresolvable: ``(None, "<reason>")`` so the caller
      returns a `failed` ActionResult with the message.
    * Strings that are NOT `now` / `start` pass through unchanged
      (``(raw, None)``) — non-token paths are unaffected.

    Token semantics
    ---------------
    * ``now`` — today's date in UTC. Always unambiguous.
    * ``start`` — the entity's ``earliest_known`` anchor (per the
      missing-attributes / attribute-dates audit convention): earliest
      non-2014-10-17 open attribute date_from, falling back to the
      open parent-join's time_from. Errors if neither resolves.
      Evaluated once per entity per apply run (cached on the writer)
      so the same `start` token resolves to the same value across
      multiple ACTIONs in one file, even when later ACTIONs mutate
      the underlying entity state.

    See also: memory ``project_layer6_followup_date_shortcuts``,
    ``project_2014_10_17_metadata_cleanup_artifacts``.
    """
    if raw == _DATE_TOKEN_NOW:
        import datetime as _dt

        return (
            _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
            None,
        )

    if raw == _DATE_TOKEN_START:
        if writer is None:
            return (None, "'start' token requires a live writer for entity lookup")
        try:
            resolved = writer._get_earliest_known(id_entity)
        except Exception as exc:  # noqa: BLE001
            return (None, f"'start' token lookup failed: {exc}")
        if resolved is None:
            return (
                None,
                (
                    f"'start' token can't resolve for id_entity={id_entity} — "
                    "entity has no non-cleanup-artifact open attribute "
                    "date_from and no open parent join time_from"
                ),
            )
        return (resolved, None)

    return (raw, None)


def _dispatch_decommission(
    writer, action: ParsedAction, *, open_joins_by_device: "Dict[int, Any]"
) -> ActionResult:
    """Retire a device — close its open join (if any) + transition status.

    Two writes per device:

    1. **Close the open join** with ``time_to=<retirement_date>`` via
       :func:`devices.close_join`. Skipped (with a noted "no open join
       — skip close" in the detail) when the device has no open join —
       that's a legitimate state for a device already lifted out of
       TOS.
    2. **Transition the ``status`` attribute** from its open value (most
       commonly ``virkt``) to ``óvirkt`` via
       :func:`devices.transition_attribute`. History-preserving.

    A partial failure (e.g. join closed OK, status PATCH 400s) still
    returns ``failed`` so the operator notices, but the closed-join
    side will already have happened on the server. The detail string
    enumerates both write outcomes so the operator can see exactly
    what state TOS is in.

    The :func:`devices.decommission_device` composite does the same
    workflow plus an internal history fetch and "close all open
    joins" loop; the apply path uses the pre-computed
    ``open_joins_by_device`` cache from
    :func:`_build_open_joins_lookup` to avoid a redundant GET, and
    fleet survey shows zero devices with multiple open parent joins
    so the "close all" semantics is moot in practice. Both paths now
    invoke the same :func:`devices.close_join` /
    :func:`devices.transition_attribute` sub-primitives.
    """
    from . import devices

    retirement_date_raw = action.args[0]

    # Resolve `now` / `start` tokens.
    resolved, err = _resolve_date_token(retirement_date_raw, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action, status="failed", detail=f"decommission: {err}"
        )
    retirement_date = resolved or retirement_date_raw

    open_join = open_joins_by_device.get(action.id_entity)

    join_detail = "no open join — skip close"
    if open_join is not None:
        try:
            devices.close_join(
                writer,
                id_connection=open_join.id_entity_connection,
                date_to=retirement_date,
            )
            join_detail = (
                f"PATCH /join/{open_join.id_entity_connection} "
                f"time_to={retirement_date} (parent={open_join.id_entity_parent})"
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                action=action,
                status="failed",
                detail=f"patch_entity_connection raised: {exc}",
            )

    try:
        status_resp = devices.transition_attribute(
            writer,
            device_id=action.id_entity,
            code="status",
            new_value="óvirkt",
            date=retirement_date,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"join: {join_detail}; status transition raised: {exc}",
        )

    closed_part = (
        "closed prior status period"
        if status_resp.get("closed") is not None
        else "no prior status — opened first óvirkt period"
    )
    return ActionResult(
        action=action,
        status="ok",
        detail=f"join: {join_detail}; status: {closed_part} + óvirkt from {retirement_date}",
    )


def _dispatch_move(
    writer, action: ParsedAction, *, open_joins_by_device: "Dict[int, Any]"
) -> ActionResult:
    """Relocate a device — close its open join and open a new one on the same date.

    Unlike :func:`_dispatch_decommission`, a missing open join is a
    hard failure here: there's nothing to close, so the move is
    ill-defined. Don't silently POST the new join on its own — the
    operator's input file is wrong.

    The dispatcher invokes ``close_join`` + ``open_join`` directly
    (rather than the bundled :func:`devices.move_device` composite)
    so a second-step failure surfaces the first step's success in
    the detail string. That lets the operator see exactly what state
    TOS is in if the new join POST fails: the old parent is closed,
    the device is parent-less, and the new join needs manual
    creation.
    """
    from . import devices

    to_parent_token, date = action.args[0], action.args[1]
    try:
        to_parent_id = int(to_parent_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(f"move requires integer to_parent_id, got {to_parent_token!r}"),
        )

    # Resolve `now` / `start` tokens.
    resolved, err = _resolve_date_token(date, action.id_entity, writer)
    if err is not None:
        return ActionResult(action=action, status="failed", detail=f"move: {err}")
    date = resolved or date

    open_join = open_joins_by_device.get(action.id_entity)
    if open_join is None:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"cannot move device {action.id_entity}: no open parent "
                "join to close"
            ),
        )

    try:
        devices.close_join(
            writer, id_connection=open_join.id_entity_connection, date_to=date
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"close_join raised: {exc}",
        )

    close_detail = (
        f"PATCH /join/{open_join.id_entity_connection} "
        f"time_to={date} (was parent={open_join.id_entity_parent})"
    )

    try:
        devices.open_join(
            writer,
            parent_id=to_parent_id,
            child_id=action.id_entity,
            date_from=date,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"close: {close_detail}; open_join raised: {exc} — "
                "device is parent-less, manual cleanup needed"
            ),
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"close: {close_detail}; "
            f"open: POST /join parent={to_parent_id} date_from={date}"
        ),
    )


def _dispatch_create_join(writer, action: ParsedAction) -> ActionResult:
    """Open a fresh parent→child join (or backfill a closed one).

    Action shapes:
      * ``ACTION <child_id> create-join <parent_id> <date_from>`` — open
        join (``time_to=None``). Use when the device needs to land at a
        new parent and there's no existing open join to ``move`` from
        (e.g. after manually closing the prior join, or for devices
        that come out of nowhere mid-reconstruction).
      * ``ACTION <child_id> create-join <parent_id> <date_from>
        <date_to>`` — closed historical join. Functionally equivalent
        to ``fill-gap`` but with consistent verb naming; prefer
        ``fill-gap`` when you specifically mean "backfill a gap".

    Pure single-write verb — no prerequisite reads, no open-joins
    cache. Dispatcher-safe and order-independent within an apply run.
    Dates pass through ``writer.create_entity_connection`` which
    normalises them via ``_tos_date`` (TOS rejects bare YYYY-MM-DD
    on the /joins endpoint).
    """
    parent_token = action.args[0]
    date_from = action.args[1]
    date_to = action.args[2] if len(action.args) >= 3 else None

    try:
        parent_id = int(parent_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(f"create-join requires integer parent_id, got {parent_token!r}"),
        )

    # Resolve `now` / `start` tokens on both date arguments.
    resolved, err = _resolve_date_token(date_from, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action, status="failed", detail=f"create-join: {err}"
        )
    date_from = resolved or date_from

    if date_to is not None:
        resolved, err = _resolve_date_token(date_to, action.id_entity, writer)
        if err is not None:
            return ActionResult(
                action=action, status="failed", detail=f"create-join: {err}"
            )
        date_to = resolved or date_to

    try:
        response = writer.create_entity_connection(
            id_parent=parent_id,
            id_child=action.id_entity,
            time_from=date_from,
            time_to=date_to,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"create_entity_connection raised: {exc}",
        )

    end = date_to if date_to is not None else "open"
    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"POST /joins parent={parent_id} child={action.id_entity} "
            f"{date_from} → {end} — {response!r}"
        ),
    )


def _dispatch_fill_gap(writer, action: ParsedAction) -> ActionResult:
    """Backfill a closed historical join for a known window.

    Pure single-write verb — no prerequisite reads. The action shape
    is ``ACTION <child_id> fill-gap <parent_id> <date_from>
    <date_to>``. Surfaces the writer's response or exception verbatim
    in the detail string.
    """
    from . import devices

    parent_token, date_from, date_to = action.args[0], action.args[1], action.args[2]
    try:
        parent_id = int(parent_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"fill-gap requires integer parent_id, got {parent_token!r}",
        )

    # Resolve `now` / `start` tokens on both date arguments.
    for label, raw in (("date_from", date_from), ("date_to", date_to)):
        resolved, err = _resolve_date_token(raw, action.id_entity, writer)
        if err is not None:
            return ActionResult(
                action=action,
                status="failed",
                detail=f"fill-gap ({label}): {err}",
            )
        if label == "date_from":
            date_from = resolved or date_from
        else:
            date_to = resolved or date_to

    try:
        devices.fill_join_gap(
            writer,
            parent_id=parent_id,
            child_id=action.id_entity,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"fill_join_gap raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"POST /join parent={parent_id} child={action.id_entity} "
            f"{date_from} → {date_to}"
        ),
    )


def _dispatch_patch_attribute_date(writer, action: ParsedAction) -> ActionResult:
    """Re-date an existing TOS attribute period in-place.

    Action shape: ``ACTION <id_entity> patch-attribute-date <code>
    <old_date_from> <new_date_from>``. Looks up the attribute period
    via fresh writer.get_attribute_values (so we never operate on a
    stale id_attribute_value), then PATCHes ``date_from``.

    Match rule
    ----------
    A period matches when its ``date_from`` *date-only* prefix
    (``YYYY-MM-DD``) equals ``old_date_from``. The same normalisation
    applied at audit / suppression time — without it, ``"2014-10-17"
    != "2014-10-17 00:00:00"`` lexically and the dispatcher would
    silently no-op against live TOS.

    Failure modes
    -------------
    * **Zero matches** — the audit's old_date_from doesn't appear on
      the device. Returns ``failed``; the operator should re-audit
      and regenerate the triage file.
    * **Multiple matches** — two or more periods for the same code
      share the same date-only ``date_from``. Refuse to PATCH rather
      than pick arbitrarily (silent corruption is the failure mode
      we're guarding against). Operator must disambiguate manually.
    * **Period has no ``id_attribute_value``** — partial TOS payload.
      Returns ``failed``; rerun against a fresh history.
    """
    code = action.args[0]
    old_date_raw = action.args[1]
    new_date_raw = action.args[2]

    # Resolve `now` / `start` on the NEW date argument. (The OLD date
    # is a match-anchor against existing TOS data; it makes no sense to
    # symbolise it, so we leave it literal.)
    resolved_new, err = _resolve_date_token(new_date_raw, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch-attribute-date: {err}",
        )
    new_date_raw = resolved_new or new_date_raw

    # Normalise both date arguments to YYYY-MM-DD up front. Matches the
    # audit-time _date_only() contract and keeps the comparison robust
    # to operator-pasted datetimes.
    old_date = old_date_raw[:10]
    new_date = new_date_raw[:10]
    if len(new_date) != 10 or new_date[4] != "-" or new_date[7] != "-":
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: new_date_from must be YYYY-MM-DD "
                f"(got {new_date_raw!r})"
            ),
        )

    try:
        attrs = writer.get_attribute_values(action.id_entity, code)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"get_attribute_values raised: {exc}",
        )

    matches: List[Dict[str, Any]] = []
    for a in attrs:
        df = a.get("date_from")
        if not df:
            continue
        if str(df)[:10] == old_date:
            matches.append(a)

    if not matches:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: no period found for "
                f"id_entity={action.id_entity} code={code!r} "
                f"date_from={old_date} (re-audit and regenerate triage)"
            ),
        )
    if len(matches) > 1:
        ids = ", ".join(str(a.get("id_attribute_value")) for a in matches)
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: {len(matches)} periods match "
                f"id_entity={action.id_entity} code={code!r} "
                f"date_from={old_date} (id_attribute_value: {ids}); "
                "refusing to PATCH ambiguously — disambiguate manually"
            ),
        )

    target = matches[0]
    id_av_raw = target.get("id_attribute_value")
    if id_av_raw is None:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "patch-attribute-date: matching period has no "
                "id_attribute_value (partial payload); rerun later"
            ),
        )

    try:
        id_av = int(id_av_raw)
    except (TypeError, ValueError):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-date: id_attribute_value={id_av_raw!r} "
                "is not an integer (unexpected TOS payload shape)"
            ),
        )

    try:
        response = writer.patch_attribute_value(id_av, date_from=new_date)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch_attribute_value raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"PATCH /attribute_value/{id_av} "
            f"date_from {old_date} → {new_date} "
            f"(code={code!r}) — {response!r}"
        ),
    )


def _dispatch_add_attribute(writer, action: ParsedAction) -> ActionResult:
    """Add a new attribute period to an existing entity.

    Action shape: ``ACTION <id_entity> add-attribute <code> <value>
    <date_from>``. Fires the missing-attributes audit's ``add-attribute``
    verb — used to fill the gap when an entity is required to carry a
    code but has no open period for it.

    Pre-flight checks before POSTing
    --------------------------------
    * **Placeholder rejection** — if ``value`` or ``date_from`` still
      contains a ``<FILL_*>`` placeholder, the operator forgot to
      replace it. Refuse rather than POST a literal placeholder string
      to TOS.
    * **Date format** — ``date_from`` must be ``YYYY-MM-DD``.
    * **Conflict detection** — fetches existing periods via
      :meth:`writer.get_attribute_values`; if an open period already
      exists with the same value, the action is a no-op (idempotent).
      If an open period exists with a *different* value, refuse —
      silent overwrite is the failure mode we're explicitly guarding
      against; the operator should use a transition verb if they want
      history-preserving update.
    * **Multiple open periods** — refuse; the entity is already in a
      corrupt state and ``add-attribute`` would compound it.

    Otherwise calls :meth:`writer.add_attribute_value` to POST a new
    period with ``date_to=None``. The writer's ``dry_run`` flag
    controls whether anything actually goes over the wire.
    """
    code = action.args[0]
    value = action.args[1]
    date_from_raw = action.args[2]

    # Placeholder rejection — anything matching <...> shape is a
    # triage marker the operator forgot to fill in. Cheaper to refuse
    # here than to debug a literal "<FILL_VALUE>" string sitting in TOS.
    if value.startswith("<") and value.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-attribute: value placeholder {value!r} not replaced — "
                "fill in the value before applying"
            ),
        )
    if date_from_raw.startswith("<") and date_from_raw.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-attribute: date placeholder {date_from_raw!r} not "
                "replaced — fill in the date_from before applying"
            ),
        )

    # Resolve `now` / `start` tokens before the format check.
    resolved, err = _resolve_date_token(date_from_raw, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"add-attribute: {err}",
        )
    date_from_raw = resolved or date_from_raw

    # Date format check — same YYYY-MM-DD contract as patch-attribute-date.
    date_from = date_from_raw[:10]
    if len(date_from) != 10 or date_from[4] != "-" or date_from[7] != "-":
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-attribute: date_from must be YYYY-MM-DD "
                f"(got {date_from_raw!r})"
            ),
        )

    try:
        existing = writer.get_attribute_values(action.id_entity, code)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"get_attribute_values raised: {exc}",
        )

    open_values = [a for a in existing if a.get("date_to") is None]
    if len(open_values) > 1:
        ids = ", ".join(str(a.get("id_attribute_value")) for a in open_values)
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-attribute: {len(open_values)} open periods already "
                f"exist for code {code!r} on id_entity={action.id_entity} "
                f"(ids: {ids}) — refusing to add; clean up the existing "
                "duplicates first"
            ),
        )
    if len(open_values) == 1:
        current = open_values[0]
        current_value = current.get("value")
        if str(current_value) == value:
            return ActionResult(
                action=action,
                status="ok",
                detail=(
                    f"already present: open period for {code!r} on "
                    f"id_entity={action.id_entity} already has value "
                    f"{value!r} (no-op)"
                ),
            )
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-attribute: open period for {code!r} on "
                f"id_entity={action.id_entity} already has value "
                f"{current_value!r}; refuse to overwrite with {value!r}. "
                "Use a transition verb (history-preserving) instead."
            ),
        )

    try:
        response = writer.add_attribute_value(action.id_entity, code, value, date_from)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"add_attribute_value raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"POST /attribute_values id_entity={action.id_entity} "
            f"code={code!r} value={value!r} date_from={date_from} "
            f"— {response!r}"
        ),
    )


def _dispatch_add_visit(writer, action: ParsedAction) -> ActionResult:
    """Create a vitjun on an entity from a triage ACTION line.

    Action shape: ``ACTION <id_entity> add-visit <reasons_csv> <date>
    <work_text> [open|closed]``.

    Lifecycle-tracker use case: operators chain ACTION lines next to
    ``move`` / ``decommission`` / ``add-attribute`` to leave an audit
    trail of physical interventions ("sent for repair: cable damage",
    "back from vendor; firmware updated"). See CLAUDE.md "Device
    lifecycle tracker" section.

    Arguments
    ---------
    reasons_csv
        Comma-separated reason codes (e.g. ``repairs`` or
        ``repairs,change``). Each code is validated against
        :data:`MAINTENANCE_REASON_CODES` BEFORE the writer call —
        unknown codes return a ``failed`` ActionResult so the runner
        keeps going.
    date
        ISO date (``YYYY-MM-DD``) or token (``now`` / ``start``).
        Used as both ``start_time`` and ``end_time`` (instantaneous
        visit — matches the writer default).
    work_text
        Free-text "Framkvæmt" / "Vinna" description (shlex-quoted in
        the triage file so it can contain spaces).
    open|closed (optional)
        Default ``closed`` (``completed=True``). ``open`` marks the
        visit as in-progress (``completed=False``) — use for "sent for
        repair" entries that close later with another ``add-visit``
        line for "back from repair".

    Defaults: ``maintenance_type=on_site``, ``participants=""``,
    ``comment=None``, ``remaining=None``. Operators who need to set
    these directly use ``tos visit add --no-dry-run``. Auto-filling
    participants from the ``[tos] username`` is a Phase C.5 follow-up.

    Pre-flight checks
    -----------------
    * ``<FILL_*>`` placeholders in any positional → refuse (operator
      didn't fill the template).
    * Unknown reason code → refuse without calling the writer.
    * Date token resolution failure → refuse.
    * Unknown 4th positional → refuse (must be ``open`` or ``closed``).
    """
    if len(action.args) < 3 or len(action.args) > 4:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "add-visit: expected 3 or 4 positional args "
                "(<reasons_csv> <date> <work_text> [open|closed]), "
                f"got {len(action.args)}"
            ),
        )
    reasons_raw = action.args[0]
    date_raw = action.args[1]
    work_text = action.args[2]
    status_raw = action.args[3] if len(action.args) >= 4 else "closed"

    # Placeholder rejection — same convention as _dispatch_add_attribute.
    # Refusing here is cheaper than POSTing a literal "<FILL_VALUE>" to TOS.
    for label, val in (
        ("reasons", reasons_raw),
        ("date", date_raw),
        ("work", work_text),
    ):
        if val.startswith("<") and val.endswith(">"):
            return ActionResult(
                action=action,
                status="failed",
                detail=(
                    f"add-visit: {label} placeholder {val!r} not replaced "
                    "— fill in the value before applying"
                ),
            )

    # Reason validation BEFORE writer call. The writer also validates
    # but surfaces ValueError; doing it here returns a clean failed
    # ActionResult and avoids the noisy stack trace path.
    reasons = [c.strip() for c in reasons_raw.split(",") if c.strip()]
    if not reasons:
        return ActionResult(
            action=action,
            status="failed",
            detail="add-visit: reasons_csv must contain at least one code",
        )
    unknown = [c for c in reasons if c not in MAINTENANCE_REASON_CODES]
    if unknown:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-visit: unknown reason code(s) {unknown!r} — allowed: "
                f"{sorted(MAINTENANCE_REASON_CODES)}"
            ),
        )

    # 4th positional: open/closed → completed boolean.
    status_norm = status_raw.lower()
    if status_norm not in ("open", "closed"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"add-visit: 4th positional must be 'open' or 'closed' "
                f"(got {status_raw!r})"
            ),
        )
    completed = status_norm == "closed"

    # Resolve `now` / `start` tokens.
    resolved, err = _resolve_date_token(date_raw, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"add-visit: {err}",
        )
    date_resolved = resolved or date_raw

    # Date format check — same YYYY-MM-DD contract as add-attribute.
    date_yyyy_mm_dd = date_resolved[:10]
    if (
        len(date_yyyy_mm_dd) != 10
        or date_yyyy_mm_dd[4] != "-"
        or date_yyyy_mm_dd[7] != "-"
    ):
        return ActionResult(
            action=action,
            status="failed",
            detail=(f"add-visit: date must be YYYY-MM-DD " f"(got {date_resolved!r})"),
        )

    try:
        response = writer.add_maintenance_visit(
            action.id_entity,
            start_time=date_yyyy_mm_dd,
            end_time=None,  # writer defaults to start_time
            maintenance_type="on_site",
            participants="",
            reasons=reasons,
            work=work_text,
            comment=None,
            remaining=None,
            completed=completed,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"add_maintenance_visit raised: {exc}",
        )

    new_id = response.get("id_maintenance") if isinstance(response, dict) else None
    state_label = "completed" if completed else "open"
    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"POST /maintenances/id_entity/{action.id_entity} "
            f"reasons={reasons!r} date={date_yyyy_mm_dd} {state_label} "
            f"— id_maintenance={new_id!r}"
        ),
    )


def _dispatch_patch_attribute_value(writer, action: ParsedAction) -> ActionResult:
    """Correct a wrong attribute value in-place (Pattern 1 / Pattern 4).

    Action shape: ``ACTION <id_entity> patch-attribute-value <code>
    <date_from_match> <new_value>``. Same date-prefix lookup as
    :func:`_dispatch_patch_attribute_date` (match by ``YYYY-MM-DD``
    prefix, refuse on 0 or >1 matches), but PATCHes the ``value`` field
    instead of ``date_from``.

    Use case
    --------
    TOS holds a wrong value for a known time period — e.g. a serial
    recorded as ``"UNKNOWN"`` that the reference source says is
    ``"3163"``, or a misspelled station name. The time period itself is
    correct; only the stored value is wrong. PATCH overwrites in place
    (history-destructive); use a transition verb instead if the value
    actually changed at a date and the old value should be preserved.

    Pre-flight checks
    -----------------
    * **Placeholder rejection** — refuses literal ``<FILL_*>`` markers.
    * **Date format** — ``date_from_match`` must look like YYYY-MM-DD.
    * **Idempotence** — if the matched period already holds the requested
      value, returns ``status="ok"`` with "already present" detail and
      skips the PATCH.
    """
    code = action.args[0]
    old_date_raw = action.args[1]
    new_value = action.args[2]

    # Placeholder rejection — same contract as add-attribute.
    if new_value.startswith("<") and new_value.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-value: value placeholder {new_value!r} not "
                "replaced — fill in the value before applying"
            ),
        )
    if old_date_raw.startswith("<") and old_date_raw.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-value: date placeholder {old_date_raw!r} not "
                "replaced — fill in the date_from_match before applying"
            ),
        )

    old_date = old_date_raw[:10]
    if len(old_date) != 10 or old_date[4] != "-" or old_date[7] != "-":
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-value: date_from_match must be YYYY-MM-DD "
                f"(got {old_date_raw!r})"
            ),
        )

    try:
        attrs = writer.get_attribute_values(action.id_entity, code)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"get_attribute_values raised: {exc}",
        )

    matches: List[Dict[str, Any]] = []
    for a in attrs:
        df = a.get("date_from")
        if not df:
            continue
        if str(df)[:10] == old_date:
            matches.append(a)

    if not matches:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-value: no period found for "
                f"id_entity={action.id_entity} code={code!r} "
                f"date_from={old_date} (re-audit and regenerate triage)"
            ),
        )
    if len(matches) > 1:
        ids = ", ".join(str(a.get("id_attribute_value")) for a in matches)
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-value: {len(matches)} periods match "
                f"id_entity={action.id_entity} code={code!r} "
                f"date_from={old_date} (id_attribute_value: {ids}); "
                "refusing to PATCH ambiguously — disambiguate manually"
            ),
        )

    target = matches[0]
    current_value = target.get("value")
    if str(current_value) == new_value:
        return ActionResult(
            action=action,
            status="ok",
            detail=(
                f"already present: period for {code!r} on "
                f"id_entity={action.id_entity} date_from={old_date} already "
                f"has value {new_value!r} (no-op)"
            ),
        )

    id_av_raw = target.get("id_attribute_value")
    if id_av_raw is None:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "patch-attribute-value: matching period has no "
                "id_attribute_value (partial payload); rerun later"
            ),
        )

    try:
        id_av = int(id_av_raw)
    except (TypeError, ValueError):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-attribute-value: id_attribute_value={id_av_raw!r} "
                "is not an integer (unexpected TOS payload shape)"
            ),
        )

    try:
        response = writer.patch_attribute_value(id_av, value=new_value)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch_attribute_value raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"PATCH /attribute_value/{id_av} "
            f"value {current_value!r} → {new_value!r} "
            f"(code={code!r} date_from={old_date}) — {response!r}"
        ),
    )


_PATCH_JOIN_DATE_FIELDS = ("time_from", "time_to")


def _dispatch_patch_join_date(writer, action: ParsedAction) -> ActionResult:
    """PATCH a single date field on an existing join row.

    Action shape: ``ACTION <id_device> patch-join-date <id_connection>
    <field> <new_date>`` where ``field ∈ {time_from, time_to}``. Used
    for missing-join backfills (extend ``time_from`` back to the real
    deployment date) and historical join close-out corrections.

    Field whitelist
    ---------------
    Only ``time_from`` and ``time_to`` are accepted. The underlying
    :meth:`TOSWriter.patch_entity_connection` will happily PATCH
    ``id_entity_parent`` / ``id_entity_child`` too, but that is the
    semantics of ``move`` (close+open) — refuse it here so this verb
    can't be misused as a backdoor reparent.

    Trust model
    -----------
    ``id_connection`` is trusted without verifying that it belongs to
    ``id_entity``. Same precedent as :func:`_dispatch_fill_gap`. The
    pre-flight table + dry-run review is the safety net.
    """
    connection_token, field, new_date = (
        action.args[0],
        action.args[1],
        action.args[2],
    )

    try:
        id_connection = int(connection_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-join-date requires integer id_connection, got "
                f"{connection_token!r}"
            ),
        )

    if field not in _PATCH_JOIN_DATE_FIELDS:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-join-date: field must be one of "
                f"{', '.join(_PATCH_JOIN_DATE_FIELDS)} (got {field!r}). "
                "Use the move verb to reparent."
            ),
        )

    if new_date.startswith("<") and new_date.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-join-date: date placeholder {new_date!r} not "
                "replaced — fill in the new date before applying"
            ),
        )

    # Resolve `now` / `start` tokens.
    resolved_new, err = _resolve_date_token(new_date, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch-join-date: {err}",
        )
    new_date = resolved_new or new_date

    date_prefix = new_date[:10]
    if len(date_prefix) != 10 or date_prefix[4] != "-" or date_prefix[7] != "-":
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-join-date: new_date must be YYYY-MM-DD " f"(got {new_date!r})"
            ),
        )

    try:
        response = writer.patch_entity_connection(id_connection, **{field: new_date})
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch_entity_connection raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"PATCH /join/{id_connection} {field}={new_date} "
            f"(device={action.id_entity}) — {response!r}"
        ),
    )


#: Editable fields on a contact↔entity relationship via
#: ``patch-contact-relationship``. ``role`` is a string; the two date
#: fields go through the date-token resolver + YYYY-MM-DD check.
_PATCH_CONTACT_FIELDS = ("time_from", "time_to", "role")


def _dispatch_patch_contact_relationship(writer, action: ParsedAction) -> ActionResult:
    """Correct a contact↔entity relationship's period or role.

    Action shape: ``ACTION <id_entity> patch-contact-relationship
    <id_relationship> <field> <value>`` where
    ``field ∈ {time_from, time_to, role}``.

    Primary use case: backdate a ``time_from`` that is a TOS-migration
    artifact (the contact↔station relationship row was created when the
    contact was loaded into the new TOS, not when the contact actually
    started owning the station). See
    ``docs/architecture/contact-write-api.md``.

    The ``id_entity`` slot is the STATION the contact is mapped to —
    same convention as ``patch-join-date`` (device in the id slot,
    connection id in args[0]). Keeping the station there means the
    ``start`` date-token resolves against the station's earliest_known,
    which is exactly the anchor you want when backdating a migration
    date to founding.

    For ``field=role`` the value is a bare string (``owner`` /
    ``operator`` / ...) — no date resolution. For the date fields,
    ``now`` / ``start`` tokens resolve and the result is format-checked.
    """
    if len(action.args) != 3:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "patch-contact-relationship: expected 3 args "
                "(<id_relationship> <field> <value>), got "
                f"{len(action.args)}"
            ),
        )
    rel_token, field, value = action.args[0], action.args[1], action.args[2]

    try:
        id_relationship = int(rel_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "patch-contact-relationship requires integer "
                f"id_relationship, got {rel_token!r}"
            ),
        )

    if field not in _PATCH_CONTACT_FIELDS:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "patch-contact-relationship: field must be one of "
                f"{', '.join(_PATCH_CONTACT_FIELDS)} (got {field!r})"
            ),
        )

    if value.startswith("<") and value.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"patch-contact-relationship: value placeholder {value!r} "
                "not replaced — fill it in before applying"
            ),
        )

    kwargs: Dict[str, Any] = {}
    if field == "role":
        kwargs["role"] = value
    else:
        # Date field — resolve now/start, then YYYY-MM-DD check.
        resolved, err = _resolve_date_token(value, action.id_entity, writer)
        if err is not None:
            return ActionResult(
                action=action,
                status="failed",
                detail=f"patch-contact-relationship: {err}",
            )
        value = resolved or value
        date_prefix = value[:10]
        if len(date_prefix) != 10 or date_prefix[4] != "-" or date_prefix[7] != "-":
            return ActionResult(
                action=action,
                status="failed",
                detail=(
                    "patch-contact-relationship: date must be YYYY-MM-DD "
                    f"(got {value!r})"
                ),
            )
        kwargs[field] = value

    try:
        response = writer.patch_contact_relationship(id_relationship, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"patch_contact_relationship raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"PUT /admin_contact_entity_relationship_row/{id_relationship} "
            f"{field}={value} (station={action.id_entity}) — {response!r}"
        ),
    )


def _dispatch_assign_contact(writer, action: ParsedAction) -> ActionResult:
    """Assign a contact to a station/device (open a new relationship).

    Action shape: ``ACTION <id_entity> assign-contact <id_contact>
    <role> <time_from>``. The ``id_entity`` is the station/device the
    contact is mapped to; ``id_contact`` is the contact entity (e.g.
    1256 = Veðurstofa).

    ``time_from`` accepts ``now`` / ``start`` tokens (``start`` =
    the station's earliest_known).
    """
    if len(action.args) != 3:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "assign-contact: expected 3 args (<id_contact> <role> "
                f"<time_from>), got {len(action.args)}"
            ),
        )
    contact_token, role, time_from = (
        action.args[0],
        action.args[1],
        action.args[2],
    )

    try:
        id_contact = int(contact_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"assign-contact requires integer id_contact, got {contact_token!r}",
        )

    if role.startswith("<") and role.endswith(">"):
        return ActionResult(
            action=action,
            status="failed",
            detail=f"assign-contact: role placeholder {role!r} not replaced",
        )

    resolved, err = _resolve_date_token(time_from, action.id_entity, writer)
    if err is not None:
        return ActionResult(
            action=action,
            status="failed",
            detail=f"assign-contact: {err}",
        )
    time_from = resolved or time_from
    date_prefix = time_from[:10]
    if len(date_prefix) != 10 or date_prefix[4] != "-" or date_prefix[7] != "-":
        return ActionResult(
            action=action,
            status="failed",
            detail=f"assign-contact: time_from must be YYYY-MM-DD (got {time_from!r})",
        )

    try:
        response = writer.create_contact_relationship(
            id_contact, action.id_entity, role, time_from
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"create_contact_relationship raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"POST /contact_joins id_contact={id_contact} "
            f"id_entity={action.id_entity} role={role!r} "
            f"time_from={time_from} — {response!r}"
        ),
    )


def _dispatch_delete_contact_relationship(writer, action: ParsedAction) -> ActionResult:
    """Permanently DELETE a contact↔entity relationship row.

    Action shape: ``ACTION <id_entity> delete-contact-relationship
    <id_relationship>``.

    Destructive — erases history. To END a genuinely-valid relationship
    prefer ``patch-contact-relationship <id> time_to <date>`` (preserves
    history). Use delete only for a wrong mapping / duplicate.

    Trust model: ``id_relationship`` is trusted without verifying it
    belongs to ``id_entity`` (same precedent as
    :func:`_dispatch_delete_join`). Pre-flight + dry-run is the safety net.
    """
    if len(action.args) != 1:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "delete-contact-relationship: expected 1 arg "
                f"(<id_relationship>), got {len(action.args)}"
            ),
        )
    (rel_token,) = (action.args[0],)
    try:
        id_relationship = int(rel_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                "delete-contact-relationship requires integer "
                f"id_relationship, got {rel_token!r}"
            ),
        )

    try:
        response = writer.delete_contact_relationship(id_relationship)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"delete_contact_relationship raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"DELETE /admin_contact_entity_relationship_row/{id_relationship} "
            f"(station={action.id_entity}) — {response!r}"
        ),
    )


def _dispatch_delete_attribute_value(writer, action: ParsedAction) -> ActionResult:
    """Permanently DELETE an attribute_value row from TOS.

    Action shape: ``ACTION <id_entity> delete-attribute-value
    <id_attribute_value>``.

    Destructive — erases history. Use only on known-bad rows such as
    wrong-scope id_attribute FKs (the resolver bug fixed 2026-05-25
    sent some monument attributes to station-scoped schema rows;
    cleaning them up requires DELETE + re-write via the fixed
    resolver).

    Trust model: ``id_attribute_value`` is trusted without verifying
    it belongs to ``id_entity`` (same precedent as
    :func:`_dispatch_delete_join`). Pre-flight + dry-run is the
    safety net.
    """
    (av_token,) = (action.args[0],)

    try:
        id_av = int(av_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"delete-attribute-value requires integer "
                f"id_attribute_value, got {av_token!r}"
            ),
        )

    try:
        response = writer.delete_attribute_value(id_av)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"delete_attribute_value raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"DELETE /attribute_value/{id_av} "
            f"(device={action.id_entity}) — {response!r}"
        ),
    )


def _dispatch_delete_join(writer, action: ParsedAction) -> ActionResult:
    """Permanently DELETE a join row from TOS.

    Action shape: ``ACTION <id_entity> delete-join <id_connection>``.

    Destructive — erases history. Use only on known-bad rows such as
    SOPAC-convention split-monument workaround entities whose historical
    joins never represented a real physical install (e.g. SAVI monument
    5244 → join 6429). The default close-out workflow PATCHes time_to;
    deletion removes the row entirely so it stops appearing in
    ``device list --all`` and parent_history walks.

    Trust model: ``id_connection`` is trusted without verifying it
    belongs to ``id_entity`` (same precedent as patch-join-date /
    fill-gap). The pre-flight table + dry-run review is the safety
    net. The verb is admin-level; non-admin tokens will get a 403 from
    TOS and the dispatcher will surface that as ``failed``.
    """
    (connection_token,) = (action.args[0],)

    try:
        id_connection = int(connection_token)
    except ValueError:
        return ActionResult(
            action=action,
            status="failed",
            detail=(
                f"delete-join requires integer id_connection, got "
                f"{connection_token!r}"
            ),
        )

    try:
        response = writer.delete_entity_connection(id_connection)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action=action,
            status="failed",
            detail=f"delete_entity_connection raised: {exc}",
        )

    return ActionResult(
        action=action,
        status="ok",
        detail=(
            f"DELETE /join/{id_connection} "
            f"(device={action.id_entity}) — {response!r}"
        ),
    )


def _dispatch_action(
    writer,
    action: ParsedAction,
    *,
    subtype_id_by_code: "Dict[str, int] | None" = None,
    open_joins_by_device: "Dict[int, Any] | None" = None,
) -> ActionResult:
    """Apply one parsed action through ``writer``. Never raises.

    The writer's ``dry_run`` flag controls whether anything is sent over
    the wire — this function only knows about TOS-level semantics.

    ``subtype_id_by_code`` maps canonical subtype strings (``digitizer``,
    ``gps_clock``, ...) to the integer FK TOS uses on the admin write
    path. Built once per apply run by :func:`_apply_main` from
    ``GET /entity_subtypes/``. Required for the ``change-subtype`` verb;
    unused otherwise.

    ``open_joins_by_device`` maps device id_entity → its current open
    :class:`tostools.history.Join` (or None if no open join). Built once
    per apply run from the join index. Required for ``decommission``;
    unused otherwise.
    """
    if action.verb == "defer":
        return ActionResult(action=action, status="deferred", detail="defer (no-op)")
    if action.verb == "decommission":
        return _dispatch_decommission(
            writer, action, open_joins_by_device=open_joins_by_device or {}
        )
    if action.verb == "move":
        return _dispatch_move(
            writer, action, open_joins_by_device=open_joins_by_device or {}
        )
    if action.verb == "create-join":
        return _dispatch_create_join(writer, action)
    if action.verb == "fill-gap":
        return _dispatch_fill_gap(writer, action)
    if action.verb == "patch-attribute-date":
        return _dispatch_patch_attribute_date(writer, action)
    if action.verb == "patch-attribute-value":
        return _dispatch_patch_attribute_value(writer, action)
    if action.verb == "patch-join-date":
        return _dispatch_patch_join_date(writer, action)
    if action.verb == "delete-attribute-value":
        return _dispatch_delete_attribute_value(writer, action)
    if action.verb == "delete-join":
        return _dispatch_delete_join(writer, action)
    if action.verb == "add-attribute":
        return _dispatch_add_attribute(writer, action)
    if action.verb == "add-visit":
        return _dispatch_add_visit(writer, action)
    if action.verb == "patch-contact-relationship":
        return _dispatch_patch_contact_relationship(writer, action)
    if action.verb == "assign-contact":
        return _dispatch_assign_contact(writer, action)
    if action.verb == "delete-contact-relationship":
        return _dispatch_delete_contact_relationship(writer, action)
    if action.verb == "change-subtype":
        code = action.args[0]
        mapping = subtype_id_by_code or {}
        sid = mapping.get(code)
        if sid is None:
            return ActionResult(
                action=action,
                status="failed",
                detail=(
                    f"unknown subtype code {code!r} — not in TOS's "
                    "/entity_subtypes/ list. Check spelling against the "
                    "vault reference note."
                ),
            )
        try:
            response = writer.update_entity_subtype(action.id_entity, sid)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                action=action,
                status="failed",
                detail=f"update_entity_subtype raised: {exc}",
            )
        return ActionResult(
            action=action,
            status="ok",
            detail=(
                f"PUT /admin_entity_row/{action.id_entity} "
                f"id_entity_subtype={sid} ({code!r}) — {response!r}"
            ),
        )
    # Unreachable — parser rejects unknown verbs.
    return ActionResult(
        action=action, status="failed", detail=f"unimplemented verb {action.verb!r}"
    )


def _build_open_joins_lookup(client, *, target_ids):
    """Build ``{device_id: open_join_or_None}`` via the global join index.

    The ~110s cost is the marker-resolution + parent walk inside
    :func:`tostools.history.build_join_index`. For the apply workflow
    this is the price of a decommission action — we need to know *which
    join* to close (it's keyed by ``id_entity_connection``, which only
    the parent's ``children_connections`` carries).

    Only includes devices in ``target_ids``; the rest of the index
    contents are discarded. The returned dict has one entry per target
    id; the value is the device's currently-open :class:`Join` or
    ``None`` if the device has no open join (already orphan).
    """
    from .history import build_join_index

    progress_to_stderr = sys.stderr.isatty()
    if progress_to_stderr:
        sys.stderr.write(
            "Building join index to locate open joins for decommission "
            "(~110s on the IMO fleet)...\n"
        )
        sys.stderr.flush()
    index = build_join_index(client)
    out: Dict[int, Any] = {}
    for did in target_ids:
        timeline = index.timeline(int(did))
        open_joins = timeline.open_joins
        out[int(did)] = open_joins[0] if open_joins else None
    return out


def _fetch_subtype_id_by_code(client) -> "Dict[str, int]":
    """Return a ``{code_entity_subtype: id_entity_subtype}`` mapping from TOS.

    Calls ``GET /entity_subtypes/`` once and folds the result. Needed by
    the ``change-subtype`` action verb: the operator types the canonical
    string code (e.g. ``digitizer``) but TOS's admin write path keys on
    the integer FK (``id``). See vault note
    `1778677922-tos-entity-subtype-codes` for the human-readable
    Icelandic ↔ code reference. Returns an empty dict if the endpoint
    is unreachable; callers should report the failure rather than
    silently writing wrong ids.
    """
    try:
        rows = client._make_request("/entity_subtypes/")
    except Exception:  # noqa: BLE001
        rows = None
    out: Dict[str, int] = {}
    for r in rows or []:
        code = r.get("code")
        sid = r.get("id")
        if code and isinstance(sid, int):
            out[code] = sid
    return out


def _fetch_action_meta(client, ids):
    """Return ``{id: {"subtype", "serial", "model"}}`` for each unique id.

    One :meth:`TOSClient.get_entity_history` call per unique id; cached
    in a local dict so repeats are free. A missing/unreadable entity
    yields a dict of ``None`` fields rather than raising — the apply
    runner uses ``?`` placeholders in that case and lets the writer
    decide whether the id is actually valid.

    Reuses the open-period attribute reader from :mod:`tostools.history`
    so we don't duplicate the ``date_to is None`` filtering logic.
    """
    from .history import _open_attribute_value

    meta_by_id: Dict[int, Dict[str, Any]] = {}
    for raw in ids:
        did = int(raw)
        if did in meta_by_id:
            continue
        entry: Dict[str, Any] = {"subtype": None, "serial": None, "model": None}
        try:
            history = client.get_entity_history(did)
        except Exception:  # noqa: BLE001
            history = None
        if history:
            entry["subtype"] = history.get("code_entity_subtype") or None
            attrs = history.get("attributes") or []
            entry["serial"] = _open_attribute_value(attrs, "serial_number")
            entry["model"] = _open_attribute_value(attrs, "model")
        meta_by_id[did] = entry
    return meta_by_id


def _fmt_action_verb(action: "ParsedAction") -> str:
    """Render a verb + its args as a single human-readable token string."""
    return (action.verb + " " + " ".join(action.args)).rstrip()


def _print_apply_preflight_table(actions, meta, *, mode_tag: str) -> None:
    """Print a pre-flight table summarising every action before HTTP runs.

    Operator scans the serial column against the TOS web UI in one pass
    before committing — the dry-run becomes self-sufficient as a
    pre-commit checklist.
    """
    rows = []
    for a in actions:
        m = meta.get(a.id_entity, {})
        rows.append(
            (
                str(a.id_entity),
                m.get("serial") or "?",
                m.get("model") or "?",
                m.get("subtype") or "?",
                _fmt_action_verb(a),
            )
        )
    headers = ("id", "serial", "model", "current_subtype", "→ action")
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    print(f"Pre-flight ({len(actions)} action(s), {mode_tag}):")
    print()
    line = "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    print()


def _apply_main(args) -> int:
    """Handle ``tos audit apply <file>``.

    Strict-then-permissive: parse the whole file first, refuse to send
    any HTTP if a single line is malformed; once all lines parse, run
    them in file order, continuing past individual failures so an
    operator with N independent fixes doesn't see one bad line abort
    the rest.
    """
    import json as _json
    from pathlib import Path

    from .api.tos_client import TOSClient
    from .api.tos_writer import TOSWriter

    path = Path(args.action_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read action file: {exc}", file=sys.stderr)
        return 2

    actions, errors = _parse_action_file(text)
    if errors:
        print(
            f"Refusing to apply — {len(errors)} parse error(s) in {path}:",
            file=sys.stderr,
        )
        for err in errors:
            print(
                f"  line {err.line_no}: {err.message}\n    | {err.raw}",
                file=sys.stderr,
            )
        return 2

    scheme = "https" if args.port == 443 else "http"
    base_url = f"{scheme}://{args.server}:{args.port}/tos/v1"
    dry_run = not args.apply

    # Pre-flight metadata lookup. Read-only client (no auth needed) — separate
    # from the writer so authentication only happens if we actually need to
    # write. One GET per unique id; failed lookups yield None fields and the
    # row renders with `?` placeholders.
    client = TOSClient(base_url=base_url)
    meta = _fetch_action_meta(client, (a.id_entity for a in actions))

    # Build the subtype code → id resolver lazily — only when at least one
    # action actually needs it. Avoids the GET when every line is `defer`.
    needs_subtypes = any(a.verb == "change-subtype" for a in actions)
    subtype_id_by_code = _fetch_subtype_id_by_code(client) if needs_subtypes else {}

    # Build the open-join lookup lazily — only when decommission or move
    # appears. Both verbs need to close the device's currently-open parent
    # join. The index build is the ~110s cost; once built, the per-device
    # open join is an O(1) dict access. Shared across all such actions in
    # this run.
    _NEEDS_JOIN_INDEX = {"decommission", "move"}
    needs_join_index = any(a.verb in _NEEDS_JOIN_INDEX for a in actions)
    open_joins_by_device: Dict[int, Any] = {}
    if needs_join_index:
        open_joins_by_device = _build_open_joins_lookup(
            client,
            target_ids={a.id_entity for a in actions if a.verb in _NEEDS_JOIN_INDEX},
        )

    writer = TOSWriter(base_url=base_url, dry_run=dry_run)

    results: List[ActionResult] = []
    for action in actions:
        results.append(
            _dispatch_action(
                writer,
                action,
                subtype_id_by_code=subtype_id_by_code,
                open_joins_by_device=open_joins_by_device,
            )
        )

    if args.json:
        payload = {
            "file": str(path),
            "dry_run": dry_run,
            "total_actions": len(actions),
            "results": [
                {
                    "line_no": r.action.line_no,
                    "id_entity": r.action.id_entity,
                    "serial": meta.get(r.action.id_entity, {}).get("serial"),
                    "model": meta.get(r.action.id_entity, {}).get("model"),
                    "current_subtype": meta.get(r.action.id_entity, {}).get("subtype"),
                    "verb": r.action.verb,
                    "args": r.action.args,
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in results
            ],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode_tag = "DRY-RUN" if dry_run else "APPLY"
        print(f"Action file: {path}")
        _print_apply_preflight_table(actions, meta, mode_tag=mode_tag)

        for r in results:
            marker = {"ok": "✓", "deferred": "·", "failed": "✗"}.get(r.status, "?")
            m = meta.get(r.action.id_entity, {})
            sn = m.get("serial") or "?"
            model = m.get("model") or "?"
            cur = m.get("subtype") or "?"
            print(
                f"  {marker} line {r.action.line_no}: "
                f"id={r.action.id_entity} SN {sn} ({model}, {cur}) → "
                f"{_fmt_action_verb(r.action)}"
            )
            print(f"      → {r.detail}")
        counts = {"ok": 0, "deferred": 0, "failed": 0}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        print(
            f"\nSummary: {counts['ok']} ok, {counts['deferred']} deferred, "
            f"{counts['failed']} failed"
        )
        if dry_run:
            print("(no writes were sent — re-run with --apply to commit)")

    return 0 if all(r.status != "failed" for r in results) else 1


def _stderr_progress(unit: str):
    """Return a (current, total) callback that overwrites a single stderr line.

    Used by ``tos audit fleet-gaps`` so the operator sees the parent walk
    is alive (the index build is the slow step, ~10s on the live IMO
    fleet). Skipped when stderr isn't a TTY to avoid polluting log files.
    """
    if not sys.stderr.isatty():
        return None

    def cb(current: int, total: int) -> None:
        end = "\n" if current == total else ""
        sys.stderr.write(f"\r  {unit}: {current}/{total}{end}")
        sys.stderr.flush()

    return cb


def _device_report_to_dict(report):
    """Convert a :class:`DeviceAuditReport` to a JSON-serialisable dict."""
    return {
        "kind": "device",
        "id_entity": report.id_entity,
        "subtype": report.subtype,
        "serial": report.serial,
        "current_parent_id": report.current_parent_id,
        "current_parent_name": report.current_parent_name,
        "current_parent_subtype": report.current_parent_subtype,
        "open_joins": [_join_to_dict(j) for j in report.open_joins],
        "invariant_I1_ok": report.invariant_I1_ok,
        "invariant_violations": list(report.invariant_violations),
    }


def _attribute_date_report_to_dict(report):
    """Convert a :class:`StationAttributeDateReport` to a JSON-serialisable dict."""

    def _violation_dict(v):
        return {
            "id_entity": v.id_entity,
            "subtype": v.subtype,
            "serial": v.serial,
            "code": v.code,
            "date_from": v.date_from,
            "value": v.value,
            "earliest_known": v.earliest_known,
            "anchor_source": v.anchor_source,
        }

    return {
        "kind": "attribute-dates",
        "station_id": report.station_id,
        "station_name": report.station_name,
        "audited_devices": report.audited_devices,
        "devices_skipped": report.devices_skipped,
        "unknown_codes": list(report.unknown_codes),
        "violations": [_violation_dict(v) for v in report.violations],
        "suppressed": [
            {
                **_violation_dict(s.violation),
                "suppressions_path": str(s.suppressions_path),
                "line_no": s.line_no,
            }
            for s in report.suppressed
        ],
        "suppressions_path": (
            str(report.suppressions_path) if report.suppressions_path else None
        ),
        "suppressions_disabled": report.suppressions_disabled,
        "suppressions_errors": [
            {"line_no": e.line_no, "message": e.message, "raw": e.raw}
            for e in report.suppressions_errors
        ],
        "included_codes": list(report.included_codes),
        "excluded_codes": list(report.excluded_codes),
        "included_codes_unmatched": list(report.included_codes_unmatched),
    }


def _print_attribute_date_report(report, *, verbose: bool = False):
    """Render an attribute-dates audit report as plain text on stdout.

    Groups violations under each device for a compact, human-readable layout.
    ``verbose=True`` shows:

    * the anchor source on each line (which signal pinned earliest_known)
    * a copy-pasteable ``SUPPRESS`` hint per violation
    * unknown attribute codes that the catalog doesn't cover yet
    * the suppressed entries that the file silenced, with file:lineno
      references — the only audit trail of silenced violations
    """
    status = "CLEAN" if not report.has_violations else "VIOLATIONS"
    marker = "✓" if not report.has_violations else "✗"
    name = report.station_name or "?"
    print(f"{marker} Station {name!r} (id_entity={report.station_id}) — " f"{status}")
    print(
        f"  audited devices: {report.audited_devices}  "
        f"(skipped {report.devices_skipped} outside requested subtypes)"
    )
    if report.suppressed_count:
        print(
            f"  suppressed: {report.suppressed_count} entry(ies) via "
            f"{report.suppressions_path}"
        )
    elif report.suppressions_disabled:
        print("  suppressions: disabled (--no-suppressions)")
    if report.included_codes:
        print(f"  --include codes: {', '.join(report.included_codes)}")
    if report.excluded_codes:
        print(f"  --exclude codes: {', '.join(report.excluded_codes)}")

    if report.violations:
        # Group by (device_id, subtype, serial) to render a compact block per device.
        by_device: Dict[int, List] = {}
        device_meta: Dict[int, tuple] = {}
        for v in report.violations:
            by_device.setdefault(v.id_entity, []).append(v)
            device_meta[v.id_entity] = (v.subtype, v.serial)
        print()
        print(f"  flagged ({len(report.violations)} period(s)):")
        for did in sorted(by_device):
            subtype, serial = device_meta[did]
            serial_label = f" SN {serial!r}" if serial else ""
            print(f"    {subtype} id_entity={did}{serial_label}")
            for v in by_device[did]:
                value_part = f" value={v.value!r}" if v.value is not None else ""
                if verbose:
                    print(
                        f"      · {v.code:24s} date_from={v.date_from}  "
                        f"(earliest_known={v.earliest_known}, "
                        f"anchor={v.anchor_source}){value_part}"
                    )
                    # Copy-pasteable SUPPRESS line — closes the loop between
                    # detection and committing a suppression.
                    print(
                        f"        suppress: SUPPRESS {v.id_entity} "
                        f"{v.code} {v.date_from}"
                    )
                else:
                    print(
                        f"      · {v.code:24s} date_from={v.date_from}  "
                        f"earliest_known={v.earliest_known}{value_part}"
                    )

    if verbose and report.suppressed:
        print()
        print(f"  suppressed ({len(report.suppressed)} silenced entry(ies)):")
        by_device_s: Dict[int, List] = {}
        device_meta_s: Dict[int, tuple] = {}
        for s in report.suppressed:
            v = s.violation
            by_device_s.setdefault(v.id_entity, []).append(s)
            device_meta_s[v.id_entity] = (v.subtype, v.serial)
        for did in sorted(by_device_s):
            subtype, serial = device_meta_s[did]
            serial_label = f" SN {serial!r}" if serial else ""
            print(f"    {subtype} id_entity={did}{serial_label}")
            for s in by_device_s[did]:
                v = s.violation
                value_part = f" value={v.value!r}" if v.value is not None else ""
                print(
                    f"      · {v.code:24s} date_from={v.date_from}  "
                    f"(suppressed at {s.suppressions_path}:{s.line_no})"
                    f"{value_part}"
                )

    if verbose and report.unknown_codes:
        print()
        print(
            f"  unknown attribute codes (seen in TOS, missing from catalog): "
            f"{len(report.unknown_codes)}"
        )
        for code in report.unknown_codes:
            print(f"    - {code}")
    elif report.unknown_codes and not verbose:
        print()
        print(
            f"  ({len(report.unknown_codes)} unknown attribute code(s); "
            f"re-run with --verbose to list them)"
        )


def _missing_attributes_report_to_dict(report):
    """Convert a :class:`StationMissingAttributesReport` to a JSON-serialisable dict."""

    def _violation_dict(v):
        return {
            "id_entity": v.id_entity,
            "subtype": v.subtype,
            "name": v.name,
            "code": v.code,
            "scope": v.scope,
            "suggested_value": v.suggested_value,
            "suggested_date_from": v.suggested_date_from,
        }

    return {
        "kind": "missing-attributes",
        "station_id": report.station_id,
        "station_name": report.station_name,
        "audited_entities": report.audited_entities,
        "devices_skipped": report.devices_skipped,
        "violations": [_violation_dict(v) for v in report.violations],
        "suppressed": [
            {
                **_violation_dict(s.violation),
                "suppressions_path": str(s.suppressions_path),
                "line_no": s.line_no,
            }
            for s in report.suppressed
        ],
        "suppressions_path": (
            str(report.suppressions_path) if report.suppressions_path else None
        ),
        "suppressions_disabled": report.suppressions_disabled,
        "suppressions_errors": [
            {"line_no": e.line_no, "message": e.message, "raw": e.raw}
            for e in report.suppressions_errors
        ],
    }


def _print_missing_attributes_report(report, *, verbose: bool = False):
    """Render a missing-attributes audit report as plain text on stdout.

    Groups violations by entity (station first, then devices) for a
    compact, human-readable layout. ``verbose=True`` shows:

    * a copy-pasteable ``SUPPRESS`` hint per violation
    * the suppressed entries that the file silenced, with file:lineno
      references — the only audit trail of silenced violations
    """
    status = "CLEAN" if not report.has_violations else "VIOLATIONS"
    marker = "✓" if not report.has_violations else "✗"
    name = report.station_name or "?"
    print(f"{marker} Station {name!r} (id_entity={report.station_id}) — {status}")
    print(
        f"  audited entities: {report.audited_entities}  "
        f"(skipped {report.devices_skipped} non-GPS device(s))"
    )
    if report.suppressed_count:
        print(
            f"  suppressed: {report.suppressed_count} entry(ies) via "
            f"{report.suppressions_path}"
        )
    elif report.suppressions_disabled:
        print("  suppressions: disabled (--no-suppressions)")

    if report.violations:
        # Group by entity for the same shape as the triage file.
        station_vios: List = []
        by_device: Dict[int, List] = {}
        entity_meta: Dict[int, tuple] = {}
        for v in report.violations:
            if v.id_entity == report.station_id:
                station_vios.append(v)
            else:
                by_device.setdefault(v.id_entity, []).append(v)
            entity_meta[v.id_entity] = (v.subtype, v.name, v.scope)
        print()
        print(f"  flagged ({len(report.violations)} attribute(s)):")

        def _emit_entity(eid: int, vios: List) -> None:
            subtype, label, _scope = entity_meta[eid]
            label_part = f" {label!r}" if label else ""
            print(f"    {subtype} id_entity={eid}{label_part}")
            for v in vios:
                hint_parts = []
                if v.suggested_value is not None:
                    hint_parts.append(f"suggested: {v.suggested_value!r}")
                else:
                    hint_parts.append("suggested: <FILL_VALUE>")
                if v.suggested_date_from is not None:
                    hint_parts.append(f"date hint: {v.suggested_date_from}")
                hint_str = ", ".join(hint_parts)
                print(f"      · {v.code:24s} ({hint_str})")
                if verbose:
                    print(f"        suppress: SUPPRESS {v.id_entity} {v.code}")

        if station_vios:
            _emit_entity(report.station_id, station_vios)
        for did in sorted(by_device):
            _emit_entity(did, by_device[did])

    if verbose and report.suppressed:
        print()
        print(f"  suppressed ({len(report.suppressed)} silenced entry(ies)):")
        by_device_s: Dict[int, List] = {}
        entity_meta_s: Dict[int, tuple] = {}
        for s in report.suppressed:
            v = s.violation
            by_device_s.setdefault(v.id_entity, []).append(s)
            entity_meta_s[v.id_entity] = (v.subtype, v.name)
        for eid in sorted(by_device_s):
            subtype, label = entity_meta_s[eid]
            label_part = f" {label!r}" if label else ""
            print(f"    {subtype} id_entity={eid}{label_part}")
            for s in by_device_s[eid]:
                v = s.violation
                print(
                    f"      · {v.code:24s} "
                    f"(suppressed at {s.suppressions_path}:{s.line_no})"
                )


def _visit_coverage_report_to_dict(report):
    """Convert a :class:`StationVisitCoverageReport` to JSON-serialisable dict."""

    def _violation_dict(v):
        return {
            "device_id": v.device_id,
            "device_subtype": v.device_subtype,
            "device_label": v.device_label,
            "event_date": v.event_date,
            "coverage_window_days": v.coverage_window_days,
        }

    return {
        "kind": "visit-coverage",
        "station_id": report.station_id,
        "station_name": report.station_name,
        "since": report.since,
        "coverage_window_days": report.coverage_window_days,
        "audited_events": report.audited_events,
        "violations": [_violation_dict(v) for v in report.violations],
        "suppressed": [
            {
                **_violation_dict(s.violation),
                "suppressions_path": str(s.suppressions_path),
                "line_no": s.line_no,
            }
            for s in report.suppressed
        ],
        "suppressions_path": (
            str(report.suppressions_path) if report.suppressions_path else None
        ),
        "suppressions_disabled": report.suppressions_disabled,
        "suppressions_errors": [
            {"line_no": e.line_no, "message": e.message, "raw": e.raw}
            for e in report.suppressions_errors
        ],
    }


def _print_visit_coverage_report(report, *, verbose: bool = False):
    """Render a visit-coverage audit report as plain text on stdout.

    Groups violations by device. ``verbose=True`` adds a copy-pasteable
    ``SUPPRESS`` hint per violation + lists silenced entries with
    file:lineno references.
    """
    status = "CLEAN" if not report.has_violations else "VIOLATIONS"
    marker = "✓" if not report.has_violations else "✗"
    name = report.station_name or "?"
    print(f"{marker} Station {name!r} (id_entity={report.station_id}) — {status}")
    print(
        f"  window: ±{report.coverage_window_days}d  "
        f"since: {report.since}  "
        f"events audited: {report.audited_events}"
    )
    if report.suppressed_count:
        print(
            f"  suppressed: {report.suppressed_count} entry(ies) via "
            f"{report.suppressions_path}"
        )
    elif report.suppressions_disabled:
        print("  suppressions: disabled (--no-suppressions)")

    if report.violations:
        by_device: Dict[int, List] = {}
        device_meta: Dict[int, tuple] = {}
        for v in report.violations:
            by_device.setdefault(v.device_id, []).append(v)
            device_meta[v.device_id] = (v.device_subtype, v.device_label)
        print()
        print(f"  uncovered events ({len(report.violations)}):")
        for did in sorted(by_device):
            subtype, label = device_meta[did]
            label_part = f" {label!r}" if label else ""
            print(f"    {subtype} id_entity={did}{label_part}")
            for v in by_device[did]:
                print(f"      · {v.event_date}")
                if verbose:
                    print(f"        suppress: SUPPRESS {v.device_id} {v.event_date}")

    if verbose and report.suppressed:
        print()
        print(f"  suppressed ({len(report.suppressed)} silenced entry(ies)):")
        by_device_s: Dict[int, List] = {}
        device_meta_s: Dict[int, tuple] = {}
        for s in report.suppressed:
            v = s.violation
            by_device_s.setdefault(v.device_id, []).append(s)
            device_meta_s[v.device_id] = (v.device_subtype, v.device_label)
        for did in sorted(by_device_s):
            subtype, label = device_meta_s[did]
            label_part = f" {label!r}" if label else ""
            print(f"    {subtype} id_entity={did}{label_part}")
            for s in by_device_s[did]:
                v = s.violation
                print(
                    f"      · {v.event_date} "
                    f"(suppressed at {s.suppressions_path}:{s.line_no})"
                )


def _contact_dates_report_to_dict(report):
    """Convert a :class:`StationContactDatesReport` to JSON-serialisable dict."""

    def _violation_dict(v):
        return {
            "id_relationship": v.id_relationship,
            "id_contact": v.id_contact,
            "contact_label": v.contact_label,
            "role": v.role,
            "per_time_from": v.per_time_from,
        }

    return {
        "kind": "contact-dates",
        "station_id": report.station_id,
        "station_name": report.station_name,
        "audited_relationships": report.audited_relationships,
        "violations": [_violation_dict(v) for v in report.violations],
        "suppressed": [
            {
                **_violation_dict(s.violation),
                "suppressions_path": str(s.suppressions_path),
                "line_no": s.line_no,
            }
            for s in report.suppressed
        ],
        "suppressions_path": (
            str(report.suppressions_path) if report.suppressions_path else None
        ),
        "suppressions_disabled": report.suppressions_disabled,
        "suppressions_errors": [
            {"line_no": e.line_no, "message": e.message, "raw": e.raw}
            for e in report.suppressions_errors
        ],
    }


def _print_contact_dates_report(report, *, verbose: bool = False):
    """Render a contact-dates audit report as plain text on stdout."""
    status = "CLEAN" if not report.has_violations else "VIOLATIONS"
    marker = "✓" if not report.has_violations else "✗"
    name = report.station_name or "?"
    print(f"{marker} Station {name!r} (id_entity={report.station_id}) — {status}")
    print(f"  relationships audited: {report.audited_relationships}")
    if report.suppressed_count:
        print(
            f"  suppressed: {report.suppressed_count} entry(ies) via "
            f"{report.suppressions_path}"
        )
    elif report.suppressions_disabled:
        print("  suppressions: disabled (--no-suppressions)")

    if report.violations:
        print()
        print(f"  migration-artifact dates ({len(report.violations)}):")
        for v in report.violations:
            label = f" {v.contact_label!r}" if v.contact_label else ""
            role = f" role={v.role}" if v.role else ""
            print(f"    rel {v.id_relationship} — contact {v.id_contact}{label}{role}")
            print(f"      · per_time_from = {v.per_time_from}  → backdate to `start`")
            if verbose:
                print(f"        suppress: SUPPRESS {v.id_relationship}")

    if verbose and report.suppressed:
        print()
        print(f"  suppressed ({len(report.suppressed)} silenced entry(ies)):")
        for s in report.suppressed:
            v = s.violation
            print(
                f"    rel {v.id_relationship} (per_time_from={v.per_time_from}) "
                f"— suppressed at {s.suppressions_path}:{s.line_no}"
            )


def _station_report_to_dict(report):
    """Convert a :class:`StationAuditReport` to a JSON-serialisable dict."""
    return {
        "kind": "station",
        "id_entity": report.id_entity,
        "name": report.name,
        "subtype": report.subtype,
        "is_real_station": report.is_real_station,
        "open_children_by_subtype": {
            subtype: [_join_to_dict(j) for j in joins]
            for subtype, joins in report.open_children_by_subtype.items()
        },
        "invariant_I2_ok": report.invariant_I2_ok,
        "invariant_violations": list(report.invariant_violations),
        "completeness_warnings": list(report.completeness_warnings),
    }


def _join_to_dict(join):
    if join is None:
        return None
    return {
        "id_entity_parent": join.id_entity_parent,
        "id_entity_child": join.id_entity_child,
        "parent_name": join.parent_name,
        "child_subtype": join.child_subtype,
        "time_from": join.time_from,
        "time_to": join.time_to,
    }


def _fleet_gap_report_to_dict(report, *, top=None):
    """Convert a :class:`history.FleetGapReport` to a JSON-serialisable dict.

    Honors ``top`` by trimming the device list to the N rows with the
    longest gaps (rows are pre-sorted by max-gap descending).

    When the report was built with ``with_timelines=True``, each device
    dict carries a ``timeline`` field with its complete join history;
    otherwise that field is ``None``.
    """
    devices = report.devices if top is None else report.devices[: max(top, 0)]
    return {
        "kind": "fleet-gaps",
        "min_days": report.min_days,
        "build": {
            "parents_walked": report.parents_walked,
            "parents_failed": report.parents_failed,
            "total_joins": report.total_joins,
            "total_devices": report.total_devices,
        },
        "summary": {
            "devices_with_gaps": report.devices_with_gaps,
            "gap_count": report.gap_count,
            "truly_orphan": report.orphan_count,
            "rows_returned": len(devices),
        },
        "parent_names": {
            str(pid): name for pid, name in sorted(report.parent_names.items())
        },
        "devices": [
            {
                "id_entity": d.id_entity,
                "subtype": d.subtype,
                "serial": d.serial,
                "model": d.model,
                "is_truly_orphan": d.is_truly_orphan,
                "last_parent_id": d.last_parent_id,
                "last_parent_name": d.last_parent_name,
                "max_gap_days": d.max_gap_days,
                "gaps": [
                    {
                        "after_parent": g.after.id_entity_parent,
                        "before_parent": g.before.id_entity_parent,
                        "time_from": g.time_from,
                        "time_to": g.time_to,
                        "duration_days": g.duration_days,
                    }
                    for g in d.gaps
                ],
                "timeline": _embedded_timeline_to_dict(d.timeline, report.parent_names),
            }
            for d in devices
        ],
    }


def _embedded_timeline_to_dict(timeline, parent_names):
    """Render an embedded DeviceTimelineReport as a dict, or None.

    Used by the fleet-gaps `--with-timelines` JSON path. Excludes the
    redundant id_entity/subtype/serial/model fields (they're already on
    the surrounding device row) — the embedded view is just the joins
    + every gap, which is what the headline row doesn't carry.
    """
    if timeline is None:
        return None
    return {
        "is_currently_attached": timeline.is_currently_attached,
        "joins": [
            {
                "id_entity_connection": j.id_entity_connection,
                "id_entity_parent": j.id_entity_parent,
                "parent_name": parent_names.get(j.id_entity_parent),
                "time_from": j.time_from,
                "time_to": j.time_to,
                "is_open": j.is_open,
            }
            for j in timeline.joins
        ],
        "all_gaps": [
            {
                "after_parent": g.after.id_entity_parent,
                "before_parent": g.before.id_entity_parent,
                "time_from": g.time_from,
                "time_to": g.time_to,
                "duration_days": g.duration_days,
            }
            for g in timeline.gaps
        ],
    }


def _print_fleet_gap_report(report, *, top=None, verbose: bool = False):
    """Render a fleet-gap report as plain text on stdout.

    Layout: header counts, gap-bearing rows (one line per device, with
    the longest gap inline), then a separate truly-orphan section.
    ``verbose=True`` lists every gap on every gap-bearing device, not
    just the longest. ``top`` trims the gap-bearing rows; orphans are
    always shown in full when present.
    """
    print(
        f"Fleet gap report — min duration {report.min_days:g} days "
        f"(walked {report.parents_walked} parents, "
        f"{report.parents_failed} failed; "
        f"{report.total_joins} joins / {report.total_devices} devices indexed)"
    )

    gap_rows = [d for d in report.devices if d.gaps]
    orphan_rows = [d for d in report.devices if d.is_truly_orphan and not d.gaps]
    print(
        f"  {report.devices_with_gaps} device(s) with gaps ≥{report.min_days:g}d, "
        f"{report.gap_count} gap(s) total"
    )
    if report.orphan_count:
        print(
            f"  {report.orphan_count} device(s) truly orphan (joins exist, none open)"
        )
    print()

    shown_gap_rows = gap_rows if top is None else gap_rows[: max(top, 0)]
    if shown_gap_rows:
        suffix = (
            f" (top {len(shown_gap_rows)} of {len(gap_rows)})"
            if top is not None and len(shown_gap_rows) < len(gap_rows)
            else ""
        )
        print(f"Gaps{suffix}:")
        for d in shown_gap_rows:
            _print_fleet_gap_row(d, verbose=verbose, parent_names=report.parent_names)
    elif gap_rows:
        # `top=0` edge case
        print(f"Gaps: (suppressed by --top {top})")

    if orphan_rows:
        print()
        print("Truly orphan:")
        for d in orphan_rows:
            _print_fleet_orphan_row(d)

    if not gap_rows and not orphan_rows:
        print("No gaps or orphans matched the current filters.")


def _fmt_device_label(d) -> str:
    """Format the leading id/serial/model/subtype block for one device row."""
    serial = d.serial or "?"
    model = d.model or "?"
    subtype = d.subtype or "?"
    return f"id={d.id_entity:<6d} SN {serial:<14s} {model:<18s} {subtype}"


def _fmt_gap(g) -> str:
    """Format a single Gap as ``Nd  from → to  (parent → parent)``."""
    return (
        f"{g.duration_days:>6.0f}d  "
        f"{g.time_from} → {g.time_to}  "
        f"(parent {g.after.id_entity_parent} → {g.before.id_entity_parent})"
    )


def _print_fleet_gap_row(d, *, verbose: bool, parent_names=None) -> None:
    label = _fmt_device_label(d)
    gaps = sorted(d.gaps, key=lambda g: -g.duration_days)
    show_block = verbose or len(gaps) == 1 or d.timeline is not None
    if show_block:
        print(f"  {label}")
        for g in gaps:
            print(f"      {_fmt_gap(g)}")
    else:
        # Headline: one line per device, longest gap inline; mention overflow.
        print(f"  {label}  {_fmt_gap(gaps[0])}  (+{len(gaps) - 1} more)")
    if d.timeline is not None:
        _print_embedded_timeline(d.timeline, parent_names or {})


def _print_embedded_timeline(timeline, parent_names):
    """Render the embedded join history under a fleet-gap row.

    ``parent_names`` is the report-level dict from
    :func:`scan_fleet_gaps`; entries not in it fall back to ``?``.
    """
    joins = timeline.joins
    if not joins:
        print("      (no joins indexed — device unreachable from any walked parent)")
        return
    print(f"      Full history — {len(joins)} join(s):")
    gap_by_after_id: Dict[int, Any] = {id(g.after): g for g in timeline.gaps}
    for i, j in enumerate(joins, 1):
        kind = "open  " if j.is_open else "closed"
        pname = parent_names.get(j.id_entity_parent) or "?"
        end = j.time_to if j.time_to is not None else "—"
        print(
            f"        {i:2d}. [{kind}] {j.time_from} → {end}   "
            f"parent={j.id_entity_parent} ({pname})"
        )
        g = gap_by_after_id.get(id(j))
        if g is not None:
            print(f"              ⚠ gap of {g.duration_days:.0f}d before next join")


def _print_fleet_orphan_row(d) -> None:
    label = _fmt_device_label(d)
    if d.last_parent_id is not None:
        parent = d.last_parent_name or "?"
        tail = f"last at {parent!r} (id_entity={d.last_parent_id})"
    else:
        tail = "no closed-join history available"
    print(f"  {label}  {tail}")


def _timelines_report_to_dict(report):
    """Convert a :class:`history.TimelinesReport` to a JSON-serialisable dict."""
    return {
        "kind": "timeline",
        "build": {
            "parents_walked": report.parents_walked,
            "parents_failed": report.parents_failed,
            "total_joins": report.total_joins,
            "total_devices": report.total_devices,
        },
        "parent_names": {
            str(pid): name for pid, name in sorted(report.parent_names.items())
        },
        "timelines": [
            {
                "id_entity": t.id_entity,
                "subtype": t.subtype,
                "serial": t.serial,
                "model": t.model,
                "is_currently_attached": t.is_currently_attached,
                "is_truly_orphan": t.is_truly_orphan,
                "joins": [
                    {
                        "id_entity_connection": j.id_entity_connection,
                        "id_entity_parent": j.id_entity_parent,
                        "parent_name": report.parent_names.get(j.id_entity_parent),
                        "time_from": j.time_from,
                        "time_to": j.time_to,
                        "is_open": j.is_open,
                    }
                    for j in t.joins
                ],
                "gaps": [
                    {
                        "after_parent": g.after.id_entity_parent,
                        "before_parent": g.before.id_entity_parent,
                        "time_from": g.time_from,
                        "time_to": g.time_to,
                        "duration_days": g.duration_days,
                    }
                    for g in t.gaps
                ],
            }
            for t in report.timelines
        ],
    }


def _print_timelines_report(report):
    """Render a TimelinesReport as plain text on stdout.

    One block per device: header (id / subtype / serial / model / state),
    then a numbered list of every join in chronological order, with the
    gap before each non-first join annotated inline.
    """
    print(
        f"Timeline lookup — walked {report.parents_walked} parents, "
        f"{report.parents_failed} failed; "
        f"{report.total_joins} joins / {report.total_devices} devices indexed"
    )
    for t in report.timelines:
        print()
        _print_one_timeline(t, report.parent_names)


def _print_one_timeline(t, parent_names):
    subtype = t.subtype or "?"
    serial = t.serial or "?"
    model = t.model or "?"
    state_bits = []
    if t.is_currently_attached:
        state_bits.append("currently attached")
    if t.is_truly_orphan:
        state_bits.append("truly orphan (no open join)")
    if not state_bits:
        state_bits.append("no joins indexed" if not t.joins else "state unknown")
    state = "; ".join(state_bits)
    print(
        f"Device id={t.id_entity}  SN {serial!r}  model {model!r}  "
        f"{subtype}  — {state}"
    )

    if not t.joins:
        print("  (no joins in the walked parent set — device unreachable)")
        return

    print(f"  History — {len(t.joins)} join(s):")
    gap_by_after_id: Dict[int, Any] = {id(g.after): g for g in t.gaps}
    for i, j in enumerate(t.joins, 1):
        kind = "open  " if j.is_open else "closed"
        pname = parent_names.get(j.id_entity_parent) or "?"
        end = j.time_to if j.time_to is not None else "—"
        print(
            f"    {i:2d}. [{kind}] {j.time_from} → {end}   "
            f"parent={j.id_entity_parent} ({pname})"
        )
        g = gap_by_after_id.get(id(j))
        if g is not None:
            print(
                f"          ⚠ gap of {g.duration_days:.0f}d before next join "
                f"({g.time_from} → {g.time_to})"
            )


def _orphan_scan_to_dict(scan):
    """Convert an :class:`OrphanScanResult` to a JSON-serialisable dict."""
    return {
        "kind": "orphans",
        "subtype": scan.subtype,
        "models_searched": list(scan.models_searched),
        "total_audited": scan.total_audited,
        "violation_count": scan.violation_count,
        "orphan_reports": [_device_report_to_dict(r) for r in scan.orphan_reports],
    }


def _print_orphan_scan(scan, *, verbose: bool = False):
    """Render an orphan-scan summary as plain text on stdout.

    Default: one row per orphan. ``verbose=True`` prepends a paragraph
    explaining what an I1 orphan is and how to fix one (shared preamble
    covers every row, since they're all the same violation type).
    """
    from . import audit as audit_mod

    print(
        f"Scanned {scan.total_audited} {scan.subtype} devices "
        f"(models: {', '.join(scan.models_searched)}) — "
        f"{scan.violation_count} I1 violation(s)."
    )
    if not scan.orphan_reports:
        print("  (no orphans found)")
        return
    if verbose:
        print()
        print(audit_mod.orphan_scan_preamble())
        print()
        print("Orphans:")
    for r in scan.orphan_reports:
        serial = r.serial or "?"
        if r.current_parent_id is None:
            tail = "no current parent in TOS"
        else:
            tail = (
                f"last at {r.current_parent_name!r} "
                f"(id_entity={r.current_parent_id})"
            )
        print(f"  ✗ id_entity={r.id_entity} SN {serial}  {tail}")
    if not verbose:
        print()
        print("(run with --verbose for what this means and how to fix)")


def _print_device_report(report, *, verbose: bool = False):
    """Render a device audit report as plain text on stdout.

    Default: header + structural lines + short tagged violation strings.
    ``verbose=True`` appends a three-block explainer (What this means /
    Expected state / To fix).
    """
    from . import audit as audit_mod

    serial = report.serial or "?"
    if report.current_parent_id is None:
        parent = "<none>"
    else:
        subtype_tag = (
            f", {report.current_parent_subtype}"
            if report.current_parent_subtype
            else ""
        )
        parent = (
            f"id_entity={report.current_parent_id} "
            f"({report.current_parent_name!r}{subtype_tag})"
        )
    status = "I1 OK" if report.invariant_I1_ok else "I1 VIOLATION"
    marker = "✓" if report.invariant_I1_ok else "✗"
    print(
        f"{marker} Device {report.subtype} SN {serial} "
        f"(id_entity={report.id_entity}) — {status}"
    )
    print(f"  current parent: {parent}")
    if not report.open_joins:
        print("  open joins: <none>")
    elif len(report.open_joins) == 1:
        j = report.open_joins[0]
        end = j.time_to or "present"
        print(f"  open join: {j.time_from} → {end}")
    else:
        print(f"  open joins: {len(report.open_joins)} (I1 violation)")
        for j in report.open_joins:
            end = j.time_to or "present"
            print(f"    - {j.time_from} → {end}")
    for v in report.invariant_violations:
        print(f"  · {v}")
    if report.invariant_violations:
        if verbose:
            print()
            print(audit_mod.explain_device_violations(report))
        else:
            print()
            print("  (run with --verbose for what this means and how to fix)")


def _print_station_report(report, *, verbose: bool = False):
    """Render a station audit report as plain text on stdout.

    Default: header + open-children list + short violation/warning strings.
    ``verbose=True`` appends a three-block explainer for I2 violations.
    """
    from . import audit as audit_mod

    subtype_tag = f", {report.subtype}" if report.subtype else ""
    if not report.is_real_station:
        # Warehouse-style entity (e.g. B9 - Kjallari - Jörð). I2 doesn't
        # apply — render an inventory listing instead. No violation marker.
        print(
            f"📦 Inventory at {report.name!r} "
            f"(id_entity={report.id_entity}{subtype_tag})"
        )
        if not report.open_children_by_subtype:
            print("  (no open children)")
        else:
            counts = {k: len(v) for k, v in report.open_children_by_subtype.items()}
            total = sum(counts.values())
            print(f"  {total} open device(s):")
            for st in sorted(counts):
                print(f"    {st:14s} {counts[st]}")
            if verbose:
                print()
                print("  detail:")
                for st in sorted(report.open_children_by_subtype):
                    for j in report.open_children_by_subtype[st]:
                        print(
                            f"    {st:14s} id_entity={j.id_entity_child} "
                            f"from {j.time_from}"
                        )
        return

    status = "I2 OK" if report.invariant_I2_ok else "I2 VIOLATION"
    marker = "✓" if report.invariant_I2_ok else "✗"
    print(
        f"{marker} Station {report.name!r} "
        f"(id_entity={report.id_entity}{subtype_tag}) — {status}"
    )
    if not report.open_children_by_subtype:
        print("  (no open children)")
    else:
        print("  open children:")
        for subtype in sorted(report.open_children_by_subtype):
            joins = report.open_children_by_subtype[subtype]
            for j in joins:
                print(
                    f"    {subtype:14s} id_entity={j.id_entity_child} "
                    f"from {j.time_from}"
                )
    for v in report.invariant_violations:
        print(f"  · {v}")
    for w in report.completeness_warnings:
        print(f"  ⚠ {w}")
    if report.invariant_violations:
        if verbose:
            print()
            print(audit_mod.explain_station_violations(report))
        else:
            print()
            print("  (run with --verbose for what this means and how to fix)")


def _print_top_level_help() -> None:
    """Umbrella help — lists every subcommand.

    Argparse's per-subparser help only sees its own arguments, so the bare
    ``tos --help`` would otherwise only show what argparse rooted at the
    top-level parser knows. Print our own overview instead.
    """
    print(
        "usage: tos <command> [args...]\n"
        "\n"
        "GPS / GNSS station-metadata tool.\n"
        "\n"
        "Subcommands:\n"
        "  owners     Manage the recognised TOS device-owner allow-list.\n"
        "             Examples: `tos owners list`, `tos owners list --refresh`.\n"
        "\n"
        "  device     Manage device entities (warehouse intake + inspection).\n"
        "             Examples: `tos device add ...`, `tos device list --station SAVI`,\n"
        "             `tos device show --id N`.\n"
        "\n"
        "  station    Top-level station orchestration. Subverbs:\n"
        "               station triage <STN>    Generate combined triage file\n"
        "                                       (commented ACTION lines).\n"
        "               station verify <STN>    Re-run audits; exit 0 clean,\n"
        "                                       1 findings, 2 failure.\n"
        "               station show <STN>      Show station identity, open\n"
        "                                       attributes, joined devices.\n"
        "\n"
        "  contact    Inspect TOS contact records (id_contact namespace).\n"
        "               contact show --id N     One contact record.\n"
        "               contact list --station S  Contacts for a station.\n"
        "               contact patch-relationship <id_rel> --time-from DATE\n"
        "                                       Correct a contact↔station\n"
        "                                       relationship date/role (dry-run\n"
        "                                       default; --no-dry-run commits).\n"
        "               contact assign / remove   Open / delete a relationship.\n"
        "               contact create --name … Create a new contact entity.\n"
        "               contact patch-entity <id> …  Edit a contact (FLEET-GLOBAL).\n"
        "\n"
        "  visit      Inspect or create TOS vitjun (visit / maintenance) records.\n"
        "               visit list --station S         Vitjanir for a station.\n"
        "               visit list --device <id>       Vitjanir for a device.\n"
        "               visit show <id_maintenance>    One vitjun's full detail.\n"
        "               visit add  --station S --start DATE [opts]\n"
        "                                              Create a new vitjun (dry-run\n"
        "                                              default; --no-dry-run commits).\n"
        "             Filters (list): --type, --reason, --since, --participants,\n"
        "                             --open / --completed.\n"
        "\n"
        "  fleet      Fleet-wide orchestrators (loop station verbs over\n"
        "             every GNSS station in stations.cfg ~173 sites).\n"
        "               fleet status            Bulk verify oracle —\n"
        "                                       exit 0 clean / 1 findings\n"
        "                                       / 2 audit failure. No\n"
        "                                       disk writes. Suitable\n"
        "                                       for cron / CI.\n"
        "               fleet triage            Generate per-station\n"
        "                                       triage files for\n"
        "                                       `tos audit apply` (skips\n"
        "                                       clean stations by\n"
        "                                       default).\n"
        "               fleet contact-dates     Sweep contact-dates audit\n"
        "                                       fleet-wide; --triage emits a\n"
        "                                       combined fix file.\n"
        "             Filters: --include / --exclude STN1 STN2 …,\n"
        "                      --limit N, --with-archive, --json.\n"
        "             Use `tos fleet --help` for the full operator guide.\n"
        "\n"
        "  audit      Read-only invariants + history reconstruction.\n"
        "             Subverbs:\n"
        "               audit device --id N | --serial SN --subtype TYPE\n"
        "                            I1 single-device audit (current parent +\n"
        "                            open joins).\n"
        "               audit station NAME | --id N\n"
        "                            I2 single-station audit; inventory view\n"
        "                            for warehouses.\n"
        "               audit orphans --subtype TYPE\n"
        "                            Fleet I1-orphan scan (model-search\n"
        "                            enumeration).\n"
        "               audit fleet-gaps [--min-days N] [--subtype TYPE]\n"
        "                            Fleet-wide gap-detection report from\n"
        "                            the global join index.\n"
        "               audit timeline ID [ID ...]\n"
        "                            Per-device complete join history.\n"
        "               audit show --id N | --serial SN --subtype TYPE\n"
        "                            Full device record (attributes +\n"
        "                            optional join chronology).\n"
        "               audit apply <file> [--apply]\n"
        "                            Operator-edited action file —\n"
        "                            change-subtype / decommission / defer.\n"
        "                            Dry-run by default.\n"
        "\n"
        "Per-subcommand help: `tos <subcommand> --help`.\n"
    )


def main(argv=None):
    """Entry point — dispatches to `tos <subcommand> ...`.

    ``tos --help`` / ``tos -h`` prints a custom umbrella help listing every
    subcommand. Unknown / missing subcommands print the umbrella and exit 2.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        _print_top_level_help()
        return 0 if argv else 2
    if argv[0] in KNOWN_SUBCOMMANDS:
        subcmd = argv[0]
        rest = argv[1:]
        if subcmd == "owners":
            return _owners_main(rest)
        if subcmd == "device":
            return _device_main(rest)
        if subcmd == "audit":
            return _audit_main(rest)
        if subcmd == "station":
            return _station_main(rest)
        if subcmd == "contact":
            return _contact_main(rest)
        if subcmd == "fleet":
            return _fleet_main(rest)
        if subcmd == "visit":
            return _visit_main(rest)
    print(f"tos: unknown subcommand {argv[0]!r}\n", file=sys.stderr)
    _print_top_level_help()
    return 2


# ---------------------------------------------------------------------------
# Device-record display (REPL/scripting helper)
# ---------------------------------------------------------------------------


def display_device_record(
    client=None,
    *,
    serial: "str | None" = None,
    id_entity: "int | None" = None,
    subtype: "str | None" = None,
    with_joins: bool = True,
    file=None,
) -> None:
    """Print everything TOS knows about one device.

    Accepts either ``id_entity`` (preferred) **or** ``(serial, subtype)``
    — same contract as :func:`tostools.audit.audit_device`. Useful from
    the REPL after a write to verify TOS state matches intent (status
    transitions, decommissions, subtype changes — all visible in one
    block).

    Output sections:

    1. **Header** — id, subtype, currently-open serial / model.
    2. **Attribute periods** — every attribute code grouped together,
       periods sorted chronologically. Shows closed AND open periods so
       the historical record is visible (status transitions land here).
    3. **Join history** — when ``with_joins=True`` (default), uses the
       global join index to dump every join the device has ever had,
       chronologically. **Triggers a ~110s index build** on first call;
       pass ``with_joins=False`` to skip when you only want attributes.

    Reuses :func:`tostools.audit._resolve_device_entity` for the lookup
    path and :func:`tostools.history.build_join_index` for the
    chronology. The on-screen rendering mirrors
    :func:`_print_one_timeline` so the visual shape is consistent
    across the ``tos audit timeline`` and ``device`` CLI verbs.

    Args:
        client: An unauthenticated :class:`TOSClient`. If omitted, one
            is constructed against the default ``vi-api.vedur.is`` host
            — useful for one-line REPL invocations.
        serial: Device serial number; requires ``subtype``.
        id_entity: Device primary key (preferred when known).
        subtype: Required with ``serial`` to disambiguate (a serial can
            legitimately collide across device types).
        with_joins: Whether to build the join index and print the join
            chronology. Off-by-default mode (False) is the fast path:
            no parent walk, just attribute periods.
        file: Output stream; defaults to ``sys.stdout``. Pass an
            ``io.StringIO`` to capture for tests.
    """
    import sys as _sys

    from .api.tos_client import TOSClient
    from .devices import (
        attribute_periods as _attribute_periods,
    )
    from .devices import (
        find_device as _find_device,
    )
    from .devices import (
        open_attribute as _open_attribute,
    )

    if file is None:
        file = _sys.stdout
    if client is None:
        client = TOSClient()

    history = _find_device(client, serial=serial, id_entity=id_entity, subtype=subtype)
    did = int(history["id_entity"])
    dev_subtype = history.get("code_entity_subtype") or "?"

    open_serial = _open_attribute(history, "serial_number") or "?"
    open_model = _open_attribute(history, "model") or "?"
    open_status = _open_attribute(history, "status") or "<no status attribute>"
    parent_id = history.get("id_entity_parent")

    print(
        f"Device id={did}  subtype={dev_subtype}  SN {open_serial!r}  "
        f"model {open_model!r}",
        file=file,
    )
    print(
        f"  current status: {open_status}  "
        f"id_entity_parent (stale field): {parent_id}",
        file=file,
    )

    # ---- Attribute periods ------------------------------------------------
    by_code = _attribute_periods(history)
    total_periods = sum(len(v) for v in by_code.values())
    print(file=file)
    print(f"Attribute periods ({total_periods}):", file=file)
    for code in sorted(by_code):
        print(f"  {code}:", file=file)
        for p in by_code[code]:
            df = p.get("date_from") or "?"
            dt = p.get("date_to") or "open"
            v = p.get("value")
            marker = "·" if p.get("date_to") is None else " "
            print(
                f"    {marker} {df} → {dt:24s} value={v!r}",
                file=file,
            )

    # ---- Join history -----------------------------------------------------
    if not with_joins:
        print(file=file)
        print(
            "(join history skipped — pass with_joins=True for the full "
            "chronology, costs ~110s for the index build)",
            file=file,
        )
        return

    from .history import build_join_index, enumerate_known_parents

    print(file=file)
    print(
        "Building join index for chronology (~110s on the IMO fleet)...",
        file=_sys.stderr,
    )
    parents = enumerate_known_parents(client)
    parent_names: Dict[int, "str | None"] = {p.id_entity: p.name for p in parents}
    index = build_join_index(client, parents=parents)
    timeline = index.timeline(did)

    state_bits = []
    if timeline.is_currently_attached:
        state_bits.append("currently attached")
    if timeline.is_truly_orphan:
        state_bits.append("truly orphan (no open join)")
    if not timeline.joins:
        state_bits.append("no joins indexed")
    state = "; ".join(state_bits) if state_bits else "state unknown"

    print(f"Join history — {state}:", file=file)
    if not timeline.joins:
        print("  (device unreachable from any walked parent)", file=file)
        return
    gaps = timeline.gaps(min_days=0.0)
    gap_by_after_id = {id(g.after): g for g in gaps}
    for i, j in enumerate(timeline.joins, 1):
        kind = "open  " if j.is_open else "closed"
        pname = parent_names.get(j.id_entity_parent) or "?"
        end = j.time_to if j.time_to is not None else "—"
        print(
            f"  {i:2d}. [{kind}] {j.time_from} → {end:24s} "
            f"parent={j.id_entity_parent} ({pname})",
            file=file,
        )
        g = gap_by_after_id.get(id(j))
        if g is not None:
            print(
                f"        ⚠ gap of {g.duration_days:.0f}d before next join "
                f"({g.time_from} → {g.time_to})",
                file=file,
            )


if __name__ == "__main__":
    sys.exit(main())
