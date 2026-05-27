# TOS Write API — Technical Reference

TOS (Tæknileg Opin Stöðuvar) is the Icelandic Met Office's station metadata registry — a **temporal attribute store** where every piece of metadata is an entity with time-bounded attribute values.

## Data Model

### Entity Hierarchy

```
station (entity_subtype="geophysical")
├── attributes[]                ← lat, lon, altitude, marker, name, date_start, …
└── children_connections[]      ← device sessions
    ├── gnss_receiver  → attributes[]: model, serial, firmware_version
    ├── antenna        → attributes[]: model, serial, height
    └── radome         → attributes[]: model
```

Device entity attributes are fetched separately: `children_connections[].id_entity_child` gives the device's `id_entity`; call `GET /history/entity/{id}/` to get its attribute history.

### Attribute Value Fields

Each record in `attributes[]` from a history response:

| Field | Type | Notes |
|-------|------|-------|
| `id_attribute_value` | int | PK — required for PATCH |
| `id_entity` | int | Entity this value belongs to |
| `code` | str | e.g. `"marker"`, `"lat"`, `"model"`, `"serial"` |
| `value` | str | Always a string — numbers stored as strings |
| `date_from` | str | `"YYYY-MM-DDTHH:MM:SS"` — no timezone suffix |
| `date_to` | str or null | Same format, or `null` (open / currently active) |

## API Endpoints

Base URL: `https://vi-api.vedur.is/tos/v1`

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| POST | `/login` | Basic | Obtain JWT — response: `sid` (token), `ttl` (seconds to expiry), `profile.scope` |
| GET | `/history/entity/{id}/` | None | Full history with `attributes[]` and `children_connections[]` |
| POST | `/entity/search/{entity_type}/{domain}/` | None | Search by attribute code/value |
| POST | `/attribute_values` | JWT | Create a new attribute value |
| PATCH | `/attribute_value/{id_attribute_value}` | JWT | Edit existing value (value, date_from, date_to — any combination) |
| POST | `/joins` | JWT | Create parent→child entity connection |
| PATCH | `/join/{id_connection}` | JWT | Modify a join record (e.g. close a device session) |
| GET | `/entity/parent_history/{id_child}` | None | List all (open + closed) parent connections of a child entity |
| POST | `/basic_search/` | None | Fuzzy search by attribute code/value (returns `distance` per hit) |
| POST | `/maintenances/id_entity/{id_entity}` | JWT | Create vitjun stub; seeds blank attribute-value rows. Returns `{id, …}` but NOT the seeded value IDs |
| GET | `/maintenances/id_entity/{id_entity}` | None | List vitjun records for an entity (flat web-UI shape) |
| GET | `/maintenance/id_maintenance/{id}` | None | One vitjun's full detail incl. `maintenance_attribute_values[]` with each row's `id_maintenance_attribute_value` |
| PUT | `/maintenance/id_maintenance/{id}` | JWT | Fill in vitjun details: `participants`, dates, `completed`, list of `{id_maintenance_attribute_value, value}` |

## Python Client

`TOSWriter` (`tostools.api.tos_writer`) is the authenticated write client. `TOSClient` (`tostools.api.tos_client`) is the read-only companion.

```python
from tostools.api.tos_writer import TOSWriter

writer = TOSWriter(dry_run=True)   # default — logs but does not send mutating requests
writer = TOSWriter(dry_run=False)  # live writes
```

**Credential resolution order** (highest wins): constructor args → `TOS_USERNAME`/`TOS_PASSWORD` env vars → `[tos]` section in `database.cfg` → interactive prompt. For automated/non-TTY use, configure `[tos]` in `database.cfg`.

The JWT is kept in memory only. `TOSWriter` re-authenticates when the token is within 60 s of expiry or on HTTP 401.

**Key methods:**

