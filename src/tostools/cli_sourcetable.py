"""``tosGPS sourcetable`` — regenerate the NTRIP caster sourcetable.

Generate + diff only: reads a copy of the live ``sourcetable.dat`` and the
mountpoint inventory gathered on the caster host, measures every mountpoint,
and writes three files to ``--output-dir`` — the candidate table, a unified
diff against the live one, and a report. Nothing is written to the caster.
See :mod:`tostools.ntrip_sourcetable` for the field rules.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Set

from . import ntrip_sourcetable as nst

logger = logging.getLogger(__name__)

EPILOG = """
Inventory files are one mountpoint per line, collected on the caster host
(names only — ntripcaster.conf and *.aut hold passwords):

  ssh gpsops@ntrcaster.vedur.is 'cat /etc/ntripcaster/sourcetable.dat' > current.dat
  ssh gpsops@ntrcaster.vedur.is 'awk -F: "!/^#/ && NF>1 {print \\$1}" \\
      /etc/ntripcaster/clientmounts.aut' | tr -d / > aut.mounts

Examples:
  tosGPS sourcetable --current current.dat --mounts all.mounts \\
      --auth-mounts aut.mounts --exclude TEST0 TEST1 -o out/
  tosGPS sourcetable --current current.dat --only SKSH0 THNA0 -o out/
