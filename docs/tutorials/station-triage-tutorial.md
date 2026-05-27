# Station Triage Tutorial — your first solo run

This walks you end-to-end through fixing a single GPS station's TOS
metadata using `tos station triage`. Aimed at someone who has never
done this before. After reading, you should be able to take an
unfamiliar station from "looks broken in `tosGPS PrintTOS`" to
"everything green in the audits" in about 30 minutes.

> Worked example: HEDI station, reconstructed on 2026-05-26.
> Reference: `data/triage/hedi/` in the repo holds the actual triage
> files we used. Read alongside this tutorial.

## What you'll need

| Thing | How to check |
|---|---|
| Python env with `tostools` installed | `tos --help` runs |
| TOS credentials | env vars `TOS_USERNAME` + `TOS_PASSWORD`, OR `[tos]` in `~/.config/database.cfg`, OR interactive prompt at apply time |
| Read access to the cold RINEX archive | `ls /mnt_data/rawgpsdata/` returns years (used by `verify-from-rinex`, optional but useful) |
| A station that needs fixing | Run `tos audit attribute-dates <STN>` — any violation means it needs triage |

If you don't have any of those, fix that first (the GPS Library hub
in the vault has setup notes).

## The big picture

TOS stores GPS station metadata as a **temporal attribute store**.
Three things you need to internalize:

1. **Entities** — stations, monuments, receivers, antennas. Each has
   an `id_entity` integer FK.
2. **Attributes** — code/value pairs with a `date_from` / `date_to`
   bounding their validity period. The same entity has many
   attributes; the same attribute code can appear multiple times for
   one entity (different periods).
3. **Joins** — parent-child connections (monument 5107 → station 4316)
   also with `time_from` / `time_to`. Devices "move" by closing one
   join and opening another at the same date.

Routine TOS-data problems are:
- Cleanup-artifact dates (the 2014-10-17 fleet bulk-load issue)
- Split-monument workaround entities (SOPAC-convention 2006-2014 era)
- Missing required attributes
- Wrong attribute codes (catalog declares `infrastructure_type` but
  TOS uses `model` — see catalog ghost remapping below)

The `tos station triage` orchestrator catches the first three
mechanically. The fourth + structural decisions are operator-knowledge.

## The 6-step workflow

```bash
# Pick a station that needs fixing.
STN=HEDI

# 1. Generate a triage file
tos station triage $STN
# → writes data/triage/hedi/hedi_audit_<YYYYMMDD>.txt
# All suggested ACTIONs are commented out by default. The orchestrator
# tells you the path it wrote to + how many findings.

# 2. Edit the file
$EDITOR data/triage/hedi/hedi_audit_*.txt
# (see "What to edit" below)

# 3. Dry-run — no writes go out
tos audit apply data/triage/hedi/hedi_audit_*.txt
# Look at the "Summary: N ok, 0 deferred, M failed" line. If M > 0,
# fix the failures BEFORE applying. The failed lines tell you why.

# 4. Apply — commits writes to TOS
tos audit apply data/triage/hedi/hedi_audit_*.txt --apply
# Run ONCE. See "Double-apply hazard" below for why.

# 5. Verify
tos audit attribute-dates $STN          # should be CLEAN
tos audit missing-attributes $STN       # remaining LOW-confidence items
tos audit verify-from-rinex --station $STN
tosGPS PrintTOS $STN                    # session table sanity

# 6. Commit triage to git for provenance
git add data/triage/hedi/
git commit -m "fix HEDI: cleanup-artifact backdates + split-monument + attrs"
git push
```

## What to edit (step 2 in depth)

Open the auto-generated file in your editor. It has 3 sections by
default:

### Header
Just metadata + how-to-run hints. Don't edit.

### Section: suspicious attribute dates (HIGH confidence)
The audit found N entities with `date_from=2014-10-17` (the fleet
bulk-load artifact). Each gets a commented suggestion like:
```
#ACTION 4572 patch-attribute-date serial_number 2014-10-17 2006-06-29
```

**Uncomment all of these unless you know they're wrong.** The
`2006-06-29` was derived from the entity's other open attributes
(lat/lon/marker dates, join time_from). This is the safest, most
mechanical fix.

### Section: missing required attributes (MEDIUM/LOW)
Catalog says these required attributes are missing. Three groupings
emerge:

**MEDIUM — concrete value already filled in.** Uncomment if the
suggested value is right. Examples (from HEDI):
```
#ACTION 4676 add-attribute status virkt 2012-06-27
#ACTION 4676 add-attribute antenna_offset_north 0.0 2012-06-27
#ACTION 4905 add-attribute GPS true 2012-06-27
#ACTION 4905 add-attribute http_port 80 2012-06-27
```

Catalog typo gotcha: the catalog says default `status = virk` but TOS
requires `virkt`. The orchestrator now writes `virkt` directly. If
you see `virk` in an auto-generated file, change it to `virkt`.

