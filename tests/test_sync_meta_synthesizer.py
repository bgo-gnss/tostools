"""Pin the synthesizer wiring for tosGPS syncMeta.

Regression test: previously ``_safe_update_workflow`` and
``_process_single_station`` called ``gpsqc.gps_metadata`` directly,
forcing syncMeta onto the legacy synthesis chain even when the rest
of tosGPS (PrintTOS / sitelog / rinex) was running the new
``devices.station_sessions`` chain. The legacy chain's
``print_station_info`` validator silently dropped sessions with
missing monument data, so ``syncMeta --update`` wrote partial
station.info to the GAMIT server (e.g. SAVI lost its ASHTECH UZ-12
session at the start of the timeline).

Fix: both helpers now accept a ``synthesizer`` callable defaulting
to ``gps_metadata_via_devices`` (the new chain). The
``_handle_sync_meta_subcommand`` caller passes
``_select_synthesizer(args)`` so ``--use-legacy-synthesis`` still
opts back into the legacy chain.

These tests pin the contract: both helpers must invoke the
synthesizer they were handed, not the hard-coded legacy one.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from tostools import gps_metadata_qc as gpsqc
from tostools.tosGPS import _select_synthesizer


def test_select_synthesizer_defaults_to_new_chain():
    """``_select_synthesizer`` returns the new-chain synthesizer by default."""
    args = SimpleNamespace()
    assert _select_synthesizer(args) is gpsqc.gps_metadata_via_devices


def test_select_synthesizer_opts_into_legacy_with_flag():
    """``--use-legacy-synthesis`` opts back into the legacy chain."""
    args = SimpleNamespace(use_legacy_synthesis=True)
    assert _select_synthesizer(args) is gpsqc.gps_metadata


def test_select_synthesizer_false_flag_uses_new_chain():
    """Explicit ``use_legacy_synthesis=False`` still selects new chain."""
    args = SimpleNamespace(use_legacy_synthesis=False)
    assert _select_synthesizer(args) is gpsqc.gps_metadata_via_devices


def _fake_station_info(marker: str, num_sessions: int) -> dict:
    """Minimal station_info dict; we only care about identity here."""
    return {
        "marker": marker,
        "name": "Fake Station",
        "_session_count": num_sessions,
    }


def test_safe_update_workflow_invokes_passed_synthesizer():
    """``_safe_update_workflow`` must call the synthesizer it was handed."""
    from tostools import tosGPS

    captured: list[tuple[str, str]] = []

    def fake_new_chain(station, url, loglevel=logging.WARNING):
        captured.append(("new", station))
        return _fake_station_info(station, num_sessions=4)

    # Stub everything downstream of the synthesizer so the workflow short-
    # circuits cleanly after step 4.
    with (
        patch.object(tosGPS, "_download_fresh_reference") as fresh,
        patch.object(tosGPS, "_create_versioned_backup"),
        patch.object(tosGPS, "_create_working_copy") as work,
        patch.object(
            tosGPS, "_generate_station_lines_from_tos", return_value=["fake-line"]
        ),
        patch.object(tosGPS, "_apply_station_updates", return_value={"success": True}),
        patch.object(
            tosGPS,
            "_verify_intended_changes",
            return_value={"success": True, "change_summary": {}},
        ),
        patch.object(tosGPS, "_generate_change_report", return_value=""),
        patch.object(
            tosGPS, "_safe_upload_with_rollback", return_value={"success": True}
        ),
        patch.object(tosGPS, "_cleanup_working_files"),
    ):
        fresh.return_value = {
            "success": True,
            "temp_path": "/tmp/fake",
            "file_size": 1,
            "changed": True,
        }
        work.return_value = {"work_path": "/tmp/fake_work"}

        result = tosGPS._safe_update_workflow(
            stations=["SAVI"],
            metadata_type="gamit-station-info",
            url="https://example.invalid/tos/v1",
            update_mode=True,
            dry_run=True,
            interactive=False,
            backup_required=False,
            synthesizer=fake_new_chain,
        )

    assert captured == [("new", "SAVI")], (
        "workflow must call the synthesizer it was handed, "
        f"not the legacy default. Got: {captured}"
    )
    assert "SAVI" in result["stations_processed"]


def test_safe_update_workflow_defaults_to_new_chain_when_synthesizer_omitted():
    """Default synthesizer is ``gps_metadata_via_devices``."""
    from tostools import tosGPS

    captured: list[str] = []

    def spy(station, url, loglevel=logging.WARNING):
        captured.append(station)
        return {"marker": station}

    with (
        patch.object(gpsqc, "gps_metadata_via_devices", side_effect=spy),
        patch.object(tosGPS, "_download_fresh_reference") as fresh,
        patch.object(tosGPS, "_create_versioned_backup"),
        patch.object(tosGPS, "_create_working_copy") as work,
        patch.object(
            tosGPS, "_generate_station_lines_from_tos", return_value=["fake-line"]
        ),
        patch.object(tosGPS, "_apply_station_updates", return_value={"success": True}),
        patch.object(
            tosGPS,
            "_verify_intended_changes",
            return_value={"success": True, "change_summary": {}},
        ),
        patch.object(tosGPS, "_generate_change_report", return_value=""),
        patch.object(
            tosGPS, "_safe_upload_with_rollback", return_value={"success": True}
        ),
        patch.object(tosGPS, "_cleanup_working_files"),
    ):
        fresh.return_value = {
            "success": True,
            "temp_path": "/tmp/fake",
            "file_size": 1,
            "changed": True,
        }
        work.return_value = {"work_path": "/tmp/fake_work"}

        tosGPS._safe_update_workflow(
            stations=["SAVI"],
            metadata_type="gamit-station-info",
            url="https://example.invalid/tos/v1",
            update_mode=True,
            dry_run=True,
            interactive=False,
            backup_required=False,
        )

    assert captured == ["SAVI"], (
        "default synthesizer must be gps_metadata_via_devices; "
        f"got captured={captured}"
    )


def test_safe_update_refreshes_local_cached_reference_after_upload(tmp_path):
    """After a successful (non-dry-run) upload, the local cached station.info
    must be refreshed from the just-uploaded working copy.

    Otherwise the next no-update ``tosGPS syncMeta`` run reads the stale
    pre-upload content and shows phantom diffs against TOS, making it look
    like the update silently failed when it actually succeeded.
    """
    from tostools import tosGPS

    # Set up a fake station_config_dir layout: an existing local reference
    # (stale content) and a working copy (post-upload content).
    fake_station_config_dir = tmp_path / "gpsconfig"
    fake_station_config_dir.mkdir()
    local_reference = fake_station_config_dir / "station.info.sopac.apr05"
    local_reference.write_text("STALE pre-upload content\n", encoding="utf-8")

    work_path = tmp_path / "work_copy.txt"
    work_path.write_text("FRESH uploaded content\n", encoding="utf-8")

    def fake_synth(station, url, loglevel=logging.WARNING):
        return {"marker": station}

    with (
        patch.object(
            tosGPS, "_get_station_config_dir", return_value=fake_station_config_dir
        ),
        patch.object(tosGPS, "_download_fresh_reference") as fresh,
        patch.object(
            tosGPS, "_create_versioned_backup", return_value={"backup_id": "fake"}
        ),
        patch.object(tosGPS, "_create_working_copy") as work,
        patch.object(
            tosGPS, "_generate_station_lines_from_tos", return_value=["fake-line"]
        ),
        patch.object(tosGPS, "_apply_station_updates", return_value={"success": True}),
        patch.object(
            tosGPS,
            "_verify_intended_changes",
            return_value={"success": True, "change_summary": {}},
        ),
        patch.object(tosGPS, "_generate_change_report", return_value=""),
        patch.object(
            tosGPS, "_safe_upload_with_rollback", return_value={"success": True}
        ),
        patch.object(tosGPS, "_cleanup_working_files"),
    ):
        fresh.return_value = {
            "success": True,
            "temp_path": str(tmp_path / "fresh"),
            "file_size": 1,
            "changed": True,
        }
        work.return_value = {"work_path": str(work_path)}

        result = tosGPS._safe_update_workflow(
            stations=["SAVI"],
            metadata_type="gamit-station-info",
            url="https://example.invalid/tos/v1",
            update_mode=True,
            dry_run=False,  # full upload path
            interactive=False,
            backup_required=False,
            synthesizer=fake_synth,
        )

    assert result["success"], f"workflow should succeed; errors={result['errors']}"
    assert local_reference.read_text(encoding="utf-8") == "FRESH uploaded content\n", (
        "local cached reference must be refreshed from the working copy "
        "after a successful upload"
    )


def test_safe_update_dry_run_does_not_refresh_local_cache(tmp_path):
    """Dry-run mode must NOT touch the local cached reference."""
    from tostools import tosGPS

    fake_station_config_dir = tmp_path / "gpsconfig"
    fake_station_config_dir.mkdir()
    local_reference = fake_station_config_dir / "station.info.sopac.apr05"
    local_reference.write_text("ORIGINAL content\n", encoding="utf-8")

    work_path = tmp_path / "work_copy.txt"
    work_path.write_text("would-be-uploaded content\n", encoding="utf-8")

    def fake_synth(station, url, loglevel=logging.WARNING):
        return {"marker": station}

    with (
        patch.object(
            tosGPS, "_get_station_config_dir", return_value=fake_station_config_dir
        ),
        patch.object(tosGPS, "_download_fresh_reference") as fresh,
        patch.object(
            tosGPS, "_create_versioned_backup", return_value={"backup_id": "fake"}
        ),
        patch.object(tosGPS, "_create_working_copy") as work,
        patch.object(
            tosGPS, "_generate_station_lines_from_tos", return_value=["fake-line"]
        ),
        patch.object(tosGPS, "_apply_station_updates", return_value={"success": True}),
        patch.object(
            tosGPS,
            "_verify_intended_changes",
            return_value={"success": True, "change_summary": {}},
        ),
        patch.object(tosGPS, "_generate_change_report", return_value=""),
        patch.object(
            tosGPS, "_safe_upload_with_rollback", return_value={"success": True}
        ),
        patch.object(tosGPS, "_cleanup_working_files"),
    ):
        fresh.return_value = {
            "success": True,
            "temp_path": str(tmp_path / "fresh"),
            "file_size": 1,
            "changed": True,
        }
        work.return_value = {"work_path": str(work_path)}

        tosGPS._safe_update_workflow(
            stations=["SAVI"],
            metadata_type="gamit-station-info",
            url="https://example.invalid/tos/v1",
            update_mode=True,
            dry_run=True,
            interactive=False,
            backup_required=False,
            synthesizer=fake_synth,
        )

    assert (
        local_reference.read_text(encoding="utf-8") == "ORIGINAL content\n"
    ), "dry-run must not overwrite the local cached reference"


def test_process_single_station_invokes_passed_synthesizer():
    """``_process_single_station`` must use the synthesizer it was given."""
    from tostools import tosGPS

    captured: list[str] = []

    def fake_synth(station, url, loglevel=logging.WARNING):
        captured.append(station)
        return {"marker": station}

    with (
        patch.object(tosGPS.gpsf, "print_station_info", return_value=[]),
        patch.object(tosGPS, "_lines_are_identical", return_value=True),
    ):
        tosGPS._process_single_station(
            station="SAVI",
            metadata_type="gamit-station-info",
            reference_data={"SAVI": ["existing-line"]},
            url="https://example.invalid/tos/v1",
            log_level=SimpleNamespace(value=logging.CRITICAL),
            update_mode=False,
            show_comparison=False,
            backup=False,
            synthesizer=fake_synth,
        )

    assert captured == [
        "SAVI"
    ], "_process_single_station must call the synthesizer it was handed"
