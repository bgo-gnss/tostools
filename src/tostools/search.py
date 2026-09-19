"""General TOS station search — the engine behind ``tos search``.

Fleet-wide, read-only search over the bulk geophysical station listing
(one bodyless GET — the same endpoint the TOS web UI search uses), with
client-side filtering by:

- **Station attribute predicates** — ``code OP value`` where OP is
  ``=`` / ``!=`` (equality) or ``~`` / ``!~`` (substring), evaluated
  against the station's *currently-open* attribute period (the value
  the TOS UI shows in its Eigindi panel). ``null`` as the value tests
  absence/presence.
- **Device criteria** — ``[TYPE:]MODEL`` specs matched against the
  station's *currently-joined* devices (the TOS UI's Tæki tengd stöð
  panel), with negation forms.

Device data is NOT in the bulk listing — it needs a per-station history
walk (1 + N_children HTTP calls). The pipeline therefore applies the
attribute predicates first (free) and only walks devices for stations
that survive them.

Value normalization is case-insensitive and folds the Icelandic boolean
vocabulary (``já``/``nei``) onto ``true``/``false`` — TOS stores mixed
vocabulary in the wild (RHOF carries ``in_network_epos=true`` but
``is_near_fault_zones=nei``).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .api.client_cache import SnapshotMiss
from .utils.logging import get_logger

logger = get_logger(__name__, logging.WARNING)

# ---------------------------------------------------------------------------
# Coordinate frames (geofunc) — the computed ``--show coord.*`` columns
# ---------------------------------------------------------------------------

_coord_frame_cache: Dict[str, Any] = {}


def coord_frame(name: str) -> Any:
    """The geofunc :class:`CoordFrame` registered under ``name``.

    ``geofunc`` is a hard dependency of tostools, but the import is deferred
    so ``tostools.search`` stays importable in contexts that never touch
    ``--show coord.*`` (e.g. a minimal test environment without geofunc).

    Raises:
        KeyError: if ``name`` is unknown, with the valid frames listed.
    """
    if name not in _coord_frame_cache:
        from geofunc.coords import FRAMES

        if name not in FRAMES:
            valid = ", ".join(sorted(FRAMES))
            raise KeyError(f"unknown coordinate frame {name!r}; valid frames: {valid}")
        _coord_frame_cache[name] = FRAMES[name]
    return _coord_frame_cache[name]


def coord_frame_names() -> List[str]:
    """Sorted names of every registered geofunc frame (for ``--show coord.*``)."""
    from geofunc.coords import frame_names

    return list(frame_names())


def parse_coord_selector(raw: str) -> Optional[str]:
    """Frame name if ``raw`` is a ``coords.<frame>`` selector, else ``None``.

    Accepts both the canonical ``coords.`` and the singular ``coord.`` alias;
    returns the frame name lowercased. The caller validates it against the
    geofunc registry (an unknown frame raises ``KeyError`` from
    :func:`coord_frame`).
    """
    for pfx in (COORD_NAMESPACE + ".", "coord."):
        if raw.startswith(pfx):
            return raw[len(pfx) :].strip().lower()
    return None


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------

_TRUE_WORDS = {"true", "já", "ja", "yes", "1"}
_FALSE_WORDS = {"false", "nei", "no", "0"}
_NULL_WORDS = {"null", "none", "nil", "-"}


def norm_value(raw: Any) -> str:
    """Normalize a TOS attribute value (or user query term) for comparison.

    Case-insensitive; the Icelandic boolean vocabulary (true/já/yes/1 and
    false/nei/no/0) canonicalizes to ``true`` / ``false`` so
    ``in_network_epos = true`` matches regardless of which variant a
    station's row carries. Whitespace is stripped.
    """
    s = str(raw).strip().lower()
    if s in _TRUE_WORDS:
        return "true"
    if s in _FALSE_WORDS:
        return "false"
    return s


def is_null_term(value: str) -> bool:
    """True when a predicate value means 'absent' (``null`` keyword)."""
    return value.strip().lower() in _NULL_WORDS


# ---------------------------------------------------------------------------
# Text matching — substring (default), glob, regex
# ---------------------------------------------------------------------------

#: Opt-in prefix selecting regex semantics for a ``~`` / ``!~`` term.
REGEX_PREFIX = "re:"

#: Characters that make a ``~`` term a glob rather than a plain substring.
_GLOB_META = ("*", "?", "[")


def has_glob(term: str) -> bool:
    """True when ``term`` carries glob metacharacters."""
    return any(ch in term for ch in _GLOB_META)


def text_matches(current: Any, term: str) -> bool:
    """Does ``current`` satisfy a ``~`` term?

    Three styles, and which one applies is decided by the term itself so
    that nothing is ever reinterpreted behind the operator's back:

    1. ``re:PATTERN`` — regex, unanchored (``.search``). Opt-in only: a
       term is NEVER treated as a regex unless it says so, because
       ``name ~ a.b`` must keep meaning a literal ``a.b``.
    2. Contains ``*`` / ``?`` / ``[`` — glob, **fully anchored**
       (``fnmatch``): ``marker ~ HVE*`` is 'starts with HVE', not
       'contains HVE*'. Wrap in stars for substring: ``~ *vík*``.
    3. Anything else — plain substring, exactly as before this feature.

    Both sides go through :func:`norm_value` first, so the Icelandic
    boolean folding (``já``/``nei``) and case-insensitivity that plain
    substring matching has always had apply to patterns too.
    """
    if current is None:
        return False
    hay = norm_value(current)

    if term.startswith(REGEX_PREFIX):
        raw = term[len(REGEX_PREFIX) :]
        if not raw:
            raise ValueError(
                f"empty regex after {REGEX_PREFIX!r} — e.g. 'name ~ re:veður|vega'"
            )
        try:
            rx = re.compile(raw, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"bad regex {raw!r}: {exc}") from exc
        return rx.search(hay) is not None

    if has_glob(term):
        import fnmatch

        return fnmatch.fnmatch(hay, norm_value(term))

    return norm_value(term) in hay


# ---------------------------------------------------------------------------
# Selectors — 'code' (station) or 'namespace.code' (joined device)
# ---------------------------------------------------------------------------

#: Namespace meaning 'a device of any subtype'.
ANY_DEVICE = "*"


class UnsupportedSelector(ValueError):
    """A selector is well-formed but cannot be answered on this code path.

    Subclasses ``ValueError`` so the CLI's existing parse-error path turns
    it into a usage error (exit 2) with the message attached.
    """


#: The visit (vitjun) selector namespace — a THIRD selector kind alongside
#: station attributes (bare code) and device selectors (dotted). A visit is
#: a list-per-entity of maintenance records, so visit predicates are
#: existential over the entity's visit list (see :func:`visits_satisfy`).
VISIT_NAMESPACE = "visit"

#: Free-text note fields a visit carries; ``visit.all`` (aliases ``any`` /
#: ``text``) matches when ANY of these match.
VISIT_TEXT_FIELDS = ("work", "comment", "remaining")

#: Aliases for the "search across every note field" selector.
VISIT_ALL_ALIASES = ("all", "any", "text")

#: Structured (non-text) visit fields. ``type`` reads ``maintenance_type``;
#: ``participants`` matches participants OR participants_names.
VISIT_STRUCTURED_FIELDS = ("type", "participants")

#: Every code the ``visit.`` namespace accepts.
VISIT_CODES = frozenset(VISIT_TEXT_FIELDS + VISIT_ALL_ALIASES + VISIT_STRUCTURED_FIELDS)


#: The contact selector namespace — a FOURTH selector kind. A station's
#: contacts (Eigandi stöðvar / Rekstraraðili / Eigandi gagna) carry the
#: owner/operator organisation that the ``owner`` attribute does NOT — that
#: is why filtering 'UI stations' (Jarðvísindastofnun Háskóla Íslands) needs
#: this, not ``owner ~ ...``.
CONTACT_NAMESPACE = "contact"

#: Role-scoped selectors — match only contacts playing that role.
CONTACT_ROLE_CODES = ("owner", "operator", "data_owner")

#: Field selectors — match the field across every contact.
CONTACT_FIELD_CODES = ("organization", "name", "email")

#: Every code the ``contact.`` namespace accepts.
CONTACT_CODES = frozenset(CONTACT_ROLE_CODES + CONTACT_FIELD_CODES)

#: The coordinate-frame namespace — ``--show coords.<frame>`` projects a
#: station's lat/lon/altitude into a geofunc frame (three columns, one per
#: axis). Projection-only: ``parse_selector`` rejects it, so it can never
#: appear in a filter expression. The singular ``coord.`` is accepted as an
#: alias (both have been typed); ``coords.`` is canonical.
COORD_NAMESPACE = "coords"

#: The contact fields that read an ORGANISATION — these get abbreviation
#: expansion (``contact.owner ~ IES`` resolves to the full org name).
CONTACT_ORG_FIELDS = CONTACT_ROLE_CODES + ("organization",)

#: Icelandic role keywords, matched as substrings of ``role_is``.
_CONTACT_ROLE_KEYWORDS = {
    "owner": ("eigandi stöðvar",),
    "operator": ("rekstraraðili",),
    "data_owner": ("eigandi gagna",),
}


def parse_selector(raw: str) -> tuple:
    """Split ``'code'`` or ``'namespace.code'`` into ``(namespace, code)``.

    A bare code addresses a **station** attribute and returns namespace
    ``None`` — unchanged from before this feature. A dotted selector
    addresses an attribute of a currently-joined **device**::

        receiver.firmware_version   the station's GNSS receiver
        antenna.serial_number
        *.model                     a device of any subtype

    The namespace accepts the same aliases as ``--device TYPE:MODEL``, so
    ``receiver`` and ``gnss_receiver`` are interchangeable. Raises
    ``ValueError`` on an unknown namespace, listing the valid ones.
    """
    raw = raw.strip()
    if "." not in raw:
        return None, raw.lower()

    ns, _, code = raw.partition(".")
    ns = ns.strip().lower()
    code = code.strip().lower()
    if not code:
        raise ValueError(
            f"selector {raw!r} has no attribute after the dot — "
            "e.g. 'receiver.firmware_version'"
        )
    if ns == ANY_DEVICE:
        return ANY_DEVICE, code
    if ns == VISIT_NAMESPACE:
        if code not in VISIT_CODES:
            raise ValueError(
                f"unknown visit field {code!r} in {raw!r} — valid: "
                + ", ".join(sorted(VISIT_CODES))
            )
        return VISIT_NAMESPACE, code
    if ns == CONTACT_NAMESPACE:
        if code not in CONTACT_CODES:
            raise ValueError(
                f"unknown contact field {code!r} in {raw!r} — valid: "
                + ", ".join(sorted(CONTACT_CODES))
            )
        return CONTACT_NAMESPACE, code
    # DEVICE_SUBTYPE_ALIASES is defined further down (device-spec section);
    # module globals resolve at call time, so the forward reference is fine.
    if ns not in DEVICE_SUBTYPE_ALIASES:
        raise ValueError(
            f"unknown device namespace {ns!r} in {raw!r} — valid: "
            + ", ".join(sorted(set(DEVICE_SUBTYPE_ALIASES)) + [ANY_DEVICE])
        )
    return DEVICE_SUBTYPE_ALIASES[ns], code


def require_station_selector(namespace: Optional[str], raw: str) -> None:
    """Guard the station-only code paths against a device selector.

    Device selectors ARE supported (see
    :func:`predicate_matches_devices`), but they can only be answered from
    the per-station device walk. The bulk station listing carries no
    devices, so anything evaluating against it must refuse rather than
    quietly report False.
    """
    if namespace is not None:
        raise UnsupportedSelector(
            f"device selector {raw!r} cannot be resolved from the bulk "
            "station listing — it needs the per-station device walk. This is "
            "an internal guard; report it as a bug if you hit it from the CLI."
        )


# ---------------------------------------------------------------------------
# Attribute predicates
# ---------------------------------------------------------------------------

#: Operator alternation — ``!=`` / ``!~`` must precede ``=`` / ``~`` so the
#: regex engine never bites the ``=`` half of ``!=``.
#: The code group accepts ``.`` and ``*`` so a selector namespace
#: (``receiver.firmware_version``, ``*.model``) parses here rather than
#: failing as an unparsable expression.
_EXPR_RE = re.compile(r"^\s*([A-Za-z0-9_.*]+)\s*(!=|!~|=|~)\s*(.+?)\s*$")

#: Sugar flag → predicate mapping.
EPOS_CODE = "in_network_epos"


@dataclass(frozen=True)
class Predicate:
    """One station-attribute criterion: ``code op value``.

    ``op`` is one of ``=``, ``!=``, ``~``, ``!~``, plus the list forms
    ``in`` / ``not in`` (``code = [a, b]``). The value ``null`` (any
    casing) tests absence (``=``) or presence (``!=``) of the code; the
    value ``all`` (any casing) matches everything (``=``) or nothing
    (``!=``) — used by ``subtype = all`` to lift the default GPS scope.
    """

    code: str
    op: str
    value: str = ""
    values: tuple = ()
    namespace: Optional[str] = None

    @property
    def selector(self) -> str:
        """Human-facing selector: ``code`` or ``namespace.code``."""
        return f"{self.namespace}.{self.code}" if self.namespace else self.code

    def describe(self) -> str:
        if self.op in ("in", "not in"):
            opstr = "in" if self.op == "in" else "not in"
            return f"{self.selector} {opstr} [{', '.join(self.values)}]"
        return f"{self.selector} {self.op} {self.value}"


#: The operational IMO GNSS fleet definition — what ``--active-gps`` expands
#: to: GPS subtype, not ended (no open Lokadagsetning), on bedrock rather
#: than a moving glacier (geological != ice, absent geology passes), and
#: recorded as a continuous station (Samfella = continuous — strict, so
#: unrecorded continuity does NOT pass; fill the attribute instead).
ACTIVE_GPS_PREDICATES = (
    Predicate("subtype", "=", "GPS stöð"),
    Predicate("date_end", "=", "null"),
    Predicate("geological_characteristic", "!=", "ice"),
    Predicate("continuity", "=", "continuous"),
)


def sugar_predicates(args) -> List[Predicate]:
    """Translate the shared sugar flags into attribute predicates.

    ``--epos`` / ``--no-epos`` / ``--active-gps`` / ``--discontinued`` /
    ``--continuous`` / ``--campaign`` / ``--no-ice`` — the one mapping every
    scope-taking verb reads, so ``tos search`` and ``tos visit search``
    (and future verbs) can't drift apart. Flags are read defensively via
    ``getattr`` so a caller with only a subset still works.
    """
    preds: List[Predicate] = []
    if getattr(args, "epos", False):
        preds.append(Predicate(EPOS_CODE, "=", "true"))
    if getattr(args, "no_epos", False):
        preds.append(Predicate(EPOS_CODE, "!=", "true"))
    if getattr(args, "active_gps", False):
        preds.extend(ACTIVE_GPS_PREDICATES)
    if getattr(args, "discontinued", False):
        preds.append(Predicate("date_end", "!=", "null"))
    if getattr(args, "continuous", False):
        preds.append(Predicate("continuity", "=", "continuous"))
    if getattr(args, "campaign", False):
        preds.append(Predicate("continuity", "=", "campaign"))
    if getattr(args, "no_ice", False):
        preds.append(Predicate("geological_characteristic", "!=", "ice"))
    return preds


def parse_expression(expr: str) -> Predicate:
    """Parse ``'code OP value'`` into a :class:`Predicate`.

    Raises ``ValueError`` on a missing operator or an unparsable shape —
    the CLI turns that into a usage error (exit 2) with the syntax hint.
    """
    m = _EXPR_RE.match(expr)
    if not m:
        raise ValueError(
            f"cannot parse {expr!r} — expected 'code OP value' with OP one of "
            "= != ~ !~ (e.g. 'in_network_epos = true', 'marker ~ ve')"
        )
    raw_code, op, value = m.groups()
    namespace, code = parse_selector(raw_code)
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        items = [v.strip() for v in stripped[1:-1].split(",") if v.strip()]
        if not items:
            raise ValueError(
                f"empty list in {expr!r} — e.g. 'subtype = [GPS stöð, SIL stöð]'"
            )
        if op == "=":
            return Predicate(
                code=code, op="in", values=tuple(items), namespace=namespace
            )
        if op == "!=":
            return Predicate(
                code=code, op="not in", values=tuple(items), namespace=namespace
            )
        raise ValueError(
            f"list value only supports '=' / '!=' (got {op!r} in {expr!r})"
        )
    # Strip a MATCHING pair of surrounding quotes — 'marker ~ "a b"' and
    # "marker ~ 'a b'" both mean the multi-word value `a b`, not the quoted
    # literal. Only when both ends match (a lone leading quote stays literal).
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        stripped = stripped[1:-1]
    return Predicate(code=code, op=op, value=stripped, namespace=namespace)


def split_expression_list(raw: str) -> List[str]:
    """Split a comma-separated expression list on commas OUTSIDE quotes/brackets.

    ``--show`` already accepts comma-separated selectors; this gives the
    positional expression list the same ergonomics, so
    ``'visit.all !~ "EPOS disseminated", tectonic_plate ~ "Eurasia"'``
    becomes two expressions. A comma inside a quoted value
    (``name ~ "a, b"``) or an in-list value (``subtype = [GPS stöð, SIL stöð]``)
    is NOT a separator.
    """
    parts: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    depth = 0  # square-bracket nesting
    for ch in raw:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if quote is not None:
        raise ValueError(
            f"unbalanced quote in {raw!r} — a stray quote breaks comma "
            "splitting (check for a missing/extra \" or ')"
        )
    if depth:
        raise ValueError(f"unbalanced '[' in {raw!r}")
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def open_value(entity: dict, code: str) -> Optional[str]:
    """Value of the currently-open period for ``code``, or None.

    Mirrors :func:`tostools.devices.open_attribute` against the bulk
    search listing's attribute shape (``code`` / ``value`` / ``date_from``
    / ``date_to``). Closed periods are ignored — the search answers 'what
    does this station's Eigindi panel show today', matching the UI.
    """
    for attr in entity.get("attributes") or []:
        if attr.get("code") != code:
            continue
        if attr.get("date_to") is not None:
            continue
        v = attr.get("value")
        if v is not None:
            return str(v)
    return None


def values_satisfy(values: List[Optional[str]], pred: Predicate) -> bool:
    """Does any candidate value satisfy ``pred``?

    The single evaluation rule behind every predicate in this module. A
    station attribute at one instant contributes a one-element list; a
    two-receiver station contributes two; ``--history`` contributes every
    period. Aggregation is **existential** throughout, so all three read
    the same way: 'is there a value here that matches'.

    Absence rules, unchanged from the original scalar form: ``= all``
    matches everything, ``null`` tests presence, and with nothing present
    only a negative operator can hold.
    """
    present = [v for v in values if v is not None]

    if pred.op in ("=", "!=") and norm_value(pred.value) == "all":
        return pred.op == "="
    if pred.op in ("=", "!=") and is_null_term(pred.value):
        return bool(present) if pred.op == "!=" else not present

    if not present:
        return pred.op in ("!=", "!~", "not in")

    if pred.op == "in":
        wanted = {norm_value(v) for v in pred.values}
        return any(norm_value(v) in wanted for v in present)
    if pred.op == "not in":
        wanted = {norm_value(v) for v in pred.values}
        return any(norm_value(v) not in wanted for v in present)
    if pred.op == "=":
        return any(norm_value(v) == norm_value(pred.value) for v in present)
    if pred.op == "!=":
        return any(norm_value(v) != norm_value(pred.value) for v in present)
    if pred.op == "~":
        return any(text_matches(v, pred.value) for v in present)
    if pred.op == "!~":
        return any(not text_matches(v, pred.value) for v in present)
    return False


def as_day(raw: Optional[str]) -> Optional[str]:
    """``2019-10-15T00:00:00`` → ``2019-10-15``; None stays None.

    TOS timestamps are ISO-8601, so day strings compare correctly with
    ``<``/``>=`` and no date parsing is needed anywhere in this module.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    return s[:10] if s else None


