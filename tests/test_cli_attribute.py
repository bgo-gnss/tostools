"""`tos attribute show` — the drill-down for an id_attribute_value.

Every TOS temporal view is assembled from `attribute_value` rows and every
table prints their ids, but nothing could look one back up: `TOSClient` had
getters keyed on `id_entity`, `id_contact`, `id_maintenance` and `id_child`,
and `GET /attribute_value/{id}` was reachable only from inside `TOSWriter` as
its post-PATCH read-back.

What these tests pin, in order of how badly each would hurt:

* **The rendered output is never truncated.** The whole reason this verb
  exists is that summary views lose the time component; a six-column rich
  table elides `2009-05-09T00:00:00` to `2009-05-09T…` at ordinary terminal
  widths, which reintroduces the exact defect. The record layout must survive
  a narrow console.
* **A missing id does not hide the rows that WERE found**, and still exits 1.
* **`--json` is the raw row, verbatim.** No reshaping, no date truncation —
  it is the machine surface for precisely the comparison the day-granularity
  form cannot support.
* **Entity resolution never fails the read.** It is a convenience lookup; a
  broken or absent entity must degrade to the bare id.
* **An open period renders as "open", not as blank** — a blank cell reads as
  missing data.
"""

from __future__ import annotations

import io
import json

import pytest

from tostools import cli_attribute

# V159's real `name` chain — a 10-hour period handing off to its successor.
ROW_OLD = {
    "id_attribute_value": 109880,
    "code": "name",
    "id_entity": 8934,
    "date_from": "2009-05-09T00:00:00",
    "date_to": "2009-05-09T10:00:00",
    "value": "Sandgígjukvísl",
}
ROW_OPEN = {
    "id_attribute_value": 109888,
    "code": "name",
    "id_entity": 8934,
    "date_from": "2009-05-09T10:00:00",
    "date_to": None,
    "value": "Gígjukvísl",
}
ENTITY = {
    "subtype": "hydrological",
    "attributes": [
        {"code": "marker", "value": "V159", "date_to": None},
        {"code": "name", "value": "Gígjukvísl", "date_to": None},
    ],
}


class FakeClient:
    def __init__(self, rows=None, entity=ENTITY, entity_raises=False):
        self.rows = (
            rows
            if rows is not None
            else {r["id_attribute_value"]: r for r in (ROW_OLD, ROW_OPEN)}
        )
        self.entity = entity
        self.entity_raises = entity_raises
        self.entity_calls = 0

    def get_attribute_value(self, id_av):
        return self.rows.get(int(id_av))

    def get_entity_history(self, id_entity):
        self.entity_calls += 1
        if self.entity_raises:
            raise RuntimeError("TOS unreachable")
        return self.entity


@pytest.fixture
def patched(monkeypatch):
    """Install a FakeClient in place of TOSClient; return a runner."""
    holder = {}

    def run(argv, **client_kw):
        client = FakeClient(**client_kw)
        holder["client"] = client
        import tostools.api.tos_client as mod

        monkeypatch.setattr(mod, "TOSClient", lambda *a, **k: client)
        return cli_attribute.main(argv)

    run.holder = holder
    return run


def _render_to_text(rows, client, *, resolve=True, width=60):
    from rich.console import Console

    buf = io.StringIO()
    cli_attribute._render(
        Console(file=buf, width=width, no_color=True), rows, client, resolve=resolve
    )
    return buf.getvalue()


