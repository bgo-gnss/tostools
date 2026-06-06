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
    # GET /admin_attribute_rows during the dry-run smoke test. Cache is
    # keyed by (code, id_entity_type) — None scope works as fallback.
    w._id_attribute_cache = {("marker", None): 1, ("name", None): 2}
    # Pre-populate the entity-type cache so the writer doesn't issue a
    # GET /admin_entity_rows/<id> probe during the dry-run smoke test.
    w._entity_type_cache = {1: None}
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
        # No scope passed — single-variant codes resolve via Rule 2/3 fallback.
        assert w._resolve_id_attribute("marker") == 1
        assert w._resolve_id_attribute("visit_class") == 30
        assert w._resolve_id_attribute("name") == 2
    # Only one GET should have been issued — the cache absorbs the rest.
    assert mock_req.call_count == 1
    assert mock_req.call_args.args == ("GET", "/admin_attribute_rows")


def test_resolve_id_attribute_unknown_code_raises_value_error():
    """Surfacing typos at the boundary beats sending an unresolvable POST."""
    w = _logged_in_writer(dry_run=True)
    w._id_attribute_cache = {("marker", None): 1}
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
    assert w._id_attribute_cache == {("marker", None): 1}


def test_resolve_id_attribute_filters_by_entity_type_when_multiple_variants():
    """Critical bugfix coverage: TOS schema has multiple id_attribute
    rows per code (one per entity_type). E.g. ``model`` has id=27 for
    devices (entity_type=4) and id=59 for monuments (entity_type=3).
    Passing the target entity's id_entity_type picks the scope-matching
    row.

    Reason this matters: SAVI monument 5245 had its ``subtype`` value
    written to id=114 (station scope) instead of id=65 (monument
    scope) on 2026-05-25 because the pre-fix resolver overwrote the
    cache entry for ``subtype`` with whatever row appeared last in the
    /admin_attribute_rows response."""
    w = _logged_in_writer(dry_run=True)
    rows = [
        {"id": 27, "code": "model", "id_entity_type": 4},  # device
        {"id": 59, "code": "model", "id_entity_type": 3},  # monument
        {"id": 65, "code": "subtype", "id_entity_type": 3},  # monument
        {"id": 114, "code": "subtype", "id_entity_type": 2},  # station
    ]
    with patch.object(w, "_request", return_value=rows):
        # Monument scope (entity_type=3) picks the monument-scoped row.
        assert w._resolve_id_attribute("model", id_entity_type=3) == 59
        assert w._resolve_id_attribute("subtype", id_entity_type=3) == 65
        # Device / station scopes pick their own rows.
        assert w._resolve_id_attribute("model", id_entity_type=4) == 27
        assert w._resolve_id_attribute("subtype", id_entity_type=2) == 114


def test_resolve_id_attribute_ambiguous_code_without_scope_raises():
    """When a code has multiple entity_type variants and no scope is
    provided, refuse to guess — the original bug picked arbitrarily."""
    w = _logged_in_writer(dry_run=True)
    rows = [
        {"id": 65, "code": "subtype", "id_entity_type": 3},
        {"id": 114, "code": "subtype", "id_entity_type": 2},
    ]
    with patch.object(w, "_request", return_value=rows):
        with pytest.raises(ValueError, match="ambiguous attribute code"):
            w._resolve_id_attribute("subtype")


def test_resolve_id_attribute_single_variant_works_without_scope():
    """Single-variant codes (e.g. ``monument_height`` only exists for
    monuments) should keep working when callers don't bother to plumb
    entity_type — Rule 3 fallback."""
    w = _logged_in_writer(dry_run=True)
    rows = [{"id": 175, "code": "monument_height", "id_entity_type": 3}]
    with patch.object(w, "_request", return_value=rows):
        # Without scope: only one variant, take it.
        assert w._resolve_id_attribute("monument_height") == 175
        # With matching scope: same answer.
        assert w._resolve_id_attribute("monument_height", id_entity_type=3) == 175
        # With non-matching scope: falls through to single-variant rule.
        assert w._resolve_id_attribute("monument_height", id_entity_type=999) == 175


def test_resolve_id_attribute_prefers_none_scope_row_when_present():
    """If a row exists with id_entity_type=None (cross-scope catalog
    entry, rare) and no exact-scope match, prefer the None-scope row
    over the single-variant fallback."""
    w = _logged_in_writer(dry_run=True)
    rows = [
        {"id": 10, "code": "shared", "id_entity_type": None},
        {"id": 11, "code": "shared", "id_entity_type": 4},
    ]
    with patch.object(w, "_request", return_value=rows):
        # Exact scope match still wins.
        assert w._resolve_id_attribute("shared", id_entity_type=4) == 11
        # No match → None-scope row wins.
        assert w._resolve_id_attribute("shared", id_entity_type=99) == 10
        # No scope passed → None-scope row wins.
        assert w._resolve_id_attribute("shared") == 10


def test_get_entity_type_caches_per_id():
    """Two-step lookup (history endpoint for subtype code, then
    entity_subtypes for the type FK), both cached per writer instance.
    Subsequent calls for the same id hit the cache — keeps the per-
    action POST cost at one round-trip total when many attributes
    target the same entity.

    Why two-step: TOS doesn't surface ``id_entity_type`` directly on
    entity rows — it surfaces ``code_entity_subtype`` (e.g.
    'monument'), and the subtype → entity_type mapping lives in
    /entity_subtypes/."""
    w = _logged_in_writer(dry_run=True)
    history_response = {"code_entity_subtype": "monument"}
    subtypes_response = [
        {"code": "monument", "id_entity_type": 3},
        {"code": "gnss_receiver", "id_entity_type": 4},
    ]
    with patch.object(
        w, "_request", side_effect=[history_response, subtypes_response]
    ) as mock_req:
        assert w._get_entity_type(5245) == 3
        # Subsequent calls don't re-fetch — per-entity cache hits.
        assert w._get_entity_type(5245) == 3
        assert w._get_entity_type(5245) == 3
    # Only two GETs total — one per endpoint.
    assert mock_req.call_count == 2
    urls = [c.args[1] for c in mock_req.call_args_list]
    assert urls == ["/history/entity/5245/", "/entity_subtypes/"]


