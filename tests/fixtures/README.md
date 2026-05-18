# TOS oracle fixtures — capture workflow

Foundation for the phase 1c byte-equality gate against
`gps_metadata_qc.gps_metadata`. See the design note
[[1778713245-tostools-devices-design]] §9 row 2 for the role this plays
in the migration plan.

## Layout

```
tests/
├── cassettes/test_composer_oracle/<test>.yaml   vcr cassette (HTTP-level)
├── _oracle_outputs/<MARKER>_legacy.json         canonical legacy snapshot
└── test_composer_oracle.py                      assertive test
```

The cassette captures every TOS HTTP exchange the legacy synthesis chain
makes for one station marker (1 search POST + 1 station-history GET +
N child-history GETs + 1 contacts GET). The snapshot is the
canonicalised legacy output that future composers must reproduce.

## Add a new station marker

```
# 1. Drop a new test into tests/test_composer_oracle.py:
#    def test_lmi_legacy_synthesis_matches_snapshot():
#        ...same shape as test_rhof...

# 2. Record the cassette (one network round-trip):
pytest tests/test_composer_oracle.py::test_lmi_legacy_synthesis_matches_snapshot \
    --record-mode=once

# 3. Capture the snapshot from the cassette (no network):
python scripts/capture_oracle.py LMI

# 4. Verify deterministic replay (no network):
pytest tests/test_composer_oracle.py --record-mode=none

# 5. Commit cassette + snapshot together.
```

## Refresh an existing fixture (TOS schema or data drift)

```
rm tests/cassettes/test_composer_oracle/test_rhof_legacy_synthesis_matches_snapshot.yaml
rm tests/_oracle_outputs/RHOF_legacy.json

pytest tests/test_composer_oracle.py::test_rhof_legacy_synthesis_matches_snapshot \
    --record-mode=once
python scripts/capture_oracle.py RHOF
pytest tests/test_composer_oracle.py --record-mode=none

# Review the diff carefully before committing — drift in the legacy
# output is exactly what byte-equality is designed to surface.
```

## Notes

- `Authorization`, `Cookie`, and `Set-Cookie` headers are filtered from
  cassettes (see `tests/conftest.py`). Don't disable this.
- POST body matching uses a JSON-aware matcher so cassettes survive
  future TOSClient refactors that reorder dict keys (see
  `_match_json_body` in conftest).
- Snapshots are produced with `sort_keys=True` for stable diffs and
  datetimes serialised as ISO strings; the test re-canonicalises the
  *fresh* result the same way before comparing.
