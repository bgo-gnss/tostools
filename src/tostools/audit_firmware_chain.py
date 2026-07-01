"""``tos audit firmware-chain`` — reconstruct a receiver's firmware history.

Mines the archive firmware timeline (``rinex-timeline --field firmware``) for a
station's CURRENT receiver, normalizes + merges it, tiers it by how defensibly it
reconstructs, and emits a triage file (``delete-attribute-value`` the stale TOS
firmware rows + ``add-attribute-period`` one per firmware period) for
``tos audit apply``.

Tiers:

* ``single`` — one firmware period → one open period (skipped if TOS already holds it).
* ``clean`` — strictly-increasing distinct semver, no suspiciously-short middle
  period → full chain, auto-appliable.
* ``netrs`` — current receiver is a Trimble NetRS; its ``1.x`` VERS headers flip
  notation and can't be chained → current-period-only, using ``--probe-value`` (the
  operator's live-probed firmware) dated from the unit's first archive day.
* ``anomaly`` — non-monotonic or short-period → emitted **commented** for manual verify.

Safety — this triage is destructive by construction (it deletes every existing
firmware row). These cases force **commented** (not auto-appliable) output:

1. ``anomaly`` tier.
2. **Serial truncation** — the current unit's serial is written two ways across its
   own tenure (Trimble ``20147817`` vs ``7817``). We keep the WHOLE unit (one chain
   from the earliest day, via a truncation-aware tenure walk) rather than dropping
   the earlier-spelling periods — but flag it, because merging two spellings is a
   guess, not a fact.
3. **TOS already multi-period** — TOS holds a curated chain, not one stale value;
   blind delete-all-rebuild would replace good data with gap-prone archive data.
4. **netrs without ``--probe-value``** — the fallback would be the 1.x header the
   tier itself calls unreliable; only a probe value makes netrs auto-appliable.
5. **Unreadable current serial** — if the anchor (latest) segment has no serial,
   unit identity is unconfirmed and every earlier same-type segment over-merges
   as a wildcard.

Read-only vs TOS (writes only the triage file). The pure core
(:func:`build_firmware_chain`) takes an injected ``timeline`` + ``tos_receiver`` so
it is unit-testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .receiver_timeline import (
    ReceiverSegment,
    _norm_fw,
    _norm_serial,
    _norm_type,
)

_TIER_SINGLE = "single"
_TIER_CLEAN = "clean"
_TIER_NETRS = "netrs"
_TIER_ANOMALY = "anomaly"
_TIER_SKIP = "skip"

# A firmware period shorter than this in the MIDDLE of a chain is glitch-suspect
# (a one-file header error re-flashing an intermediate version) → anomaly.
_MIN_MIDDLE_DAYS = 50


# --------------------------------------------------------------------------- model
@dataclass
class FirmwarePeriod:
    value: str
    rec_type: Optional[str]
    rec_serial: Optional[str]
    date_from: str  # ISO YYYY-MM-DD
    date_to: Optional[str]  # ISO YYYY-MM-DD, or None = open


@dataclass
class TosReceiver:
    """The station's open TOS gnss_receiver, resolved by the CLI (injected here)."""

    id_entity: int
    serial: Optional[str]
    fw_rows: List[Dict[str, Any]]
    """Every ``firmware_version`` attribute period (open + closed), each carrying
    ``id_attribute_value`` / ``value`` / ``date_from`` / ``date_to``."""

    @property
    def open_value(self) -> Optional[str]:
        for r in self.fw_rows:
            if r.get("date_to") is None:
                return r.get("value")
        return None

    @property
    def is_multiperiod(self) -> bool:
        return len(self.fw_rows) > 1


@dataclass
class FirmwareChainResult:
    station: str
    tier: str
    reason: str
    id_entity: Optional[int] = None
    tos_current_value: Optional[str] = None
    periods: List[FirmwarePeriod] = field(default_factory=list)
    action_lines: List[str] = field(default_factory=list)
    commented: bool = False
    truncation_detected: bool = False
    tos_multiperiod: bool = False

    @property
    def is_actionable(self) -> bool:
        """True when the ACTION lines are safe to apply as written.

        Invariant the CLI relies on for exit codes: actionable ⇒ real ACTION lines,
        NOT commented, and a real (non-skip) tier.
        """
        return (
            bool(self.action_lines) and not self.commented and self.tier != _TIER_SKIP
        )

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "station": self.station,
            "tier": self.tier,
            "reason": self.reason,
            "id_entity": self.id_entity,
            "tos_current_value": self.tos_current_value,
            "commented": self.commented,
            "truncation_detected": self.truncation_detected,
            "tos_multiperiod": self.tos_multiperiod,
            "actionable": self.is_actionable,
            "periods": [
                {
                    "value": p.value,
                    "date_from": p.date_from,
                    "date_to": p.date_to,
                    "rec_type": p.rec_type,
                    "rec_serial": p.rec_serial,
                }
                for p in self.periods
            ],
            "action_lines": self.action_lines,
        }


