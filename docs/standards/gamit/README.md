# GAMIT/GLOBK Standards

Documentation for GAMIT/GLOBK GPS processing software standards implementation in tostools.

## Overview

GAMIT/GLOBK is professional GPS/GNSS processing software developed at MIT for high-precision positioning and analysis. It requires specific data formats and station information standards for reliable processing.

**Primary Source**: https://geoweb.mit.edu/gg/
**Documentation**: https://geoweb.mit.edu/gg/docs.php

## Station Information File Format

### station.info File Standard

The `station.info` file contains essential station metadata required for GAMIT processing in a fixed-width format.

#### Format Specification
```
Columns  Field               Description
1-4      Station ID          4-character station identifier
6-13     Latitude            Degrees (decimal, 8 characters)
15-22    Longitude           Degrees (decimal, 8 characters) 
24-30    Height              Meters (7 characters, 1 decimal place)
32-35    Start Date          YYDDD format (year, day of year)
37-40    End Date            YYDDD format (99999 for present)
42-60    Station Name        Description (up to 19 characters)
```

#### Example Format
```
RHOF  64.13170 -15.95272   24.7 01193 99999 Raufarhofn
REYK  64.13847 -21.95571   93.0 96001 99999 Reykjavik
HOFN  64.26820 -15.21271   75.9 96001 99999 Hofn
```

### tostools Implementation

**Module**: `src/tostools/gps_metadata_functions.py`
**Function**: `print_station_info()` 
**Command**: `tosGPS PrintTOS STATION --format gamit`

#### Key Features
- **Fixed-Width Formatting**: Exact column positioning for GAMIT compatibility
- **Session-Level Validation**: Prevents processing crashes from invalid data
- **Equipment Change Tracking**: Multiple sessions per station for equipment changes
- **Data Quality Assurance**: Essential vs non-essential field validation

#### Implementation Example
```python
def format_gamit_line(station_data: Dict, device_session: Dict) -> str:
    """Format station data for GAMIT station.info file."""
    marker = station_data['marker'].upper()
    lat = float(station_data['lat'])
    lon = float(station_data['lon']) 
    alt = float(station_data['altitude'])
    
    # Convert session dates to YYDDD format
    start_date = format_gamit_date(device_session['time_from'])
    end_date = format_gamit_date(device_session.get('time_to', '99999'))
    
    name = station_data['name'][:19]  # Truncate to 19 chars
    
    # Fixed-width formatting critical for GAMIT
    return f"{marker:<4} {lat:8.5f} {lon:9.5f} {alt:7.1f} {start_date:>5} {end_date:>5} {name:<19}"
```

#### Robust Data Validation
```python
# Session-level validation prevents GPS processing crashes
def validate_gamit_session(station_data: Dict, device_session: Dict) -> bool:
    """Validate session data for GAMIT processing compatibility."""
    required_fields = ['marker', 'name', 'lat', 'lon', 'altitude']
    
    # Check essential station data
    for field in required_fields:
        if not station_data.get(field):
            logger.error(f"Missing essential field: {field}")
            return False
    
    # Validate session time range
    if not device_session.get('time_from'):
        logger.error("Missing session start time")
        return False
    
    return True
```

### Usage Examples
```bash
# Generate GAMIT-compatible station list
tosGPS PrintTOS RHOF REYK HOFN --format gamit > stations.info

# With validation reporting
tosGPS --log-level INFO PrintTOS RHOF --format gamit

# Session-specific output
tosGPS PrintTOS RHOF --format gamit --show-history
```

## Processing Configuration Standards

### GAMIT Processing Requirements

#### Essential Data Quality
- **Coordinates**: Millimeter-level accuracy required for scientific processing
- **Equipment Information**: Complete receiver/antenna metadata
- **Time Ranges**: Precise session boundaries for equipment changes
- **Reference Frame**: Consistent ITRF coordinates

#### Data Validation Workflow
```python
# tostools validation for GAMIT processing
validation_results = {
    'stations_valid': 0,
    'stations_skipped': 0, 
    'sessions_valid': 0,
    'sessions_skipped': 0
}

for session in device_sessions:
    if validate_gamit_session(station_data, session):
        validation_results['sessions_valid'] += 1
        # Include in GAMIT output
    else:
        validation_results['sessions_skipped'] += 1
        # Skip invalid session, continue processing
```

### Equipment Change Handling

