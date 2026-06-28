# Duplicate-device merge — scoping

Status: **scoping / not implemented** (2026-06-28). Author: fleet-sweep cleanup session.

## Problem

When a physical receiver moves from station A to station B, the correct TOS action
is `move_device` (Pattern 2: close A's join, open B's join on the **same** entity).
Historically some moves were entered as a **new device entity at B** instead. Result:
**two entity rows with the same serial**, one physical unit — a duplicate.

Confirmed pairs (from the 2026-06 fleet sweep):

| Serial | Entities | Physical history |
|--------|----------|------------------|
| NETR9 `5048K71916` | dev **4910** (HOTJ) + dev **16358** (BRTT) | HOTJ 2012-06-27→2019-08 → BRTT 2019-08→2024-10 |
| POLARX5 `4100591` | dev **20576** (LISF) + dev **20858** (URHC) | LISF 2024-10→11 → URHC 2025-03→ |

After the swap fixes, the redundant legs are **closed** (no open-join conflict, no sweep
flag), so this is a **latent inventory-quality defect**, not an operational break. Costs:

- `find_device_by_serial` is ambiguous (returns one of N — first match wins).
- Inventory looks like 2 physical units where there is 1.
- The unit's life history is split across two records; neither is complete.

This is **not** caught by `tos audit fleet-sweep` (which only inspects the *open* receiver).
It needs its own detector.

## Feasibility — entity deletion UNCONFIRMED (validate before building)

Earlier belief ("the ACTION grammar has no delete-entity") was a **tooling** gap. Whether the
*API* supports entity deletion is **not yet established**. Probe (2026-06-28):

```
OPTIONS /admin_entity_row/16358  → 405, Allow: DELETE      # admin row endpoint
OPTIONS /entity/16358            → 405, Allow: GET          # public, read-only
```

This is **suggestive, not authoritative**. The `Allow: DELETE` header is provably unreliable on
this server: `change_subtype` does `PUT /admin_entity_row/<id>` in production and that works — yet
the header omits PUT. A header that's wrong about PUT cannot be trusted to be right about DELETE.
So the probe establishes only that the admin row endpoint exists; it does **not** confirm DELETE
actually removes an entity.

Worse, the same **admin-DELETE family** has a documented **silent-no-op** history: `delete-join`
on `/admin_entity_connection_row/{id}` intermittently returns success while the row survives (the
FIHO quirk, seen repeatedly this session). "Nominally allowed" ≠ "reliably deletes." Entity DELETE
could 200-and-no-op too.

**Almost certainly no cascade**: TOS exposes a *separate* admin DELETE per row type, so an entity
DELETE will likely be rejected (FK constraint) while joins / attribute-values still reference it —
entity delete must be the **last** step, after joins and attribute rows are removed.

### Gate: a throwaway-entity test decides the whole approach

Before any merge tool is built, run this on a junk entity (not real data):

1. `tos device add` a fake serial to B9 (creates entity + intake join).
2. Delete its intake join + its attribute-values.
3. `DELETE /admin_entity_row/{id}` on the now-bare entity.
4. **Re-read** `GET /entity/{id}` and confirm it is genuinely gone (404), not a 200-no-op.

If step 4 fails (entity survives), true-merge is **off the table** — fall back to Plan B below.
This gate, not the OPTIONS header, is what green-lights `tos device merge`.

## The merge operation

Two variants. **Plan A** (true delete) needs the gate above to pass; **Plan B** (no delete) is the
strictly-safer fallback and works regardless.

### Plan A — `merge(loser → survivor, cutover_date)` (delete the loser)

1. **Pick the survivor.** Keep the entity whose identity/attributes are richer or whose record
   is the one downstream tooling already references. (Heuristic: the earlier-created entity, or
   the one with the longer join history. For the two known pairs the *earlier* entity — 4910,
   and for 4100591 whichever holds the current open join — is the natural survivor.)
2. **Consolidate joins.** For each of the loser's station joins, recreate it on the survivor
   (`create_entity_connection`), resolving the **overlap** at the A↔B junction (see below) by
   choosing a single `cutover_date`. The B9 intake / zero-day joins are dropped, not copied.