def test_get_entity_type_caches_subtype_map_across_entities():
    """The /entity_subtypes/ lookup is fetched once per writer and
    reused for all subsequent entity lookups, even for different
    entities."""
    w = _logged_in_writer(dry_run=True)
    monument_history = {"code_entity_subtype": "monument"}
    receiver_history = {"code_entity_subtype": "gnss_receiver"}
    subtypes_response = [
        {"code": "monument", "id_entity_type": 3},
        {"code": "gnss_receiver", "id_entity_type": 4},
    ]
    with patch.object(
        w,
        "_request",
        side_effect=[monument_history, subtypes_response, receiver_history],
    ) as mock_req:
        assert w._get_entity_type(5245) == 3  # monument
        assert w._get_entity_type(21197) == 4  # gnss_receiver
    # 3 GETs: history(5245), entity_subtypes, history(21197). No
    # second /entity_subtypes/ fetch despite covering two entities.
    assert mock_req.call_count == 3
    urls = [c.args[1] for c in mock_req.call_args_list]
    assert urls == [
        "/history/entity/5245/",
        "/entity_subtypes/",
        "/history/entity/21197/",
    ]


def test_get_entity_type_returns_none_on_lookup_failure():
    """If the history endpoint 404s or any step is malformed, fall back
    to None — :meth:`_resolve_id_attribute` then uses scope=None rules
    (cross-scope row preferred, single-variant fallback) and raises
    only for genuine multi-scope ambiguity."""
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "_request", side_effect=RuntimeError("404")):
        assert w._get_entity_type(99999) is None
    # Cached as None so we don't retry on every call.
    assert 99999 in w._entity_type_cache
    assert w._entity_type_cache[99999] is None


def test_get_earliest_known_picks_earliest_open_attribute_date():
    """Earliest non-cleanup-artifact open attribute date_from wins.
    Skips 2014-10-17 bulk-load artifacts even when they're the
    chronologically earliest entry."""
    w = _logged_in_writer(dry_run=True)
    history = {
        "attributes": [
            {"date_from": "2014-10-17T00:00:00", "date_to": None},  # artifact, skip
            {
                "date_from": "2007-09-07T00:00:00",
                "date_to": None,
            },  # earliest non-artifact
            {"date_from": "2010-01-15T00:00:00", "date_to": None},
            {
                "date_from": "2005-01-01T00:00:00",
                "date_to": "2007-09-07T00:00:00",
            },  # closed — skip
        ]
    }
    with patch.object(w, "_request", return_value=history):
        assert w._get_earliest_known(5245) == "2007-09-07"


def test_get_earliest_known_falls_back_to_open_join_time_from():
    """When the entity has no non-artifact open attributes, fall back
    to the open parent-join's time_from. Required for freshly-created
    entities that haven't had attributes filled yet."""
    w = _logged_in_writer(dry_run=True)
    # First _request: entity history (only artifact attribute).
    # Second _request: parent_history (one open join from 2016).
    history = {
        "attributes": [
            {"date_from": "2014-10-17T00:00:00", "date_to": None},  # artifact — skipped
        ]
    }
    joins = [
        {"time_from": "2016-07-02T00:00:00", "time_to": None},
    ]
    with patch.object(w, "_request", side_effect=[history, joins]):
        assert w._get_earliest_known(21511) == "2016-07-02"


def test_get_earliest_known_returns_none_when_nothing_resolves():
    """No non-artifact attributes AND no open join → None. The token
    resolver then surfaces this as a failed ActionResult."""
    w = _logged_in_writer(dry_run=True)
    history = {"attributes": []}
    joins: list = []
    with patch.object(w, "_request", side_effect=[history, joins]):
        assert w._get_earliest_known(99999) is None


def test_get_earliest_known_is_cached_per_id():
    """Subsequent calls for the same id_entity served from cache —
    one history GET per entity, even with many `start` references in
    a single apply run."""
    w = _logged_in_writer(dry_run=True)
    history = {"attributes": [{"date_from": "2007-09-07T00:00:00", "date_to": None}]}
    with patch.object(w, "_request", return_value=history) as mock_req:
        assert w._get_earliest_known(5245) == "2007-09-07"
        assert w._get_earliest_known(5245) == "2007-09-07"
        assert w._get_earliest_known(5245) == "2007-09-07"
    assert mock_req.call_count == 1


def test_get_earliest_known_caches_negative_result():
    """Negative result (None) is also cached — don't keep retrying
    failed lookups across many ACTIONs."""
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "_request", side_effect=[{"attributes": []}, []]) as mock_req:
        assert w._get_earliest_known(99999) is None
        # Second call: cache hit, no network.
        assert w._get_earliest_known(99999) is None
    # First call: 2 GETs (history + parent_history fallback). Second
    # call: 0 GETs.
    assert mock_req.call_count == 2


def test_get_entity_type_returns_none_for_unknown_subtype():
    """Defensive: if /entity_subtypes/ doesn't list the entity's
    subtype code (e.g. a freshly-added subtype not yet in the static
    table), fall back to None rather than crashing."""
    w = _logged_in_writer(dry_run=True)
    with patch.object(
        w,
        "_request",
        side_effect=[
            {"code_entity_subtype": "brand_new_subtype"},
            [{"code": "monument", "id_entity_type": 3}],
        ],
    ):
        assert w._get_entity_type(77777) is None


