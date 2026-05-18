# Phase 5 sign-off — domain review of divergent-station synthesis

This bundle contains side-by-side legacy / new-synthesis output for the
four stations where the new `tostools.devices.station_sessions`
composer chain diverges from the legacy `gps_metadata_qc.gps_metadata`
chain. Phase 5 (flip the `--use-new-synthesis` default in `tosGPS`)
ships these behavior changes to production, so this review is the
gate.

For the design context, read first:

- `docs/architecture/synthesis-legacy-divergence.md` — the two legacy
  bugs and the boundary-merge pivot fix.

## Files

For each station `<MARKER>` in `{AUST, AKUR, REYK, HOFN}`:

| File | Content |
|---|---|
| `<MARKER>_legacy.json` | `tosGPS sitelog <MARKER> --format json` |
| `<MARKER>_new.json` | `tosGPS --use-new-synthesis sitelog <MARKER> --format json` |
| `<MARKER>_legacy_windows.txt` | per-session digest (time_from → time_to + receiver/antenna model/serial) |
| `<MARKER>_new_windows.txt` | same digest from the new chain |
| `<MARKER>_windows.diff` | unified diff between the two digests |

We captured the JSON form rather than the full IGS site-log text
because the text renderer (`legacy/gps_metadata_functions.py`) raises
on AUST / REYK / HOFN with pre-existing NoneType / empty-string
errors that surface only on these messy stations. The errors hit
*both* chains identically, so they're unrelated to the synthesis
change. Tracked separately; not in scope for phase 4/5.

## Headline finding — legacy emits orphaned components

Across all four divergent stations, the legacy chain produces sessions
where the receiver slot is filled but the antenna slot is empty (or
vice versa) — even though the station physically had both installed
at the time. The new chain pairs them correctly.

Concrete examples below.

## AUST

| chain | sessions | inverted (time_to < time_from) | orphaned slot |
|---|---|---|---|
| legacy | 25 | several (counted in earlier compare) | extensive |
| new | 31 | **0** | 0 |

The new chain adds well-ordered sub-windows for every device
transition and assigns equipment to every session. Legacy's
position-wise zip produced bogus sessions and dropped equipment
assignments.

**Review checklist:**

1. Do the new 31 windows match the physical installation timeline
   for AUST?
2. Are receiver / antenna model + serial assignments correct in
   each window? (Cross-check against receiver health logs and field
   visit records.)
3. Were there genuinely overlapping installations during the
   2000-07 to 2003-06 period (~10 sub-windows in 3 years)? If yes,
   the new output reflects reality; if not, flag the slicer.

## AKUR

The most pedagogically clear case — legacy has the antenna installed
*alone* for the entire 24-year history, with no receiver paired in
any of those sessions:

```
legacy:
  2001-07-31 → open                    receiver=—              antenna=TRM29659.00
  2001-07-31 → 2015-12-21              receiver=TRIMBLE 4700   antenna=—            ← receiver alone
  2015-12-21 → 2018-01-21              receiver=NOV OEM638     antenna=—            ← receiver alone
  2018-01-30 → 2025-04-15              receiver=TRIMBLE NETR5  antenna=—            ← receiver alone
  2025-04-15 → open                    receiver=TRIMBLE NETR5  antenna=—            ← receiver alone

new:
  2001-07-31 → 2015-12-21              receiver=TRIMBLE 4700   antenna=TRM29659.00
  2015-12-21 → 2018-01-21              receiver=NOV OEM638     antenna=TRM29659.00
  2018-01-21 → 2018-01-30              receiver=—              antenna=TRM29659.00  ← 9-day swap gap
  2018-01-30 → 2023-09-07              receiver=TRIMBLE NETR5  antenna=TRM29659.00
  2023-09-07 → 2025-04-15              receiver=TRIMBLE NETR5  antenna=TRM29659.00  ← firmware bump
  2025-04-15 → open                    receiver=TRIMBLE NETR5  antenna=TRM29659.00
```

The legacy site log for AKUR was effectively unusable — five sessions
where every single equipment slot is wrong by omission. The new chain
produces six well-paired sessions plus correctly captures the
2018-01-21 → 2018-01-30 receiver-swap gap window (antenna present, no
receiver).

**Review checklist:**

1. Was AKUR's antenna `TRM29659.00 / 0220145519` indeed installed
   from 2001-07-31 through today?
2. Was the receiver swap on 2018-01-21 → 2018-01-30 indeed a
   ~9-day window with the antenna in place?
3. Was the 2023-09-07 boundary a firmware bump for receiver
   serial 4806K53395? (Check session 4 vs session 5 in `AKUR_new.json`
   — firmware versions differ.)

## REYK

| chain | sessions | inverted | orphaned slot |
|---|---|---|---|
| legacy | 16 | 1 known (REYK was original "REYK [13]: 2014-10-17 → 2013-05-02") | extensive — see diff |
| new | 16 | 0 | 0 |

Same orphan pattern as AKUR — legacy produces lots of "antenna only"
or "receiver only" sessions for periods where both were installed.
Session count happens to match because REYK's overlap pattern works
out arithmetically; content is very different.

## HOFN

| chain | sessions | inverted | orphaned slot |
|---|---|---|---|
| legacy | 8 | — | 5 of 8 sessions have one empty slot |
| new | 13 | 0 | 0 |

The boundary-merge captures additional firmware / version transitions
(1999-07-31, 2008-01-17, 2010-03-26, 2010-03-30, 2013-10-09,
2014-02-18) that legacy collapsed.

## How to review one station

```bash
cd data/sitelogs_archive/new_synthesis/

# 1. Glance at the diff
less AKUR_windows.diff

# 2. Look at the full new JSON for detail
jq '.device_sessions[0]' AKUR_new.json

# 3. Cross-check one window's equipment against TOS web UI / field log
#    Pick a window, find the device IDs, walk to the receiver / antenna
#    entity, verify model + serial + dates.

# 4. If the new chain is correct, the file is approved.
#    If something's wrong, write a note here in this file and flag the
#    specific window.
```

## What "approval" means for phase 5

For each station, the reviewer signs off that:

1. Window boundaries match real installation events (not synthetic
   noise from the slicer).
2. Equipment slots in each window reflect what was physically on
   the antenna at that time.
3. Receiver-swap gap windows (sessions with `gnss_receiver` slot
   absent) reflect real swap operations, not data-entry errors.

Once all four are approved, phase 5 can flip the
`--use-new-synthesis` default in `tosGPS` and these `<MARKER>_new.json`
files become the new reference golden files.

If any station's review surfaces an issue with the *new* chain (not
just a divergence from buggy legacy), that's a phase-3.5 follow-up:
amend the composer / slicer / pivot, re-capture, re-review.

## Review status

| Station | Status | Reviewer | Date | Notes |
|---|---|---|---|---|
| AKUR | pending | — | — | — |
| AUST | pending | — | — | — |
| REYK | pending | — | — | — |
| HOFN | pending | — | — | — |