3. **Strip the loser:** `delete_entity_connection` every loser join, then
   `delete_attribute_value` every loser attribute-value row.
4. **Delete the loser entity:** `DELETE /admin_entity_row/{loser}` (new `TOSWriter.delete_entity`).
5. **Back-date survivor attrs** if the consolidated joins now predate the survivor's
   `serial_number` / `model` `date_from` (the recurring `2014-10-17` migration-artifact problem —
   same `patch-attribute-date` fix used throughout the sweep).

### Plan B — consolidate + husk (no entity delete)

Identical steps 1–2 and 5 (pick survivor, recreate the loser's joins on the survivor with the
cutover, back-date attrs), but instead of deleting the loser:

- `delete_entity_connection` the loser's joins (so it no longer claims any station), and
- mark the loser inert: `decommission` (óvirkt) and/or add a `merged_into=<survivor_id>` /
  `duplicate_of` attribute as a breadcrumb so tooling and `find_device_by_serial` can resolve the
  husk to the canonical entity.

The loser entity row survives as a tombstone. This **fully clears** the operational symptoms —
`find_device_by_serial` ambiguity (the husk is flagged `merged_into`) and the split history (all
joins now on the survivor) — without any irreversible entity DELETE. The only thing it doesn't do
is reclaim the row, which is cosmetic. **Plan B is the default recommendation** unless the
throwaway gate proves DELETE is clean *and* there's a concrete reason to reclaim rows.

### The overlap / cutover problem

