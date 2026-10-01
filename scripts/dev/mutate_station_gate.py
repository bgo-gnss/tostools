#!/usr/bin/env python3
"""Prove the station-gate tests actually test the station gate.

A green suite over an UNWIRED helper is indistinguishable from a working
feature. This repo has been bitten by that repeatedly — tests that passed
while the thing they claimed to cover was reverted — so every wiring point
of the gate gets broken in turn, and the test that claims to cover it must
go **red**.

The critical ones are the WIRING mutations, not the helper mutations. A
correct ``select_station`` reached by nobody is an orphaned abstraction;
mutating only the helper would never notice.

Usage::

    python3 scripts/dev/mutate_station_gate.py          # all mutations
    python3 scripts/dev/mutate_station_gate.py -k soho  # one subset

Do NOT run this beside a test run: it rewrites ``src/`` in a loop and would
corrupt both. It refuses if it sees a pytest already running, and it always
restores the tree, including on Ctrl-C.

Anchors are literal source text and WILL rot when the code moves. A rotted
or ambiguous anchor fails loudly (``ANCHOR NOT FOUND`` / ``AMBIGUOUS``), as
does a selector matching no tests (``NO TESTS MATCHED``) — fix the anchor,
never delete the mutation.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "tostools"
TESTS = "tests/test_station_kind_gate.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    why: str
    #: pytest -k selector for the test(s) that MUST go red
    expect_red: str
    #: how many occurrences the anchor is EXPECTED to match. >1 where one
    #: logical seam is spread over identical call sites (the station
    #: fan-out pre-resolve sits in both `verify` and `triage`). A count
    #: mismatch fails loudly rather than silently mutating the wrong number.
    count: int = 1


MUTATIONS = [
    # ======================= the wiring =======================
    Mutation(
        name="dispatch-passes-no-profile",
        path="tosGPS.py",
        old="return handler(sys.argv[2:], profile=gps_profile())",
        new="return handler(sys.argv[2:], profile=None)",
        why=(
            "The single line that turns the filter on for EVERY profiled verb. "
            "Revert it and the whole feature is inert while each helper still "
            "works perfectly. This is THE mutation — a suite that stays green "
            "here proves nothing at all."
        ),
        expect_red="cannot_see or resolves_the_gps_entity",
    ),
    Mutation(
        name="station-handler-drops-the-predicate",
        path="tos.py",
        old="    predicate = profile.admits_station if profile is not None else None",
        new="    predicate = None",
        why="_station_main's derivation — breaks every `station` verb at once.",
        expect_red="station-show or station-verify or station-set",
    ),
    Mutation(
        name="station-fan-out-skips-the-pre-resolve",
        path="tos.py",
        old="ok, _gated_id = _gated_station_id(client, args.station, predicate)",
        new="ok, _gated_id = (True, None)",
        why=(
            "`station verify` / `station triage` would fall through to the "
            "audits, which re-resolve the marker UNGATED and reach the very "
            "entity the filter excluded."
        ),
        expect_red="station-verify or station-triage or same_entity",
        count=2,
    ),
    Mutation(
        name="gated-lookup-miss-proceeds-anyway",
        path="tos.py",
        old=(
            "        return False, None\n" '    return True, int(entity["id_entity"])'
        ),
        new=("        return True, None\n" '    return True, int(entity["id_entity"])'),
        why=(
            "The miss branch is what makes a filtered-out station behave as "
            "NOT FOUND. Returning True instead lets the verb run unconstrained."
        ),
        # Deliberately an OFFLINE selector: the dispatch tests can only
        # reach this through a cassette mismatch, which proves nothing.
        expect_red="gated_station_id_reports_a_miss",
    ),
    Mutation(
        name="audit-handler-never-gates",
        path="tos.py",
        old="if not _gate_audit_station(",
        new="if False and _gate_audit_station(",
        why=(
            "The audit verbs are where the FALSE PASS lives — "
            "`audit missing-attributes VLFS` IS the vacuous oracle."
        ),
        expect_red="audit-station or audit-missing-attributes",
    ),
    Mutation(
        name="device-list-drops-the-predicate",
        path="tos.py",
        old=(
            "    parent_id = _resolve_parent_id(\n"
            "        client,\n"
            "        predicate=predicate,"
        ),
        new=(
            "    parent_id = _resolve_parent_id(\n"
            "        client,\n"
            "        predicate=None,"
        ),
        why="`device list --station` is a sibling that could be missed alone.",
        expect_red="device-list or resolves_the_gps_entity",
    ),
    Mutation(
        name="station-show-device-delegates-ungated",
        path="tos.py",
        old="_device_list_main(delegated, predicate=predicate)",
        new="_device_list_main(delegated)",
        why=(
            "`station show VLFS --device` built its own namespace and lost the "
            "predicate — zero devices, exit 0, the vacuous pass on the verb the "
            "plain `station show` test appears to cover. Found in review."
        ),
        expect_red="station-show-device",
    ),
    Mutation(
        name="station-write-path-ungated",
        path="tos.py",
        old="client, station_marker=station, predicate=predicate",
        new="client, station_marker=station, predicate=None",
        why=(
            "`station set` / `station describe` are WRITES; ungated they PATCH "
            "a precipitation gauge, or the DOAS gas station for SOHO. Also "
            "covers `station receivers`. Found in review."
        ),
        expect_red="station-set or station-describe or station-receivers",
        count=2,
    ),
    # ======================= the predicate =======================
    Mutation(
        name="select-takes-the-first-candidate",
        path="station_kind.py",
        old="    admitted = [c for c in cands if predicate(c)]",
        new="    admitted = cands[:1]",
        why="The filter collapses to first-hit: SOHO lands on the DOAS station.",
        expect_red="cannot_see or resolves_the_gps_entity",
    ),
    Mutation(
        name="predicate-ignores-entity-type",
        path="search_selectors.py",
        old='            if entity.get("code_entity_subtype") not in self.entity_scopes:',
        new="            if False:",
        why=(
            "Drops the level that excludes other disciplines. Only ONE test can "
            "catch it — a non-geophysical entity with NO subtype attribute. "
            "Everything else is caught on the subtype level, which is exactly "
            "why this mutation survived the harness's first run."
        ),
        expect_red="no_subtype_is_still_excluded",
    ),
    Mutation(
        name="predicate-ignores-the-subtype-attribute",
        path="search_selectors.py",
        old=(
            "        if found is not None:\n" "            return found == self.subtype"
        ),
        new="        if found is not None:\n            return True",
        why=(
            "Drops the level that separates GPS from SIL/DOAS — both are "
            "`geophysical`, so SOHO becomes a coin toss again."
        ),
        expect_red="entity_type_alone or resolves_the_gps_entity",
    ),
    Mutation(
        name="closed-subtype-reads-as-never-set",
        path="search_selectors.py",
        old="        return not ever or self.subtype in ever",
        new="        return True",
        why=(
            "A decommissioned DOAS station (only a CLOSED `subtype` period) "
            "would be handed to the GPS audits through the absent-subtype "
            "leniency. Raised in review."
        ),
        expect_red="decommissioned_doas",
    ),
]


def _guard_no_concurrent_pytest() -> None:
    out = subprocess.run(
        ["pgrep", "-af", "pytest"], capture_output=True, text=True
    ).stdout
    live = [
        ln
        for ln in out.splitlines()
        if "mutate_station_gate" not in ln and "pgrep" not in ln
    ]
    if live:
        sys.exit(
            "REFUSING: a pytest run is already live — this script rewrites src/ "
            "in a loop and would corrupt both.\n  " + "\n  ".join(live)
        )


def _apply(m: Mutation) -> str:
    path = SRC / m.path
    text = path.read_text(encoding="utf-8")
    n = text.count(m.old)
    if n == 0:
        sys.exit(f"ANCHOR NOT FOUND in {m.path} for {m.name!r} — fix the anchor.")
    if n != m.count:
        sys.exit(
            f"ANCHOR COUNT CHANGED in {m.path} for {m.name!r}: found {n}, "
            f"expected {m.count}. The code moved — fix the anchor or the count, "
            f"never delete the mutation."
        )
    path.write_text(text.replace(m.old, m.new), encoding="utf-8")
    return text


def _pytest(selector: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TESTS,
            "-q",
            "-p",
            "no:randomly",
            "--record-mode=none",
            "-k",
            selector,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-k", dest="filter", default="", help="substring of mutation name")
    args = ap.parse_args()
    _guard_no_concurrent_pytest()

    chosen = [m for m in MUTATIONS if args.filter in m.name]
    if not chosen:
        sys.exit(f"no mutation matches {args.filter!r}")

    # Baseline: everything must be green before breaking anything, or a
    # "detected" verdict could just be a pre-existing failure.
    base = _pytest("")
    if base.returncode != 0:
        print(base.stdout[-4000:])
        sys.exit("BASELINE IS RED — fix the suite before trusting any mutation.")
    print(f"baseline: green ({len(chosen)} mutation(s) to run)\n")

    results = []
    backups: dict[str, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="mutate-gate-"))
    try:
        for m in chosen:
            original = _apply(m)
            backups[m.path] = original
            # stale .pyc from the unmutated file reads as "not detected"
            for pyc in SRC.rglob("__pycache__"):
                shutil.rmtree(pyc, ignore_errors=True)
            run = _pytest(m.expect_red)
            (SRC / m.path).write_text(original, encoding="utf-8")
            backups.pop(m.path, None)

            blob = run.stdout + run.stderr
            if "no tests ran" in run.stdout or "NO TESTS" in run.stdout:
                verdict = "NO TESTS MATCHED"
            elif "CannotOverwriteExistingCassette" in blob and (
                "AssertionError" not in blob and "assert" not in blob
            ):
                # The mutation changed the REQUEST SEQUENCE, so VCR refused
                # before any assertion ran. The suite went red, but not for
                # the reason the test claims to check — a much weaker signal,
                # and reporting it as DETECTED would overstate the coverage.
                verdict = "CASSETTE-ONLY"
            elif run.returncode != 0:
                verdict = "DETECTED"
            else:
                verdict = "NOT DETECTED"
            results.append((m, verdict))
            mark = "✓" if verdict == "DETECTED" else "✗"
            print(f"{mark} {verdict:16} {m.name}")
            if verdict != "DETECTED":
                print(f"    selector: -k {m.expect_red!r}")
                print(f"    why it matters: {m.why}")
                (tmp / f"{m.name}.log").write_text(run.stdout + run.stderr)
                print(f"    log: {tmp / (m.name + '.log')}")
    finally:
        for path, text in backups.items():
            (SRC / path).write_text(text, encoding="utf-8")
        for pyc in SRC.rglob("__pycache__"):
            shutil.rmtree(pyc, ignore_errors=True)

    missed = [m.name for m, v in results if v != "DETECTED"]
    print()
    if missed:
        print(f"{len(missed)} mutation(s) NOT caught: {', '.join(missed)}")
        return 1
    print(f"all {len(results)} mutation(s) detected — the tests test the wiring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
