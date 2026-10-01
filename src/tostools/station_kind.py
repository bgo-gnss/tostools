"""Is this TOS station entity the kind of station this tool is for?

``tos`` answers for every discipline — meteorological, hydrological,
geophysical. ``tosGPS`` answers for GPS only, and until now it had no way
to say so about a *station*: the GPS audits narrow which **attributes**
they grade, so pointing them at a non-GPS station made them pass
**vacuously**. ``tosGPS station verify VLFS`` reported ``✓ clean`` for a
1963 precipitation gauge with zero devices. A false PASS from the oracle
is worse than a wrong table — it is the oracle agreeing with you.

This module is the leaf the resolvers share. It deliberately imports
nothing from ``tostools``: the resolvers live in ``tos.py``, ``audit.py``,
``history.py`` and ``api/tos_writer.py``, and a predicate built from the
attribute catalog (:func:`tostools.search_selectors.gps_profile`) must be
passable to all four without dragging the catalog into the ``api`` import
graph. So a predicate here is **just a callable** over an entity mapping,
and tests can pass a lambda.

Two levels, and the predicate needs BOTH
----------------------------------------

===========================  ==========  ============  ===========
level                        VFLS (GPS)  VLFS (met)    SIL seismic
===========================  ==========  ============  ===========
``code_entity_subtype``      geophysical meteorological **geophysical**
``subtype`` attribute        GPS stöð    Úrkomustöð     SIL stöð
===========================  ==========  ============  ===========

* **Entity type alone is insufficient** — SIL seismic stations are
  ``geophysical`` too; the ``subtype`` attribute is what separates them
  (see :func:`tostools.location.summarize_location_children`).
* **The ``subtype`` attribute alone is circular** — ``missing-attributes``
  *audits* that very attribute (catalog default ``GPS stöð``), so gating
  the audit on it begs the question, and would lock out a real GPS
  station that is merely missing it.

Hence: entity type in scope **AND** (subtype attribute matches **OR** is
absent). The absent-leniency is what keeps the gate from pre-empting the
audit that exists to report it.

Filter candidates — never resolve-then-refuse
---------------------------------------------

The gate is applied while **choosing** among the marker's candidates, not
to the one a first-hit resolver already picked. That is not a stylistic
preference; markers are **not** unique in TOS, and the live fleet proves
all three outcomes:

* ``SOHO`` has **two geophysical entities** on marker ``soho`` — 5356
  (``DOAS``, a volcanic-gas station) and 4416 (``GPS stöð``, the real GPS
  station carrying receiver 3075357). Every first-hit resolver returns
  **5356**, so ``tosGPS station verify SOHO`` audits a gas station's
  monuments and the GPS station is never audited at all. Resolve-then-
  refuse would turn that into a *refusal* of a perfectly valid GPS
  station. Filtering picks 4416 — the only correct answer.
* ``AUST``, ``HLFJ``, ``HOFN``, ``KVSK`` each share their marker with a
  precipitation gauge. They resolve correctly **today only because the
  geophysical domain is searched first** — luck, not logic. Filtering
  makes them deterministic.
* ``BRST`` is *Brest, France* (``is_in_iceland = false``), an external IGS
  reference site with **no TOS entity**. The only candidate on marker
  ``brst`` is entity 646, *Berustaðir í Ásum* — an Icelandic weather
  station. Refusing is correct; returning 646 is what happens today.

Measured over all 344 ``stations.cfg`` markers (2026-10-01): 198 resolve
to exactly one admitted candidate (unchanged); 140 have no candidate at
all and already refuse today (136 ``is_reference_site`` entries plus
``ELAT``, ``LEBA``, ``UNIV`` and ``VCAP``, four cfg stations simply absent
from TOS); 5 have two candidates of which exactly **one** is admitted; and
1 (``BRST``) has a candidate but none admitted. **No marker admitted two
or more**, so there is no tie to break — if one ever appears,
:class:`AmbiguousStation` is raised rather than guessed.
"""

from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional, Sequence

#: A predicate over a station entity mapping. Receives something shaped
#: like a ``get_entity_history`` result or an ``/entity/search/station/``
#: hit — i.e. carrying ``code_entity_subtype`` and an ``attributes`` list
#: — and answers whether this tool should act on it. ``None`` anywhere a
#: resolver accepts one means "no gate", which is how ``tos`` and every
#: internal caller keep their exact previous behaviour.
StationPredicate = Callable[[Mapping[str, Any]], bool]


def open_attribute(entity: Mapping[str, Any], code: str) -> Optional[str]:
    """The value of ``entity``'s currently-open ``code`` attribute.

    "Open" is ``date_to is None``. Returns ``None`` when the attribute is
    absent entirely or carries only closed periods — the caller must
    distinguish "absent" from "present and wrong", because the predicate
    is lenient about the former and strict about the latter.
    """
    for attr in entity.get("attributes") or ():
        if not isinstance(attr, Mapping):
            continue
        if attr.get("code") == code or attr.get("code_attribute") == code:
            if attr.get("date_to") is None:
                value = attr.get("value")
                if value is None:
                    value = attr.get("value_varchar")
                return value if isinstance(value, str) else None
    return None