Archive dates at a station→station junction frequently **overlap** (e.g. `5048K71916`: HOTJ
archive runs to ~2019-08-09 while BRTT archive starts 2019-07-20). One physical unit cannot be at
two stations at once, so a single-device timeline **must** pick one cutover date — it cannot
faithfully hold both. The merge therefore *requires* an explicit `--at <date>` (default: the
later station's join `time_from`) and records the chosen boundary. This is the core reason the
merge can't be fully automatic.

## Detection (build first)

There is no "list all entities of subtype" endpoint; `tos device list` is parent-scoped. A
fleet-wide dup scan must walk the **global join index** (the `fleet-gaps` pattern — every
parent's `children_connections`), collect every `gnss_receiver` (and antenna/…) entity id, fetch
each serial, and group by `(subtype, serial)` → report any serial with ≥2 entity ids. Read-only,
~1 fleet walk. This sizes the problem and becomes the verify oracle after merges.

**BUILT 2026-06-28**: `tos audit duplicate-serials` (`src/tostools/audit_duplicate_serials.py`,
read-only). Walks `build_join_index`, enriches each device with `(subtype, serial)`, skips
placeholder serials (`_synthetic` + an all-same-digit rule for `99999999`), groups by
`(subtype, serial)`. Flags: `--subtype`, `--include-synthetic`, `--json`, `--strict`,
`--no-progress`. Exit 0 (or 1 with `--strict`). ~5.5 min on the live fleet (204 parents, 0 failed
→ complete census).

**Measured (first run, gnss_receiver, 2026-06-28): 19 duplicate groups / 38 entities** — far
more than the 2 known. Most are the create-instead-of-move signature: a deployed station entity
+ a B9-warehouse husk sharing one serial (the *easy* case — Plan B husk-consolidation is clean
since the husk has no real station claim). The 2 known pairs (`5048K71916` 4910+16358;
`4100591` 20576+20858) are confirmed. One oddball to eyeball: serial `MO5` (19443 @ B9 + 19478 @
Gónhóll) — a model-code-looking string that passes the placeholder filter. Full list:
`gps-tos-corrections/fleet_sweep/duplicate_serials_20260628.json`. 19 (not 2) confirms the merge
tooling is worth building. (A full all-subtype run finds **190 groups** — the duplicate pattern
is fleet-wide across every instrument type, not just GPS; the non-GPS ones are other teams'
equipment but the same data-quality defect.)

### ⚠️ `find_device_by_serial` (basic_search) is UNRELIABLE — root cause of dups

The detector's first run exposed *why* dups keep getting created, and proved it by catching one
the GRAN reconstruction had **just** made: `find_device_by_serial("5046K71747")` returns `None`
even though **two** entities carry that serial (dev 4909 @ GSIG, dev 21602 @ GRAN). The
`POST /basic_search` index does not return some existing serials. So the device-intake guard
"search by serial → not found → create a new entity" **silently creates duplicates** whenever the
search misses — almost certainly the mechanism behind a large share of the 190 fleet-wide dups.

Consequences for design:
- **Detection / survivor-finding must use the join-index walk, never basic_search.** The
  `duplicate-serials` detector is authoritative; `find_device_by_serial` is not.
- **`tos device add` / `cfg add-receiver` should cross-check against the join-index** (or at least
  warn) before creating — a basic_search miss is not proof of absence.
- The merge tool must take explicit `--from`/`--into` entity **ids**, never resolve "the entity
  with serial X" via basic_search.

(The GRAN self-inflicted dup is fixed by `gps-tos-corrections/gran/gran_fix_5046_duplicate_20260628.txt`:
consolidate the GRAN leg onto dev 4909, orphan dev 21602 — the first real Plan-B consolidation.)

## Proposed CLI surface

- **Detector:** `tos audit duplicate-serials` (read-only report; exit 0, or 1 with `--strict`).
- **Merge:** `tos device merge --from <loser_id> --into <survivor_id> --at <YYYY-MM-DD>
  [--apply]`. Dry-run by default; prints the full plan (joins to recreate, rows to delete,
  attr back-dates). **Hard guard:** refuse unless `loser.serial == survivor.serial` and same
  subtype. Single station/operator-confirmed.
- A `merge-device` ACTION verb for `tos audit apply` is possible but **not recommended**: merge
  is multi-step, order-sensitive, and irreversible — a dedicated command with its own dry-run plan
  is safer than an ACTION line.

## Risks

- **Irreversible.** Entity DELETE has no undo. Dry-run + explicit `--apply` + the same-serial
  guard are mandatory. Consider archiving the loser's full history (JSON dump) before deletion.
- **Admin DELETE flakiness.** Join deletes intermittently silent-no-op (the FIHO quirk). The
  merge must **re-read** after each delete and fail loudly if a row survives — otherwise it could
  delete the entity while a dangling join remains (orphan join → broken history view).
- **FK ordering.** Entity delete only after all joins + attribute-values are gone; verify each.
- **Wrong survivor / wrong cutover.** Operator picks both explicitly; no silent defaults beyond
  the documented `--at` fallback.
- **Out-of-scope reach.** The `4100591` pair spans URHC (not a swept station) — a merge there
  touches URHC's open receiver. Confirm URHC's state first; do the in-scope `5048K71916` pair
  (HOTJ+BRTT, both reconstructed) as the pilot.

## Recommendation

1. **Build `tos audit duplicate-serials`** (cheap, read-only, global join walk grouped by
   `(subtype, serial)`) → measure the real fleet count. Only 2 pairs are *known*; the true count
   is unmeasured and decides whether merge tooling is worth it at all.
2. **Run the throwaway-entity gate** (above) to learn whether `DELETE /admin_entity_row` actually
   removes an entity on this backend. This — not the OPTIONS header — decides Plan A vs Plan B.
3. **Build `tos device merge`** with dry-run, same-serial+same-subtype guard, re-read-after-each-
   delete safety (FIHO no-op defence), explicit `--at` cutover, and a pre-deletion JSON dump of the
   loser. Implement **Plan B** (no delete) as the default path; enable the Plan A delete step only
   if step 2 passed. Pilot on `5048K71916` (HOTJ+BRTT, both in-scope/reconstructed). Defer
   `4100591` until URHC's state is confirmed (out of scope).

Net: worth doing for inventory integrity. Plan B (consolidate + husk) is safe and sufficient for
the operational symptoms today. Plan A (true delete) is the **highest-risk** write in the toolkit
(irreversible, on a flaky admin-DELETE family whose support is unconfirmed) — pursue it only if the
throwaway gate proves the delete is clean and reclaiming rows is actually wanted.
