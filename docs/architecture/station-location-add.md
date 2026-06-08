# Scope — `tos station add` + `tos location add`

Status: **in progress** (design agreed 2026-06-07: find-or-create location ·
station shell only · CLI verb + `--triage` handoff).

- **`tos location add`** + `TOSWriter.find_land_location_by_name` —
  implemented 2026-06-07. `src/tostools/location.py`,
  `_location_main`/`_location_add_main` in `tos.py`, pinned by
  `tests/test_location_add.py` (28 tests). Catalog `staðsetning`→`land` fix
  applied (§6.3). Two paths with **different verification status**:
    - ✅ **Reuse path — live-verified.** A site that already exists (commonly
      because a SIL station is there) is found and reused, child summary
      shown, never duplicated. Verified live against HEDI (site 4315, returns
      `reused=True`, lists the colocated GPS + SIL stations).
    - ⚠️ **Create path — dry-run validated, LIVE-UNVERIFIED.** All create
      tests are dry-run or `_FakeWriter`; `create_entity("land", …)` has never
      run against the API. A minted `land` entity is **irreversible** — TOS has
      no entity-delete endpoint (only `delete_entity_connection` /
      `delete_attribute_value`), so no throwaway test is possible. Same
      posture as the contact-entity writes: *verify on first genuine use.* The
      CLI prints this irreversibility caution on the create path. Residual
      risk is narrow: `create_entity` is a plain `POST /entities
      {entity_subtype, attributes}` that does **not** touch the
      `_subtype_to_entity_type_cache` (so the monument-114 precedent does not
      apply) — the only open question is whether `/entities` accepts a
      location-type subtype, which the first live create will settle.
- ⬜ **`tos station add`** — not yet started.

## 1. Problem

There is no operator command to bring a **new station online from scratch**.
`tos device add` mints devices (gnss_receiver, antenna, radome, monument) but
nothing creates the station entity itself or its parent site. Library
primitives exist (`TOSWriter.create_entity`, `create_entity_connection`,
`find_station_by_marker`, `find_location_by_name`) but are unexposed.

## 2. TOS data model (verified against live TOS, 2026-06-07)

The hierarchy is **two levels above devices**, not one. The
`tos-write-api.md` diagram showing the station as root is incomplete.

```
land  (entity_subtype="land",        entity_type=location, id_entity_type=1)   ← THE LOCATION / SITE
│      id 103 in /entity_subtypes/.  TOS desc: "Land-based location of one or
│      multiple colocated stations. REQUIRED PARENT of any land station in
│      regular operations."
│      attrs: name, lat, lon, altitude   (+ optional lon_isn93, lat_isn93,
│             identifier, notes).  Top-level — no parent of its own.
│
├── geophysical (entity_subtype="geophysical", entity_type=station, id 111)   ← THE STATION
│      attrs: marker, name, subtype(="GPS stöð"), lat, lon, altitude,
│             operational_class, date_start, continuity,
│             geological_characteristic, is_near_fault_zones,
│             bedrock_condition, bedrock_type, in_network_epos
│      └── devices: gnss_receiver, antenna, radome, monument (via joins)
│
└── (other colocated stations — e.g. a weather station — share the same land site)
```

Worked reference — **HEDI**: land site `id 4315` ("Héðinshöfði", subtype
`land`, lat/lon/alt only) → open join → geophysical station `id 4316`
("Héðinshöfði", marker `hedi`, the full GPS attribute set). The site also
carries a second child (5442) = a colocated instrument. This is the canonical
shape `tos station add` must reproduce: **a `geophysical` entity joined under a
`land` site.**

## 3. Decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| Q1 | Location handling | **Find-or-create in one verb.** `tos station add` resolves the site by name; mints the `land` entity if none matches, then attaches. `--location-id` forces an existing site; `--create-location` requires a fresh one (error if name already taken). Standalone `tos location add` also provided for site-first workflows. |
| Q2 | Station-add scope | **Station shell only** — `geophysical` entity + required attrs + join to the site. Devices added afterward via existing `tos device add` + `create-join`. |
| Q3 | Interface | **CLI verbs + `--triage` handoff** — dry-run default, `--triage PATH --placeholder TOKEN` substitutes the new id into a waiting triage file (reuses `_substitute_id_in_triage`). No new ACTION verbs. |

"Check what's in TOS" (the third ask) = the **pre-flight duplicate guard**,
modelled on `create_device`'s duplicate-serial guard: refuse + report the
existing entity, `--force` to override.

## 4. Verbs

### 4.1 `tos location add`

