"""Unit tests for TOSWriter — no network required."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from tostools.api.tos_writer import (
    DryRunResult,
    TOSWriter,
    _find_database_cfg,
    _load_credentials_from_cfg,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_writer(**kwargs: object) -> TOSWriter:
    """Writer with credentials pre-set so tests never hit getpass/input."""
    defaults = dict(dry_run=True, username="testuser", password="testpass")
    defaults.update(kwargs)  # type: ignore[arg-type]
    return TOSWriter(**defaults)  # type: ignore[arg-type]


def _jwt_for(exp: float) -> str:
    """Build a minimal JWT with the given exp timestamp."""
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"header.{payload}.sig"


def _login_response(exp: float, scope: Optional[List[str]] = None) -> dict:
    scope = scope or ["ALLIR", "UT", "AOT", "TOS"]
    return {
        "token": _jwt_for(exp),
        "exp": exp,
        "user": "testuser",
        "email": "test@example.com",
        "scope": scope,
    }


# ---------------------------------------------------------------------------
# _find_database_cfg / _load_credentials_from_cfg
# ---------------------------------------------------------------------------


def test_find_database_cfg_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GPS_CONFIG_PATH", raising=False)
    # Point home to tmp so ~/.config/gpsconfig/database.cfg doesn't exist
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _find_database_cfg()
    assert result is None or result.exists()


def test_load_credentials_from_cfg(tmp_path):
    cfg = tmp_path / "database.cfg"
    cfg.write_text("[tos]\nusername = myuser\npassword = mypass\n")
    user, pwd = _load_credentials_from_cfg(cfg)
    assert user == "myuser"
    assert pwd == "mypass"


def test_load_credentials_from_cfg_missing_section(tmp_path):
    cfg = tmp_path / "database.cfg"
    cfg.write_text("[postgresql]\nhost = localhost\n")
    user, pwd = _load_credentials_from_cfg(cfg)
    assert user is None
    assert pwd is None


# ---------------------------------------------------------------------------
# TOSWriter — authentication
# ---------------------------------------------------------------------------


def test_token_invalid_when_not_logged_in():
    w = _make_writer()
    assert not w._token_valid()


def test_token_valid_after_login():
    w = _make_writer()
    w._token = "sometoken"
    w._token_exp = time.time() + 3600
    assert w._token_valid()


def test_token_invalid_when_near_expiry():
    w = _make_writer()
    w._token = "sometoken"
    w._token_exp = time.time() + 30  # within 60s buffer
    assert not w._token_valid()


def test_parse_exp_from_jwt():
    exp = time.time() + 7200
    token = _jwt_for(exp)
    parsed = TOSWriter._parse_exp_from_jwt(token)
    assert abs(parsed - exp) < 1.0


def test_parse_exp_from_jwt_malformed_returns_future():
    parsed = TOSWriter._parse_exp_from_jwt("not.a.jwt")
    assert parsed > time.time()


def test_login_stores_token_and_exp():
    w = _make_writer()
    exp = time.time() + 3600
    mock_resp = MagicMock()
    mock_resp.json.return_value = _login_response(exp)
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp) as mock_post:
        w.login()

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "Authorization" in call_kwargs.kwargs["headers"]
    assert call_kwargs.kwargs["headers"]["Authorization"].startswith("Basic ")

    assert w._token is not None
    assert abs(w._token_exp - exp) < 1.0
    assert w._token_valid()


def test_login_warns_on_missing_tos_scope():
    w = _make_writer()
    exp = time.time() + 3600
    mock_resp = MagicMock()
    mock_resp.json.return_value = _login_response(exp, scope=["ALLIR"])
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        with patch.object(w._logger, "warning") as mock_warn:
            w.login()
    mock_warn.assert_called_once()
    assert "scope" in mock_warn.call_args.args[0]


def test_login_raises_on_missing_token():
    w = _make_writer()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"user": "x"}
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(ValueError, match=r"missing 'sid'/'token'"):
            w.login()


def test_login_does_not_log_password(caplog):
    w = _make_writer()
    exp = time.time() + 3600
    mock_resp = MagicMock()
    mock_resp.json.return_value = _login_response(exp)
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp):
        with caplog.at_level(logging.DEBUG):
            w.login()

    assert "testpass" not in caplog.text


# ---------------------------------------------------------------------------
# TOSWriter — dry-run mode
# ---------------------------------------------------------------------------


def _logged_in_writer(**kwargs: object) -> TOSWriter:
    w = _make_writer(**kwargs)
    w._token = "tok"
    w._token_exp = time.time() + 3600
    return w


def test_dry_run_returns_dry_run_result_for_post():
    w = _logged_in_writer(dry_run=True)
    # Pre-populate the id_attribute cache so the writer doesn't try to
    # GET /admin_attribute_rows during the dry-run smoke test.
    w._id_attribute_cache = {"marker": 1, "name": 2}
    result = w.add_attribute_value(
        id_entity=1,
        code="marker",
        value="eldc",
        date_from="2022-01-01T00:00:00",
    )
    assert isinstance(result, DryRunResult)
    assert result.method == "POST"
    # Admin endpoint — the public /attribute_values returns 401 on
    # many real (entity, code) combinations. See add_attribute_value
    # docstring.
    assert result.endpoint == "/admin_attribute_value_rows"
    assert result.payload is not None
    assert result.payload["value_varchar"] == "eldc"
    assert result.payload["id_attribute"] == 1
    assert result.payload["id_entity"] == 1


def test_resolve_id_attribute_caches_admin_attribute_rows():
    """First call fetches /admin_attribute_rows; subsequent calls hit
    the cache. Keeps the per-action POST cost at one GET amortised
    across an apply run."""
    w = _logged_in_writer(dry_run=True)
    rows = [
        {"id": 1, "code": "marker"},
        {"id": 2, "code": "name"},
        {"id": 30, "code": "visit_class"},
    ]
    with patch.object(w, "_request", return_value=rows) as mock_req:
        assert w._resolve_id_attribute("marker") == 1
        assert w._resolve_id_attribute("visit_class") == 30
        assert w._resolve_id_attribute("name") == 2
    # Only one GET should have been issued — the cache absorbs the rest.
    assert mock_req.call_count == 1
    assert mock_req.call_args.args == ("GET", "/admin_attribute_rows")


def test_resolve_id_attribute_unknown_code_raises_value_error():
    """Surfacing typos at the boundary beats sending an unresolvable POST."""
    w = _logged_in_writer(dry_run=True)
    w._id_attribute_cache = {"marker": 1}
    with pytest.raises(ValueError, match="unknown attribute code"):
        w._resolve_id_attribute("not_a_real_code")


def test_resolve_id_attribute_skips_rows_missing_id_or_code():
    """Defensive: malformed rows from /admin_attribute_rows (missing
    ``id`` or ``code``) are silently filtered, not crashed on."""
    w = _logged_in_writer(dry_run=True)
    rows = [
        {"id": 1, "code": "marker"},
        {"id": None, "code": "ghost"},
        {"code": "no_id"},
        {"id": 2},  # no code
    ]
    with patch.object(w, "_request", return_value=rows):
        assert w._resolve_id_attribute("marker") == 1
    assert w._id_attribute_cache == {"marker": 1}


def test_add_attribute_value_resolves_id_then_posts_admin_endpoint():
    """End-to-end: add_attribute_value performs the lookup, then POSTs
    to the admin endpoint with id_attribute (int) and value_varchar."""
    w = _logged_in_writer(dry_run=False)
    rows = [{"id": 30, "code": "visit_class"}]

    with patch.object(w, "_request") as mock_req:
        # First call (GET /admin_attribute_rows) returns the rows;
        # second call (POST) returns the create response.
        mock_req.side_effect = [rows, {"id_attribute_value": 99001}]
        result = w.add_attribute_value(
            id_entity=4257,
            code="visit_class",
            value="B",
            date_from="2018-03-15",
        )

    assert result == {"id_attribute_value": 99001}
    assert mock_req.call_count == 2
    # Second call is the POST — assert URL + body.
    post_call = mock_req.call_args_list[1]
    assert post_call.args[0] == "POST"
    assert post_call.args[1] == "/admin_attribute_value_rows"
    body = post_call.kwargs["data"]
    assert body["id_entity"] == 4257
    assert body["id_attribute"] == 30
    assert body["value_varchar"] == "B"
    assert body["date_from"] == "2018-03-15T00:00:00"  # _tos_date promotes
    assert body["date_to"] is None


def test_dry_run_does_not_send_http():
    w = _logged_in_writer(dry_run=True)
    with patch("requests.request") as mock_req:
        w.create_entity(
            entity_subtype="geophysical",
            attributes=[{"code": "marker", "value": "eldc"}],
        )
    mock_req.assert_not_called()


def test_dry_run_get_still_sends_request():
    w = _logged_in_writer(dry_run=True)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"[]"
    mock_resp.json.return_value = []
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.request", return_value=mock_resp) as mock_req:
        result = w.get_attribute_templates()

    mock_req.assert_called_once()
    assert isinstance(result, list)


def test_create_entity_dry_run_per_call_override():
    """dry_run=False on a call overrides instance-level dry_run=True."""
    w = _logged_in_writer(dry_run=True)
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.content = b'{"id_entity": 99}'
    mock_resp.json.return_value = {"id_entity": 99}
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.request", return_value=mock_resp) as mock_req:
        result = w.create_entity(
            entity_subtype="geophysical",
            attributes=[],
            dry_run=False,
        )
    mock_req.assert_called_once()
    assert result == {"id_entity": 99}


# ---------------------------------------------------------------------------
# TOSWriter — patch_attribute_value validation
# ---------------------------------------------------------------------------


def test_patch_attribute_value_raises_with_no_fields():
    w = _logged_in_writer()
    with pytest.raises(ValueError, match="at least one field"):
        w.patch_attribute_value(42)


def test_patch_entity_connection_raises_with_no_kwargs():
    w = _logged_in_writer()
    with pytest.raises(ValueError, match="at least one field"):
        w.patch_entity_connection(7)


def test_update_entity_subtype_uses_admin_endpoint_with_put():
    """Reclassifying a device sends PUT /admin_entity_row/<id>/ with the
    integer id_entity_subtype (not the string code) — the public
    /entity/<id>/ endpoint is read-only (Allow: HEAD, GET, OPTIONS)."""
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 16321, "id_entity_subtype": 25}
        result = w.update_entity_subtype(16321, 25)

    mock_req.assert_called_once()
    call = mock_req.call_args
    assert call.args[0] == "PUT"
    assert "/admin_entity_row/16321" in call.args[1]
    assert call.kwargs["data"] == {"id_entity_subtype": 25}
    assert result["id_entity_subtype"] == 25


def test_update_entity_subtype_coerces_id_to_int():
    """A stringified int still produces a valid integer payload — guards
    against operator passing args[0] from the parser as raw text."""
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        w.update_entity_subtype(16321, "25")  # type: ignore[arg-type]
    assert mock_req.call_args.kwargs["data"] == {"id_entity_subtype": 25}


def test_update_entity_subtype_respects_dry_run():
    """In dry-run mode no HTTP request goes out; DryRunResult is returned."""
    from tostools.api.tos_writer import DryRunResult

    w = _logged_in_writer(dry_run=True)
    with patch("requests.request") as mock_http:
        result = w.update_entity_subtype(16321, 25)
    mock_http.assert_not_called()
    assert isinstance(result, DryRunResult)
    assert result.method == "PUT"
    assert "/admin_entity_row/16321" in result.endpoint


# ---------------------------------------------------------------------------
# TOSWriter — upsert_attribute_value logic
# ---------------------------------------------------------------------------


def test_upsert_patches_when_open_value_exists():
    w = _logged_in_writer(dry_run=False)
    # TOSWriter.upsert_attribute_value reads `id_attribute_value` (the field
    # name used by the TOS API), not `id`.
    existing = [
        {"id_attribute_value": 55, "code": "marker", "value": "old", "date_to": None}
    ]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 55, "value": "new"}
            w.upsert_attribute_value(1, "marker", "new", "2022-01-01T00:00:00")

    mock_req.assert_called_once()
    call = mock_req.call_args
    assert call.args[0] == "PATCH"
    assert "/attribute_value/55" in call.args[1]
    assert call.kwargs["data"]["value"] == "new"


def test_upsert_noop_when_value_already_matches():
    w = _logged_in_writer(dry_run=False)
    existing = [{"id": 55, "code": "marker", "value": "eldc", "date_to": None}]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            result = w.upsert_attribute_value(
                1, "marker", "eldc", "2022-01-01T00:00:00"
            )

    mock_req.assert_not_called()
    assert result["id"] == 55


def test_upsert_posts_when_no_open_value():
    w = _logged_in_writer(dry_run=False)
    # Pre-populate the id_attribute cache so the POST path doesn't
    # need to fetch /admin_attribute_rows during the test.
    w._id_attribute_cache = {"marker": 1}
    closed = [{"id": 10, "code": "marker", "value": "old", "date_to": "2021-12-31"}]

    with patch.object(w, "get_attribute_values", return_value=closed):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 11}
            w.upsert_attribute_value(1, "marker", "new", "2022-01-01T00:00:00")

    call = mock_req.call_args
    assert call.args[0] == "POST"
    # add_attribute_value routes through the admin endpoint.
    assert call.args[1] == "/admin_attribute_value_rows"


# ---------------------------------------------------------------------------
# TOSWriter — upsert_attribute_value with date_hint (Pattern 4)
# ---------------------------------------------------------------------------


def test_upsert_with_date_hint_patches_closed_period():
    """date_hint targets the closed period that covers the given date."""
    w = _logged_in_writer(dry_run=False)
    existing = [
        {
            "id_attribute_value": 10,
            "code": "firmware_version",
            "value": "5.4.0",
            "date_from": "2024-01-01T00:00:00",
            "date_to": "2025-06-01T00:00:00",
        },
        {
            "id_attribute_value": 20,
            "code": "firmware_version",
            "value": "5.5.0",
            "date_from": "2025-06-01T00:00:00",
            "date_to": None,
        },
    ]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 10, "value": "5.4.1"}
            w.upsert_attribute_value(
                1,
                "firmware_version",
                "5.4.1",
                "2024-01-01T00:00:00",
                date_hint="2025-01-15T00:00:00",
            )

    # PATCHed the closed period (id=10), not the open one (id=20).
    mock_req.assert_called_once()
    call = mock_req.call_args
    assert call.args[0] == "PATCH"
    assert "/attribute_value/10" in call.args[1]
    assert call.kwargs["data"]["value"] == "5.4.1"


def test_upsert_with_date_hint_targets_open_period():
    """date_hint that falls in the open period should PATCH the open period."""
    w = _logged_in_writer(dry_run=False)
    existing = [
        {
            "id_attribute_value": 10,
            "code": "status",
            "value": "virkt",
            "date_from": "2020-01-01T00:00:00",
            "date_to": None,
        },
    ]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 10, "value": "óvirkt"}
            w.upsert_attribute_value(
                1, "status", "óvirkt", "2025-01-01T00:00:00", date_hint="2025-01-01"
            )

    mock_req.assert_called_once()
    call = mock_req.call_args
    assert "/attribute_value/10" in call.args[1]


def test_upsert_with_date_hint_no_match_posts():
    """date_hint that falls in no period falls back to POST."""
    w = _logged_in_writer(dry_run=False)
    # Pre-populate the id_attribute cache; the POST path goes through
    # add_attribute_value → admin endpoint with id_attribute (int FK).
    w._id_attribute_cache = {"firmware_version": 7}
    existing = [
        {
            "id_attribute_value": 10,
            "code": "firmware_version",
            "value": "5.0.0",
            "date_from": "2020-01-01T00:00:00",
            "date_to": "2021-12-31T00:00:00",
        },
    ]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 11}
            w.upsert_attribute_value(
                1,
                "firmware_version",
                "5.1.0",
                "2022-01-01T00:00:00",
                date_hint="2023-06-01T00:00:00",
            )

    mock_req.assert_called_once()
    call = mock_req.call_args
    assert call.args[0] == "POST"


def test_upsert_with_date_hint_noop_when_value_matches():
    """date_hint that hits a period with the same value skips PATCH."""
    w = _logged_in_writer(dry_run=False)
    existing = [
        {
            "id_attribute_value": 10,
            "code": "model",
            "value": "SEPT POLARX5",
            "date_from": "2023-01-01T00:00:00",
            "date_to": "2024-12-31T00:00:00",
        },
    ]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            result = w.upsert_attribute_value(
                1,
                "model",
                "SEPT POLARX5",
                "2023-01-01T00:00:00",
                date_hint="2024-03-01T00:00:00",
            )

    mock_req.assert_not_called()
    assert result["id_attribute_value"] == 10


def test_upsert_with_date_hint_bare_date_promoted():
    """date_hint='2025-06-15' (bare date) covers the open period starting 2025-06-01."""
    w = _logged_in_writer(dry_run=False)
    existing = [
        {
            "id_attribute_value": 20,
            "code": "firmware_version",
            "value": "5.5.0",
            "date_from": "2025-06-01T00:00:00",
            "date_to": None,
        },
    ]

    with patch.object(w, "get_attribute_values", return_value=existing):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 20, "value": "5.6.0"}
            w.upsert_attribute_value(
                1,
                "firmware_version",
                "5.6.0",
                "2025-06-01T00:00:00",
                date_hint="2025-06-15",
            )

    mock_req.assert_called_once()


# ---------------------------------------------------------------------------
# TOSWriter — 401 re-login
# ---------------------------------------------------------------------------


def test_request_retries_on_401():
    w = _logged_in_writer(dry_run=False)

    resp_401 = MagicMock()
    resp_401.status_code = 401
    resp_401.raise_for_status = MagicMock(side_effect=Exception("401"))
    resp_401.content = b""

    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.content = b'{"ok": true}'
    resp_ok.json.return_value = {"ok": True}
    resp_ok.raise_for_status = MagicMock()

    login_resp = MagicMock()
    login_resp.json.return_value = _login_response(time.time() + 3600)
    login_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=login_resp):
        with patch("requests.request", side_effect=[resp_401, resp_ok]) as mock_req:
            resp_401.raise_for_status.side_effect = None
            result = w._request("GET", "/something")

    assert mock_req.call_count == 2
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# TOSWriter — find_device_by_serial / create_device duplicate-serial guard
# ---------------------------------------------------------------------------


def _basic_search_hit(
    serial: str,
    device_id: int,
    code: str = "serial_number",
    distance: int = 0,
) -> dict:
    return {
        "code": code,
        "value_varchar": serial,
        "distance": distance,
        "id_lvl_three": device_id,
    }


def test_find_device_by_serial_returns_match_when_subtype_matches():
    w = _logged_in_writer(dry_run=False)
    search_results = [_basic_search_hit("SN123", device_id=999)]
    entity = {
        "id_entity": 999,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [],
    }

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [search_results, entity]
        result = w.find_device_by_serial("gnss_receiver", "SN123")

    assert result is entity
    assert mock_req.call_count == 2
    # First call should be the search; second the entity GET
    assert mock_req.call_args_list[0].args[0] == "POST"
    assert "/basic_search" in mock_req.call_args_list[0].args[1]
    assert mock_req.call_args_list[1].args[0] == "GET"
    assert mock_req.call_args_list[1].args[1] == "/entity/999/"


def test_find_device_by_serial_returns_none_when_no_hits():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=[]):
        assert w.find_device_by_serial("gnss_receiver", "SN-MISSING") is None


def test_find_device_by_serial_skips_subtype_mismatch():
    w = _logged_in_writer(dry_run=False)
    search_results = [_basic_search_hit("SN123", device_id=999)]
    entity_wrong_subtype = {
        "id_entity": 999,
        "code_entity_subtype": "antenna",
        "attributes": [],
    }

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [search_results, entity_wrong_subtype]
        result = w.find_device_by_serial("gnss_receiver", "SN123")

    assert result is None


def test_find_device_by_serial_filters_distance_and_code():
    w = _logged_in_writer(dry_run=False)
    search_results = [
        # wrong code (matches a model field, not a serial)
        _basic_search_hit("SN123", device_id=111, code="model"),
        # right code but distance > 0 (fuzzy match)
        _basic_search_hit("SN123", device_id=222, distance=2),
        # exact value-mismatch even though code matches
        {
            "code": "serial_number",
            "value_varchar": "SN999",
            "distance": 0,
            "id_lvl_three": 333,
        },
        # the real exact match
        _basic_search_hit("SN123", device_id=444),
    ]
    entity = {"id_entity": 444, "code_entity_subtype": "gnss_receiver"}

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [search_results, entity]
        result = w.find_device_by_serial("gnss_receiver", "SN123")

    assert result is entity
    # Only 1 entity GET — confirms the other hits were filtered out
    get_calls = [c for c in mock_req.call_args_list if c.args[0] == "GET"]
    assert len(get_calls) == 1


def test_find_device_by_serial_empty_serial_short_circuits():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        assert w.find_device_by_serial("gnss_receiver", "") is None
    mock_req.assert_not_called()


def test_find_device_by_serial_search_runs_in_dry_run_mode():
    """The basic_search POST must bypass dry-run interception."""
    w = _logged_in_writer(dry_run=True)
    with patch("requests.request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[]"
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp
        result = w.find_device_by_serial("gnss_receiver", "SN123")

    assert result is None
    # Confirms an actual HTTP POST was sent despite dry_run=True
    assert mock_req.called
    assert mock_req.call_args.args[0] == "POST"


# ---------------------------------------------------------------------------
# find_location_by_name / connect_device_to_location
# ---------------------------------------------------------------------------


def _basic_search_location_hit(
    name: str,
    entity_id: int,
    type_lvl_two: str = "vöruhús",
    distance: int = 0,
) -> dict:
    """Mirror the basic_search hit shape for an entity-name match (location)."""
    return {
        "code": "name",
        "value_varchar": name,
        "distance": distance,
        "id_entity": entity_id,
        "id_lvl_two": entity_id,
        "id_lvl_three": None,
        "type_lvl_two": type_lvl_two,
        "subtype_lvl_two": "Lager",
    }


def test_find_location_by_name_returns_id_on_exact_match():
    w = _logged_in_writer(dry_run=False)
    hits = [_basic_search_location_hit("B9 - Kjallari - Jörð", entity_id=4)]
    with patch.object(w, "_request", return_value=hits):
        assert w.find_location_by_name("B9 - Kjallari - Jörð") == 4


def test_find_location_by_name_returns_none_on_no_match():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=[]):
        assert w.find_location_by_name("Nowhere") is None


def test_find_location_by_name_filters_distance_and_value():
    w = _logged_in_writer(dry_run=False)
    hits = [
        # fuzzy match — should be skipped
        _basic_search_location_hit("B9 - Kjallari", entity_id=999, distance=2),
        # wrong value_varchar with distance=0 — should be skipped
        _basic_search_location_hit("B7 - Kjallari", entity_id=998),
        # the right one
        _basic_search_location_hit("B9 - Kjallari - Jörð", entity_id=4),
    ]
    with patch.object(w, "_request", return_value=hits):
        assert w.find_location_by_name("B9 - Kjallari - Jörð") == 4


def test_find_location_by_name_filters_type():
    w = _logged_in_writer(dry_run=False)
    # value matches but type_lvl_two doesn't match the warehouse filter
    hits = [
        _basic_search_location_hit(
            "B9 - Kjallari - Jörð", entity_id=999, type_lvl_two="stöð"
        )
    ]
    with patch.object(w, "_request", return_value=hits):
        assert w.find_location_by_name("B9 - Kjallari - Jörð") is None
    # Disabling the type filter recovers the match.
    with patch.object(w, "_request", return_value=hits):
        assert (
            w.find_location_by_name("B9 - Kjallari - Jörð", type_filter="")
            == 999
        )


def test_find_location_by_name_empty_short_circuits():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        assert w.find_location_by_name("") is None
    mock_req.assert_not_called()


def test_connect_device_to_location_resolves_then_creates_join():
    w = _logged_in_writer(dry_run=False)
    hits = [_basic_search_location_hit("B9 - Kjallari - Jörð", entity_id=4)]
    join_response = {"id_connection": 12345}
    with patch.object(w, "_request") as mock_req:
        # 1st call: basic_search hits; 2nd call: create_entity_connection POST
        mock_req.side_effect = [hits, join_response]
        result = w.connect_device_to_location(
            id_device=21499,
            location_name="B9 - Kjallari - Jörð",
            date_start="2026-05-21T00:00:00",
        )

    assert result is join_response
    # Confirm the second call was POST /entity_connection/ with the right body
    post = mock_req.call_args_list[1]
    assert post.args[0] == "POST"
    body = post.kwargs.get("data") or post.args[2]
    assert body["id_entity_parent"] == 4
    assert body["id_entity_child"] == 21499
    assert body["time_from"] == "2026-05-21T00:00:00"
    assert body["time_to"] is None


def test_connect_device_to_location_raises_when_unresolved():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=[]):
        with pytest.raises(ValueError, match="not found in TOS"):
            w.connect_device_to_location(
                id_device=21499,
                location_name="Unknown - Place",
                date_start="2026-05-21T00:00:00",
            )


def test_create_device_rejects_duplicate_serial():
    w = _logged_in_writer(dry_run=True)
    existing = {
        "id_entity": 19140,
        "code_entity_subtype": "gnss_receiver",
        "attributes": [],
    }
    with patch.object(w, "find_device_by_serial", return_value=existing):
        with pytest.raises(ValueError, match=r"already exists.*id_entity=19140"):
            w.create_device(
                "gnss_receiver",
                [
                    {
                        "code": "serial_number",
                        "value": "SN123",
                        "date_from": "2026-05-10T00:00:00",
                    }
                ],
            )


def test_create_device_force_bypasses_duplicate_check():
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "find_device_by_serial") as mock_find:
        with patch.object(
            w, "create_entity", return_value={"id_entity": 99}
        ) as mock_create:
            result = w.create_device(
                "gnss_receiver",
                [
                    {
                        "code": "serial_number",
                        "value": "SN123",
                        "date_from": "2026-05-10T00:00:00",
                    }
                ],
                force=True,
            )

    mock_find.assert_not_called()
    mock_create.assert_called_once()
    assert result == {"id_entity": 99}


def test_create_device_creates_when_no_duplicate():
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "find_device_by_serial", return_value=None):
        with patch.object(
            w, "create_entity", return_value=DryRunResult("POST", "/entities", {})
        ) as mock_create:
            result = w.create_device(
                "antenna",
                [
                    {
                        "code": "serial_number",
                        "value": "ANT-1",
                        "date_from": "2026-05-10T00:00:00",
                    },
                    {
                        "code": "model",
                        "value": "TRM57971.00",
                        "date_from": "2026-05-10T00:00:00",
                    },
                ],
            )

    mock_create.assert_called_once()
    call = mock_create.call_args
    assert call.kwargs["entity_subtype"] == "antenna"
    assert isinstance(result, DryRunResult)


def test_create_device_raises_without_serial():
    w = _logged_in_writer(dry_run=True)
    with pytest.raises(ValueError, match="non-empty 'serial_number'"):
        w.create_device(
            "gnss_receiver",
            [
                {
                    "code": "model",
                    "value": "SEPT POLARX5",
                    "date_from": "2026-05-10T00:00:00",
                }
            ],
        )


def test_create_device_raises_with_empty_serial():
    w = _logged_in_writer(dry_run=True)
    with pytest.raises(ValueError, match="non-empty 'serial_number'"):
        w.create_device(
            "gnss_receiver",
            [
                {
                    "code": "serial_number",
                    "value": "",
                    "date_from": "2026-05-10T00:00:00",
                }
            ],
        )
