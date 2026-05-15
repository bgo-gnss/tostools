"""Canonicalisation helper for byte-equality snapshot comparisons.

Recursively normalises a result tree returned by `gps_metadata` or a future
`station_sessions`-style composer so the same logical structure compares
equal whether it was just produced by code (datetime objects, native dict
ordering) or just loaded from a JSON snapshot (ISO strings, key order
from the file).

Applied to both sides of the equality before the comparison; never used
to write the snapshot file (which uses `json.dumps(..., default=isoformat,
sort_keys=True)` directly).
"""

import datetime as _dt
from typing import Any, Callable, Optional

JsonLike = Any


# Stable sort keys for list-of-dicts where ordering is not semantically
# significant — e.g. children_connections returned in different orders
# by TOS across requests. Order matters in the per-session output of
# gps_metadata (it sorts by device.date_from explicitly, see line 582),
# so we leave that ordering alone. The map below stays empty until a
# real ordering instability surfaces.
_SORT_KEY: dict[str, Callable[[dict], Any]] = {}


def _normalise(value: JsonLike, path: str = "") -> JsonLike:
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _normalise(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        items = [_normalise(item, f"{path}[]") for item in value]
        key_name = path.rsplit(".", 1)[-1] if path else ""
        sort_fn: Optional[Callable[[dict], Any]] = _SORT_KEY.get(key_name)
        if sort_fn and all(isinstance(item, dict) for item in items):
            items = sorted(items, key=sort_fn)
        return items
    return value


def canonicalize(value: JsonLike) -> JsonLike:
    """Return a JSON-comparable copy of ``value``.

    - ``datetime`` / ``date`` → ISO 8601 string
    - Nested dicts / lists walked recursively
    - List-of-dict orderings normalised only for keys listed in ``_SORT_KEY``
      (currently none — extend as instabilities are discovered)
    """
    return _normalise(value)