- `get_entity_history(id_entity)` — GET `/history/entity/{id}/`
- `get_attribute_values(id_entity, code=None)` — convenience wrapper; filters by code if given
- `upsert_attribute_value(id_entity, code, value, date_from, date_to=None)` — PATCH open value if it exists and differs, POST otherwise
- `add_attribute_value(id_entity, code, value, date_from, date_to=None)` — POST, no existence check
- `patch_attribute_value(id_attribute_value, *, value, date_from, date_to)` — PATCH, only non-None fields sent
- `create_entity_connection(id_parent, id_child, time_from, time_to=None)` — POST `/joins`
- `patch_entity_connection(id_connection, **kwargs)` — PATCH `/join/{id}`
- `find_station_by_marker(marker)` — case-insensitive RINEX-marker → `id_entity` lookup; filters to `type_lvl_two="stöð"`
- `get_open_parent_join(id_child)` — GET `/entity/parent_history/{id}`, return the row with `time_to is None`
- `move_device(id_device, to_id_entity, transition_date, from_id_entity=None)` — **Pattern 2 for joins** (close open parent + open new), see below
- `list_maintenance_visits(id_entity)` / `get_maintenance_visit(id_maintenance)` — read vitjun
- `add_maintenance_visit(id_entity, *, start_time, end_time=None, maintenance_type, participants, reasons, work, comment, remaining, completed)` — 3-call POST + GET + PUT flow for creating vitjun

## Write Patterns

### Pattern 1 — Correct an existing value (same time period)

Use for typos, wrong antenna height, wrong firmware string — time period unchanged.

```python
# 1. Fetch: attrs = writer.get_attribute_values(id_entity, "firmware_version")
# 2. Find id_attribute_value, then:
writer.patch_attribute_value(id_av, value="4.14.0")
```

### Pattern 2 — Record a change (new instrument, firmware update)

Use when a physical change occurred: receiver replaced, antenna changed, firmware upgraded.

```python
# Close old period, open new one:
writer.patch_attribute_value(id_av, date_to="2026-03-01T00:00:00")
writer.add_attribute_value(id_entity, "firmware_version", "4.14.0", "2026-03-01T00:00:00")
```

### Pattern 3 — Add a brand-new attribute (never set before)

```python
writer.add_attribute_value(id_entity, "altitude", "112.5", "2019-06-01T00:00:00")
```

### Pattern 4 — Correct a historical value (closed period)

Same as Pattern 1 but targets a historical record. `upsert_attribute_value` operates on the most recent open value; for a closed period, use `get_attribute_values()` to locate the record by `date_from`, then call `patch_attribute_value()` directly.

### Pattern 2 for joins (device move)

Pattern 2 also applies to *joins* — closing the open parent connection and opening a new one is how devices change locations (warehouse → station, station → station, station → warehouse). Use `move_device()`:

```python
# Auto-detect from (B9 warehouse 4) → HRAC station 16096:
writer.move_device(id_device=21501, to_id_entity=16096,
                   transition_date="2026-05-22")

# Sanity-check the current parent (raises ValueError on mismatch):
writer.move_device(21501, 16096, "2026-05-22", from_id_entity=4)
```

The transition date sets `time_to` on the closed join and `time_from` on the new one (same string for both — TOS allows back-to-back joins with no gap). Bare dates (`"2026-05-22"`) are promoted to midnight by `_tos_date()`.

### Vitjun (maintenance / station visit)

A vitjun is created in three round-trips because TOS does not return the auto-seeded attribute-value IDs on POST:

```python
writer.add_maintenance_visit(
    id_entity=16096,                        # station HRAC
    start_time="2026-05-22",                # accepts YYYY-MM-DD or full ISO
    maintenance_type="on_site",             # or "remote" (Fjarvitjun)
    participants="bgo@vedur.is",            # comma-sep emails
    reasons=["change"],                     # multi-select; allowed:
                                            # change/repairs/inspection/improvements/other
    work="Skipt um móttakara",              # Framkvæmt
    comment=None,                           # Athugasemdir (None → leave default)
    remaining=None,                         # Útistandandi
    completed=True,
)
```

Internally: POST `/maintenances/id_entity/{id_entity}` → GET `/maintenance/id_maintenance/{new_id}` to discover seeded `id_maintenance_attribute_value` per code (`reason_change`, `work`, etc.) → PUT `/maintenance/id_maintenance/{new_id}` with the value list.

Reason fields are stored as **booleans** ("true"/"false") — multiple can be true on one vitjun. Text fields not passed (`None`) are left at the seeded default (empty string).

In `dry_run=True` mode the POST short-circuits and returns `id_maintenance="<dry-run>"` — the GET + PUT roundtrip cannot be simulated without a real ID.

## IGS Equipment Name Convention

