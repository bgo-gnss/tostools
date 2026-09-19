"""Agency reference data for IGS site logs and RINEX headers (deployed config).

Lives in tostools because BOTH site-log callers need it: ``tosGPS sitelog``
and receivers' M3G dissemination. It used to live in
``receivers/dissemination/agencies.py``, which meant only the receivers path
could fill §11/§12/§13 — ``tosGPS sitelog`` silently fell back to the legacy
TOS-contact rendering and produced a DIFFERENT agency block from the one
actually published (single-line mailing address, among others). receivers
re-exports from here, so its own imports are unchanged.

The RINEX header OBSERVER/AGENCY and the IGS site-log §11/§12/§13 need each station's
agency in **English**, with an abbreviation / URL / generic GNSS-team email — none of
which a TOS *contact entity* can carry (single Icelandic ``name``, no english-name /
abbreviation / url field). So a curated ``agencies.yaml`` supplies the presentation,
while TOS contact **roles** say which agency plays each part (see
``docs/architecture/epos-observer-agency-and-sitelog.md``).

``agencies.yaml`` is a **deployed config file** — it lives in ``gps-config-data`` under
version control and is synced to ``~/.config/gpsconfig/`` (or ``GPS_CONFIG_PATH``), the
same mechanism as ``stations.cfg`` / ``sync.yaml``. This module loads it and resolves a
TOS owner organization → :class:`AgencyInfo`, with the IMO default for the operator /
data-center roles a station may not carry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def default_agencies_path() -> Path:
    """``agencies.yaml`` in ``GPS_CONFIG_PATH`` (else ``~/.config/gpsconfig``).

    Mirrors :func:`receivers.archive.config._default_config_path` for ``sync.yaml`` so
    the whole config set resolves the same way.
    """
    base = os.getenv("GPS_CONFIG_PATH")
    root = Path(base) if base else Path.home() / ".config" / "gpsconfig"
    return root / "agencies.yaml"


@dataclass(frozen=True)
class AgencyInfo:
    """One agency's render data (from ``agencies.yaml``).

    English fields (``english_name`` / ``abbrev``) are the international IGS/EPOS site-log
    forms; ``observer`` / ``agency_label`` are the RINEX header strings. ``address`` is a
    tuple of mailing-address lines (site-log Mailing Address is multi-line).
    """

    org: str
    """The Icelandic TOS org key this entry resolves (contact.owner.organization)."""
    english_name: str
    icelandic_name: str = ""
    department_en: str = ""
    department_is: str = ""
    abbrev: str = ""
    """English preferred abbreviation (site-log Preferred Abbreviation)."""
    abbrev_is: str = ""
    observer: str = ""
    """RINEX OBSERVER (e.g. ``GNSSatIMO``)."""
    agency_label: str = ""
    """RINEX AGENCY (≤40 chars, e.g. ``Vedurstofa Islands``)."""
    address: tuple[str, ...] = ()
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    url: str = ""
    dc_label: str = ""
    """Short label used in site-log §13 Data Center fields (defaults to ``abbrev``;
    NATT overrides — the user-facing short name is NATT, not the EN abbrev NSII)."""

    @property
    def rinex_agency(self) -> str:
        """The RINEX AGENCY string (A40 field). Prefer the full English
        institutional name for a human-readable header; fall back to the short
        ``agency_label`` (else ``abbrev``) only when the full name would overflow
        the 40-char field (e.g. IES's "Institute of Earth Sciences, University of
        Iceland")."""
        name = (self.english_name or "").strip()
        if name and len(name) <= 40:
            return name
        return (self.agency_label or self.abbrev or name).strip()


class AgencyResolver:
    """Resolve a TOS owner organization → :class:`AgencyInfo` via ``agencies.yaml``.

    Exact-match by org string; :meth:`resolve` returns None for an unknown org (the
    caller then uses the config/hardcoded default). The operator / data-center
    **defaults** (IMO) back the site-log §11/§13 when a station carries no operator /
    data-owner role — see the role-guided model in the design doc.
    """

    def __init__(
        self, agencies: dict[str, AgencyInfo], defaults: dict[str, str]
    ) -> None:
        self._by_org = agencies
        self._defaults = defaults

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> AgencyResolver:
        """Load from ``agencies.yaml``; a missing/empty file yields an empty resolver.

        Non-fatal by design: the config is optional infrastructure, so a missing file
        must not crash dissemination — callers fall back to their config defaults.
        """
        p = path or default_agencies_path()
        if not p.is_file():
            logger.warning("agencies.yaml not found at %s — using empty resolver", p)
            return cls({}, {})
        try:
            import yaml

            raw = yaml.safe_load(p.read_text()) or {}
        except Exception as exc:  # noqa: BLE001 - bad config ⇒ empty, never fatal
            logger.warning("could not read %s (%s) — using empty resolver", p, exc)
            return cls({}, {})
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgencyResolver:
        """Build from a parsed ``agencies.yaml`` mapping (testable offline)."""
        agencies: dict[str, AgencyInfo] = {}
        for org, d in (raw.get("agencies") or {}).items():
            d = d or {}
            addr = d.get("address") or ()
            if isinstance(addr, str):
                addr = (addr,)
            agencies[org] = AgencyInfo(
                org=org,
                english_name=str(d.get("english_name", "")),
                icelandic_name=str(d.get("icelandic_name", "")),
                department_en=str(d.get("department_en", "")),
                department_is=str(d.get("department_is", "")),
                abbrev=str(d.get("abbrev", "")),
                abbrev_is=str(d.get("abbrev_is", "")),
                observer=str(d.get("observer", "")),
                agency_label=str(d.get("agency_label", "")),
                address=tuple(str(x) for x in addr),
                contact_name=str(d.get("contact_name", "")),
                phone=str(d.get("phone", "")),
                email=str(d.get("email", "")),
                url=str(d.get("url", "")),
                dc_label=str(d.get("dc_label") or d.get("abbrev", "")),
            )
        defaults = {k: str(v) for k, v in (raw.get("defaults") or {}).items()}
        return cls(agencies, defaults)

    # -- resolution --------------------------------------------------------
    def resolve(self, org: Optional[str]) -> Optional[AgencyInfo]:
        """The :class:`AgencyInfo` for ``org``, or None if unknown/blank."""
        if not org:
            return None
        return self._by_org.get(org.strip())

    def default_agency(self) -> Optional[AgencyInfo]:
        """Fallback :class:`AgencyInfo` for a station with no resolvable owner org —
        IMO, which owns and operates the fleet (``defaults.operator_agency``). Drives
        the RINEX OBSERVER/AGENCY default, so both come from agencies.yaml (English
        name via :attr:`AgencyInfo.rinex_agency`), not a sync.yaml literal."""
        return self.resolve(self._defaults.get("operator_agency"))

    def resolve_by_code(self, code: Optional[str]) -> Optional[AgencyInfo]:
        """The :class:`AgencyInfo` whose English ``abbrev`` or Icelandic
        ``abbrev_is`` matches ``code`` (case-insensitive) — the stations.cfg
        ``station_operator`` key (e.g. ``IMO`` / ``NATT`` / ``IES``). This is the
        offline join used by the daily convert (no TOS call), the mirror of
        :meth:`resolve` which keys on the TOS owner-org string. None if
        unknown/blank."""
        if not code:
            return None
        c = code.strip().upper()
        for info in self._by_org.values():
            if c in {
                (info.abbrev or "").strip().upper(),
                (info.abbrev_is or "").strip().upper(),
            }:
                return info
        return None

    def operator_default(self) -> Optional[AgencyInfo]:
        """§11 On-Site POC default (``defaults.operator_agency`` → its AgencyInfo)."""
        return self.resolve(self._defaults.get("operator_agency"))

    def data_center_default(self) -> Optional[AgencyInfo]:
        """§13 Primary Data Center default (``defaults.data_center_agency``)."""
        return self.resolve(self._defaults.get("data_center_agency"))

    def url_default(self) -> str:
        """§13 URL for More Information (``defaults.url``)."""
        return self._defaults.get("url", "")


# ---------------------------------------------------------------------------
# Site-log §11/§12/§13 resolution
# ---------------------------------------------------------------------------


def agency_dict(info: Any) -> Dict[str, Any]:
    """``AgencyInfo`` → the plain dict :func:`site_log` renders (§11/§12)."""
    return {
        "name_lines": [ln for ln in (info.english_name, info.department_en) if ln],
        "abbrev": info.abbrev,
        "address": list(info.address),
        "contact_name": info.contact_name,
        "phone": info.phone,
        "email": info.email,
    }


def _relationship_is_open(row: Dict[str, Any]) -> bool:
    """True when a contact relationship has no end — i.e. it holds *now*."""
    return not (row.get("per_time_to") or "").strip()


def _relationship_recency(row: Dict[str, Any]) -> tuple:
    """Sort key placing the relationship that speaks for the station *today* first.

    An OPEN period always outranks a closed one; among closed periods the
    latest start wins. ISO-8601 timestamps compare correctly as strings, so no
    parsing (and no date-library import) is needed. A missing ``per_time_from``
    degrades to the empty string, sorting last within its class.
    """
    return (1 if _relationship_is_open(row) else 0, (row.get("per_time_from") or "").strip())


def station_role_orgs(client: Any, meta: Dict[str, Any]) -> Dict[str, str]:
    """The station's contact-role → organization map from raw TOS contacts.

    Reads ``get_contacts(id_entity)`` (raw rows) rather than the processed
    ``meta['contact']`` dict — the processed view keeps one contact per bucket
    and its 'eigandi' substring match cannot distinguish *Eigandi stöðvar*
    (station owner) from *Eigandi gagna* (data owner). Best-effort: any failure
    → empty map (the IMO defaults then apply).

    A role may carry SEVERAL relationships, because ownership moves: SENG has an
    IES owner closed 2025-08-19 *and* an IMO owner still open, and the raw rows
    arrive closed-row-first. Picking the first row per role would therefore name
    a superseded agency in §12 while an open relationship says otherwise — the
    stale-owner bug. Resolve by DATE instead: the OPEN period wins, and when no
    period is open the most recently started one does.
    """
    try:
        rows = client.get_contacts(meta.get("id_entity")) or []
    except Exception as exc:  # noqa: BLE001 - roles are enrichment, not required
        logger.warning("site log: contact-role lookup failed: %s", exc)
        return {}

    # bucket -> (recency key, org); the first row wins a tie, so an unrelated
    # duplicate relationship cannot displace an equally-recent sibling.
    best: Dict[str, tuple] = {}
    for row in rows:
        role = f"{row.get('role_is') or ''} {row.get('role') or ''}".lower()
        org = (row.get("organization") or row.get("name") or "").strip()
        if not org:
            continue
        # 'eigandi gagna' MUST be tested first: the role string carries both the
        # Icelandic and the English form, so a data owner also matches 'owner'.
        if "eigandi gagna" in role or "data_owner" in role or "data owner" in role:
            bucket = "data_owner"
        elif "eigandi" in role or "owner" in role:
            bucket = "owner"
        else:
            continue
        key = _relationship_recency(row)
        if bucket not in best or key > best[bucket][0]:
            best[bucket] = (key, org)
    return {bucket: org for bucket, (_key, org) in best.items()}


def resolve_sitelog_agencies(
    client: Any, meta: Dict[str, Any], resolver: Any = None
) -> Dict[str, Any]:
    """Role-guided §11/§12/§13 agency data (TOS roles = who, agencies.yaml = render).

    - §11 On-Site POC        ← always the IMO default: IMO runs the network and
      disseminates the data, so it is the on-site/data point of contact even when
      TOS records another org as Rekstraraðili (that upkeep role belongs in §12).
    - §12 Responsible Agency ← owner role — only when it differs from §11.
    - §13 Primary DC         ← data-owner role, else the IMO default;
      Secondary DC           ← owner (when ≠ primary); URL ← agencies.yaml default.

    Unknown-org fallbacks keep the log renderable: an owner org missing from
    agencies.yaml is emitted by its raw TOS name (never dropped silently).

    Pass the result as ``site_log(..., agencies=...)``. Omitting it drops the
    renderer back to the legacy TOS-contact rendering, which produces a
    *different* §11 (single-line mailing address) from what gets published —
    the reason this now lives in tostools rather than in receivers.
    """
    if resolver is None:
        resolver = AgencyResolver.load()
    roles = station_role_orgs(client, meta)

    poc_info = resolver.operator_default()
    dc_info = (
        resolver.resolve(roles.get("data_owner")) or resolver.data_center_default()
    )
    owner_org = roles.get("owner") or ""
    owner_info = resolver.resolve(owner_org)

    agencies: Dict[str, Any] = {
        "poc": agency_dict(poc_info) if poc_info else None,
        "responsible": None,
        "data_center": {
            "primary": dc_info.dc_label if dc_info else "",
            "secondary": "",
            "url": resolver.url_default(),
        },
    }
    # §12 only when the responsible (owner) agency differs from the §11 contact.
    poc_org = poc_info.org if poc_info else ""
    if owner_org and owner_org != poc_org:
        agencies["responsible"] = (
            agency_dict(owner_info) if owner_info else {"name_lines": [owner_org]}
        )
    # §13 secondary = the owner, when it isn't already the primary data center.
    dc_org = dc_info.org if dc_info else ""
    if owner_org and owner_org != dc_org:
        agencies["data_center"]["secondary"] = (
            owner_info.dc_label if owner_info else owner_org
        )
    return agencies
