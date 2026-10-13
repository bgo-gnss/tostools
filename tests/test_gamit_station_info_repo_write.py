"""Tests for writing station.info into the gps-config-data repo (`--write-repo`).

The refusals matter more than the happy path. This function is the ONE writer of a
file that drives GAMIT PCV and heights for the whole network, and its failure modes
are quiet:

* a clone that is not even with origin must be refused, or the host puller's
  fast-forward pull breaks and config sync silently stalls network-wide;
* a real write without ``--push`` must be refused, because the local-only commit
  it leaves behind is exactly what breaks that pull;
* identical content must not create an empty commit (or every 15-minute run
  would manufacture history);
* a dry run must not touch the working tree at all.

The cutover guard is the other half: the legacy direct scp write to the GAMIT host
is refused by default, because a host-side puller would revert it on its next tick.

Every test drives real git in a tmp_path bare-origin + clone, so the git plumbing
(upstream resolution, push) is exercised rather than mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tostools.tosGPS import (
    REPO_STATION_INFO_REL,
    _describe_station_info_files,
    _generate_sync_summary,
    _reference_from_repo,
    _resolve_config_data_repo,
    _safe_upload_with_rollback,
    _write_station_info_to_repo,
)

GIT_ID = ["-c", "user.email=test@example.invalid", "-c", "user.name=test"]


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_ID, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _make_clone(tmp_path: Path, content: str = " SKDA  seed\n") -> tuple[Path, Path]:
    """Bare origin + a clone tracking it, with one commit on the branch.

    Also pins a repo-local git identity: the production function does not pass
    -c user.*, so a clone without identity would fail to commit — which is
    correct behaviour, and not what these tests are about.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")

    dest = repo / REPO_STATION_INFO_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "push", "-q", "-u", "origin", "HEAD")
    return repo, dest


def _work_file(tmp_path: Path, content: str) -> Path:
    work = tmp_path / "work" / "station.info.sopac.apr05"
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_text(content)
    return work


# ---------------------------------------------------------------------------
# refusals — the paths that protect the repo
# ---------------------------------------------------------------------------


def test_refuses_when_not_a_git_clone(tmp_path):
    """A misconfigured --repo-path must fail loudly, not write somewhere random."""
    work = _work_file(tmp_path, " NEW\n")
    result = _write_station_info_to_repo(
        work, ["skda"], repo_path=str(tmp_path / "not-a-repo"), push=True
    )
    assert result["success"] is False
    assert "not a git clone" in result["error"]