def test_add_attribute_value_resolves_id_then_posts_admin_endpoint():
    """End-to-end: add_attribute_value performs the lookup chain, then
    POSTs to the admin endpoint with id_attribute (int) and
    value_varchar.

    Four GETs amortised across the apply run (then served from cache):
    history-of-entity (subtype code) + entity_subtypes (type FK) +
    admin_attribute_rows (schema catalog) + the POST itself."""
    w = _logged_in_writer(dry_run=False)
    rows = [{"id": 30, "code": "visit_class", "id_entity_type": 2}]
    history = {"code_entity_subtype": "geophysical"}
    subtypes = [{"code": "geophysical", "id_entity_type": 2}]

    with patch.object(w, "_request") as mock_req:
        # Calls in order: history (for subtype code), entity_subtypes
        # (subtype→type FK), admin_attribute_rows (id_attribute lookup),
        # POST attribute_value (the actual write).
        mock_req.side_effect = [
            history,
            subtypes,
            rows,
            {"id_attribute_value": 99001},
        ]
        result = w.add_attribute_value(
            id_entity=4257,
            code="visit_class",
            value="B",
            date_from="2018-03-15",
        )

    assert result == {"id_attribute_value": 99001}
    assert mock_req.call_count == 4
    # Fourth call is the POST — assert URL + body.
    post_call = mock_req.call_args_list[3]
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


def test_create_entity_connection_normalizes_bare_date_to_datetime():
    """Regression for the SAVI live-apply failure (2026-05-25): TOS rejects
    bare YYYY-MM-DD on the /joins endpoint with HTTP 400. The writer must
    promote bare dates to full datetimes via _tos_date, same as
    patch_entity_connection already does. Without this, _dispatch_move's
    `open new join` step fails after `close old join` succeeds — leaving
    the device parent-less and needing manual recovery."""
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 99999}
        w.create_entity_connection(
            id_parent=4440, id_child=21510, time_from="2007-09-07"
        )
    payload = mock_req.call_args.kwargs["data"]
    # Must be the full-datetime form TOS accepts; the bare date is rejected.
    assert payload["time_from"] == "2007-09-07T00:00:00"
    assert payload["time_to"] is None


def test_create_entity_connection_normalizes_both_dates_when_closed_join():
    """When backfilling a closed historical join (fill-gap), both
    time_from and time_to are bare dates that need promotion."""
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 99999}
        w.create_entity_connection(
            id_parent=4440,
            id_child=21510,
            time_from="2007-09-07",
            time_to="2016-07-02",
        )
    payload = mock_req.call_args.kwargs["data"]
    assert payload["time_from"] == "2007-09-07T00:00:00"
    assert payload["time_to"] == "2016-07-02T00:00:00"


def test_create_entity_connection_passes_full_datetime_through():
    """If the caller already supplies a full datetime, don't double-normalize."""
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 99999}
        w.create_entity_connection(
            id_parent=4440,
            id_child=21510,
            time_from="2007-09-07T12:30:00",
        )
    payload = mock_req.call_args.kwargs["data"]
    assert payload["time_from"] == "2007-09-07T12:30:00"


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
    # Pre-populate the id_attribute + entity_type caches so the POST
    # path doesn't fetch /admin_attribute_rows or /admin_entity_rows
    # during the test. Cache is keyed by (code, id_entity_type).
    w._id_attribute_cache = {("marker", None): 1}
    w._entity_type_cache = {1: None}
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
    # Pre-populate the id_attribute + entity_type caches; the POST path
    # goes through add_attribute_value → admin endpoint with
    # id_attribute (int FK). Cache is keyed by (code, id_entity_type).
    w._id_attribute_cache = {("firmware_version", None): 7}
    w._entity_type_cache = {1: None}
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
        assert w.find_location_by_name("B9 - Kjallari - Jörð", type_filter="") == 999


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


# ---------------------------------------------------------------------------
# find_station_by_marker
# ---------------------------------------------------------------------------


def _basic_search_marker_hit(
    marker: str,
    entity_id: int,
    type_lvl_two: str = "stöð",
    distance: int = 0,
) -> dict:
    """Mirror the basic_search hit shape for a marker-attribute match."""
    return {
        "code": "marker",
        "value_varchar": marker,
        "distance": distance,
        "id_entity": entity_id,
        "id_lvl_two": entity_id,
        "id_lvl_three": None,
        "type_lvl_two": type_lvl_two,
        "subtype_lvl_two": "geophysical",
    }


def test_find_station_by_marker_returns_id_on_exact_match():
    w = _logged_in_writer(dry_run=False)
    hits = [_basic_search_marker_hit("hrac", entity_id=16096)]
    with patch.object(w, "_request", return_value=hits):
        assert w.find_station_by_marker("HRAC") == 16096


def test_find_station_by_marker_is_case_insensitive():
    """TOS stores markers lowercase; the helper accepts either case."""
    w = _logged_in_writer(dry_run=False)
    hits = [_basic_search_marker_hit("hrac", entity_id=16096)]
    with patch.object(w, "_request", return_value=hits):
        assert w.find_station_by_marker("hrac") == 16096
    with patch.object(w, "_request", return_value=hits):
        assert w.find_station_by_marker("Hrac") == 16096


def test_find_station_by_marker_returns_none_on_no_match():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=[]):
        assert w.find_station_by_marker("XXXX") is None


