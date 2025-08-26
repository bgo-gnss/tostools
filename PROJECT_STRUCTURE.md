# tostools Project Structure

This document provides an overview of the organized project structure for better navigation and maintenance.

## 📁 Root Directory Structure

```
tostools/
├── src/tostools/           # Main source code
├── docs/                   # Documentation and standards
├── tests/                  # Test suite
├── scripts/                # Utility scripts
├── bin/                    # Binary tools (RINEX processing)
├── import_scripts/         # Database import utilities
├── logs/                   # Application logs
├── sitelogs/               # Generated IGS site logs
├── reference_data/         # Reference implementations
├── tmp/                    # Organized temporary data
├── *.md                    # Project documentation
└── pyproject.toml          # Package configuration
```

## 🗂️ Key Directories

### `src/tostools/` - Main Application Code
- **tosGPS.py** - Main GPS QC application entry point
- **cli/** - Command-line interface modules (modular architecture)
- **api/** - TOS API client modules
- **core/** - Core business logic and data models
- **rinex/** - RINEX processing modules
- **io/** - Input/Output formatting utilities
- **utils/** - Shared utilities (logging, etc.)
- **legacy/** - Original modules (transition period)

### `docs/standards/` - GPS/GNSS Standards Repository
- **README.md** - Main standards overview
- **local_copies/** - Local standards documents (IGS, RINEX, etc.)
- **igs/** - International GNSS Service standards
- **rinex/** - RINEX format specifications
- **gamit/** - GAMIT/GLOBK processing standards
- **itrf/** - International Terrestrial Reference Frame
- **epn/** - EUREF Permanent Network standards

### `tmp/organized/` - Organized Temporary Data
- **rinex/** - RINEX observation files (*.D, *.gz)
- **sitelogs/** - Generated site logs and station logs
- **station_data/** - GPS station configuration and metadata
- **test_files/** - Test outputs and debug files
- **reference_data/** - Reference standards and documentation

### `scripts/` - Utility Scripts
- **update_standards.py** - Automated GPS/GNSS standards management
- **generate_reference_data.py** - Reference data generation
- **validate_modular_system.py** - System validation utilities

### `import_scripts/` - Database Import Tools
- Meteorological station import utilities
- Access database processing scripts
- Operational classification tools

## 🏗️ Architecture Overview

### Current State (v0.2.4)
- **Production Ready**: Core GPS QC functionality fully operational
- **Modular Foundation**: New architecture modules created and tested
- **Legacy Compatibility**: Original interfaces maintained during transition
- **Standards Compliance**: Full IGS v2.0 and RINEX compliance

### Key Features
- **Rich Visual Display**: Color-coded tables for manual QC workflows
- **Clean Data Export**: Automation-friendly output formats
- **GAMIT Integration**: Production-ready GPS processing format
- **IGS Compliance**: Professional site log generation
- **Robust Validation**: RINEX vs TOS metadata cross-validation

## 📊 Data Flow

1. **Station Query** → TOS API → GPS metadata retrieval
2. **Data Processing** → Equipment history → Session validation
3. **Format Output** → Rich/Table/JSON/GAMIT → User workflows
4. **Quality Control** → RINEX validation → Site log generation
5. **Standards Compliance** → IGS/RINEX/GAMIT → Professional output

## 🔧 Development Guidelines

### File Organization
- Keep source code in `src/tostools/`
- Put documentation in `docs/`
- Store utilities in `scripts/`
- Use `tmp/organized/` for development data
- Place tests in `tests/`

### Standards Integration
- All GPS/GNSS functionality must comply with documented standards
- Standards validation runs in CI/CD pipeline
- Local standards repository kept up to date
- Cross-validation between different standards implementations

### Logging and Output
- Clean output by default (stdout for data, stderr for status)
- Comprehensive file logging with level separation
- Structured JSON logging for analysis
- Unix standards compliance for piping and automation

## 📚 Documentation

### Main Documentation Files
- **README.md** - Main project overview and usage
- **CLAUDE.md** - Claude Code session guidance
- **CHANGELOG.md** - Version history and changes
- **TODO-COMMENTS.md** - Technical debt tracking system
- **LOGGING_SYSTEM.md** - Logging architecture documentation
- **VALIDATION_REPORT.md** - System validation results

### Standards Documentation
- Complete GPS/GNSS standards repository in `docs/standards/`
- Individual standard implementations documented
- Workflow integration guidelines
- Compliance validation procedures

## 🎯 Quality Assurance

### Automated Testing
- Unit tests in `tests/`
- CI/CD pipeline with GitHub Actions
- Standards compliance validation
- Console script functionality testing
- Multi-version Python support (3.8-3.13)

### Code Quality
- Pre-commit hooks for formatting and linting
- Ruff linting and Black formatting
- Security scanning with Bandit
- TODO comment tracking system

---

**Last Updated**: 2025-08-26  
**Project Version**: 0.2.4  
**Status**: Production Ready with Modular Architecture Foundation