# tostools Data Directory

This directory contains GPS station data, reference files, and samples organized by type for production use and development.

## Directory Structure

### `rinex_samples/`
RINEX (Receiver Independent Exchange Format) files for GPS data processing:
- `*.D`, `*.D.Z`, `*.D.gz` - Compressed daily RINEX observation files
- Used for GPS station data validation and processing
- Examples: RHOF station data from various dates (2002-2024)

### `sitelogs_archive/`
IGS-compliant site log files and historical station logs:
- `*sitelog*.txt` - Generated IGS v2.0 compliant site logs
- `*.log` - Station-specific log files by date
- Used for GPS station documentation and compliance
- Historical archive for reference and validation

### `station_config/`
GPS station configuration and reference data:
- `station-plate` - Tectonic plate assignments by station marker
- `antenna_arp.list` - Antenna reference point specifications
- `*.json` - Station metadata in JSON format
- `*.list` - Station lists and configurations  
- `*.info` - Station information files (SOPAC, LMI formats)
- DOMES and IERS station information

### `reference/`
Reference standards and documentation files:
- Site log instructions and format specifications
- Standards documentation for GPS/GNSS compliance
- Reference implementations and examples

### `test_outputs/`
Test outputs, debug files, and development data:
- `test*.txt` - Test outputs and reports
- `debug.log`, `stdout.log`, `stderr.log` - Debug and logging files
- Development scripts and temporary conversions
- Legacy executable scripts for reference

## Usage Notes

- **Production Ready**: Core configuration files used by tosGPS for station processing
- **Station Config**: Essential files (station-plate, antenna_arp.list) required for sitelog generation
- **RINEX Samples**: Representative files for testing and development workflows
- **Historical Archive**: Site logs and station logs for reference and validation
- **Reference Standards**: Documentation and specifications for GPS/GNSS compliance

## File Dependencies

- `station_config/station-plate` - Required for tectonic plate assignment in site logs
- `station_config/antenna_arp.list` - Required for antenna reference point calculations
- Other files provide reference data and examples for development

## Maintenance

- **Station Config**: Update when new stations added or antenna specifications change
- **Reference Files**: Update when GPS/GNSS standards change
- **RINEX Samples**: Add new samples for testing different data scenarios
- **Archive**: Periodic cleanup of old logs while preserving reference examples

---
Created: 2025-08-26  
Updated: 2025-08-26 (moved from tmp/organized to project-level data directory)  
Purpose: GPS/GNSS station data management and production support