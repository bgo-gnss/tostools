# Network-data extraction — design note

## Context

Both `tostools/data/` and `receivers/config/` currently mingle two
categories of files that have different lifecycles, different
audiences, and different reasons to exist. This document proposes
extracting the network-specific category into a separate
`gps-network-data` repo so the two code repos become portable to
other GNSS networks.

**Status:** planning. Trigger criteria not yet met (see below).
No code change is proposed today; this note exists so the intent
survives between sessions and the trigger conditions are explicit.

## Two categories of data, today co-located

### Category 1 — network-specific operational data (IMO-only)

State that is meaningful only to the Icelandic GNSS network operated
by Veðurstofa Íslands. Has its own lifecycle (daily / weekly churn,
append-only history, paper-trail value), independent of code releases.

Currently in `tostools/data/`:

| Path | What it is |
|------|-----------|
| `triage/<STN>/<STN>_audit_<DATE>.txt` | Per-station triage files. Canonical retrospective-write audit trail (TOS has no `created_at` on attribute_value rows — committed triage files in git ARE the lineage; see *TOS retrospective writes provenance gap* in CLAUDE.md). |
| `sitelogs_archive/` | Snapshots of IGS sitelog history per station. |
| `station_config/stations.list` | Active station inventory. |
| `station_config/LMI_stat.info`, `station_config/station.info.sopac.apr05` | GAMIT station.info compatibles. |
| `station_config/antenna.gra`, `antenna_arp.list`, `antenna_arp_tmp.list` | Antenna offset reference. |
| `station_config/fiho_domes_informations.txt`, `iers_sta_list.txt` | IERS / DOMES registry views. |
| `station_config/visit.list`, `station-plate*` | Operational lookup files. |
| `station_config/backups/`, `work/`, `cache/` | Operational scratch + backup state. |
| `audit_suppressions/attribute_dates.txt` | Site-specific SUPPRESS lines for the audit pipeline. |
| `reference/sitelog_instr.txt` | Network's local sitelog generation instructions. |

Currently in `receivers/config/` (sibling repo; not in this tree):

| Path | What it is |
|------|-----------|
| `station_areas.yaml` | Network-area→station mapping for monitoring / dashboards. |

### Category 2 — code-bundled reference data (portable)

Schema / standards / format spec that travels with the code regardless
of which network deploys it. Releases on the code's semver clock.

Currently in `tostools/data/`:

| Path | What it is |
|------|-----------|
| `data/attribute_codes.yaml` | TOS attribute-code catalog. Schema, not state. |
| `data/rinex_samples/` | Fixture RINEX files for tests. |
| `data/test_outputs/` | Fixture comparison data for tests. |
| `data/sitelogs_archive/eight_station_smoke/`, `new_synthesis/` | Test-bench output sets (technically Category 1.5 — IMO-shaped fixtures, but they exist to gate code refactors, not as operational records). Should stay co-located with the tests they pin. |

`receivers/config/defaults/` (sibling) — default-config templates that
ship with the code; rendered into Category 1 per-deployment.

## Why split

1. **Portability** — code repos become deployable against any GNSS
   network with a TOS-shaped backend. A second network could fork
   only `gps-network-data` and reuse the code unchanged.
2. **Paper trail clarity** — today the triage files are the audit
   trail for retrospective TOS writes. An isolated repo makes that
   lineage explicit; no one will accidentally bundle them with a
   code release tag.
3. **CI hygiene** — code repo CI doesn't churn on operational data
   updates. Daily triage files don't touch `setup.py`.
4. **Lifecycle decoupling** — semver releases on the code vs. daily
   operational state evolve at different paces. Sharing a repo
   forces operators to choose between "tag a code release" and
   "commit today's triage file" semantics on every push.
5. **Provenance** — Category 1 wants append-only, signed history;
   Category 2 wants normal develop-rebase-PR flow. Different defaults
   for the same git history is awkward.

## Why NOT split (today)

* **Volume is low.** Triage file count is ~5 today (HEDI, SAVI,
  HOFN, plus a handful of one-offs). `data/` is <1 MB total. The
  current friction-cost of mingling is near zero.
* **Single network deployer.** Only IMO uses this stack today. The
  portability benefit is theoretical until a second network shows up.
* **Submodule friction is real.** `git submodule` adds clone steps,
  CI matrix complications, and the perennial "I forgot to commit
  the submodule pointer" bug class. Worth incurring only when the
  benefit is concrete.
* **Tests depend on layout.** Several test fixtures live under
  `data/` (RINEX samples, sitelog snapshots used by the
  golden-output gate). A split would need to either keep those in
  the code repo (Category 2 / 1.5) or stub them out for CI.