# ------------------------------------------------------------------- unit selection
def _serial_is_truncation(a: str, b: str) -> bool:
    """True when one serial is a tail-truncation of the other (Trimble drift).

    ``20147817`` vs ``7817`` — the shorter is a suffix of the longer. Requires
    ≥4 shared trailing chars so unrelated short serials don't false-match.
    """
    short, lng = sorted([a, b], key=len)
    return len(short) >= 4 and lng.endswith(short)


def _serial_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """Same physical serial allowing an unknown wildcard or truncation drift."""
    na, nb = _norm_serial(a), _norm_serial(b)
    if na is None or nb is None:
        return True
    if na == nb:
        return True
    return _serial_is_truncation(na, nb)


def current_unit_segments(
    timeline: List[ReceiverSegment],
) -> tuple[List[ReceiverSegment], bool]:
    """The trailing run of segments belonging to the CURRENT physical receiver.

    Walks back from the last segment while the type matches and the serial is
    :func:`_serial_compatible` (equal / wildcard / truncation). Returns
    ``(segments, truncation_detected)`` — ``truncation_detected`` is True when the
    run spans two distinct real serial spellings (kept as one unit, but flagged
    because that merge is a heuristic). Firmware-only boundaries are naturally
    included (same type+serial). A genuine different serial ends the run.
    """
    if not timeline:
        return [], False
    anchor = timeline[-1].header
    run = [timeline[-1]]
    truncation = False
    for seg in reversed(timeline[:-1]):
        h = seg.header
        if _norm_type(h.rtype) != _norm_type(anchor.rtype):
            break
        if not _serial_compatible(h.serial, anchor.serial):
            break
        a, b = _norm_serial(h.serial), _norm_serial(anchor.serial)
        if a is not None and b is not None and a != b:
            truncation = True  # compatible only via truncation, not equality
        run.append(seg)
    run.reverse()
    return run, truncation


def clean_merge(segments: List[ReceiverSegment]) -> List[FirmwarePeriod]:
    """Normalize each segment's firmware + merge consecutive equal-normalized ones.

    The receiver timeline already merged on the FULL key (type, serial, fw), but a
    truncation-drift run can hold two segments with the same firmware and different
    serial spellings — genuinely one firmware period. So re-merge on normalized
    firmware alone. Period ``date_to`` is made contiguous (next period's start;
    the last stays open).
    """
    merged: List[FirmwarePeriod] = []
    for seg in segments:
        value = _norm_fw(seg.header.firmware) or (seg.header.firmware or "")
        if merged and merged[-1].value == value:
            continue  # same firmware continues; date_to fixed up below
        merged.append(
            FirmwarePeriod(
                value=value,
                rec_type=seg.header.rtype,
                rec_serial=seg.header.serial,
                date_from=seg.start.isoformat(),
                date_to=None,
            )
        )
    for i in range(len(merged) - 1):
        merged[i].date_to = merged[i + 1].date_from
    return merged


# --------------------------------------------------------------------- classify
def _vtuple(v: str) -> Optional[tuple]:
    """Parse a clean semver-ish version to a comparable tuple, or None if messy."""
    import re

    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-?patch(\d+))?$", v.strip().lower())
    if not m:
        return None
    return (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3) or 0),
        int(m.group(4) or 0),
    )


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def classify(periods: List[FirmwarePeriod]) -> str:
    """Tier a merged firmware chain: single / clean / netrs / anomaly."""
    if len(periods) == 1:
        return _TIER_SINGLE
    rt = (periods[-1].rec_type or "").upper()
    if "NETRS" in rt:
        return _TIER_NETRS
    tups = [_vtuple(p.value) for p in periods]
    if any(t is None for t in tups):
        return _TIER_NETRS if "NETR" in rt else _TIER_ANOMALY
    for a, b in zip(tups, tups[1:]):
        if b <= a:  # type: ignore[operator]
            return _TIER_ANOMALY
    for p in periods[:-1]:
        if p.date_to and _days_between(p.date_from, p.date_to) < _MIN_MIDDLE_DAYS:
            return _TIER_ANOMALY
    return _TIER_CLEAN


# ------------------------------------------------------------------- build + render
def _norm(s: Optional[str]) -> str:
    return (s or "").strip().upper()


