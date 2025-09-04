# IGS (International GNSS Service) Standards

Documentation for IGS standards implementation in tostools.

## Overview

The International GNSS Service (IGS) provides authoritative standards for:
- GNSS station site logs
- Station naming conventions
- Data formats and quality standards
- Network operations and data flow

**Primary Source**: https://igs.org/

## Site Log Standards

### IGS Site Log Instructions v2.0

**Official Document**: https://files.igs.org/pub/station/general/sitelog_instr_v2.0.txt
**Local Copy**: `../local_copies/sitelog_instr_v2.0.txt`

#### Key Requirements

##### 1. Station Identification
- **Nine-Character ID**: `{STATION}{MONUMENT}{COUNTRY}` (e.g., RHOF00ISL)
- **IERS DOMES Number**: Required for official IGS stations
- **Monument Description**: Standardized terminology

##### 2. Section Structure
```
0.   Form (preparation details)
1.   Site Identification of the GNSS Monument  
2.   Site Location Information
3.   GNSS Receiver Information
4.   GNSS Antenna Information
5.   Surveyed Local Ties
6.   Frequency Standard
7.   Collocation Information
8.   Meteorological Instrumentation
9.   On-Site, Point of Contact Agency Information
10.  Responsible Agency
11.  More Information
```

##### 3. Coordinate Formats
- **Cartesian (ITRF)**: X, Y, Z coordinates in meters (3 decimal places)
- **Geographic**: Latitude/Longitude in DDMMSS.SS format with hemisphere indicators
- **Elevation**: Ellipsoidal height in meters (1 decimal place)

##### 4. Equipment Information
- **Receiver**: Model, serial number, firmware version
- **Antenna**: Model, serial number, antenna reference point (ARP)
- **Radome**: Model specification (use "NONE" if no radome)
- **Installation dates**: YYYY-MM-DDTHH:MMZ format

#### tostools Implementation

**Module**: `src/tostools/legacy/gps_metadata_functions.py`
**Function**: `site_log()`
**Command**: `tosGPS sitelog STATION [options]`

##### Compliance Features
```python
# Nine-character station ID generation
station_id = f"{marker.upper()}00{country_code.upper()}"

# Coordinate format conversion (decimal to DMS)
def decimal_to_dms(decimal_deg):
    degrees = int(abs_deg)
    minutes = int((abs_deg - degrees) * 60)
    seconds = ((abs_deg - degrees) * 60 - minutes) * 60
    return f"{'-' if is_negative else '+'}{degrees:02d}{minutes:02d}{seconds:05.2f}"

# Country code translation
country_translation = {
    "Ísland": "ISL", "Iceland": "ISL",
    "Norge": "NOR", "Norway": "NOR", 
    # ... additional mappings
}

# Language translation for compliance
if fault_zone == "NEI": fault_zone = "NO"
elif fault_zone in ["JÁ", "JA"]: fault_zone = "YES"
```

##### Professional Features
- **Smart Change Detection**: Automatic comparison with previous logs, skips creation if no changes
- **Structured Directory Layout**: `--date-in-name` creates organized `sitelog/STATION/` structure
- **Path Intelligence**: Prevents duplicate `sitelog` directories in paths
- **Clean Terminal Output**: Minimal stderr for automated workflows
- **Intelligent Naming**: `--date-in-name` generates `rhof00isl_20250904.log`
- **Report Type Detection**: Automatic NEW/UPDATE based on previous log existence
- **Modified Sections**: Auto-detection by comparing with previous logs
- **Force Override**: `--force-update` bypasses change detection when needed

##### Usage Examples
```bash
# Basic IGS site log generation (stdout)
tosGPS sitelog RHOF

# Smart change detection with organized structure
tosGPS sitelog RHOF --date-in-name
# → Creates: ./sitelog/RHOF00ISL/rhof00isl_20250904.log
# → Skips creation if no changes since last run

# Custom directory structure
tosGPS sitelog RHOF --date-in-name --dir /data/logs
# → Creates: /data/logs/sitelog/RHOF00ISL/rhof00isl_20250904.log

# Force creation even if no changes detected
tosGPS sitelog RHOF --date-in-name --force-update

# With custom date for historical sessions
tosGPS sitelog RHOF --auto-filename --custom-date 20010719

# Manual modified sections override
tosGPS sitelog RHOF --modified-sections "1,3.2,4.2"
```

## Station Naming Conventions

### Nine-Character Format
**Format**: `{STATION}{MONUMENT}{COUNTRY}`
- **STATION**: 4-character site code (e.g., RHOF, REYK, HOFN)
- **MONUMENT**: 2-digit monument number (00 for primary monument)
- **COUNTRY**: 3-character ISO 3166-1 alpha-3 country code

### Examples
- **RHOF00ISL**: Raufarhöfn, Iceland, primary monument
- **REYK00ISL**: Reykjavik, Iceland, primary monument
- **ZIMM00CHE**: Zimmerwald, Switzerland, primary monument

### tostools Implementation
```python
def generate_igs_sitelog_filename(station_marker: str, country_code: str = "ISL", 
                                  monument_number: str = "00") -> str:
    """Generate IGS-compliant station identifier."""
    return f"{station_marker.upper()}{monument_number}{country_code.upper()}"
```

## Data Quality Standards

### Equipment Information Requirements
- **Complete Serial Numbers**: Full manufacturer serial numbers required
- **Firmware Versions**: Exact version strings for receivers
- **Installation Dates**: Precise timestamps for equipment changes
- **Antenna Information**: Model, serial number, height measurements

### Validation Criteria
```python
# Essential data validation in tosGPS
required_fields = ['marker', 'name', 'lat', 'lon', 'altitude']
missing = [f for f in required_fields if not station_data.get(f)]
if missing:
    print(f"⚠️  Station {station}: Missing required fields: {', '.join(missing)}")
```

## Integration with Other Standards

### ITRF Compatibility
- Coordinate transformations use ITRF reference frames
- DOMES number integration for station metadata
- Tectonic plate information for geological context

### RINEX Compliance
- Equipment information matches RINEX header requirements
- Time formatting consistent across site logs and RINEX files
- Antenna/receiver combinations validated against IGS standards

## Validation and Testing

### Standards Compliance Testing
```bash
# Test site log generation against reference
tosGPS sitelog RHOF > test_sitelog.txt
diff test_sitelog.txt reference_data/RHOF/legacy_sitelog.txt

# Validate with official IGS examples
tosGPS sitelog RHOF --validate
```

### Quality Assurance
- **Format Validation**: Check section structure and formatting
- **Data Completeness**: Ensure all required fields present
- **Coordinate Accuracy**: Validate coordinate transformations
- **Standards Compliance**: Compare against official IGS examples

---

**References**:
- IGS Site Guidelines: https://igs.org/wg/site-guidelines/
- IGS Data Products: https://igs.org/data-products/
- IGS Central Bureau: https://igs.org/organization/