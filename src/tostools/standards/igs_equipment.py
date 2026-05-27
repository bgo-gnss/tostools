"""IGS standard equipment name lookup tables.

TOS stores IGS-standard names for all equipment (receivers, antennas, radomes).
These dictionaries map the raw strings that receiver health extractors report
to the IGS-standard names that TOS expects when writing.

For reads (sitelog generation, reconcile display), TOS already holds the IGS
names — no conversion needed.  These lookups are only used on the **write path**:
converting health-reported or cfg-canonical strings to the correct TOS value.

Sources
-------
- IGS receiver/antenna name list: https://files.igs.org/pub/station/general/rcvr_ant.tab
- Network-specific names verified against LMI station.info and TOS attribute_values

Adding new equipment
--------------------
1. Find the exact IGS name in rcvr_ant.tab (case-sensitive, exact spacing).
2. Add all known aliases the health extractor or cfg might report as keys.
3. Bump the tostools version if the change is breaking for any caller.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Receiver IGS names
# ---------------------------------------------------------------------------
# Keys: any string a health extractor or stations.cfg might supply.
# Values: exact IGS receiver name as stored in TOS (from rcvr_ant.tab).

RECEIVER_IGS: dict[str, str] = {
    # Septentrio PolaRX5 -------------------------------------------------------
    # ProductName from SBF ReceiverSetup block (preferred, firmware-set):
    "PolaRx5": "SEPT POLARX5",
    "PolaRX5": "SEPT POLARX5",
    # RxName field (IGS code stored inside the receiver):
    "SSRC7": "SEPT POLARX5",
    # Upper-case variants seen in some contexts:
    "POLARX5": "SEPT POLARX5",
    # Septentrio mosaic-X5 -----------------------------------------------------
    "mosaic-X5": "SEPT MOSAICX5",
    "MOSAICX5": "SEPT MOSAICX5",
    # Septentrio PolaRX3e (historical) -----------------------------------------
    "PolaRX3e": "SEPT POLARX3E",
    "POLARX3E": "SEPT POLARX3E",
    # Septentrio PolaRX2 (historical, pre-PolaRX5 era; deployed at IMO
    # stations KVIS/FTEY/GAKE/GAK1/GAK2/HEDI/SAVI from ~2006-2014):
    "PolaRX2": "SEPT POLARX2",
    "PolaRx2": "SEPT POLARX2",
    "POLARX2": "SEPT POLARX2",
    # Septentrio PolaRX2E (Enhanced variant; not currently deployed at
    # IMO stations — listed for cross-network completeness):
    "PolaRX2E": "SEPT POLARX2E",
    "PolaRx2E": "SEPT POLARX2E",
    "POLARX2E": "SEPT POLARX2E",
    # Trimble NetR9 ------------------------------------------------------------
    "NetR9": "TRIMBLE NETR9",
    "NETR9": "TRIMBLE NETR9",
    # Trimble NetRS ------------------------------------------------------------
    "NetRS": "TRIMBLE NETRS",
    "NETRS": "TRIMBLE NETRS",
    # Trimble NetR5 ------------------------------------------------------------
    "NetR5": "TRIMBLE NETR5",
    "NETR5": "TRIMBLE NETR5",
    # Leica GR10 ---------------------------------------------------------------
    # Our cfg canonical "G10" collapses GR10 and GR25; only GR10 is deployed (SKFC).
    "G10": "LEICA GR10",
    "GR10": "LEICA GR10",
    # Leica GR25 (not currently deployed; add aliases when needed):
    "GR25": "LEICA GR25",
}

# ---------------------------------------------------------------------------
# Antenna IGS names
# ---------------------------------------------------------------------------
# The PolaRX5 extractor already returns IGS-format antenna names (parsed from
# the 20-char AntType field), so these aliases cover edge cases only.
# Radomes travel separately in RADOME_IGS below.

ANTENNA_IGS: dict[str, str] = {
    # Trimble choke-ring antennas ----------------------------------------------
    "TRM29659.00": "TRM29659.00",  # identity — already IGS
    "TRM33429.20+GP": "TRM33429.20+GP",
    "TRM41249.00": "TRM41249.00",
    "TRM55971.00": "TRM55971.00",
    "TRM57971.00": "TRM57971.00",
    # Leica antenna ------------------------------------------------------------
    "LEIAR25.R4": "LEIAR25.R4",
    # Ashtech / pre-IGS names seen in historical data:
    "ASH700936C_M": "ASH700936C_M",
    "ASH701945C_M": "ASH701945C_M",
    # Septentrio choke-ring ----------------------------------------------------
    "SEPCHOKE_B3E6": "SEPCHOKE_B3E6",
}

# ---------------------------------------------------------------------------
# Radome IGS codes
# ---------------------------------------------------------------------------
# Four-character codes from rcvr_ant.tab.  "NONE" means no radome.

RADOME_IGS: dict[str, str] = {
    "NONE": "NONE",
    "SPKE": "SPKE",  # Septentrio spike radome
    "SCIS": "SCIS",  # Trimble steel cover
    "LEIT": "LEIT",  # Leica transparent
    "SNOW": "SNOW",  # Snow/ice shield
    "TZGD": "TZGD",  # Trimble zephyr geodetic dome
    # Common aliases:
    "None": "NONE",
    "none": "NONE",
    "": "NONE",
}

# ---------------------------------------------------------------------------
# Public lookup helpers
# ---------------------------------------------------------------------------


def to_igs_receiver(raw: Optional[str]) -> Optional[str]:
    """Return the IGS-standard receiver name for *raw*, or ``None``.

    Resolution order:

    1. **Exact alias match** — ``RECEIVER_IGS[raw]`` (e.g. ``"PolaRx5"`` →
       ``"SEPT POLARX5"``).
    2. **Case-folded alias match** — ``"netr9"`` and ``"NETR9"`` both resolve
       to ``"TRIMBLE NETR9"``.
    3. **Canonical-name identity** — when ``raw`` is already a known IGS
       canonical name (matches any *value* in :data:`RECEIVER_IGS`), return
       it unchanged. Lets operators paste the rcvr_ant.tab spelling directly
       (e.g. ``"SEPT POLARX2"``) without needing a redundant identity entry
       for every receiver.

    Returns ``None`` if *raw* is empty or unknown — callers should treat
    ``None`` as "cannot convert, do not write".

    Args:
        raw: Raw receiver model string from a health extractor / cfg /
            operator input.

    Returns:
        IGS name string (e.g. ``"SEPT POLARX5"``), or ``None`` if unknown.
    """
    if not raw:
        return None
    result = RECEIVER_IGS.get(raw)
    if result is not None:
        return result
    # Case-insensitive alias-key fallback.
    upper = raw.upper()
    for key, value in RECEIVER_IGS.items():
        if key.upper() == upper:
            return value
    # Canonical-name identity — accept any value already known to the dict.
    if raw in set(RECEIVER_IGS.values()):
        return raw
    return None


def to_igs_antenna(raw: Optional[str]) -> Optional[str]:
    """Return the IGS-standard antenna name for *raw*, or ``None``.

    For PolaRX5 stations the extractor already returns IGS names, so this is
    mostly an identity function.  Returns ``None`` for empty or unknown input.

    Args:
        raw: Raw antenna model string.

    Returns:
        IGS name string (e.g. ``"TRM57971.00"``), or ``None`` if unknown.
    """
    if not raw:
        return None
    result = ANTENNA_IGS.get(raw)
    if result is not None:
        return result
    upper = raw.upper()
    for key, value in ANTENNA_IGS.items():
        if key.upper() == upper:
            return value
    return None


def to_igs_radome(raw: Optional[str]) -> Optional[str]:
    """Return the IGS-standard radome code for *raw*, defaulting to ``"NONE"``.

    Args:
        raw: Raw radome string from a health extractor or cfg.

    Returns:
        Four-character IGS radome code, or ``"NONE"`` for empty/unknown input.
    """
    if raw is None:
        return "NONE"
    result = RADOME_IGS.get(raw)
    if result is not None:
        return result
    upper = raw.upper()
    for key, value in RADOME_IGS.items():
        if key.upper() == upper:
            return value
    # Unknown radome: treat as no radome rather than propagating garbage to TOS.
    return "NONE"
