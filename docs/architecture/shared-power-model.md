# Scope — shared-power model for colocated stations

Status: **scoping** (design agreed 2026-06-08: power on the `land` site ·
surface + audit now · kept separate from `tos station add`). Not implemented.

Companion to `station-location-add.md` — that doc adds the `land` site + the
station-under-site join; this one decides how **power** is represented across
the stations that share a site.

## 1. Problem

Power is physically a **site-level** resource — colocated stations at one site
draw from one supply — but TOS models it **per-station**, so each station
carries its own battery / solar / power-pack devices. Two stations at the same
site therefore look like two independent power systems when they are one.

Operator-confirmed example: **HEDI**'s GPS (`geophysical` 4316) and SIL
(`geophysical` 5442) both sit on `land` site 4315 and are *both fed from the
nearby farm's mains* — one supply, modelled today as… nothing (HEDI has no
power devices at all on either station or the site). The model can neither
express "shared" nor even "present."

## 2. Verified TOS facts (2026-06-08, read-only probes)

- **`land` is the only site-level anchor.** Top-level (HEDI 4315 has no parent —
  `parent_history` 404), hosts colocated stations as children. Carries
  `name`/`lat`/`lon`/`altitude` only — **no power children today.**
- **Deployed power attaches to a station.** Sampling active
  `battery` / `solar_panel` / `power_pack` devices, open parents were
  `geophysical` (35), `meteorological` (16), `hydrological` (2) — i.e. the
  station entity, never the `land` site.
- **`area` is the warehouse, not a site grouping.** The `area`-parented power
  devices (19 in the sample) hang under `B9 - Kjallari - Jörð` (id 4) and
  `B9 - Kjallari - Veður` (id 3), both `entity_type=warehouse` — these are
  **stored/spare** units in B9, unrelated to deployment sites. (Corrects an
  earlier guess that `area` was a shared-infrastructure anchor.)
- **Power is a rich device family** (`GET /entity_subtypes/`, entity_type=device):
  `battery`, `solar_panel`, `power_pack`, `charge_regulator`, `charger`,
  `solar_charge_controller`, `ups`, `power_generator`, `switch_poe`,
  `anemometer_power_pack`.

**Conclusion:** there is no existing site-level power model to reuse —
attaching power to `land` (the Q1a decision) is a new pattern, and aligning the
fleet means reparenting the ~35+ deployed power devices from station → site.

## 3. Decisions (locked 2026-06-08)

| # | Decision | Choice |
|---|----------|--------|
| Q1 | Where shared power lives | **(a) On the `land` site.** Power devices attach to the site; colocated stations inherit one supply. New pattern (no `area`/warehouse reuse). |
| Q2 | What the tooling does | **(a) Surface** existing site power (read-only awareness) **+ (b) Audit** invariant that colocated stations share power. |
| Q3 | Scope boundary | **Separate** from `tos station add` — that verb stays station-shell + site-join; shared-power is this independent piece (it drags in a fleet-wide migration). |

Open sub-decision deferred to design (see §6): how to represent the power
**source** (the "farm" mains vs off-grid solar+battery) — a `land` attribute,
a dedicated source entity, or implicit in which power devices hang off the site.

## 4. Workstreams

Ordered cheapest-/least-risky first. Only W1 is near-term; W3 is the heavy one.

### W1 — Surface existing site power (Q2a) — ✅ IMPLEMENTED 2026-06-08

Extend the reuse view already built in `tos location add` (and the future
`tos station add`) so that when a site is found/reused, the operator sees the
power already present at the site, **aggregated across colocated stations**
(since today it lives on the stations, not the site):

```
Location 'Héðinshöfði' already exists (id_entity=4315) — reusing.
  Currently attached:
    - GPS stöð   id=4316  …
    - SIL stöð   id=5442  …
  Site power (aggregated across colocated stations):
    - battery      id=…  on SIL stöð (5442)   ← shared supply: don't add a duplicate
    - solar_panel  id=…  on SIL stöð (5442)
  (none on the site itself yet — see shared-power-model.md)
```

Implemented as `tostools.power.summarize_site_power(writer, land_id)` — walks
the `land` children + each colocated station's children, filters to
`POWER_DEVICE_SUBTYPES`, returns `{id_entity, subtype, model, serial,
on_station, on_station_name, sensor_tied, time_from, open}` (site-direct power
sorts first, then by station). Also handles power joined **directly** to the
site (W3 target state) so it keeps working as power moves site-ward. Wired into
the `tos location add` reuse view via `_print_site_power`; live-verified on
Mjóaskarð (`land` 4360 — 9 power devices across the GPS + SIL + Endurvarpi
stations surfaced with per-station attribution and a "don't duplicate"
prompt). Pinned by `tests/test_power.py` (8) + `tests/test_location_add.py`
site-power cases. `SENSOR_TIED_POWER_SUBTYPES` (`anemometer_power_pack`) is
flagged in each row so the W2 audit can exclude instrument-private power.

