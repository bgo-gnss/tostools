"""Tests for tostools.utils.archive (migrated from receivers)."""

import gzip
from pathlib import Path

import pytest

from tostools.utils.archive import (
    ArchiveLocation,
    ArchiveValidator,
    GzipValidator,
)


@pytest.fixture
def tmp_gzip_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.txt.gz"
    with gzip.open(path, "wb") as f:
        f.write(b"x" * 4096)
    return path


@pytest.fixture
def tmp_broken_gzip(tmp_path: Path) -> Path:
    path = tmp_path / "broken.txt.gz"
    path.write_bytes(b"\x1f\x8b" + b"garbage" * 500)  # valid magic, bad body
    return path


class TestGzipValidator:
    def test_recognizes_valid_gzip(self, tmp_gzip_file):
        assert GzipValidator().validate_magic_bytes(tmp_gzip_file) is True

    def test_rejects_plain_file(self, tmp_path):
        plain = tmp_path / "plain.txt"
        plain.write_text("hello")
        assert GzipValidator().validate_magic_bytes(plain) is False

    def test_returns_dot_gz_extension(self):
        assert GzipValidator().get_extension() == ".gz"


class TestArchiveValidator:
    def test_valid_gz_passes(self, tmp_gzip_file):
        v = ArchiveValidator(min_file_size=1)
        assert v.validate_archived_file(tmp_gzip_file) is True

    def test_below_min_size_fails(self, tmp_gzip_file):
        v = ArchiveValidator(min_file_size=10_000_000)
        assert v.validate_archived_file(tmp_gzip_file) is False

    def test_missing_file_fails(self, tmp_path):
        v = ArchiveValidator(min_file_size=1)
        assert v.validate_archived_file(tmp_path / "nope.gz") is False

    def test_magic_byte_mismatch_fails(self, tmp_path):
        fake = tmp_path / "fake.gz"
        fake.write_bytes(b"not a real gzip body")
        v = ArchiveValidator(min_file_size=1)
        assert v.validate_archived_file(fake) is False

    def test_tmp_integrity_detects_corruption(self, tmp_broken_gzip):
        v = ArchiveValidator(min_file_size=1)
        assert v._validate_tmp_file_integrity(tmp_broken_gzip) is False

    def test_tmp_integrity_passes_valid_file(self, tmp_gzip_file):
        v = ArchiveValidator(min_file_size=1)
        assert v._validate_tmp_file_integrity(tmp_gzip_file) is True

    def test_find_existing_archive_hits_archive(self, tmp_gzip_file, tmp_path):
        v = ArchiveValidator(min_file_size=1)
        found, path, location = v.find_existing_archive(
            tmp_gzip_file.name, str(tmp_gzip_file).replace(".gz", "")
        )
        assert found is True
        assert location == ArchiveLocation.ARCHIVE_COMPRESSED
        assert path == tmp_gzip_file

    def test_find_existing_archive_hits_tmp(self, tmp_gzip_file, tmp_path):
        # Move into a "tmp" directory, no archive
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        moved = tmp_dir / tmp_gzip_file.name
        tmp_gzip_file.rename(moved)

        v = ArchiveValidator(min_file_size=1)
        found, path, location = v.find_existing_archive(
            moved.name, str(tmp_path / "archive_that_doesnt_exist"), tmp_dir=tmp_dir
        )
        assert found is True
        assert location == ArchiveLocation.TMP
        assert path == moved

    def test_find_existing_archive_not_found(self, tmp_path):
        v = ArchiveValidator(min_file_size=1)
        found, path, location = v.find_existing_archive(
            "nope.gz", str(tmp_path / "nowhere")
        )
        assert found is False
        assert path is None
        assert location == ArchiveLocation.NOT_FOUND

    def test_batch_validate_separates_missing_and_found(self, tmp_gzip_file, tmp_path):
        v = ArchiveValidator(min_file_size=1)
        files = {tmp_gzip_file.name: "/remote/a", "missing.gz": "/remote/b"}
        archive = {
            tmp_gzip_file.name: str(tmp_gzip_file).replace(".gz", ""),
            "missing.gz": str(tmp_path / "nowhere"),
        }
        missing, found_count, _total, tmp = v.batch_validate_archives(files, archive)
        assert "missing.gz" in missing
        assert tmp_gzip_file.name not in missing
        assert found_count == 1
        assert tmp == {}

    def test_detailed_report_structure(self, tmp_gzip_file):
        v = ArchiveValidator(min_file_size=1)
        report = v.validate_with_detailed_report(tmp_gzip_file)
        assert report["valid"] is True
        assert report["file_exists"] is True
        assert report["compression_format"] == ".gz"
        assert report["compression_valid"] is True
        assert report["errors"] == []

    def test_set_min_file_size_updates(self):
        v = ArchiveValidator(min_file_size=100)
        v.set_min_file_size(2048)
        assert v.min_file_size == 2048
