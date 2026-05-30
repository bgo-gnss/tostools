# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is **tostools**, a Python3 command-line toolkit for GPS/GNSS station metadata quality control and processing.

**Main Application**: `tosGPS` - GPS metadata quality control tool that queries TOS API and validates against RINEX files.

**Current Version**: v0.6.0 (Layers 1-6 attribute-dates audit + TOS read/write API client with move_device / add_maintenance_visit; v0.7 in progress: station verify/show/triage, tos contact subcommand, verify-from-rinex refactored into reusable audit module + --with-archive plumbing, legacy flat-arg CLI removed, `tos fleet triage` / `tos fleet status` fleet-wide orchestrators)

### Core Components

1. **Primary GPS Tools** (by Benedikt): GPS metadata QC, RINEX processing, station management
2. **Legacy TOS Integration** (by Tryggvi): Tools for querying TOS API for Icelandic weather/seismic stations

### Key Features

- **Clean Output**: Silent by default, verbose when needed
- **Rich Formatting**: Color-coded GPS station data visualization
- **GAMIT Integration**: Production-ready GPS processing format
- **IGS Compliance**: Professional site log generation with v2.0 standards
- **RINEX Processing**: Validation, brary
  correction, and format compliance

## Tool boundaries — `tos` vs `tosGPS`

The package ships **two CLI entry points** with a layered split. New verbs
land via this rule:

- **`tos` — the entity layer.** Any verb that takes an `id_entity` /
  `serial` / `marker` / location-name and returns TOS state in its
  **native shape** (raw attribute periods, joins, `id_attribute_value`,
  `id_entity_connection`) belongs here. Subtype-agnostic — the same
  verb works for `gnss_receiver`, `antenna`, `seismometer`, `monument`.
  Includes audits (`tos audit *`), writes (`tos audit apply`), and
  entity inspection (`tos device show / list`).

- **`tosGPS` — the GPS interpretation layer.** Any verb that **synthesizes
  TOS entities into a GPS-domain object** — a station "session"
  (receiver+antenna+radome tuple), a GAMIT `station.info` row, an IGS
  sitelog block, a RINEX-vs-TOS diff — belongs here. Calls *into* `tos`
  primitives and reshapes their output. Opinionated by what GPS
  processing pipelines expect.

The split is **layer-based, not domain-based**. A seismic-network verb
that returns raw entities still belongs in `tos`; a GPS-only verb that
returns raw entities also belongs in `tos`. The split is about
*output shape*, not the entity types involved.

Practical examples:

| Verb | Lives in | Why |
|------|----------|-----|
| `tos device show <id>` | `tos` | Raw attribute periods + raw joins |
| `tos device list --station SAVI` | `tos` | Raw entity table with `id_entity` column |
| `tos station show <STN>` | `tos` | Station identity + open attrs + joined children |
| `tos station verify <STN>` | `tos` | Pass/fail oracle; reuses raw audit reports |
| `tos audit fleet` | `tos` | Invariants over raw TOS state |
| `tos audit apply file.txt` | `tos` | Writes raw entities |
| `tosGPS PrintTOS SAVI` | `tosGPS` | GAMIT-formatted station sessions |
| `tosGPS sitelog SAVI` | `tosGPS` | IGS v2.0 site log synthesis |
| `tosGPS syncMeta` | `tosGPS` | TOS ↔ GAMIT station.info diff |

When a `tosGPS` verb needs to fetch an entity, it should *call into*
`tos` primitives (or the underlying `tostools.api.tos_client` /
`tostools.devices` helpers) rather than re-implement them.

### Shared filter set for entity-listing verbs

Any `tos` verb that produces a list of device entities should use the
standard filter helpers in `tostools.tos`:

- `add_device_filter_arguments(parser, with_date=True)` — adds
  `--subtype`, `--model`, `--status`, `--serial`, `--date` to a
  subparser. Pass `with_date=False` for verbs operating on data without
  time bounds.