**LOW — `<FILL_VALUE>` placeholder.** Operator MUST supply the value.
Examples:
```
#ACTION 4316 add-attribute date_start <FILL_VALUE> start
#ACTION 4905 add-attribute manufacturer <FILL_VALUE> 2012-06-27
```

If you don't know the value, leave the line commented and come back
later. Applying with `<FILL_VALUE>` un-replaced will error out
("placeholder not replaced").

**Catalog ghost codes** — three audit-suggested codes that DON'T
exist in TOS schema and will fail with "unknown attribute code":

| Audit suggests | Use this instead |
|---|---|
| `infrastructure_type` (default `GPS stál-fjórfótur`) | `model` |
| `infrastructure_subtype` (default `GPS undirstaða`) | `subtype` |
| `visit_class` (default `B`) | `operational_class` |

Replace those lines with the working code+value, e.g.:
```
ACTION 5107 add-attribute model 'GPS stál-fjórfótur' 2012-06-27
ACTION 5107 add-attribute subtype 'GPS undirstaða' 2012-06-27
ACTION 4316 add-attribute operational_class B start
```

The orchestrator doesn't yet do this remapping (queued as a future
improvement); for now you do it.

### Date tokens — `start` and `now`

You'll see lines like `ACTION 4316 add-attribute operational_class B
start`. The `start` is a token that resolves at apply time to the
entity's earliest known date (earliest non-bulk-load attribute date,
falling back to the open join's time_from). For HEDI station 4316
that's `2006-06-29`.

`now` resolves to today's UTC date. Useful for ACTION lines you're
writing today (e.g. retire a device today: `decommission now`).

Both tokens work in: `add-attribute`, `patch-attribute-date`,
`patch-join-date`, `move`, `decommission`, `create-join`, `fill-gap`.

## Section 0 — split-monument cleanup (manual addition)

The orchestrator doesn't yet detect the SOPAC-convention
split-monument pattern. Many pre-2014 IMO stations have **two**
monument entities — one per major session — each carrying the
per-session height as a misplaced `antenna_height`. You'll see this
if `tos device list --station X --all` returns two monuments with
serials like `monument-X-YYYYMMDD`.

If your station has this pattern, paste this template at the TOP of
the triage file as a new "Section 0", with values filled in:

```
# Section 0: split-monument cleanup (manual — orchestrator doesn't detect)

# Adjust the placeholders:
#   <OLD_MON>          = older monument id (e.g. 5106)
#   <CUR_MON>          = current monument id (e.g. 5107)
#   <FOUNDING_DATE>    = station founding date (e.g. 2006-06-29)
#   <TRANSITION_DATE>  = equipment-change date (e.g. 2012-06-27)
#   <conn_id>          = current monument's open SAVI join (from `device show`)
#   <closed_conn_id>   = old monument's closed SAVI join

# A. Retire old monument as a workaround entity
ACTION <OLD_MON> add-attribute status virkt <FOUNDING_DATE>
ACTION <OLD_MON> decommission <TRANSITION_DATE>

# B. Move old monument to Hent (discard location, id=14)
ACTION <OLD_MON> create-join 14 <TRANSITION_DATE>

# C. Backdate current monument's station join to founding date
ACTION <CUR_MON> patch-join-date <conn_id> time_from <FOUNDING_DATE>

# D. Delete the closed-historical SAVI join on old monument
ACTION <OLD_MON> delete-join <closed_conn_id>

# E. Set monument_height = PERMANENT value on current monument
ACTION <CUR_MON> add-attribute monument_height <PERMANENT_VALUE> <FOUNDING_DATE>

# F. Add antenna_height (per-session OFFSET) to each antenna
ACTION <ANTENNA_OLD_ERA> add-attribute antenna_height <OFFSET_1> <SESSION_START_1>
ACTION <ANTENNA_NEW_ERA> add-attribute antenna_height <OFFSET_2> <SESSION_START_2>
```

The `monument_height` is one fixed value across the station's
lifetime (the actual physical height of the monument structure).
The `antenna_height` is the per-session offset from monument top to
antenna phase center reference. They sum to the total ARP-to-marker
height (the RINEX header value, or what GAMIT solves for).

For HEDI:
- Total height per era (from GAMIT): 0.914 (2006-2012) + 0.897 (2012-now)
- We picked monument_height = 0.897 (current value)
- Antenna offsets: 0.017 (AERAT era, 17mm lift) + 0.000 (NETR9 era, flush)

## After --apply: secondary cleanup pass

Often the first apply leaves some attribute-date inconsistencies:
attributes were written at the equipment-change date but the
monument's join is now backdated to founding date. The audit will
flag them. Fix with a small follow-up triage:

```
# In a new file e.g. data/triage/hedi/hedi_5107_followup_<DATE>.txt:
ACTION <CUR_MON> patch-attribute-date model <TRANSITION_DATE> start
ACTION <CUR_MON> patch-attribute-date serial_number <TRANSITION_DATE> start
ACTION <CUR_MON> patch-attribute-date antenna_offset_north <TRANSITION_DATE> start
ACTION <CUR_MON> patch-attribute-date antenna_offset_east <TRANSITION_DATE> start