def attribute_periods(entity: dict, code: str) -> List[dict]:
    """Every period recorded for ``code``, oldest first.

    ``{"value", "date_from", "date_to"}`` carry DAY-granularity strings —
    every filter in this module compares them as plain strings, which is
    why they must stay ten characters wide. ``date_to=None`` is the open
    period.

    ``date_from_ts`` / ``date_to_ts`` carry the value exactly as TOS
    returned it, timestamp included. They exist because the day form is
    lossy in the one case the caller most needs precision: two periods of
    the same attribute that start and end on the SAME DAY. V159's ``name``
    is the worked example —

        Sandgígjukvísl  2009-05-09T00:00:00 -> 2009-05-09T10:00:00
        Gígjukvísl      2009-05-09T10:00:00 -> open

    — a normal 10-hour period and a clean hand-off, which the day form
    renders as ``2009-05-09 -> 2009-05-09`` beside ``2009-05-09 -> null``
    and makes look like a zero-length, corrupt row. That reading cost a
    session. Never widen the day keys to fix this: ``value_at`` compares
    ``start > day`` against a 10-char ``day``, so a timestamp there would
    sort after its own date and silently break every ``--at`` query.
    """
    out = []
    for attr in entity.get("attributes") or []:
        if attr.get("code") != code:
            continue
        value = attr.get("value")
        raw_from, raw_to = attr.get("date_from"), attr.get("date_to")
        out.append(
            {
                "value": None if value is None else str(value),
                "date_from": as_day(raw_from),
                "date_to": as_day(raw_to),
                "date_from_ts": None if raw_from is None else str(raw_from),
                "date_to_ts": None if raw_to is None else str(raw_to),
            }
        )
    return sorted(out, key=lambda p: p["date_from"] or "")


