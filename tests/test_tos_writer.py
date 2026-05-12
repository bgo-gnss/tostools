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
    result = w.add_attribute_value(
        id_entity=1,
        code="marker",
        value="eldc",
        date_from="2022-01-01T00:00:00",
    )
    assert isinstance(result, DryRunResult)
    assert result.method == "POST"
    assert result.endpoint == "/attribute_values"
    assert result.payload is not None
    assert result.payload["value"] == "eldc"


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


# ---------------------------------------------------------------------------
# TOSWriter — delete_entity_connection
# ---------------------------------------------------------------------------


def test_delete_entity_connection_dry_run_returns_dry_run_result():
    """The shared dry-run interception in TOSWriter._request must cover
    DELETE — that's how `cfg move` rollback stays safe in dry-run mode."""
    w = _logged_in_writer(dry_run=True)
    result = w.delete_entity_connection(id_connection=42)
    assert isinstance(result, DryRunResult)
    assert result.method == "DELETE"
    assert result.endpoint == "/joins/42/"


def test_delete_entity_connection_dry_run_does_not_send_http():
    w = _logged_in_writer(dry_run=True)
    with patch("requests.request") as mock_req:
        w.delete_entity_connection(id_connection=42)
    mock_req.assert_not_called()


def test_delete_entity_connection_live_sends_delete_request():
    w = _logged_in_writer(dry_run=False)
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.content = b""
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.request", return_value=mock_resp) as mock_req:
        w.delete_entity_connection(id_connection=42)

    mock_req.assert_called_once()
    call = mock_req.call_args
    # Verifies the TOS path quirk: DELETE on /joins/<id>/ (plural), not
    # /join/<id>/ which is reserved for PATCH.
    assert call.args[0] == "DELETE"
    assert call.args[1].endswith("/joins/42/")


def test_delete_entity_connection_per_call_dry_run_override():
    """Per-call dry_run=False overrides the instance-level dry_run=True."""
    w = _logged_in_writer(dry_run=True)
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.content = b""
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.request", return_value=mock_resp) as mock_req:
        w.delete_entity_connection(id_connection=42, dry_run=False)

    mock_req.assert_called_once()
    # Instance dry_run is restored after the call.
    assert w.dry_run is True


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
    closed = [{"id": 10, "code": "marker", "value": "old", "date_to": "2021-12-31"}]

    with patch.object(w, "get_attribute_values", return_value=closed):
        with patch.object(w, "_request") as mock_req:
            mock_req.return_value = {"id": 11}
            w.upsert_attribute_value(1, "marker", "new", "2022-01-01T00:00:00")

    call = mock_req.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "/attribute_values"


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