```
tos location add --name NAME --lat LAT --lon LON --altitude ALT
                 --date-start DATE
                 [--lon-isn93 V] [--lat-isn93 V] [--identifier V] [--notes V]
                 [--force] [--no-dry-run] [--json]
                 [--triage PATH --placeholder TOKEN]
                 [--server H] [--port N]
```

**Attribute `date_from` rule:** every minted attribute value (name, lat, lon,
altitude, optionals) gets `date_from = --date-start`, `date_to = null` (open).
When `tos station add` auto-mints a site, it passes the station's
`--date-start` through, so the site and station share one founding date.

Flow:
1. Validate `--lat/--lon/--altitude` numeric; `--name` non-empty;
   `--date-start` parses (`normalize_date_start`).
2. **Pre-flight:** `find_land_location_by_name(name)` (see §6 open item). On a
   hit → refuse, print existing `id_entity` + coords, exit 1 unless `--force`.
   (Stretch: warn when coords fall within N m of an existing site even if the
   name differs — v2.)
3. `writer.create_entity("land", attrs)` where attrs = name, lat, lon,
   altitude (+ provided optionals), each with `date_from=--date-start`.
4. Report new `id_entity`; `--triage` substitution + sed-hint, same as
   `tos device add`.

### 4.2 `tos station add`

```
tos station add --marker MARKER --name NAME --lat LAT --lon LON --altitude ALT
                --operational-class CLASS --date-start DATE
                --continuity VAL --geological-characteristic VAL
                --is-near-fault-zones {yes|no} --bedrock-condition VAL
                --bedrock-type VAL --in-network-epos {ja|nei}
                [--subtype 'GPS stöð']            # default; the station-kind attr
                [--location-id ID | --location-name NAME]   # default name = --name
                [--create-location]               # require a fresh site (error if name taken)
                [--force] [--no-dry-run] [--json]
                [--triage PATH --placeholder TOKEN]
                [--server H] [--port N]
```

Required-attribute set is taken from `data/attribute_codes.yaml` → `stations`
scope, `required_for == ['geophysical']`:
`marker, name, subtype, lat, lon, altitude, operational_class, date_start,
continuity, geological_characteristic, is_near_fault_zones, bedrock_condition,
bedrock_type, in_network_epos`. Each maps to a flag; a missing required value
with no catalog default → exit 2 listing what's absent. Enum-valued codes
(operational_class, continuity, in_network_epos, bedrock_*,
geological_characteristic, is_near_fault_zones) validated against catalog
`allowed_values` where present.

Flow:
1. Validate date (`normalize_date_start`), coords numeric, enums.
2. **Pre-flight — station:** `find_station_by_marker(marker)`. On a hit →
   refuse, print existing `id_entity` + name, exit 1 unless `--force`. Marker
   is the uniqueness key.
3. **Pre-flight — location (find-or-create):**
   - `--location-id ID` → fetch, assert `code_entity_subtype == "land"`, else
     exit 2. Use it.
   - else resolve by `--location-name` (default `--name`):
     - found → attach (print "using existing site id=X").
     - not found → **auto-mint** a `land` site from the station's
       name+coords (the Q1 default). Loudly logged in dry-run so the operator
       sees the implicit creation before `--no-dry-run`.
   - `--create-location` flips "found" into an error (refuse to reuse a name).
4. Create the site if minting (`create_entity("land", …)`).
5. `create_entity("geophysical", station_attrs)`.
6. `create_entity_connection(id_parent=site_id, id_child=station_id,
   time_from=date_start)`.
