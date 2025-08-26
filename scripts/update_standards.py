#!/usr/bin/env python3
"""
Automated GPS/GNSS Standards Update System

This script automatically checks for updates to GPS/GNSS standards documents
and maintains the local standards repository.
"""

import os
import sys
import requests
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class StandardsUpdater:
    """Manages updates to GPS/GNSS standards documentation."""
    
    def __init__(self, standards_dir: str = "docs/standards"):
        self.standards_dir = Path(standards_dir)
        self.local_copies_dir = self.standards_dir / "local_copies"
        self.local_copies_dir.mkdir(parents=True, exist_ok=True)
        
        # Standards sources configuration
        self.standards_sources = {
            'igs_sitelog_v2': {
                'url': 'https://files.igs.org/pub/station/general/sitelog_instr_v2.0.txt',
                'local_path': 'sitelog_instr_v2.0.txt',
                'description': 'IGS Site Log Instructions v2.0',
                'check_frequency': 30  # days
            },
            'domes_requirements': {
                'url': 'https://itrf.ign.fr/docs/domes/domes.req',
                'local_path': 'domes.req', 
                'description': 'DOMES Station Requirements',
                'check_frequency': 60  # days
            },
            'rinex2_format': {
                'url': 'ftp://igs.org/pub/data/format/rinex2.txt',
                'local_path': 'rinex_v2_format.txt',
                'description': 'RINEX Version 2 Format Specification',
                'check_frequency': 90  # days
            },
            'rinex3_format': {
                'url': 'ftp://igs.org/pub/data/format/rinex3.txt', 
                'local_path': 'rinex_v3_format.txt',
                'description': 'RINEX Version 3 Format Specification',
                'check_frequency': 90  # days
            }
        }
        
    def get_file_hash(self, file_path: Path) -> Optional[str]:
        """Calculate SHA-256 hash of file for change detection."""
        if not file_path.exists():
            return None
            
        try:
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            logger.error(f"Error calculating hash for {file_path}: {e}")
            return None
    
    def download_file(self, url: str, local_path: Path) -> bool:
        """Download file from URL to local path."""
        try:
            logger.info(f"Downloading {url}")
            
            # Handle FTP URLs differently
            if url.startswith('ftp://'):
                # For FTP, we might need to use a different approach
                # For now, try converting to HTTP
                http_url = url.replace('ftp://igs.org', 'https://files.igs.org')
                response = requests.get(http_url, timeout=30)
            else:
                response = requests.get(url, timeout=30)
            
            response.raise_for_status()
            
            # Write to local file
            with open(local_path, 'wb') as f:
                f.write(response.content)
                
            logger.info(f"Successfully downloaded to {local_path}")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error downloading {url}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            return False
    
    def check_for_updates(self, force_check: bool = False) -> Dict[str, bool]:
        """Check for updates to standards documents."""
        results = {}
        
        for standard_id, config in self.standards_sources.items():
            logger.info(f"Checking {config['description']}")
            
            local_path = self.local_copies_dir / config['local_path']
            
            # Check if we need to update based on frequency
            if local_path.exists() and not force_check:
                file_age = datetime.now() - datetime.fromtimestamp(local_path.stat().st_mtime)
                if file_age.days < config['check_frequency']:
                    logger.info(f"Skipping {standard_id} - checked recently")
                    results[standard_id] = False
                    continue
            
            # Get current hash if file exists
            current_hash = self.get_file_hash(local_path)
            
            # Download to temporary location
            temp_path = local_path.with_suffix(local_path.suffix + '.tmp')
            
            if self.download_file(config['url'], temp_path):
                new_hash = self.get_file_hash(temp_path)
                
                if current_hash != new_hash:
                    # File has changed, replace the original
                    if local_path.exists():
                        # Backup old version
                        backup_path = local_path.with_suffix(
                            f"{local_path.suffix}.backup_{datetime.now().strftime('%Y%m%d')}"
                        )
                        local_path.rename(backup_path)
                        logger.info(f"Backed up old version to {backup_path}")
                    
                    temp_path.rename(local_path)
                    logger.info(f"Updated {config['description']}")
                    results[standard_id] = True
                else:
                    # No changes, remove temp file
                    temp_path.unlink()
                    logger.info(f"No changes to {config['description']}")
                    results[standard_id] = False
            else:
                # Download failed
                if temp_path.exists():
                    temp_path.unlink()
                results[standard_id] = False
        
        return results
    
    def validate_standards_compliance(self) -> Dict[str, List[str]]:
        """Validate that tostools code complies with current standards."""
        validation_results = {}
        
        # Check IGS site log compliance
        sitelog_issues = self._validate_sitelog_compliance()
        if sitelog_issues:
            validation_results['sitelog'] = sitelog_issues
        
        # Check RINEX format compliance  
        rinex_issues = self._validate_rinex_compliance()
        if rinex_issues:
            validation_results['rinex'] = rinex_issues
        
        # Check GAMIT format compliance
        gamit_issues = self._validate_gamit_compliance()
        if gamit_issues:
            validation_results['gamit'] = gamit_issues
        
        return validation_results
    
    def _validate_sitelog_compliance(self) -> List[str]:
        """Validate site log generation against IGS standards."""
        issues = []
        
        # Check if site log function exists and is properly implemented
        sitelog_file = Path("src/tostools/legacy/gps_metadata_functions.py")
        if sitelog_file.exists():
            content = sitelog_file.read_text()
            
            # Check for required IGS v2.0 features
            required_features = [
                "Nine Character ID",  # Station ID format
                "DDMMSS.SS",         # Coordinate format
                "Modified/Added Sections",  # Report sections
                "YYYY-MM-DDTHH:MMZ"  # Time format
            ]
            
            for feature in required_features:
                if feature not in content:
                    issues.append(f"Missing IGS v2.0 feature: {feature}")
        else:
            issues.append("Site log generation module not found")
        
        return issues
    
    def _validate_rinex_compliance(self) -> List[str]:
        """Validate RINEX processing against format standards.""" 
        issues = []
        
        # Check RINEX modules
        rinex_dir = Path("src/tostools/rinex")
        if rinex_dir.exists():
            # Check for FORTRAN77 format preservation warnings
            for module_file in rinex_dir.glob("*.py"):
                content = module_file.read_text()
                if "FORTRAN77" not in content:
                    issues.append(f"Missing FORTRAN77 format warning in {module_file.name}")
        else:
            issues.append("RINEX processing modules not found")
        
        return issues
    
    def _validate_gamit_compliance(self) -> List[str]:
        """Validate GAMIT format against processing standards."""
        issues = []
        
        # Check GAMIT format implementation
        gamit_file = Path("src/tostools/gps_metadata_functions.py")
        if gamit_file.exists():
            content = gamit_file.read_text()
            
            # Check for fixed-width formatting
            if "fixed-width" not in content.lower():
                issues.append("Missing fixed-width format documentation")
            
            # Check for session validation
            if "validate_gamit_session" not in content:
                issues.append("Missing GAMIT session validation")
        
        return issues
    
    def generate_update_report(self, update_results: Dict[str, bool], 
                             validation_results: Dict[str, List[str]]) -> str:
        """Generate comprehensive update and validation report."""
        report_lines = [
            f"GPS/GNSS Standards Update Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 70,
            ""
        ]
        
        # Standards update results
        report_lines.extend([
            "Standards Update Results:",
            "-" * 25
        ])
        
        for standard_id, updated in update_results.items():
            config = self.standards_sources[standard_id]
            status = "UPDATED" if updated else "NO CHANGES"
            report_lines.append(f"{config['description']:<40} {status}")
        
        report_lines.append("")
        
        # Validation results
        if validation_results:
            report_lines.extend([
                "Standards Compliance Issues:",
                "-" * 28
            ])
            
            for component, issues in validation_results.items():
                report_lines.append(f"\n{component.upper()}:")
                for issue in issues:
                    report_lines.append(f"  - {issue}")
        else:
            report_lines.append("✅ All standards compliance checks passed")
        
        report_lines.extend([
            "",
            "Next Steps:",
            "-" * 11,
            "1. Review any updated standards documents in docs/standards/local_copies/",
            "2. Address any compliance issues identified above",
            "3. Update code implementation if standards have changed",
            "4. Run tests to ensure continued compatibility",
            "",
            f"Report generated by: {__file__}",
            f"Standards repository: {self.standards_dir}",
        ])
        
        return "\n".join(report_lines)

def main():
    """Main script entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="GPS/GNSS Standards Update System"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force check all standards regardless of last check time"
    )
    parser.add_argument(
        "--validate-only", action="store_true", 
        help="Only run validation checks, skip downloading"
    )
    parser.add_argument(
        "--report", type=str,
        help="Save report to specified file"
    )
    
    args = parser.parse_args()
    
    # Initialize updater
    updater = StandardsUpdater()
    
    # Run updates unless validation-only mode
    if args.validate_only:
        update_results = {}
        logger.info("Skipping standards updates (validation-only mode)")
    else:
        logger.info("Checking for standards updates...")
        update_results = updater.check_for_updates(force_check=args.force)
    
    # Run validation checks
    logger.info("Running standards compliance validation...")
    validation_results = updater.validate_standards_compliance()
    
    # Generate report
    report = updater.generate_update_report(update_results, validation_results)
    
    if args.report:
        with open(args.report, 'w') as f:
            f.write(report)
        logger.info(f"Report saved to {args.report}")
    else:
        print("\n" + report)
    
    # Exit with error code if validation issues found
    if validation_results:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()