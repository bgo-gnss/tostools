"""OLKE Trimble 4700 (device 4798) firmware Pattern-2: 1.12 -> 1.30 at 2000-10-17.
Preserves the 1.12 history (closes it at 2000-10-17) and opens 1.30 from there.
DRY-RUN unless run with `commit` arg.  (The 1.30 datum currently lives on the
bogus duplicate 4979, which the structural ACTION file deletes.)"""
import sys
from tostools.api.tos_writer import TOSWriter

commit = len(sys.argv) > 1 and sys.argv[1] == "commit"
w = TOSWriter(dry_run=not commit)
for code in ("firmware_version", "software_version"):
    r = w.transition_attribute_value(4798, code, "1.30", "2000-10-17")
    print(f"{code:18} -> closed={r.get('closed')!r}  opened={r.get('opened')!r}")
print("COMMITTED" if commit else "DRY-RUN (no writes) — re-run with: ... olke_firmware_transition.py commit")