- `apply_device_filters(rows, args)` — applies all five filters to
  enriched rows (shape: `subtype`, `model`, `status`, `serial`,
  `time_from`, `time_to`). AND'd; order preserved. Tolerates missing
  arg attributes (each treated as "no constraint").

Match semantics: `subtype` exact, `model` case-insensitive substring,
`status` exact, `serial` case-sensitive substring, `date` "device
present on date" (time_from ≤ date < time_to). See
`tests/test_tos_client.py::apply_device_filters` for the full pinning.

Verbs that should adopt these (and don't yet): the audit reporters that
emit device tables — `tos audit fleet`, `tos audit orphans`,
`tos audit timelines`, plus any future `tos device search` /
`tos station list`.

### Shared filter set for attribute-period verbs

Verbs that show TOS attribute periods (open or historical) should use:

- `add_attribute_filter_arguments(parser)` — adds `--code` (repeatable),
  `--value`, `--on-date`, `--suspicious` to a subparser.
- `apply_attribute_filters(periods, args)` — filters TOS attribute-value
  dicts (`code`, `value`, `date_from`, `date_to`). AND'd; order
  preserved; tolerates missing arg attributes.

Match semantics: `code` exact (any of listed values OR'd), `value`
case-insensitive substring, `on_date` "period active on date"
(date_from ≤ date < date_to), `suspicious` "date_from is the
fleet-wide cleanup-artifact date 2014-10-17" (see memory
`project_2014_10_17_metadata_cleanup_artifacts`). Pinning in
`tests/test_tos_client.py::apply_attribute_filters`.

Adopted by `tos device show` (--attributes and --attributes-history
sections both filter through this helper). Future candidates:
`tos audit attribute-dates`, `tos audit missing-attributes`, any new
fleet-wide attribute-inspection verb.

### Station triage orchestrator — `tos station triage`

`tos station triage <STATION>` is the single-command entry point for
the SAVI-style reconstruction workflow. It runs every available audit
against the station, aggregates findings into one ACTION-style file
consumable by `tos audit apply`, and writes it to
`data/triage/<station>/<station>_audit_<YYYYMMDD>.txt` by default
(configurable via `--out PATH`, or `--stdout` to skip disk).

All suggested ACTION lines are **commented out by default** — operator
opts IN by uncommenting + filling `<FILL_VALUE>` / `<FILL_DATE>`
placeholders. Sections are ordered by confidence:

  * **HIGH** — cleanup-artifact backdates (the fleet-wide 2014-10-17
    pattern, well-documented)
  * **MEDIUM** — missing required attributes with catalog defaults
  * **LOW** — missing required attributes with `<FILL>` placeholders
    (operator MUST replace before apply)

Implementation in `src/tostools/station_triage.py` —
`generate_station_triage()` calls the existing audit
entry points (`audit_station_missing_attributes`,
`audit_station_attribute_dates`) directly rather than re-implementing.
Each section's body is delegated to the originating audit module's
`format_triage_file()` so format-stability of those audits is
preserved.

Happy-path workflow (no AI required for routine stations):

```
tos station show HEDI                          # current state snapshot
tos station triage HEDI                        # generate triage file
$EDITOR data/triage/hedi/hedi_audit_*.txt      # uncomment / fill
tos audit apply <file>                         # dry-run
tos audit apply <file> --apply                 # commit
tos station verify HEDI                        # exit 0 confirms clean
git add <file> && git commit                   # provenance
```

Pinned by `tests/test_station_triage.py`,
`tests/test_station_verify.py`, `tests/test_station_show.py`.

### `tos station verify` — apply→verify oracle

`tos station verify <STATION>` re-runs every audit and exits with a
pass/fail signal:

  * `0`  clean — every audit ran and reported no violations
  * `1`  findings — at least one audit has surviving violations
  * `2`  failure — at least one audit raised (TOS lookup error,
    malformed catalog). Distinct from `findings` so cron / CI can
    distinguish "station needs work" from "oracle broken".

Reuses :func:`station_triage.generate_station_triage` as the
aggregator; the same sub-reports feed both the triage renderer and
this oracle. ``--no-suppressions`` bypasses the per-audit SUPPRESS
files (forwarded through the generator to both underlying audits).
``--json`` emits ``{station, station_id, status, exit_code, audits,
notes}``.

### `tos station show` — current-state snapshot

`tos station show <STATION>` is read-only inspection:

  * default: station identity (id, marker, name, status) + open
    attribute periods + currently-joined child devices
  * `--all`: adds attribute history (closed periods) + closed joins
    — mirrors the TOS web UI's *Saga eiginda tækis* +
    *Saga staðsetningar tækis* panels
  * `--device`: delegate to ``tos device list --station <STN>``
    (passes ``--all`` through)
  * `--json`: full payload shape

Reuses ``_render_show_open_attributes`` + ``_render_show_attribute_history``
from the device-show family — same suspicion coloring (2014-10-17
yellow, bilað/óvirkt red, closed dimmed, id_attribute_value cyan).

**For first-time operators**: see
`docs/tutorials/station-triage-tutorial.md` for a step-by-step
walkthrough including the catalog-ghost remapping table, the
split-monument Section 0 template, double-apply hazard recovery, and
a worked example using HEDI's committed triage files.

### Archive verification — `tos audit verify-from-rinex`

`tos audit verify-from-rinex --station X` cross-checks TOS state against
the cold RINEX archive. Walks
``<archive>/<YYYY>/<mon>/<STATION>/15s_24hr/{raw,rinex}/``, classifies
each file by receiver-brand family (`.sbf` → septentrio, `.T02` →
trimble_netr9, etc.), reports brand transitions + data gaps, and
cross-references against TOS's child-device joins. Deterministic,
primary-source-anchored — operators don't have to take anyone's word on
historical install dates.

**Archive root resolution** (`src/tostools/archive.py::cold_archive_prepath`):

  1. `--archive-root` CLI override
  2. Env var `TOSTOOLS_ARCHIVE_ROOT`
  3. `[archive_paths] cold_archive_prepath` in the shared
     `~/.config/gpsconfig/receivers.cfg` (same cfg the receivers package
     uses — single source of truth, no duplication)
  4. Probe `/mnt/rawgpsdata` then `/mnt_data/rawgpsdata`
  5. `FileNotFoundError` with a candidate list

Reusable helpers in `tostools.archive`: `cold_archive_prepath()`,
`classify_file_format()`, `walk_station_timeline()`,
`detect_brand_transitions()`, `detect_data_gaps()`,
`coalesce_brand_runs()` (rinex-format days absorbed into surrounding
brand when bracketed by the same brand; ambiguous spans surfaced
separately), `detect_rinex_only_spans()` (operationally important —
windows where raw is missing and only RINEX archived; useful "we're
losing raw" signal). Pinned by `tests/test_archive.py`.

**Cross-tool wiring**: `tosGPS syncMeta --type gamit-station-info ...
--with-archive` calls the same helpers and appends an "Archive evidence"
panel below the TOS-vs-REF session diff. Opt-in (off by default — archive
access is unreliable on offline / no-mount workflows). When set, every
station's comparison gains brand-timeline + transitions + gaps under the
existing diff, in one command, so operators get all three sources (TOS,
REF, ARCHIVE) without leaving the syncMeta flow. Same `--archive-root`
override applies; `--archive-min-gap-days` controls the gap threshold.

### Fleet ops — `tos fleet triage` and `tos fleet status`

Fleet-wide orchestrators that loop the single-station verbs over every
GNSS station in `stations.cfg` (`code_subtype == "geophysical"`).
Phase 4 of the station-triage sequence (Phases 1-3 = single-station
triage / verify / show).

  * `tos fleet status` — bulk verify oracle. No disk writes. Exit code
    mirrors `tos station verify`: 0 all clean / 1 any findings / 2 any
    audit failure. Default text output suppresses clean stations;
    `--show-clean` for the full table; `--json` for automation.
  * `tos fleet triage` — generate per-station triage files into
    `data/triage/<STN>/<STN>_audit_<YYYYMMDD>.txt`. Clean stations are
    **skipped by default** — `--include-clean` opts in to a full
    inventory. Same-day re-runs overwrite that day's file.

Both verbs share the standard filter set: `--include STN1 STN2`,
`--exclude STN3`, `--limit N` (test helper), `--stations-cfg PATH`,
`--catalog PATH`, `--no-suppressions`, `--with-archive` /
`--archive-root` / `--archive-min-gap-days` (verify-from-rinex
passthrough — slow at fleet scale, 173 archive walks).

Implementation in `src/tostools/fleet_ops.py`:
`enumerate_fleet_stations()` filters `enumerate_known_parents` to
`REAL_STATION_SUBTYPES`, applies include/exclude/limit, raises
`RuntimeError` if zero stations resolve (guards against the
infrastructure-only fallback when `stations.cfg` is missing).
`_iterate_fleet()` is the shared loop body — wraps
`generate_station_triage()` per-station, captures per-station
failures into the result, calls a side-effect callback (file write
for triage, no-op for status). Sequential by design (~5-15 min on a
warm cache for 173 stations); parallelism is a documented follow-up.
Pinned by `tests/test_fleet_ops.py`.

## Quick Start

### Environment Setup

```bash
mamba activate tostools
pip install -e .

# Install pre-commit hooks (recommended for development)
pip install pre-commit
pre-commit install
```

### Core Commands

```bash
# GPS station metadata (clean output for automation)
tosGPS PrintTOS RHOF --format table > data.csv
tosGPS PrintTOS RHOF --format rich      # Color-coded for manual QC
tosGPS PrintTOS RHOF --format gamit     # GPS processing format

# Site log generation with smart change detection
tosGPS sitelog RHOF --date-in-name --dir ./logs  # Creates organized structure
tosGPS sitelog RHOF | process_data.py           # Pipe to stdout for processing

# RINEX validation and correction
tosGPS rinex RHOF data/*.rnx --fix --backup

# Verbose output when needed
tosGPS --log-level INFO PrintTOS RHOF
tosGPS --debug-all --log-dir logs sitelog RHOF
```

### `tos` subcommands

```bash
tos station show <STN>             # current-state snapshot (with Recent vitjanir)
tos station triage <STN>           # combined triage file
tos station verify <STN>           # apply→verify oracle
tos device list --station <STN>    # currently-joined devices
tos device show --id <id>          # device detail (with Recent vitjanir)
tos visit list --station <STN>     # vitjanir attached to a station
tos visit list --device <id>       # vitjanir attached to a device
tos visit show <id_maintenance>    # one vitjun, full detail
tos audit apply <triage_file>      # dry-run; --apply to commit
tos fleet status                   # bulk verify oracle (exit 0/1/2)
tos fleet triage                   # generate per-station triage files
```

Legacy flat-arg form (`tos RHOF`, `tos -s SERIAL`, `tos --fdsnxml/--sc3ml`)
and the `json2ascii` / `metadata2rmq` console scripts were removed in
v0.7. The SC3/FDSN XML export pipeline is out of scope for this package.

### `tos visit` — vitjun (visit / maintenance) inspection

Vitjanir are entity-attached temporal records (`id_maintenance`
namespace, distinct from `id_entity` / `id_contact`). The schema is
generic on `id_entity`: stations and devices can both carry vitjanir.
In current GPS data every vitjun is station-attached (device-attached
vitjanir today are exclusively on meteorological sensors); Phase C of
the vitjanir expansion will start writing device-attached vitjanir for
the GPS lifecycle tracker (firmware bumps, sent-for-repair, etc.).

  * `tos visit list --station S` — vitjanir for a station, most-recent first
  * `tos visit list --device <id>` — vitjanir for a single device
  * `tos visit list --entity <id>` — escape hatch (any entity by id)
  * `tos visit show <id_maintenance>` — one vitjun, full detail
    including `maintenance_attribute_values` rows (`work` / `comment` /
    `remaining` / per-reason booleans + each row's
    `id_maintenance_attribute_value` for the writer's update path)

Standard filter set (read-only): `--type {on_site,remote}`,
`--reason CODE` (repeatable; `change` / `repairs` / `inspection` /
`improvements` / `other`), `--since DATE`, `--participants SUBSTR`,
`--open` / `--completed`. The `--reason` filter translates English
codes to the Icelandic display strings TOS emits on the list endpoint
(see `MAINTENANCE_REASON_DISPLAY` in `src/tostools/tos.py`).

`tos station show` and `tos device show --id <id>` surface a "Recent
vitjanir" section by default — every open visit + the 3 most-recent
closed. Station show aggregates from the station + currently-joined
devices with a `source` column for attribution (forward-compatible
with Phase C). `--no-visits` suppresses the section (and skips the
HTTP); `--all` on station show extends to full visit history.

## Architecture

### Current Structure

```
src/tostools/
├── tosGPS.py                    # Main GPS QC application (console script)
├── io/rich_formatters.py        # Enhanced table formatting with colors
├── legacy/                      # Original modules (production-ready)
│   ├── gps_metadata_functions.py  # Site log generation, station processing
│   ├── gps_metadata_qc.py         # Quality control and validation
│   └── gps_rinex.py               # RINEX file processing
├── api/tos_client.py            # TOS API client (class-based)
├── api/tos_writer.py            # Authenticated write client (JWT, PATCH/POST attribute values)
├── standards/igs_equipment.py   # IGS rcvr_ant.tab equipment name lookup (write-path conversion)
├── core/                        # Business logic & data models
├── rinex/                       # RINEX processing modules
├── io/                          # File I/O and formatting utilities
└── utils/logging.py             # Centralized logging system
```

### Key Data Sources

- **TOS API**: `https://vi-api.vedur.is:11223/tos/v1` (Icelandic weather/seismic stations)
- **Local databases**: `stations.list`, `station.info.sopac.apr05` (in tmp/)
- **Binary tools**: `bin/` contains RINEX conversion utilities

### Station Types

- GPS/GNSS stations (primary focus)
- Meteorological stations
- Geophysical/seismic stations (SIL network)
- Hydrological and remote sensing platforms

## TOS Write API

The `api/tos_writer.py` module provides authenticated write access to TOS. Key design decisions:

- **Dry-run by default**: `TOSWriter(dry_run=True)` logs but does not send mutating requests. Confirm payloads before setting `dry_run=False`.
- **Credential resolution**: constructor args → `TOS_USERNAME`/`TOS_PASSWORD` env vars → `[tos]` section in `database.cfg` → interactive prompt. Configure `[tos]` in `database.cfg` for non-TTY use.
- **Temporal model**: TOS is a temporal attribute store. Use PATCH to correct an existing value in-place; use POST to add a new period (e.g. instrument change). See `docs/architecture/tos-write-api.md` for full patterns.
- **IGS names**: TOS stores IGS rcvr_ant.tab format names (`"SEPT POLARX5"`, `"NONE"` for no radome). `tostools.standards.igs_equipment` converts from health-reported short names.

See `docs/architecture/tos-write-api.md` for the complete API reference, write patterns, and gotchas.

### Retrospective writes — triage files in git as canonical provenance

TOS exposes only `date_from` / `date_to` on attribute_value rows — there
is **no `created_at` / `modified_at`** field. A back-fill written today
but dated to 2007 is indistinguishable in TOS from a 2007-contemporaneous
record. The `id_attribute_value` sequence is a soft signal (higher =
newer, with the 2014-10-17 fleet bulk-load at id_av ≈ 32000–35000 as a
historical inflection point), but not authoritative.

**Convention**: every retrospective TOS write goes through a committed
triage file at the repo root (`<station>_*.txt`), processed by
`tos audit apply`. The triage file format (Header / Known ids / STEP +
Run/adjust / Verification — see `savi.txt` as reference) is
self-documenting, and the commit log gives the apply date. Triage
files are the canonical audit trail for back-fills, since TOS itself
doesn't track this.

When investigating "was this value contemporaneous or back-filled?":
1. Check `id_attribute_value` against the rough threshold for that era
2. `git log -- <station>_*.txt` for triage files touching the entity
3. The ACTION lines in matching triage files show exactly what was
   written and the cited evidence

Future tooling (project-todo): `tos device show --highlight-since
<id_av>` will surface this visually.

## Development Workflow

### Code Quality

```bash
# Local testing (matches CI pipeline)
ruff check src/
black --check src/
pytest tests/ -v
python scripts/update_standards.py --validate-only  # GPS/GNSS standards compliance
```

### CI/CD Pipeline

- **GitHub Actions**: `.github/workflows/ci.yml`
- **Python versions**: 3.8 through 3.13 compatibility
- **Quality checks**: ruff linting, black formatting, pytest testing
- **Standards compliance**: GPS/GNSS standards validation
- **Package validation**: Build and twine checks on master branch
- **Pre-commit hooks**: Automated standards checking before commits

### Dependencies

Key dependencies managed through `pyproject.toml`:

- `requests` (TOS API), `pandas` (data processing), `rich` (formatting)
- `gtimes` (GPS time), `pyproj` (coordinates), `fortranformat` (RINEX)

## Important Technical Notes

### RINEX Format Requirements ⚠️

- **FORTRAN77 column formatting** - spaces vs tabs matter critically
- **Exact column positions** - extra spaces break parsing
- **Preserve alignment** when editing RINEX headers

### Output Streams Architecture

- **stdout**: Program data (tables, site logs) - perfect for piping
- **stderr**: Status messages, progress info, errors
- **Files**: Comprehensive logging with `--log-dir logs`

### GPS Standards Compliance

- **IGS v2.0**: Nine-character IDs, proper coordinate formats (DMS)
- **Country translation**: Iceland→ISL, NEI→NO, JÁ→YES with fallback handling
- **Equipment tracking**: Integration with GAMIT session history

## Current Capabilities (v0.2.6)

### ✅ Production Ready

- **Safe Update System**: Enterprise-grade reference data updates with multiple safety layers
- **Smart Site Log Generation**: IGS v2.0 compliant with automatic change detection and organized directory structure
- **Intelligent Change Detection**: Skips file creation when no meaningful changes detected, perfect for automation
- **Clean Terminal Output**: Minimal stderr messages optimized for cron jobs and automated workflows
- **Rich Table Formatting**: Color-coded GPS data with optimal spacing
- **GAMIT Integration**: Robust data validation prevents processing crashes
- **RINEX Processing**: Validation, correction, and format compliance

### 🛡️ Safe Update System Features

- **Fresh Download Verification**: Always uses latest server data with integrity validation
- **Automatic Backup System**: Timestamped backups with 10-version retention policy
- **Change Verification**: Ensures only intended stations are modified, preserves file integrity
- **Working Copy Isolation**: Safe editing environment prevents corruption of originals
- **Pre-Upload Validation**: Comprehensive format and content checks before remote changes
- **Rollback Capability**: Instant restoration from any backup version with full metadata
- **Dry-Run Mode**: Complete workflow simulation without actual uploads for safe testing
- **Production Logging**: Structured logging for monitoring, alerting, and operational visibility

### ⚠️ Known Issues & TODOs

- **Contact Management**: Hardcoded IMO fallback needs architectural review
- **Group Header Alignment**: Minor fine-tuning needed in rich formatter
- **CLI Feature Gaps**: Missing `--no-static`, `--contact` flags
- **Standards Documentation**: Need comprehensive GPS/GNSS standards repository

## Future Development Priorities

### Devices §3 refactor status

Phases 1–5 of the `devices` refactor are complete. The new
`devices.station_sessions` composer chain is now the default
synthesis path in `tosGPS PrintTOS` / `tosGPS sitelog` /
`tosGPS rinex`. Pass `--use-legacy-synthesis` for the old chain
(deprecated, slated for removal after production confidence
builds). See `docs/architecture/synthesis-legacy-divergence.md`
for the full picture; `data/sitelogs_archive/new_synthesis/`
holds the per-station review bundle.

### Next Phase Tasks

1. **TOS data cleanup for noisy attribute periods**: triage AUST
   2003-06 (receiver SN + firmware drift) and HOFN 2013-10 → 2014-10
   (firmware bumps + late-arriving SN) — both surface as residual
   over-splits in the new chain because TOS records genuine
   attribute differences at boundaries that don't reflect physical
   equipment changes. Candidates for `tos audit apply` corrections
   once the pattern is mapped.
2. **Fix legacy `site_log()` non-determinism**: pre-existing race /
   iteration-order bug in `legacy/gps_metadata_functions.py:site_log`
   produces different antenna-serial-number output on consecutive
   runs (observed on SJUK; flakes both chains since IGS text path
   bypasses the synthesis output). Goes away when legacy is removed.
3. **Wire `--push-tos` Pattern 2 / Pattern 4** (in `receivers`
   package, separate repo): underlying writer support is in place
   (`TOSWriter.transition_attribute_value` +
   `upsert_attribute_value(..., date_hint=...)`); the reconcile
   dispatcher just needs to call them when the diff shape demands.
4. **Device entity writes for `--push-tos`** (also in `receivers`):
   currently station-only. Receiver model / serial / firmware
   require resolving the gnss_receiver child entity —
   `devices.find_device(client, serial=..., subtype=...)` is the
   lookup primitive.
5. **Remove legacy synthesis after burn-in**: once
   `--use-legacy-synthesis` has gone unused in production for a
   reasonable interval, delete `gps_metadata_qc.gps_metadata`,
   `gps_metadata_qc.get_device_history`,
   `gps_metadata_qc.device_attribute_history`, and the
   `TOSClient.get_complete_station_metadata` fallback path.
6. **Web/phone interface**: CLI is the primary interface; a REST
   API wrapper (in `receivers`) will serve web and mobile UIs.

### Long-term Architecture

- **Modular Migration**: Gradual transition from legacy/ to modular components
- **API Enhancement**: Improve TOS API integration and error handling
- **Standards Automation**: Automated sourcing and storage of GPS standards
- **Testing Enhancement**: Comprehensive validation framework

## TODO Comment System

The codebase uses structured TODO comments for tracking technical debt:

- **FIXME**: Critical bugs needing immediate attention
- **TODO**: Features and improvements to implement
- **HACK**: Temporary solutions needing proper implementation
- **REVIEW**: Code sections needing architectural review
- **WARNING**: Important constraints and gotchas

Integration with VS Code Todo Tree and Neovim todo-comments.nvim available.

---

## Quick Reference

### Project Status: **Active Development** (v0.6.0)

### Main Focus: **TOS read/write integration and GPS station metadata management**

### Architecture: **Legacy modules (stable) + Modular components (active development)**

### Key Strength: **Automated workflows with intelligent change detection and clean output**

---

_Last updated: 2026-05-28_

