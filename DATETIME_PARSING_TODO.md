# TODO: Migrate to gtimes Centralized Datetime Parsing

## 🎯 Objective

Replace current duplicate datetime parsing logic in tostools with centralized `gtimes.parse_datetime_flexible()` function once [gtimes PR #1](https://github.com/bennigo/gtimes/pull/1) is merged.

## 📋 Current Implementation

Currently tostools has datetime parsing try-catch blocks scattered across multiple files:

### Files with datetime parsing logic:
1. `src/tostools/tosGPS.py` - Line 687-697 (session filtering)
2. `src/tostools/gps_metadata_functions.py` - Line 817-823 (site log generation)  
3. `src/tostools/legacy/gps_metadata_functions.py` - Line 754-760 (site log generation)

### Current pattern:
```python
try:
    dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M')
except ValueError:
    dt = datetime.strptime(time_str[:19], '%Y-%m-%dT%H:%M:%S')
```

## 🔄 Migration Plan

### Phase 1: Update tostools dependencies
- [ ] Merge gtimes PR #1: "Add flexible datetime parsing for GPS/GNSS applications"
- [ ] Update tostools to use gtimes version with `parse_datetime_flexible()`
- [ ] Update `requirements.txt` or `pyproject.toml`

### Phase 2: Replace datetime parsing calls

**Replace in `src/tostools/tosGPS.py` (lines 687-697):**
```python
# OLD:
try:
    session_start = datetime.strptime(time_str, '%Y-%m-%d %H:%M')
except ValueError:
    session_start = datetime.strptime(time_str[:19], '%Y-%m-%dT%H:%M:%S')

# NEW:
from gtimes import parse_datetime_flexible
session_start = parse_datetime_flexible(time_str)
```

**Replace in `src/tostools/gps_metadata_functions.py` (lines 817-823):**
```python
# OLD:
try:
    station_start_date = dt.strptime(station_start_date, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%MZ")
except ValueError:
    station_start_date = dt.strptime(station_start_date[:19], "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%dT%H:%MZ")

# NEW:
from gtimes import parse_datetime_flexible
station_start_date = parse_datetime_flexible(station_start_date).strftime("%Y-%m-%dT%H:%MZ")
```

**Replace in `src/tostools/legacy/gps_metadata_functions.py` (lines 754-760):**
```python
# Same pattern as above
```

### Phase 3: Testing and validation
- [ ] Run full test suite to ensure no regressions
- [ ] Test with both datetime formats:
  - `'2023-08-25 14:30'` (original TOS API)
  - `'2023-08-25T14:30:00'` (new TOS API ISO format)
- [ ] Validate site log generation still works correctly
- [ ] Test session filtering functionality

### Phase 4: Cleanup
- [ ] Remove now-unused imports (if any)
- [ ] Update any related documentation
- [ ] Remove this TODO file

## 🎯 Benefits After Migration

1. **Single source of truth**: All datetime parsing handled by gtimes
2. **Reduced code duplication**: Remove 3+ duplicate parsing implementations
3. **Better error handling**: gtimes provides descriptive error messages
4. **Future-proof**: New datetime formats added to gtimes automatically benefit tostools
5. **Consistency**: Same parsing behavior across all GPS/GNSS projects using gtimes

## 📚 References

- **gtimes PR**: https://github.com/bennigo/gtimes/pull/1
- **Original issue**: HildurMaria's PR #1 in tostools (now merged as commit a04cf55)
- **Related commit**: `a04cf55` - "Apply HildurMaria's fixes: datetime ISO parsing, sitelog .log extension, and contact information"

---

**Created**: 2025-09-03  
**Status**: Waiting for gtimes PR #1 to be merged  
**Priority**: Medium (technical debt cleanup)