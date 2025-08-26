# GPS/GNSS Standards Workflow Integration

This document describes how GPS/GNSS standards are integrated into the tostools development and production workflow.

## 🔄 Current Integration Status

### ✅ **Fully Integrated Standards**

#### IGS Site Log Standards (Production Ready)
- **Command**: `tosGPS sitelog STATION`
- **Standards**: IGS Site Log Instructions v2.0
- **Integration**: Complete - used in production for site log generation
- **Validation**: Automated format checking, coordinate validation
- **Workflow**: Standard development workflow, CI/CD pipeline testing

#### GAMIT Processing Standards (Production Ready)
- **Command**: `tosGPS PrintTOS STATION --format gamit`
- **Standards**: GAMIT/GLOBK station.info format
- **Integration**: Complete - used for GPS processing preparation
- **Validation**: Fixed-width format checking, session validation
- **Workflow**: Integrated into GPS processing workflows

#### RINEX Format Standards (Active Development)
- **Command**: `tosGPS rinex STATION FILES`
- **Standards**: RINEX v2/v3/v4 specifications
- **Integration**: Partial - format preservation implemented
- **Validation**: FORTRAN77 format checking, cross-validation with TOS
- **Workflow**: Development validation, needs production integration

## 🛠️ **Development Workflow Integration**

### Pre-commit Hooks (Automated)
```bash
# Install pre-commit hooks (one-time setup)
pip install pre-commit
pre-commit install

# Hooks run automatically on git commit:
# 1. Code formatting (black, ruff) 
# 2. Basic file checks
# 3. GPS/GNSS standards compliance validation
# 4. Security scanning
```

### Local Development Testing
```bash
# Complete local testing workflow (matches CI pipeline)
ruff check src/                                      # Code linting
black --check src/                                   # Format checking  
pytest tests/ -v                                     # Unit tests
python scripts/update_standards.py --validate-only  # Standards compliance
```

### CI/CD Pipeline Integration
**File**: `.github/workflows/ci.yml`

The CI/CD pipeline automatically validates standards compliance:
```yaml
- name: Validate GPS/GNSS standards compliance
  run: |
    python scripts/update_standards.py --validate-only
```

**Pipeline Steps**:
1. **Code Quality**: Linting, formatting, testing
2. **Standards Validation**: GPS/GNSS compliance checking
3. **Console Script Testing**: Command functionality validation
4. **Package Building**: Distribution creation and validation

## 📊 **Standards Compliance Monitoring**

### Automated Validation Checks
The standards validation system checks:

#### IGS Site Log Compliance
- ✅ Nine-character station ID format (RHOF00ISL)
- ✅ DMS coordinate format conversion
- ✅ Country/language translation tables
- ✅ Section structure and numbering

#### RINEX Format Compliance  
- ✅ FORTRAN77 formatting preservation warnings
- ✅ Header field validation
- ✅ Multi-version support documentation
- ⚠️ Cross-validation with processing software (needs enhancement)

#### GAMIT Processing Compliance
- ✅ Fixed-width column formatting
- ✅ Session-level data validation
- ✅ Equipment change tracking
- ✅ Processing-ready output validation

### Compliance Reporting
```bash
# Generate comprehensive standards compliance report
python scripts/update_standards.py --report standards_compliance.txt

# Example output:
# Standards Update Results:
# IGS Site Log Instructions v2.0        NO CHANGES
# DOMES Station Requirements            NO CHANGES
# RINEX Version 2 Format Specification NO CHANGES
#
# Standards Compliance Issues:
# RINEX:
#   - Missing FORTRAN77 format warning in validator.py
#   - Cross-validation needs enhancement
```

## 🔄 **Standards Update Workflow**

### Monthly Standards Review
```bash
# Check for updated standards documents
python scripts/update_standards.py

# If updates found:
# 1. Review changes in updated documents
# 2. Assess impact on existing code
# 3. Update implementation if needed
# 4. Run comprehensive testing
# 5. Update documentation
```

### Standards Document Management
```bash
# Local standards repository structure
docs/standards/local_copies/
├── sitelog_instr_v2.0.txt     # IGS Site Log Instructions
├── domes.req                  # DOMES Requirements
├── rinex_v2_format.txt        # RINEX v2 Specification
└── rinex_v3_format.txt        # RINEX v3 Specification
```

