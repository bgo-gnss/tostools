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

### W2 — Audit invariant: colocated stations must share power (Q2b)

`tos audit shared-power <STN>` (and a fleet sweep) flags a site where
colocated stations have:
  * **separate/duplicate** power systems (each station its own battery+solar —
    the duplication the requirement forbids), or
  * **missing** power (the HEDI "neither models any power" case), or
  * power that should be site-level still sitting on a station (drift signal
    feeding the W3 migration).

Off-by-default / `--with-power` opt-in at first (mirrors `--with-coverage`),
because until the W3 migration runs every site will look "wrong." SUPPRESS
file `data/audit_suppressions/shared_power.txt`. `--triage` emits the
reparent ACTIONs (see W3).

### W3 — Site-power model + migration (Q1a) — fleet-wide, heavy

Make `land` the parent of shared power:
  * **Write path:** attach a power device to the `land` site via the existing
    `create_entity_connection(parent=land, child=power_device)` — no new writer
    primitive needed. New `move`-style ACTION reparents an existing
    station-parented power device to the site (`move_device(power_id,
    to_id_entity=land_id, …)` — already exists).
  * **Migration:** reparent the ~35+ deployed power devices station → site.
    Phased, per-site, through committed triage files (the canonical
    retrospective-write trail). Per-site: confirm which station(s) the power
    actually served, move to the `land` parent, leave a vitjun if a physical
    visit was involved.
  * **Decision point:** does *every* power device move to the site, or only the
    genuinely-shared ones (a station-private UPS in a hut might legitimately
    stay per-station)? Resolve in the W3 design, not here.

## 5. Boundary with `tos station add`

`tos station add` (other doc) is unchanged: it creates the `geophysical`
station and joins it under the site. It will **call W1's `summarize_site_power`**
to show the operator the shared supply when attaching to an existing site, but
it does **not** write power. Power onboarding (attach a new battery/solar to a
site) is W3, via `tos device add` + a site-parent join — not a `station add`
responsibility.

## 6. Open questions

1. **Power-source representation.** "Powered from the farm" (mains) vs off-grid
   (solar+battery) is a real distinction. Options: a `land` attribute
   (`power_source: mains|solar_battery|generator|…`), a dedicated power-source
   entity the site references, or purely implicit in the attached power
   devices. The farm itself may warrant being an entity (external mains
   supplier). Decide before W3.
2. **Migration completeness — all vs shared-only** (see W3 decision point).
3. **Meteorological / hydrological colocation.** The requirement was framed for
   GPS+SIL, but sites host weather/hydro stations too (and they carry the bulk
   of power devices). Does site-level power span *all* domains at a site, or
   just the geophysical ones? Almost certainly all — confirm.
4. **`anemometer_power_pack` and sensor-specific power.** Some power devices are
   tied to one instrument (an anemometer's own pack). Those are genuinely
   per-device, not site-shared — the audit/migration must not flag them.