def test_find_station_by_marker_filters_distance_value_and_type():
    w = _logged_in_writer(dry_run=False)
    hits = [
        _basic_search_marker_hit("hraf", entity_id=999, distance=1),
        _basic_search_marker_hit("save", entity_id=998),
        _basic_search_marker_hit("hrac", entity_id=997, type_lvl_two="vöruhús"),
        _basic_search_marker_hit("hrac", entity_id=16096),
    ]
    with patch.object(w, "_request", return_value=hits):
        assert w.find_station_by_marker("HRAC") == 16096


def test_find_station_by_marker_empty_short_circuits():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        assert w.find_station_by_marker("") is None
    mock_req.assert_not_called()


# ---------------------------------------------------------------------------
# get_open_parent_join / move_device — Pattern 2 for joins
# ---------------------------------------------------------------------------


def test_get_open_parent_join_returns_open_one():
    w = _logged_in_writer(dry_run=False)
    history = [
        {
            "id": 100,
            "id_entity_child": 21501,
            "id_entity_parent": 1,
            "time_from": "2020-01-01T00:00:00",
            "time_to": "2026-05-21T00:00:00",
        },
        {
            "id": 200,
            "id_entity_child": 21501,
            "id_entity_parent": 4,
            "time_from": "2026-05-21T00:00:00",
            "time_to": None,
        },
    ]
    with patch.object(w, "_request", return_value=history):
        join = w.get_open_parent_join(21501)
    assert join is not None
    assert join["id"] == 200
    assert join["id_entity_parent"] == 4


def test_get_open_parent_join_returns_none_when_no_open():
    w = _logged_in_writer(dry_run=False)
    history = [
        {
            "id": 100,
            "id_entity_parent": 1,
            "time_from": "2020-01-01T00:00:00",
            "time_to": "2026-05-21T00:00:00",
        },
    ]
    with patch.object(w, "_request", return_value=history):
        assert w.get_open_parent_join(21501) is None


def test_get_open_parent_join_handles_multiple_open():
    """Defensive: if TOS has >1 open join (invariant violation), pick newest."""
    w = _logged_in_writer(dry_run=False)
    history = [
        {
            "id": 100,
            "id_entity_parent": 1,
            "time_from": "2020-01-01T00:00:00",
            "time_to": None,
        },
        {
            "id": 200,
            "id_entity_parent": 4,
            "time_from": "2026-05-21T00:00:00",
            "time_to": None,
        },
    ]
    with patch.object(w, "_request", return_value=history):
        join = w.get_open_parent_join(21501)
    assert join is not None
    assert join["id"] == 200


def test_move_device_closes_old_and_opens_new():
    w = _logged_in_writer(dry_run=False)
    open_join = {
        "id": 28698,
        "id_entity_child": 21501,
        "id_entity_parent": 4,
        "time_from": "2026-05-21T00:00:00",
        "time_to": None,
    }
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [
            [open_join],
            {"id": 28698, "time_to": "2026-05-22T00:00:00"},
            {"id_connection": 28699},
        ]
        result = w.move_device(21501, 16096, "2026-05-22")
    assert result["from_id_entity"] == 4
    assert result["to_id_entity"] == 16096
    methods = [c.args[0] for c in mock_req.call_args_list]
    assert methods == ["GET", "PATCH", "POST"]
    patch_call = mock_req.call_args_list[1]
    assert patch_call.args[1] == "/join/28698"
    assert patch_call.kwargs["data"]["time_to"] == "2026-05-22T00:00:00"
    post_call = mock_req.call_args_list[2]
    assert post_call.args[1] == "/joins"
    assert post_call.kwargs["data"]["id_entity_parent"] == 16096
    assert post_call.kwargs["data"]["id_entity_child"] == 21501
    assert post_call.kwargs["data"]["time_from"] == "2026-05-22T00:00:00"
    assert post_call.kwargs["data"]["time_to"] is None


def test_move_device_when_no_open_join_still_opens_new():
    """Floating device (no open parent) — move_device just opens new join."""
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [[], {"id_connection": 99}]
        result = w.move_device(21501, 16096, "2026-05-22")
    assert result["closed"] is None
    assert result["from_id_entity"] is None
    methods = [c.args[0] for c in mock_req.call_args_list]
    assert methods == ["GET", "POST"]


def test_move_device_raises_when_from_mismatch():
    w = _logged_in_writer(dry_run=False)
    open_join = {
        "id": 100,
        "id_entity_parent": 4,
        "time_from": "2026-05-21T00:00:00",
        "time_to": None,
    }
    with patch.object(w, "_request", return_value=[open_join]):
        with pytest.raises(ValueError, match="currently under parent 4"):
            w.move_device(21501, 16096, "2026-05-22", from_id_entity=999)


def test_move_device_promotes_bare_date():
    """transition_date as YYYY-MM-DD must promote to full datetime."""
    w = _logged_in_writer(dry_run=False)
    open_join = {
        "id": 100,
        "id_entity_parent": 4,
        "time_from": "2026-05-21T00:00:00",
        "time_to": None,
    }
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [[open_join], {}, {"id_connection": 99}]
        w.move_device(21501, 16096, "2026-05-22")
    assert mock_req.call_args_list[1].kwargs["data"]["time_to"] == (
        "2026-05-22T00:00:00"
    )
    assert mock_req.call_args_list[2].kwargs["data"]["time_from"] == (
        "2026-05-22T00:00:00"
    )


def test_move_device_dry_run_returns_dry_run_results():
    w = _logged_in_writer(dry_run=True)
    open_join = {
        "id": 100,
        "id_entity_parent": 4,
        "time_from": "2026-05-21T00:00:00",
        "time_to": None,
    }

    def side_effect(method, path, *_a, **kw):
        if method == "GET":
            return [open_join]
        return DryRunResult(method=method, endpoint=path, payload=kw.get("data"))

    with patch.object(w, "_request", side_effect=side_effect):
        result = w.move_device(21501, 16096, "2026-05-22")
    assert isinstance(result["closed"], DryRunResult)
    assert isinstance(result["opened"], DryRunResult)