### W2 — Audit `tos audit shared-power` (Q2b) — SCOPED 2026-06-09

> **W2 and W3 are one piece.** The audit's `--triage` output *is* the migration
> tool — it emits the `move <power_id> <site_id> <date>` ACTIONs that reparent
> power station→site. There is no separate "W3 migration" step; running the
> triage performs it. The old W3 section below is folded in here.

#### Discriminating findings (read-only fleet probe, 2026-06-09)

These numbers reshaped the invariant — the naive checks the original stub
proposed are mostly false positives.

- **110 distinct `land` sites already carry ≥1 power device.** Power is
  *broadly* modelled per-station. The total land-site count is **not
  established** (both enumeration probes returned 0 — no working
  list-all-land endpoint found yet), and weather/hydro sites are in the
  universe, so MISSING **prevalence is unknown** — could be a minority gap or
  common. The audit itself will reveal it; do not pre-judge. Either way MISSING
  at a *true colocation* (below) is a meaningful finding.
- **Of 11 sites with power on ≥2 colocated stations, only 1 is a repeater.**
  The rest exposed that **a `land` site is overloaded**: sometimes a true
  single-point colocation (HEDI, Mjóaskarð GPS+SIL), sometimes a **regional
  administrative grouping** spanning kilometres — `Reykjanes` (site 5495)
  groups **7** stations (Herdísarvík … Litla-Skógfell, tens of km apart);
  `Askja`, `Geldingadalir`, `Gígjukvísl` likewise. At a grouping site,
  "colocated stations share power" is simply **false**.

**Consequence:** the core invariant cannot key on "stations share a `land`
site." It needs a **coordinate-proximity gate** — only stations physically
close (all within a small radius of each other / the site coordinate) are a
genuine colocation that should share power. This gate is the linchpin of the
whole audit; without it ~90 % of "duplication" findings are wrong.

#### The proximity gate — VERIFIED + calibrated (2026-06-09)

