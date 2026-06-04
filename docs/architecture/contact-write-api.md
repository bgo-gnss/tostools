# Contact-write API — design / scope

**Status:** relationship CRUD SHIPPED + **all three write paths
live-verified** (PUT/POST/DELETE). Phase 4 (`tos audit contact-dates`)
SHIPPED + live-validated. Phase 5 (contact-entity create/edit) SHIPPED,
dry-run validated (no contact-delete endpoint exists, so create is
verified on first genuine use).
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

**Contact-entity endpoints** (`id_contact`), wired in Phase 5:

| Op | Endpoint | Method |
|----|----------|--------|
| List | `/contacts` | GET |
| Create | `/contacts` | POST |
| Read | `/contact/{id}/` | GET |
| Edit | `/contact/{id}/` | PUT |
| **Delete** | — | **none exists** |

`/contact/{id}/` allows only `GET, PUT, HEAD, OPTIONS` (no DELETE) and
`/admin_contact_row/*` 404s — **a contact entity cannot be deleted**,
only deactivated via `end_date`. Editing a contact is **fleet-global**
(one contact serves many stations), so the edit verb shouts that.

> **Verification status (2026-05-31): all three write paths LIVE-VERIFIED.**
>
> * **`PUT /admin_contact_entity_relationship_row/{id}` — LIVE-VERIFIED.**
>   First real write executed against HEDI rel-5018 (the operator's
>   migration-date fix): `time_from 2025-02-04T15:32:38 → 2006-06-29`.
>   Re-GET diff confirmed **only `time_from` changed** — id_contact /
>   id_entity / role / time_to all preserved, **same field set** (the
>   admin GET returns the complete row; the GET-merge-PUT does NOT null
>   unseen columns). The joined read-view `entity_contacts/4316/` now
>   reads `per_time_from = 2006-06-29`, so the fix surfaces in
>   `tos station show`. Provenance: `data/triage/hedi/hedi_contact_5018_backdate_20260531.txt`.
> * **`POST /contact_joins` (assign/create) — LIVE-VERIFIED.** The
>   inferred body `{id_contact, id_entity, role, time_from, time_to}`
>   was **correct** — a throwaway round-trip (assign Veðurstofa 1256 →
>   warehouse B9 id_entity=4 role=operator from 2099-01-01) created
>   rel-5171 with exactly the sent values, confirmed in both the raw
>   admin row and the `entity_contacts/4/` joined view. No
>   `id_entity_parent`/`id_entity_child` rename needed despite the
>   `/joins` analogy worry.
> * **`DELETE /admin_contact_entity_relationship_row/{id}` — LIVE-VERIFIED.**
>   Same round-trip: deleting rel-5171 returned B9 to its empty
>   baseline; a follow-up GET on the row 404s with
>   `"Couldn't find id: 5171 in table: public.contact_entity_relationship"`
>   (also confirms the backing table name). No junk left in production.

## Phase 1 — writer methods (SHIPPED, live-verified)

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

## Phase 2 — ACTION verbs (SHIPPED, live-verified)

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

## Phase 3 — standalone CLI verbs (SHIPPED; patch+remove use the verified paths, assign live-verified via ACTION form)

```
tos contact patch-relationship <id_rel> --time-from DATE [--time-to DATE] [--role R] [--no-dry-run]
tos contact assign --station S --contact <id> --role owner --from DATE [--no-dry-run]
tos contact remove <id_rel> [--no-dry-run]
```

Dry-run default + `--no-dry-run` to commit, matching `tos device add`
/ `tos visit add`. `--json` on all three.

## Phase 4 — `tos audit contact-dates` (SHIPPED)

```
tos audit contact-dates <STN> [--triage path.txt] [--no-suppressions] [--json]
```

**Artifact signature** (fleet probe 2026-05-31, 77 relationships across
71 stations): migration bulk-loads cluster on a few instants, each
shared *identically* across the batch —
`2025-02-04T15:32:38 ×26`, `2025-02-05T11:19:42 ×8`,
`2025-09-12T09:41:14 ×4`. The discriminator is **a non-midnight
time-of-day**: genuine ownership-start dates are recorded at
`T00:00:00` (33 of 38 real-date relationships in the probe); the
bulk-loads all carry a real clock time. So the rule —

