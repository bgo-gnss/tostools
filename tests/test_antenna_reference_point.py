"""A new antenna must carry a GAMIT height code, defaulting to DHARP.

The failure this prevents, 2026-10-03: `tosGPS syncMeta` wrote `BPA` into
`station.info`'s `HtCod` column and the GAMIT run failed. `BPA` is the **IGS
site-log** ARP code for the antenna model (`antenna_arp.list` has
`SEPVC6150L BPA NOM`, and that file's vocabulary — BAM/BPA/TOP/BCR/TGP/TCR —
comes from `antenna.gra`). GAMIT needs a height code: DHARP/DHPAB/DHBCR/DHTCR.

One TOS attribute, two incompatible vocabularies, and the renderer
`legacy.gps_metadata_functions.print_station_info` copies it VERBATIM. The
intended direction is the opposite — the site-log generator expects `DHARP`
stored and translates outward to the IGS code via that same file.

Root cause: no intake verb set the attribute at all, so a new antenna carried
whatever a human typed into the TOS web UI, or nothing (VOTT: `-----` on 6
rows, the `is None` branch). Both break GAMIT.
"""

from __future__ import annotations

import pytest

from tostools.device import (
    DEFAULT_ANTENNA_REFERENCE_POINT,
    GAMIT_HEIGHT_CODES,
    build_antenna_attributes,
)

ARGS = ("2402130023", "SEPVC6150L", "IMO", "2026-09-30T18:45:29")


def _codes(attrs):
    return {a["code"]: a["value"] for a in attrs}


def test_a_new_antenna_always_gets_a_reference_point():
    """The regression in one assertion: the attribute must be emitted at all."""
    attrs = build_antenna_attributes(*ARGS)
    assert "antenna_reference_point" in _codes(
        attrs
    ), "left unset, station.info renders '-----' in HtCod and GAMIT fails"


def test_it_defaults_to_dharp():
    attrs = build_antenna_attributes(*ARGS)
    assert _codes(attrs)["antenna_reference_point"] == "DHARP"
    assert DEFAULT_ANTENNA_REFERENCE_POINT == "DHARP"


def test_the_igs_arp_vocabulary_is_refused():
    """BPA is exactly what broke VFLS/VFLN — it must not be storable.

    These are the values in `antenna_arp.list`, i.e. the site-log vocabulary.
    Passing one through to a GAMIT field is the whole bug.
    """
    for igs in ("BPA", "BAM", "BCR", "TOP", "TGP", "TCR"):
        with pytest.raises(ValueError) as exc:
            build_antenna_attributes(*ARGS, antenna_reference_point=igs)
        assert "GAMIT height code" in str(exc.value)


def test_the_other_real_height_codes_are_accepted():
    """DHPAB/DHBCR/DHTCR exist in the fleet — 471/267/4 rows. Not just DHARP."""
    for code in sorted(GAMIT_HEIGHT_CODES):
        attrs = build_antenna_attributes(*ARGS, antenna_reference_point=code)
        assert _codes(attrs)["antenna_reference_point"] == code


def test_it_is_normalised_not_trusted():
    attrs = build_antenna_attributes(*ARGS, antenna_reference_point=" dharp ")
    assert _codes(attrs)["antenna_reference_point"] == "DHARP"


def test_empty_or_none_is_refused_rather_than_silently_omitted():
    """Silently omitting is how '-----' reached VOTT's station.info."""
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            build_antenna_attributes(*ARGS, antenna_reference_point=bad)


def test_it_shares_the_install_date_with_the_other_attributes():
    """A different date_from would split the session — see the onboarding SOP."""
    attrs = build_antenna_attributes(*ARGS, antenna_height="0.0")
    arp = next(a for a in attrs if a["code"] == "antenna_reference_point")
    assert arp["date_from"] == ARGS[3]
    assert arp["date_to"] is None
    assert {a["date_from"] for a in attrs} == {ARGS[3]}


def test_antenna_height_still_optional_and_unaffected():
    assert "antenna_height" not in _codes(build_antenna_attributes(*ARGS))
    assert (
        _codes(build_antenna_attributes(*ARGS, antenna_height="1.078"))[
            "antenna_height"
        ]
        == "1.078"
    )