## Trigger criteria

Execute the split when **any** of:

1. **Volume threshold** — triage file count crosses ~50 across the
   fleet. At that point grep / git log noise from operational data
   starts to dominate the code repo's own signal.
2. **Second network** — any non-IMO deployer commits to using this
   stack. The portability win becomes concrete; the friction
   becomes worth paying.
3. **Code-history pollution** — operational-data commits start
   making code-only `git log` queries unwieldy (subjective, but
   "more than half of recent commits touch only `data/`" is a
   reasonable threshold).
4. **Compliance ask** — a stakeholder (legal / IT) asks for the
   audit trail to live in a clearly separate system.

## Implementation sketch (when triggered)

1. **Create the repo.** `gps-network-data` (or `imo-gps-network`
   if other networks may want their own) at the same git host
   as tostools / receivers.
2. **Extract preserving history.**
   ```bash
   git subtree split --prefix=data/triage -b extracted-triage
   git subtree split --prefix=data/sitelogs_archive -b extracted-sitelogs
   git subtree split --prefix=data/station_config -b extracted-station-config
   git subtree split --prefix=data/audit_suppressions -b extracted-suppressions
   git subtree split --prefix=data/reference -b extracted-reference
   ```
   In the new repo, pull each extracted branch under a top-level
   subdir: `triage/`, `sitelogs_archive/`, `station_config/`,
   `audit_suppressions/`, `reference/`. Receivers-side extracts
   `config/station_areas.yaml` similarly.
3. **Decide on consumption mechanism.** Two viable approaches:
   * **`git submodule`** — explicit, every deployment ties to a
     pinned data-repo SHA. Friction: requires `git submodule update`
     after clone. Best for paper-trail use cases where the pinned
     SHA matters.
   * **`git subtree` pull-on-deploy** — looser; deployers can pull
     latest data without a pointer-update commit. Friction lower
     but provenance fuzzier.
   * **External path + env var** — `$TOSTOOLS_NETWORK_DATA` points
     at a clone the operator manages independently. Zero
     submodule friction; code just reads paths under that root.
     Today this is the closest match to actual ops (operators
     already `cd` to their working clone).
   The recommended starting point is **env var + path override**,
   defaulting to the in-tree location for backwards compatibility
   during a transition period.
4. **Update code references.** Anywhere code reads
   `data/<category-1-path>/...`, route through a configurable
   resolver:
   ```python
   def network_data_root() -> Path:
       return Path(
           os.environ.get("TOSTOOLS_NETWORK_DATA")
           or _find_in_tree_data()
       )
   ```
   Mirrors the existing pattern used for `cold_archive_prepath()`
   in `archive.py` (env var → cfg file → mount probe → error).
5. **Update CLAUDE.md.** Both tostools and receivers. Add a
   "Network data location" section pointing at the env var + the
   sibling repo.
6. **CI matrix.** Code repo CI ignores network-data entirely;
   network-data repo CI runs `tos fleet status --json` against
   a frozen tostools tag to validate metadata health on every
   data-repo push.
7. **Migrate gradually.** Keep the in-tree paths working via
   fallback resolution for one release; remove the fallback in
   the release after that. The cutover should be invisible to
   operators who don't change their working directory layout.

## Open questions

* **Where do the test fixtures live?** `data/rinex_samples/`,
  `data/test_outputs/`, the byte-equality oracle fixtures — these
  are Category 2 (they gate code refactors) but they're IMO-shaped
  (real RHOF / SAVI / HOFN data). Decision: keep in the code repo
  as `tests/fixtures/` (move them out of `data/` to make the
  category boundary obvious in the path).
* **Receivers-side extraction.** The receivers package has a richer
  `config/` directory than tostools. This design doc covers the
  tostools side; a parallel design note in the receivers repo
  should mirror it before either is executed.
* **Submodule vs path-override choice.** Worth a small RFC at
  trigger time. Today's lean is path-override (matches existing
  archive-root pattern, lowest friction); revisit if compliance
  pushes for pinned-SHA provenance.

## Companion notes

* CLAUDE.md *TOS retrospective writes provenance gap* — explains
  why the triage files are operationally load-bearing.
* `project_station_triage_orchestrator` memory — narrower version
  of this idea (just `data/triage/` extraction) recorded earlier.
* `project_network_data_extraction` memory — operator-side
  paragraph + trigger criteria, also point back here.

---

*Filed 2026-05-28 as a planning artifact. No code change accompanies
this document; the split is gated on the trigger criteria above.*