def test_refuses_when_clone_is_ahead_of_origin(tmp_path):
    """An ahead-of-origin clone is exactly what breaks the host puller."""
    repo, dest = _make_clone(tmp_path)
    # a stray local commit makes the clone ahead of origin
    (repo / "stray.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "stray")

    work = _work_file(tmp_path, " CHANGED\n")
    result = _write_station_info_to_repo(work, ["SKDA"], repo_path=str(repo), push=True)

    assert result["success"] is False
    assert "not even with origin" in result["error"]
    assert "ahead 1" in result["error"]
    # and critically: nothing was written
    assert dest.read_text() == " SKDA  seed\n"


def test_refuses_when_clone_is_behind_origin(tmp_path):
    """Behind is the other divergence; also a human's problem, not ours to paper over."""
    repo, dest = _make_clone(tmp_path)
    # advance origin from a second clone
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.invalid")
    _git(other, "config", "user.name", "test")
    (other / "upstream_change.txt").write_text("y\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "upstream")
    _git(other, "push", "-q", "origin", "HEAD")

    work = _work_file(tmp_path, " CHANGED\n")
    result = _write_station_info_to_repo(work, ["SKDA"], repo_path=str(repo), push=True)

    assert result["success"] is False
    assert "not even with origin" in result["error"]
    assert dest.read_text() == " SKDA  seed\n"


def test_refuses_real_write_without_push(tmp_path):
    """A local-only commit leaves the clone ahead — refuse rather than half-do it."""
    repo, dest = _make_clone(tmp_path)
    # same marker set as the repo: this test is about --push, not about content
    work = _work_file(tmp_path, " SKDA  changed\n")

    result = _write_station_info_to_repo(work, ["SKDA"], repo_path=str(repo), push=False)

    assert result["success"] is False
    assert "--push" in result["error"]
    assert dest.read_text() == " SKDA  seed\n", "must not write before the push check"


# ---------------------------------------------------------------------------
# idempotence and dry run
# ---------------------------------------------------------------------------


def test_identical_content_is_a_no_op(tmp_path):
    """Must not manufacture an empty commit on every scheduled run."""
    repo, dest = _make_clone(tmp_path, content=" SAME\n")
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    work = _work_file(tmp_path, " SAME\n")

    result = _write_station_info_to_repo(work, ["SKDA"], repo_path=str(repo), push=True)

    assert result["success"] is True
    assert result["changed"] is False
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before, "no new commit"


def test_dry_run_writes_nothing(tmp_path):
    repo, dest = _make_clone(tmp_path)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    work = _work_file(tmp_path, " SKDA  changed\n")

    result = _write_station_info_to_repo(
        work, ["SKDA"], repo_path=str(repo), push=True, dry_run=True
    )

    assert result["success"] is True and result["changed"] is True
    assert dest.read_text() == " SKDA  seed\n", "dry run must not touch the file"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# the real write
# ---------------------------------------------------------------------------


def test_writes_commits_and_pushes(tmp_path):
    repo, dest = _make_clone(tmp_path)
    work = _work_file(tmp_path, " SKDA  new\n")

    result = _write_station_info_to_repo(work, ["skda"], repo_path=str(repo), push=True)

    assert result["success"] is True, result.get("error")
    assert result["changed"] is True
    assert result["commit"]
    assert dest.read_text() == " SKDA  new\n"
    # committed, not merely written
    assert "station.info.sopac.apr05" in _git(repo, "show", "--stat", "--oneline", "HEAD").stdout
    # and actually pushed: origin now matches
    origin_head = subprocess.run(
        ["git", "-C", str(tmp_path / "origin.git"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert origin_head == _git(repo, "rev-parse", "HEAD").stdout.strip()
    # clone is left even with origin, so the host puller still fast-forwards
    assert _git(repo, "rev-parse", "HEAD").stdout == _git(repo, "rev-parse", "@{u}").stdout


def test_commit_message_names_the_stations(tmp_path):
    repo, _ = _make_clone(tmp_path)
    work = _work_file(tmp_path, " SKDA  new\n")
    _write_station_info_to_repo(work, ["skda"], repo_path=str(repo), push=True)
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert "SKDA" in body, "the commit must say which stations changed"


# ---------------------------------------------------------------------------
# repo resolution precedence
# ---------------------------------------------------------------------------


def test_repo_resolution_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("GPS_CONFIG_DATA_REPO", str(tmp_path / "from-env"))
    assert _resolve_config_data_repo() == tmp_path / "from-env"
    assert _resolve_config_data_repo(str(tmp_path / "from-arg")) == tmp_path / "from-arg"

    monkeypatch.delenv("GPS_CONFIG_DATA_REPO")
    assert _resolve_config_data_repo().name, "falls back to the default clone"


# ---------------------------------------------------------------------------
# the cutover guard: the legacy host write is closed by default
# ---------------------------------------------------------------------------


def test_direct_host_write_is_refused_by_default(tmp_path):
    """With a host puller live, a direct write is reverted by the next pull.

    That failure is silent, so the guard must be a hard stop rather than a warning.
    """
    work = _work_file(tmp_path, " X\n")
    result = _safe_upload_with_rollback(work, {}, {}, metadata_type="gamit-station-info")
    assert result["success"] is False
    assert result.get("error_kind") == "host_write_disabled"
    assert "--write-repo" in result["error"], "the refusal must say what to do instead"


def test_direct_host_write_refused_even_in_dry_run(tmp_path):
    """A dry run must not imply the write is available."""
    work = _work_file(tmp_path, " X\n")
    result = _safe_upload_with_rollback(
        work, {}, {}, metadata_type="gamit-station-info", dry_run=True
    )
    assert result["success"] is False
    assert result.get("error_kind") == "host_write_disabled"


def test_guard_is_bypassed_only_explicitly(tmp_path):
    """--allow-host-write restores the legacy path for a deliberate recovery."""
    work = _work_file(tmp_path, " X\n")
    result = _safe_upload_with_rollback(
        work, {}, {}, metadata_type="gamit-station-info", dry_run=True,
        allow_host_write=True,
    )
    # reaches the simulated upload rather than the guard
    assert result.get("error_kind") != "host_write_disabled"


# ---------------------------------------------------------------------------
# the reported target must follow --write-repo
# ---------------------------------------------------------------------------


def _target_line(lines) -> str:
    return next(ln for ln in lines if ln.strip().startswith("target"))


def test_file_report_names_the_repo_under_write_repo(tmp_path):
    """Regression: the report named okada even when writing to the repo.

    That is the exact confusion the reporting was added to remove — the output
    pointed at a file the run would never touch.
    """
    lines = _describe_station_info_files(
        writing=True, write_repo=True, repo_path=str(tmp_path)
    )
    target = _target_line(lines)
    assert str(tmp_path) in target
    assert REPO_STATION_INFO_REL in target
    assert "okada" not in target, "must not name a host that is not the target"
    assert "this is the file that changes" in target


def test_file_report_names_the_host_without_write_repo():
    target = _target_line(_describe_station_info_files(writing=True))
    assert "okada" in target
    assert REPO_STATION_INFO_REL not in target
    assert "this is the file that changes" in target


def test_file_report_says_nothing_changes_without_update():
    """A read-only run must not read as if it would write."""
    target = _target_line(_describe_station_info_files(writing=False))
    assert "--update only" in target
    assert "this is the file that changes" not in target


def test_summary_file_line_follows_the_target(tmp_path, capsys):
    results = {
        "gamit-station-info": {
            "success": True,
            "stations_processed": 1,
            "stations_updated": 0,
        }
    }
    _generate_sync_summary(
        results, ["gamit-station-info"], ["SKDA"],
        write_repo=True, repo_path=str(tmp_path),
    )
    err = capsys.readouterr().err
    assert str(tmp_path) in err
    assert "okada" not in err


def test_summary_reports_errors_without_stations_failed(capsys):
    """Regression: gating on 'stations_failed' discarded every workflow error.

    The safe-update workflow populates 'errors' but never 'stations_failed', so a
    refused or failed write exited 1 with no visible reason — which is how a
    closed write path stays silent.
    """
    results = {
        "gamit-station-info": {
            "success": False,
            "errors": ["direct host write is disabled (cutover)"],
            "stations_processed": 1,
            "stations_updated": 0,
        }
    }
    _generate_sync_summary(results, ["gamit-station-info"], ["SKDA"])
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "direct host write is disabled" in err


def test_summary_is_quiet_when_everything_succeeded(capsys):
    """The fix must not turn every successful run into a warning."""
    results = {
        "gamit-station-info": {
            "success": True,
            "errors": [],
            "stations_processed": 1,
            "stations_updated": 1,
        }
    }
    _generate_sync_summary(results, ["gamit-station-info"], ["SKDA"])
    err = capsys.readouterr().err
    assert "FAILED" not in err


# ---------------------------------------------------------------------------
# read and write must point at the SAME file in --write-repo mode
# ---------------------------------------------------------------------------


def test_reference_comes_from_the_repo(tmp_path):
    """In --write-repo the reference must be the repo file, not the GAMIT host.

    Reading okada while writing the repo derives the result from a different file
    than the one being maintained.
    """
    repo, _ = _make_clone(tmp_path, content=" SKDA  repo-side\n")
    src = _reference_from_repo("gamit-station-info", str(repo), tmp_path / "cache")
    assert src["success"] is True, src.get("error")
    assert src["metadata"]["source_host"] == "gps-config-data"
    assert Path(src["temp_path"]).read_text() == " SKDA  repo-side\n"
    assert src["file_size"] == len(" SKDA  repo-side\n")


def test_reference_fails_cleanly_without_a_repo_file(tmp_path):
    repo, dest = _make_clone(tmp_path)
    dest.unlink()
    src = _reference_from_repo("gamit-station-info", str(repo), tmp_path / "cache")
    assert src["success"] is False
    assert "no station.info in the repo" in src["error"]


# ---------------------------------------------------------------------------
# the marker-loss guard: a merge file and a host file differ invisibly
# ---------------------------------------------------------------------------


def test_refuses_to_drop_markers_present_in_the_repo(tmp_path):
    """The regression this exists for: a host-derived candidate drops the other half.

    Measured on the real files: 92 markers / 494 occupations would have vanished.
    """
    repo, dest = _make_clone(tmp_path, content=" AAAA  one\n BBBB  two\n")
    work = _work_file(tmp_path, " AAAA  one\n")  # BBBB absent

    result = _write_station_info_to_repo(work, ["SKDA"], repo_path=str(repo), push=True)

    assert result["success"] is False
    assert "would disappear" in result["error"]
    assert result["lost_markers"] == ["BBBB"]
    assert dest.read_text() == " AAAA  one\n BBBB  two\n", "must not write"


def test_marker_guard_also_fires_on_a_dry_run(tmp_path):
    """A dry run must surface the refusal, not report a clean write."""
    repo, dest = _make_clone(tmp_path, content=" AAAA  one\n BBBB  two\n")
    work = _work_file(tmp_path, " AAAA  one\n")
    result = _write_station_info_to_repo(
        work, ["SKDA"], repo_path=str(repo), push=True, dry_run=True
    )
    assert result["success"] is False
    assert result["lost_markers"] == ["BBBB"]
    assert dest.read_text() == " AAAA  one\n BBBB  two\n"


def test_gaining_markers_is_allowed(tmp_path):
    """The guard is one-directional: new markers are normal, lost ones are not."""
    repo, dest = _make_clone(tmp_path, content=" AAAA  one\n")
    work = _work_file(tmp_path, " AAAA  one\n BBBB  new\n")
    result = _write_station_info_to_repo(work, ["SKDA"], repo_path=str(repo), push=True)
    assert result["success"] is True, result.get("error")
    assert "BBBB" in dest.read_text()
