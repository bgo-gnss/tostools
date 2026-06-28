"""Fleet sweep for KRIV-class TOS metadata defects (CHEAP — no archive walk).

Uses ``stations.cfg`` as the "current reality" reference (it tracks the live
receiver/antenna) and compares it to TOS. Per station, flags:

  (#1)  ``rx_type_mismatch``    — cfg ``receiver_type`` != TOS open
        ``gnss_receiver`` model (stale TOS, e.g. KRIV's Trimble while
        cfg=PolaRX5).
  (#1b) ``rx_serial_mismatch``  — cfg ``receiver_serial`` != TOS open serial
        (only when the TOS serial isn't synthetic and the types agree).
  (#3)  ``synthetic_rx_serial`` — TOS serial is a placeholder
        (``receiver-XXXX-YYYYMMDD`` / ``0000000000`` / non-numeric).
  (#4)  ``antenna_split``       — one antenna device with consecutive touching
        joins to the SAME station (artificial split).

Archive timelines (#2 multi-segment) are a per-station deep-dive on flagged
stations, not part of this cheap fleet pass. Read-only.

This module is the importable core behind ``tos audit fleet-sweep``. The
detection logic is a faithful port of the standalone
``gps-tos-corrections/fleet_sweep/sweep.py`` script, validated against the
live IMO fleet (2026-06).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .receiver_timeline import _norm_type

# Synthetic-identifier prefix: ``receiver-``/``antenna-``/``radome-``/``monument-``.
_SYNTH = re.compile(r"(?i)^(receiver|antenna|radome|monument)-")

# stations.cfg section header — a 4-char uppercase/digit marker, e.g. ``[KRIV]``.
_SECTION = re.compile(r"^\[([A-Z0-9]{4})\]")


def parse_stations_cfg(path: str) -> Dict[str, Dict[str, str]]:
    """Parse ``stations.cfg`` into ``marker -> {key: value}``.

    A faithful port of ``sweep.py``'s ``_parse_cfg``: a hand-rolled INI
    reader (NOT :mod:`configparser`) so section-selection and key-handling
    semantics match the validated script byte-for-byte. Only sections whose
    header is a 4-char ``[A-Z0-9]{4}`` marker are kept; ``#``-comment lines
    and lines without ``=`` are skipped.
    """
    with open(path, encoding="utf-8", errors="ignore") as fh:
        txt = fh.read()
    out: Dict[str, Dict[str, str]] = {}
    cur: Optional[str] = None
    for line in txt.splitlines():
        m = _SECTION.match(line)
        if m:
            cur = m.group(1)
            if cur is not None:  # group 1 is mandatory; guard narrows for type-checkers
                out[cur] = {}
            continue
        if cur and "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[cur][k.strip()] = v.strip()
    return out


def _synthetic(serial: Any) -> bool:
    """True when a serial is a TOS placeholder, not a real hardware serial."""
    if not serial:
        return False
    s = str(serial).strip()
    return bool(_SYNTH.match(s)) or s == "0000000000" or not re.search(r"\d", s)


def _attr(ch: Dict[str, Any], code: str) -> Optional[Any]:
    """Pull the value of attribute ``code`` from an entity-history dict."""
    return next(
        (a.get("value") for a in ch.get("attributes", []) if a.get("code") == code),
        None,
    )


def _nt(x: Optional[str]) -> Optional[str]:
    """Canonicalize to the IGS receiver name so cfg short-names (NetR5,
    PolaRX5) and TOS/IGS names (TRIMBLE NETR5, SEPT POLARX5) compare equal."""
    return _norm_type(x) if x else None


def analyze(client: Any, marker: str, cfg: Dict[str, str]) -> Dict[str, Any]:
    """Compare one station's cfg row against its TOS history.

    ``client`` must expose ``find_station_by_marker(marker)`` and
    ``get_entity_history(id_entity)`` (a ``TOSWriter`` in dry-run, or any
    duck-typed equivalent). Returns the per-station result dict described in
    the module docstring; never raises for a missing-station / missing-receiver
    case (those become ``note`` entries with empty ``flags``).
    """
    out: Dict[str, Any] = {"marker": marker, "flags": []}
    eid = client.find_station_by_marker(marker)
    if eid is None:
        out["note"] = "no_tos_station"
        return out
    hist = client.get_entity_history(eid)
    children = (hist or {}).get("children_connections") or []

    open_rx = rx_serial = rx_type = None
    ant_by_dev: Dict[Any, Dict[str, Any]] = {}
    for c in children:
        cid = c.get("id_entity_child")
        ch = client.get_entity_history(int(cid))
        if not isinstance(ch, dict):
            continue
        sub = ch.get("code_entity_subtype")
        if sub == "gnss_receiver" and c.get("time_to") is None:
            open_rx = cid
            rx_serial = _attr(ch, "serial_number")
            rx_type = _attr(ch, "model")
        if sub == "antenna":
            ant_by_dev.setdefault(cid, {"joins": [], "ch": ch})["joins"].append(c)

    cfg_rx_type = cfg.get("receiver_type")
    cfg_rx_serial = cfg.get("receiver_serial")

    if open_rx is None:
        out["note"] = "no_open_tos_receiver"
    else:
        out["tos_open_rx"] = f"{rx_type}/{rx_serial}"
        out["cfg_rx"] = f"{cfg_rx_type}/{cfg_rx_serial}"
        # (#1) type mismatch — cfg is current reality
        if cfg_rx_type and _nt(cfg_rx_type) != _nt(rx_type):
            out["flags"].append(f"rx_type_mismatch:tos={rx_type}|cfg={cfg_rx_type}")
        # (#3) synthetic serial
        if _synthetic(rx_serial):
            out["flags"].append(f"synthetic_rx_serial:{rx_serial}")
        # (#1b) serial mismatch (only when TOS serial isn't synthetic and types agree)
        elif (
            cfg_rx_serial
            and _nt(cfg_rx_type) == _nt(rx_type)
            and str(cfg_rx_serial).strip() != str(rx_serial).strip()
        ):
            out["flags"].append(
                f"rx_serial_mismatch:tos={rx_serial}|cfg={cfg_rx_serial}"
            )

    # (#4) antenna split — same device, consecutive touching joins to this station
    for cid, ent in ant_by_dev.items():
        joins = sorted(ent["joins"], key=lambda x: (x.get("time_from") or ""))
        sn = _attr(ent["ch"], "serial_number")
        for a, b in zip(joins, joins[1:]):
            if a.get("time_to") and a.get("time_to") == b.get("time_from"):
                out["flags"].append(
                    f"antenna_split:dev{cid}@{str(a.get('time_to'))[:10]}(sn {sn})"
                )
    return out


def run_fleet_sweep(
    client: Any,
    markers: List[str],
    cfg: Dict[str, Dict[str, str]],
    *,
    only_diffs: bool = False,
    progress: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Run the sweep over ``markers``, returning a list of per-station dicts.

    Each station is analyzed in isolation: an exception during analysis is
    caught and turned into ``{"marker", "error", "flags": []}`` so one bad
    station can't abort a 173-station sweep.

    ``progress`` — optional callable invoked as ``progress(index, total,
    marker, result)`` after each station (for a stderr progress line).
    ``only_diffs`` — when True, results with no flags are dropped.
    """
    results: List[Dict[str, Any]] = []
    total = len(markers)
    for i, m in enumerate(markers, 1):
        try:
            r = analyze(client, m, cfg.get(m, {}))
        except Exception as e:  # noqa: BLE001 — isolate per-station failures
            r = {"marker": m, "error": str(e)[:120], "flags": []}
        if progress is not None:
            progress(i, total, m, r)
        if only_diffs and not r.get("flags"):
            continue
        results.append(r)
    return results


def summarize(results: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Group flagged markers by flag category (the part before ``:``)."""
    by: Dict[str, List[str]] = {}
    for r in results:
        for f in r.get("flags", []):
            by.setdefault(f.split(":")[0], []).append(r["marker"])
    return by


def format_report(results: List[Dict[str, Any]], *, total: int) -> str:
    """Pretty-print the sweep results as plain text (mirrors sweep.py output)."""
    lines: List[str] = []
    for r in results:
        tag = ",".join(r.get("flags", [])) or r.get("note") or r.get("error") or "clean"
        lines.append(f"{r['marker']}: {tag}")
    flagged = [r for r in results if r.get("flags")]
    lines.append("")
    lines.append(f"=== {len(flagged)}/{total} stations flagged")
    for k, v in sorted(summarize(flagged).items()):
        lines.append(f"  {k}: {len(v)}  {', '.join(sorted(set(v)))}")
    return "\n".join(lines)