def value_at(entity: dict, code: str, at: Optional[str] = None) -> Optional[str]:
    """Value of ``code`` on the day ``at``, or of the open period.

    ``at=None`` means 'now' and selects the open period — the value the
    TOS UI's Eigindi panel shows today, which is what every caller wanted
    before ``--at`` existed. With a day given, the covering period wins:
    ``date_from <= at`` and (``date_to`` is open or ``at < date_to``).
    """
    if at is None:
        return open_value(entity, code)
    day = as_day(at)
    for p in attribute_periods(entity, code):
        start, end = p["date_from"], p["date_to"]
        if start is not None and start > day:
            continue
        if end is not None and day >= end:
            continue
        if p["value"] is not None:
            return p["value"]
    return None


def history_values(entity: dict, code: str) -> List[str]:
    """Every non-null value ``code`` has ever held, oldest first."""
    return [
        p["value"] for p in attribute_periods(entity, code) if p["value"] is not None
    ]


def period_boundaries(entity: dict, code: str) -> List[str]:
    """Distinct dates at which ``code`` changes — segment edges."""
    edges = set()
    for p in attribute_periods(entity, code):
        for d in (p["date_from"], p["date_to"]):
            if d:
                edges.add(d)
    return sorted(edges)


def predicate_matches(
    station: dict, pred: Predicate, *, at: Optional[str] = None, history: bool = False
) -> bool:
    """Evaluate one :class:`Predicate` against a bulk-listing station dict.

    Station-attribute predicates only — a device selector cannot be
    answered from the bulk listing (it carries no devices) and is rejected
    here as a backstop, since the CLI already refuses it at parse time.
    """
    require_station_selector(pred.namespace, pred.selector)
    if history:
        # 'ever held this value' — every recorded period is a candidate.
        return values_satisfy(history_values(station, pred.code), pred)
    return values_satisfy([value_at(station, pred.code, at)], pred)


class UnknownStationCode(ValueError):
    """A station attribute code no source vouches for.

    Raised BEFORE filtering, because an unvalidated code does not fail —
    it answers. ``value_at`` returns ``None`` for a code nothing carries,
    so ``'typo != null'`` renders a confident ``0 station(s) match`` and
    ``'typo = null'`` matches the WHOLE fleet with a column of dashes.
    Both are indistinguishable from real answers, and this output drives
    live TOS writes.
    """

    def __init__(self, unknown: List[tuple], domain: str):
        #: ``[(code, [suggestion, ...]), ...]`` — suggestions may be empty.
        self.unknown = unknown
        self.domain = domain
        super().__init__("; ".join(code for code, _ in unknown))


