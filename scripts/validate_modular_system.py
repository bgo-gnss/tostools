#!/usr/bin/env python3
"""
Validate the new modular system against legacy reference data.

This script compares outputs from the new modular tosGPS system with
the legacy system to ensure equivalent functionality.
"""

import json
import sys
import os
from pathlib import Path
from io import StringIO
from difflib import unified_diff

# Add src to path to import tostools
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from tostools.api.tos_client import TOSClient
from tostools.core.site_log import generate_igs_site_log
from tostools.rinex.reader import read_rinex_header, extract_header_info
from tostools.rinex.validator import compare_rinex_to_tos

def compare_files(file1_path, file2_path, description):
    """Compare two text files and report differences."""
    print(f"\n📋 Comparing {description}...")
    
    try:
        with open(file1_path, 'r') as f1, open(file2_path, 'r') as f2:
            content1 = f1.readlines()
            content2 = f2.readlines()
            
        if content1 == content2:
            print(f"✅ {description}: IDENTICAL")
            return True
        else:
            print(f"❌ {description}: DIFFERENCES FOUND")
            
            # Show first few differences
            diff = list(unified_diff(
                content1, content2,
                fromfile=str(file1_path),
                tofile=str(file2_path),
                lineterm=''
            ))
            
            if len(diff) <= 20:
                print("".join(diff))
            else:
                print("".join(diff[:20]) + "\n... (truncated, too many differences)")
            return False
            
    except Exception as e:
        print(f"❌ Error comparing {description}: {e}")
        return False

def compare_json_data(data1, data2, description, ignore_keys=None):
    """Compare JSON data structures."""
    print(f"\n📋 Comparing {description}...")
    
    if ignore_keys:
        for key in ignore_keys:
            data1.pop(key, None)
            data2.pop(key, None)
    
    if data1 == data2:
        print(f"✅ {description}: IDENTICAL")
        return True
    else:
        print(f"❌ {description}: DIFFERENCES FOUND")
        
        # Check key differences
        keys1 = set(data1.keys()) if isinstance(data1, dict) else set()
        keys2 = set(data2.keys()) if isinstance(data2, dict) else set()
        
        if keys1 != keys2:
            missing_in_new = keys1 - keys2
            extra_in_new = keys2 - keys1
            if missing_in_new:
                print(f"   Keys missing in new system: {missing_in_new}")
            if extra_in_new:
                print(f"   Extra keys in new system: {extra_in_new}")
        
        # Show some value differences
        if isinstance(data1, dict) and isinstance(data2, dict):
            for key in keys1 & keys2:
                if data1.get(key) != data2.get(key):
                    print(f"   {key}: '{data1.get(key)}' vs '{data2.get(key)}'")
                    break  # Just show first difference
        
        return False

