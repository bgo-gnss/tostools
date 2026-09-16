"""``tos attribute`` — drill down on a single ``id_attribute_value``.

Every temporal view in TOS is assembled from ``attribute_value`` rows, and
every table that renders one prints its id (the cyan column in ``tos station
show`` / ``tos device show``). Until now there was no way to look that id back
up: the read client had getters keyed on ``id_entity``, ``id_contact``,
``id_maintenance`` and ``id_child``, but none on ``id_attribute_value``, and
the endpoint was reachable only from inside ``TOSWriter`` as its post-PATCH
read-back.

Deliberately entity-agnostic. A row is a row whether it belongs to a GPS
station, a hydrological gauge, a SIM card or a warehouse — so this verb takes
no ``--station`` and applies no subtype pin.

Why it earns its place: this is the only read that returns a period boundary
at full precision and unfiltered. ``tos search --history`` groups by code and
reports day granularity in its table; the summary views show open periods
only. When two periods of one attribute start and end inside the SAME DAY,
nothing else distinguishes them. The worked example is V159's ``name``::

    109880  Sandgígjukvísl  2009-05-09T00:00:00 -> 2009-05-09T10:00:00
    109888  Gígjukvísl      2009-05-09T10:00:00 -> open

A 10-hour period and a clean hand-off — which at day granularity reads as
``2009-05-09 -> 2009-05-09`` and looks like a corrupt zero-length row.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

# Attribute codes that name their entity, best first. Used only to label a
# row's owner in the rendered view.
_NAME_CODES = ("name", "marker", "serial_number", "model")


def _entity_label(client, id_entity: Optional[int]) -> str:
    """A human label for ``id_entity`` — ``marker/name (subtype)``.

    Best-effort: a row is worth showing even when its owner cannot be
    resolved, so every failure degrades to the bare id.
    """
    if not id_entity:
        return "—"
    try:
        hist = client.get_entity_history(int(id_entity))
    except Exception:  # noqa: BLE001 - labelling must never fail the read
        return f"id_entity={id_entity}"
    if not isinstance(hist, dict):
        return f"id_entity={id_entity}"

    open_vals: Dict[str, str] = {}
    for attr in hist.get("attributes") or []:
        code = attr.get("code")
        if code in _NAME_CODES and attr.get("date_to") in (None, ""):
            open_vals.setdefault(str(code), str(attr.get("value") or ""))

    label = next((open_vals[c] for c in _NAME_CODES if open_vals.get(c)), "")
    subtype = hist.get("subtype") or ""
    marker = open_vals.get("marker")
    if marker and label and marker != label:
        label = f"{marker}/{label}"
    bits = [b for b in (label, f"({subtype})" if subtype else "") if b]
    return " ".join(bits) or f"id_entity={id_entity}"


def _render(console, rows: List[Dict[str, Any]], client, *, resolve: bool) -> None:
    """One block per row — deliberately NOT a table.

    A six-column table squeezes ``date_from`` / ``date_to`` and rich elides
    them to ``2009-05-09T…``, which destroys the one thing this verb exists
    to show. A record layout cannot truncate at any terminal width, and a
    drill-down is normally one or two ids anyway.
    """
    for i, row in enumerate(rows):
        if i:
            console.print()
        date_to = row.get("date_to")
        value = row.get("value")
        entity = (
            _entity_label(client, row.get("id_entity"))
            if resolve
            else str(row.get("id_entity") or "—")
        )
        console.print(
            f"[bold cyan]{row.get('id_attribute_value', '—')}[/]  "
            f"[bold]{row.get('code') or '—'}[/]"
        )
        console.print(f"  value      {value if value is not None else '—'}")
        console.print(f"  date_from  {row.get('date_from') or '—'}")
        # "open" is the word the rest of the CLI uses for a live period; an
        # empty cell would read as missing data instead.
        console.print(f"  date_to    {'open' if date_to in (None, '') else date_to}")
        console.print(
            f"  entity     {entity}  [dim](id_entity={row.get('id_entity')})[/]"
        )


def main(argv: List[str]) -> int:
    """Handle ``tos attribute <verb>``. 0 ok, 1 not found, 2 usage."""
    p = argparse.ArgumentParser(
        prog="tos attribute",
        description=(
            "Look up attribute_value rows by id — the atom every TOS "
            "temporal view is built from, and the id printed in the cyan "
            "column of `tos station show` / `tos device show`.\n\n"
            "Entity-agnostic: works for a GPS station, a hydrological "
            "gauge, a SIM card or a warehouse alike.\n\n"
            "Unlike the summary views this returns the period boundaries "
            "at FULL precision, which is what settles two periods of the "
            "same attribute inside one day."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="verb", required=True)

    p_show = sub.add_parser(
        "show",
        help="Show one or more attribute_value rows by id.",
        description=(
            "Fetch each id and render it. Missing ids are reported on "
            "stderr and set exit 1; any row that WAS found is still "
            "printed, so one bad id in a batch does not hide the rest."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_show.add_argument(
        "ids",
        nargs="+",
        type=int,
        metavar="ID",
        help="One or more id_attribute_value values.",
    )
    p_show.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw rows as JSON, exactly as TOS returned them.",
    )
    p_show.add_argument(
        "--no-resolve",
        action="store_true",
        help=(
            "Skip the id_entity -> marker/name lookup (one extra request "
            "per distinct entity). Print the bare id instead."
        ),
    )
    p_show.add_argument(
        "--server",
        default="vi-api.vedur.is",
        help="TOS API host (default: vi-api.vedur.is).",
    )
    p_show.add_argument("--port", default=None, help="TOS API port.")

    args = p.parse_args(argv)

    from .api.tos_client import TOSClient

    kwargs: Dict[str, Any] = {}
    if args.server:
        kwargs["base_url"] = f"https://{args.server}/tos/internal"
    client = TOSClient(**kwargs) if kwargs else TOSClient()

    rows: List[Dict[str, Any]] = []
    missing: List[int] = []
    for id_av in args.ids:
        row = client.get_attribute_value(id_av)
        if row is None:
            missing.append(id_av)
        else:
            rows.append(row)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif rows:
        from rich.console import Console

        _render(Console(), rows, client, resolve=not args.no_resolve)

    if missing:
        import sys

        print(
            f"tos attribute: no attribute_value row for "
            f"{', '.join(str(m) for m in missing)}",
            file=sys.stderr,
        )
        return 1
    return 0
