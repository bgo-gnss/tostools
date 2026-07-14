"""
RINEX file processing modules.

This package contains modules for reading, validating, and editing RINEX files,
as well as comparing RINEX data with TOS metadata.
"""

from .corrector import correct_rinex_from_tos
from .domes import domes_or_skip, is_iers_domes
from .reader import get_rinex_labels, read_rinex_file, read_rinex_header

__all__ = [
    "correct_rinex_from_tos",
    "domes_or_skip",
    "is_iers_domes",
    "get_rinex_labels",
    "read_rinex_file",
    "read_rinex_header",
]