def known_station_codes(
    fleet: List[dict], *, catalog_codes: Optional[Iterable[str]] = None
) -> set:
    """The vocabulary a station predicate may draw on: observed ∪ catalog.

    **Observed** is every code carried by any station in ``fleet`` — the
    same aggregation ``--attribute-list`` prints. **Catalog** is
    ``data/attribute_codes.yaml``. The union is deliberate and neither
    half is sufficient:

    - Observed alone would reject a real-but-unpopulated code, breaking
      ``'code = null'`` — "which stations lack this?" is the query that
      found the DOMES gap, and it is precisely the one with no observed
      values to learn from.
    - The catalog alone would reject codes that are live and in daily
      use. It is hand-written (every entry carries ``walked: false``) and
      has never been reconciled with the API: it calls ``date_end``
      ``close_date``, and ``--active-gps`` expands to a predicate the
      catalog does not contain.

    A missing/unreadable catalog degrades to observed-only rather than
    breaking search.
    """
    codes = {
        a.get("code")
        for st in fleet
        for a in (st.get("attributes") or [])
        if a.get("code")
    }
    if catalog_codes is None:
        try:  # lazy — search_selectors imports this module
            from .search_selectors import catalog_station_codes

            catalog_codes = catalog_station_codes().keys()
        except Exception as exc:  # noqa: BLE001 — catalog is an optional source
            logger.warning(
                "attribute catalog unavailable, validating on observed codes only: %s",
                exc,
            )
            catalog_codes = ()
    return codes | {c for c in catalog_codes if c}


def validate_station_codes(
    fleet: List[dict],
    predicates: List[Predicate],
    show_codes: Optional[List[str]] = None,
    *,
    domain: str = "geophysical",
    catalog_codes: Optional[Iterable[str]] = None,
) -> None:
    """Refuse station codes outside the known vocabulary.

    Station-scope only — a dotted selector is a device code and is checked
    against a different vocabulary during the device walk. Raises
    :class:`UnknownStationCode` naming every offender at once, so a
    two-typo command is corrected in one round trip rather than two.
    """
    known = known_station_codes(fleet, catalog_codes=catalog_codes)
    if not known:  # empty fleet — nothing to validate against, don't guess
        return
    wanted: List[str] = []
    for pred in predicates:
        if pred.namespace is None and pred.code not in wanted:
            wanted.append(pred.code)
    for raw in show_codes or []:
        code = raw.lower()
        if "." not in code and code not in wanted:
            wanted.append(code)

    ordered = sorted(known)
    unknown = [
        (code, difflib.get_close_matches(code, ordered, n=3, cutoff=0.6))
        for code in wanted
        if code not in known
    ]
    if unknown:
        raise UnknownStationCode(unknown, domain)


def filter_by_predicates(
    stations: List[dict],
    predicates: List[Predicate],
    *,
    at: Optional[str] = None,
    history: bool = False,
) -> List[dict]:
    """Keep stations satisfying every predicate (AND)."""
    if not predicates:
        return list(stations)
    return [
        s
        for s in stations
        if all(predicate_matches(s, p, at=at, history=history) for p in predicates)
    ]


# ---------------------------------------------------------------------------
# Device specs
# ---------------------------------------------------------------------------

#: Short alias → canonical TOS device subtype (superset of audit.py's
#: SUBTYPE_ALIASES — search also needs the telemetry hardware types that
#: appear in the TOS UI device panel: Beinir/router, SIM-kort/sim_card,
#: modem_gsm).
DEVICE_SUBTYPE_ALIASES: Dict[str, str] = {
    "receiver": "gnss_receiver",
    "gnss_receiver": "gnss_receiver",
    "antenna": "antenna",
    "radome": "radome",
    "monument": "monument",
    "modem": "modem_gsm",
    "modem_gsm": "modem_gsm",
    "sim": "sim_card",
    "sim_card": "sim_card",
    "router": "router",
}

#: Wildcard model terms — 'has any device of TYPE' rather than a model match.
_WILDCARDS = {"any", "*", ""}


@dataclass(frozen=True)
class DeviceSpec:
    """One device criterion: ``[TYPE:]MODEL`` with optional negation.

    ``subtype`` is the canonical TOS subtype or None (any type). ``model``
    is a normalized substring or None (any model). ``negate`` inverts the
    test (must NOT have such a device).
    """

    subtype: Optional[str] = None
    model: Optional[str] = None
    negate: bool = False

    def describe(self) -> str:
        lhs = self.subtype or "device"
        rhs = self.model or "any"
        op = "has-no" if self.negate else "has"
        return f"{op} {lhs}:{rhs}"


def parse_device_spec(raw: str, *, negate: bool = False) -> DeviceSpec:
    """Parse ``'[TYPE:]MODEL'`` into a :class:`DeviceSpec`.

    ``TYPE`` is a short alias or canonical subtype (receiver, antenna,
    radome, monument, modem, sim, router). ``MODEL`` is a case-insensitive
    substring; ``any`` / ``*`` (or empty) means any model — so
    ``router:any`` is 'has a router at all'. A bare ``teltonika`` (no
    colon) matches any device type by model.

    Raises ``ValueError`` on an unknown TYPE alias.
    """
    raw = raw.strip()
    subtype: Optional[str] = None
    model_part = raw
    if ":" in raw:
        type_part, model_part = raw.split(":", 1)
        type_part = type_part.strip().lower()
        if type_part and type_part not in _WILDCARDS:
            if type_part not in DEVICE_SUBTYPE_ALIASES:
                raise ValueError(
                    f"unknown device type {type_part!r} in {raw!r} — valid: "
                    + ", ".join(sorted(set(DEVICE_SUBTYPE_ALIASES)))
                )
            subtype = DEVICE_SUBTYPE_ALIASES[type_part]
        if not subtype:
            subtype = None
    model = model_part.strip().lower()
    if model in _WILDCARDS:
        model = None
    return DeviceSpec(subtype=subtype, model=model, negate=negate)


def device_spec_matches(device: dict, spec: DeviceSpec) -> bool:
    """Does one open-join device dict satisfy a (positive) spec?"""
    if spec.subtype and device.get("subtype") != spec.subtype:
        return False
    if spec.model:
        model = device.get("model")
        if model is None:
            return False
        if spec.model not in norm_value(model):
            return False
    return True


def device_in_namespace(device: dict, namespace: str) -> bool:
    """Is this joined device addressed by ``namespace``?

    ``*`` (:data:`ANY_DEVICE`) matches every subtype; anything else is an
    exact canonical-subtype match.
    """
    if namespace == ANY_DEVICE:
        return True
    return device.get("subtype") == namespace


def device_values(
    devices: List[dict],
    namespace: str,
    code: str,
    *,
    at: Optional[str] = None,
    history: bool = False,
) -> List[Any]:
    """Values of ``code`` across every device in ``namespace``.

    One entry per matching device, ``None`` where that device has no value
    at ``at``. An empty list means the station has no device of that
    subtype at all — a different thing from having one whose attribute is
    unset, and the two are deliberately NOT collapsed: a station with two
    receivers contributes two entries, so nothing silently reports one
    receiver's firmware as though it were the station's.

    ``history=True`` flattens every recorded period of every matching
    device instead, which is what ``--history`` filters on.
    """
    matching = [d for d in devices if device_in_namespace(d, namespace)]
    if history:
        out: List[Any] = []
        for d in matching:
            out.extend(history_values(d, code))
        return out
    return [value_at(d, code, at) for d in matching]


def device_periods(devices: List[dict], namespace: str, code: str) -> List[List[dict]]:
    """Per-device period lists for ``code`` — one inner list per device."""
    return [
        attribute_periods(d, code) for d in devices if device_in_namespace(d, namespace)
    ]


def predicate_matches_devices(
    devices: List[dict],
    pred: Predicate,
    *,
    at: Optional[str] = None,
    history: bool = False,
) -> bool:
    """Evaluate a device-selector predicate against a station's devices.

    Aggregation is **existential**: the station matches when *any* device
    in the namespace satisfies the predicate. So
    ``receiver.firmware_version != 5.7.0`` means 'has a receiver that is
    not on 5.7.0', which on a two-receiver station is a weaker claim than
    'no receiver is on 5.7.0' — stated in ``--help`` rather than made
    configurable. Under ``--history`` the same rule spans periods, so the
    predicate asks 'did any receiver ever hold this'.

    Absence mirrors the station-attribute rules: with no value present,
    only a negative operator can hold, and ``= null`` / ``!= null`` test
    for the attribute's presence anywhere in the namespace.
    """
    return values_satisfy(
        device_values(
            devices, pred.namespace or ANY_DEVICE, pred.code, at=at, history=history
        ),
        pred,
    )


