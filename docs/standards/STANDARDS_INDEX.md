# GPS/GNSS Standards Index

Comprehensive index of all GPS/GNSS standards implemented in tostools.

## Quick Reference

| Standard | Version | tostools Implementation | Status |
|----------|---------|-------------------------|--------|
| IGS Site Log | v2.0 | `tosGPS sitelog` | ✅ Production |
| RINEX | v2/v3/v4 | `tosGPS rinex` | ✅ Production |
| GAMIT station.info | Latest | `tosGPS --format gamit` | ✅ Production |  
| DOMES | Current | Site log integration | ✅ Integrated |
| ITRF Coordinates | 2020 | Coordinate transformations | ✅ Active |

## Standards Documentation

### 📁 Directory Structure
```
docs/standards/
├── README.md                    # Overview and maintenance guide
├── STANDARDS_INDEX.md           # This index file
├── igs/README.md               # IGS standards (site logs, naming)
├── rinex/README.md             # RINEX format standards
├── gamit/README.md             # GAMIT/GLOBK processing standards
├── itrf/                       # ITRF coordinate standards
├── epn/                        # EPN network standards  
└── local_copies/               # Local standards documents
    ├── sitelog_instr_v2.0.txt
    ├── domes.req
    └── rinex_format_specs/
```

### 📋 Implementation Matrix

#### IGS (International GNSS Service)
- **Site Log Generation** → `tosGPS sitelog STATION`
  - ✅ IGS v2.0 compliance (nine-character IDs, DMS coordinates)
  - ✅ Professional directory management (`--dir ./sitelogs`)
  - ✅ Intelligent file naming (`--date-in-name`)
  - ✅ Report type detection (NEW/UPDATE)
  - 📍 **Code**: `src/tostools/legacy/gps_metadata_functions.py:site_log()`

#### RINEX (Receiver Independent Exchange Format)
- **RINEX Processing** → `tosGPS rinex STATION FILES`
  - ✅ Multi-version support (RINEX 2/3/4)
  - ✅ FORTRAN77 format preservation
  - ✅ Compressed file support (.gz, .Z)
  - ✅ Cross-validation with TOS metadata
  - 📍 **Code**: `src/tostools/rinex/` modules

#### GAMIT/GLOBK (MIT GPS Processing)
- **Station Information** → `tosGPS PrintTOS STATION --format gamit`
  - ✅ Fixed-width column formatting
  - ✅ Session-level data validation
  - ✅ Equipment change tracking
  - ✅ Processing-ready output format
  - 📍 **Code**: `src/tostools/gps_metadata_functions.py:print_station_info()`

#### ITRF (International Terrestrial Reference Frame)
- **Coordinate Standards** → Integrated across all commands
  - ✅ ITRF coordinate transformations
  - ✅ DOMES number integration
  - ✅ Tectonic plate mapping
  - ✅ Reference frame consistency

## Automated Standards Management

### 🔄 Update System
**Script**: `scripts/update_standards.py`

```bash
# Check for standards updates
python scripts/update_standards.py

# Force check all standards
python scripts/update_standards.py --force

# Validation only (no downloads)
python scripts/update_standards.py --validate-only

# Generate detailed report
python scripts/update_standards.py --report standards_report.txt
```

### 📊 Standards Compliance Monitoring
- **Automated Updates**: Monthly check for new standards versions
- **Compliance Validation**: Code validation against current standards  
- **Change Detection**: SHA-256 hash comparison for document changes
- **Backup Management**: Automatic backup of previous versions

## Critical Implementation Notes

### ⚠️ RINEX Format Requirements
```python
# CRITICAL: FORTRAN77 formatting must be preserved exactly
header_line = f"{field:<20}{value:>40}COMMENT"  # Exact spacing required
```

### 🎯 IGS Site Log Compliance  
```python
# Nine-character station ID (STATION + MONUMENT + COUNTRY)
station_id = f"{marker.upper()}00{country_code.upper()}"  # e.g., RHOF00ISL

# DMS coordinate format conversion
latitude_dms = f"+{degrees:02d}{minutes:02d}{seconds:05.2f}"
```

### 📏 GAMIT Fixed-Width Format
```python
# Fixed column positions for GAMIT compatibility
gamit_line = f"{marker:<4} {lat:8.5f} {lon:9.5f} {alt:7.1f} {start:>5} {end:>5} {name:<19}"
```

## Standards Sources & References

### 🌐 Primary Authoritative Sources

#### International GNSS Service (IGS)
- **Website**: https://igs.org/
- **Standards**: https://files.igs.org/pub/station/general/
- **Implementation**: Site log generation, station naming

#### International Terrestrial Reference Frame (ITRF)
- **Website**: https://itrf.ign.fr/en/homepage  
- **DOMES**: https://itrf.ign.fr/docs/domes/domes.req
- **Implementation**: Coordinate transformations, reference frames

#### EUREF Permanent Network (EPN)
- **Website**: https://www.epncb.oma.be/
- **Guidelines**: Network operations, data quality
- **Implementation**: Station establishment standards

#### GAMIT/GLOBK (MIT)
- **Website**: https://geoweb.mit.edu/gg/
- **Documentation**: Processing software standards
- **Implementation**: Station information formats

## Validation & Testing

### 🧪 Standards Compliance Testing
```bash
# Test IGS site log compliance
tosGPS sitelog RHOF --validate

# Test RINEX format compliance  
tosGPS rinex RHOF data/*.rnx --validate

# Test GAMIT format compliance
tosGPS PrintTOS RHOF --format gamit | validate_gamit_format.py
```

### 📈 Quality Assurance Metrics
- **Format Validation**: Automated format checking
- **Cross-Reference Validation**: Consistency between standards
- **Real-World Testing**: Validation with actual GPS station data
- **Processing Compatibility**: Integration with GPS processing software

## Future Standards Integration

### 🔮 Planned Additions
1. **RINEX 4.0 Support**: Enhanced precision and metadata
2. **EPN Guidelines**: European network operational standards
3. **Multi-GNSS Standards**: GPS, GLONASS, Galileo, BeiDou integration
4. **Real-Time Standards**: RTCM, NTRIP protocol compliance

### 🚀 Enhancement Roadmap
- **Standards Automation**: Fully automated standards updates
- **Compliance Dashboard**: Real-time standards compliance monitoring
- **Version Management**: Standards version tracking and migration
- **Integration Testing**: Automated testing against multiple standards versions

---

**Maintenance**: This index is automatically updated by the standards management system
**Last Updated**: 2025-08-25 (Professional Site Log Management & IGS v2.0 Compliance)
**Next Review**: Monthly standards update check