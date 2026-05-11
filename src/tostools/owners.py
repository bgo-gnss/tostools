"""TOS device owners — curated allow-list with TOS-verification refresh.

Device ownership in TOS is a free-text string stored as ``value_varchar`` on a
``code=owner`` attribute attached to device entities (antenna, gnss_receiver,
radome, etc.). There is no separate "owner entity" — the seed list below is
the canonical set of recognized owner labels used across the IMO GNSS network.

The local cache (default ``~/.config/tostools/owners.yaml``) holds the curated
list. ``OwnersCache.refresh`` verifies each known owner is still present in TOS
via ``basic_search``; it does NOT discover new owner strings. New entries must
be added manually (edit the cache file) or via a future ``tos owners add``
subcommand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import yaml

from .api.tos_client import TOSClient

# Canonical seed list of recognized owner labels.
KNOWN_OWNERS: Tuple[str, ...] = (
    "Veðurstofa Íslands",
    "Jarðeðlismælihópur",
    "Vatnamælihópur",
    "Veðurmælihópur",
    "Cambridge",
    "ÍSOR",
)

DEFAULT_CACHE_PATH = Path.home() / ".config" / "tostools" / "owners.yaml"

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    """Outcome of a refresh probe against TOS."""

    in_use: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)


class OwnersCache:
    """File-backed cache of recognized TOS device owner strings."""

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH

    def load(self) -> List[str]:
        """Return the sorted list of owners.

        Falls back to ``KNOWN_OWNERS`` when no cache file exists or the file
        does not declare an ``owners`` list.
        """
        if not self.cache_path.exists():
            return sorted(KNOWN_OWNERS)
        with self.cache_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        owners = data.get("owners") if isinstance(data, dict) else None
        if not owners:
            return sorted(KNOWN_OWNERS)
        return sorted({str(o) for o in owners})

    def save(self, owners: Iterable[str]) -> None:
        """Write owners list to the cache file (creates parent dirs)."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"owners": sorted({str(o) for o in owners})}
        with self.cache_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)

    def refresh(self, client: TOSClient) -> RefreshResult:
        """Verify each ``KNOWN_OWNERS`` entry is still in use in TOS.

        Probes ``basic_search`` once per seed name and looks for at least one
        hit with ``code='owner'``, ``value_varchar`` matching the seed exactly,
        and ``distance=0`` (TOS substring match score; 0 means exact value).

        This is verify-only — it does not discover new owner strings. The
        cache is rewritten with the in-use owners so missing seeds drop out.
        """
        in_use: List[str] = []
        missing: List[str] = []
        for name in KNOWN_OWNERS:
            hits = client.basic_search(name)
            if any(
                h.get("code") == "owner"
                and h.get("value_varchar") == name
                and h.get("distance") == 0
                for h in hits
            ):
                in_use.append(name)
            else:
                missing.append(name)
                logger.warning("Owner %r not found in TOS basic_search hits", name)
        self.save(in_use)
        return RefreshResult(in_use=in_use, missing=missing)


def find_by_name(name: str, cache: Optional[OwnersCache] = None) -> Optional[str]:
    """Exact-match lookup against the cache. Returns the owner or None."""
    c = cache if cache is not None else OwnersCache()
    owners = c.load()
    return name if name in owners else None