### Change Detection and Backup
- **SHA-256 hashing**: Detects document changes automatically
- **Automatic backup**: Previous versions preserved with timestamps
- **Change logging**: All updates logged with timestamps and descriptions

## 🎯 **Production Workflow Integration**

### GPS Station Processing Workflow
```bash
# 1. Station metadata extraction with standards compliance
tosGPS PrintTOS STATION --format gamit > stations.info

# 2. IGS-compliant site log generation  
tosGPS sitelog STATION --auto-filename --dir ./sitelogs

# 3. RINEX file validation and correction
tosGPS rinex STATION data/*.rnx --fix --backup

# 4. Quality control validation
tosGPS --log-level INFO PrintTOS STATION --validate
```

### Standards-Compliant Data Processing
1. **Input Validation**: All GPS data validated against standards before processing
2. **Format Compliance**: Output formats meet IGS, RINEX, and GAMIT requirements  
3. **Quality Assurance**: Cross-validation between different standard implementations
4. **Error Handling**: Standards violations flagged and reported

## 🔧 **Implementation Examples**

### IGS Site Log Generation (Standards-Compliant)
```python
# Generate IGS v2.0 compliant site log
def generate_igs_sitelog(station_marker: str, options: Dict) -> str:
    """Generate IGS v2.0 compliant site log."""
    
    # Nine-character station ID (IGS standard)
    station_id = f"{station_marker.upper()}00ISL"
    
    # DMS coordinate conversion (IGS requirement)  
    lat_dms = decimal_to_dms(station_data['lat'])
    lon_dms = decimal_to_dms(station_data['lon'])
    
    # Country code translation (IGS compliance)
    country = translate_country_code(station_data['country'])
    
    return site_log_content
```

### RINEX Format Preservation (Standards-Compliant)
```python
# RINEX editing with FORTRAN77 compliance
def edit_rinex_header(file_path: str, corrections: Dict) -> None:
    """Edit RINEX header preserving FORTRAN77 format."""
    
    # WARNING: RINEX uses strict FORTRAN77 column formatting
    # Spaces vs tabs matter - must use exact spacing, not tabs
    header_line = f"{field:<20}{value:>40}COMMENT"
    
    # Preserve exact column positions
    with open(file_path, 'r') as f:
        lines = f.readlines()
    
    # Apply corrections while maintaining format
    # ...
```

### GAMIT Format Generation (Standards-Compliant)
```python
# GAMIT station.info format generation
def format_gamit_station(station_data: Dict, session: Dict) -> str:
    """Format station data for GAMIT processing."""
    
    # Fixed-width column formatting (GAMIT requirement)
    marker = station_data['marker'].upper()
    lat = float(station_data['lat'])
    lon = float(station_data['lon'])  
    alt = float(station_data['altitude'])
    
    # YYDDD date format (GAMIT standard)
    start_date = format_gamit_date(session['time_from'])
    end_date = format_gamit_date(session.get('time_to', '99999'))
    
    # Exact column positioning for GAMIT compatibility
    return f"{marker:<4} {lat:8.5f} {lon:9.5f} {alt:7.1f} {start_date:>5} {end_date:>5} {name:<19}"
```

## 🚀 **Future Enhancement Plans**

### Enhanced Standards Integration
1. **Real-time Standards Monitoring**: Automated alerts for standards updates
2. **Enhanced Validation**: More comprehensive compliance checking
3. **Multi-standard Cross-validation**: Consistency checking across standards
4. **Standards Version Management**: Support for multiple standard versions

### Workflow Automation
1. **Standards-driven Development**: Code generation based on standards
2. **Automated Migration**: Code updates when standards change
3. **Compliance Dashboard**: Real-time standards compliance monitoring
4. **Integration Testing**: Automated testing against reference implementations

### Production Enhancements
1. **Standards Traceability**: Track which standards version used for each output
2. **Compliance Certification**: Generate standards compliance certificates
3. **Quality Metrics**: Measure compliance levels across processing workflows
4. **Standards Auditing**: Regular comprehensive standards compliance audits

---

**Summary**: GPS/GNSS standards are now **actively integrated** into the tostools development and production workflow through automated validation, CI/CD pipeline checking, and comprehensive compliance monitoring. The system ensures continued standards compliance while providing developers with clear guidance and automated validation tools.

**Status**: ✅ **Production Ready** - Standards workflow integration complete and operational.