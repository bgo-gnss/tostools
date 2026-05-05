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


def _load_from_pass(pass_path: str) -> Optional[str]:
    """Return the first line of a pass(1) entry, or None on any error.

    Calls ``pass show <pass_path>`` and returns the first non-empty line.
    The subprocess stdout is captured and never logged. Stderr is discarded
    to avoid leaking path information to logs.

    Returns None if pass is not installed, the entry does not exist, or the
    GPG key is unavailable (e.g. on a headless server without a GPG agent).
    """
    try:
        result = subprocess.run(
            ["pass", "show", pass_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        first_line = result.stdout.split("\n")[0].strip()
        return first_line or None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _load_credentials_from_cfg(
    cfg_path: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return (username, password) from database.cfg [tos] section, or (None, None).

    Supports two forms in the ``[tos]`` section:

    Plain text (convenient for non-sensitive username, avoid for password)::

        [tos]
        username = bgo
        password = secret

    Pass-store references (recommended — password never written to disk)::

        [tos]
        username = bgo
        password_pass_path = database/tos_password

    Both ``username_pass_path`` and ``password_pass_path`` may be set to
    retrieve both values from the pass store.  Plain-text ``username`` /
    ``password`` are used as fallbacks when the pass-path key is absent.
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
    ) -> Any:
        """Send an authenticated HTTP request.

        GET requests are always sent (reads are safe). Mutating requests
        (POST, PATCH, PUT, DELETE) are intercepted in dry-run mode.
        """
        self._ensure_authenticated()

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        is_mutating = method.upper() not in ("GET", "HEAD", "OPTIONS")

        if is_mutating and self.dry_run:
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

    @staticmethod
    def _tos_date(dt: Optional[str]) -> Optional[str]:
        """Normalise a date string to TOS format ``YYYY-MM-DDTHH:MM:SS``.

        TOS rejects timezone offsets (+00:00 / Z). Strip them here so callers
        can pass standard ISO-8601 strings without worrying about the format.
        """
        if dt is None:
            return None
        # Remove trailing timezone: +HH:MM, -HH:MM, or Z
        import re as _re
        return _re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", dt)

    def add_attribute_value(
        self,
        id_entity: int,
        code: str,
        value: str,
        date_from: str,
        date_to: Optional[str] = None,
    ) -> Any:
        """Add an attribute value to an existing entity.

        Does NOT check for existing values — use :meth:`upsert_attribute_value`
        for idempotent writes.
        """
        return self._request(
            "POST",
            "/attribute_values",
            data={
                "id_entity": id_entity,
                "code": code,
                "value": value,
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
    ) -> Any:
        """Idempotent write: PATCH the open attribute value if it exists, else POST.

        "Open" means the most recent value with ``date_to`` of ``None`` or
        the latest by ``date_from``.  Only the value field is updated on PATCH;
        the dates are left unchanged unless they differ from what we intend to set.

        Returns the PATCH or POST response, or :class:`DryRunResult`.
        """
        existing = self.get_attribute_values(id_entity, code)
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
        ``id_entity_child``.
        """
        if not kwargs:
            raise ValueError(
                "patch_entity_connection: at least one field must be provided"
            )
        return self._request("PATCH", f"/join/{id_connection}", data=kwargs)


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