"""


def add_parser(subparsers) -> argparse.ArgumentParser:
    p = subparsers.add_parser(
        "sourcetable",
        help="Regenerate the NTRIP caster sourcetable (generate + diff only)",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--current", required=True, type=Path, help="Copy of the live sourcetable.dat"
    )
    p.add_argument(
        "--mounts",
        type=Path,
        help="Mountpoint inventory (default: the current table's STR lines)",
    )
    p.add_argument(
        "--auth-mounts",
        type=Path,
        help="Mounts listed in clientmounts.aut → auth B, others N "
        "(default: keep each line's previous value, B for new lines)",
    )
    p.add_argument("--exclude", nargs="*", default=[], help="Mounts to leave out")
    p.add_argument(
        "--only",
        nargs="*",
        help="Regenerate only these mounts; every other line is copied verbatim",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Seconds to sample each stream (default 60; ephemerides need ≥60)",
    )
    p.add_argument(
        "--workers", type=int, default=12, help="Concurrent probes (default 12)"
    )
    p.add_argument(
        "--credentials",
        type=Path,
        help="receivers.cfg holding [ntrip_defaults] (default: $GPS_CONFIG_PATH "
        "or ~/.config/gpsconfig); NTRIP_USER/NTRIP_PASSWORD override",
    )
    p.add_argument(
        "-o", "--output-dir", type=Path, default=Path("."), help="Where to write"
    )
    return p


def _read_names(path: Optional[Path]) -> Optional[List[str]]:
    if path is None:
        return None
    names = []
    for line in path.read_text().splitlines():
        name = line.strip().lstrip("/")
        if name and not name.startswith("#"):
            names.append(name)
    return names


def _station_info(marker: str, url: str) -> Optional[nst.StationInfo]:
    from .gps_metadata_qc import gps_metadata_via_devices

    try:
        data = gps_metadata_via_devices(marker, url, loglevel=logging.ERROR)
    except Exception as exc:  # TOS miss / HTTP error: report, don't abort
        logger.debug("TOS lookup %s failed: %s", marker, exc)
        return None
    if not data or data.get("lat") is None or data.get("lon") is None:
        return None
    model = ""
    for session in data.get("device_history") or []:
        if session.get("time_to") is None:
            model = (session.get("gnss_receiver") or {}).get("model") or ""
    return nst.StationInfo(
        name=data.get("name") or marker,
        lat=data["lat"],
        lon=data["lon"],
        receiver_model=model,
    )


def run(args, url: str) -> int:
    current_text = args.current.read_text(encoding="latin-1")
    current = nst.parse_sourcetable(current_text)
    inventory = _read_names(args.mounts) or list(current.records)
    excluded = set(args.exclude)
    auth_listed: Optional[Set[str]] = (
        set(_read_names(args.auth_mounts) or []) if args.auth_mounts else None
    )

    mounts = sorted((set(inventory) | set(current.records)) - excluded)
    todo = [m for m in mounts if args.only is None or m in set(args.only)]
    host, port, user, password = nst.load_caster_credentials(args.credentials)
    # One TOS round-trip per marker; the client logs each request at INFO.
    logging.getLogger("tostools.api").setLevel(logging.WARNING)
    print(
        f"Probing {len(todo)} mountpoints on {host}:{port} for "
        f"{args.duration:.0f} s each ({args.workers} at a time)…",
        file=sys.stderr,
    )

    def probe(mount: str) -> nst.ProbeResult:
        return nst.probe_mountpoint(
            host, port, mount, user, password, duration_s=args.duration
        )

    markers = sorted({m[:4].upper() for m in todo})
    with ThreadPoolExecutor(args.workers) as ex:
        stations = dict(zip(markers, ex.map(lambda s: _station_info(s, url), markers)))
        probes = dict(zip(todo, ex.map(probe, todo)))

    records: Dict[str, List[str]] = {
        m: f for m, f in current.records.items() if m not in excluded
    }
    outcomes: Dict[str, List[str]] = defaultdict(list)
    for mount in todo:
        previous = current.records.get(mount)
        if auth_listed is not None:
            auth = "B" if mount in auth_listed else "N"
        else:
            auth = previous[15] if previous and len(previous) > 15 else "B"
        fields, outcome = nst.build_record(
            mount,
            previous=previous,
            probe=probes[mount],
            station=stations.get(mount[:4].upper()),
            auth=auth,
        )
        if fields is not None:
            records[mount] = fields
        outcomes[outcome].append(mount)

    new_table = nst.Sourcetable(current.header, records)
    new_text = nst.render_sourcetable(new_table)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "sourcetable.dat").write_text(new_text, encoding="latin-1")
    # The live file is unordered and the output is sorted; diffing against
    # the live file as-is would bury every content change in reorder noise.
    (out / "sourcetable.diff").write_text(
        nst.unified_diff(nst.render_sourcetable(current), new_text),
        encoding="latin-1",
    )
    report = _report(current, new_table, probes, stations, outcomes, excluded, args)
    (out / "sourcetable-report.txt").write_text(report)
    print(report)
    print(f"Wrote {out}/sourcetable.dat, sourcetable.diff, sourcetable-report.txt")
    return 0


def _report(current, new, probes, stations, outcomes, excluded, args) -> str:
    old_m, new_m = set(current.records), set(new.records)
    changed = sorted(m for m in old_m & new_m if current.records[m] != new.records[m])
    by_status: Dict[str, List[str]] = defaultdict(list)
    for mount, result in probes.items():
        by_status[result.status].append(mount)
    unrecognised = [
        m
        for m, r in probes.items()
        if r.summary is not None and r.summary.stream_format == "UNKNOWN"
    ]
    no_tos = sorted(m for m in probes if stations.get(m[:4].upper()) is None)

    def line(label: str, items) -> str:
        items = sorted(items)
        return f"{label} ({len(items)}): {' '.join(items) if items else '-'}"

    rows = [
        "NTRIP sourcetable regeneration report",
        f"  live table {len(old_m)} STR → generated {len(new_m)} STR"
        f"; sample {args.duration:.0f} s per stream",
        "  NOTE: the (n) after each message is now the measured update interval",
        "  in seconds (NTRIP spec); the old table carried copied message counts,",
        "  so nearly every existing line differs for that reason alone.",
        "",
        line("added", new_m - old_m),
        line("changed", changed),
        line("unchanged", old_m & new_m - set(changed)),
        "",
        line("probe live", by_status[nst.LIVE]),
        line("probe not fed", by_status[nst.NOT_FED]),
        line(
            "probe auth denied (login not in the mount's group)",
            by_status[nst.AUTH_DENIED],
        ),
        line("probe error", by_status[nst.ERROR]),
        "",
        line(
            "LIVE WITHOUT OBSERVATIONS — receiver fault, previous line kept",
            outcomes["no_obs"],
        ),
        line(
            "live, unrecognised format (e.g. Trimble RT17/RT27) — previous line kept",
            unrecognised,
        ),
        line("kept unmeasured (previous line, TOS fields refreshed)", outcomes["kept"]),
        line(
            "not added (new mount, not measurable or not in TOS)", outcomes["skipped"]
        ),
        line("no TOS station for marker", no_tos),
        line("excluded", excluded),
        line(
            "in table, not fed now (removal candidates — not removed)",
            (old_m - excluded) & set(by_status[nst.NOT_FED]),
        ),
    ]
    errors = [f"    {m}: {probes[m].detail}" for m in sorted(by_status[nst.ERROR])]
    return "\n".join(rows + errors) + "\n"