#### Session Management
GAMIT processing requires separate entries for each equipment configuration:

```python
# Multiple sessions for equipment changes
sessions = [
    {'time_from': '2001-07-19', 'time_to': '2002-03-29', 
     'receiver': 'ASHTECH UZ-12', 'antenna': 'ASH701945C_M'},
    {'time_from': '2002-03-29', 'time_to': '2012-08-28',
     'receiver': 'ASHTECH UZ-12', 'antenna': 'ASH701945C_M'}, 
    {'time_from': '2012-08-28', 'time_to': '99999',
     'receiver': 'TRIMBLE NETR9', 'antenna': 'TRM57971.00'}
]
```

#### Missing Data Handling
```python
# Graceful handling of missing monument data
try:
    monument_height = float(monument_data.get('monument_height', 0.0))
    antenna_height = float(antenna_data.get('antenna_height', 0.0))
    total_height = monument_height + antenna_height
except (ValueError, TypeError):
    # Use antenna-only height when monument data unavailable
    total_height = float(antenna_data.get('antenna_height', 0.0))
    logger.warning(f"Using antenna-only height for {station_id}")
```

## Processing Workflow Integration

### GAMIT/GLOBK Processing Pipeline

#### 1. Station Information Preparation
```bash
# Generate stations.info file for processing campaign
tosGPS PrintTOS station1 station2 station3 --format gamit > stations.info
```

#### 2. Data Quality Assessment  
```bash
# Validate data quality before processing
tosGPS --log-level INFO PrintTOS stations.list --format gamit 2>&1 | \
    grep -E "(valid|skipped)" > validation_summary.txt
```

#### 3. Session-Level Processing
- Each equipment configuration gets separate processing session
- Time boundaries match exact equipment installation dates
- Coordinate consistency maintained across sessions

### Quality Control Standards

#### Validation Reporting
```
Station RHOF: 3/3 sessions valid (0 skipped)
Station REYK: 12/15 sessions valid (3 skipped - missing coordinates)  
Station HOFN: 8/8 sessions valid (0 skipped)

Total: 23/26 sessions valid for GAMIT processing
```

#### Error Handling
- **Invalid sessions skipped**: Processing continues with valid sessions
- **Station-level failures**: Individual stations excluded, others processed
- **Essential data missing**: Clear error messages for resolution

## Coordinate Reference Standards

### ITRF Integration
- **Reference Frame**: ITRF coordinates required for global processing
- **Epoch Definition**: Coordinate epochs match observation periods  
- **Precision Requirements**: Sub-centimeter coordinate accuracy

### Coordinate Transformations
```python
# ITRF coordinate handling in tostools
coordinates = {
    'X': float(station_data.get('x_coordinate', 0.0)),
    'Y': float(station_data.get('y_coordinate', 0.0)), 
    'Z': float(station_data.get('z_coordinate', 0.0)),
    'lat': float(station_data.get('lat', 0.0)),
    'lon': float(station_data.get('lon', 0.0)),
    'alt': float(station_data.get('altitude', 0.0))
}
```

## File Format Validation

### Standards Compliance Testing
```bash
# Test GAMIT format output
tosGPS PrintTOS RHOF --format gamit | head -5
# Expected output:
# RHOF  64.13170 -15.95272   24.7 01193 99999 Raufarhofn

# Validate column alignment
tosGPS PrintTOS RHOF --format gamit | cut -c1-4,6-13,15-22,24-30
```

### Format Requirements Checklist
- [ ] Fixed-width columns maintained
- [ ] Decimal precision consistent (5 for lat/lon, 1 for height)
- [ ] Date format YYDDD correctly applied
- [ ] Station names truncated to 19 characters
- [ ] No missing essential data fields

## Integration with Site Log Standards

### Cross-Reference Validation
- Station coordinates match site log values
- Equipment information consistent between GAMIT and IGS formats
- Time ranges align with site log equipment sections

### Dual Format Support
```bash
# Generate both GAMIT and IGS formats
tosGPS PrintTOS RHOF --format gamit > stations.info
tosGPS sitelog RHOF --auto-filename --dir ./sitelogs
```

---

**Key References**:
- GAMIT/GLOBK Documentation: https://geoweb.mit.edu/gg/docs.php
- GAMIT Tutorial: https://geoweb.mit.edu/gg/tutorial.html
- Processing Guidelines: https://geoweb.mit.edu/gg/GAMIT_Ref.pdf