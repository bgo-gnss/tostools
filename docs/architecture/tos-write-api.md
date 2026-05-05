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

## Future Work

- **Pattern 2 in `receivers cfg reconcile`** — `--push-tos` handles Pattern 1 only. Closing old periods and opening new ones for instrument changes is not yet implemented.
- **Pattern 4 with `date_hint`** — `upsert_attribute_value` needs a `date_hint` parameter to target a specific closed period rather than always operating on the most recent open value.
- **Device entity writes via reconcile** — `--push-tos` writes to the station entity only. Receiver and antenna attributes require separate child entity resolution.
- **Join record updates** — Device session start/end dates live in the join record. `patch_entity_connection` is implemented but not yet wired to any reconcile workflow.