# For date_start: both VALUE and date_from need patching
ACTION <CUR_MON> patch-attribute-value date_start <TRANSITION_DATE> '<FOUNDING_DATE> 00:00:00'
ACTION <CUR_MON> patch-attribute-date date_start <TRANSITION_DATE> start
```

## Gotchas

### Double-apply hazard

Don't re-run `tos audit apply <file> --apply` after a successful
landing. The second run's `decommission` will close the open join
that the first run's `create-join` opened, then create another fresh
one — leaving a duplicate. Fix: `ACTION <id> delete-join
<zero_duration_conn>` to remove the artifact.

Symptom: `tos device show <id> --list` shows two joins to Hent, one
closed 0-duration + one open.

### TOS auth expires

JWT tokens have ~hour TTL. If apply fails with 401 ("invalid token"),
the writer auto-refreshes (retries once). If THAT fails too,
re-export `TOS_USERNAME`/`TOS_PASSWORD` or just run apply again — a
fresh process gets fresh credentials.

### Catalog vs TOS divergence — false positives

After applying, `tos audit missing-attributes <STN>` may still flag:
- `infrastructure_type` (you wrote `model` — equivalent in TOS)
- `infrastructure_subtype` (you wrote `subtype`)
- `visit_class` (you wrote `operational_class`)

These are false positives. The audit doesn't know about the
catalog-vs-TOS code remapping. Ignore them — the underlying
attribute is already correct in TOS.

## Reference: ACTION verbs

| Verb | Shape | Use |
|---|---|---|
| `add-attribute` | `<id> add-attribute <code> <value> <date_from>` | Set a missing attribute |
| `patch-attribute-date` | `<id> patch-attribute-date <code> <old_date> <new_date>` | Backdate (e.g. 2014-10-17 → install date) |
| `patch-attribute-value` | `<id> patch-attribute-value <code> <date_anchor> <new_value>` | Fix a wrong value (history-destructive) |
| `move` | `<id> move <to_parent_id> <date>` | Close open join + open new (e.g. device moves to warehouse) |
| `create-join` | `<id> create-join <parent_id> <date_from> [<date_to>]` | Open a new parent join |
| `patch-join-date` | `<id> patch-join-date <conn_id> time_from\|time_to <new_date>` | Move a join's date |
| `decommission` | `<id> decommission <date>` | Close open join + transition status to óvirkt |
| `delete-join` | `<id> delete-join <conn_id>` | Permanently remove a join (artifacts only) |
| `delete-attribute-value` | `<id> delete-attribute-value <id_av>` | Permanently remove an attribute_value (artifacts only) |
| `fill-gap` | `<id> fill-gap <parent_id> <date_from> <date_to>` | Backfill a closed historical join |
| `change-subtype` | `<id> change-subtype <code>` | Change entity_subtype (admin) |
| `defer` | `<id> defer` | No-op placeholder (review later) |

## Reference: where things live

| Thing | Location |
|---|---|
| Auto-generated triage files | `data/triage/<station>/` |
| Catalog (attribute schema) | `data/attribute_codes.yaml` |
| Cold RINEX archive | `/mnt_data/rawgpsdata/<YYYY>/<mon>/<STN>/15s_24hr/` |
| Project tutorials (this file) | `docs/tutorials/` |
| Architecture docs | `docs/architecture/` |
| Existing commit messages | `git log --oneline data/triage/` for prior fixes |

## Where to go next

- **`docs/architecture/tos-write-api.md`** — the write API design,
  data model, write patterns. Read if you're going to write code, not
  just operate.
- **Vault hub** — `2.Areas/VI_GPS_Library/1776347706-gps-library-ecosystem-hub.md` —
  cross-package context, design notes, session logs.
- **CLAUDE.md** in the project root — references everything, including
  the retrospective-writes-as-provenance convention.

## Worked example — read the HEDI files

The repo committed all four HEDI triage files (`data/triage/hedi/`).
They're the canonical worked example. Read them in order:

1. `hedi_audit_20260526.txt` — initial orchestrator output + operator-added
   Section 0 + uncommented ACTIONs
2. `hedi_5107_dates_dedupe_20260526.txt` — post-apply alignment pass
3. `hedi_date_start_continuity_20260526.txt` — final touch-ups

Plus the SAVI references in `data/triage/savi/`.

The commit messages (`git log --oneline -- data/triage/`) tell the
story of what happened when.
