"""`station_role_orgs` must resolve a role to the agency that speaks for the
station NOW, not the first row TOS happens to return.

Regression for the stale-owner bug: SENG (Svartsengi) had an IES *owner*
relationship closed 2025-08-19 and an IMO *owner* relationship still open, and
the raw rows arrive closed-row-first. Picking the first row named the superseded
agency in site-log §12 — which then aborted the whole export, because the IES
entry in agencies.yaml still holds `TODO — IES contact` placeholders whose em
dash is not Latin-1 encodable.

No network: the client is a two-line stub, because the function only ever calls
`get_contacts`.
"""

from __future__ import annotations

from typing import Any, Dict, List

from tostools.core.agencies import station_role_orgs

IMO = "Veðurstofa Íslands"
IES = "Jarðvísindastofnun Háskóla Íslands"


class _StubClient:
    """Minimal TOSClient stand-in — `get_contacts` is the only method used."""

    def __init__(self, contacts: List[Dict[str, Any]]) -> None:
        self._contacts = contacts

    def get_contacts(self, entity_id: Any) -> List[Dict[str, Any]]:  # noqa: ARG002
        return self._contacts


def _rel(role: str, role_is: str, org: str, frm: str, to: str | None) -> Dict[str, Any]:
    return {
        "role": role,
        "role_is": role_is,
        "organization": org,
        "per_time_from": frm,
        "per_time_to": to,
    }


SENG_CONTACTS = [
    # The order TOS actually returns them in (see `tos contact list --json`).
    _rel("data_owner", "Eigandi gagna", IMO, "2025-08-19T13:21:44", None),
    _rel("operator", "Rekstraraðili stöðvar", IMO, "2025-08-19T13:21:44", None),
    _rel("owner", "Eigandi stöðvar", IES, "1000-01-01T00:00:00", "2025-08-19T13:23:12"),
    _rel("owner", "Eigandi stöðvar", IMO, "2015-06-26T00:00:00", None),
]


def test_open_owner_beats_an_earlier_closed_one() -> None:
    """The regression: the CLOSED IES row is listed first and must still lose."""
    roles = station_role_orgs(_StubClient(SENG_CONTACTS), {"id_entity": 16818})
    assert roles["owner"] == IMO, "a closed superseded owner relationship won §12"
    assert roles["data_owner"] == IMO


def test_row_order_does_not_matter() -> None:
    """Same data, reversed: the answer is a property of the dates, not the order."""
    roles = station_role_orgs(_StubClient(list(reversed(SENG_CONTACTS))), {})
    assert roles["owner"] == IMO


def test_closed_only_role_falls_back_to_the_latest_start() -> None:
    """No open period (a decommissioned station) → the most recent one wins."""
    rows = [
        _rel("owner", "Eigandi stöðvar", "A", "2001-01-01T00:00:00", "2005-01-01T00:00:00"),
        _rel("owner", "Eigandi stöðvar", "B", "2005-01-01T00:00:00", "2011-01-01T00:00:00"),
    ]
    assert station_role_orgs(_StubClient(rows), {})["owner"] == "B"


def test_data_owner_is_not_swallowed_by_the_owner_bucket() -> None:
    """`data_owner` contains the substring 'owner' — the bucket test must not."""
    roles = station_role_orgs(
        _StubClient(
            [_rel("data_owner", "Eigandi gagna", "DATAORG", "2020-01-01T00:00:00", None)]
        ),
        {},
    )
    assert roles == {"data_owner": "DATAORG"}


def test_operator_role_is_ignored() -> None:
    """§11 is always the IMO default; a Rekstraraðili must not leak into §12."""
    roles = station_role_orgs(
        _StubClient(
            [_rel("operator", "Rekstraraðili stöðvar", "OPORG", "2020-01-01T00:00:00", None)]
        ),
        {},
    )
    assert roles == {}


def test_empty_or_failing_lookup_degrades_to_no_roles() -> None:
    assert station_role_orgs(_StubClient([]), {}) == {}

    class _Boom:
        def get_contacts(self, entity_id: Any) -> List[Dict[str, Any]]:  # noqa: ARG002
            raise RuntimeError("TOS down")

    assert station_role_orgs(_Boom(), {}) == {}


def test_rows_without_an_org_are_skipped() -> None:
    """A role row with no organization must not shadow a later usable one."""
    rows = [
        _rel("owner", "Eigandi stöðvar", "", "2026-01-01T00:00:00", None),
        _rel("owner", "Eigandi stöðvar", IMO, "2015-06-26T00:00:00", None),
    ]
    assert station_role_orgs(_StubClient(rows), {})["owner"] == IMO
