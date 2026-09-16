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
