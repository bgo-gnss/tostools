"""Unit tests for ``tos device show`` rendering helpers.

Currently focuses on :func:`_color_id_with_recency` — the
``--highlight-since`` flag's primary discriminator. TOS exposes no
created_at on attribute_value rows; this helper uses id_attribute_value
as the soft recency signal. See CLAUDE.md "Retrospective writes" +
memory project_tos_retrospective_writes_provenance_gap.
"""

from __future__ import annotations

from tostools.tos import _color_id_with_recency


class TestColorIdWithRecency:
    def test_no_threshold_renders_same_as_color_id(self) -> None:
        """When highlight_since is None, behaves like the unflagged helper."""
        assert _color_id_with_recency(12345, None) == "[cyan]12345[/cyan]"

    def test_id_above_threshold_gets_star_marker(self) -> None:
        """Values strictly greater than threshold render with ★ + bold magenta."""
        rendered = _color_id_with_recency(152907, 100000)
        assert "★" in rendered
        assert "bold magenta" in rendered
        assert "152907" in rendered

    def test_id_at_threshold_is_NOT_highlighted(self) -> None:
        """Boundary check: 'since X' means 'greater than X', not 'greater-or-equal'.

        Keeps the threshold inclusive of the last not-highlighted write —
        operator can bookmark the id_av they want to mark *as the cutoff*
        and that bookmark itself stays unmarked.
        """
        rendered = _color_id_with_recency(100000, 100000)
        assert "★" not in rendered
        assert rendered == "[cyan]100000[/cyan]"

    def test_id_below_threshold_renders_normally(self) -> None:
        """Old values keep the standard cyan styling — no ★ marker."""
        rendered = _color_id_with_recency(32027, 100000)
        assert "★" not in rendered
        assert rendered == "[cyan]32027[/cyan]"

    def test_none_value_renders_question_mark(self) -> None:
        """Missing id stays '?' regardless of threshold (preserves the
        prior _color_id contract)."""
        assert _color_id_with_recency(None, 100000) == "?"
        assert _color_id_with_recency(None, None) == "?"

    def test_non_numeric_value_falls_through_to_cyan(self) -> None:
        """If id_attribute_value happens not to be an int (defensive — TOS
        always emits ints, but the helper tolerates surprises), the
        threshold-comparison silently fails and we fall back to the
        standard cyan rendering."""
        rendered = _color_id_with_recency("not-a-number", 100000)
        assert "★" not in rendered
        assert rendered == "[cyan]not-a-number[/cyan]"

    def test_string_id_above_threshold_still_highlights(self) -> None:
        """Numeric strings round-trip through int() — they should still
        compare against the threshold (TOS occasionally returns ids as
        strings depending on the endpoint)."""
        rendered = _color_id_with_recency("152907", 100000)
        assert "★" in rendered
        assert "152907" in rendered
