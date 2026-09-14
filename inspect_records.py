#!/usr/bin/env python3
"""Dump the fields that decide whether a listing gets scored at all.

Usage:  AIRTABLE_PAT=... python3 inspect_records.py recXXX [recYYY ...]
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "appYHxsLYleU2RVYk"
TABLE = "Listings"
SHOW = ["Project name", "Status", "Activity status", "Liveliness score",
        "Last activity date", "Last timeliness check", "False inactive",
        "New launch?", "Requested launch date", "Website URL"]

pat = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_API_KEY")
if not pat:
    sys.exit("set AIRTABLE_PAT")
if len(sys.argv) < 2:
    sys.exit("pass one or more record ids")

now = datetime.now(timezone.utc)

for rid in sys.argv[1:]:
    url = "https://api.airtable.com/v0/%s/%s/%s" % (BASE, urllib.parse.quote(TABLE), rid)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + pat})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rec = json.load(r)
    except Exception as e:
        print("%s  FAILED: %s\n" % (rid, e))
        continue

    f = rec.get("fields", {})
    created = rec.get("createdTime")
    age = None
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age = (now - dt).days
        except ValueError:
            pass

    print("=== %s ===" % rid)
    print("  %-24s %s" % ("added to directory:",
                          "%s  (%s days ago)" % (created, age) if age is not None else created))
    for k in SHOW:
        v = f.get(k)
        print("  %-24s %s" % (k + ":", "(empty)" if v in (None, "", []) else v))

    # the two gates that decide whether this record should be scored at all
    excluded = []
    if f.get("Status") in ("Inactive", "N/A"):
        excluded.append("Status is %r — is_excluded() skips it" % f.get("Status"))
    if f.get("False inactive"):
        excluded.append("False inactive is ticked — a curator overruled the algorithm")
    launch_fired = bool(str(f.get("New launch?") or "").strip()) and age is not None and age <= 274
    print("  %-24s %s" % ("would the queue skip it:",
                          "; ".join(excluded) if excluded else "no, it is scoreable"))
    print("  %-24s %s" % ("recent-launch rule:",
                          "FIRES (floors at 60)" if launch_fired else "does not fire"))
    print()