> flag a relationship whose `per_time_from` has a non-midnight time
> component —

auto-catches every migration batch without a hardcoded date list, and
is robust to future loads. False positives (a genuine relationship
with a clock time) go in the SUPPRESS file
(`data/audit_suppressions/contact_dates.txt`, key `SUPPRESS
<id_relationship>`); every emitted ACTION is commented for review.

Module `src/tostools/audit_contact_dates.py` mirrors
`audit_visit_coverage` (report dataclass + suppression loader +
`format_triage_file`). The triage emitter writes
`#ACTION <station> patch-contact-relationship <id_rel> time_from start`
per violation — `start` resolves at apply time to the station's
earliest_known (founding date for VÍ-owned sites). Standalone cleanup
audit (not in the recurring verify oracle — migration artifacts are a
one-time fixup, not ongoing drift). Pinned by
`tests/test_audit_contact_dates.py`.

**Fleet sweep** — `tos fleet contact-dates [--triage PATH]` loops the
audit over every GNSS station and aggregates. `--triage` emits ONE
combined file with a confidence split: **owner-role** relationships
uncommented (backdating to `start`/founding is always correct — the
owner owned the station from founding regardless of which org);
non-owner roles (data_owner / operator / observer) commented for review
(they may have a genuinely recent start date). First fleet run
(2026-06-04): 129 violations across 192 stations — 100 owner
(ready-to-apply) + 29 non-owner (review). Provenance:
`data/triage/contact_dates_fleet_20260604.txt`. In `fleet_ops.py`:
`run_fleet_contact_dates` + `format_fleet_contact_dates_triage`.

## Phase 5 — contact-entity writes (SHIPPED; dry-run validated)

Writer `create_contact(*, name, **fields)` → `POST /contacts`;
`patch_contact(id_contact, **fields)` → `PUT /contact/{id}/`
(GET-merge-PUT). Writable fields (`TOSWriter.CONTACT_FIELDS`): name,
organization, job_title, phone_primary/secondary/tertiary, email,
address, comment, start_date, end_date, ssid. Dates `_tos_date`-
normalised.

CLI:
```
tos contact create --name "…" [--organization …] [--phone …] [--email …]
                    [--address …] [--start-date DATE] [--ssid …] [--no-dry-run]
tos contact patch-entity <id_contact> [--name …] [--phone …] … [--no-dry-run]
```

`create` returns the new id_contact (then `tos contact assign` maps it
to a station). `patch-entity` is **fleet-global** — one contact serves
many stations, so a phone/address change propagates everywhere; the
help + output shout this.

> **Verification status:** **dry-run validated only.** Body inferred
> from the GET entity shape (same approach that worked for
> `/contact_joins`). **No live write executed** — and crucially, **there
> is no contact-delete endpoint** (`/contact/{id}/` allows only
> GET/PUT/HEAD/OPTIONS; `/admin_contact_row/*` 404s), so a throwaway
> create could not be cleaned up. The POST is therefore verified on the
> first *genuine* contact-add (confirm via `tos contact show --id
> <new_id>`), not a throwaway. `patch_contact` is reversible via a
> second PUT but fleet-global, so left dry-run-validated too.

Creating a contact cannot be undone — a mis-created contact can only be
deactivated via `--end-date`, not deleted. There is no remaining
deferred contact-write work.

## Effort

| Phase | Work | Status |
|---|---|---|
| 0 | Endpoint discovery | ✅ done (read-only probing) |
| 1 | Writer methods + mock tests | ✅ shipped |
| 2 | ACTION verbs | ✅ shipped |
| 3 | Standalone `tos contact` verbs | ✅ shipped |
| 4 | `tos audit contact-dates` + triage emitter | ⏳ follow-up (fleet probe first) |
| 5 | Contact-entity writes (create `POST /contacts` + edit `PUT /contact/{id}/`) | ✅ shipped (dry-run validated; no delete endpoint) |

## Cross-references

- `docs/architecture/tos-write-api.md` — the write-endpoint inventory
  this extends
- Memory `project_2014_10_17_metadata_cleanup_artifacts` — the
  analogous attribute migration-artifact pattern
- Memory `project_contact_write_support` — this scope, condensed
