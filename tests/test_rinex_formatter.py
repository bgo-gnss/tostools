"""Tests for tostools.rinex.formatter (migrated from receivers)."""

import pytest

from tostools.rinex.formatter import (
    RINEX_FIELD_SPECS,
    format_antenna_type_with_radome,
    format_rinex_field,
)


class TestFormatRinexField:
    def test_marker_name_uppercase_padded_to_60(self):
        result = format_rinex_field("MARKER NAME", "eldc")
        assert result is not None
        assert result.startswith("ELDC")
        assert len(result) == 60

    def test_marker_name_empty_returns_none(self):
        assert format_rinex_field("MARKER NAME", "") is None
        assert format_rinex_field("MARKER NAME", None) is None

    def test_marker_number_padded_to_20(self):
        result = format_rinex_field("MARKER NUMBER", "12345M001")
        assert result is not None
        assert result.startswith("12345M001")
        assert len(result) == 20

    def test_observer_agency_tuple(self):
        result = format_rinex_field("OBSERVER / AGENCY", ("BGO", "IMO"))
        assert result is not None
        assert result.startswith("BGO")
        assert "IMO" in result
        assert len(result) == 60

    def test_observer_agency_string_split(self):
        result = format_rinex_field("OBSERVER / AGENCY", "BGO IMO")
        assert result is not None
        assert result.startswith("BGO")
        assert "IMO" in result
        assert len(result) == 60

    def test_rec_type_vers_three_parts(self):
        result = format_rinex_field(
            "REC # / TYPE / VERS", ("1234567", "SEPT POLARX5", "5.4.0")
        )
        assert result is not None
        assert result.startswith("1234567")
        assert "SEPT POLARX5" in result
        assert "5.4.0" in result
        assert len(result) == 60

    def test_ant_type_with_radome(self):
        result = format_rinex_field(
            "ANT # / TYPE", ("CR6200", "ASH701945C_M    SCIS")
        )
        assert result is not None
        assert result.startswith("CR6200")
        assert "ASH701945C_M" in result
        assert len(result) == 40

    def test_antenna_delta_h_e_n_tuple(self):
        result = format_rinex_field("ANTENNA: DELTA H/E/N", (0.1234, 0.0, 0.0))
        assert result is not None
        assert len(result) == 42
        assert "0.1234" in result

    def test_antenna_delta_height_only(self):
        result = format_rinex_field("ANTENNA: DELTA H/E/N", 0.5)
        assert result is not None
        assert len(result) == 42
        assert "0.5000" in result

    def test_approx_position_xyz(self):
        result = format_rinex_field(
            "APPROX POSITION XYZ", (2470000.0, -1100000.0, 5800000.0)
        )
        assert result is not None
        assert len(result) == 42

    def test_interval_float(self):
        assert format_rinex_field("INTERVAL", 15.0) == "    15.000"

    def test_interval_invalid_returns_none(self):
        assert format_rinex_field("INTERVAL", "not-a-number") is None

    def test_unknown_field_passthrough(self):
        assert format_rinex_field("CUSTOM FIELD", "value") == "value"


class TestFormatAntennaTypeWithRadome:
    def test_basic(self):
        result = format_antenna_type_with_radome("ASH701945C_M", "SCIS")
        assert len(result) == 20
        assert result.startswith("ASH701945C_M")
        assert result.endswith("SCIS")

    def test_default_radome_is_none(self):
        result = format_antenna_type_with_radome("SEPPOLANT_X_MF")
        assert result.endswith("NONE")

    def test_long_model_truncated_to_15(self):
        result = format_antenna_type_with_radome("A" * 20, "DOME")
        assert result[:15] == "A" * 15
        assert result.endswith("DOME")
        assert len(result) == 20

    def test_long_radome_truncated_to_4(self):
        result = format_antenna_type_with_radome("ANT", "DOMEXYZ")
        assert result.endswith("DOME")
        assert len(result) == 20


class TestFieldSpecs:
    def test_marker_name_spec(self):
        fmt, width = RINEX_FIELD_SPECS["MARKER NAME"]
        assert fmt == "A60"
        assert width == 60

    def test_ant_type_spec(self):
        fmt, width = RINEX_FIELD_SPECS["ANT # / TYPE"]
        assert fmt == "A20,A20"
        assert width == 40
