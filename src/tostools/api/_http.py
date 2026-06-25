"""Shared HTTP helpers for the TOS API clients (:class:`TOSClient`, :class:`TOSWriter`)."""

from __future__ import annotations

# Path segments that mean a None/Null value leaked into URL string formatting
# (e.g. ``/entity/None`` when an id_entity is None). The TOS backend rejects
# these with 422 — we fail fast with a clear error instead.
_NULL_SEGMENTS = frozenset({"None", "Null", "null", "NULL"})


def canonical_tos_url(base_url: str, endpoint: str) -> str:
    """Join ``base_url`` + ``endpoint`` into the canonical TOS request URL.

    The TOS backend (revised 2026-06) canonicalizes paths **without** a trailing
    slash: a ``/``-terminated path 308-redirects to the slashless form. That
    redirect both adds noise to backend monitoring and can drop the
    ``Authorization`` header on mutating calls (a likely cause of intermittent
    401s on PATCH/POST). So strip a trailing slash here, at the single point
    every request flows through — endpoint string literals can keep their
    historical trailing slash; the wire request is normalized.

    It also returns 422 for a ``None``/``Null`` path segment. Refuse to send
    such a URL with an explicit error that names the offending path, rather than
    letting it fail server-side as an opaque 422.

    Args:
        base_url: API base, e.g. ``https://vi-api.vedur.is/tos/internal``.
        endpoint: Path part, with or without leading/trailing slashes.

    Returns:
        The normalized absolute URL (no trailing slash, any inline query kept).

    Raises:
        ValueError: If a path segment is ``None``/``Null`` (formatting bug).
    """
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    # Split off any (rare) inline query string before trimming the path.
    head, sep, query = url.partition("?")
    head = head.rstrip("/")
    for segment in head.split("/"):
        if segment in _NULL_SEGMENTS:
            raise ValueError(
                f"Refusing TOS request with a {segment!r} path segment "
                f"({url!r}) — a None/Null value leaked into URL formatting."
            )
    return head + sep + query
