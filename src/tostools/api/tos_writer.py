"""TOS API write client with JWT authentication.

Companion to :class:`TOSClient` (read-only). TOSWriter adds authenticated
write operations via the TOS REST API.

Credential resolution order (highest wins):
1. Constructor args ``username`` / ``password``
2. ``TOS_USERNAME`` / ``TOS_PASSWORD`` environment variables
3. ``[tos]`` section in ``database.cfg``:
   - ``password_pass_path`` / ``username_pass_path`` — retrieve from pass(1) store
   - ``username`` / ``password`` — plaintext fallback (avoid for password)
4. Interactive ``getpass`` prompt (always available as last resort)

The JWT token is kept in memory only — never written to disk. Expiry is
read from the ``exp`` claim in the response body; the client re-logs in
automatically when the token is within 60 s of expiry or on HTTP 401.

All mutating methods (POST, PATCH) respect a ``dry_run`` flag (default
``True``). In dry-run mode the request is logged but not sent; a
:class:`DryRunResult` is returned so callers can inspect the payload.
"""

from __future__ import annotations

import base64
import configparser
import getpass
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from ..utils.logging import get_logger

DEFAULT_TOS_URL = "https://vi-api.vedur.is/tos/v1"
DEFAULT_TIMEOUT = 15
_TOKEN_EXPIRY_BUFFER_S = 60  # re-login this many seconds before expiry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DryRunResult:
    """Returned by mutating methods when dry_run=True."""

    method: str
    endpoint: str
    payload: Optional[Dict[str, Any]]

    def __repr__(self) -> str:
        return f"DryRunResult({self.method} {self.endpoint})"


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _find_database_cfg() -> Optional[Path]:
    """Locate database.cfg using the same search order as receivers."""
    candidates: List[Path] = []

    gps_config_env = os.environ.get("GPS_CONFIG_PATH")
    if gps_config_env:
        candidates.append(Path(gps_config_env) / "database.cfg")  # type: ignore[arg-type]

    try:
        import gps_parser  # type: ignore[import]

        config_dir = gps_parser.ConfigParser().config_path
        if config_dir:
            candidates.append(Path(config_dir) / "database.cfg")
    except Exception:
        pass

    candidates.append(Path.home() / ".config" / "gpsconfig" / "database.cfg")

    for p in candidates:
        if p.is_file():
            return p
    return None


