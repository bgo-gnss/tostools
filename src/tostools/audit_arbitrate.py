"""``tos audit duplicate-serials --arbitrate`` — classify a duplicate-serial group.

``duplicate-serials`` finds entities that share a serial; ``--arbitrate`` decides
what that *means* by comparing each entity's **archive-attested** deployment
periods (from ``rinex-timeline``), then recommends the fix. It is READ-ONLY —
it emits a verdict + the exact command sequence a human runs behind the merge
verb's own dry-run/guards.

Verdicts:

* ``rove`` — the serial appears at each entity's station **sequentially, without
  overlap** in the real archive data → one physical unit recorded as N entities
  (create-instead-of-move). Recommend a merge onto the canonical survivor.
* ``collision`` — the serial appears at ≥2 stations **overlapping** in the
  archive → genuinely two units (or a header-config error); a merge would be
  wrong. Flag for manual work.
* ``junk`` — the shared serial is a placeholder (``receiver-*`` / all-same-digit).
* ``inconclusive`` — the serial could **not** be positively confirmed in the
  archive at one or more legs (sparse/unreadable data, or that station shows a
  *different* serial for the period). NEVER treated as rove — a rove verdict
  drives a destructive merge, so absence of confirmation blocks it (same
  asymmetry as device-find / firmware-chain).

Why archive-periods and not TOS join windows: the duplicate exists *because* the
TOS dates are wrong (the ODDF case: 18973's GRVC join reads 2020-06→2022-06, but
the archive shows 2021-11→2021-12). So the archive is the arbiter; TOS joins are
only used to know *which stations* to read.

The pure core (:func:`classify_arbitration`) takes per-leg archive periods and is
unit-testable offline; the CLI gathers the legs live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple

from .audit_duplicate_serials import _is_placeholder

ROVE = "rove"
COLLISION = "collision"
JUNK = "junk"
INCONCLUSIVE = "inconclusive"

# A (date_from, date_to) pair; date_to None = open/ongoing.
Period = Tuple[str, Optional[str]]


@dataclass
class ArbitrationLeg:
    """One entity in the duplicate group, with its archive-confirmed periods."""

    entity_id: int
    tos_serial: str
    station: Optional[str]
    archive_periods: List[Period] = field(default_factory=list)
    """(date_from, date_to) spans where the GROUP serial was positively seen in
    this entity's station archive. Empty ⇒ unconfirmed (drives INCONCLUSIVE)."""
    archive_serial_seen: Optional[str] = None
    """What serial the station's archive actually shows for this entity's period,
    when it differs from the group serial — the likely-typo signal."""

    @property
    def confirmed(self) -> bool:
        return bool(self.archive_periods)

    @property
    def earliest_start(self) -> Optional[str]:
        starts = [p[0] for p in self.archive_periods if p[0]]
        return min(starts) if starts else None


@dataclass
class ArbitrationVerdict:
    serial: str
    verdict: str
    reason: str
    survivor_id: Optional[int] = None
    loser_ids: List[int] = field(default_factory=list)
    legs: List[ArbitrationLeg] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "serial": self.serial,
            "verdict": self.verdict,
            "reason": self.reason,
            "survivor_id": self.survivor_id,
            "loser_ids": self.loser_ids,
            "legs": [
                {
                    "entity_id": leg.entity_id,
                    "tos_serial": leg.tos_serial,
                    "station": leg.station,
                    "archive_periods": leg.archive_periods,
                    "archive_serial_seen": leg.archive_serial_seen,
                    "confirmed": leg.confirmed,
                }
                for leg in self.legs
            ],
            "recommended_steps": self.steps,
        }


def _overlaps(a: Period, b: Period) -> bool:
    """True when two date spans overlap; a None end is treated as open (max date)."""
    a0 = date.fromisoformat(a[0][:10])
    a1 = date.fromisoformat(a[1][:10]) if a[1] else date.max
    b0 = date.fromisoformat(b[0][:10])
    b1 = date.fromisoformat(b[1][:10]) if b[1] else date.max
    return a0 < b1 and b0 < a1


def _any_cross_leg_overlap(legs: List[ArbitrationLeg]) -> bool:
    """Any two periods on DIFFERENT legs overlapping = same serial, two places at once."""
    for i, li in enumerate(legs):
        for lj in legs[i + 1 :]:
            for pa in li.archive_periods:
                for pb in lj.archive_periods:
                    if _overlaps(pa, pb):
                        return True
    return False


def _pick_survivor(legs: List[ArbitrationLeg]) -> ArbitrationLeg:
    """Canonical entity to keep: earliest archive period, tie-broken by a clean
    (untrimmed, non-placeholder) serial then lowest entity id."""
    return sorted(
        legs,
        key=lambda leg: (
            leg.earliest_start or "9999-99-99",
            leg.tos_serial != leg.tos_serial.strip(),  # trailing junk sorts last
            leg.entity_id,
        ),
    )[0]