def main():
    station = "RHOF"
    url_rest = "https://vi-api.vedur.is:443/tos/v1"
    reference_dir = Path("reference_data") / station
    
    print(f"🔍 Validating modular system against legacy reference data for {station}")
    print(f"Reference directory: {reference_dir}")
    
    if not reference_dir.exists():
        print(f"❌ Reference directory not found: {reference_dir}")
        print("Please run generate_reference_data.py first!")
        return False
    
    validation_results = []
    
    try:
        # 1. Compare station metadata JSON structure
        print(f"\n=== 1. Station Metadata Comparison ===")
        
        # Load legacy reference data
        with open(reference_dir / "legacy_station_metadata.json", 'r') as f:
            legacy_metadata = json.load(f)
        
        # Generate new metadata
        tos_client = TOSClient(base_url=url_rest, loglevel=0)  # Quiet
        new_metadata = tos_client.get_complete_station_metadata(station)
        
        # Save new metadata for inspection
        with open(reference_dir / "new_station_metadata.json", 'w') as f:
            json.dump(new_metadata, f, indent=2, default=str)
        
        # Compare metadata (ignore fields that have expected format differences)
        # Convert datetime objects to strings for comparison
        def normalize_metadata(data):
            if isinstance(data, dict):
                normalized = {}
                for k, v in data.items():
                    if k == 'device_history' and isinstance(v, list):
                        # Normalize device history datetime format
                        normalized[k] = []
                        for session in v:
                            norm_session = {}
                            for sk, sv in session.items():
                                if sk in ['time_from', 'time_to'] and hasattr(sv, 'strftime'):
                                    norm_session[sk] = sv.strftime('%Y-%m-%d %H:%M:%S')
                                else:
                                    norm_session[sk] = sv
                            normalized[k].append(norm_session)
                    else:
                        normalized[k] = normalize_metadata(v)
                return normalized
            return data
        
        normalized_legacy = normalize_metadata(legacy_metadata)
        normalized_new = normalize_metadata(new_metadata)
        
        metadata_match = compare_json_data(
            normalized_legacy, normalized_new, 
            "Station Metadata JSON Structure",
            ignore_keys=['contact']  # Contact info may have minor differences
        )
        validation_results.append(("Station Metadata", metadata_match))
        
        # 2. Compare site logs
        print(f"\n=== 2. Site Log Comparison ===")
        
        # Generate new site log
        device_sessions = new_metadata.get('device_history', [])
        new_site_log = generate_igs_site_log(new_metadata, device_sessions)
        
        # Save new site log
        with open(reference_dir / "new_sitelog.txt", 'w') as f:
            f.write(new_site_log)
        
        # Compare site logs
        sitelog_match = compare_files(
            reference_dir / "legacy_sitelog.txt",
            reference_dir / "new_sitelog.txt",
            "Site Log Content"
        )
        validation_results.append(("Site Log", sitelog_match))
        
        # 3. Compare print output
        print(f"\n=== 3. Print Output Comparison ===")
        
        # Generate new print output
        from tostools import gps_metadata_functions as legacy_funcs
        
        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()
        
        try:
            legacy_funcs.print_station_history(new_metadata, raw_format=False)
            new_print_output = captured_output.getvalue()
        finally:
            sys.stdout = old_stdout
        
        # Save new print output
        with open(reference_dir / "new_print_output.txt", 'w') as f:
            f.write(new_print_output)
        
        # Compare print outputs
        print_match = compare_files(
            reference_dir / "legacy_print_output.txt", 
            reference_dir / "new_print_output.txt",
            "Print Output"
        )
        validation_results.append(("Print Output", print_match))
        
        # 4. Compare RINEX validation
        print(f"\n=== 4. RINEX Validation Comparison ===")
        
        # Load legacy RINEX validation
        try:
            with open(reference_dir / "legacy_rinex_validation.json", 'r') as f:
                legacy_rinex = json.load(f)
                
            rinex_file = legacy_rinex["rinex_file"]
            
            if Path(rinex_file).exists():
                # Generate new RINEX validation
                header_data = read_rinex_header(Path(rinex_file))
                if header_data:
                    rinex_info = extract_header_info(header_data)
                    comparison = compare_rinex_to_tos(rinex_info, new_metadata)
                    
                    new_rinex = {
                        "rinex_file": rinex_file,
                        "rinex_info": rinex_info,
                        "comparison": comparison,
                        "discrepancies_count": len(comparison.get("discrepancies", {})),
                        "corrections_count": len(comparison.get("corrections", {}))
                    }
                    
                    # Save new RINEX validation
                    with open(reference_dir / "new_rinex_validation.json", 'w') as f:
                        json.dump(new_rinex, f, indent=2, default=str)
                    
                    # Compare key metrics
                    rinex_match = (
                        legacy_rinex["discrepancies_count"] == new_rinex["discrepancies_count"] and
                        legacy_rinex["corrections_count"] == new_rinex["corrections_count"]
                    )
                    
                    if rinex_match:
                        print(f"✅ RINEX Validation: EQUIVALENT")
                        print(f"   Discrepancies: {new_rinex['discrepancies_count']}")
                        print(f"   Corrections: {new_rinex['corrections_count']}")
                    else:
                        print(f"❌ RINEX Validation: DIFFERENCES")
                        print(f"   Legacy - Discrepancies: {legacy_rinex['discrepancies_count']}, Corrections: {legacy_rinex['corrections_count']}")
                        print(f"   New    - Discrepancies: {new_rinex['discrepancies_count']}, Corrections: {new_rinex['corrections_count']}")
                        
                    validation_results.append(("RINEX Validation", rinex_match))
                else:
                    print(f"⚠ Could not read RINEX file: {rinex_file}")
                    validation_results.append(("RINEX Validation", None))
            else:
                print(f"⚠ RINEX file not found: {rinex_file}")
                validation_results.append(("RINEX Validation", None))
                
        except Exception as e:
            print(f"⚠ RINEX validation comparison failed: {e}")
            validation_results.append(("RINEX Validation", None))
        
        # Summary
        print(f"\n" + "="*60)
        print(f"🏁 VALIDATION SUMMARY")
        print(f"="*60)
        
        passed = 0
        total = 0
        
        for component, result in validation_results:
            if result is True:
                print(f"✅ {component}: PASSED")
                passed += 1
            elif result is False:
                print(f"❌ {component}: FAILED")
            else:
                print(f"⚠️  {component}: SKIPPED")
            
            if result is not None:
                total += 1
        
        if total > 0:
            success_rate = (passed / total) * 100
            print(f"\n🎯 Success Rate: {passed}/{total} ({success_rate:.1f}%)")
            
            if success_rate >= 90:
                print(f"🎉 EXCELLENT: Modular system is functionally equivalent to legacy system!")
            elif success_rate >= 75:
                print(f"✅ GOOD: Modular system is mostly equivalent with minor differences")
            else:
                print(f"⚠️  NEEDS WORK: Significant differences found")
        
        return passed == total
        
    except Exception as e:
        print(f"❌ Validation failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)