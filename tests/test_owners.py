"""Unit tests for the owners module — no network required."""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import yaml

from tostools.owners import (
    KNOWN_OWNERS,
    OwnersCache,
    RefreshResult,
    find_by_name,
)


# ---------------------------------------------------------------------------
# OwnersCache.load
# ---------------------------------------------------------------------------


def test_load_returns_seed_when_no_cache(tmp_path: Path) -> None:
    cache = OwnersCache(tmp_path / "missing.yaml")
    assert cache.load() == sorted(KNOWN_OWNERS)


def test_load_returns_cache_contents(tmp_path: Path) -> None:
    p = tmp_path / "owners.yaml"
    p.write_text(yaml.safe_dump({"owners": ["Foo", "Bar"]}, allow_unicode=True))
    cache = OwnersCache(p)
    assert cache.load() == ["Bar", "Foo"]


def test_load_falls_back_when_owners_key_empty(tmp_path: Path) -> None:
    p = tmp_path / "owners.yaml"
    p.write_text(yaml.safe_dump({"owners": []}, allow_unicode=True))
    cache = OwnersCache(p)
    assert cache.load() == sorted(KNOWN_OWNERS)


def test_load_falls_back_when_file_empty(tmp_path: Path) -> None:
    p = tmp_path / "owners.yaml"
    p.write_text("")
    cache = OwnersCache(p)
    assert cache.load() == sorted(KNOWN_OWNERS)


def test_load_handles_unicode_owner_names(tmp_path: Path) -> None:
    p = tmp_path / "owners.yaml"
    p.write_text(
        yaml.safe_dump(
            {"owners": ["Veðurstofa Íslands", "Jarðeðlismælihópur"]},
            allow_unicode=True,
        )
    )
    cache = OwnersCache(p)
    loaded = cache.load()
    assert "Veðurstofa Íslands" in loaded
    assert "Jarðeðlismælihópur" in loaded


# ---------------------------------------------------------------------------
# OwnersCache.save
# ---------------------------------------------------------------------------


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "owners.yaml"
    cache = OwnersCache(target)
    cache.save(["A", "B"])
    assert target.exists()
    data = yaml.safe_load(target.read_text())
    assert data == {"owners": ["A", "B"]}


def test_save_deduplicates(tmp_path: Path) -> None:
    p = tmp_path / "owners.yaml"
    OwnersCache(p).save(["Foo", "Foo", "Bar"])
    data = yaml.safe_load(p.read_text())
    assert data == {"owners": ["Bar", "Foo"]}


def test_save_preserves_unicode(tmp_path: Path) -> None:
    p = tmp_path / "owners.yaml"
    OwnersCache(p).save(["Veðurstofa Íslands", "ÍSOR"])
    text = p.read_text()
    assert "Veðurstofa Íslands" in text  # not escaped to \uXXXX


# ---------------------------------------------------------------------------
# OwnersCache.refresh
# ---------------------------------------------------------------------------


def _hit(name: str, code: str = "owner", distance: int = 0) -> dict:
    return {"code": code, "value_varchar": name, "distance": distance}


def test_refresh_marks_all_in_use_when_hits_match(tmp_path: Path) -> None:
    client = MagicMock()
    client.basic_search.side_effect = lambda name: [_hit(name)]

    cache = OwnersCache(tmp_path / "owners.yaml")
    result = cache.refresh(client)

    assert isinstance(result, RefreshResult)
    assert sorted(result.in_use) == sorted(KNOWN_OWNERS)
    assert result.missing == []
    assert client.basic_search.call_count == len(KNOWN_OWNERS)


def test_refresh_flags_missing_owners(tmp_path: Path) -> None:
    def fake_search(name: str) -> List[dict]:
        # Cambridge has no hits
        return [] if name == "Cambridge" else [_hit(name)]

    client = MagicMock()
    client.basic_search.side_effect = fake_search

    cache = OwnersCache(tmp_path / "owners.yaml")
    result = cache.refresh(client)

    assert result.missing == ["Cambridge"]
    assert "Cambridge" not in result.in_use


def test_refresh_ignores_non_owner_hits(tmp_path: Path) -> None:
    """A hit with code != 'owner' must not count as a match."""
    client = MagicMock()
    client.basic_search.return_value = [_hit("Cambridge", code="description")]

    cache = OwnersCache(tmp_path / "owners.yaml")
    result = cache.refresh(client)

    # All seeds appear missing because no hit has code='owner'
    assert sorted(result.missing) == sorted(KNOWN_OWNERS)
    assert result.in_use == []


def test_refresh_ignores_partial_matches(tmp_path: Path) -> None:
    """Hits with distance != 0 (substring partial) must not count."""
    client = MagicMock()
    client.basic_search.return_value = [_hit("Cambridge", distance=4)]

    cache = OwnersCache(tmp_path / "owners.yaml")
    result = cache.refresh(client)

    assert sorted(result.missing) == sorted(KNOWN_OWNERS)


def test_refresh_persists_in_use_owners(tmp_path: Path) -> None:
    def fake_search(name: str) -> List[dict]:
        return [] if name == "Cambridge" else [_hit(name)]

    client = MagicMock()
    client.basic_search.side_effect = fake_search

    p = tmp_path / "owners.yaml"
    OwnersCache(p).refresh(client)

    data = yaml.safe_load(p.read_text())
    assert "Cambridge" not in data["owners"]
    assert "Veðurstofa Íslands" in data["owners"]


# ---------------------------------------------------------------------------
# find_by_name
# ---------------------------------------------------------------------------


def test_find_by_name_exact_match(tmp_path: Path) -> None:
    cache = OwnersCache(tmp_path / "missing.yaml")  # falls back to seed
    assert find_by_name("Veðurstofa Íslands", cache=cache) == "Veðurstofa Íslands"


def test_find_by_name_missing_returns_none(tmp_path: Path) -> None:
    cache = OwnersCache(tmp_path / "missing.yaml")
    assert find_by_name("Acme Co", cache=cache) is None


def test_find_by_name_is_case_sensitive(tmp_path: Path) -> None:
    cache = OwnersCache(tmp_path / "missing.yaml")
    assert find_by_name("veðurstofa íslands", cache=cache) is None


def test_find_by_name_uses_default_cache_when_none_passed(
    tmp_path: Path, monkeypatch
) -> None:
    """No cache arg → uses DEFAULT_CACHE_PATH; fallback seed still resolves."""
    monkeypatch.setattr(
        "tostools.owners.DEFAULT_CACHE_PATH", tmp_path / "owners.yaml"
    )
    assert find_by_name("Veðurstofa Íslands") == "Veðurstofa Íslands"
    assert find_by_name("definitely-not-an-owner") is None