TOS stores equipment names in IGS rcvr_ant.tab format: `"SEPT POLARX5"`, `"TRIMBLE NETR9"`, `"LEICA GR10"`, `"SEPPOLANT X_MF"`, `"NONE"` (for no radome). The `receivers` health system reports abbreviated names (`"PolaRx5"`, `"NetR9"`). Convert before writing:

```python
from tostools.standards.igs_equipment import to_igs_receiver, to_igs_antenna, to_igs_radome

to_igs_receiver("PolaRx5")  # → "SEPT POLARX5"
to_igs_radome(None)         # → "NONE"
```

## Critical Gotchas

1. **Date format** — TOS accepts `"YYYY-MM-DDTHH:MM:SS"` only. Timezone suffixes (`+00:00`, `Z`) are rejected. Use `TOSWriter._tos_date()` to strip them before sending.
2. **JWT token field** — Login response uses `"sid"` (not `"token"`). Expiry is `"ttl"` (seconds from now). Scope is inside `profile.scope`.
3. **`id_attribute_value` for PATCH** — Use this exact field name from the history response — not `"id"`.
4. **401 retry** — `_request` retries once on 401 (re-login). This adds latency; avoid triggering it from read-only paths.
5. **dry_run default is True** — Confirm payloads via dry-run before setting `dry_run=False`.
6. **Device entity writes** — Receiver model/serial/firmware live on the gnss_receiver child entity. Resolve via `children_connections[].id_entity_child`, then fetch its history separately.

## Non-Public Endpoints

### `PUT /admin_entity_row/<id_entity>`

Used by :meth:`TOSWriter.update_entity_subtype` — the only write method
hitting a non-public endpoint. The public ``/entity/<id>`` is read-only
(``Allow: HEAD, GET, OPTIONS``). Requires admin-level TOS access. Prefer
the attribute and join verbs for routine metadata writes.

## `tos audit apply` ACTION verbs

Operator-facing surface for the patterns above. Each ACTION line in a triage file is `ACTION <id_entity> <verb> [args...]`; placeholders (`<FILL_*>`) are rejected at dispatch time; `--apply` flips dry-run off.

| Verb | Args | Pattern | Use |
|------|------|---------|-----|
| `defer` | — | — | No-op, mark for later |
| `change-subtype` | `<subtype_code>` | — | Reclassify entity (`PUT /admin_entity_row/<id>`) |
| `decommission` | `<date>` | 2 (joins) | Close current open join + transition `status` |
| `move` | `<to_parent_id> <date>` | 2 (joins) | Close open join + open new — relocate device |
| `fill-gap` | `<parent_id> <date_from> <date_to>` | — | Create a closed historical join |
| `patch-join-date` | `<id_connection> <field> <new_date>` | — | PATCH `time_from` or `time_to` on an existing join (extend a join's start back, correct a close-out date). `field ∈ {time_from, time_to}` — reparent attempts refused; use `move` for that |
| `add-attribute` | `<code> <value> <date_from>` | 3 | POST new attribute period; refuses if a different open period exists |
| `patch-attribute-date` | `<code> <old_date_from> <new_date_from>` | — | Re-date an existing attribute period in place |
| `patch-attribute-value` | `<code> <date_from_match> <new_value>` | 1, 4 | Correct a wrong value in place (e.g. `serial="UNKNOWN"` → `"3163"`). Same date-prefix lookup as `patch-attribute-date`; idempotent on already-correct values |

## Future Work

- **Triage generators for cross-source diffs** — `tos audit missing-attributes` emits triage files today. A similar `tos audit fix <STATION>` (or `audit reference-diff`) that emits `patch-attribute-value` + `patch-join-date` actions by comparing TOS to GAMIT station.info / IGS site-logs would close the loop on the HEDI-style data-quality task class. Today operators hand-write the triage file from `tosGPS syncMeta` output.
- **Join record updates** — Device session start/end dates live in the join record. `patch_entity_connection` is wired through `move_device()` (close+open) and the `patch-join-date` apply verb (single-field PATCH). Bulk reconcile workflows are still open.
- **CLI verbs** — The `tos` CLI today only exposes read verbs. `move_device` and `add_maintenance_visit` are reached from the `receivers cfg install-device` / `receivers cfg visit` workflow (see `receivers/CLAUDE.md`). A standalone `tos device move` / `tos visit add` is open work.