# ---------------------------------------------------------------------------
# Maintenance / vitjun
# ---------------------------------------------------------------------------


def _maintenance_detail_with_seeded_attrs(
    id_maintenance: int = 9999,
    base: int = 80000,
) -> dict:
    """Realistic detail-GET shape, with auto-seeded attribute_value rows."""
    return {
        "id_maintenance": id_maintenance,
        "maintenance_type": "on_site",
        "start_time": "2026-05-22T00:00:00",
        "end_time": "2026-05-22T00:00:00",
        "participants": "",
        "completed": False,
        "maintenance_attribute_values": [
            {
                "code": "reason_change",
                "id_maintenance_attribute_value": base + 1,
                "value": "false",
            },
            {
                "code": "reason_repairs",
                "id_maintenance_attribute_value": base + 2,
                "value": "false",
            },
            {
                "code": "reason_inspection",
                "id_maintenance_attribute_value": base + 3,
                "value": "false",
            },
            {
                "code": "reason_improvements",
                "id_maintenance_attribute_value": base + 4,
                "value": "false",
            },
            {
                "code": "reason_other",
                "id_maintenance_attribute_value": base + 5,
                "value": "false",
            },
            {
                "code": "work",
                "id_maintenance_attribute_value": base + 6,
                "value": "",
            },
            {
                "code": "comment",
                "id_maintenance_attribute_value": base + 7,
                "value": "",
            },
            {
                "code": "remaining",
                "id_maintenance_attribute_value": base + 8,
                "value": "",
            },
        ],
        "employees": [],
    }


def test_list_maintenance_visits_returns_list():
    w = _logged_in_writer(dry_run=False)
    sample = [{"id": 5146, "maintenance_type": "on_site", "reason": "Breyting"}]
    with patch.object(w, "_request", return_value=sample) as mock_req:
        assert w.list_maintenance_visits(16096) == sample
    mock_req.assert_called_once_with("GET", "/maintenances/id_entity/16096")


def test_list_maintenance_visits_empty_when_not_list():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value={"error": "404"}):
        assert w.list_maintenance_visits(99999) == []


def test_get_maintenance_visit_returns_dict():
    w = _logged_in_writer(dry_run=False)
    sample = {"id_maintenance": 5146, "maintenance_attribute_values": []}
    with patch.object(w, "_request", return_value=sample) as mock_req:
        assert w.get_maintenance_visit(5146) == sample
    mock_req.assert_called_once_with("GET", "/maintenance/id_maintenance/5146")


def test_add_maintenance_visit_validates_unknown_reason():
    w = _logged_in_writer(dry_run=False)
    with pytest.raises(ValueError, match="unknown reason codes"):
        w.add_maintenance_visit(
            16096, start_time="2026-05-22", reasons=["nope", "change"]
        )


def test_add_maintenance_visit_validates_maintenance_type():
    w = _logged_in_writer(dry_run=False)
    with pytest.raises(ValueError, match="maintenance_type must be"):
        w.add_maintenance_visit(
            16096, start_time="2026-05-22", maintenance_type="onsite"
        )


def test_add_maintenance_visit_three_call_flow():
    """POST + GET + PUT with discovered attribute IDs."""
    w = _logged_in_writer(dry_run=False)
    created = {
        "id": 9999,
        "maintenance_type": "on_site",
        "start_time": "2026-05-22T00:00:00",
        "end_time": "2026-05-22T00:00:00",
    }
    detail = _maintenance_detail_with_seeded_attrs(9999, base=80000)
    put_resp = {"ok": True}
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [created, detail, put_resp]
        result = w.add_maintenance_visit(
            16096,
            start_time="2026-05-22",
            maintenance_type="on_site",
            participants="bgo@vedur.is",
            reasons=["change"],
            work="Skipt um móttakara",
            remaining="Athuga loftnet næst",
        )
    assert result["id_maintenance"] == 9999
    methods = [c.args[0] for c in mock_req.call_args_list]
    assert methods == ["POST", "GET", "PUT"]

    post_call = mock_req.call_args_list[0]
    assert post_call.args[1] == "/maintenances/id_entity/16096"
    assert post_call.kwargs["data"] == {
        "maintenance_type": "on_site",
        "start_time": "2026-05-22T00:00:00",
        "end_time": "2026-05-22T00:00:00",
    }

    put_call = mock_req.call_args_list[2]
    assert put_call.args[1] == "/maintenance/id_maintenance/9999"
    put_body = put_call.kwargs["data"]
    assert put_body["participants"] == "bgo@vedur.is"
    assert put_body["completed"] is True
    assert put_body["start_time"] == "2026-05-22T00:00:00"
    av_by_id = {
        row["id_maintenance_attribute_value"]: row["value"]
        for row in put_body["maintenance_attribute_values"]
    }
    assert av_by_id[80001] == "true"  # reason_change
    assert av_by_id[80002] == "false"  # reason_repairs
    assert av_by_id[80003] == "false"  # reason_inspection
    assert av_by_id[80004] == "false"  # reason_improvements
    assert av_by_id[80005] == "false"  # reason_other
    assert av_by_id[80006] == "Skipt um móttakara"  # work
    assert av_by_id[80008] == "Athuga loftnet næst"  # remaining
    # comment was not supplied → should NOT appear in PUT payload
    assert 80007 not in av_by_id


