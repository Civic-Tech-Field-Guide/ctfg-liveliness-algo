#!/usr/bin/env python3
"""
Put back the activity status of records a bulk pass should not have scored.

A bulk re-scoring run fed ids straight to --records, which bypasses
is_excluded() by design so a curator can force a single re-check. In bulk that
overwrote records the daily queue deliberately leaves alone.

Restores the same way restore_scored_wrong() does: Status carries the verdict,
so Activity status is set back to it. Status only holds Active / Inactive / N/A,
and N/A is not an activity, so N/A and blank both restore to a blank Activity
status rather than inventing a value. The score stays cleared — an excluded
record is one nothing is claiming to have measured — except where a curator
ticked False inactive, which is an explicit human verdict and carries
EXEMPT_SCORE the way the restore path does.

    python3 restore_excluded.py .backfill-state.redone            # dry run
    python3 restore_excluded.py .backfill-state.redone --apply
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE, TABLE = "appYHxsLYleU2RVYk", "Listings"
EXEMPT_SCORE = 100
FIELDS = ("Project name", "Status", "Activity status", "Liveliness score", "False inactive")

pat = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_API_KEY")
if not pat:
    sys.exit("set AIRTABLE_PAT")
if len(sys.argv) < 2:
    sys.exit(__doc__)

state = sys.argv[1]
apply_changes = "--apply" in sys.argv
ids = [l.strip() for l in open(state) if l.strip()]


def api(path, method="GET", payload=None):
    url = "https://api.airtable.com/v0/%s/%s%s" % (BASE, urllib.parse.quote(TABLE), path)
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "Bearer " + pat,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


records = []
for i in range(0, len(ids), 50):
    chunk = ids[i:i + 50]
    params = [("filterByFormula", "OR(%s)" % ",".join('RECORD_ID()="%s"' % r for r in chunk)),
              ("pageSize", "100")] + [("fields[]", f) for f in FIELDS]
    records.extend(api("?" + urllib.parse.urlencode(params)).get("records", []))

updates, held, skipped = [], [], 0
for rec in records:
    f = rec.get("fields", {})
    status, exempt = f.get("Status"), bool(f.get("False inactive"))
    if status not in ("Inactive", "N/A") and not exempt:
        skipped += 1
        continue
    # A record the curator marked Inactive that has just scored high on a dated
    # signal is not a record to quietly revert: the algorithm found something,
    # and which of the two is wrong is a curator's call, not this script's.
    score_now = f.get("Liveliness score")
    if isinstance(score_now, (int, float)) and score_now >= 70 and not exempt:
        held.append((f.get("Project name", rec["id"]), status, score_now, rec["id"]))
        continue

    restore = status if status in ("Active", "Inactive") else None
    fields = {"Activity status": restore,
              "Liveliness score": EXEMPT_SCORE if exempt else None}
    if (f.get("Activity status") == restore
            and f.get("Liveliness score") == fields["Liveliness score"]):
        skipped += 1
        continue
    updates.append({"id": rec["id"], "fields": fields,
                    "_name": f.get("Project name", rec["id"]),
                    "_was": (f.get("Activity status"), f.get("Liveliness score"))})

print("%d id(s) in %s — %d scoreable or already correct, %d to restore\n"
      % (len(ids), state, skipped, len(updates)))
for u in updates:
    was_act, was_score = u["_was"]
    print("  %-42s %s / %s  ->  %s / %s"
          % (u["_name"][:42], was_act, was_score,
             u["fields"]["Activity status"], u["fields"]["Liveliness score"]))

if held:
    print("\n%d record(s) left alone — marked Inactive/N/A but scoring 70+ on a dated\n"
          "signal, so the contradiction needs a curator rather than a revert:" % len(held))
    for n, st, sc, rid in sorted(held, key=lambda x: -x[2]):
        print("  %-40s %-9s score %s" % (n[:40], st, sc))
        print("       https://app.civictech.guide/p/?recordId=%s" % rid)

if not updates:
    sys.exit(0)
if not apply_changes:
    print("\ndry run — pass --apply to write these")
    sys.exit(0)

for i in range(0, len(updates), 10):
    batch = [{"id": u["id"], "fields": u["fields"]} for u in updates[i:i + 10]]
    api("", method="PATCH", payload={"records": batch})
    print("wrote %d" % len(batch))
print("\nrestored %d record(s)" % len(updates))
