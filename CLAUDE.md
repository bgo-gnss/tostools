# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is **tostools**, a Python3 command-line toolkit for GPS/GNSS station metadata quality control and processing.

**Main Application**: `tosGPS` - GPS metadata quality control tool that queries TOS API and validates against RINEX files.

**Current Version**: v0.3.5 (TOS read/write API client with IGS equipment standard support)

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

### Legacy TOS Tools

```bash
tos vadla -o json    # Original TOS API client (Tryggvi)
json2ascii input.json output.txt
metadata2rmq
```

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

### Next Phase Tasks

1. **Retrofit `_dispatch_decommission` + `display_device_record`** (devices §3 phase 3): both already work, but they predate the `devices` primitives. Refactor to call `devices.decommission_device` / `devices.attribute_periods` so the apply path and the inspection path share the same code with the new composers/composites.
2. **Adapter + `--use-new-synthesis` flag** (devices §3 phase 4): write `_sessions_to_legacy_dict(station_sessions(...))`; gate `tosGPS PrintTOS` and `tosGPS sitelog` behind the flag. Sign-off: site log diff against `data/sitelogs_archive/RHOF_*.txt`. For AUST/AKUR/REYK/HOFN expect non-empty diff (see `docs/architecture/synthesis-legacy-divergence.md`) — capture new golden files with domain-expert review.
3. **Flip flag default + deprecate legacy synthesis** (devices §3 phase 5).
4. **Wire `--push-tos` Pattern 2 / Pattern 4** (in `receivers` package, separate repo): the underlying writer support is in place (`TOSWriter.transition_attribute_value` + `upsert_attribute_value(..., date_hint=...)`); the reconcile dispatcher just needs to call them when the diff shape demands it.
5. **Device entity writes for `--push-tos`** (also in `receivers`): currently station-only. Receiver model/serial/firmware require resolving the gnss_receiver child entity — `devices.find_device(client, serial=..., subtype=...)` is the lookup primitive.
6. **Web/phone interface**: CLI is the primary interface; a REST API wrapper (in `receivers`) will serve web and mobile UIs.

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

### Project Status: **Active Development** (v0.3.5)

### Main Focus: **TOS read/write integration and GPS station metadata management**

### Architecture: **Legacy modules (stable) + Modular components (active development)**

### Key Strength: **Automated workflows with intelligent change detection and clean output**

---

_Last updated: 2026-05-05_

