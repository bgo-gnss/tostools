"""Archive file validation and discovery utilities.

Domain-generic archive integrity helpers: gzip/zip magic-byte validation,
full-read integrity checks for partial-download detection, size thresholds,
and multi-location archive discovery. Originally extracted from the
``receivers`` package (``receivers.utils.archive_validator``) because the
logic is not GPS-specific — any pipeline that writes compressed files to a
"permanent archive" + "temp staging" layout can use it.

Design:
- Plugin architecture for compression format validators
  (:class:`CompressionValidator` protocol). New formats (bz2, xz, zstd) can
  register without touching :class:`ArchiveValidator`.
- Configurable minimum file size threshold.
- :meth:`ArchiveValidator.find_existing_archive` handles the common pattern
  of "check archive location first, then compressed variant, then temp
  staging".
"""

import gzip
import logging
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple


class ArchiveLocation(Enum):
    """Where an archive file was found by
    :meth:`ArchiveValidator.find_existing_archive`."""

    ARCHIVE = "archive"
    ARCHIVE_COMPRESSED = "archive_compressed"
    TMP = "tmp"
    NOT_FOUND = "not_found"


class CompressionValidator(Protocol):
    """Protocol for compression-format magic-byte validators."""

    def validate_magic_bytes(self, file_path: Path) -> bool: ...

    def get_extension(self) -> str: ...


class GzipValidator:
    """Gzip magic-byte validator."""

    MAGIC_BYTES = b"\x1f\x8b"

    def validate_magic_bytes(self, file_path: Path) -> bool:
        try:
            with open(file_path, "rb") as f:
                return f.read(2) == self.MAGIC_BYTES
        except (OSError, IOError):
            return False

    def get_extension(self) -> str:
        return ".gz"