def test_add_maintenance_visit_dry_run_short_circuits():
    """In dry-run we cannot discover IDs, so GET+PUT must NOT fire."""
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = DryRunResult(
            method="POST",
            endpoint="/maintenances/id_entity/16096",
            payload={},
        )
        result = w.add_maintenance_visit(
            16096,
            start_time="2026-05-22",
            reasons=["change"],
            work="dry-run test",
        )
    assert result["id_maintenance"] == "<dry-run>"
    assert result["updated"] is None
    assert mock_req.call_count == 1
    assert mock_req.call_args.args[0] == "POST"


def test_add_maintenance_visit_supports_multiple_reasons():
    w = _logged_in_writer(dry_run=False)
    created = {"id": 9999}
    detail = _maintenance_detail_with_seeded_attrs(9999, base=80000)
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [created, detail, {"ok": True}]
        w.add_maintenance_visit(
            16096,
            start_time="2026-05-22",
            reasons=["change", "repairs"],
            work="Skipt um móttakara og lagaði kapal",
        )
    put_body = mock_req.call_args_list[2].kwargs["data"]
    av_by_id = {
        r["id_maintenance_attribute_value"]: r["value"]
        for r in put_body["maintenance_attribute_values"]
    }
    assert av_by_id[80001] == "true"  # change
    assert av_by_id[80002] == "true"  # repairs
    assert av_by_id[80003] == "false"  # inspection still false


def test_add_maintenance_visit_no_reasons_sets_all_false():
    """remote vitjun with no `reasons` arg — all reason_* booleans = false."""
    w = _logged_in_writer(dry_run=False)
    created = {"id": 9999}
    detail = _maintenance_detail_with_seeded_attrs(9999, base=80000)
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [created, detail, {"ok": True}]
        w.add_maintenance_visit(
            16096,
            start_time="2026-05-22",
            maintenance_type="remote",
            work="Stillti hluti yfir SSH",
        )
    put_body = mock_req.call_args_list[2].kwargs["data"]
    av_by_id = {
        r["id_maintenance_attribute_value"]: r["value"]
        for r in put_body["maintenance_attribute_values"]
    }
    for attr_id in (80001, 80002, 80003, 80004, 80005):
        assert av_by_id[attr_id] == "false"


def test_add_maintenance_visit_raises_if_post_returns_no_id():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value={"unexpected": "response"}):
        with pytest.raises(RuntimeError, match="POST returned no id"):
            w.add_maintenance_visit(16096, start_time="2026-05-22")


def test_add_maintenance_visit_raises_if_detail_missing():
    w = _logged_in_writer(dry_run=False)
    created = {"id": 9999}
    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [created, None]
        with pytest.raises(RuntimeError, match="cannot discover seeded"):
            w.add_maintenance_visit(16096, start_time="2026-05-22")


# ---------------------------------------------------------------------------
# delete_entity_connection
# ---------------------------------------------------------------------------


def test_delete_entity_connection_hits_admin_endpoint():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=None) as mock_req:
        w.delete_entity_connection(27836)
    mock_req.assert_called_once_with("DELETE", "/admin_entity_connection_row/27836")


def test_delete_entity_connection_respects_dry_run():
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = DryRunResult(
            method="DELETE",
            endpoint="/admin_entity_connection_row/27836",
            payload=None,
        )
        result = w.delete_entity_connection(27836)
    assert isinstance(result, DryRunResult)
    assert result.method == "DELETE"
    assert result.endpoint == "/admin_entity_connection_row/27836"


# ---------------------------------------------------------------------------
# update_maintenance_visit — fetch / merge / PUT
# ---------------------------------------------------------------------------


def test_update_maintenance_visit_preserves_unspecified_fields():
    """Only the fields the caller passes should change; others preserve."""
    w = _logged_in_writer(dry_run=False)
    current = _maintenance_detail_with_seeded_attrs(5147, base=80000)
    current["start_time"] = "2025-09-23T00:00:00"
    current["end_time"] = "2025-09-23T00:00:00"
    current["participants"] = "bhb@vedur.is"
    current["completed"] = True
    # Pretend "work" has existing text, reason_change=true:
    for av in current["maintenance_attribute_values"]:
        if av["code"] == "work":
            av["value"] = "Old text"
        if av["code"] == "reason_change":
            av["value"] = "true"

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [current, {"ok": True}]
        result = w.update_maintenance_visit(5147, remaining="Þarf að mála")

    # Only "remaining" should be the new value; everything else preserved
    put_call = mock_req.call_args_list[1]
    assert put_call.args[0] == "PUT"
    assert put_call.args[1] == "/maintenance/id_maintenance/5147"
    body = put_call.kwargs["data"]
    assert body["participants"] == "bhb@vedur.is"
    assert body["start_time"] == "2025-09-23T00:00:00"
    assert body["completed"] is True
    av_by_code = {
        row["id_maintenance_attribute_value"]: row["value"]
        for row in body["maintenance_attribute_values"]
    }
    # work preserved
    assert av_by_code[80006] == "Old text"
    # remaining set
    assert av_by_code[80008] == "Þarf að mála"
    # reason_change preserved
    assert av_by_code[80001] == "true"
    assert result["id_maintenance"] == 5147


def test_update_maintenance_visit_reasons_replaces_full_set():
    """Passing `reasons` replaces all reason booleans, not just one."""
    w = _logged_in_writer(dry_run=False)
    current = _maintenance_detail_with_seeded_attrs(5147, base=80000)
    for av in current["maintenance_attribute_values"]:
        if av["code"] == "reason_change":
            av["value"] = "true"
        if av["code"] == "reason_repairs":
            av["value"] = "false"

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [current, {"ok": True}]
        w.update_maintenance_visit(5147, reasons=["repairs", "inspection"])

    body = mock_req.call_args_list[1].kwargs["data"]
    av_by_code = {
        row["id_maintenance_attribute_value"]: row["value"]
        for row in body["maintenance_attribute_values"]
    }
    assert av_by_code[80001] == "false"  # change — was true, replaced
    assert av_by_code[80002] == "true"  # repairs — set
    assert av_by_code[80003] == "true"  # inspection — set
    assert av_by_code[80004] == "false"  # improvements
    assert av_by_code[80005] == "false"  # other


