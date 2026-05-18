# Synthesis: legacy vs. new composer chain — known divergences

## Context

Phase 1c of the devices refactor (vault note
`1778713245-tostools-devices-design`) introduces a new composer chain:

```
slice_attributes_by_window  →  device_sessions  →  station_sessions
       (the slicer)             (per-device pivot)   (per-station pivot)
```

This replaces the legacy synthesis in
`gps_metadata_qc.device_attribute_history` (slicer) +
`gps_metadata_qc.get_device_history` (pivot), invoked from
`gps_metadata_qc.gps_metadata`.

The §9 byte-equality gate (level 2) asserts that on a fixture station
(RHOF) the new chain reproduces legacy output byte-for-byte
(`tests/_oracle_outputs/RHOF_legacy.json`). That gate **holds for clean
fixture stations** — RHOF, AKUR, VMEY, SKRO. It **does not hold** for
three real-world stations carrying messier metadata: AUST severely,
REYK and HOFN mildly.

The cause is not a bug in the new chain. It is two bugs in the legacy
chain that the byte-equality gate happens to mask on clean data and
that surface on stations with overlapping device installations or
misaligned attribute periods.

## Bug 1 — pair-based slicer drops sub-windows

`gps_metadata_qc.device_attribute_history` deduplicates sub-sessions
via `set(zip(dates_from, dates_to))` — keyed by the *tuple* of an
attribute period's start and end. This is correct when every tracked
attribute on a device transitions on the same dates. It silently
drops sub-windows when attribute periods misalign.

Concrete case — HOFN antenna `id_entity=4621`:

```
attribute              date_from   date_to
─────────────────────  ──────────  ────────
antenna_model          2013-05-05  None
antenna_reference_pt   2013-05-05  None
serial_number          2014-10-17  None
```

The serial number was added 17 months after the antenna's model + ARP
were recorded. The set of `(date_from, date_to)` pairs collapses to
just `(2014-10-17, None)`, so the slicer emits one sub-window starting
2014-10-17. The 2013-05-05 → 2014-10-17 window — during which the
device existed but its serial was not yet recorded — is silently
omitted.

The new slicer (`slice_attributes_by_window`) takes the union of every
attribute boundary in the window, emitting one atomic sub-window per
unique segment. For HOFN antenna 4621 it correctly emits two
sub-windows: 2013-05-05 → 2014-10-17 (with model + ARP only) and
2014-10-17 → None (with serial added).

## Bug 2 — pivot's independent-iter zip inverts sessions

The legacy `get_device_history` pivot — preserved exactly by the new
`station_sessions` for compatibility — builds the per-station session
list like this:

```python
starts = iter(sorted({s.device.date_from for s in device_sessions}))
ends   = iter(sorted({s.device.date_to   for s in device_sessions
                                          if s.device.date_to is not None}))
for start in starts:
    end = next(ends, None)
    # ... emit session (start, end, slots) ...
```

Two **independently sorted** sets are zipped position-by-position.
This is correct only when the boundaries interleave cleanly — every
sub-session's `date_to` equals the next sub-session's `date_from`, so
the `i`-th `start` and the `i`-th `end` describe the same logical
session.

That precondition holds when the slicer's output is well-ordered. The
new slicer guarantees it by construction: each atomic sub-window's end
is the next sub-window's start. The legacy slicer's pair-based output
does *not* guarantee it. On AUST, the resulting position-wise zip
produces 17 sessions where `time_to < time_from` — e.g. the legacy
`device_history` reports a session running from `2001-03-26` back to
`2000-07-10`.

Mathematically: when `sorted(starts)[i]` and `sorted(ends)[i]` are
drawn from sets of different sizes or with non-interleaving values,
they describe unrelated boundaries. The pivot then renders these as
malformed sessions.

The new chain fixes this not by changing the pivot but by feeding it
well-ordered slicer output.

## Known-divergent stations

