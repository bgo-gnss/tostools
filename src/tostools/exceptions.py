"""Typed exceptions for tostools.

Deliberately small. The one rule worth stating: **library code raises, CLI
entry points decide the exit code.** Library modules here used to call
``sys.exit(1)`` directly on a transport failure, which meant a transient TOS
blip during a multi-hour ``--fix-headers`` or re-rinex run killed the whole
process instead of failing one file.

``TOSConnectionError`` subclasses builtin :class:`ConnectionError` (and so
:class:`OSError`) on purpose, not just :class:`Exception`. Downstream callers
already funnel transport failures through ``except OSError`` — notably
``receivers.rinex.converter_base``, which converts them into its own
``NetworkUnavailableError`` and retries. Inheriting from ``ConnectionError``
means those handlers keep catching this without any downstream change, while
callers who want to be specific can catch ``TOSConnectionError`` directly.
"""

from __future__ import annotations


class TOSError(Exception):
    """Base class for errors originating in tostools."""


class TOSConnectionError(TOSError, ConnectionError):
    """Could not reach the TOS API.

    Raised instead of ``sys.exit(1)`` so a caller can decide whether one
    unreachable request should end the whole run.
    """


class IdenticalRenderSessionsError(TOSError):
    """Two adjacent device slices in a site log would render identically.

    This is the signature of a **TOS date misalignment**, not of an equipment
    change: an attribute period whose ``date_to`` lands at midnight (or whose
    ``date_from`` lands away from the real change) partitions one physical
    configuration into two eras that render byte-identically. The site-log
    section is then wrong — it reports an installation that never happened.

    Observed live on HRIC 2026-09-15: antenna device 4680's five closed periods
    (``status``, ``antenna_height``, both offsets, ``azimuth``) all ended
    ``2022-11-22T00:00:00`` while its **join** ended ``2022-11-22T16:00:00``, so
    §4 gained a 16-hour duplicate of the preceding section. M3G noticed and
    asked us to remove it. The receiver on the same parent (device 19668) has a
    join ending at the *same instant* and no midnight attribute boundary, and
    renders as ONE era — which isolates the cause to the midnight attribute
    periods. The same class appears at GRIV (monument 19950, a zero-length join)
    and ISAF (a 2004 phantom antenna session).

    Raising is deliberate. Publishing a phantom era to an international
    registry (M3G/EPOS) is worse than stopping, and once published it cannot be
    removed through the API at all — M3G refuses any upload with fewer
    subsections than the live record, so a phantom can only be pruned by hand on
    the portal. The site-log path therefore refuses to render until the TOS rows
    are fixed, or until an operator has investigated and sets
    ``TOSTOOLS_ALLOW_IDENTICAL_SESSIONS=1``.

    Fixing the date needs a full timestamp, which the audited triage verb cannot
    express: ``ACTION <id> patch-attribute-date-to <code> <from> <to>`` truncates
    ``to`` to ``YYYY-MM-DD`` and would recreate the midnight boundary. Use
    :meth:`tostools.api.tos_writer.TOSWriter.patch_attribute_value` with
    ``date_to="2022-11-22T16:00:00"`` instead.
    """