def _load_from_pass(pass_spec: str) -> Optional[str]:
    """Return a value from a pass(1) entry, or None on any error.

    ``pass_spec`` is either a bare entry path (returns the first line, i.e.
    the password) or ``entry_path:field_name`` (returns the value of a named
    field from the multiline body).

    Conventional pass multiline format::

        <password>          ← first line, returned when no field given
        username: bgo       ← returned when field='username'
        url: https://...

    Examples::

        _load_from_pass("accounts/bgo")             → password (first line)
        _load_from_pass("accounts/bgo:username")     → "bgo"

    The subprocess stdout is captured and never logged. Stderr is discarded.
    Returns None if pass is not installed, the entry does not exist, the GPG
    key is unavailable, or the requested field is not found in the entry.
    """
    # Split path:field — a bare path has no colon (Windows paths notwithstanding;
    # pass entries use slash-only paths so a colon always means a field name).
    if ":" in pass_spec:
        entry_path, field_name = pass_spec.split(":", 1)
        entry_path = entry_path.strip()
        field_name = field_name.strip().lower()
    else:
        entry_path = pass_spec.strip()
        field_name = None

    try:
        result = subprocess.run(
            ["pass", "show", entry_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.split("\n")
        if field_name is None:
            # No field requested → return the first non-empty line (the password)
            return lines[0].strip() or None
        # Named field → search subsequent lines for "field: value"
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.lower().startswith(field_name + ":"):
                value = stripped[len(field_name) + 1 :].strip()
                return value or None
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _load_credentials_from_cfg(
    cfg_path: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return (username, password) from database.cfg [tos] section, or (None, None).

    Supports three forms in the ``[tos]`` section:

    Plain text (avoid for password)::

        [tos]
        username = bgo
        password = secret

    Separate pass entries::

        [tos]
        username = bgo
        password_pass_path = accounts/bgo

    Single shared pass entry (recommended when one account spans services)::

        [tos]
        username_pass_path = accounts/bgo:username
        password_pass_path = accounts/bgo

    The ``path:field`` syntax reads a named field from the multiline pass
    entry body (e.g. ``username: bgo``). Without a field suffix the first
    line (the password) is returned. Plain-text ``username`` / ``password``
    are used as fallbacks when the pass-path key is absent.
    """
    path = cfg_path or _find_database_cfg()
    if path is None:
        return None, None
    try:
        cp = configparser.ConfigParser()
        cp.read(path)

        # Username: prefer pass-path, fall back to plaintext
        username: Optional[str] = None
        u_pass_path = cp.get("tos", "username_pass_path", fallback=None)
        if u_pass_path:
            username = _load_from_pass(u_pass_path.strip())
        if not username:
            username = cp.get("tos", "username", fallback=None) or None

        # Password: prefer pass-path, fall back to plaintext
        password: Optional[str] = None
        p_pass_path = cp.get("tos", "password_pass_path", fallback=None)
        if p_pass_path:
            password = _load_from_pass(p_pass_path.strip())
        if not password:
            password = cp.get("tos", "password", fallback=None) or None

        return username, password
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# TOSWriter
# ---------------------------------------------------------------------------


class TOSWriter:
    """Authenticated write client for the TOS REST API.

    Args:
        base_url: TOS API base URL.
        dry_run: When True (default), mutating requests are logged but not
            sent. Set to False only after confirming a safe target environment.
        username: TOS login username. Resolved from env/config if omitted.
        password: TOS login password. Resolved from env/config if omitted.
        cfg_path: Explicit path to ``database.cfg``. Auto-discovered if None.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_TOS_URL,
        *,
        dry_run: bool = True,
        username: Optional[str] = None,
        password: Optional[str] = None,
        cfg_path: Optional[Path] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout
        self._logger = get_logger(__name__)

        self._username = username
        self._password = password
        self._cfg_path = cfg_path

        self._token: Optional[str] = None
        self._token_exp: float = 0.0

        # Lazily-populated ``code → id_attribute`` map from
        # ``GET /admin_attribute_rows``. Used by ``add_attribute_value``,
        # which posts to the admin endpoint (the only attribute-value POST
        # path that accepts our tokens — the public ``/attribute_values``
        # endpoint returns confusing 401s, see :meth:`add_attribute_value`).
        self._id_attribute_cache: Optional[Dict[str, int]] = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _resolve_credentials(self) -> tuple[str, str]:
        """Return (username, password), prompting interactively as last resort."""
        username = self._username
        password = self._password

        if not username:
            username = os.environ.get("TOS_USERNAME")
        if not password:
            password = os.environ.get("TOS_PASSWORD")

        if not username or not password:
            cfg_user, cfg_pass = _load_credentials_from_cfg(self._cfg_path)
            username = username or cfg_user
            password = password or cfg_pass

        if not username:
            username = input("TOS username: ").strip()
        if not password:
            password = getpass.getpass("TOS password: ")

        return username, password

    def _token_valid(self) -> bool:
        return (
            self._token is not None
            and time.time() + _TOKEN_EXPIRY_BUFFER_S < self._token_exp
        )

    def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """Authenticate and store JWT in memory.

        Raises:
            requests.HTTPError: On authentication failure.
            ValueError: If response is missing the expected token fields.
        """
        if username:
            self._username = username
        if password:
            self._password = password

        user, pwd = self._resolve_credentials()

        credentials = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        url = f"{self.base_url}/login"
        self._logger.debug("POST %s (login)", url)

        resp = requests.post(
            url,
            headers={"Authorization": f"Basic {credentials}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()

        data = resp.json()
        # TOS login response uses 'sid' for the JWT and 'ttl' (seconds) for expiry.
        token = data.get("sid") or data.get("token")
        if not token:
            raise ValueError(f"TOS login response missing 'sid'/'token': {data}")

        profile = data.get("profile", {})
        scope = profile.get("scope") or data.get("scope", [])
        tos_scopes = {"API.TOS.Admin", "API.TOS.User", "TOS"}
        if not any(s in tos_scopes for s in scope):
            self._logger.warning("TOS token scope %r may not permit writes", scope)

        ttl = data.get("ttl")
        exp = data.get("exp")
        if ttl is not None:
            self._token_exp = time.time() + float(ttl)
        elif exp is not None:
            self._token_exp = float(exp)
        else:
            self._token_exp = self._parse_exp_from_jwt(token)

        self._token = token
        user = data.get("user") or profile.get("user")
        self._logger.info(
            "TOS login OK — user=%s, exp=%s, scope=%s",
            user,
            self._token_exp,
            scope,
        )

    @staticmethod
    def _parse_exp_from_jwt(token: str) -> float:
        """Decode ``exp`` from the JWT payload (no signature verification)."""
        try:
            _, payload_b64, _ = token.split(".")
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return float(payload["exp"])
        except Exception:
            # Treat as "never expires" for this session — will re-login on 401.
            return time.time() + 3600.0

    def _ensure_authenticated(self) -> None:
        if not self._token_valid():
            self._logger.debug("Token missing or near-expiry — re-logging in")
            self.login()

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        *,
        _retry: bool = True,
        _force_send: bool = False,
    ) -> Any:
        """Send an authenticated HTTP request.

        GET requests are always sent (reads are safe). Mutating requests
        (POST, PATCH, PUT, DELETE) are intercepted in dry-run mode unless
        ``_force_send=True`` — used for read-only POST endpoints like
        ``/basic_search/``.
        """
        self._ensure_authenticated()

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        is_mutating = method.upper() not in ("GET", "HEAD", "OPTIONS")

        if is_mutating and self.dry_run and not _force_send:
            self._logger.info(
                "[DRY-RUN] %s %s — payload: %s",
                method.upper(),
                url,
                json.dumps(data, default=str),
            )
            return DryRunResult(method=method.upper(), endpoint=endpoint, payload=data)

        self._logger.debug("%s %s", method.upper(), url)

        resp = requests.request(
            method.upper(),
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(data) if data is not None else None,
            params=params,
            timeout=self.timeout,
        )

        if resp.status_code == 401 and _retry:
            self._logger.debug("Got 401 — refreshing token and retrying")
            self._token = None
            self._token_exp = 0.0
            self._ensure_authenticated()
            return self._request(
                method, endpoint, data=data, params=params, _retry=False
            )

        if not resp.ok:
            body = ""
            try:
                body = f" — body: {resp.text[:500]}"
            except Exception:
                pass
            resp.reason = f"{resp.reason}{body}"
            resp.raise_for_status()

        if resp.content:
            return resp.json()
        return None

    # ------------------------------------------------------------------
    # Read helpers (safe to call without dry_run concern)
    # ------------------------------------------------------------------

    def get_attribute_templates(
        self,
        entity_type: str = "station",
        entity_subtype: str = "geophysical",
    ) -> List[Dict[str, Any]]:
        """Return the attribute template list for the given entity type/subtype."""
        result = self._request(
            "GET",
            f"/attribute_templates/entity_type/{entity_type}/entity_subtype/{entity_subtype}",
        )
        if isinstance(result, list):
            return result
        return []

    def get_entity_history(self, id_entity: int) -> Optional[Dict[str, Any]]:
        """Return the full history dict for an entity."""
        return self._request("GET", f"/history/entity/{id_entity}/")

    def find_device_by_serial(
        self,
        entity_subtype: str,
        serial_number: str,
    ) -> Optional[Dict[str, Any]]:
        """Look up a device entity by serial number, filtered to a subtype.

        Uses ``POST /basic_search/`` to find entities by serial_number, then
        ``GET /entity/{id}/`` on each candidate to verify the
        ``code_entity_subtype`` matches. Match is exact (case-sensitive, no
        whitespace normalization).

        Args:
            entity_subtype: e.g. ``"gnss_receiver"``, ``"antenna"``, ``"radome"``.
            serial_number: serial to search for. Empty/None returns ``None``.

        Returns:
            First matching entity dict (``id_entity``, ``code_entity_subtype``,
            ``attributes``, ...) or ``None`` if no device with that
            (subtype, serial) exists.
        """
        if not serial_number:
            return None

        results = self._request(
            "POST",
            "/basic_search/",
            data={"search_term": serial_number},
            _force_send=True,
        )
        if not isinstance(results, list):
            return None

        for hit in results:
            if hit.get("code") != "serial_number":
                continue
            if hit.get("distance") != 0:
                continue
            if hit.get("value_varchar") != serial_number:
                continue
            device_id = hit.get("id_lvl_three")
            if not device_id:
                continue
            entity = self._request("GET", f"/entity/{device_id}/")
            if entity and entity.get("code_entity_subtype") == entity_subtype:
                return entity
        return None

    def get_attribute_values(
        self,
        id_entity: int,
        code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return attribute values for an entity, optionally filtered by code.

        Uses the entity history endpoint.  Attribute records include
        ``id_attribute_value`` which is used by :meth:`upsert_attribute_value`
        to PATCH existing records in-place.
        """
        history = self.get_entity_history(id_entity)
        if not history:
            return []
        attrs: List[Dict[str, Any]] = history.get("attributes") or []
        if code is not None:
            attrs = [a for a in attrs if a.get("code") == code]
        return attrs

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_entity(
        self,
        entity_subtype: str,
        attributes: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
    ) -> Any:
        """Create a new entity with initial attributes.

        Args:
            entity_subtype: e.g. ``"geophysical"`` for GPS stations.
            attributes: List of attribute dicts, each with keys
                ``code``, ``value``, ``date_from`` (ISO-8601), ``date_to``.
            dry_run: Override the instance-level dry_run for this call.

        Returns:
            API response dict, or :class:`DryRunResult`.
        """
        with _dry_run_override(self, dry_run):
            return self._request(
                "POST",
                "/entities",
                data={"entity_subtype": entity_subtype, "attributes": attributes},
            )

    def create_device(
        self,
        entity_subtype: str,
        attributes: List[Dict[str, Any]],
        *,
        force: bool = False,
        dry_run: Optional[bool] = None,
    ) -> Any:
        """Create a device entity (gnss_receiver, antenna, radome, ...).

        Wraps :meth:`create_entity` with a duplicate-serial guard:
        :meth:`find_device_by_serial` is called before POSTing. A
        :class:`ValueError` is raised if the (subtype, serial_number) pair
        already exists. Pass ``force=True`` to override — should be a manual
        conscious decision (warranty replacement, manufacturer reusing a
        serial across product lines, etc.).

        Args:
            entity_subtype: device subtype, e.g. ``"gnss_receiver"``.
            attributes: list of ``{code, value, date_from, date_to?}`` dicts;
                must include a ``serial_number`` attribute.
            force: skip the duplicate-serial guard.
            dry_run: per-call override of instance-level dry_run.

        Returns:
            API response dict from :meth:`create_entity`, or
            :class:`DryRunResult` in dry-run mode.

        Raises:
            ValueError: no ``serial_number`` attribute in ``attributes``;
                or duplicate (subtype, serial_number) exists and not ``force``.
        """
        serial = next(
            (a.get("value") for a in attributes if a.get("code") == "serial_number"),
            None,
        )
        if serial is None or serial == "":
            raise ValueError(
                "create_device requires a non-empty 'serial_number' attribute "
                "(use create_entity to bypass the duplicate-serial guard)"
            )

        if not force:
            existing = self.find_device_by_serial(entity_subtype, str(serial))
            if existing is not None:
                existing_id = existing.get("id_entity")
                raise ValueError(
                    f"Device with serial_number={serial!r} already exists "
                    f"as {entity_subtype} (id_entity={existing_id}). "
                    f"Pass force=True to add a duplicate."
                )

        return self.create_entity(
            entity_subtype=entity_subtype,
            attributes=attributes,
            dry_run=dry_run,
        )

    @staticmethod
    def _tos_date(dt: Optional[str]) -> Optional[str]:
        """Normalise a date string to TOS format ``YYYY-MM-DDTHH:MM:SS``.

        TOS rejects timezone offsets (+00:00 / Z) AND date-only inputs
        (``2026-05-13``). It wants a full datetime. Strip the timezone
        and promote bare dates to midnight on that day. Empirically
        confirmed against the live API on 2026-05-13: ``time_to`` and
        ``date_to`` columns both 400 on date-only inputs with
        ``Value is not valid time``.
        """
        if dt is None:
            return None
        import re as _re

        # Remove trailing timezone: +HH:MM, -HH:MM, or Z
        dt = _re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", dt)
        # Promote date-only YYYY-MM-DD → YYYY-MM-DDT00:00:00
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt):
            dt = f"{dt}T00:00:00"
        return dt

    def _resolve_id_attribute(self, code: str) -> int:
        """Look up the integer ``id_attribute`` FK for an attribute ``code``.

        Loads the full attribute table from ``GET /admin_attribute_rows``
        on first call and caches it on the instance — there's no
        ``/attributes/<code>`` lookup endpoint per the OpenAPI spec
        (confirmed 2026-05-20), so client-side filtering is the only path.

        Raises :class:`ValueError` if the code isn't present in TOS's
        attribute catalog — surfaces typos at the boundary rather than
        sending an unresolvable POST.
        """
        if self._id_attribute_cache is None:
            rows = self._request("GET", "/admin_attribute_rows")
            self._id_attribute_cache = {
                r["code"]: int(r["id"])
                for r in (rows or [])
                if r.get("code") and r.get("id") is not None
            }
        if code not in self._id_attribute_cache:
            raise ValueError(
                f"unknown attribute code {code!r} — not in "
                "/admin_attribute_rows. Check spelling or refresh the "
                "writer instance to bust the cache."
            )
        return self._id_attribute_cache[code]

    def add_attribute_value(
        self,
        id_entity: int,
        code: str,
        value: str,
        date_from: str,
        date_to: Optional[str] = None,
    ) -> Any:
        """Add an attribute value to an existing entity.

        Routes through ``POST /admin_attribute_value_rows`` rather than
        the public ``/attribute_values`` endpoint. The public endpoint
        returns 401 ``"User provided an invalid token"`` for many
        (entity, code) combinations even with valid ``jwt_auth_simple``
        tokens — confirmed empirically 2026-05-20 on the live API. The
        admin endpoint accepts the same JWT bearer (we already require
        ``API.TOS.Admin`` in the scope claim for other writes) and works
        cleanly. Mirrors the precedent set by :meth:`update_entity_subtype`,
        which uses ``/admin_entity_row/`` for the same reason.

        Tradeoff: the admin endpoint takes an integer ``id_attribute`` FK
        rather than the string ``code``, so we cache the catalog from
        ``GET /admin_attribute_rows`` on first call (see
        :meth:`_resolve_id_attribute`).

        Does NOT check for existing values — use
        :meth:`upsert_attribute_value` for idempotent writes.
        """
        return self._request(
            "POST",
            "/admin_attribute_value_rows",
            data={
                "id_entity": id_entity,
                "id_attribute": self._resolve_id_attribute(code),
                "value_varchar": value,
                "date_from": self._tos_date(date_from),
                "date_to": self._tos_date(date_to),
            },
        )

    def patch_attribute_value(
        self,
        id_attribute_value: int,
        *,
        value: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Any:
        """Modify an existing attribute value by its primary key.

        Only fields that are not None are included in the PATCH body.
        """
        body: Dict[str, Any] = {}
        if value is not None:
            body["value"] = value
        if date_from is not None:
            body["date_from"] = self._tos_date(date_from)
        if date_to is not None:
            body["date_to"] = self._tos_date(date_to)
        if not body:
            raise ValueError(
                "patch_attribute_value: at least one field must be provided"
            )
        return self._request(
            "PATCH", f"/attribute_value/{id_attribute_value}", data=body
        )

    def upsert_attribute_value(
        self,
        id_entity: int,
        code: str,
        value: str,
        date_from: str,
        date_to: Optional[str] = None,
        *,
        date_hint: Optional[str] = None,
    ) -> Any:
        """Idempotent write: PATCH the matching attribute value if it exists, else POST.

        By default (``date_hint=None``), "matching" means the most recent
        **open** value with ``date_to`` of ``None`` — Pattern 1 (correct the
        current value in place, history-destructive).

        When ``date_hint`` is an ISO-8601 date/datetime string, the method
        instead finds the attribute period that *covers* that date (whether
        open or closed) and PATCHes it — Pattern 4 (correct a historical
        value). If no period covers ``date_hint``, the method falls back to
        POSTing a new value — same as when no matching period exists.

        Only the value field is updated on PATCH; the dates are left
        unchanged unless they differ from what we intend to set.

        Returns the PATCH or POST response, or :class:`DryRunResult`.
        """
        existing = self.get_attribute_values(id_entity, code)

        if date_hint is not None:
            # Pattern 4 — find the period covering date_hint.
            # Normalise bare YYYY-MM-DD to full datetime so comparisons
            # against TOS-stored datetimes work (same logic as
            # _normalise_iso_for_compare in devices.py).
            hint_n = self._tos_date(date_hint)
            if hint_n is None:
                raise ValueError("upsert_attribute_value: date_hint resolved to None")
            for a in existing:
                df = a.get("date_from") or ""
                dt = a.get("date_to")
                if df > hint_n:
                    continue
                if dt is not None and dt <= hint_n:
                    continue
                # This period covers date_hint — PATCH it.
                if a.get("value") == value:
                    return a  # already correct, skip PATCH
                id_av = a.get("id_attribute_value")
                if id_av is None:
                    self._logger.warning(
                        "upsert_attribute_value: period for %s/%s covering %s has no id"
                        " — falling back to POST",
                        id_entity,
                        code,
                        date_hint,
                    )
                    break
                patch_body: Dict[str, Any] = {"value": value}
                return self._request(
                    "PATCH", f"/attribute_value/{id_av}", data=patch_body
                )
            # No period covering date_hint (or it had no id) — fall through to POST.
            return self.add_attribute_value(id_entity, code, value, date_from, date_to)

        # Default: Pattern 1 — target the most recent open value.
        open_values = [a for a in existing if a.get("date_to") is None]

        if open_values:
            # Take the most recent open value by date_from
            current = max(open_values, key=lambda a: a.get("date_from", ""))
            if current.get("value") == value:
                return current  # already correct, skip PATCH
            id_av = current.get("id_attribute_value")
            if id_av is None:
                self._logger.warning(
                    "upsert_attribute_value: open value for %s/%s has no id"
                    " — falling back to POST",
                    id_entity,
                    code,
                )
            else:
                patch_body: Dict[str, Any] = {"value": value}
                return self._request(
                    "PATCH", f"/attribute_value/{id_av}", data=patch_body
                )

        return self.add_attribute_value(id_entity, code, value, date_from, date_to)

    def create_entity_connection(
        self,
        id_parent: int,
        id_child: int,
        time_from: str,
        time_to: Optional[str] = None,
    ) -> Any:
        """Create a parent→child entity connection (e.g. station → receiver).

        Args:
            id_parent: Parent entity id (e.g. station id_entity).
            id_child: Child entity id (e.g. gnss_receiver id_entity).
            time_from: ISO-8601 start of the connection.
            time_to: ISO-8601 end, or ``None`` for currently active.
        """
        return self._request(
            "POST",
            "/joins",
            data={
                "id_entity_parent": id_parent,
                "id_entity_child": id_child,
                "time_from": time_from,
                "time_to": time_to,
            },
        )

    def patch_entity_connection(
        self,
        id_connection: int,
        **kwargs: Any,
    ) -> Any:
        """Modify a join record (e.g. close or extend a device session).

        Accepted kwargs: ``time_from``, ``time_to``, ``id_entity_parent``,
        ``id_entity_child``. Date-shaped kwargs are normalised through
        :meth:`_tos_date` so callers can pass ``YYYY-MM-DD`` and have it
        promoted to a full datetime (TOS rejects date-only inputs on
        the join endpoint with HTTP 400).
        """
        if not kwargs:
            raise ValueError(
                "patch_entity_connection: at least one field must be provided"
            )
        for key in ("time_from", "time_to"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = self._tos_date(kwargs[key])
        return self._request("PATCH", f"/join/{id_connection}", data=kwargs)

    def transition_attribute_value(
        self,
        id_entity: int,
        code: str,
        new_value: str,
        transition_date: str,
    ) -> Dict[str, Any]:
        """Close the currently-open attribute period and open a new one.

        Unlike :meth:`upsert_attribute_value` (which PATCHes ``value`` on
        the open period in place — overwriting history), this method
        *preserves* the historical record by closing the existing period
        with ``date_to=<transition_date>`` and then opening a new period
        starting on the same date with ``new_value``. Two HTTP calls.

        Canonical use case: marking a device as retired. The device was
        ``status=virkt`` from 1992 to today; on retirement we want TOS
        to show ``virkt`` from 1992-05-28 to 2025-12-31, and ``óvirkt``
        from 2025-12-31 onwards. ``upsert_attribute_value`` would
        clobber the 33-year ``virkt`` history; this method keeps it.

        If no open period exists for ``code``, falls back to
        :meth:`add_attribute_value` and just opens the new period — no
        close to do, no error.

        Args:
            id_entity: The entity whose attribute we're transitioning.
            code: Attribute code (e.g. ``"status"``).
            new_value: The value for the new period (e.g. ``"óvirkt"``).
            transition_date: ISO-8601 date for both the close of the old
                period (``date_to``) and the open of the new
                (``date_from``).

        Returns:
            ``{"closed": <patch_response>, "opened": <post_response>}``.
            In dry-run mode the values are :class:`DryRunResult`.
            ``closed`` is ``None`` when there was no pre-existing open
            period to close.
        """
        existing = self.get_attribute_values(id_entity, code)
        open_periods = [a for a in existing if a.get("date_to") is None]
        closed_resp: Any = None
        if open_periods:
            # Most recent open period by date_from wins (defensive — the
            # invariant is "exactly one open period per (id_entity, code)"
            # but we don't trust callers' assumption).
            current = max(open_periods, key=lambda a: a.get("date_from") or "")
            id_av = current.get("id_attribute_value") or current.get("id")
            if id_av is not None:
                closed_resp = self.patch_attribute_value(
                    int(id_av), date_to=transition_date
                )
        opened_resp = self.add_attribute_value(
            id_entity,
            code=code,
            value=new_value,
            date_from=transition_date,
        )
        return {"closed": closed_resp, "opened": opened_resp}

    def update_entity_subtype(
        self,
        id_entity: int,
        id_entity_subtype: int,
    ) -> Any:
        """Reclassify an existing entity by changing its subtype.

        .. warning::

           This method hits the **admin** endpoint
           ``PUT /admin_entity_row/<id_entity>`` — the only non-public
           endpoint used by ``TOSWriter``. The public ``/entity/<id>``
           is read-only (``Allow: HEAD, GET, OPTIONS``). Use only when
           you have admin-level TOS access and a confirmed subtype
           integer FK; prefer the attribute and join verbs for routine
           writes.

        Uses the admin endpoint ``PUT /admin_entity_row/<id_entity>``,
        the only TOS verb that lets us flip ``code_entity_subtype`` on
        an entity record (the public ``/entity/<id>`` is read-only —
        ``Allow: HEAD, GET, OPTIONS``).

        TOS keys subtypes by integer FK (``id_entity_subtype``), not by
        the string ``code`` — pass the int here. Resolve string codes
        via ``GET /entity_subtypes/`` (see :func:`tostools.tos._fetch_subtype_id_by_code`).

        The canonical use case is fixing a misclassified entity — e.g.
        a u-blox GPS clock that TOS recorded as a ``gnss_receiver`` (id
        49), needs to be ``gps_clock`` (id 29). The model / serial /
        join graph were all correct; only the subtype label was wrong,
        and that misclassification propagates into every audit and
        report downstream.

        Args:
            id_entity: The entity primary key to reclassify.
            id_entity_subtype: The new subtype's integer FK.

        Returns:
            API response dict, or :class:`DryRunResult` in dry-run mode.
        """
        return self._request(
            "PUT",
            f"/admin_entity_row/{id_entity}",
            data={"id_entity_subtype": int(id_entity_subtype)},
        )


# ---------------------------------------------------------------------------
# Context manager helper for per-call dry_run override
# ---------------------------------------------------------------------------


class _dry_run_override:
    """Temporarily override the writer's dry_run flag inside a with-block."""

    def __init__(self, writer: TOSWriter, dry_run: Optional[bool]) -> None:
        self._writer = writer
        self._override = dry_run
        self._saved: bool = writer.dry_run

    def __enter__(self) -> None:
        if self._override is not None:
            self._writer.dry_run = self._override

    def __exit__(self, *_: Any) -> None:
        self._writer.dry_run = self._saved
