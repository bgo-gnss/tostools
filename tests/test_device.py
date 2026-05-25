"""Unit tests for the device module + the ``tos device add`` CLI handler.

No network required — TOSWriter and TOSClient are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tostools.device import (
    OPTIONAL_ATTR_CODES,
    REQUIRED_ATTR_CODES,
    VALID_SUBTYPES,
    build_required_attributes,
    iter_optional_attributes,
    normalize_date_start,
    validate_model,
)

# ---------------------------------------------------------------------------
# normalize_date_start
# ---------------------------------------------------------------------------


def test_normalize_date_start_expands_calendar_date() -> None:
    assert normalize_date_start("2026-05-11") == "2026-05-11T00:00:00"


def test_normalize_date_start_passes_full_iso_through() -> None:
    assert normalize_date_start("2026-05-11T14:30:00") == "2026-05-11T14:30:00"


def test_normalize_date_start_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        normalize_date_start("")


def test_normalize_date_start_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_date_start("11/05/2026")


def test_normalize_date_start_rejects_timezone_suffix() -> None:
    # We let TOSWriter._tos_date strip Z/+HH:MM downstream; our normaliser is
    # strict so the user sees the mismatch up-front.
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_date_start("2026-05-11T14:30:00Z")


def test_normalize_date_start_rejects_invalid_calendar() -> None:
    with pytest.raises(ValueError):
        normalize_date_start("2026-02-30")


# ---------------------------------------------------------------------------
# validate_model
# ---------------------------------------------------------------------------


def test_validate_model_gnss_receiver_alias_resolves_to_igs() -> None:
    assert validate_model("gnss_receiver", "PolaRx5") == "SEPT POLARX5"


def test_validate_model_gnss_receiver_identity_for_igs_name() -> None:
    # The IGS dict maps canonical strings only through alias keys — the
    # canonical "SEPT POLARX5" is not itself a key, so this exercises the
    # case-folded fallback in to_igs_receiver.
    assert validate_model("gnss_receiver", "POLARX5") == "SEPT POLARX5"


def test_validate_model_gnss_receiver_polarx2_alias_resolves_to_igs() -> None:
    # POLARX2 (historical pre-PolaRX5 era) — multiple aliases all
    # resolve to the canonical IGS name. Added during SAVI history
    # reconstruction; KVIS/FTEY/GAKE/GAK1/GAK2/HEDI also need this.
    assert validate_model("gnss_receiver", "PolaRX2") == "SEPT POLARX2"
    assert validate_model("gnss_receiver", "PolaRx2") == "SEPT POLARX2"


def test_validate_model_gnss_receiver_polarx2_canonical_identity() -> None:
    # Bare "SEPT POLARX2" — should resolve via the case-folded fallback
    # since "POLARX2" is now in RECEIVER_IGS as a key.
    assert validate_model("gnss_receiver", "SEPT POLARX2") == "SEPT POLARX2"


def test_validate_model_gnss_receiver_polarx2e_alias_resolves_to_igs() -> None:
    # PolaRX2E (Enhanced variant) — separate model from PolaRX2.
    assert validate_model("gnss_receiver", "PolaRX2E") == "SEPT POLARX2E"


def test_validate_model_antenna_identity() -> None:
    assert validate_model("antenna", "TRM57971.00") == "TRM57971.00"


def test_validate_model_radome_none() -> None:
    assert validate_model("radome", "NONE") == "NONE"


def test_validate_model_monument_passthrough() -> None:
    assert validate_model("monument", "Steel pillar v3") == "Steel pillar v3"


def test_validate_model_rejects_empty() -> None:
    with pytest.raises(ValueError, match="--model is required"):
        validate_model("gnss_receiver", "")


def test_validate_model_unknown_receiver_lists_known_names() -> None:
    with pytest.raises(ValueError) as exc:
        validate_model("gnss_receiver", "NotAReceiver")
    msg = str(exc.value)
    assert "NotAReceiver" in msg
    assert "SEPT POLARX5" in msg
    assert "TRIMBLE NETR9" in msg
    assert "igs_equipment" in msg


def test_validate_model_unknown_antenna_lists_known_names() -> None:
    with pytest.raises(ValueError) as exc:
        validate_model("antenna", "BogusAntenna")
    msg = str(exc.value)
    assert "BogusAntenna" in msg
    assert "TRM57971.00" in msg


def test_validate_model_unknown_radome_lists_known_codes() -> None:
    with pytest.raises(ValueError) as exc:
        validate_model("radome", "WIBBLE")
    msg = str(exc.value)
    assert "WIBBLE" in msg
    assert "SPKE" in msg
    assert "NONE" in msg


def test_validate_model_rejects_unknown_subtype() -> None:
    with pytest.raises(ValueError, match="Unknown subtype"):
        validate_model("starship", "Falcon-9")


# ---------------------------------------------------------------------------
# build_required_attributes
# ---------------------------------------------------------------------------


def test_build_required_attributes_shape() -> None:
    attrs = build_required_attributes(
        serial="SN1",
        model="SEPT POLARX5",
        owner="Veðurstofa Íslands",
        date_start="2026-05-11T00:00:00",
    )
    assert [a["code"] for a in attrs] == list(REQUIRED_ATTR_CODES)
    assert all(a["date_from"] == "2026-05-11T00:00:00" for a in attrs)
    by_code = {a["code"]: a["value"] for a in attrs}
    assert by_code["serial_number"] == "SN1"
    assert by_code["model"] == "SEPT POLARX5"
    assert by_code["owner"] == "Veðurstofa Íslands"
    assert by_code["status"] == "virkt"
    assert by_code["date_start"] == "2026-05-11T00:00:00"
    # Location is intentionally NOT an attribute on the device — it is
    # represented via an entity_connection to the location entity instead.
    assert "location" not in by_code


# ---------------------------------------------------------------------------
# iter_optional_attributes
# ---------------------------------------------------------------------------


def test_iter_optional_attributes_drops_empty_and_none() -> None:
    assert iter_optional_attributes(firmware="", comment=None, galvos="") == []


def test_iter_optional_attributes_canonical_order() -> None:
    pairs = iter_optional_attributes(firmware="4.14.0", comment="hello", galvos="99999")
    assert pairs == [
        ("firmware_version", "4.14.0"),
        ("comment", "hello"),
        ("galvos", "99999"),
    ]
    assert [code for code, _ in pairs] == list(OPTIONAL_ATTR_CODES)


def test_iter_optional_attributes_partial() -> None:
    assert iter_optional_attributes(firmware=None, comment="note", galvos=None) == [
        ("comment", "note"),
    ]


# ---------------------------------------------------------------------------
# VALID_SUBTYPES contract
# ---------------------------------------------------------------------------


def test_valid_subtypes_are_the_supported_ones() -> None:
    assert set(VALID_SUBTYPES) == {"gnss_receiver", "antenna", "radome", "monument"}


# ---------------------------------------------------------------------------
# _device_main CLI handler
# ---------------------------------------------------------------------------


@pytest.fixture
def owners_yaml(tmp_path: Path) -> Path:
    """Materialise a small owners cache file with one known owner."""
    p = tmp_path / "owners.yaml"
    p.write_text(yaml.safe_dump({"owners": ["Veðurstofa Íslands"]}, allow_unicode=True))
    return p


@pytest.fixture
def base_argv(owners_yaml: Path) -> list:
    """Argv for the happy-path `add` invocation (dry-run by default)."""
    return [
        "add",
        "--subtype",
        "gnss_receiver",
        "--serial",
        "SN_HAPPY",
        "--model",
        "PolaRx5",
        "--owner",
        "Veðurstofa Íslands",
        "--location",
        "Bench A",
        "--date-start",
        "2026-05-11",
        "--owners-cache",
        str(owners_yaml),
    ]


def _make_writer_mock(*, dry_run_response=None, live_response=None) -> MagicMock:
    """Build a TOSWriter mock that mimics the real return contract."""
    writer = MagicMock()
    writer.create_device.return_value = (
        dry_run_response or live_response or {"id_entity": 12345}
    )
    writer.upsert_attribute_value.return_value = {"ok": True}
    return writer


def test_device_main_dry_run_happy_path(base_argv: list, capsys) -> None:
    from tostools.tos import _device_main

    writer = _make_writer_mock()
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer) as tw_cls:
        rc = _device_main(base_argv)
    out = capsys.readouterr()

    assert rc == 0, out.err
    # TOSWriter was constructed in dry-run by default.
    assert tw_cls.call_args.kwargs["dry_run"] is True
    # create_device received the canonical IGS receiver name.
    call = writer.create_device.call_args
    assert call.args[0] == "gnss_receiver"
    attrs = {a["code"]: a["value"] for a in call.args[1]}
    assert attrs["model"] == "SEPT POLARX5"
    assert attrs["serial_number"] == "SN_HAPPY"
    assert attrs["owner"] == "Veðurstofa Íslands"
    # location is NOT a device attribute — it is conveyed via entity_connection
    assert "location" not in attrs
    assert attrs["status"] == "virkt"
    assert attrs["date_start"] == "2026-05-11T00:00:00"
    assert call.kwargs["force"] is False
    # No optional inputs → no upsert calls.
    writer.upsert_attribute_value.assert_not_called()
    assert "DRY RUN" in out.out or "dry-run" in out.out.lower()


def test_device_main_unknown_owner(base_argv: list, capsys) -> None:
    from tostools.tos import _device_main

    argv = list(base_argv)
    argv[argv.index("Veðurstofa Íslands")] = "Bogus Group"

    with patch("tostools.api.tos_writer.TOSWriter") as tw_cls:
        rc = _device_main(argv)
    err = capsys.readouterr().err

    assert rc == 2
    assert "Bogus Group" in err
    assert "tos owners list" in err
    tw_cls.assert_not_called()


def test_device_main_unknown_model(base_argv: list, capsys) -> None:
    from tostools.tos import _device_main

    argv = list(base_argv)
    argv[argv.index("PolaRx5")] = "NotAReceiver"

    with patch("tostools.api.tos_writer.TOSWriter") as tw_cls:
        rc = _device_main(argv)
    err = capsys.readouterr().err

    assert rc == 2
    assert "NotAReceiver" in err
    assert "SEPT POLARX5" in err  # known-models table
    tw_cls.assert_not_called()


def test_device_main_duplicate_serial_without_force(base_argv: list, capsys) -> None:
    from tostools.tos import _device_main

    writer = MagicMock()
    writer.create_device.side_effect = ValueError(
        "Device with serial_number='SN_HAPPY' already exists as gnss_receiver "
        "(id_entity=42). Pass force=True to add a duplicate."
    )
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer):
        rc = _device_main(base_argv)
    err = capsys.readouterr().err

    assert rc == 1
    assert "already exists" in err
    assert "--force" in err


def test_device_main_force_flag_passes_through(base_argv: list) -> None:
    from tostools.tos import _device_main

    writer = _make_writer_mock()
    argv = base_argv + ["--force"]
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer):
        rc = _device_main(argv)

    assert rc == 0
    assert writer.create_device.call_args.kwargs["force"] is True


def test_device_main_no_dry_run_flips_writer_flag(base_argv: list) -> None:
    from tostools.tos import _device_main

    writer = _make_writer_mock()
    argv = base_argv + ["--no-dry-run"]
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer) as tw_cls:
        rc = _device_main(argv)

    assert rc == 0
    assert tw_cls.call_args.kwargs["dry_run"] is False


def test_device_main_optional_attrs_drive_upserts_in_live_mode(
    base_argv: list,
) -> None:
    from tostools.tos import _device_main

    writer = _make_writer_mock(live_response={"id_entity": 999})
    argv = base_argv + [
        "--no-dry-run",
        "--firmware",
        "4.14.0",
        "--comment",
        "warehouse intake",
        "--galvos",
        "12345",
    ]
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer):
        rc = _device_main(argv)

    assert rc == 0
    # Each optional attribute → one upsert against the new id_entity.
    upsert_calls = writer.upsert_attribute_value.call_args_list
    assert len(upsert_calls) == 3
    codes = [c.kwargs.get("code") or c.args[1] for c in upsert_calls]
    assert codes == ["firmware_version", "comment", "galvos"]
    for call in upsert_calls:
        # id_entity is the first positional, date_from carries date_start.
        assert call.args[0] == 999
        assert call.kwargs.get("date_from") == "2026-05-11T00:00:00"


def test_device_main_dry_run_logs_optional_upserts_without_calling_them(
    base_argv: list, capsys
) -> None:
    from tostools.tos import _device_main

    writer = _make_writer_mock()
    argv = base_argv + ["--firmware", "4.14.0", "--galvos", "55555"]
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer):
        rc = _device_main(argv)
    out = capsys.readouterr().out

    assert rc == 0
    writer.upsert_attribute_value.assert_not_called()
    assert "firmware_version" in out
    assert "4.14.0" in out
    assert "galvos" in out
    assert "55555" in out


def test_device_main_json_output(base_argv: list, capsys) -> None:
    import json as _json

    from tostools.tos import _device_main

    writer = _make_writer_mock()
    argv = base_argv + ["--json"]
    with patch("tostools.api.tos_writer.TOSWriter", return_value=writer):
        rc = _device_main(argv)
    out = capsys.readouterr().out

    assert rc == 0
    payload = _json.loads(out)
    assert payload["subtype"] == "gnss_receiver"
    assert payload["serial"] == "SN_HAPPY"
    assert payload["model"] == "SEPT POLARX5"
    assert payload["dry_run"] is True


def test_device_main_requires_add_action() -> None:
    from tostools.tos import _device_main

    # `tos device` with no action → argparse should abort with non-zero exit.
    with pytest.raises(SystemExit) as exc:
        _device_main([])
    assert exc.value.code != 0