def build_firmware_chain(
    station: str,
    tos_receiver: TosReceiver,
    *,
    timeline: List[ReceiverSegment],
    probe_value: Optional[str] = None,
) -> FirmwareChainResult:
    """Reconstruct the current receiver's firmware chain into an apply-ready triage.

    Args:
        station: Marker (for labels).
        tos_receiver: The station's open TOS receiver (id + serial + fw rows),
            resolved + injected by the caller.
        timeline: Receiver/firmware segments from
            :func:`receiver_timeline.build_receiver_timeline` (injected).
        probe_value: Operator-supplied live firmware, used for the ``netrs`` tier.
    """
    sid = station.upper()
    segments, truncation = current_unit_segments(timeline)
    if not segments:
        return FirmwareChainResult(
            station=sid,
            tier=_TIER_SKIP,
            reason="no archive firmware periods",
            id_entity=tos_receiver.id_entity,
            tos_current_value=tos_receiver.open_value,
        )

    periods = clean_merge(segments)
    archive_serial = segments[-1].header.serial

    if not _serial_compatible(archive_serial, tos_receiver.serial):
        return FirmwareChainResult(
            station=sid,
            tier=_TIER_SKIP,
            reason=(
                f"serial mismatch: archive {archive_serial!r} vs TOS "
                f"{tos_receiver.serial!r} (possible truncation drift, or the wrong "
                "receiver is open in TOS) — resolve before chaining"
            ),
            id_entity=tos_receiver.id_entity,
            tos_current_value=tos_receiver.open_value,
            truncation_detected=truncation,
        )

    tier = classify(periods)
    tos_multiperiod = tos_receiver.is_multiperiod
    # A netrs chain is only trustworthy with an operator-supplied probe value —
    # without one it would fall back to the 1.x header string the tier itself
    # declares unreliable. And if the CURRENT (anchor) segment has no readable
    # serial, unit identity can't be confirmed: every earlier same-type segment
    # joins as a wildcard, silently over-merging possibly-distinct receivers.
    netrs_no_probe = tier == _TIER_NETRS and not probe_value
    anchor_unknown = _norm_serial(segments[-1].header.serial) is None
    # commented (manual-review) whenever the rebuild is a guess or would clobber a
    # curated chain — see the module docstring's safety cases.
    commented = (
        tier == _TIER_ANOMALY
        or truncation
        or tos_multiperiod
        or netrs_no_probe
        or anchor_unknown
    )

    dels = [
        f"ACTION {tos_receiver.id_entity} delete-attribute-value "
        f"{r.get('id_attribute_value')}"
        for r in tos_receiver.fw_rows
    ]

    # Base result carrying the fields common to every tier; each branch fills in
    # tier / reason / action_lines / commented.
    result = FirmwareChainResult(
        station=sid,
        tier=tier,
        reason="",
        id_entity=tos_receiver.id_entity,
        tos_current_value=tos_receiver.open_value,
        periods=periods,
        truncation_detected=truncation,
        tos_multiperiod=tos_multiperiod,
    )

    def _add(period_value: str, date_from: str, date_to: str) -> str:
        return (
            f"ACTION {tos_receiver.id_entity} add-attribute-period "
            f"firmware_version {period_value} {date_from} {date_to}"
        )

    if tier == _TIER_SINGLE:
        only = periods[0]
        if (
            not truncation
            and not tos_multiperiod
            and _norm(tos_receiver.open_value) == _norm(only.value)
        ):
            result.tier = "single-ok"
            result.reason = f"TOS already holds {tos_receiver.open_value!r}"
            return result
        result.reason = f"single firmware period {only.value}"
        result.action_lines = dels + [_add(only.value, only.date_from, "open")]
        result.commented = commented
        return result

    if tier == _TIER_NETRS:
        ver = probe_value or periods[-1].value
        result.reason = (
            "NetRS 1.x VERS headers are unreliable — current-period-only using "
            f"{'probe' if probe_value else 'last-archive'} value {ver!r}"
        )
        result.action_lines = dels + [_add(ver, periods[0].date_from, "open")]
        result.commented = commented
        return result

    # clean or anomaly → full chain
    result.action_lines = list(dels) + [
        _add(p.value, p.date_from, p.date_to or "open") for p in periods
    ]
    result.reason = (
        f"{len(periods)}-period chain " + "/".join(p.value for p in periods)
        if tier == _TIER_CLEAN
        else "non-monotonic or short-period chain — MANUAL VERIFY"
    )
    result.commented = commented
    return result


def render_triage(result: FirmwareChainResult) -> List[str]:
    """Render the triage file body: header comments + (commented?) ACTION lines."""
    lines: List[str] = [
        f"# {result.station} firmware chain — tier={result.tier}",
        f"# {result.reason}",
    ]
    if result.truncation_detected:
        lines.append(
            "# ⚠ SERIAL TRUNCATION: the current unit's serial is written two ways "
            "across its tenure; the chain merges them as one unit — VERIFY."
        )
    if result.tos_multiperiod:
        lines.append(
            "# ⚠ TOS already holds a MULTI-PERIOD firmware chain; delete-all-rebuild "
            "would replace curated data with archive-derived data — VERIFY."
        )
    if result.commented:
        lines.append("# ACTIONS COMMENTED — do NOT apply as-is; verify then uncomment.")
    prefix = "# " if result.commented else ""
    for a in result.action_lines:
        lines.append(prefix + a)
    return lines


def format_report(result: FirmwareChainResult) -> str:
    """Human-readable summary of a firmware-chain result."""
    lines = [f"{result.station} firmware-chain: {result.tier} — {result.reason}"]
    if result.periods:
        for p in result.periods:
            dt = p.date_to or "open"
            lines.append(f"  {p.date_from} → {dt}  fw={p.value}")
    if result.action_lines:
        lines.append(
            f"  {len(result.action_lines)} ACTION line(s)"
            + (" (COMMENTED — manual review)" if result.commented else "")
        )
    else:
        lines.append("  no actions (nothing to do)")
    return "\n".join(lines)
