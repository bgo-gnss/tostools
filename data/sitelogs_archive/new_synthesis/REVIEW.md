# Phase 5 sign-off — domain review of divergent-station synthesis

This bundle contains side-by-side legacy / new-synthesis output for
the four stations where the new `tostools.devices.station_sessions`
composer chain diverges from the legacy
`gps_metadata_qc.gps_metadata` chain. Phase 5 (flip the
`--use-new-synthesis` default in `tosGPS`) ships these behavior
changes to production, so this review is the gate.

For the design context, read first:

- `docs/architecture/synthesis-legacy-divergence.md` — the two
  legacy bugs and the boundary-merge pivot fix.

## How to review (silence = approval)

For each station, look at the `_table.colordiff` file — it's the
`tosGPS PrintTOS` view (the same rich table you see at the
terminal) with character-level coloring on the changes:

```bash
less -R data/sitelogs_archive/new_synthesis/AKUR_table.colordiff
# - lines in plain text are unchanged
# - red strikethroughs = removed from legacy
# - green = added in new
# - inline char-level = corrected values within a line
```

**If the new chain looks correct, do nothing.** The default state
of every row in the "Review status" table below is approval. Only
add an entry if a station's new output has an issue worth flagging
— then phase 5 won't flip the default for that station until the
issue is resolved.

## Files per station

| File | What it is |
|---|---|
| `<MARKER>_legacy_table.txt` | plain-text PrintTOS rendering, legacy chain |
| `<MARKER>_new_table.txt` | plain-text PrintTOS rendering, new chain |
| `<MARKER>_table.colordiff` | character-level colored diff between the two (view with `less -R`) |
| `<MARKER>_legacy.json` | raw `tosGPS sitelog --format json`, legacy chain |
| `<MARKER>_new.json` | raw `tosGPS sitelog --format json`, new chain |
| `<MARKER>_legacy_windows.txt` | compact `time_from → time_to | receiver | antenna` digest |
| `<MARKER>_new_windows.txt` | same digest, new chain |
| `<MARKER>_windows.diff` | unified diff between the two digests |

The `_table.colordiff` is the **primary review artifact** — it
matches what an operator sees in the terminal. The JSON +
windows files are reference detail.

Captured via JSON because the full IGS text-form renderer raises
pre-existing NoneType / empty-string errors on AUST / REYK / HOFN
that hit *both* chains identically (unrelated to the synthesis
change). Tracked separately; not in scope for phase 4/5.

## Caveat on the underlying JSON

The raw `device_sessions` JSON from the legacy chain contains
sessions with orphaned slots (e.g. `gnss_receiver` present but
`antenna: null`, or vice versa) for every divergent station.
`tosGPS PrintTOS` post-processes this and fills slots from adjacent
sessions before rendering — so the user-facing table is much closer
to the new chain than the raw JSON suggests. The divergence the
reviewer can actually see is the `_table.colordiff`. The JSON-level
divergence is what the new composer chain fixes upstream of the
renderer; consumers other than PrintTOS (e.g. `receivers cfg
reconcile`, future web UI) get the cleaner data directly.

## Per-station summary

| Station | Sessions (legacy → new) | What changed in the rendered table |
|---|---|---|
| AKUR | 5 → 6 | one added: the 2018-01-21 → 2018-01-30 receiver-swap gap window |
| AUST | 25 → 31 | seven added sub-windows + several `time_to` corrections (legacy had inverted ranges like `2001-03-26 → 2000-07-10`) |
| REYK | 16 → 16 | content-level changes (no row count delta) — one inverted legacy row, several boundary corrections |
| HOFN | 8 → 13 | five added sub-windows around firmware bumps; one row gets serial `1830199` populated where legacy showed `N/A` |

## AKUR (the simplest case)

The only visible change is one new row — the 9-day receiver-swap gap:

```
2018-01-21 → 2018-01-30  receiver=N/A  antenna=TRM29659.00/0220145519  radome=SCIS
```

Was the receiver swap on 2018-01-21 → 2018-01-30 indeed a real
~9-day window with the antenna in place? If yes → approve (do
nothing). If no → flag below.

## AUST (the busiest case)

Seven added sub-windows + corrections to inverted time ranges.
Legacy had rows like `2001-03-26 → 2000-07-10` (end before start)
— the new chain produces well-ordered ranges plus extra
sub-windows for periods legacy collapsed.