| Station | Sessions (new / legacy) | Failure mode |
|---|---|---|
| AUST    | 26 / 24  | 17 of legacy's 24 sessions have `time_to < time_from` (bug 2). 2 sessions missing from legacy tail (bug 1 dropping late sub-windows). |
| REYK    | n / n−1  | One legacy session has `time_to < time_from` (bug 2). |
| HOFN    | n / n    | One legacy session reports `None` for 3 subtypes because their pre-serial sub-window was dropped (bug 1). |

These three are real production stations. The legacy synthesis output
has been the source of GAMIT `station.info` and IGS site logs for
years. Either downstream consumers tolerate the inverted / incomplete
windows silently (most likely — IGS site logs render `time_from` only
for the current session), or the divergence shows up as harmless
metadata noise that humans have learned to filter out.

## Why we do not match bug-for-bug

Three reasons:

1. **The output is wrong.** A session with `time_to < time_from` is
   not a session. Reproducing it in the new code would propagate a
   defect that downstream users (cfg reconciliation, audit, future
   web/phone interfaces) would then have to special-case around.
2. **The bugs are not stable.** They depend on the exact distribution
   of attribute-period boundaries for a station, which changes over
   time as TOS edits land. A station that synthesizes "correctly"
   today can start producing inverted sessions tomorrow after a
   single attribute backfill. Locking to the buggy behaviour gives a
   moving target.
3. **The new chain is structurally correct.** The fix is in the
   slicer (atomic union-of-boundaries instead of pair-based dedup);
   the pivot is unchanged. There is no extra surface to maintain.

## Sign-off contract for phase 1c

For each station in scope of the byte-equality gate:

* **Legacy-correct stations** (RHOF, AKUR, VMEY, SKRO):
  `station_sessions(...)` must byte-equal
  `gps_metadata(...)["device_history"]`. Locked by
  `test_rhof_station_sessions_matches_legacy_device_history` in
  `tests/test_composer_oracle.py`. Snapshot: `RHOF_legacy.json`.

* **Legacy-buggy stations** (AUST and friends): the new composer
  output is locked against its own captured snapshot, *not* against
  legacy. The snapshot is captured once from the new chain (after
  human review of the divergence) and replayed on every CI run.
  Locked by `test_aust_station_sessions_locked` in
  `tests/test_composer_oracle.py`. Snapshot: `AUST_new.json`.

REYK and HOFN can be added in the same pattern (`<MARKER>_new.json`)
as the need arises — neither blocks phase 1c sign-off because they're
mild single-session divergences, not the fleet-wide pattern AUST
exhibits.

## Implications for phase 4 (site-log gate)

Phase 4 of the design lands a `--use-new-synthesis` flag on
`tosGPS sitelog` / `tosGPS PrintTOS`. The sign-off there is the
golden-file diff against `data/sitelogs_archive/<STATION>_*.txt`.

For RHOF / AKUR / VMEY / SKRO that diff must be empty.

For AUST / REYK / HOFN the diff will be non-empty by definition. New
golden files for these stations must be captured from the new chain
and **manually reviewed by a domain expert** (GAMIT operator or IGS
site-log curator) before they are committed as the new reference. The
review confirms that:

* Equipment metadata in each session matches what was physically on
  the antenna at the time (cross-checked against TOS UI / receiver
  health logs).
* The added or shifted sub-windows reflect real installation events,
  not synthetic noise.
* No session has `time_to < time_from`. (This should be impossible by
  construction in the new chain; flag any occurrence as a slicer or
  pivot regression.)

Until that review happens for a given station, the legacy synthesis
remains the operational source of truth for *that station's* IGS
artefacts. The flag default flips per-station as reviews complete.

## References

* Vault note `1778713245-tostools-devices-design` — design proposal.
* `tests/test_composer_oracle.py` — oracle harness.
* `tests/_oracle_outputs/` — snapshot files.
* `scripts/capture_oracle.py` — captures both legacy and new snapshots
  from a VCR cassette (`--source legacy` / `--source new`).
* `docs/architecture/tos-write-api.md` — TOS write-side patterns
  (Pattern 1 / 2 / 4) that compose with the read-side synthesis
  described here.
