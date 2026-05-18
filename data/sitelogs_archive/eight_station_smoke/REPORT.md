# 8-station smoke comparison — legacy vs new synthesis (post phase 5)

Captured after phase 5 (new chain is `tosGPS` default) plus the
sitelog-renderer None-handling fixes. Verifies that the default
flip doesn't regress any of the eight selected stations across all
three user-facing subcommands.

Method:

```bash
for sta in VMEY DYNC RHOF HELC THOB SLEC GRVA SJUK; do
  for sub in PrintTOS sitelog; do
    diff -u <(tosGPS --use-legacy-synthesis $sub $sta) \
            <(tosGPS $sub $sta)
  done
  diff -u <(tosGPS --use-legacy-synthesis rinex $sta <RINEX_FILE>) \
          <(tosGPS rinex $sta <RINEX_FILE>)
done
```

RINEX files: `/mnt_data/rawgpsdata/2024/jun/<STA>/15s_24hr/rinex/<STA>1530.24D.Z`
(GRVA used `2020/jun/...` — no 2024 data). All `rinex` runs are
read-only — `--fix` was not passed.

## Results

| Station | PrintTOS diff | sitelog diff | rinex diff | Verdict |
|---|---|---|---|---|
| VMEY | 0 lines | 0 lines | 0 lines | identical |
| RHOF | 0 lines | 0 lines | 0 lines | identical |
| HELC | 0 lines | 0 lines | 0 lines | identical |
| THOB | 0 lines | 0 lines | 0 lines | identical |
| SLEC | 0 lines | 0 lines | 0 lines | identical |
| DYNC | 10 lines | 0 lines | 0 lines | **new chain improvement** (adds receiver-swap gap window) |
| SJUK | 12 lines | flaky (see below) | 0 lines | **new chain improvement** (legacy emitted two sessions both ending "Present"; new orders them and populates SN) |
| GRVA | 13 lines | 20 lines | 0 lines | **new chain improvement** (receiver SN + antenna SN populated where legacy showed None; adds historical session + receiver-swap gap window) |

**All `rinex` outputs identical across all 8 stations.** That's the
most operationally critical: receiver-validation behavior is
unchanged.

## Improvements caught by the new chain

### DYNC PrintTOS

Adds a 9-month receiver-swap gap window where legacy collapsed it
(same pattern as AKUR's 9-day swap gap):

```
+ 2018-09-06 2019-06-01 N/A           N/A             N/A  N/A TRM41249.00  NONE   60243759
```

### SJUK PrintTOS

Legacy emitted two sessions both ending "Present" (impossible —
only one can be currently open). The new chain orders them
correctly and populates the receiver / antenna SN that legacy left
as N/A:

```
- 2012-05-24 Present    TRIMBLE NETR9 N/A         NP 4.60 4.60 TRM57971.00  NONE   N/A
- 2014-10-17 Present    TRIMBLE NETR9 N/A         NP 4.60 4.60 TRM57971.00  NONE   N/A
+ 2012-01-01 2012-05-24 TRIMBLE NETR9 N/A         NP 4.60 4.60 TRM57971.00  NONE   N/A
+ 2012-05-24 2014-10-17 TRIMBLE NETR9 N/A           4.6.0 4.60 TRM57971.00  NONE   N/A
+ 2014-10-17 Present    TRIMBLE NETR9 5049K72257    4.6.0 4.60 TRM57971.00  NONE   532098
```

### GRVA PrintTOS

Adds an early session legacy hid, populates receiver SN +
antenna SN, and captures a 10-month 2024-2025 receiver-swap gap:

```
- 2014-10-17 2024-09-23 TRIMBLE NETRS N/A           NP 1.25 1.25 ... NONE   N/A
+ 2008-07-18 2014-10-17 TRIMBLE NETRS N/A           NP 1.25 1.25 ... NONE   N/A
+ 2014-10-17 2024-09-23 TRIMBLE NETRS 4731136285    NP 1.25 1.25 ... NONE   60240833
+ 2024-09-23 2025-07-25 N/A           N/A             N/A  N/A     ... NONE   60240833
+ 2025-07-25 Present    TRIMBLE NETRS 4539258376    1.3-2 1.32       ... NONE   60240833
```

### GRVA sitelog

Receiver SN and antenna SN populate where legacy emitted `None`:

```
3.1  Receiver Type            : TRIMBLE NETRS
-    Serial Number            : None
+    Serial Number            : 4731136285

4.1  Antenna Type             : TRM41249.00    NONE
-    Serial Number            : None
+    Serial Number            : 60240833
```

## Pre-existing non-determinism in legacy `site_log()`

SJUK sitelog output varies run-to-run regardless of the chain
selected — the legacy `gps_metadata_functions.site_log()` function
has a pre-existing race / iteration-order bug that surfaces as
different serial numbers on consecutive runs:

```
$ for i in 1 2 3; do
    legacy=$(tosGPS --use-legacy-synthesis sitelog SJUK | grep "Serial Number" | head -1)
    echo "run $i legacy='$legacy'"
  done
run 1 legacy='     Serial Number            : 5049K72257'
run 2 legacy='     Serial Number            : 5049K72257'
run 3 legacy='     Serial Number            : None'
```

By contrast, the new chain's JSON output is fully deterministic
across runs (MD5 stable when `generated_date` is filtered out).
This is a side-benefit of phase 5: the new synthesis path is
reproducible; the legacy isn't. Diffs against legacy that surface
as one-off serial-number changes are usually rolls of the legacy
dice, not new-chain regressions.

## Conclusion

Phase 5 default flip is safe across the eight tested stations. The
three stations where output changed (DYNC, SJUK, GRVA) all show
the new chain *fixing* legacy bugs — adding receiver-swap gap
windows that legacy collapsed, populating serial numbers legacy
emitted as `None`, ordering sessions correctly where legacy
emitted multiple sessions ending "Present". No regressions.

The `rinex` validator path is byte-identical on all eight stations,
which is the most operationally critical case (production header
checks).
