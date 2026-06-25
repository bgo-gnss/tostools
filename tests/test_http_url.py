"""Tests for :func:`tostools.api._http.canonical_tos_url`.

Covers the 2026-06 TOS backend migration: trailing-slash stripping (the new
backend 308-redirects ``/``-terminated paths, dropping auth on mutating calls)
and the fail-fast guard against a ``None``/``Null`` id leaking into a URL path.
"""

import pytest

from tostools.api._http import canonical_tos_url

BASE = "https://vi-api.vedur.is/tos/internal"


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        # Trailing slash is stripped (the whole point of the migration).
        ("/history/entity/5245/", f"{BASE}/history/entity/5245"),
        ("/basic_search/", f"{BASE}/basic_search"),
        (
            "/entity/search/station/geophysical/",
            f"{BASE}/entity/search/station/geophysical",
        ),
        ("/entity_subtypes/", f"{BASE}/entity_subtypes"),
        # Already slashless → unchanged.
        ("/join/28865", f"{BASE}/join/28865"),
        ("/entity/parent_history/17234", f"{BASE}/entity/parent_history/17234"),
        # Leading slash optional on the endpoint.
        ("join/28865", f"{BASE}/join/28865"),
    ],
)
def test_canonical_url_normalizes_trailing_slash(endpoint, expected):
    assert canonical_tos_url(BASE, endpoint) == expected


def test_base_url_with_trailing_slash_does_not_double():
    # Callers rstrip base_url, but the helper must not produce '//' regardless.
    assert canonical_tos_url(BASE + "/", "/join/1") == f"{BASE}/join/1"


def test_inline_query_string_is_preserved():
    # A query string must survive; only the path segment is trimmed.
    assert (
        canonical_tos_url(BASE, "/entity/search/?code=marker")
        == f"{BASE}/entity/search?code=marker"
    )


@pytest.mark.parametrize("null", ["None", "Null", "null", "NULL"])
def test_none_segment_raises_before_send(null):
    # A None/Null id in the path (str(None) → 'None') is a formatting bug that
    # the backend answers with 422 — fail fast with a clear, named error.
    with pytest.raises(ValueError, match="None/Null value leaked"):
        canonical_tos_url(BASE, f"/entity/{null}/")


def test_none_substring_in_value_is_allowed():
    # Only an exact 'None' path *segment* is rejected — a legitimate value that
    # merely contains the letters is fine.
    assert (
        canonical_tos_url(BASE, "/entity/search/None_Station")
        == f"{BASE}/entity/search/None_Station"
    )
