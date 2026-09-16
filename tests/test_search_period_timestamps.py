"""`--history` period lists must not lose the time component.

`tos search --history --json` emits the raw period lists, and its own comment
says they are "for comparing against another record of the same chains". Day
truncation defeats exactly that in the one case where precision matters: two
periods of the same attribute that begin and end on the SAME DAY.

MEASURED on V159 (Gígjukvísl), 2026-09-16:

    GET /attribute_value/109880  name  Sandgígjukvísl
        2009-05-09T00:00:00 -> 2009-05-09T10:00:00
    GET /attribute_value/109888  name  Gígjukvísl
        2009-05-09T10:00:00 -> open

A normal 10-hour period handing off cleanly to its successor. Truncated to
days it reads `2009-05-09 -> 2009-05-09` beside `2009-05-09 -> null`, which
looks like a zero-length corrupt row — and was diagnosed as one.

What these tests pin:

* **The `_ts` keys carry the full timestamp**, so a same-day hand-off is
  legible.
* **The day keys stay exactly ten characters.** This is the load-bearing one:
  `value_at` compares `start > day` against a 10-char day string, so widening
  `date_from` to a timestamp would make it sort AFTER its own date and break
  every `--at` query — silently, by returning the wrong period rather than
  raising.
* **`--at` still selects correctly across a same-day boundary.**
"""

from __future__ import annotations

from tostools.search import attribute_periods, value_at

# V159's real `name` chain, in the shape TOS returns.
V159 = {
    "attributes": [
        {
            "code": "name",
            "value": "Sandgígjukvísl",
            "date_from": "2009-05-09T00:00:00",
            "date_to": "2009-05-09T10:00:00",
        },
        {
            "code": "name",
            "value": "Gígjukvísl",
            "date_from": "2009-05-09T10:00:00",
            "date_to": None,
        },
    ]
}


class TestTheTimestampSurvives:
    def test_ts_keys_are_not_truncated(self):
        periods = attribute_periods(V159, "name")
        assert [p["date_from_ts"] for p in periods] == [
            "2009-05-09T00:00:00",
            "2009-05-09T10:00:00",
        ]
        assert [p["date_to_ts"] for p in periods] == ["2009-05-09T10:00:00", None]

    def test_a_same_day_handoff_is_distinguishable(self):
        """The whole point: the two periods must not look identical."""
        first, second = attribute_periods(V159, "name")
        assert first["date_from"] == second["date_from"] == "2009-05-09"
        assert first["date_from_ts"] != second["date_from_ts"]

    def test_the_closed_period_is_not_zero_length(self):
        first = attribute_periods(V159, "name")[0]
        assert first["date_from_ts"] != first["date_to_ts"]

    def test_open_period_has_null_ts(self):
        assert attribute_periods(V159, "name")[-1]["date_to_ts"] is None


class TestTheDayKeysStayTenCharacters:
    """Widening these silently breaks every --at query. Do not."""

    def test_day_keys_are_day_granularity(self):
        for p in attribute_periods(V159, "name"):
            assert len(p["date_from"]) == 10
            if p["date_to"] is not None:
                assert len(p["date_to"]) == 10

    def test_at_still_resolves_across_a_same_day_boundary(self):
        # Day granularity cannot split 2009-05-09, and that is accepted —
        # `--at` is a day-level filter by design. What must hold is that it
        # keeps returning a covering period rather than nothing.
        assert value_at(V159, "name", "2009-05-10") == "Gígjukvísl"
        assert value_at(V159, "name") == "Gígjukvísl"

    def test_at_before_the_chain_starts_selects_nothing(self):
        assert value_at(V159, "name", "2008-01-01") is None


class TestDevicePeriodsInherit:
    def test_device_periods_carry_the_ts_keys_too(self):
        """device_periods delegates to attribute_periods — pin that it stays so."""
        from tostools.search import device_periods

        # `subtype` is a TOP-LEVEL key on the joined device, not an attribute —
        # `device_in_namespace` reads `device["subtype"]` and compares it to the
        # namespace verbatim.
        dev = {
            "subtype": "gnss_receiver",
            "attributes": [
                {
                    "code": "firmware_version",
                    "value": "5.7.0",
                    "date_from": "2026-01-02T09:30:00",
                    "date_to": None,
                },
            ],
        }
        out = device_periods([dev], "gnss_receiver", "firmware_version")
        assert out and out[0][0]["date_from_ts"] == "2026-01-02T09:30:00"
        assert out[0][0]["date_from"] == "2026-01-02"
