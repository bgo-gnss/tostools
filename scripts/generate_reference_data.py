#!/usr/bin/env python3
"""
Generate reference data using the legacy tosGPS system.

This script runs the original legacy system to create baseline reference data
that we can validate our new modular system against.
"""

import json
import sys
import os
from pathlib import Path

# Add src to path to import tostools
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from tostools import gps_metadata_qc as legacy_qc
from tostools.core.site_log import generate_igs_site_log

def main():
    station = "RHOF"
    url_rest = "https://vi-api.vedur.is:443/tos/v1"
    reference_dir = Path("reference_data") / station
    reference_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Generating reference data for station {station}")
    print(f"Reference directory: {reference_dir}")
    
    # 1. Generate complete station metadata using legacy system
    print("\n1. Retrieving station metadata using legacy system...")
    try:
        legacy_station_data = legacy_qc.gps_metadata(station, url_rest)
        
        # Save raw JSON structure
        with open(reference_dir / "legacy_station_metadata.json", 'w') as f:
            json.dump(legacy_station_data, f, indent=2, default=str)
        print(f"✓ Saved legacy station metadata JSON")
        
        # Generate site log using legacy data
        print("\n2. Generating site log using legacy system...")
        device_sessions = legacy_station_data.get('device_history', [])
        site_log_content = generate_igs_site_log(legacy_station_data, device_sessions)
        
        with open(reference_dir / "legacy_sitelog.txt", 'w') as f:
            f.write(site_log_content)
        print(f"✓ Saved legacy site log")
        
        # Generate print output using legacy system  
        print("\n3. Generating print output using legacy system...")
        from tostools import gps_metadata_functions as legacy_funcs
        from io import StringIO
        import sys
        
        # Capture print output
        old_stdout = sys.stdout
        sys.stdout = captured_output = StringIO()
        
        try:
            legacy_funcs.print_station_history(legacy_station_data, raw_format=False)
            print_output = captured_output.getvalue()
        finally:
            sys.stdout = old_stdout
            
        with open(reference_dir / "legacy_print_output.txt", 'w') as f:
            f.write(print_output)
        print(f"✓ Saved legacy print output")
        
        # Save key statistics for comparison
        stats = {
            "station": station,
            "device_history_sessions": len(device_sessions),
            "station_data_keys": list(legacy_station_data.keys()),
            "coordinates": {
                "lat": legacy_station_data.get('lat'),
                "lon": legacy_station_data.get('lon'), 
                "altitude": legacy_station_data.get('altitude')
            },
            "device_types_found": []
        }
        
        # Analyze device sessions
        for session in device_sessions:
            for key in session:
                if key not in ['time_from', 'time_to'] and key not in stats["device_types_found"]:
                    stats["device_types_found"].append(key)
                    
        with open(reference_dir / "legacy_stats.json", 'w') as f:
            json.dump(stats, f, indent=2, default=str)
        print(f"✓ Saved legacy statistics")
        
        # 4. Generate RINEX validation using legacy system
        print(f"\n4. Generating RINEX validation using legacy system...")
        rinex_file = "tmp/RHOF0790.02D"  # Use uncompressed file
        
        if Path(rinex_file).exists():
            try:
                # Use legacy RINEX validation components
                from tostools.rinex.reader import read_rinex_header, extract_header_info
                from tostools.rinex.validator import compare_rinex_to_tos
                
                # Read RINEX header
                header_data = read_rinex_header(Path(rinex_file))
                if header_data:
                    rinex_info = extract_header_info(header_data)
                    comparison = compare_rinex_to_tos(rinex_info, legacy_station_data)
                    
                    # Save RINEX validation results
                    rinex_results = {
                        "rinex_file": rinex_file,
                        "rinex_info": rinex_info,
                        "comparison": comparison,
                        "discrepancies_count": len(comparison.get("discrepancies", {})),
                        "corrections_count": len(comparison.get("corrections", {}))
                    }
                    
                    with open(reference_dir / "legacy_rinex_validation.json", 'w') as f:
                        json.dump(rinex_results, f, indent=2, default=str)
                    print(f"✓ Saved legacy RINEX validation")
                    
                    stats["rinex_validation"] = {
                        "file": rinex_file,
                        "discrepancies": rinex_results["discrepancies_count"],
                        "corrections": rinex_results["corrections_count"]
                    }
                else:
                    print(f"⚠ Could not read RINEX header from {rinex_file}")
                    
            except Exception as e:
                print(f"⚠ RINEX validation failed: {e}")
        else:
            print(f"⚠ RINEX file not found: {rinex_file}")
        
        # Update stats with final info
        with open(reference_dir / "legacy_stats.json", 'w') as f:
            json.dump(stats, f, indent=2, default=str)
        
        print(f"\n✅ Reference data generation complete!")
        print(f"   - Station sessions: {stats['device_history_sessions']}")
        print(f"   - Device types: {stats['device_types_found']}")
        print(f"   - Coordinates: lat={stats['coordinates']['lat']}, lon={stats['coordinates']['lon']}")
        if "rinex_validation" in stats:
            print(f"   - RINEX discrepancies: {stats['rinex_validation']['discrepancies']}")
        
    except Exception as e:
        print(f"❌ Error generating reference data: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()