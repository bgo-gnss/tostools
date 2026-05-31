# Contact-write API — design / scope

**Status:** relationship CRUD implemented (dry-run validated; **no live
write executed yet** — see verification note under Phase 0). Phases 4-5
(audit + contact-entity writes) not built.
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

## Phase 0 — endpoint discovery (RESOLVED 2026-05-31)

Discovered by **read-only probing** (GET + OPTIONS only — no mutating
requests against live TOS). The `admin_*_row` family mirrors
`admin_entity_connection_row` / `admin_entity_row` exactly.

**Relationship endpoints** (`id_contact_entity_relationship`):

| Op | Endpoint | Method |
|----|----------|--------|
| Read one row | `/admin_contact_entity_relationship_row/{id}` | GET |
| Edit (PUT-replace) | `/admin_contact_entity_relationship_row/{id}` | PUT |
| Delete | `/admin_contact_entity_relationship_row/{id}` | DELETE |
| Create | `/contact_joins` | POST |

OPTIONS on the row returns `GET, HEAD, DELETE, PUT, OPTIONS`. The
**raw** admin row uses `time_from` / `time_to` (the joined read-view
`entity_contacts/{id}/` renames them `per_time_from` / `per_time_to`):

```json
{ "id": 5018, "id_contact": 1256, "id_entity": 4316,
  "role": "owner", "time_from": "2025-02-04T15:32:38", "time_to": null }
```

**Contact-entity endpoints** (`id_contact`), discovered alongside,
NOT yet wired (Phase 5):

| Op | Endpoint | Method |
|----|----------|--------|
| List | `/contacts` | GET |
| Create | `/contacts` | POST |
| Read | `/contact/{id}/` | GET |
| Edit | `/contact/{id}/` | PUT |

Note `/contact/{id}/` allows PUT — contact-entity edits (phone /
address / name) are reachable, but they're **fleet-global** (one
contact serves many stations), so they're deferred to Phase 5 with a
louder confirmation path.

> **Verification status (2026-05-31):** endpoints discovered by
> read-only GET/OPTIONS probing; writer methods + verbs validated in
> **dry-run only** (27 unit tests mock `_request` — they pin payload
> *construction*, not TOS *acceptance*). **No live write has been
> executed against any of these three endpoints.** The
> `POST /contact_joins` body in particular is **inferred** from the
> `/joins` analogy — `/joins` takes `id_entity_parent`/`id_entity_child`
> while we send `id_contact`/`id_entity`; this is the most likely to
> 400 on first real use. First live write should be the HEDI rel-5018
> date fix (the operator's actual need) with a re-GET diff to confirm
> the PUT contract + that the GET response is the complete row (not a
> projection that PUT-replace would null).

## Phase 1 — writer methods (implemented; live-write unverified)

`src/tostools/api/tos_writer.py`:

```python
def get_contact_relationship(self, id_relationship):
    # GET /admin_contact_entity_relationship_row/{id}
def patch_contact_relationship(self, id_relationship, *, time_from=None, time_to=None, role=None):
    # GET-merge-PUT (admin endpoint is PUT-replace, not PATCH)
def create_contact_relationship(self, id_contact, id_entity, role, time_from, time_to=None):
    # POST /contact_joins
def delete_contact_relationship(self, id_relationship):
    # DELETE /admin_contact_entity_relationship_row/{id}
```

`patch_contact_relationship` GET-merges-PUTs because the admin endpoint
replaces the whole row: it reads the current row, overlays the changed
fields, writes the full row back. Dry-run respects `self.dry_run` (the
GET still runs — reads are safe; only the PUT/POST/DELETE is held).
Dates normalised via `_tos_date`.

## Phase 2 — ACTION verbs (implemented; live-write unverified)

Triage-file forms so contact corrections batch with metadata fixes in
one `tos audit apply` (the retrospective-writes-provenance pattern —
the committed triage file is the audit trail). The `id_entity` slot
holds the **station** (so the `start` date-token resolves against the
station's earliest_known — exactly the founding date you backdate a
migration artifact to); the relationship / contact id is the first
positional arg, same convention as `patch-join-date`:

```
ACTION <id_entity> patch-contact-relationship <id_rel> <field> <value>  # field ∈ {time_from,time_to,role}
ACTION <id_entity> assign-contact <id_contact> <role> <time_from>
ACTION <id_entity> delete-contact-relationship <id_rel>
```

`start` / `now` date tokens, `<FILL_*>` placeholder rejection, field
whitelist, and writer-exception-as-failed all match the other ACTION
verbs.

## Phase 3 — standalone CLI verbs (implemented; live-write unverified)

```
tos contact patch-relationship <id_rel> --time-from DATE [--time-to DATE] [--role R] [--no-dry-run]
tos contact assign --station S --contact <id> --role owner --from DATE [--no-dry-run]
tos contact remove <id_rel> [--no-dry-run]
```

Dry-run default + `--no-dry-run` to commit, matching `tos device add`
/ `tos visit add`. `--json` on all three.

## Phase 4 — audit (migration-artifact pattern, NOT YET BUILT)

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
`earliest_known` (the same anchor `start` resolves to elsewhere). The
emitter would write `#ACTION <station> patch-contact-relationship
<id_rel> time_from start` lines.

## Phase 5 — contact-entity writes (NOT YET BUILT)

`PUT /contact/{id}/` edits the contact entity (name / phone / address
/ start_date). **Fleet-global** — one contact serves many stations —
so it needs a louder confirmation than the per-station relationship
edits. `POST /contacts` creates a new contact entity. Deferred until
there's a concrete need; the relationship edits cover the immediate
migration-date problem.

## Effort

| Phase | Work | Status |
|---|---|---|
| 0 | Endpoint discovery | ✅ done (read-only probing) |
| 1 | Writer methods + mock tests | ✅ shipped |
| 2 | ACTION verbs | ✅ shipped |
| 3 | Standalone `tos contact` verbs | ✅ shipped |
| 4 | `tos audit contact-dates` + triage emitter | ⏳ follow-up (fleet probe first) |
| 5 | Contact-entity writes (`PUT /contact/{id}/`) | ⏳ deferred (fleet-global blast radius) |

## Cross-references

- `docs/architecture/tos-write-api.md` — the write-endpoint inventory
  this extends
- Memory `project_2014_10_17_metadata_cleanup_artifacts` — the
  analogous attribute migration-artifact pattern
- Memory `project_contact_write_support` — this scope, condensed