def devices_satisfy(
    devices: List[dict], must: List[DeviceSpec], must_not: List[DeviceSpec]
) -> bool:
    """True when the station's device list satisfies every spec.

    Positive specs are existential (at least one matching device each);
    negated specs are universal (no device may match).
    """
    for spec in must:
        if not any(device_spec_matches(d, spec) for d in devices):
            return False
    for spec in must_not:
        if any(device_spec_matches(d, spec) for d in devices):
            return False
    return True


# ---------------------------------------------------------------------------
# Visit (vitjun) predicates — the third selector kind
# ---------------------------------------------------------------------------

_OP_NEGATION = {
    "=": "!=",
    "!=": "=",
    "~": "!~",
    "!~": "~",
    "in": "not in",
    "not in": "in",
}


def _negate_predicate(pred: Predicate) -> Predicate:
    """The positive form of a negative predicate (for universal negation)."""
    return Predicate(
        code=pred.code,
        op=_OP_NEGATION[pred.op],
        value=pred.value,
        values=pred.values,
        namespace=pred.namespace,
    )


def _visit_field_values(visit: dict, code: str) -> List[str]:
    """The candidate text value(s) one visit carries for a ``visit.`` field.

    Empty/None → empty list, so ``= null`` reads as absence and a bare
    ``~``/``=`` never matches a blank note. ``all`` expands to the three
    note fields (work/comment/remaining); ``participants`` matches the
    email AND the resolved name.
    """
    if code in VISIT_ALL_ALIASES:
        return [str(visit[f]) for f in VISIT_TEXT_FIELDS if visit.get(f)]
    if code == "type":
        v = visit.get("maintenance_type")
        return [str(v)] if v else []
    if code == "participants":
        return [
            str(visit[k])
            for k in ("participants", "participants_names")
            if visit.get(k)
        ]
    v = visit.get(code)
    return [str(v)] if v else []


def visits_satisfy(visits: List[dict], preds: List[Predicate]) -> bool:
    """Same-visit AND for positives; universal negation for negatives.

    Visit predicates are the third selector kind — a station matches when::

        visit.X ~ v / = v / in [...]   some ONE visit's X matches
        visit.X !~ v / != v / not in   NO visit's X matches v
        visit.X = null                 no visit carries X
        visit.X != null                some visit carries X

    Multiple positive predicates group onto ONE visit (``visit.work ~ TOS``
    AND ``visit.type = remote`` = a remote visit whose work matches); a
    negative predicate holds station-wide, so the 'not yet onboarded'
    cross-check is ``visit.all !~ "TOS reviewed"``.
    """
    positive = [
        p for p in preds if p.op in ("=", "~", "in") and not is_null_term(p.value)
    ]
    negative = [
        p for p in preds if p.op in ("!=", "!~", "not in") and not is_null_term(p.value)
    ]
    absent = [p for p in preds if p.op == "=" and is_null_term(p.value)]
    present = [p for p in preds if p.op == "!=" and is_null_term(p.value)]

    if positive and not any(
        all(values_satisfy(_visit_field_values(v, p.code), p) for p in positive)
        for v in visits
    ):
        return False
    for p in negative:
        inv = _negate_predicate(p)
        if any(values_satisfy(_visit_field_values(v, inv.code), inv) for v in visits):
            return False
    for p in absent:
        if any(_visit_field_values(v, p.code) for v in visits):
            return False
    for p in present:
        if not any(_visit_field_values(v, p.code) for v in visits):
            return False
    return True


def walk_visits(
    client: Any,
    station_ids: List[int],
    *,
    max_workers: int = 8,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[int, List[dict]]:
    """Vitjanir for many stations, walked on a thread pool.

    A failed station (TOS error) yields an empty list + a warning, never
    an abort — a flaky single station must not sink the fleet search.
    Returns ``{id_entity: [visit, ...]}``.
    """
    out: Dict[int, List[dict]] = {}
    ids = list(station_ids)
    if not ids:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(client.list_maintenance_visits, sid): sid for sid in ids}
        for done, fut in enumerate(as_completed(futures), 1):
            sid = futures[fut]
            try:
                out[sid] = fut.result() or []
            except SnapshotMiss:
                raise  # a replay gap is deterministic, not a per-station flake
            except Exception as exc:  # noqa: BLE001 — per-station isolation
                logger.warning("visit walk failed for id_entity=%s: %s", sid, exc)
                out[sid] = []
            if progress is not None:
                progress(done, len(ids))
    return out


# ---------------------------------------------------------------------------
# Contact (eigandi/operator) predicates — the fourth selector kind
# ---------------------------------------------------------------------------


def _contact_role(contact: dict, code: str) -> bool:
    """Whether a contact plays the role a role-scoped selector names.

    Field selectors (``organization``/``name``/``email``) match every
    contact, so this returns True for them.
    """
    if code not in CONTACT_ROLE_CODES:
        return True
    role = str(contact.get("role_is") or contact.get("role") or "").lower()
    return any(kw in role for kw in _CONTACT_ROLE_KEYWORDS[code])


def _contact_field(contact: dict, code: str) -> Optional[str]:
    """The value a contact selector reads for one contact.

    Role-scoped selectors read the ORGANISATION (the agencies.yaml key);
    field selectors read the named field.
    """
    if code in CONTACT_ROLE_CODES:
        return contact.get("organization") or contact.get("name")
    return contact.get(code)


def _contact_values(contacts: List[dict], code: str) -> List[str]:
    """Candidate values for one contact selector across the contact list."""
    out: List[str] = []
    for c in contacts:
        if not _contact_role(c, code):
            continue
        v = _contact_field(c, code)
        if v:
            out.append(str(v))
    return out


_ORG_ALIASES: Optional[Dict[str, str]] = None


def _agencies_yaml_paths() -> List[Path]:
    """Candidate agencies.yaml locations, deployed config first."""
    paths: List[Path] = []
    cfg = os.environ.get("GPS_CONFIG_PATH")
    if cfg:
        for d in cfg.split(":"):
            if d:
                paths.append(Path(d) / "agencies.yaml")
    paths.append(Path.home() / ".config" / "gpsconfig" / "agencies.yaml")
    paths.append(Path.home() / "git" / "gps-config-data" / "agencies.yaml")
    return paths


def _org_aliases() -> Dict[str, str]:
    """``abbrev``/``abbrev_is`` (case-folded) → full Icelandic org name.

    Read from ``agencies.yaml`` (deployed config first, then the repo) so
    ``contact.owner ~ IES`` resolves to 'Jarðvísindastofnun Háskóla
    Íslands' without the operator knowing the Icelandic inflection. Cached
    per process; an absent/unreadable file degrades to no expansion.
    """
    global _ORG_ALIASES
    if _ORG_ALIASES is not None:
        return _ORG_ALIASES
    mapping: Dict[str, str] = {}
    for path in _agencies_yaml_paths():
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — optional enrichment
            continue
        for full, entry in (data.get("agencies") or {}).items():
            entry = entry or {}
            for key in ("abbrev", "abbrev_is"):
                a = str(entry.get(key) or "").strip()
                if a:
                    mapping[norm_value(a)] = full
        if mapping:
            break  # first readable file with data wins
    _ORG_ALIASES = mapping
    return mapping


def _expand_org_term(term: str) -> str:
    """The full org name when ``term`` is its abbreviation, else unchanged.

    ``contact.owner ~ IES`` → matches 'Jarðvísindastofnun Háskóla Íslands'
    because 'IES' is its ``abbrev``; a plain substring ('Háskóla',
    'Jarðvísinda') passes through untouched.
    """
    full = _org_aliases().get(norm_value(term))
    return full if full else term


def _existential_decision(values: List[str], pred: Predicate) -> bool:
    """Existential positive / universal negative / null presence, on a flat
    candidate-value list (contacts, or one visit's fields)."""
    if pred.op == "=" and is_null_term(pred.value):
        return not values  # absent everywhere
    if pred.op == "!=" and is_null_term(pred.value):
        return bool(values)  # present somewhere
    if pred.op in ("=", "~", "in"):
        return values_satisfy(values, pred)
    inv = _negate_predicate(pred)
    return not values_satisfy(values, inv)