class ArchiveValidator:
    """Unified archive file validation and multi-location discovery."""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        min_file_size: int = 1024,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.min_file_size = min_file_size
        self._compression_validators: Dict[str, CompressionValidator] = {
            ".gz": GzipValidator(),
        }

    def register_compression_validator(
        self, extension: str, validator: CompressionValidator
    ) -> None:
        self._compression_validators[extension] = validator
        self.logger.debug(f"Registered compression validator for {extension}")

    def validate_archived_file(self, file_path: Path) -> bool:
        """Size + magic-byte sanity check. Does not decompress."""
        try:
            if not file_path.exists():
                self.logger.debug(f"File does not exist: {file_path}")
                return False

            file_size = file_path.stat().st_size
            if file_size < self.min_file_size:
                self.logger.debug(
                    "File too small (%s bytes, minimum %s): %s",
                    file_size,
                    self.min_file_size,
                    file_path,
                )
                return False

            file_extension = "".join(file_path.suffixes[-1:])
            if file_extension in self._compression_validators:
                validator = self._compression_validators[file_extension]
                if not validator.validate_magic_bytes(file_path):
                    self.logger.debug(
                        "File doesn't have valid %s magic header: %s",
                        file_extension,
                        file_path,
                    )
                    return False

            return True

        except (OSError, IOError) as e:
            self.logger.debug(f"Error validating archived file {file_path}: {e}")
            return False

    def _validate_tmp_file_integrity(self, file_path: Path) -> bool:
        """Full-read integrity check for tmp/partial files."""
        try:
            if not file_path.exists():
                return False

            file_size = file_path.stat().st_size
            if file_size < self.min_file_size:
                self.logger.debug(
                    f"Tmp file too small ({file_size} bytes): {file_path.name}"
                )
                return False

            file_extension = "".join(file_path.suffixes[-1:])
            if file_extension == ".gz":
                try:
                    with gzip.open(file_path, "rb") as gz_file:
                        while True:
                            chunk = gz_file.read(1024 * 1024)
                            if not chunk:
                                break
                    self.logger.debug(
                        f"Tmp file gzip integrity verified: {file_path.name}"
                    )
                    return True
                except (OSError, gzip.BadGzipFile) as e:
                    self.logger.debug(
                        "Tmp file gzip integrity FAILED (partial?): %s - %s",
                        file_path.name,
                        e,
                    )
                    return False
            else:
                return self.validate_archived_file(file_path)

        except Exception as e:
            self.logger.debug(f"Error validating tmp file {file_path}: {e}")
            return False

    def find_existing_archive(
        self,
        filename: str,
        archive_path: str,
        tmp_dir: Optional[Path] = None,
    ) -> Tuple[bool, Optional[Path], ArchiveLocation]:
        archive_path_obj = Path(archive_path)

        if archive_path_obj.exists():
            if self.validate_archived_file(archive_path_obj):
                return True, archive_path_obj, ArchiveLocation.ARCHIVE
            else:
                size = archive_path_obj.stat().st_size
                self.logger.warning(
                    f"Corrupt archive ({size:,} bytes), will re-download: {archive_path_obj}"
                )

        for ext, _validator in self._compression_validators.items():
            compressed_path = Path(str(archive_path) + ext)
            if compressed_path.exists():
                if self.validate_archived_file(compressed_path):
                    return True, compressed_path, ArchiveLocation.ARCHIVE_COMPRESSED
                else:
                    size = compressed_path.stat().st_size
                    self.logger.warning(
                        f"Corrupt compressed archive ({size:,} bytes), will re-download: {compressed_path}"
                    )

        if tmp_dir:
            tmp_file_path = tmp_dir / filename
            if tmp_file_path.exists():
                if self._validate_tmp_file_integrity(tmp_file_path):
                    return True, tmp_file_path, ArchiveLocation.TMP
                else:
                    self.logger.debug(
                        f"Tmp file exists but incomplete/invalid: {tmp_file_path}"
                    )

        return False, None, ArchiveLocation.NOT_FOUND

    def batch_validate_archives(
        self,
        files_dict: Dict[str, str],
        archive_files_dict: Dict[str, str],
        tmp_dir: Optional[Path] = None,
    ) -> Tuple[Dict[str, str], int, int, Dict[str, Path]]:
        missing_files_dict: Dict[str, str] = {}
        files_in_tmp_dict: Dict[str, Path] = {}
        files_found_in_archive = 0
        validated_files = 0

        for filename, remote_dir in files_dict.items():
            validated_files += 1
            archive_path = archive_files_dict.get(filename)

            if archive_path:
                found, path, location = self.find_existing_archive(
                    filename, archive_path, tmp_dir
                )
                if found:
                    if location == ArchiveLocation.TMP:
                        files_in_tmp_dict[filename] = path  # type: ignore[assignment]
                    else:
                        files_found_in_archive += 1
                    continue

            missing_files_dict[filename] = remote_dir

        if files_in_tmp_dict:
            self.logger.info(
                "Found %s files in tmp directory that need archiving",
                len(files_in_tmp_dict),
            )

        return (
            missing_files_dict,
            files_found_in_archive,
            validated_files,
            files_in_tmp_dict,
        )

    def get_compression_extensions(self) -> List[str]:
        return list(self._compression_validators.keys())

    def set_min_file_size(self, min_size: int) -> None:
        self.min_file_size = min_size
        self.logger.debug(f"Updated minimum file size to {min_size} bytes")

    def validate_with_detailed_report(self, file_path: Path) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "valid": False,
            "file_exists": False,
            "file_size": 0,
            "meets_min_size": False,
            "compression_format": None,
            "compression_valid": None,
            "errors": [],
        }

        if not file_path.exists():
            report["errors"].append(f"File does not exist: {file_path}")
            return report

        report["file_exists"] = True

        try:
            file_size = file_path.stat().st_size
            report["file_size"] = file_size
            report["meets_min_size"] = file_size >= self.min_file_size
            if not report["meets_min_size"]:
                report["errors"].append(
                    f"File size {file_size} bytes < minimum {self.min_file_size} bytes"
                )
        except (OSError, IOError) as e:
            report["errors"].append(f"Error reading file size: {e}")
            return report

        file_extension = "".join(file_path.suffixes[-1:])
        if file_extension in self._compression_validators:
            report["compression_format"] = file_extension
            validator = self._compression_validators[file_extension]
            report["compression_valid"] = validator.validate_magic_bytes(file_path)
            if not report["compression_valid"]:
                report["errors"].append(f"Invalid {file_extension} compression format")

        report["valid"] = len(report["errors"]) == 0
        return report
