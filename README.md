# tostools

**GPS Metadata Quality Control & TOS API Toolkit**

A Python3 command-line toolkit for GPS/GNSS station metadata quality control, RINEX processing, and TOS API integration. Combines GPS station validation tools with Icelandic weather/seismic station queries.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## 🚀 Quick Start

```bash
# Install in development mode
mamba activate tostools  # or conda activate tostools
pip install -e .

# GPS station metadata with rich formatting
tosGPS PrintTOS RHOF

# Generate GAMIT processing file
tosGPS PrintTOS RHOF REYK --format gamit > stations.info

# Validate RINEX files
tosGPS rinex RHOF data/RHOF*.rnx

# Generate IGS-compliant site log with change detection
tosGPS sitelog RHOF --auto-filename --date-in-name --dir ./sitelogs

# Synchronize and compare reference data (safe update system)
tosGPS syncMeta --type gamit-station-info RHOF --update --dry-run      # Test mode
tosGPS syncMeta --type gamit-station-info RHOF --update  # Safe production (interactive by default)

# Validate GPS/GNSS standards compliance
python scripts/update_standards.py --validate-only
```

## 📋 Table of Contents

- [Features](#-features)
- [Installation](#-installation)
- [GPS Tools](#-gps-tools)
- [TOS API Tools](#-tos-api-tools)
- [Output Formats](#-output-formats)
- [Examples](#-examples)
- [Architecture](#-architecture)
- [Development](#-development)
- [GPS/GNSS Standards](#gpsgnss-standards)
- [License](#-license)

## ✨ Features

### 🛰️ GPS Metadata Quality Control
- **Rich Visual Display**: Color-coded equipment tables optimized for manual QC
- **GAMIT/GLOBK Integration**: Production-ready format for GPS processing workflows  
- **RINEX Validation**: Compare RINEX headers against TOS metadata
- **Site Log Generation**: IGS-compliant site logs with complete station history and automatic change detection
- **Reference Data Management**: Automated fetching and comparison with external datasets
- **Robust Data Validation**: Prevents processing crashes with smart error handling

### 🌐 TOS API Integration
- **Station Search**: Query Icelandic weather/seismic stations by name or ID
- **Equipment History**: Complete device and sensor change tracking
- **Multiple Domains**: Meteorological, geophysical, hydrological stations
- **Contact Management**: Owner and operator information in multiple languages

### 🎨 Professional Output
- **Rich Tables**: Color-coded equipment groups with proper alignment
- **Multiple Formats**: Table, JSON, GAMIT, rich visual displays
- **Clean Data Export**: Perfect for automation and data pipelines
- **Selective Display**: Show only the data sections you need

### 📐 GPS/GNSS Standards Compliance
- **IGS v2.0 Site Logs**: Complete compliance with International GNSS Service standards
- **RINEX Format Support**: Multi-version support (v2/3/4) with FORTRAN77 format preservation
- **GAMIT/GLOBK Integration**: Fixed-width station.info format for GPS processing workflows
- **ITRF Coordinates**: Proper reference frame handling and transformations
- **Automated Validation**: Built-in standards compliance checking and validation

## 🔧 Installation

### Prerequisites
- Python 3.8+ (supports up to Python 3.13)
- Recommended: Mamba or Conda for environment management

### Environment Setup
```bash
# Create dedicated environment
mamba create -n tostools python=3.11
mamba activate tostools

# Install from repository
git clone https://github.com/bennigo/tostools.git
cd tostools
pip install -e .

# Or install development dependencies
pip install -e ".[dev]"
```

### Dependencies
The package automatically installs:
- `requests` - TOS API communication
- `pandas` - Data processing and analysis
- `tabulate` - Simple table formatting  
- `rich` - Enhanced terminal output with colors
- `gtimes` - GPS time functions
- `python-dateutil` - Date/time utilities
- `fortranformat` - RINEX FORTRAN77 parsing
- `pyproj` - Coordinate transformations
- `argparse-logging` - Enhanced CLI logging

## 🛰️ GPS Tools

### PrintTOS - Station Metadata Display

**Rich Visual Output** (Default - Perfect for Manual QC)
```bash
# Enhanced color-coded tables
tosGPS PrintTOS RHOF                    # Rich format (default)
tosGPS PrintTOS RHOF --format rich      # Explicit rich format

# Show only specific sections
tosGPS PrintTOS RHOF --show-history     # Device history only
tosGPS PrintTOS RHOF --show-static --show-contacts  # Static + contacts

# Detailed contact information  
tosGPS PrintTOS RHOF --contact          # English/Icelandic details
```

**Clean Data Export** (Perfect for Automation)
```bash
# Simple table format
tosGPS --log-level ERROR PrintTOS RHOF --format table > station_data.csv

# JSON for data processing
tosGPS PrintTOS RHOF --format json | jq .

# Multiple stations
tosGPS PrintTOS REYK HOFN RHOF --format table
```

**GAMIT/GLOBK Processing**
```bash
# Generate stations.info file for GPS processing
tosGPS PrintTOS RHOF REYK --format gamit > stations.info

# Includes robust data validation and error handling
# Skips invalid sessions while preserving valid ones
# Critical data issues visible at ERROR logging level
```

### RINEX Validation

```bash
# Basic validation (results to stdout)
tosGPS rinex RHOF data/RHOF*.rnx

# Validate with progress information
tosGPS --log-level INFO rinex RHOF data/RHOF0790.02D

# Apply corrections with backup
tosGPS rinex RHOF data/RHOF0790.02D --fix --backup

# Generate QC report
tosGPS rinex RHOF data/*.rnx --report qc_report.txt

# Silent validation for scripting
tosGPS --log-level ERROR rinex RHOF file.rnx 2>/dev/null
echo $?  # Check exit code: 0=success, 1=discrepancies
```

### Metadata Synchronization

The unified metadata sync system (`syncMeta`) provides automated downloading, validation, and comparison of external GPS/GNSS reference data files with operational-grade reliability.

**Key Features:**
- **Multi-type support**: GAMIT stations, IGS logs, ANTEX data, etc.
- **Intelligent caching**: Conditional downloads with checksum validation
- **Operational resilience**: Never abort - complete what's possible
- **YAML configuration**: Flexible server and type management
- **Smart comparison**: Automatic detailed analysis for single stations

```bash
# Discovery and status
tosGPS syncMeta --list-types          # Show available metadata types
tosGPS syncMeta --list-servers        # Show configured servers  
tosGPS syncMeta --status              # Show sync status of all types

# Basic operations (check differences - default)
tosGPS syncMeta --type gamit-station-info RHOF                 # Check differences
tosGPS syncMeta --type gamit-station-info RHOF --update        # Update with confirmation

# Batch operations
tosGPS syncMeta --type gamit-station-info RHOF REYK HOFN --update --no-compare
tosGPS syncMeta --type all --all-stations                  # Check all TOS stations

# Multi-type operations
tosGPS syncMeta --type gamit-station-info,igs-logs RHOF
tosGPS syncMeta --type gamit-station-info,igs-logs RHOF --update --no-compare

# Advanced options  
tosGPS syncMeta --type gamit-station-info --force-server okada RHOF  # Force specific server
tosGPS syncMeta --type gamit-station-info RHOF --force-download # Bypass cache
tosGPS syncMeta --type gamit-station-info RHOF --backup        # Create backup
```

**Configuration Setup:**
```bash
# Create configuration directory
mkdir -p ~/.config/tostools

# Copy example configuration  
cp docs/sync-config.yaml.example ~/.config/tostools/sync-config.yaml

# Edit configuration for your environment
editor ~/.config/tostools/sync-config.yaml
```

The system uses a default configuration if no YAML file is found, but creating your own configuration allows:
- Adding multiple metadata types (igs-logs, antex-data, rinex-nav)
- Configuring multiple servers with fallback priorities
- Customizing cache settings and validation parameters
- Setting up operational monitoring and error handling

**Operational Features:**
- **Exit codes**: 0=success, 1=partial failure, 2=total failure (for monitoring)
- **Never abort**: Continues processing even when individual stations fail
- **Clean output**: Status to stderr, data to stdout (pipe-friendly)
- **Intelligent caching**: Downloads only when data has changed
- **Visual diff output**: Colored highlighting shows TOS vs reference differences
# - Yellow: Session differences (equipment changes, coordinates)
```

**Features:**
- **Automated Downloads**: Secure SSH/SCP fetching from okada server
- **Visual Comparisons**: Color-coded diff output for quality control
- **Icelandic Normalization**: Handles ÁÐÍÓÚÝÞÆØå characters for GAMIT compatibility
- **Session Analysis**: Line-by-line comparison with precise column-level detection
- **Clean Output**: Optimized for both manual review and automation workflows

### Site Log Generation

```bash
# Output to stdout (pipe-friendly)
tosGPS sitelog RHOF
tosGPS sitelog RHOF | grep "Antenna"

# Save to file
tosGPS sitelog RHOF --output RHOF_site.log

# Automated change detection (skips if no changes)
tosGPS sitelog RHOF --auto-filename --date-in-name

# Force creation even if no changes
tosGPS sitelog RHOF --auto-filename --date-in-name --force-update

# Process multiple stations
for station in REYK HOFN RHOF; do
    tosGPS sitelog $station --auto-filename --date-in-name --dir ./logs
done
```

### SyncMeta - Safe Reference Data Updates

**🛡️ Production-Grade Safe Update System**

The syncMeta safe update system provides enterprise-level reliability for updating critical reference files like `station.info.sopac.apr05` with multiple safety layers and rollback capabilities.

```bash
# Safe Update Workflow (Recommended)
tosGPS syncMeta --type gamit-station-info RHOF --update --dry-run      # Test everything
tosGPS syncMeta --type gamit-station-info RHOF --update  # Safe production (interactive by default)

# Production Mode (for automation/cron)
tosGPS syncMeta --type gamit-station-info RHOF --update --production-mode

# Backup Management
tosGPS syncMeta --type gamit-station-info --list-backups              # Show available backups
tosGPS syncMeta --type gamit-station-info --rollback 20250904_143022   # Restore from backup

# File Integrity Checking
tosGPS syncMeta --type gamit-station-info --verify-only               # Check file health
```

**🔒 Safety Features:**
- ✅ **Fresh Download Verification** - Always uses latest server data with integrity checks
- ✅ **Automatic Backups** - Timestamped backups with 10-version retention
- ✅ **Change Verification** - Ensures only intended stations are modified
- ✅ **Working Copy Isolation** - Edits in safe environment, never corrupts originals
- ✅ **Pre-Upload Validation** - Comprehensive checks before any remote changes
- ✅ **Rollback Capability** - Easy restoration from any backup version
- ✅ **Test Mode** - Complete dry-run simulation without actual uploads
- ✅ **Production Logging** - Structured logging for monitoring and alerting

**🗂️ File Structure:**
```
data/station_config/
├── station.info.sopac.apr05           # Current reference file
├── backups/                           # Automatic versioned backups
│   ├── station.info.sopac.apr05.backup.20250904_143022
│   └── backup_registry.json          # Backup metadata tracking
├── work/                              # Working copies for safe editing
└── cache/                             # Fresh downloads and verification
```

**🎯 Use Cases:**
- **Development**: `--dry-run` for safe testing of all operations
- **Non-Interactive**: `--non-interactive` to skip confirmation prompts (interactive is default)
- **Production**: `--production-mode` for clean structured logging
- **Recovery**: `--rollback <backup-id>` for instant restoration
- **Monitoring**: `--verify-only` for health checks and integrity validation

## 🌐 TOS Subcommands

```bash
# Station orchestration
tos station show <STN>              # identity + open attributes + joined devices
tos station show <STN> --all        # adds attribute + join history
tos station show <STN> --device     # delegate to `tos device list --station <STN>`
tos station triage <STN>            # generate combined triage file
tos station verify <STN>            # re-run audits, exit 0 clean / 1 findings / 2 failure

# Device inspection
tos device list --station <STN>     # currently-joined devices
tos device show --id N              # full device record

# Audits
tos audit attribute-dates <STN>     # suspicious date_from values
tos audit missing-attributes <STN>  # required attrs with no open period
tos audit verify-from-rinex --station <STN>   # cross-check vs cold RINEX archive
tos audit apply <triage_file>       # apply operator-edited triage (dry-run by default)
```

The legacy flat-arg form (`tos RHOF`, `tos -s SERIAL`, `tos --fdsnxml/--sc3ml`,
and the `json2ascii`/`metadata2rmq` helpers) was removed in v0.7. SC3/FDSN XML
generation is not in scope for this package.

## 📊 Output Formats

### Rich Format (Default)
- **Color-coded equipment groups**: Receiver (green), Antenna (red), Monument (yellow)
- **Professional layout**: Compact spacing optimized for terminal viewing
- **Complete data visibility**: No truncation, proper decimal alignment
- **Contact information**: Clean tables with English/Icelandic translations

### Table Format
- **Simple tabular output**: Perfect for CSV export and data analysis
- **Script-friendly**: Clean format for automated processing
- **Pipe-compatible**: Works seamlessly with Unix tools

### GAMIT Format
- **GPS Processing Ready**: Fixed-width columns with proper headers
- **Robust Validation**: Invalid sessions skipped, valid sessions preserved
- **Production Tested**: Compatible with GAMIT/GLOBK workflows
- **Error Reporting**: Clear logging of data quality issues

### JSON Format
- **Complete metadata**: All station information in structured format
- **API Integration**: Perfect for web services and data pipelines
- **Processing Scripts**: Easy parsing with jq and other JSON tools

## 🔄 Examples

### Manual Quality Control Workflow
```bash
# 1. Review station with rich visual display
tosGPS PrintTOS RHOF --format rich

# 2. Check specific equipment history
tosGPS PrintTOS RHOF --show-history

# 3. Verify contact information
tosGPS PrintTOS RHOF --contact

# 4. Validate against RINEX files
tosGPS rinex RHOF data/RHOF*.rnx --report validation.txt

# 5. Generate site log if needed
tosGPS sitelog RHOF --output RHOF.log
```

### GPS Processing Preparation
```bash
# Generate GAMIT stations file with validation
tosGPS PrintTOS RHOF REYK HOFN --format gamit > stations.info

# Check for any data quality issues
tosGPS --log-level ERROR PrintTOS RHOF REYK HOFN --format gamit

# Process with logging for quality control
tosGPS --log-dir logs PrintTOS RHOF REYK --format gamit > stations.info
```

### Automated Data Pipeline
```bash
# Clean data extraction for automation
tosGPS --log-level ERROR PrintTOS RHOF --format table > station_data.csv

# JSON processing pipeline
tosGPS PrintTOS RHOF --format json | jq '.device_history[] | .time_from' 

# Batch validation
for file in data/*.rnx; do
    tosGPS --log-level ERROR rinex RHOF "$file" || echo "Issue in $file"
done
```

### Legacy TOS Integration
```bash
# Search Icelandic weather stations
tos vadla -o json > vadla_station.json

# Find equipment by serial number
tos -s A086 -o table

# Geophysical network query
tos -D geophysical reyk
```

## 🏗️ Architecture

### Modular Design (v0.2.3+)
```
src/tostools/
├── cli/                    # Command-line interfaces
│   ├── main.py            # Modern modular CLI
│   └── rinex_cli.py       # RINEX processing commands
├── io/                    # Input/Output formatting
│   ├── rich_formatters.py # Rich table display
│   └── formatters.py      # JSON/table formatters
├── utils/                 # Utilities
│   └── logging.py         # Production logging system
├── tosGPS.py              # Main GPS QC application (legacy compatible)
└── legacy/                # Original modules (transitioning)
    ├── gps_metadata_*.py # GPS processing functions
    └── tos.py             # TOS API client
```

### Key Features
- **Clean Output by Default**: All commands produce automation-friendly output
- **Rich Visual Mode**: Enhanced tables for manual quality control
- **Robust Error Handling**: Graceful handling of real-world data issues
- **Production Logging**: Comprehensive file logging with level separation
- **Backward Compatibility**: Legacy interfaces maintained during transition

## 👨‍💻 Development

### Running Tests
```bash
# Install development dependencies
pip install -e ".[dev]"

# Run test suite
pytest tests/ -v

# Test console scripts
tosGPS --help
tos --help
```

### Code Quality
```bash
# Linting
ruff check src/

# Formatting  
black src/

# GPS/GNSS standards validation
python scripts/update_standards.py --validate-only

# Full CI pipeline locally
ruff check src/ && black --check src/ && pytest tests/ -v && python scripts/update_standards.py --validate-only
```

### GPS/GNSS Standards
```bash
# Check for standards updates
python scripts/update_standards.py

# Generate standards compliance report
python scripts/update_standards.py --report standards_report.txt

# View standards documentation
ls docs/standards/
```

### Contributing
1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Ensure CI pipeline passes (includes standards compliance validation)
5. Submit a pull request

## 📐 GPS/GNSS Standards

tostools maintains strict compliance with authoritative GPS/GNSS standards:

### Supported Standards
- **IGS Site Log Instructions v2.0**: International GNSS Service site log format
- **RINEX v2/3/4**: Receiver Independent Exchange Format specifications  
- **GAMIT/GLOBK**: MIT GPS processing software station.info format
- **ITRF/DOMES**: International Terrestrial Reference Frame standards
- **EPN Guidelines**: EUREF Permanent Network operational standards

### Standards Integration
- **Automated Validation**: Pre-commit hooks and CI/CD pipeline validation
- **Local Repository**: Complete standards documents stored in `docs/standards/`
- **Update Management**: Automated checking for standards updates
- **Compliance Reporting**: Generate detailed compliance reports

### Key Implementation Features
- **IGS Compliance**: Nine-character station IDs, DMS coordinate formatting
- **RINEX Compliance**: FORTRAN77 format preservation, multi-version support
- **GAMIT Compliance**: Fixed-width station.info format, session validation
- **Cross-validation**: Consistency checking across different standards

### Documentation
- **Standards Index**: `docs/standards/STANDARDS_INDEX.md`
- **Workflow Integration**: `docs/standards/WORKFLOW_INTEGRATION.md`
- **Implementation Details**: Individual standards documented in `docs/standards/`

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **Benedikt G. Ófeigsson** - GPS metadata QC and RINEX processing tools
- **Tryggvi Hjörvar** - Original TOS API integration and Icelandic station tools
- **Icelandic Meteorological Office (IMO)** - TOS API and station data
- **International GNSS Service (IGS)** - Site log standards and specifications
- **MIT GAMIT/GLOBK Team** - GPS processing software compatibility
- **ITRF/IGN** - International Terrestrial Reference Frame standards

## 📞 Contact

- **Email**: bgo@vedur.is (Benedikt) or hildur@vedur.is (Hildur)
- **Issues**: [GitHub Issues](https://github.com/bennigo/tostools/issues)
- **TOS API**: [https://tos.vedur.is](https://tos.vedur.is)

---

**Version**: 0.2.3 | **Python**: 3.8+ | **Status**: Production Ready