# RINEX Format Standards

Documentation for RINEX (Receiver Independent Exchange Format) standards implementation in tostools.

## Overview

RINEX is the standard format for exchanging GNSS observation data and navigation messages. The format ensures interoperability between different GNSS receivers and processing software.

**Official Specifications**: 
- RINEX 2: ftp://igs.org/pub/data/format/rinex2.txt
- RINEX 3: ftp://igs.org/pub/data/format/rinex3.txt  
- RINEX 4: ftp://igs.org/pub/data/format/rinex4.txt

## Critical Implementation Requirements

### ⚠️ FORTRAN77 Formatting Requirements

**CRITICAL**: RINEX files use strict FORTRAN77 column formatting that must be preserved exactly.

#### Key Constraints
- **Spaces vs Tabs**: Must use exact spacing, NEVER tabs
- **Column Alignment**: Each field has specific character positions
- **Extra Spaces**: Can break parsing in FORTRAN77 readers
- **Line Length**: Fixed-width records, no truncation allowed

#### tostools Implementation
```python
# FORTRAN77 format preservation in RINEX editing
# WARNING: Maintain exact column positions
header_line = f"{field:<20}{value:>40}COMMENT"  # Exact spacing required
```

**Code Locations**:
- **RINEX Reader**: `src/tostools/rinex/reader.py`
- **RINEX Editor**: `src/tostools/rinex/editor.py`
- **RINEX Validator**: `src/tostools/rinex/validator.py`

## RINEX Version Support

### RINEX 2.x (Legacy)
- **File Extensions**: `.YYO`, `.YYN`, `.YYM`, `.YYG`
- **Compression**: `.Z`, `.gz` supported
- **Header Format**: Fixed 80-character lines
- **Observation Types**: L1, L2, C1, P1, P2, D1, D2, S1, S2

### RINEX 3.x (Current Standard)
- **File Extensions**: `.YYO`, `.YYN`, `.YYM`, `.YYG`, `.YYP`, `.YYL`, `.YYB`
- **Enhanced Features**: Multi-GNSS support, extended observation types
- **Header Improvements**: Better metadata support
- **Observation Types**: Extended for GPS, GLONASS, Galileo, BeiDou, QZSS

### RINEX 4.x (Latest)
- **Enhanced Precision**: Improved numerical precision
- **New Observation Types**: Advanced GNSS signal support
- **Metadata**: Enhanced header information

## tostools RINEX Implementation

### Core Modules

#### RINEX Reader (`src/tostools/rinex/reader.py`)
```python
def read_rinex_header(file_path: str) -> Dict[str, Any]:
    """
    Read RINEX header preserving exact FORTRAN77 formatting.
    Handles compressed files (.gz, .Z) automatically.
    """
    # Implementation maintains strict column formatting
    # Supports all RINEX versions 2.x, 3.x, 4.x
```

#### RINEX Validator (`src/tostools/rinex/validator.py`)
```python
def compare_rinex_to_tos(rinex_data: Dict, tos_data: Dict) -> List[str]:
    """
    Compare RINEX header information against TOS API metadata.
    Identifies discrepancies for quality control.
    """
    # Cross-validation between RINEX files and station metadata
```

#### RINEX Editor (`src/tostools/rinex/editor.py`)
```python
def update_rinex_files(files: List[str], corrections: Dict, 
                      backup: bool = True) -> None:
    """
    Apply corrections to RINEX files while preserving format.
    Creates backups and maintains FORTRAN77 compliance.
    """
    # CRITICAL: Maintains exact column positioning
```

### Command Interface

#### RINEX Processing Commands
```bash
# RINEX validation against TOS metadata
tosGPS rinex RHOF data/*.rnx

# Apply corrections with backup
tosGPS rinex RHOF data/*.rnx --fix --backup

# Generate validation report
tosGPS rinex RHOF data/*.rnx --report validation_report.txt
```

#### File Support
- **Compressed Files**: Automatic handling of `.gz`, `.Z` compressed RINEX
- **Batch Processing**: Process multiple RINEX files simultaneously
- **Format Detection**: Automatic RINEX version detection

## Header Field Standards

