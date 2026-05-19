#!/usr/bin/env python3
"""Side-by-side compare of legacy `gps_metadata` vs new `station_sessions`.

Hits live TOS once per station and prints both chains' session lists,
flagging legacy sessions that come out inverted (``time_to < time_from``)
or that the new chain emits with no legacy counterpart.

Usage::

    python scripts/compare_synthesis.py AUST
    python scripts/compare_synthesis.py RHOF VMEY HOFN
    python scripts/compare_synthesis.py AUST --full   # include subtype slots

See ``docs/architecture/synthesis-legacy-divergence.md`` for the two
legacy bugs this script exposes.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tostools import devices, gps_metadata_qc  # noqa: E402
from tostools.api.tos_client import TOSClient  # noqa: E402


def _iso(dt: Any) -> str:
    if dt is None:
        return "open"
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _is_inverted(session: Dict[str, Any]) -> bool:
    tf, tt = session.get("time_from"), session.get("time_to")
    if tf is None or tt is None:
        return False
    return tt < tf


def _run_legacy(marker: str) -> List[Dict[str, Any]]:
    result: Any = gps_metadata_qc.gps_metadata(
        marker, gps_metadata_qc.URL_REST_TOS, loglevel=logging.WARNING
    )
    if not isinstance(result, dict):
        return []
    return list(result.get("device_history") or [])


def _run_new(marker: str) -> List[Dict[str, Any]]:
    client = TOSClient(base_url=gps_metadata_qc.URL_REST_TOS)
    hits = client.search_stations(marker, domains="geophysical")
    if not hits:
        sys.exit(f"  ✗ {marker} not found in TOS (geophysical domain)")
    station_id = hits[0]["id_entity"]
    return devices.station_sessions(client, station_id)


def _format_window(session: Dict[str, Any]) -> str:
    tf = _iso(session.get("time_from"))
    tt = _iso(session.get("time_to"))
    return f"{tf:>19s} → {tt:<19s}"


def _format_full(session: Dict[str, Any]) -> str:
    parts = []
    rcv = session.get("gnss_receiver") or {}
    if rcv:
        parts.append(
            f"      receiver: {rcv.get('model', '—')!s:<18s} "
            f"sn={rcv.get('serial_number', '—')!s:<14s} "
            f"fw={rcv.get('firmware_version', '—')}"
        )
    ant = session.get("antenna") or {}
    if ant:
        parts.append(
            f"      antenna:  {ant.get('model', '—')!s:<18s} "
            f"sn={ant.get('serial_number', '—')!s:<14s} "
            f"h={ant.get('antenna_height', '—')}"
        )
    rad = session.get("radome") or {}
    if rad:
        parts.append(f"      radome:   {rad.get('model', '—')}")
    mon = session.get("monument") or {}
    if mon:
        parts.append(
            f"      monument: sn={mon.get('serial_number', '—')!s:<14s} "
            f"h={mon.get('monument_height', '—')}"
        )
    return "\n".join(parts)


def _print_chain(label: str, sessions: List[Dict[str, Any]], full: bool) -> None:
    inverted = sum(1 for s in sessions if _is_inverted(s))
    flag = f"  ⚠ {inverted} inverted (time_to < time_from)" if inverted else ""
    print(f"\n  {label}  ({len(sessions)} sessions){flag}")
    print(f"  {'─' * 70}")
    for idx, s in enumerate(sessions):
        marker = " ⚠ INVERTED" if _is_inverted(s) else ""
        print(f"  [{idx:3d}] {_format_window(s)}{marker}")
        if full:
            detail = _format_full(s)
            if detail:
                print(detail)


def _compare_one(marker: str, full: bool) -> bool:
    print(f"\n{'═' * 74}")
    print(f"  {marker}")
    print(f"{'═' * 74}")

    legacy = _run_legacy(marker)
    new = _run_new(marker)

    _print_chain("LEGACY  (gps_metadata)        ", legacy, full)
    _print_chain("NEW     (station_sessions)    ", new, full)

    # Quick verdict
    legacy_windows = {
        (_iso(s.get("time_from")), _iso(s.get("time_to"))) for s in legacy
    }
    new_windows = {(_iso(s.get("time_from")), _iso(s.get("time_to"))) for s in new}

    print(f"\n  {'─' * 70}")
    if legacy_windows == new_windows:
        print(f"  ✓ Window sets match ({len(new_windows)} sessions agree on bounds)")
        return True

    only_legacy = legacy_windows - new_windows
    only_new = new_windows - legacy_windows
    print(
        f"  ✗ Window sets differ: "
        f"{len(only_legacy)} legacy-only, {len(only_new)} new-only"
    )
    if only_legacy:
        print(f"\n  Legacy-only windows ({len(only_legacy)}):")
        for tf, tt in sorted(only_legacy):
            print(f"    {tf:>19s} → {tt:<19s}")
    if only_new:
        print(f"\n  New-only windows ({len(only_new)}):")
        for tf, tt in sorted(only_new):
            print(f"    {tf:>19s} → {tt:<19s}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("markers", nargs="+", help="Station markers (e.g. AUST RHOF)")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include per-session subtype slots (receiver/antenna/radome/monument)",
    )
    args = parser.parse_args()

    results = []
    for marker in args.markers:
        ok = _compare_one(marker.upper(), full=args.full)
        results.append((marker.upper(), ok))

    print(f"\n{'═' * 74}")
    print("  Summary")
    print(f"{'═' * 74}")
    for marker, ok in results:
        verdict = "✓ match" if ok else "✗ diverge"
        print(f"  {marker:<8s}  {verdict}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