Pay attention to:

1. The 2000-07-06 → 2000-07-10 region: legacy had one row spanning
   the period; new splits it into three sub-windows. Were there
   genuinely three distinct receiver / antenna / firmware
   transitions in those four days?
2. The 2011-06-13 → 2011-06-21 and 2014-06-01 → 2014-09-30 added
   windows.
3. The 2014-09-30 → 2014-10-17, 2014-10-17 → 2016-02-12, and
   2019-01-20 → 2019-06-05 added windows.

## REYK

Same session count but content shifted (legacy had one
end-before-start row that the new chain replaces). Cross-check the
2014-2018 region in particular.

## HOFN

Added rows around 1999-07-31, 2008-01-17, 2010-03-26 → 2010-03-30,
2013-10-09 → 2014-02-18 — these are firmware transition boundaries
that legacy collapsed into the surrounding rows. Also, the
2014-02-18 → 2014-10-17 row gets the receiver serial `1830199`
populated where legacy showed `N/A`.

## How to act on issues

If a station's new output looks wrong:

1. Add an entry to "Review status" below — station, your initials,
   date, and a sentence about what's off.
2. Phase 5 will not flip the default for that station until the
   issue is resolved (a fix to the composer / slicer / pivot, then
   re-capture + re-review).

If everything looks correct: leave the table as-is. Phase 5 will
read the table's emptiness as approval and proceed.

## Review status

(Add a row only if you find an issue. Empty table = full approval.)

| Station | Reviewer | Date | Issue |
|---|---|---|---|
| AUST | BGO | 2026-05-18 | Over-splits at 2000-07-06 → 2000-07-08 (phantom boundary at 2000-07-07) and 2003-06-18 → 2003-06-21 (should be one session). **First case fixed by coalescing pass** (`2000-07-06→07` + `2000-07-07→08` merged because identical equipment). The 2003-06-18→19 split remains — receiver SN and firmware genuinely differ in TOS records, needs upstream data cleanup. |
| HOFN | BGO | 2026-05-18 | Over-splits in 2013-10-09 → Present period from firmware bumps + late-arriving SN boundaries. **Not fixed by coalescing pass** — TOS records different firmware versions and SN populations at those boundaries; synthesis can't merge across genuine attribute differences. Needs upstream TOS data cleanup. |

### Coalescing pass (`station_sessions(..., coalesce=True)`)

Implemented in `src/tostools/devices.py` after the pivot. Walks the
emitted sessions and merges any consecutive pair whose four subtype
slots (`gnss_receiver`, `antenna`, `radome`, `monument`) are
identical. Default `coalesce=True`; pass `False` to see raw atomic
sub-windows.

**What it catches:**

- AUST `2000-07-06 → 2000-07-07` + `2000-07-07 → 2000-07-08` →
  merged (identical equipment + offsets in both sub-windows).
- AUST `2011-06-21 → 2011-09-19` + `2011-09-19 → 2013-12-09` →
  merged (a similar phantom boundary that wasn't called out by the
  reviewer but coalesces correctly on the same rule).

Result: AUST snapshot drops from 31 → 29 sessions.

**What it does NOT catch (by design):**

- AUST `2003-06-18 → 2003-06-21` — receiver SN changes (25148 →
  25992) and firmware changes (7.29 → 7.19) between the two
  sub-windows. From TOS's perspective these are distinct sessions.
- HOFN `2013-10-09 → 2014-10-17` — firmware bumps (`3.01/6.2 →
  3.03/6.12`) and a serial-number population (`N/A → 1830199`).

For these, the underlying TOS data needs cleanup: someone added or
edited attribute periods at those dates even though the physical
equipment was unchanged. The synthesis chain can only render what
TOS records; merging across genuine attribute differences would
mask legitimate state.

**Recommended follow-up:** triage TOS data for AUST 2003-06 and
HOFN 2013-10 → 2014-10 — likely candidates for the
`tos audit apply` workflow once the patterns are identified.

## TOS data issues surfaced during review

(Not synthesis bugs — TOS data-modeling corrections. These don't
block phase 5 but are worth a future cleanup pass.)

- **AUST**: `antenna_offset_east = -0.0033` and
  `antenna_offset_north = -0.0056` are stored on the antenna entity
  but physically describe a monument eccentricity — they should
  live on the monument entity, not the antenna.