def contacts_satisfy(contacts: List[dict], preds: List[Predicate]) -> bool:
    """Evaluate contact selectors against a station's contact list.

    Each predicate is independent (role-scoped). Positive ops are
    existential over the scoped contacts; negative ops universal —
    ``contact.owner !~ Háskóli`` means 'the owner org does NOT match',
    the inverse of the ``--owner`` include form. Organisation terms get
    abbreviation expansion, so ``contact.owner ~ IES`` matches the org
    whose ``abbrev`` is 'IES'.
    """
    for p in preds:
        term = p.value
        if p.code in CONTACT_ORG_FIELDS and not is_null_term(term):
            term = _expand_org_term(term)
            p = Predicate(
                code=p.code, op=p.op, value=term, values=p.values, namespace=p.namespace
            )
        if not _existential_decision(_contact_values(contacts, p.code), p):
            return False
    return True


def walk_contacts(
    client: Any,
    station_ids: List[int],
    *,
    max_workers: int = 8,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[int, List[dict]]:
    """Contacts for many stations, walked on a thread pool.

    A failed station (TOS error) yields an empty list + a warning, never
    an abort. Returns ``{id_entity: [contact, ...]}``.
    """
    out: Dict[int, List[dict]] = {}
    ids = list(station_ids)
    if not ids:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(client.get_contacts, sid): sid for sid in ids}
        for done, fut in enumerate(as_completed(futures), 1):
            sid = futures[fut]
            try:
                out[sid] = fut.result() or []
            except SnapshotMiss:
                raise  # a replay gap is deterministic, not a per-station flake
            except Exception as exc:  # noqa: BLE001 — per-station isolation
                logger.warning("contact walk failed for id_entity=%s: %s", sid, exc)
                out[sid] = []
            if progress is not None:
                progress(done, len(ids))
    return out


# ---------------------------------------------------------------------------
# Attribute discovery (—-attribute-list / --allowed-values)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttributeInventory:
    """Vocabulary discovery for one attribute code across a fleet listing.

    ``values`` are the distinct observed values with the number of stations
    carrying each (open or historical periods — the question answered is
    'what has TOS ever accepted here', which is what an operator needs
    before writing a predicate). ``constraint`` is TOS's own
    ``python_constraint`` regex from the attribute metadata.
    """

    code: str
    name_is: Optional[str] = None
    description_en: Optional[str] = None
    datatype: Optional[str] = None
    constraint: Optional[str] = None
    station_count: int = 0
    values: List[tuple] = field(default_factory=list)


def attribute_inventory(
    stations: List[dict], code: Optional[str] = None
) -> List[AttributeInventory]:
    """Aggregate distinct attribute codes / values over a fleet listing.

    ``code`` restricts the inventory to one attribute (single-element list,
    empty when the code is never observed). Station counts are distinct
    id_entities; per-value counts likewise count stations, not periods.
    """
    agg: Dict[str, Dict[str, Any]] = {}
    for st in stations:
        sid = st.get("id_entity")
        for a in st.get("attributes") or []:
            c = a.get("code")
            if not c or (code is not None and c != code):
                continue
            entry = agg.setdefault(c, {"stations": set(), "values": {}, "meta": a})
            entry["stations"].add(sid)
            v = a.get("value")
            if v is not None:
                entry["values"].setdefault(str(v), set()).add(sid)
    out: List[AttributeInventory] = []
    for c, e in agg.items():
        meta = e["meta"]
        values = sorted(
            ((v, len(ids)) for v, ids in e["values"].items()),
            key=lambda x: (-x[1], x[0]),
        )
        out.append(
            AttributeInventory(
                code=c,
                name_is=meta.get("name_is"),
                description_en=meta.get("description_en"),
                datatype=meta.get("attribute_datatype_code"),
                constraint=meta.get("python_constraint"),
                station_count=len(e["stations"]),
                values=values,
            )
        )
    out.sort(key=lambda i: (-i.station_count, i.code))
    return out


# ---------------------------------------------------------------------------
# Fleet fetch + device walk
# ---------------------------------------------------------------------------


def fetch_fleet(client: Any, domain: str = "geophysical") -> List[dict]:
    """Bulk-fetch every station in ``domain`` (one HTTP call)."""
    return client.list_stations(domain=domain) or []


#: The domains ``tos search`` can enumerate (matches ``list_stations``'s
#: entity mapping — ``remote_sensing_platform`` resolves to the 'platform'
#: entity type). Used by the cross-domain probe so an unknown-code error can
#: point at where the code actually lives instead of stopping at "not here".
SEARCH_DOMAINS = (
    "geophysical",
    "meteorological",
    "hydrological",
    "remote_sensing_platform",
)


def probe_code_domains(
    client: Any, codes: Iterable[str], *, current_domain: str
) -> Dict[str, Dict[str, int]]:
    """Map unknown station codes to the domains that DO carry them.

    Runs only on the validation-error path, so a valid query pays nothing.
    Fetches every :data:`SEARCH_DOMAINS` other than ``current_domain`` (one
    bulk call each) and counts how many entities carry each ``code``.
    Best-effort: a domain whose fetch fails is skipped rather than masking
    the usage error already being reported, and a code observed nowhere
    simply does not appear in the result.

    Returns ``{code: {domain: entity_count}}``.
    """
    wanted = {c for c in codes if c}
    if not wanted:
        return {}
    found: Dict[str, Dict[str, int]] = {}
    for domain in SEARCH_DOMAINS:
        if domain == current_domain:
            continue
        try:
            fleet = fetch_fleet(client, domain=domain)
        except Exception:  # noqa: BLE001 — probe is best-effort enrichment
            continue
        counts: Dict[str, int] = {}
        for st in fleet:
            for a in st.get("attributes") or []:
                c = a.get("code")
                if c in wanted:
                    counts[c] = counts.get(c, 0) + 1
        for code, n in counts.items():
            found.setdefault(code, {})[domain] = n
    return found


def station_open_devices(client: Any, station_id: int) -> List[dict]:
    """Currently-joined devices of one station — the UI device panel.

    One history call for the station (children_connections) plus one per
    open child join (serial / model / subtype / status). Devices are the
    rows ``tos device list --station`` prints.
    """
    hist = client.get_entity_history(station_id) or {}
    devices: List[dict] = []
    for conn in hist.get("children_connections") or []:
        if conn.get("time_to") is not None:
            continue  # closed join — device no longer at the station
        child_id = conn.get("id_entity_child")
        if child_id is None:
            continue
        try:
            child_id = int(child_id)
        except (TypeError, ValueError):
            continue
        child = client.get_entity_history(child_id) or {}
        devices.append(
            {
                "id_entity": child_id,
                "serial": open_value(child, "serial_number") or "?",
                "model": open_value(child, "model"),
                "subtype": child.get("code_entity_subtype") or "?",
                "status": open_value(child, "status") or "—",
                "since": conn.get("time_from"),
                # Raw periods, so an arbitrary device selector
                # (receiver.firmware_version) can be resolved without a
                # second fetch. Keyed "attributes" so open_value() works on
                # this dict directly. The CLI renders an explicit summary
                # rather than dumping the dict, so this never reaches JSON.
                "attributes": child.get("attributes") or [],
            }
        )
    return devices


def walk_devices(
    client: Any,
    station_ids: List[int],
    *,
    max_workers: int = 8,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[int, List[dict]]:
    """Open-join devices for many stations, walked on a thread pool.

    A failed station (TOS error) yields an empty list + a warning, never
    an abort — a flaky single station must not sink the fleet search.
    Returns ``{id_entity: [device, ...]}``.
    """
    out: Dict[int, List[dict]] = {}
    ids = list(station_ids)
    if not ids:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(station_open_devices, client, sid): sid for sid in ids}
        for done, fut in enumerate(as_completed(futures), 1):
            sid = futures[fut]
            try:
                out[sid] = fut.result()
            except SnapshotMiss:
                raise  # a replay gap is deterministic, not a per-station flake
            except Exception as exc:  # noqa: BLE001 — per-station isolation
                logger.warning("device walk failed for id_entity=%s: %s", sid, exc)
                out[sid] = []
            if progress is not None:
                progress(done, len(ids))
    return out


