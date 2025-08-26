# GPS/GNSS Standards Documentation Repository

This directory contains comprehensive documentation and local copies of GPS/GNSS standards that tostools adheres to.

## Directory Structure

```
docs/standards/
├── README.md                   # This overview document
├── igs/                       # International GNSS Service standards
│   ├── sitelog/              # Site log standards and templates
│   ├── station_naming/       # Station naming conventions
│   └── data_formats/         # Data format specifications
├── itrf/                     # International Terrestrial Reference Frame
│   ├── domes/               # DOMES (Directory of GNSS Monitoring Stations)
│   ├── coordinates/         # Coordinate reference systems
│   └── transformations/     # Datum transformations
├── epn/                     # EUREF Permanent Network
│   ├── guidelines/          # EPN operational guidelines
│   └── data_flow/          # Data processing workflows
├── gamit/                   # GAMIT/GLOBK processing standards
│   ├── station_info/       # Station information file formats
│   ├── processing/         # Processing configuration standards
│   └── solutions/          # Solution standards and conventions
├── rinex/                   # RINEX format specifications
│   ├── v2/                 # RINEX version 2 standards
│   ├── v3/                 # RINEX version 3 standards
│   ├── v4/                 # RINEX version 4 standards
│   └── fortran_formatting/ # FORTRAN77 formatting requirements
└── local_copies/            # Local copies of key standards documents
    ├── sitelog_instr_v2.0.txt
    ├── domes.req
    └── rinex_format_specs/
```

## Standards Sources

### Primary Authoritative Sources

#### International GNSS Service (IGS)
- **Website**: https://igs.org/
- **Standards Portal**: https://files.igs.org/pub/station/general/
- **Key Documents**:
  - Site Log Instructions v2.0: https://files.igs.org/pub/station/general/sitelog_instr_v2.0.txt
  - Station Guidelines: https://igs.org/wg/site-guidelines/
  - Data Standards: https://igs.org/data-products/

#### International Terrestrial Reference Frame (ITRF)
- **Website**: https://itrf.ign.fr/en/homepage
- **DOMES Documentation**: https://itrf.ign.fr/docs/domes/domes.req
- **Coordinate Standards**: https://itrf.ign.fr/en/solutions
- **Key Resources**:
  - DOMES Requirements: Station metadata standards
  - ITRF Solutions: Reference frame realizations
  - Site Information: Station log requirements

#### EUREF Permanent Network (EPN)
- **Website**: https://www.epncb.oma.be/
- **Guidelines**: https://epncb.oma.be/_documentation/guidelines/
- **Data Processing**: https://epncb.oma.be/_productsservices/
- **Standards Coverage**:
  - Station establishment guidelines
  - Data quality standards
  - Processing procedures

#### GAMIT/GLOBK (MIT)
- **Website**: https://geoweb.mit.edu/gg/
- **Documentation**: https://geoweb.mit.edu/gg/docs.php
- **File Formats**: Station information, processing configuration
- **Standards Focus**:
  - station.info file format
  - Processing configuration standards
  - Solution file formats

### Implementation in tostools

#### Site Log Generation (`tosGPS sitelog`)
- **Standard**: IGS Site Log Instructions v2.0
- **Implementation**: `src/tostools/legacy/gps_metadata_functions.py:site_log()`
- **Compliance Features**:
  - Nine-character station IDs (e.g., RHOF00ISL)
  - Proper coordinate formatting (DMS)
  - Section numbering and structure
  - Country code translation (Iceland→ISL)

#### Station Metadata (`tosGPS PrintTOS --format gamit`)
- **Standard**: GAMIT/GLOBK station.info format
- **Implementation**: `src/tostools/gps_metadata_functions.py:print_station_info()`
- **Features**:
  - Fixed-width column formatting
  - Session-level validation
  - Equipment change tracking

#### RINEX Processing (`tosGPS rinex`)
- **Standards**: RINEX v2, v3, v4 specifications
- **Implementation**: `src/tostools/rinex/` modules
- **Critical**: FORTRAN77 formatting requirements

## Standards Integration Workflow

### 1. Local Standards Repository
All critical standards documents are stored locally in `docs/standards/local_copies/` to ensure:
- **Offline Access**: No network dependency during development
- **Version Control**: Track standards evolution over time
- **Consistency**: Ensure all developers use same standard versions

### 2. Automated Standards Updates
Future implementation will include:
- **Periodic checks** for updated standards documents
- **Automated download** of new versions with change detection
- **Version tracking** and migration guidance for code updates

### 3. Code Compliance Validation
Each standards-compliant module includes:
- **Reference documentation** linking to specific standard sections
- **Validation functions** to ensure output meets standards
- **Test cases** based on standard examples and requirements

## Usage Examples

### Accessing Standards Information
```bash
# View current standards compliance
find docs/standards/ -name "*.md" -exec grep -l "tosGPS" {} \;

# Check local standards documents
ls docs/standards/local_copies/

# Validate against specific standard
tosGPS sitelog RHOF --validate  # Uses IGS v2.0 standards
```

### Development Integration
```python
# Reference standards in code comments
# Standard: IGS Site Log Instructions v2.0, Section 1.1
# Document: docs/standards/local_copies/sitelog_instr_v2.0.txt
# Implementation: Following nine-character ID convention
station_id = f"{marker.upper()}00{country_code.upper()}"
```

## Maintenance and Updates

### Regular Maintenance Tasks
1. **Monthly**: Check for updated standards documents from primary sources
2. **Quarterly**: Review code compliance with current standards
3. **Annually**: Comprehensive standards audit and gap analysis

### Update Workflow
1. **Detection**: Automated or manual identification of standards updates
2. **Analysis**: Impact assessment on existing code implementation
3. **Integration**: Update local copies and code implementation
4. **Validation**: Test compliance with updated standards
5. **Documentation**: Update this repository with changes

## Contributing Standards Information

### Adding New Standards
1. Create appropriate directory structure under relevant category
2. Add local copy to `local_copies/` with version date
3. Document implementation impact in relevant code modules
4. Add validation examples and test cases

### Updating Existing Standards
1. Compare new version with current local copy
2. Document changes and implementation impact
3. Update code compliance as needed
4. Maintain backward compatibility where possible

---

*This standards repository ensures tostools maintains compliance with authoritative GPS/GNSS standards while providing developers with comprehensive reference documentation.*