class TestTheTimestampIsNeverTruncated:
    """The defect this verb exists to avoid must not reappear in its output."""

    @pytest.mark.parametrize("width", [40, 60, 80, 200])
    def test_full_timestamps_survive_every_width(self, width):
        text = _render_to_text([ROW_OLD], FakeClient(), width=width)
        assert "2009-05-09T00:00:00" in text
        assert "2009-05-09T10:00:00" in text
        assert "…" not in text

    def test_json_is_the_raw_row_verbatim(self, patched, capsys):
        assert patched(["show", "109880", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == [ROW_OLD]


class TestOpenPeriods:
    def test_open_period_says_open(self):
        text = _render_to_text([ROW_OPEN], FakeClient())
        assert "date_to    open" in text

    def test_json_keeps_null_for_open(self, patched, capsys):
        assert patched(["show", "109888", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["date_to"] is None


class TestMissingIds:
    def test_missing_id_exits_1(self, patched):
        assert patched(["show", "999999999"]) == 1

    def test_found_rows_still_print_when_a_sibling_is_missing(self, patched, capsys):
        rc = patched(["show", "109880", "999999999", "--json"])
        assert rc == 1
        assert json.loads(capsys.readouterr().out) == [ROW_OLD]

    def test_the_missing_id_is_named_on_stderr(self, patched, capsys):
        patched(["show", "999999999"])
        assert "999999999" in capsys.readouterr().err


class TestEntityResolution:
    def test_label_combines_marker_and_name(self):
        text = _render_to_text([ROW_OLD], FakeClient())
        assert "V159/Gígjukvísl" in text
        assert "(hydrological)" in text

    def test_a_broken_entity_lookup_does_not_fail_the_read(self):
        """Labelling is a convenience — it must never sink the row."""
        text = _render_to_text([ROW_OLD], FakeClient(entity_raises=True))
        assert "Sandgígjukvísl" in text
        assert "id_entity=8934" in text

    def test_no_resolve_skips_the_extra_request(self):
        client = FakeClient()
        _render_to_text([ROW_OLD], client, resolve=False)
        assert client.entity_calls == 0

    def test_resolve_is_the_default(self):
        client = FakeClient()
        _render_to_text([ROW_OLD], client)
        assert client.entity_calls == 1


class TestItIsEntityAgnostic:
    def test_a_sim_card_row_renders_the_same_way(self):
        """No subtype pin: a SIM, a gauge and a GPS station are all just rows."""
        sim_row = {
            "id_attribute_value": 152983,
            "code": "serial_number",
            "id_entity": 21516,
            "date_from": "2026-05-29T00:00:00",
            "date_to": None,
            "value": "89354010240902268817",
        }
        sim_entity = {
            "subtype": "sim_card",
            "attributes": [
                {
                    "code": "serial_number",
                    "value": "89354010240902268817",
                    "date_to": None,
                },
                {"code": "model", "value": "SIM kort", "date_to": None},
            ],
        }
        text = _render_to_text([sim_row], FakeClient(entity=sim_entity))
        assert "89354010240902268817" in text
        assert "(sim_card)" in text


class TestTheClientGetter:
    """The FakeClient above bypasses TOSClient entirely, so the real method
    needs its own cover — the mutation that drops its isinstance guard was
    NOT DETECTED until these existed."""

    def _client(self, payload, monkeypatch):
        from tostools.api.tos_client import TOSClient

        c = TOSClient()
        monkeypatch.setattr(c, "_make_request", lambda ep: payload)
        return c

    def test_a_dict_row_passes_through(self, monkeypatch):
        c = self._client(ROW_OLD, monkeypatch)
        assert c.get_attribute_value(109880) == ROW_OLD

    @pytest.mark.parametrize("payload", [None, [], "not-a-row", 42, ["x"]])
    def test_a_non_dict_payload_becomes_none(self, payload, monkeypatch):
        """A 404 or an unexpected shape must read as 'nothing to show', not
        crash the renderer downstream on .get()."""
        c = self._client(payload, monkeypatch)
        assert c.get_attribute_value(1) is None

    def test_it_calls_the_documented_endpoint(self, monkeypatch):
        from tostools.api.tos_client import TOSClient

        seen = {}
        c = TOSClient()
        monkeypatch.setattr(c, "_make_request", lambda ep: seen.setdefault("ep", ep))
        c.get_attribute_value(109880)
        assert seen["ep"] == "/attribute_value/109880"


class TestWiring:
    def test_attribute_is_a_known_subcommand(self):
        from tostools.tos import KNOWN_SUBCOMMANDS

        assert "attribute" in KNOWN_SUBCOMMANDS

    def test_umbrella_help_lists_it(self, capsys):
        from tostools.tos import _print_top_level_help

        _print_top_level_help()
        assert "attribute" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The entity label is resolved AS OF the row, not as of today
# ---------------------------------------------------------------------------

# V159's name chain as the entity-history endpoint returns it.
V159_HISTORY = {
    "code_entity_subtype": "hydrological",
    "attributes": [
        {
            "code": "marker",
            "value": "V159",
            "date_from": "1900-01-01T00:00:00",
            "date_to": None,
        },
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
    ],
}


class TestTheLabelIsAsOfTheRow:
    """Captioning a 2009 assertion with today's name is the same time-blind
    mistake as truncating the boundary to a day — and it is worse here,
    because the caption then contradicts the value printed directly above it.
    """

    def test_the_closed_period_is_labelled_with_its_own_name(self):
        text = _render_to_text([ROW_OLD], FakeClient(entity=V159_HISTORY))
        assert "V159/Sandgígjukvísl" in text
        assert "V159/Gígjukvísl" not in text

    def test_the_open_period_is_labelled_with_the_current_name(self):
        text = _render_to_text([ROW_OPEN], FakeClient(entity=V159_HISTORY))
        assert "V159/Gígjukvísl" in text

    def test_the_boundary_is_compared_at_full_precision(self):
        """Both periods fall on 2009-05-09; only the TIME separates them, so a
        day-truncating comparison would label them identically."""
        old = _render_to_text([ROW_OLD], FakeClient(entity=V159_HISTORY))
        new = _render_to_text([ROW_OPEN], FakeClient(entity=V159_HISTORY))
        assert old.count("Sandgígjukvísl") and "Sandgígjukvísl" not in new

    def test_an_instant_before_any_name_existed_falls_back_to_marker(self):
        row = dict(ROW_OLD, date_from="1990-01-01T00:00:00")
        text = _render_to_text([row], FakeClient(entity=V159_HISTORY))
        assert "V159" in text
        assert "Sandgígjukvísl" not in text.split("entity")[-1]


class TestCoveringPeriodSelection:
    def test_end_is_exclusive_and_start_inclusive(self):
        from tostools.cli_attribute import _covering

        periods = [
            {
                "value": "a",
                "date_from": "2009-05-09T00:00:00",
                "date_to": "2009-05-09T10:00:00",
            },
            {"value": "b", "date_from": "2009-05-09T10:00:00", "date_to": None},
        ]
        assert _covering(periods, "2009-05-09T00:00:00") == "a"
        assert _covering(periods, "2009-05-09T09:59:59") == "a"
        # The hand-off instant belongs to the SUCCESSOR, matching TOS's own
        # half-open convention — otherwise both periods claim it.
        assert _covering(periods, "2009-05-09T10:00:00") == "b"

    def test_at_none_selects_the_open_period(self):
        from tostools.cli_attribute import _covering

        periods = [
            {"value": "a", "date_from": "2000-01-01", "date_to": "2005-01-01"},
            {"value": "b", "date_from": "2005-01-01", "date_to": None},
        ]
        assert _covering(periods, None) == "b"
        # REVERSED too: "take the last row" happens to be right when the open
        # period sorts last, which let a mutation doing exactly that survive.
        assert _covering(list(reversed(periods)), None) == "b"

    def test_selection_does_not_depend_on_input_order(self):
        """Same reason: an inclusive-end bug makes TWO periods match the
        hand-off instant, and a last-wins loop hides it on sorted input."""
        from tostools.cli_attribute import _covering

        periods = [
            {
                "value": "a",
                "date_from": "2009-05-09T00:00:00",
                "date_to": "2009-05-09T10:00:00",
            },
            {"value": "b", "date_from": "2009-05-09T10:00:00", "date_to": None},
        ]
        for order in (periods, list(reversed(periods))):
            assert _covering(order, "2009-05-09T09:59:59") == "a"
            assert _covering(order, "2009-05-09T10:00:00") == "b"

    def test_no_covering_period_yields_none(self):
        from tostools.cli_attribute import _covering

        periods = [{"value": "a", "date_from": "2009-01-01", "date_to": "2010-01-01"}]
        assert _covering(periods, "2008-01-01") is None


class TestTheEntityNamespace:
    """`8934` is V159 as an ENTITY and a Dalatangi altitude as an ATTRIBUTE.

    The namespaces overlap numerically, so a bare id typed into the wrong one
    resolves silently to an unrelated row — in that real case a meteorological
    station in the east instead of a hydrological one on Skeiðarársandur.
    `--entity` gives the other namespace its own door.
    """

    def test_entity_lists_every_attribute_row(self, patched, capsys):
        rc = patched(["show", "--entity", "8934", "--json"], entity=V159_HISTORY)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert [r["code"] for r in out] == ["marker", "name", "name"]
        assert [r["value"] for r in out] == [
            "V159",
            "Sandgígjukvísl",
            "Gígjukvísl",
        ]

    def test_rows_are_stamped_with_the_requested_entity(self, patched, capsys):
        patched(["show", "--entity", "8934", "--json"], entity=V159_HISTORY)
        out = json.loads(capsys.readouterr().out)
        assert {r["id_entity"] for r in out} == {8934}

    def test_an_entity_with_no_attributes_exits_1(self, patched):
        assert patched(["show", "--entity", "1", "--json"], entity={}) == 1

    def test_neither_ids_nor_entity_is_a_usage_error(self, patched):
        with pytest.raises(SystemExit) as exc:
            patched(["show"])
        assert exc.value.code == 2
