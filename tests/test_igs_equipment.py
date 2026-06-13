"""Regression tests for IGS equipment-name lookups (igs_equipment)."""

from tostools.standards.igs_equipment import to_igs_antenna, to_igs_receiver


class TestMosaicX5:
    def test_mosaic_x5_uses_hyphenated_rcvr_ant_tab_name(self):
        # rcvr_ant.tab spells it "SEPT MOSAIC-X5" (hyphen), not "SEPT MOSAICX5".
        assert to_igs_receiver("mosaic-X5") == "SEPT MOSAIC-X5"

    def test_mosaic_x5_aliases(self):
        for alias in ("mosaicX5", "MOSAIC-X5", "MOSAICX5"):
            assert to_igs_receiver(alias) == "SEPT MOSAIC-X5"

    def test_canonical_mosaic_passthrough(self):
        assert to_igs_receiver("SEPT MOSAIC-X5") == "SEPT MOSAIC-X5"


class TestAntennaPassthrough:
    def test_listed_antenna(self):
        assert to_igs_antenna("TRM115000.10") == "TRM115000.10"

    def test_canonical_identity_passthrough(self):
        # A name already in the table as a value resolves to itself even when not
        # an explicit key (mirrors to_igs_receiver's identity step).
        assert to_igs_antenna("TRM57971.00") == "TRM57971.00"

    def test_unknown_antenna_still_none(self):
        assert to_igs_antenna("DEFINITELY_NOT_AN_ANTENNA") is None
