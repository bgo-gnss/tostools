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


MUTATIONS = [
    # ---------------- the wiring ----------------
    Mutation(
        name="dispatch-passes-no-profile",
        path="tosGPS.py",
        old="return handler(sys.argv[2:], profile=gps_profile())",
        new="return handler(sys.argv[2:], profile=None)",
        why=(
            "The single line that turns the gate on. Revert it and the whole "
            "feature is inert while every helper still works perfectly. This "
            "is THE mutation — if the suite stays green here, it proves nothing."
        ),
        expect_red="refuses or resolves_the_gps_entity",
    ),
    Mutation(
        name="station-handler-drops-the-predicate",
        path="tos.py",
        # `_fleet_predicate = profile.admits_station ...` CONTAINS the bare
        # form as a substring, so the anchor carries its comment to stay unique.
        old=(
            "    # `_visit_main(profile=None)` already use.\n"
            "    predicate = profile.admits_station if profile is not None else None"
        ),
        new=("    # `_visit_main(profile=None)` already use.\n" "    predicate = None"),
        why="_station_main's derivation — breaks station show/verify/triage only.",
        expect_red="station-show or station-verify",
    ),
    Mutation(
        name="audit-handler-never-gates",
        path="tos.py",
        old=(
            "    _gate_audit_station(\n"
            "        client, args, profile.admits_station if profile is not None else None\n"
            "    )"
        ),
        new="    pass",
        why=(
            "The audit verbs are where the FALSE PASS lives — "
            "`audit missing-attributes VLFS` is the vacuous oracle itself."
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
        new="    parent_id = _resolve_parent_id(\n        client,\n        predicate=None,",
        why="`device list --station` is a sibling that could be missed on its own.",
        expect_red="device-list or resolves_the_gps_entity",
    ),
    Mutation(
        name="triage-skips-the-pre-resolve",
        path="station_triage.py",
        old="    if predicate is not None and id_entity is None:",
        new="    if False:",
        why=(
            "Without the pre-resolve a refusal is raised INSIDE an audit, where "
            "`except Exception` turns it into 'N audits failed' (exit 2) rather "
            "than a refusal."
        ),
        expect_red="station-verify",
    ),
    # ---------------- the helper ----------------
    Mutation(
        name="select-takes-the-first-candidate",
        path="station_kind.py",
        old="    admitted = [c for c in cands if predicate(c)]",
        new="    admitted = cands[:1]",
        why="Filter-candidates collapses to first-hit: SOHO lands on the DOAS station.",
        expect_red="refuses or resolves_the_gps_entity",
    ),
    Mutation(
        name="predicate-ignores-entity-type",
        path="search_selectors.py",
        old='            if entity.get("code_entity_subtype") not in self.entity_scopes:',
        new="            if False:",
        why=(
            "Drops the level that excludes other disciplines. Only ONE test can "
            "catch this — a non-geophysical entity with NO subtype attribute. "
            "Everything else is caught on the subtype level, which is precisely "
            "why this mutation went undetected on the first run."
        ),
        expect_red="no_subtype_is_still_refused",
    ),
    Mutation(
        name="predicate-ignores-the-subtype-attribute",
        path="search_selectors.py",
        old="        return found is None or found == self.subtype",
        new="        return True",
        why=(
            "Drops the level that separates GPS from SIL/DOAS — both are "
            "`geophysical`, so SOHO becomes a coin toss again."
        ),
        expect_red="entity_type_alone or resolves_the_gps_entity",
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
    if n > 1:
        sys.exit(f"AMBIGUOUS anchor in {m.path} for {m.name!r} ({n} matches).")
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

            if "no tests ran" in run.stdout or "NO TESTS" in run.stdout:
                verdict = "NO TESTS MATCHED"
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