def classify_arbitration(serial: str, legs: List[ArbitrationLeg]) -> ArbitrationVerdict:
    """Classify a duplicate-serial group from its per-leg archive periods."""
    if _is_placeholder(serial):
        return ArbitrationVerdict(
            serial=serial,
            verdict=JUNK,
            reason="shared serial is a placeholder, not a real hardware serial",
            legs=legs,
        )

    if len(legs) < 2:
        return ArbitrationVerdict(
            serial=serial,
            verdict=INCONCLUSIVE,
            reason=f"{len(legs)} entity in group — nothing to arbitrate",
            legs=legs,
        )

    unconfirmed = [leg for leg in legs if not leg.confirmed]
    if unconfirmed:
        bits = []
        for leg in unconfirmed:
            seen = (
                f" (archive shows {leg.archive_serial_seen!r})"
                if leg.archive_serial_seen
                else ""
            )
            bits.append(f"id {leg.entity_id} @ {leg.station or '?'}{seen}")
        return ArbitrationVerdict(
            serial=serial,
            verdict=INCONCLUSIVE,
            reason=(
                "serial not confirmed in the archive at: "
                + "; ".join(bits)
                + " — cannot merge (a leg may be a different unit / mis-typed serial)"
            ),
            legs=legs,
        )

    if _any_cross_leg_overlap(legs):
        return ArbitrationVerdict(
            serial=serial,
            verdict=COLLISION,
            reason=(
                "serial confirmed at ≥2 stations with OVERLAPPING archive periods "
                "— two physical units share this serial (or a header is misconfigured); "
                "a merge would be wrong"
            ),
            legs=legs,
        )

    # rove: all confirmed, no overlap → one unit, sequential deployments.
    survivor = _pick_survivor(legs)
    losers = [leg for leg in legs if leg.entity_id != survivor.entity_id]
    return ArbitrationVerdict(
        serial=serial,
        verdict=ROVE,
        reason=(
            "serial confirmed at each station with NON-overlapping archive periods "
            "— one roving unit recorded as "
            f"{len(legs)} entities; consolidate onto the canonical survivor"
        ),
        survivor_id=survivor.entity_id,
        loser_ids=[leg.entity_id for leg in losers],
        legs=legs,
        steps=_rove_steps(survivor, losers),
    )


def _rove_steps(survivor: ArbitrationLeg, losers: List[ArbitrationLeg]) -> List[str]:
    """The 3-step, human-run merge sequence for a rove (per loser)."""
    steps: List[str] = [
        f"# survivor = id {survivor.entity_id} "
        f"(earliest leg {survivor.earliest_start}, clean serial "
        f"{survivor.tos_serial.strip()!r})",
    ]
    for loser in losers:
        cutover = loser.earliest_start or "<loser-install-date>"
        if loser.tos_serial != survivor.tos_serial:
            steps.append(
                f"# 1. correct id {loser.entity_id} serial "
                f"{loser.tos_serial!r} → {survivor.tos_serial.strip()!r} "
                "(merge refuses non-identical serials)"
            )
        steps.append(
            f"tos device merge --from {loser.entity_id} "
            f"--into {survivor.entity_id} --at {cutover}   # grafts the "
            f"{loser.station or '?'} leg, then deletes {loser.entity_id}"
        )
        periods = ", ".join(
            f"{p[0][:10]}→{(p[1][:10] if p[1] else 'open')}"
            for p in loser.archive_periods
        )
        steps.append(
            f"# 3. patch the grafted {loser.station or '?'} join to the archive "
            f"dates: {periods} (TOS join dates were wrong — the reason for the dup)"
        )
    return steps


def format_report(v: ArbitrationVerdict) -> str:
    lines = [f"serial {v.serial!r}: {v.verdict.upper()}", f"  {v.reason}"]
    for leg in v.legs:
        role = (
            "survivor"
            if leg.entity_id == v.survivor_id
            else ("loser" if leg.entity_id in v.loser_ids else "leg")
        )
        if leg.archive_periods:
            spans = ", ".join(
                f"{p[0][:10]}→{(p[1][:10] if p[1] else 'open')}"
                for p in leg.archive_periods
            )
        else:
            spans = f"UNCONFIRMED{f' (archive={leg.archive_serial_seen!r})' if leg.archive_serial_seen else ''}"
        lines.append(
            f"    [{role}] id={leg.entity_id} serial={leg.tos_serial!r} "
            f"@ {leg.station or '?'}: {spans}"
        )
    if v.steps:
        lines.append("  recommended (run behind the merge verb's own dry-run):")
        lines.extend(f"    {s}" for s in v.steps)
    return "\n".join(lines)
