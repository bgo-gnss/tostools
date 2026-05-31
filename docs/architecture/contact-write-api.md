# Contact-write API — design / scope

**Status:** planned, blocked on endpoint discovery (Phase 0).
**Flagged:** 2026-05-31, during the vitjanir CLI expansion follow-up.

## Motivation

`tos contact` is read-only today (`show` / `list`). The contact↔station
relationship surfaced in `tos station show` carries a `per_time_from`
that, for at least some stations, is a **TOS-migration artifact** — the
wall-clock moment the relationship row was created in the new TOS, not
when the contact actually started owning/operating the station.

Worked example — HEDI (id_entity=4316), raw `entity_contacts/4316/`:

```json
{
  "id_contact": 1256,
  "id_entity": 4316,
  "id_contact_entity_relationship": 5018,
  "per_time_from": "2025-02-04T15:32:38",   // ← migration timestamp, not real
  "per_time_to": null,
  "contact_start_date": "1845-01-01T00:00:00", // org founding (separate field)
  "role": "owner",
  "role_is": "Eigandi stöðvar",
  "name": "Veðurstofa Íslands"
}
```

The `2025-02-04T15:32:38` is the same *flavor* of problem as the
fleet-wide `2014-10-17` attribute cleanup-artifact (see memory
`project_2014_10_17_metadata_cleanup_artifacts`): a bulk-load date
masquerading as a real event date. Operator confirmed (2026-05-31)
this should be **backdated** to the real ownership start (for
VÍ-owned sites, effectively station founding).

## Three distinct write targets

Each is a different TOS object, likely a different endpoint:

1. **Relationship period** — `per_time_from` / `per_time_to` on
   `id_contact_entity_relationship`. The "since/until" of a contact's
   tenure on a station. *This is the immediate need.*
2. **Relationship existence** — create / close a contact↔station
   mapping (assign a new owner, end an old one).
3. **Contact entity attributes** — name / phone / address /
   `start_date` on `id_contact` itself.

## Phase 0 — endpoint discovery (HARD BLOCKER)

The documented write endpoints (`docs/architecture/tos-write-api.md`)
cover entity-connections (`/joins`, `/join/{id}`), attribute_values
(`/attribute_value/{id}`), and vitjanir (`/maintenances/...`). **None
touch `id_contact_entity_relationship`.** The read side uses
`entity_contacts/{id}/` + `contact/{id_contact}/`; the write endpoints
are undiscovered.

By analogy with the join pattern (`/joins` POST, `/join/{id}` PATCH)
it's *probably* `PATCH /contact_entity_relationship/{id}` or an
`/admin_contact_..._row/{id}` form — but this MUST be verified, not
guessed, against live TOS.

**Discovery method (recommended):** edit a contact's date in the TOS
web UI with browser dev-tools open; capture the PATCH/POST URL +
payload from the Network tab. This is an operator task (authenticated
TOS web session). The postgres-tos-readonly MCP could confirm the
table/column shape but not the HTTP endpoint, and was unavailable at
scoping time.

Until Phase 0 lands, Phases 1-4 cannot be implemented.

## Phase 1 — writer methods

Mirror the existing `patch_entity_connection` pattern in
`src/tostools/api/tos_writer.py`:

```python
def patch_contact_relationship(self, id_rel, *, time_from=None, time_to=None):
    # PATCH <discovered endpoint>/{id_rel}; _tos_date-normalise dates
def create_contact_relationship(self, id_contact, id_entity, role, time_from, time_to=None):
    # POST <discovered endpoint>
def patch_contact_attribute(self, id_contact, field, value):
    # contact-entity field edits (name/phone/address/start_date)
```

Dry-run by default (every writer method respects `self.dry_run`).
Tests against mocked `_request` — no live TOS in unit tests.

## Phase 2 — CLI verbs

Extend the `tos contact` subcommand (currently `show` / `list`):

```
tos contact patch-relationship <id_rel> time_from <date> [--no-dry-run]
tos contact assign --station S --contact <id> --role owner --from <date>
```

Dry-run default + `--no-dry-run` to commit, matching `tos device add`
/ `tos visit add`.

## Phase 3 — ACTION verb (optional)

Triage-file form so contact corrections batch with metadata fixes in
one `tos audit apply`:

```
ACTION <id_rel> patch-contact-date time_from <date>
```

Note: the ACTION dispatcher keys on `id_entity` today; the contact
relationship is `id_contact_entity_relationship`, a different
namespace. The dispatcher would need to tolerate a relationship-id in
the id slot for this verb (or a distinct parse path). Minor but real.

## Phase 4 — audit (migration-artifact pattern)

Operator confirmed the `per_time_from` migration-date is wrong
fleet-wide. Parallel to `tos audit attribute-dates`:

```
tos audit contact-dates <STN>   # flag per_time_from == <migration date pattern>
```

First task here: characterise the artifact. Is every contact
relationship's `per_time_from` clustered around a single migration
date (like 2014-10-17 for attributes), or a date range? Probe
`entity_contacts/` across the fleet to find the signature, then the
audit flags + the triage emitter suggests backdating to the station's
`earliest_known` (the same anchor `start` resolves to elsewhere).

## Effort

| Phase | Work | Blocked on |
|---|---|---|
| 0 | Endpoint discovery (dev-tools capture) | **operator** |
| 1 | Writer methods + mock tests | Phase 0 |
| 2 | `tos contact patch-relationship` / `assign` | Phase 1 |
| 3 | `patch-contact-date` ACTION verb | Phase 2 |
| 4 | `tos audit contact-dates` + triage emitter | Phase 0 + fleet probe |

~1-2 days for Phases 1-3 once the endpoint is known.

## Cross-references

- `docs/architecture/tos-write-api.md` — the write-endpoint inventory
  this extends
- Memory `project_2014_10_17_metadata_cleanup_artifacts` — the
  analogous attribute migration-artifact pattern
- Memory `project_contact_write_support` — this scope, condensed
