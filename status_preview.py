#!/usr/bin/env python3
"""
What would happen if the scorer were allowed to write the Status column.

score_to_status() has always computed a definitive Active/Inactive verdict that
nothing writes, deliberately, until the scoring is trusted. This reports what
that write would have done, without doing it, so the decision can be made
against counts and named examples instead of a guess.

Two directions are not equal. Writing Active is reversible and low-stakes.
Writing Inactive is not: is_excluded() skips any record whose Status is
Inactive, so a record written Inactive is removed from the queue and never
re-scored, and a wrong one stays wrong with nothing to correct it. Those are
listed in full rather than summarised.

    python3 status_preview.py              # everything scored today
    python3 status_preview.py 2026-09-14   # a specific check date
"""
import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date

BASE, TABLE = "appYHxsLYleU2RVYk", "Listings"
FIELDS = ("Project name", "Status", "Activity status", "Liveliness score",
          "Last activity date", "Website URL", "False inactive")

pat = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_API_KEY")
if not pat:
    sys.exit("set AIRTABLE_PAT")
when = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()


def score_to_status(score):
    """The scorer's own rule, copied so this stays a read-only tool."""
    if score is None:
        return None
    if score >= 70:
        return "Active"
    if score < 20:
        return "Inactive"
    return None


records, offset = [], None
# A date field compares as a date, not a string: {field}="2026-09-14" silently
# matches nothing, so the format has to be forced.
fmt = 'DATETIME_FORMAT({Last timeliness check},"YYYY-MM-DD")'
while True:
    params = [("filterByFormula", '%s="%s"' % (fmt, when)), ("pageSize", "100")]
    params += [("fields[]", f) for f in FIELDS]
    if offset:
        params.append(("offset", offset))
    url = "https://api.airtable.com/v0/%s/%s?%s" % (BASE, urllib.parse.quote(TABLE),
                                                    urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + pat})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    records.extend(d.get("records", []))
    offset = d.get("offset")
    if not offset:
        break

moves = collections.Counter()
to_inactive, to_active = [], []
for rec in records:
    f = rec["fields"]
    now_status = f.get("Status")
    would = score_to_status(f.get("Liveliness score"))
    if would is None:
        moves["left alone (score between 20 and 70)"] += 1
        continue
    if would == now_status:
        moves["already agrees"] += 1
        continue
    moves["%s -> %s" % (now_status or "(blank)", would)] += 1
    row = (f.get("Project name") or "?", now_status, f.get("Liveliness score"),
           f.get("Last activity date"), f.get("Website URL"), rec["id"])
    (to_inactive if would == "Inactive" else to_active).append(row)

print("%d record(s) checked on %s\n" % (len(records), when))
for k, v in moves.most_common():
    print("  %-40s %d" % (k, v))

print("\n=== would be written Inactive: %d ===" % len(to_inactive))
print("These leave the queue for good — is_excluded() skips a record whose Status")
print("is Inactive, so nothing re-scores them and a wrong one stays wrong.\n")
for name, was, score, last, site, rid in sorted(to_inactive, key=lambda r: r[2] or 0):
    print("  %-40s was %-8s score %-5s last activity %s"
          % (name[:40], was or "(blank)", score, last or "none"))
    print("       site    %s" % (site or "(none on record)"))
    print("       profile https://app.civictech.guide/p/?recordId=%s" % rid)

print("\n=== would be written Active: %d ===" % len(to_active))
for name, was, score, last, site, rid in sorted(to_active, key=lambda r: -(r[2] or 0))[:15]:
    print("  %-40s was %-8s score %-5s last activity %s"
          % (name[:40], was or "(blank)", score, last or "none"))
    print("       site    %s" % (site or "(none on record)"))
    print("       profile https://app.civictech.guide/p/?recordId=%s" % rid)
if len(to_active) > 15:
    print("  ... and %d more" % (len(to_active) - 15))