7. If station created but join fails → report the orphaned station id (mirrors
   device-add's failed-join handling), exit 1.
8. Report, `--json`, `--triage` substitution + sed-hint.

Ordering keeps the most-recoverable failure last: site → station → join.

## 5. Implementation map

| Piece | Location | Note |
|-------|----------|------|
| CLI `add` action on `tos station` | `tos.py` `_station_main` (router already has triage/verify/show) | add `add` subparser + dispatch |
| New `tos location` subcommand | `tos.py` top-level dispatch (alongside `station`/`contact`/`visit`) | brand-new router |
| Attr builders + validation | new `src/tostools/station.py` | mirror `device.py`: `build_required_station_attributes()`, `build_location_attributes()`, enum validators, `normalize_date_start` reuse |
| `find_land_location_by_name` | `api/tos_writer.py` | new, or generalize `find_location_by_name` (see §6) |
| Entity + join writes | existing `create_entity` / `create_entity_connection` | no writer change expected |
| Triage handoff | existing `_substitute_id_in_triage` + `--triage/--placeholder` | reuse verbatim |
| Catalog fix | `data/attribute_codes.yaml` `locations` scope | `required_for: ['staðsetning']` → `['land']` (stale key, confirmed §6.3) |

Tests mirror `tests/test_device_*.py`: `tests/test_station_add.py`,
`tests/test_location_add.py`. Pin: duplicate-marker refusal + `--force`
override; find / auto-mint / `--create-location`-conflict branches; join
wiring; dry-run writes nothing; required-attr validation; enum rejection;
triage substitution; orphaned-station-on-join-failure path.

## 6. Open items to resolve during build

1. ~~**`land` basic_search type filter.**~~ **RESOLVED 2026-06-07.** The `land`
   site cannot be filtered by `type_lvl_two` (it's `None`); in `basic_search`
   it surfaces as a top-level hit with `id_lvl_two=None` while stations carry
   `id_lvl_two`. `find_land_location_by_name` uses `id_lvl_two is None` as the
   pre-filter, then confirms `code_entity_subtype == "land"` via a history GET.
   Verified live against HEDI (site 4315).
2. **`create_entity` for `land`.** Verify `POST /entities {entity_subtype:
   "land", …}` is the right endpoint for a *location*-type entity (id_entity_type=1)
   and not a separate `/locations` route. The `create_device` path is only
   proven for device-type subtypes. Check `_subtype_to_entity_type_cache`
   resolves `land` → location correctly (the monument id=114 bug noted in
   tos_writer.py:253-255 is a cautionary precedent).
3. ~~**Catalog `locations` scope keys on `staðsetning`.**~~ **RESOLVED
   2026-06-07.** `staðsetning` is not a TOS subtype at all (the location
   subtypes are `land`/`stock`/`virtual`/`discontinued`/`ocean`/`vehicle`) — it
   was the Icelandic section name used as a placeholder, matched against nothing.
   All `[staðsetning]` tokens in the `locations` scope replaced with `[land]`
   in `data/attribute_codes.yaml`. Zero-risk (the scope was unconsumed; nothing
   matched the old token). The corrected scope now drives
   `LOCATION_REQUIRED_ATTR_CODES` in `location.py`.
4. **Enum allowed-values.** Confirm which station enum codes have
   `allowed_values` in the catalog vs. free-text, so validation rejects typos
   without blocking legitimate values.
5. **Exact-name reuse match → silent-duplicate risk (open).** The reuse guard
   keys on exact, case-sensitive `value_varchar == name` with no whitespace
   normalisation. A site stored as `"Héðinshöfði"` is missed by
   `"Hedinshofdi"` / a trailing space / different casing → a duplicate `land`
   site is minted (the exact outcome idempotency exists to prevent). v1
   mitigation: the create path prints a "check spelling first; this is
   irreversible" caution to stderr. Real fixes (v2): coordinate-proximity
   match, case/whitespace-insensitive compare, or a fuzzy "did you mean …?"
   suggestion from `basic_search` near-hits.

## 7. Onboarding boundary — where "online" stops

These verbs write **TOS entities only**. A station is not fully "online" for
the rest of the toolchain until it also appears in `stations.cfg` — the fleet
orchestrators (`tos fleet status/triage/verify`) enumerate via
`enumerate_fleet_stations`, which reads `stations.cfg`
(`code_subtype == "geophysical"`), not TOS. So a freshly-added TOS station is
invisible to fleet ops until a parallel `stations.cfg` entry lands.

`stations.cfg` is owned by the **`gps_parser` / `gps-config-data`** packages
(rendered from templates, deployed to `~/.config/gpsconfig/`), not tostools.
This scope deliberately does **not** touch it — but onboarding docs/tutorial
for `tos station add` must point operators at the `gps_parser deploy` step as
the second half of bringing a station online. Possible future convenience: a
`--emit-stations-cfg-stub` flag that prints the matching cfg block for the
operator to paste. Out of scope for v1.

## 8. Out of scope (deliberately)

- Initial device sessions in the same command (Q2 = shell only).
- `add-station` / `add-location` ACTION verbs for `tos audit apply` (Q3 = CLI
  only). Revisit if multi-station onboarding batches emerge.
- Coordinate-proximity duplicate detection (name-match only for v1).
- Non-`land` location subtypes (sea/ice/borehole, if they exist).
- **Shared power across colocated stations** — scoped separately in
  `shared-power-model.md` (decision 2026-06-08: power belongs on the `land`
  site, not per-station). `tos station add` will *call* that work's
  `summarize_site_power` read helper to show the operator a site's existing
  supply when attaching to an existing site, but does not write power itself.
