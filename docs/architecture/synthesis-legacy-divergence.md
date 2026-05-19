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

The legacy `get_device_history` pivot builds the per-station session
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

That precondition fails any time two device subtypes have overlapping
but non-aligned lifecycles. Example: a receiver installed 2010 → 2020
together with an antenna installed 2012 → 2018 produces
`starts = {2010, 2012}`, `ends = {2018, 2020}`. The pairwise zip emits
`(2010, 2018), (2012, 2020)` — neither pair describing a real
configuration. On AUST the same pattern across ~10 overlapping device
installations produces sessions with `time_to < time_from` —
e.g. `2001-03-26 → 2000-07-10`.

The new chain replaces the position-wise zip with a **boundary
merge**:

```python
boundaries = sorted({df for df in all_date_from} |
                    {dt for dt in all_date_to if dt is not None})
intervals  = [(boundaries[i], boundaries[i+1])
              for i in range(len(boundaries) - 1)]
if any sub-session is open:
    intervals.append((boundaries[-1], None))
```

Each adjacent boundary pair defines exactly one station-level
session window. For clean stations (every sub-session's end aligns
with the next sub-session's start) the merge produces the same
window set as the legacy zip — RHOF byte-equality holds. For
overlap-heavy stations it produces 0 inverted sessions where legacy
had many. After this fix the AUST snapshot drops from 26 sessions
with 4 inversions to 31 sessions with 0 inversions, and every
session has `time_to > time_from` by construction.

## Coalescing pass — phantom-boundary cleanup

The atomic slicer + boundary-merge pivot guarantee correctness but
react to *every* boundary in TOS, including data-entry artifacts (a
redundant attribute-period transition, a metadata cleanup with the
same value, a backfilled record). On stations with noisy attribute
periods this produces phantom sub-windows where two consecutive
sessions describe the same physical configuration.

After the pivot, `station_sessions` runs a coalescing pass that
merges consecutive sessions whose four subtype slots
(`gnss_receiver`, `antenna`, `radome`, `monument`) are *identical*.
The atomic-slicer guarantee makes this safe: each sub-window's end
is the next sub-window's start, so the merged window covers a
contiguous interval.

The pass is conservative — it only merges when *every* tracked slot
matches. Real equipment changes (different receiver SN, firmware
bump, antenna swap, height adjustment) are never merged across. For
those, the underlying TOS data needs cleanup; the synthesis chain
can only render what TOS contains.

After this pass on AUST, two phantom boundaries collapse
(`2000-07-06 → 2000-07-08` was incorrectly split at 2000-07-07;
`2011-06-21 → 2013-12-09` was incorrectly split at 2011-09-19),
dropping the snapshot from 31 to 29 sessions. RHOF byte-equality is
preserved (clean stations have no identical-adjacent sub-windows).

Pass `coalesce=False` to inspect the raw atomic sub-windows (useful
for slicer debugging).

## Enrichment — receiver-swap gap windows

Beyond the two legacy bugs, the boundary-merge pivot exposes a third
class of difference that is **not a bug on either side** but a
modeling choice. When a device of one subtype is swapped (old unit
removed → gap → new unit installed) while another subtype's device
remains in place, the gap interval is a real period during which the
station had partial equipment installed.

Concrete case — AKUR receiver swap 2018-01-21 → 2018-01-30:

```
old receiver: ... → 2018-01-21
new receiver: 2018-01-30 → ...
antenna:      2015 → 2020   (covers the gap)
radome:       2015 → 2020   (covers the gap)
monument:     2001 → open   (covers the gap)
```

Legacy `gps_metadata` drops the 9-day gap because its slicer's
pair-based dedup produces no sub-session with `date_from = 2018-01-21`
— so the position-wise zip pairs `start=2018-01-30` with the
preceding end, eliding the gap.

The new chain emits one session covering the gap with the
`gnss_receiver` slot absent and the antenna/radome/monument slots
present. Downstream consumers that require a complete equipment
configuration (IGS site logs, GAMIT `station.info`) can filter
sessions missing the receiver slot. Consumers that want the raw
temporal model (audit, web/phone UI) get the more accurate picture.

## Known-divergent stations

| Station | Sessions (new / legacy) | Cause |
|---|---|---|
| AUST | 31 / 24 | bugs 1 + 2 + enrichment — legacy under-emitted and inverted; new is well-ordered and granular. |
| AKUR | 6 / 5 | enrichment — one 9-day receiver-swap gap window legacy collapsed. |
| REYK | n / n−1 | bug 2 — one inverted legacy session. |
| HOFN | n+ / n | bug 1 — legacy session reporting `None` for 3 subtypes because the pre-serial sub-window was dropped. |

These are real production stations. The legacy synthesis output has
been the source of GAMIT `station.info` and IGS site logs for years.
Either downstream consumers tolerate the inverted / incomplete windows
silently (most likely — IGS site logs render `time_from` only for the
current session), or the divergence shows up as harmless metadata
noise that humans have learned to filter out.

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

* **Legacy-correct stations** (RHOF, VMEY, SKRO — clean, no
  cross-subtype gaps): `station_sessions(...)` must byte-equal
  `gps_metadata(...)["device_history"]`. Locked by
  `test_rhof_station_sessions_matches_legacy_device_history` in
  `tests/test_composer_oracle.py`. Snapshot: `RHOF_legacy.json`.

* **Legacy-divergent stations** (AUST, AKUR, REYK, HOFN): the new
  composer output is locked against its own captured snapshot, *not*
  against legacy. The snapshot is captured once from the new chain
  (after human review of the divergence) and replayed on every CI
  run. Locked by `test_aust_station_sessions_locked` in
  `tests/test_composer_oracle.py`. Snapshot: `AUST_new.json`. AKUR,
  REYK, HOFN can be added in the same pattern (`<MARKER>_new.json`)
  when their downstream artefacts are reviewed.

The byte-equality test for `station_sessions` on AUST also serves as
a structural guard: any session with `time_to < time_from` would
indicate a regression in the boundary-merge pivot. The locked
snapshot has zero such sessions.

## Implications for phase 4 (site-log gate)

Phase 4 of the design lands a `--use-new-synthesis` flag on
`tosGPS sitelog` / `tosGPS PrintTOS`. The sign-off there is the
golden-file diff against `data/sitelogs_archive/<STATION>_*.txt`.

For RHOF / VMEY / SKRO that diff must be empty.

For AUST / AKUR / REYK / HOFN the diff will be non-empty by definition. New
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