def stderr_progress(
    no_progress: bool, json_mode: bool
) -> Optional[Callable[[int, int], None]]:
    """Build a stderr progress callback, or None when suppressed.

    Suppressed when ``--no-progress``, ``--json`` (stdout purity), or when
    stderr is not a TTY (CI / pipe — same policy as ``tos device find``).
    """
    if no_progress or json_mode or not sys.stderr.isatty():
        return None

    def emit(done: int, total: int) -> None:
        end = "\n" if done == total else ""
        sys.stderr.write(f"\r  devices: {done}/{total} stations{end}")
        sys.stderr.flush()

    return emit


# ---------------------------------------------------------------------------
# Combined pipeline
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """Everything the renderer needs, criteria summary included."""

    stations: List[dict] = field(default_factory=list)
    predicates: List[Predicate] = field(default_factory=list)
    show_codes: List[str] = field(default_factory=list)
    device_must: List[DeviceSpec] = field(default_factory=list)
    device_must_not: List[DeviceSpec] = field(default_factory=list)
    devices_by_id: Dict[int, List[dict]] = field(default_factory=dict)
    visits_by_id: Dict[int, List[dict]] = field(default_factory=dict)
    contacts_by_id: Dict[int, List[dict]] = field(default_factory=dict)
    at: Optional[str] = None
    history: bool = False
    coord_frames: List[str] = field(default_factory=list)
    project_only: bool = False
    show_order: List[tuple] = field(default_factory=list)

    @property
    def device_filters_active(self) -> bool:
        return bool(self.device_must or self.device_must_not)

    @property
    def attribute_codes(self) -> List[str]:
        """STATION columns: bare predicate codes + bare --show selectors.

        Dotted selectors are device columns and are excluded here — see
        :attr:`device_columns`. With :attr:`project_only`, predicate codes
        are NOT projected — only the bare ``--show`` selectors are.
        """
        codes: List[str] = []
        if not self.project_only:
            for pred in self.predicates:
                if pred.namespace is None and pred.code not in codes:
                    codes.append(pred.code)
        for raw in self.show_codes:
            if "." in raw:
                continue
            if raw not in codes:
                codes.append(raw)
        return codes

    @property
    def device_columns(self) -> List[tuple]:
        """DEVICE columns as ``(namespace, code)``: predicates + --show.

        Visit and contact selectors are excluded here — they get their own
        projection columns (see :attr:`contact_columns` /
        :attr:`visit_columns`). With :attr:`project_only`, predicate device
        selectors are NOT projected — only the ``--show`` ones are.
        """
        cols: List[tuple] = []
        if not self.project_only:
            for pred in self.predicates:
                if pred.namespace is not None and pred.namespace not in (
                    VISIT_NAMESPACE,
                    CONTACT_NAMESPACE,
                ):
                    pair = (pred.namespace, pred.code)
                    if pair not in cols:
                        cols.append(pair)
        for raw in self.show_codes:
            if "." not in raw:
                continue
            pair = parse_selector(raw)
            if pair[0] in (VISIT_NAMESPACE, CONTACT_NAMESPACE):
                continue
            if pair not in cols:
                cols.append(pair)
        return cols

    @property
    def contact_columns(self) -> List[str]:
        """CONTACT projection columns — ``--show contact.*``.

        Contact selectors are filters first; ``--show`` makes them
        projection columns too (the owner/operator org is a thing you want
        to *see*, not just filter on).
        """
        cols: List[str] = []
        for raw in self.show_codes:
            if "." not in raw:
                continue
            pair = parse_selector(raw)
            if pair[0] == CONTACT_NAMESPACE and pair[1] not in cols:
                cols.append(pair[1])
        return cols

    @property
    def visit_columns(self) -> List[str]:
        """VISIT projection columns — ``--show visit.*``."""
        cols: List[str] = []
        for raw in self.show_codes:
            if "." not in raw:
                continue
            pair = parse_selector(raw)
            if pair[0] == VISIT_NAMESPACE and pair[1] not in cols:
                cols.append(pair[1])
        return cols

    def _dedupe(self, values: Iterable[str]) -> List[str]:
        """Preserve-order dedupe for a projection cell."""
        seen: List[str] = []
        for v in values:
            if v not in seen:
                seen.append(v)
        return seen

    @property
    def device_selectors_active(self) -> bool:
        return bool(self.device_columns)

    def station_devices(self, station: dict) -> List[dict]:
        return self.devices_by_id.get(station.get("id_entity"), [])

    def device_column_value(
        self, station: dict, namespace: str, code: str, *, at: Optional[str] = None
    ) -> str:
        """Rendered cell for one device column on one station.

        Every device in the namespace contributes an entry, joined by
        ``, `` — so a station with two receivers shows both firmware
        values rather than whichever the walk happened to return first.
        """
        values = device_values(
            self.station_devices(station), namespace, code, at=at or self.at
        )
        rendered = [str(v) if v is not None else "—" for v in values]
        return ", ".join(rendered) if rendered else "—"

    def contact_values(self, station: dict, code: str) -> List[str]:
        """Raw (deduped) values for one contact column on one station."""
        return self._dedupe(
            _contact_values(self.contacts_by_id.get(station.get("id_entity"), []), code)
        )

    def visit_values(self, station: dict, code: str) -> List[str]:
        """Raw (deduped) values for one visit column on one station."""
        vals: List[str] = []
        for v in self.visits_by_id.get(station.get("id_entity"), []):
            vals.extend(_visit_field_values(v, code))
        return self._dedupe(vals)

    def contact_column_value(self, station: dict, code: str) -> str:
        """Rendered cell for one contact column."""
        vals = self.contact_values(station, code)
        return ", ".join(vals) if vals else "—"

    def visit_column_value(self, station: dict, code: str) -> str:
        """Rendered cell for one visit column."""
        vals = self.visit_values(station, code)
        return ", ".join(vals) if vals else "—"

    @property
    def coord_columns(self) -> List[tuple]:
        """COORDINATE columns as ``(frame, axis)`` — three per frame.

        A frame projects to one column per axis: geocentric → ``X``/``Y``/``Z``,
        projected → ``E``/``N``/``H``, geographic → ``LON``/``LAT``/``H``.
        The rendered header is ``FRAME.AXIS`` (e.g. ``ITRF2008.X``).
        """
        cols: List[tuple] = []
        for frame in self.coord_frames:
            for axis in coord_frame(frame).axis:
                cols.append((frame, axis))
        return cols

    def coord_value(
        self, station: dict, frame: str, axis: str, *, at: Optional[str] = None
    ) -> Optional[float]:
        """One transformed coordinate component for one station.

        Reads TOS ``lat`` / ``lon`` / ``altitude`` (WGS 84) as of ``at`` and
        transforms them to ``frame`` via geofunc, returning the component
        named by ``axis``. ``None`` when any source attribute is missing or
        non-numeric (the caller renders ``—``).
        """
        from geofunc.coords import transform

        frame_obj = coord_frame(frame)
        when = at if at is not None else self.at
        try:
            lat = float(value_at(station, "lat", when))
            lon = float(value_at(station, "lon", when))
            alt = float(value_at(station, "altitude", when))
        except (TypeError, ValueError):
            return None
        vals = transform(lon, lat, alt, src="wgs84", dst=frame)
        return float(vals[frame_obj.axis.index(axis)])

    @property
    def ordered_columns(self) -> List[tuple]:
        """Ordered table columns as descriptors, honouring ``--show`` order.

        Each descriptor is one of ``(kind, ...)``:

        * ``("marker",)`` / ``("name",)`` — identity columns
        * ``("attr", code)`` — bare station attribute
        * ``("device", namespace, code)`` / ``("contact", code)`` /
          ``("visit", code)`` — selector columns
        * ``("coord", frame, axis)`` — one geofunc frame axis (a ``coords.``
          entry expands to three)

        When ``--show`` was given (``show_order`` non-empty), the columns are
        EXACTLY the requested selectors in the requested order — MARKER/NAME
        are NOT auto-added. With no ``--show``, the default is MARKER, NAME,
        then the auto-projected predicate columns.
        """
        if self.show_order:
            cols: List[tuple] = []
            for desc in self.show_order:
                if desc[0] == "coord":
                    frame = desc[1]
                    for axis in coord_frame(frame).axis:
                        cols.append(("coord", frame, axis))
                else:
                    cols.append(desc)
            return cols
        cols = [("marker",), ("name",)]
        for code in self.attribute_codes:
            if code not in ("marker", "name"):
                cols.append(("attr", code))
        for namespace, code in self.device_columns:
            cols.append(("device", namespace, code))
        for code in self.contact_columns:
            cols.append(("contact", code))
        for code in self.visit_columns:
            cols.append(("visit", code))
        for frame, axis in self.coord_columns:
            cols.append(("coord", frame, axis))
        return cols

    def column_header(self, desc: tuple) -> str:
        """Rendered table header for one column descriptor."""
        kind = desc[0]
        if kind == "marker":
            return "MARKER"
        if kind == "name":
            return "NAME"
        if kind == "attr":
            return desc[1].upper()
        if kind == "device":
            return f"{desc[1]}.{desc[2]}".upper()
        if kind == "contact":
            return f"contact.{desc[1]}".upper()
        if kind == "visit":
            return f"visit.{desc[1]}".upper()
        if kind == "coord":
            return f"{desc[1]}.{desc[2]}".upper()
        raise ValueError(f"unknown column kind {kind!r}")

    def column_value(
        self, station: dict, desc: tuple, *, at: Optional[str] = None
    ) -> str:
        """Rendered table cell for one column descriptor."""
        kind = desc[0]
        if kind == "marker":
            return (open_value(station, "marker") or "?").upper()
        if kind == "name":
            return open_value(station, "name") or "—"
        if kind == "attr":
            return value_at(station, desc[1], at if at is not None else self.at) or "—"
        if kind == "device":
            return self.device_column_value(station, desc[1], desc[2], at=at)
        if kind == "contact":
            return self.contact_column_value(station, desc[1])
        if kind == "visit":
            return self.visit_column_value(station, desc[1])
        if kind == "coord":
            v = self.coord_value(station, desc[1], desc[2], at=at)
            if v is None:
                return "—"
            fmt = ".7f" if coord_frame(desc[1]).kind == "geographic" else ".3f"
            return format(v, fmt)
        raise ValueError(f"unknown column kind {kind!r}")

    def timeline(self, station: dict) -> List[tuple]:
        """Segment a station's timeline at every boundary of every column.

        Returns ``[(date_from, date_to), ...]``, oldest first, where the
        edges are the union of the change dates of all selected station
        AND device columns. So a row is a span over which *nothing shown
        changed*, and two columns with different boundaries (firmware
        moving on one date, antenna on another) both stay correct without
        a cross product.

        With no boundaries at all — nothing selected, or nothing dated —
        the result is a single open segment, which renders exactly like
        the non-history table.
        """
        edges = set()
        for code in self.attribute_codes:
            edges.update(period_boundaries(station, code))
        devices = self.station_devices(station)
        for namespace, code in self.device_columns:
            for d in devices:
                if device_in_namespace(d, namespace):
                    edges.update(period_boundaries(d, code))
        starts = sorted(edges)
        if not starts:
            return [(None, None)]
        segments = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else None
            segments.append((start, end))
        return segments