def test_update_maintenance_visit_empty_string_writes_empty():
    """Caller passing '' should clear the field (not be treated as None)."""
    w = _logged_in_writer(dry_run=False)
    current = _maintenance_detail_with_seeded_attrs(5147, base=80000)
    for av in current["maintenance_attribute_values"]:
        if av["code"] == "remaining":
            av["value"] = "old outstanding text"

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [current, {"ok": True}]
        w.update_maintenance_visit(5147, remaining="")

    body = mock_req.call_args_list[1].kwargs["data"]
    av_by_code = {
        row["id_maintenance_attribute_value"]: row["value"]
        for row in body["maintenance_attribute_values"]
    }
    assert av_by_code[80008] == ""


def test_update_maintenance_visit_validates_reason_codes():
    w = _logged_in_writer(dry_run=False)
    with pytest.raises(ValueError, match="unknown reason codes"):
        w.update_maintenance_visit(5147, reasons=["bogus"])


def test_update_maintenance_visit_raises_when_not_found():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=None):
        with pytest.raises(RuntimeError, match="no maintenance with id"):
            w.update_maintenance_visit(99999, work="x")


def test_update_maintenance_visit_returns_before_after():
    """The result includes pre-edit state + the payload sent."""
    w = _logged_in_writer(dry_run=False)
    current = _maintenance_detail_with_seeded_attrs(5147, base=80000)
    current["participants"] = "old@vedur.is"

    with patch.object(w, "_request") as mock_req:
        mock_req.side_effect = [current, {"ok": True}]
        result = w.update_maintenance_visit(5147, participants="bgo@vedur.is")

    assert result["before"] is current
    assert result["after"]["participants"] == "bgo@vedur.is"


def test_update_maintenance_visit_dry_run_still_returns_merge():
    """Dry-run: PUT short-circuits but merge is still computed."""
    w = _logged_in_writer(dry_run=True)
    current = _maintenance_detail_with_seeded_attrs(5147, base=80000)

    def side_effect(method, path, *_a, **kw):
        if method == "GET":
            return current
        return DryRunResult(method=method, endpoint=path, payload=kw.get("data"))

    with patch.object(w, "_request", side_effect=side_effect):
        result = w.update_maintenance_visit(5147, work="test edit", reasons=["change"])
    assert isinstance(result["updated"], DryRunResult)
    # Merge still computed
    body = result["after"]
    av_by_code = {
        r["id_maintenance_attribute_value"]: r["value"]
        for r in body["maintenance_attribute_values"]
    }
    assert av_by_code[80001] == "true"  # reason_change
    assert av_by_code[80006] == "test edit"


# ---------------------------------------------------------------------------
# TOSWriter — contact↔entity relationship writes (Phase: contact writes)
# ---------------------------------------------------------------------------


def _rel_row(**overrides):
    """A raw admin contact-relationship row as TOS returns it."""
    row = {
        "id": 5018,
        "id_contact": 1256,
        "id_entity": 4316,
        "role": "owner",
        "time_from": "2025-02-04T15:32:38",
        "time_to": None,
    }
    row.update(overrides)
    return row


def test_patch_contact_relationship_requires_a_field():
    w = _logged_in_writer()
    with pytest.raises(ValueError, match="at least one of"):
        w.patch_contact_relationship(5018)


def test_patch_contact_relationship_missing_row_raises():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=None):
        with pytest.raises(ValueError, match="no relationship row"):
            w.patch_contact_relationship(99999, time_from="2006-06-29")


def test_patch_contact_relationship_get_merge_put_preserves_other_fields():
    """The admin endpoint is PUT-replace, so the writer GET-merges-PUTs:
    only the changed field differs; id_contact / id_entity / role and the
    untouched date are carried over from the current row."""
    w = _logged_in_writer(dry_run=False)
    calls = []

    def fake_request(method, endpoint, data=None, **kw):
        calls.append((method, endpoint, data))
        if method == "GET":
            return _rel_row()
        return {"ok": True}

    with patch.object(w, "_request", side_effect=fake_request):
        w.patch_contact_relationship(5018, time_from="2006-06-29")

    get_call, put_call = calls
    assert get_call[0] == "GET"
    assert put_call[0] == "PUT"
    assert put_call[1] == "/admin_contact_entity_relationship_row/5018"
    payload = put_call[2]
    assert payload["time_from"] == "2006-06-29T00:00:00"  # changed + promoted
    assert payload["id_contact"] == 1256  # preserved
    assert payload["id_entity"] == 4316  # preserved
    assert payload["role"] == "owner"  # preserved
    assert payload["time_to"] is None  # preserved


def test_patch_contact_relationship_role_only():
    w = _logged_in_writer(dry_run=False)

    def fake_request(method, endpoint, data=None, **kw):
        return _rel_row() if method == "GET" else {"ok": True}

    with patch.object(w, "_request", side_effect=fake_request) as mock_req:
        w.patch_contact_relationship(5018, role="operator")
    put_payload = mock_req.call_args.kwargs["data"]
    assert put_payload["role"] == "operator"
    assert put_payload["time_from"] == "2025-02-04T15:32:38"  # unchanged