Every station carries `lat`/`lon`; the `land` site carries one `lat`/`lon`.
Classify each multi-station site by the **max pairwise distance** among its
colocated stations:
  * **colocation** — all stations within `--proximity-m` of each other. Power
    *is* expected to be shared here.
  * **grouping** — stations spread beyond the threshold. Skip entirely (not a
    shared-power site; it's an admin grouping). Surface in a separate
    "skipped — regional grouping" list so the operator sees what was excluded
    (no silent truncation).

**The gate was the design's linchpin, so it was verified against the 11
multi-station sites before this scope shipped** — and the worry that station
coords might just be copied from the site (HEDI showed station==site coord)
was *refuted*: coords are independently measured and spread cleanly.

| Site | kind | max pairwise child distance |
|------|------|------------------------------|
| Eldey | true colocation | **2 m** |
| Mjóaskarð | colocation + repeater | GPS+SIL **7 m**; repeater **1,106 m** offset |
| Hekla | grouping | 6.7 km |
| Askja | grouping | 21 km |
| Reykjanes | grouping (7+ stns) | **52 km** |

The separation is huge — true colocations are **≤7 m**, the next thing up is
**>1 km** — so any threshold in ~100 m–1 km works; **default 150 m** is safe.
Two consequences this resolved: (a) HEDI's station==site coordinate was a
genuine colocation, not coordinate-copying — the gate is real; (b) the
**repeater is auto-separated by proximity** (1.1 km > threshold), so the
repeater/aux false-positive needs **no name-regex** — proximity handles it.

Distance via a haversine over the `lat`/`lon` decimal degrees (reuse `geofunc`
if a primitive exists; otherwise a small local haversine — same coord shape as
`location.validate_*`).

#### Findings the audit emits (per colocation site only)

  * **A — MISSING power.** A colocation with ≥1 station and **zero** power
    devices anywhere (HEDI). Split by likely supply:
      - *off-grid-suspected* → triage hint to add the real battery/solar via
        existing `tos device add` (operator knows what's installed).
      - *mains-suspected* → there may be **no device to add** (grid power). The
        fix is the `power_source` attribute (see §6.1) →
        `#ACTION <site> add-attribute power_source mains <date>`.
    The audit can't tell off-grid from mains automatically — emit both hints
    commented, operator picks. (HEDI = mains/farm, the flagship case.)
  * **B — UNCONSOLIDATED power** (the migration worklist). Power sits on a
    station at a genuine colocation. Emit a reparent ACTION per device:
    `#ACTION <power_id> move <site_id> <date>` (the existing `move` verb —
    `_dispatch_move` / `move_device`). **Excludes** `SENSOR_TIED_POWER_SUBTYPES`
    (`anemometer_power_pack`) — those stay on their instrument. Running the
    triage *is* the W3 migration.
  * **(dropped) naive duplication.** "≥2 stations each with power" is **not**
    emitted as a standalone violation — it collapses into B (consolidate to the
    site) once proximity-gated, and the repeater/aux case is operator-judgement
    (a repeater on its own panel may legitimately stay — flag, don't prescribe).

#### Shape + integration

Module `audit_shared_power.py`, mirroring `audit_visit_coverage.py`:
frozen `SharedPowerViolation` / `SharedPowerReport` dataclasses, a
`load_shared_power_suppressions()` reader for
`data/audit_suppressions/shared_power.txt` (key `SUPPRESS <site_id>
<device_id>`), `audit_station_shared_power(client, station, *, proximity_m=…,
…)`, and `format_triage_file(report)`. Reuses `power.summarize_site_power`
(already aggregates power across colocated stations + the site-direct W3 target
state) and `power.SENSOR_TIED_POWER_SUBTYPES`.

**Standalone first — no verify-oracle / fleet plumbing yet.** Unlike
`--with-coverage` (where each finding can be suppressed *to reach* clean), the
UNCONSOLIDATED worklist is dirty fleet-wide *by definition* until the migration
runs — an oracle check that can never pass pre-migration isn't an invariant,
it's a worklist. Ship `tos audit shared-power <STN>` + `--triage` standalone;
add `--with-power` to the verify oracle / `tos fleet status` **only after** the
migration establishes a baseline (then MISSING-at-a-colocation becomes a real
recurring invariant). `--proximity-m`, `--no-suppressions`, `--triage PATH`
are the v1 flags.

## 5. Boundary with `tos station add`

`tos station add` (other doc) is unchanged: it creates the `geophysical`
station and joins it under the site. It will **call W1's `summarize_site_power`**
to show the operator the shared supply when attaching to an existing site, but
it does **not** write power. Power onboarding (attach a new battery/solar to a
site) is W3, via `tos device add` + a site-parent join — not a `station add`
responsibility.

## 6. Open questions

1. **Power-source representation — minimal answer adopted.** "Powered from the
   farm" (mains) vs off-grid (solar+battery) is real and W2's MISSING finding
   can't prescribe a fix without it. **Decision: a `power_source` attribute on
   the `land` site** (`mains | solar_battery | generator | …`) — the lightest
   representation that turns a mains-MISSING site (HEDI) from an un-actionable
   flag into `add-attribute <site> power_source mains`. A dedicated
   power-source *entity* (the farm as an external supplier) is heavier and
   deferred — revisit only if mains suppliers need their own metadata. The
   `power_source` code must be added to `attribute_codes.yaml` (`locations`
   scope) as part of W2.
2. **The `land` overloading (NEW, from the 2026-06-09 probe).** A `land` site
   is sometimes a true colocation, sometimes a regional admin grouping
   (`Reykjanes` = 7 stations over tens of km). The proximity gate handles it
   *for this audit*, but it's a latent data-modelling smell: arguably the
   groupings should be a different entity layer than single-point sites.
   Out of scope here; flag for a future TOS-model review.
3. ~~**Proximity threshold value.**~~ **RESOLVED 2026-06-09** — calibrated
   against the 11 multi-station sites: true colocations ≤7 m, next-up >1 km, so
   **default 150 m** (kept tunable via `--proximity-m`). See the gate table.
4. ~~**Repeater / aux exclusion.**~~ **RESOLVED 2026-06-09** — the proximity
   gate handles it: Mjóaskarð's repeater is 1.1 km offset, so it falls outside
   the colocation radius automatically. No name-regex needed. Genuine
   on-radius consolidation stays operator judgement (commented ACTIONs).
5. **Migration completeness — all vs shared-only** (a station-private UPS in a
   hut may stay per-station). Operator decides per reparent ACTION (they're
   commented by default).
6. **Meteorological / hydrological colocation.** Sites host weather/hydro
   stations too (they carry the bulk of power devices). Site-level power almost
   certainly spans *all* domains at a true colocation — `summarize_site_power`
   already aggregates across domains; confirm the audit shouldn't GPS-restrict.
7. **`anemometer_power_pack` and sensor-specific power.** Tied to one
   instrument, not site-shared — already flagged via
   `SENSOR_TIED_POWER_SUBTYPES`; the reparent worklist excludes it.