def search_stations(
    client: Any,
    predicates: List[Predicate],
    *,
    device_must: Optional[List[DeviceSpec]] = None,
    device_must_not: Optional[List[DeviceSpec]] = None,
    show_codes: Optional[List[str]] = None,
    domain: str = "geophysical",
    progress: Optional[Callable[[int, int], None]] = None,
    max_workers: int = 8,
    at: Optional[str] = None,
    history: bool = False,
    coord_frames: Optional[List[str]] = None,
    project_only: bool = False,
    show_order: Optional[List[tuple]] = None,
) -> SearchResult:
    """Run the full pipeline: bulk fetch → attribute filter → device walk.

    The device walk (1 + N HTTP calls per station) only touches stations
    that survived the attribute stage, so ``tos search --epos --receiver
    polarx5`` costs one bulk call, ~N_station-history calls, then per-open-
    child calls — not 200 × full device history up front.
    """
    device_must = device_must or []
    device_must_not = device_must_not or []
    raw_show = [c.lower() for c in (show_codes or [])]

    # Station predicates prune the fleet BEFORE any device walk; device
    # predicates can only be evaluated after it.
    station_preds = [p for p in predicates if p.namespace is None]
    device_preds = [
        p
        for p in predicates
        if p.namespace is not None
        and p.namespace not in (VISIT_NAMESPACE, CONTACT_NAMESPACE)
    ]
    visit_preds = [p for p in predicates if p.namespace == VISIT_NAMESPACE]
    contact_preds = [p for p in predicates if p.namespace == CONTACT_NAMESPACE]

    at = as_day(at)
    fleet = fetch_fleet(client, domain=domain)
    # Before filtering, not after: an unknown code silently ANSWERS rather
    # than failing, and a wrong answer here reaches live TOS writes.
    validate_station_codes(fleet, station_preds, raw_show, domain=domain)
    survivors = filter_by_predicates(fleet, station_preds, at=at, history=history)

    # A visit selector walks the vitjun list — one call per surviving
    # station, same isolation shape as the device walk below. A --show
    # visit.* column needs the same walk even with no visit predicate.
    show_visit_cols = any(c.startswith(VISIT_NAMESPACE + ".") for c in raw_show)
    show_contact_cols = any(c.startswith(CONTACT_NAMESPACE + ".") for c in raw_show)
    visits_by_id: Dict[int, List[dict]] = {}
    if visit_preds or show_visit_cols:
        visits_by_id = walk_visits(
            client,
            [s.get("id_entity") for s in survivors if s.get("id_entity")],
            max_workers=max_workers,
            progress=progress,
        )
        if visit_preds:
            survivors = [
                s
                for s in survivors
                if visits_satisfy(visits_by_id.get(s.get("id_entity"), []), visit_preds)
            ]

    # A contact selector walks the contact list (owner/operator orgs).
    contacts_by_id: Dict[int, List[dict]] = {}
    if contact_preds or show_contact_cols:
        contacts_by_id = walk_contacts(
            client,
            [s.get("id_entity") for s in survivors if s.get("id_entity")],
            max_workers=max_workers,
            progress=progress,
        )
        if contact_preds:
            survivors = [
                s
                for s in survivors
                if contacts_satisfy(
                    contacts_by_id.get(s.get("id_entity"), []), contact_preds
                )
            ]

    # A device selector needs the walk even with no --device/--receiver
    # filter — projecting receiver.firmware_version is reason enough.
    show_needs_devices = any(
        "." in c and not c.startswith((VISIT_NAMESPACE + ".", CONTACT_NAMESPACE + "."))
        for c in raw_show
    )
    need_devices = bool(
        device_must or device_must_not or device_preds or show_needs_devices
    )

    devices_by_id: Dict[int, List[dict]] = {}
    if need_devices:
        devices_by_id = walk_devices(
            client,
            [s.get("id_entity") for s in survivors if s.get("id_entity")],
            max_workers=max_workers,
            progress=progress,
        )
        if device_must or device_must_not:
            survivors = [
                s
                for s in survivors
                if devices_satisfy(
                    devices_by_id.get(s.get("id_entity"), []),
                    device_must,
                    device_must_not,
                )
            ]
        if device_preds:
            survivors = [
                s
                for s in survivors
                if all(
                    predicate_matches_devices(
                        devices_by_id.get(s.get("id_entity"), []),
                        p,
                        at=at,
                        history=history,
                    )
                    for p in device_preds
                )
            ]

    # Sort by marker (fallback name), then apply limit upstream in the CLI.
    def _sort_key(s: dict) -> str:
        return norm_value(open_value(s, "marker") or open_value(s, "name") or "")

    survivors.sort(key=_sort_key)

    return SearchResult(
        stations=survivors,
        predicates=list(predicates),
        show_codes=raw_show,
        device_must=device_must,
        device_must_not=device_must_not,
        devices_by_id=devices_by_id,
        visits_by_id=visits_by_id,
        contacts_by_id=contacts_by_id,
        at=at,
        history=history,
        coord_frames=list(coord_frames or []),
        project_only=project_only,
        show_order=list(show_order or []),
    )