def describe_entity(entity: Mapping[str, Any]) -> str:
    """A short human label for an entity, for refusal messages.

    ``"meteorological 'Vífilsstaðir' (Úrkomustöð, id_entity=96)"`` — the
    three facts an operator needs to see that the refusal is right: which
    discipline, which place, which kind of station.
    """
    name = open_attribute(entity, "name")
    subtype = open_attribute(entity, "subtype")
    etype = entity.get("code_entity_subtype") or "unknown-type"
    eid = entity.get("id_entity")
    bits = [str(etype)]
    if name:
        bits.append(f"{name!r}")
    tail = []
    if subtype:
        tail.append(str(subtype))
    if eid is not None:
        tail.append(f"id_entity={eid}")
    label = " ".join(bits)
    return f"{label} ({', '.join(tail)})" if tail else label


class WrongStationKind(LookupError):
    """No candidate on this marker is the kind of station we act on.

    Subclasses :class:`LookupError` on purpose: every resolver's caller
    already handles a lookup miss and maps it to the right exit code, so
    a refusal rides the existing path instead of growing a second one.
    The rejected candidates ride along so the message can name what was
    actually found — a refusal an operator cannot check is a refusal they
    will work around.
    """

    def __init__(
        self,
        marker: str,
        candidates: Sequence[Mapping[str, Any]] = (),
        *,
        tool: str = "tosGPS",
        plain_tool: str = "tos",
        kind: str = "a GPS station",
    ) -> None:
        self.marker = marker
        self.candidates: List[Mapping[str, Any]] = list(candidates)
        self.tool = tool
        self.plain_tool = plain_tool
        self.kind = kind
        super().__init__(self._message())

    def _message(self) -> str:
        mk = self.marker.upper()
        if not self.candidates:
            return (
                f"{mk} is not {self.kind} in TOS — no entity carries marker "
                f"{self.marker.lower()!r}. If it is an external reference site "
                f"it has no TOS record by design; otherwise add it with "
                f"`{self.plain_tool} station add`."
            )
        found = "; ".join(describe_entity(c) for c in self.candidates)
        return (
            f"{mk} is not {self.kind} — TOS has {found}. "
            f"{self.tool} acts on GPS stations only; use "
            f"`{self.plain_tool} station show {mk}` to inspect it."
        )


class AmbiguousStation(LookupError):
    """Two or more candidates are admitted — refuse rather than guess.

    Unreachable across the whole live fleet as measured 2026-10-01, and
    that is exactly why it raises: the day a second ``GPS stöð`` appears
    on one marker, silently picking the first would be the SOHO bug
    again, one level down.
    """

    def __init__(self, marker: str, candidates: Sequence[Mapping[str, Any]]) -> None:
        self.marker = marker
        self.candidates = list(candidates)
        found = "; ".join(describe_entity(c) for c in self.candidates)
        super().__init__(
            f"{marker.upper()} is ambiguous — {len(self.candidates)} GPS "
            f"station entities carry this marker: {found}. Resolve by id "
            f"(`--id <n>`) and fix the duplicate in TOS."
        )


def select_station(
    marker: str,
    candidates: Sequence[Mapping[str, Any]],
    predicate: Optional[StationPredicate],
    *,
    kind: str = "a GPS station",
) -> Optional[Mapping[str, Any]]:
    """Pick the one candidate ``predicate`` admits.

    With ``predicate=None`` this is the historical behaviour exactly:
    first candidate wins, or ``None`` when there are none. That default
    is what keeps ``tos`` byte-identical.

    With a predicate:

    * exactly one admitted  → return it
    * none admitted         → :class:`WrongStationKind` (naming the
      rejects, or reporting that the marker is absent from TOS)
    * several admitted      → :class:`AmbiguousStation`

    Args:
        marker: the marker being resolved, for the messages.
        candidates: entity mappings, in the resolver's own preference
            order. Each must carry ``code_entity_subtype`` and
            ``attributes`` for the predicate to judge it.
        predicate: the gate, or ``None`` for no gate.
        kind: what this tool acts on, for the refusal message.

    Raises:
        WrongStationKind: nothing admitted.
        AmbiguousStation: more than one admitted.
    """
    cands = [c for c in candidates if isinstance(c, Mapping)]
    if predicate is None:
        return cands[0] if cands else None
    admitted = [c for c in cands if predicate(c)]
    if len(admitted) == 1:
        return admitted[0]
    if not admitted:
        raise WrongStationKind(marker, cands, kind=kind)
    raise AmbiguousStation(marker, admitted)
