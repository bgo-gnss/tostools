# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is **tostools**, a Python3 command-line toolkit for GPS/GNSS station metadata quality control and processing.

**Main Application**: `tosGPS` - GPS metadata quality control tool that queries TOS API and validates against RINEX files.

**Current Version**: v0.2.5 (Enhanced Site Log Generation with Smart Change Detection)

### Core Components
1. **Primary GPS Tools** (by Benedikt): GPS metadata QC, RINEX processing, station management
2. **Legacy TOS Integration** (by Tryggvi): Tools for querying TOS API for Icelandic weather/seismic stations

### Key Features
- **Clean Output**: Silent by default, verbose when needed
- **Rich Formatting**: Color-coded GPS station data visualization
- **GAMIT Integration**: Production-ready GPS processing format
- **IGS Compliance**: Professional site log generation with v2.0 standards
- **RINEX Processing**: Validation, correction, and format compliance

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

## Current Capabilities (v0.2.5)

### ✅ Production Ready
- **Smart Site Log Generation**: IGS v2.0 compliant with automatic change detection and organized directory structure
- **Intelligent Change Detection**: Skips file creation when no meaningful changes detected, perfect for automation
- **Clean Terminal Output**: Minimal stderr messages optimized for cron jobs and automated workflows
- **Rich Table Formatting**: Color-coded GPS data with optimal spacing
- **GAMIT Integration**: Robust data validation prevents processing crashes
- **RINEX Processing**: Validation, correction, and format compliance

### ⚠️ Known Issues & TODOs
- **Contact Management**: Hardcoded IMO fallback needs architectural review
- **Group Header Alignment**: Minor fine-tuning needed in rich formatter
- **CLI Feature Gaps**: Missing `--no-static`, `--contact` flags
- **Standards Documentation**: Need comprehensive GPS/GNSS standards repository

## Future Development Priorities

### Next Phase Tasks
1. **Standards Documentation System**: Create local repository of ITRF/IGS/EPN standards
2. **Period Filtering**: Add `--date-from --date-to` flags for session filtering  
3. **Project Cleanup**: Organize tmp/ directory and improve file structure
4. **Contact System Review**: Resolve architectural issues in contact management

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

### Project Status: **Production Ready** (v0.2.5)
### Main Focus: **GPS metadata quality control with smart site log generation and change detection**
### Architecture: **Legacy modules (stable) + Modular components (active development)**
### Key Strength: **Automated workflows with intelligent change detection and clean output**

---

*Last updated: 2025-09-04 (Enhanced Site Log Generation with Smart Change Detection)*