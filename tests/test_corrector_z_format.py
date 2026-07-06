"""Regression tests: corrector .Z output must be genuine LZW compress(1).

The archive's ``.Z`` convention is Unix compress LZW (magic ``1f 9d``).
receivers' converter briefly wrote gzip bytes under ``.Z`` names (the
gzip-as-.Z incident, fixed 2026-07-06); the corrector must never do the
same when rewriting a header in place.
"""

import gzip
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from tostools.rinex.corrector import _write_rinex_file

HAS_COMPRESS = shutil.which("compress") is not None

LZW_MAGIC = b"\x1f\x9d"
GZIP_MAGIC = b"\x1f\x8b"

HEADER = "NEW HEADER LINE" + " " * 45 + "END OF HEADER\n"
DATA = b"> 2026 07 06 00 00 00.0000000\ndata section bytes\n"


def _make_z_input(path: Path) -> None:
    """A gzip-content .Z input (what the pre-fix archive is full of) —
    the corrector's zcat read path handles both formats."""
    with gzip.open(path, "wb") as fh:
        fh.write(b"OLD HEADER" + b" " * 50 + b"END OF HEADER\n" + DATA)


@pytest.mark.skipif(not HAS_COMPRESS, reason="compress(1) not installed")
def test_z_output_is_lzw_not_gzip(tmp_path):
    src = tmp_path / "TEST0010.26D.Z"
    _make_z_input(src)
    out = tmp_path / "TEST0010.26D.Z"

    _write_rinex_file(src, HEADER, out, logging.getLogger("test"))

    magic = out.read_bytes()[:2]
    assert magic == LZW_MAGIC, f"corrector wrote {magic.hex()} — must be LZW 1f9d"
    assert magic != GZIP_MAGIC

    # round-trip: header replaced, data section intact
    back = subprocess.run(["zcat", str(out)], capture_output=True, check=True).stdout
    assert back.startswith(HEADER.encode())
    assert back.endswith(DATA)


def test_gz_output_stays_gzip(tmp_path):
    src = tmp_path / "TEST0010.26D.gz"
    with gzip.open(src, "wb") as fh:
        fh.write(b"OLD HEADER" + b" " * 50 + b"END OF HEADER\n" + DATA)
    out = tmp_path / "TEST0010.26D.gz"

    _write_rinex_file(src, HEADER, out, logging.getLogger("test"))

    assert out.read_bytes()[:2] == GZIP_MAGIC
    with gzip.open(out, "rb") as fh:
        back = fh.read()
    assert back.startswith(HEADER.encode())
    assert back.endswith(DATA)