### Required Header Fields (RINEX 2/3)
```
RINEX VERSION / TYPE     : Version and file type
PGM / RUN BY / DATE     : Program information and date
MARKER NAME             : Station marker name (4-char)
MARKER NUMBER           : Station number
OBSERVER / AGENCY       : Observer and agency information
REC # / TYPE / VERS     : Receiver number, type, version
ANT # / TYPE            : Antenna number and type
APPROX POSITION XYZ     : Approximate position (meters)
ANTENNA: DELTA H/E/N    : Antenna eccentricities
WAVELENGTH FACT L1/2    : Wavelength factors
# / TYPES OF OBSERV     : Number and types of observations
INTERVAL                : Observation interval
TIME OF FIRST OBS       : Time of first observation
TIME OF LAST OBS        : Time of last observation (optional)
END OF HEADER           : Header termination
```

### tostools Header Processing
```python
# Extract critical header information
header_info = {
    'marker_name': rinex_header.get('MARKER NAME', '').strip(),
    'receiver_type': rinex_header.get('REC # / TYPE / VERS', '').split()[1],
    'antenna_type': rinex_header.get('ANT # / TYPE', '').split()[1],
    'position_xyz': parse_position(rinex_header.get('APPROX POSITION XYZ')),
    'antenna_delta': parse_delta(rinex_header.get('ANTENNA: DELTA H/E/N'))
}
```

## Validation Standards

### Cross-Reference Validation
tostools validates RINEX files against TOS API metadata:

#### Station Information
- **Marker Name**: Must match TOS station identifier
- **Position**: Coordinates within tolerance of TOS data
- **Equipment**: Receiver/antenna types match equipment history

#### Equipment Validation
```python
def validate_equipment_consistency(rinex_header: Dict, tos_session: Dict) -> List[str]:
    """Validate equipment information consistency."""
    discrepancies = []
    
    # Receiver validation
    rinex_receiver = rinex_header.get('REC # / TYPE / VERS', '').split()[1]
    tos_receiver = tos_session['gnss_receiver']['model']
    if not receivers_match(rinex_receiver, tos_receiver):
        discrepancies.append(f"Receiver mismatch: RINEX={rinex_receiver}, TOS={tos_receiver}")
    
    # Antenna validation
    rinex_antenna = rinex_header.get('ANT # / TYPE', '').split()[1] 
    tos_antenna = tos_session['antenna']['model']
    if not antennas_match(rinex_antenna, tos_antenna):
        discrepancies.append(f"Antenna mismatch: RINEX={rinex_antenna}, TOS={tos_antenna}")
    
    return discrepancies
```

## Quality Control Standards

### File Integrity Checks
- **Header Completeness**: All required fields present
- **Format Compliance**: Strict FORTRAN77 formatting
- **Data Consistency**: Header matches observation data
- **Time Range**: Observations within expected time bounds

### Equipment History Integration
- **Session Matching**: RINEX time range matches TOS equipment sessions
- **Change Detection**: Equipment changes reflected in RINEX files
- **Version Tracking**: Firmware/software versions consistent

### Error Detection and Correction
```python
# Common RINEX issues detected by tostools
corrections = {
    'marker_name_mismatch': 'Update MARKER NAME field',
    'position_discrepancy': 'Correct APPROX POSITION XYZ',
    'equipment_inconsistency': 'Update receiver/antenna information',
    'time_format_error': 'Fix TIME OF FIRST/LAST OBS format'
}
```

## File Naming Conventions

### Standard RINEX Naming
- **Daily Files**: `SSSSDDD0.YYO` (station, day of year, year, observation)
- **Hourly Files**: `SSSSDDDH.YYO` (with hour indicator)
- **Compressed**: Add `.Z` or `.gz` extension

### Examples
- **RHOF2340.24D.gz**: Raufarhöfn, day 234, 2024, compressed daily file
- **REYK1200.24O**: Reykjavik, day 120, 2024, observation file

## Integration with Processing Software

### GAMIT/GLOBK Compatibility
- Header information consistent with GAMIT station.info requirements
- Antenna/receiver combinations validated against GAMIT standards
- Position accuracy suitable for GPS processing workflows

### Standards Compliance Testing
```bash
# Test RINEX format compliance
tosGPS rinex RHOF test_data/RHOF2340.24D.gz --validate

# Check against processing requirements  
tosGPS rinex RHOF data/*.rnx --gamit-check
```

---

**Key References**:
- RINEX Format Specifications: https://files.igs.org/pub/data/format/
- IGS Data Guidelines: https://igs.org/data-products/
- UNAVCO RINEX Guidelines: https://www.unavco.org/data/gps-gnss/data-access-methods/rinex/rinex.html