def test_patch_contact_relationship_respects_dry_run():
    from tostools.api.tos_writer import DryRunResult

    w = _logged_in_writer(dry_run=True)

    # GET still happens in dry-run (reads are safe); only the PUT is held.
    def fake_request(method, endpoint, data=None, _force_send=False, **kw):
        if method == "GET":
            return _rel_row()
        # Mimic the real _request dry-run interception for mutating calls.
        return DryRunResult(method=method, endpoint=endpoint, payload=data)

    with patch.object(w, "_request", side_effect=fake_request):
        result = w.patch_contact_relationship(5018, time_from="2006-06-29")
    assert isinstance(result, DryRunResult)
    assert result.method == "PUT"


def test_create_contact_relationship_posts_to_contact_joins():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 6000}
        w.create_contact_relationship(1256, 4316, "operator", "2020-01-01")
    call = mock_req.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "/contact_joins"
    payload = call.kwargs["data"]
    assert payload == {
        "id_contact": 1256,
        "id_entity": 4316,
        "role": "operator",
        "time_from": "2020-01-01T00:00:00",  # bare date promoted
        "time_to": None,
    }


def test_delete_contact_relationship_uses_admin_row_endpoint():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        w.delete_contact_relationship(5018)
    call = mock_req.call_args
    assert call.args[0] == "DELETE"
    assert call.args[1] == "/admin_contact_entity_relationship_row/5018"


def test_get_contact_relationship_returns_row_or_none():
    w = _logged_in_writer()
    with patch.object(w, "_request", return_value=_rel_row()):
        assert w.get_contact_relationship(5018)["id_contact"] == 1256
    with patch.object(w, "_request", return_value=None):
        assert w.get_contact_relationship(5018) is None


# ---------------------------------------------------------------------------
# TOSWriter — contact entity writes (create / patch_contact)
# ---------------------------------------------------------------------------


def test_create_contact_posts_to_contacts_with_defaults():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 9001}
        w.create_contact(name="Test Org", organization="Test Org", phone_primary="555")
    call = mock_req.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "/contacts"
    payload = call.kwargs["data"]
    assert payload["name"] == "Test Org"
    assert payload["organization"] == "Test Org"
    assert payload["phone_primary"] == "555"
    # Unset fields default to empty string (the GET-shape convention).
    assert payload["email"] == ""
    assert payload["address"] == ""
    assert payload["ssid"] == ""


def test_create_contact_normalises_dates():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = {"id": 9001}
        w.create_contact(name="X", start_date="2026-01-01", end_date="2030-01-01")
    payload = mock_req.call_args.kwargs["data"]
    assert payload["start_date"] == "2026-01-01T00:00:00"
    assert payload["end_date"] == "2030-01-01T00:00:00"


def test_create_contact_rejects_unknown_field():
    w = _logged_in_writer()
    with pytest.raises(ValueError, match="unknown field"):
        w.create_contact(name="X", bogus="y")


def test_create_contact_respects_dry_run():
    from tostools.api.tos_writer import DryRunResult

    w = _logged_in_writer(dry_run=True)
    with patch("requests.request") as mock_http:
        result = w.create_contact(name="X")
    mock_http.assert_not_called()
    assert isinstance(result, DryRunResult)
    assert result.method == "POST"
    assert result.endpoint == "/contacts"


def test_patch_contact_get_merge_put_preserves_unchanged():
    w = _logged_in_writer(dry_run=False)
    current = {
        "id": 1256,
        "name": "Veðurstofa Íslands",
        "organization": "Veðurstofa Íslands",
        "job_title": "",
        "phone_primary": "5226000",
        "phone_secondary": "",
        "phone_tertiary": "",
        "email": "",
        "address": "Bústaðarvegur 7-9",
        "comment": "",
        "start_date": "1845-01-01T00:00:00",
        "end_date": None,
        "ssid": "6309080350",
    }

    def fake_request(method, endpoint, data=None, **kw):
        return current if method == "GET" else {"ok": True}

    with patch.object(w, "_request", side_effect=fake_request) as mock_req:
        w.patch_contact(1256, phone_primary="9999999")
    put_payload = mock_req.call_args.kwargs["data"]
    assert put_payload["phone_primary"] == "9999999"  # changed
    assert put_payload["name"] == "Veðurstofa Íslands"  # preserved
    assert put_payload["address"] == "Bústaðarvegur 7-9"  # preserved
    assert put_payload["ssid"] == "6309080350"  # preserved


def test_patch_contact_requires_a_field():
    w = _logged_in_writer()
    with pytest.raises(ValueError, match="at least one field"):
        w.patch_contact(1256)


def test_patch_contact_rejects_unknown_field():
    w = _logged_in_writer()
    with pytest.raises(ValueError, match="unknown field"):
        w.patch_contact(1256, bogus="y")


def test_patch_contact_missing_contact_raises():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=None):
        with pytest.raises(ValueError, match="no contact"):
            w.patch_contact(99999, name="X")


# ---------------------------------------------------------------------------
# delete_maintenance
# ---------------------------------------------------------------------------


def test_delete_maintenance_hits_admin_endpoint():
    w = _logged_in_writer(dry_run=False)
    with patch.object(w, "_request", return_value=None) as mock_req:
        w.delete_maintenance(5147)
    mock_req.assert_called_once_with("DELETE", "/admin_maintenance_row/5147")


def test_delete_maintenance_respects_dry_run():
    w = _logged_in_writer(dry_run=True)
    with patch.object(w, "_request") as mock_req:
        mock_req.return_value = DryRunResult(
            method="DELETE",
            endpoint="/admin_maintenance_row/5147",
            payload=None,
        )
        result = w.delete_maintenance(5147)
    assert isinstance(result, DryRunResult)
    assert result.method == "DELETE"
    assert result.endpoint == "/admin_maintenance_row/5147"
