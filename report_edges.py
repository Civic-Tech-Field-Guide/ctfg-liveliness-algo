#!/usr/bin/env python3
"""
Pull the interesting cases out of a backfill log, with a URL for each.

A sweep of a thousand listings is unreadable as a log. What is worth a human
look is the small set where the new scoring disagrees sharply with the old, or
where a score rests on something thin. Everything here is a question, not a
verdict: a jump from the old floor to 100 is usually a project that was always
alive, and occasionally a signal read too generously.

    python3 report_edges.py [backfill.log]
"""
import re
import sys
from datetime import date

LOG = sys.argv[1] if len(sys.argv) > 1 else "backfill.log"
TODAY = date.today()
PROFILE = "https://app.civictech.guide/p/?recordId=%s"

# "  [Some Project Name]" opens a record; "    → score=..." closes it.
NAME_RE = re.compile(r"^\s{2}\[(.+)\]\s*$")
DONE_RE = re.compile(r"^\s+→ score=(\S+)\s+activity=(.+?)\s+status=")
PAGE_RE = re.compile(r"^\s+page\s+→ (.+)$")
ROW_RE = re.compile(r"^\s*(rec[A-Za-z0-9]{14,})\s+(\S+)\s+(.+?)\s{2,}(.+?)\s{2,}(\S+)\s*$")


def age_days(s):
    try:
        return (TODAY - date.fromisoformat(s)).days
    except Exception:
        return None


records, names, pages = [], [], []
cur_name, cur_page = None, []
for line in open(LOG, errors="replace"):
    m = NAME_RE.match(line)
    if m:
        cur_name, cur_page = m.group(1), []
        continue
    m = PAGE_RE.match(line)
    if m and cur_name:
        cur_page.append(m.group(1).strip())
        continue
    if DONE_RE.match(line) and cur_name:
        names.append(cur_name)
        pages.append("; ".join(cur_page) or None)
        cur_name, cur_page = None, []
        continue
    m = ROW_RE.match(line)
    if m:
        rid, score, activity, _status, last = m.groups()
        records.append({"id": rid, "score": score, "activity": activity.strip(),
                        "last": last.strip()})

# The detail blocks and the summary rows are emitted in the same order, so they
# pair up positionally. Mismatched lengths mean an interrupted run; pair what
# lines up and leave the rest unnamed rather than mislabelling anything.
for i, r in enumerate(records):
    r["name"] = names[i] if i < len(names) else "?"
    r["page"] = pages[i] if i < len(pages) else None


def num(r):
    try:
        return float(r["score"])
    except (ValueError, TypeError):
        return None


buckets = {
    "Recovered — was floored at 25, now 70 or above": [],
    "Scored 60 on a stale or missing date — check the launch rule": [],
    "Cleared to Unknown — nothing datable found anywhere": [],
    "Scored on the page alone — no repo, feed or social signal": [],
}

for r in records:
    n, a = num(r), age_days(r["last"])
    if n is not None and n >= 70:
        buckets["Recovered — was floored at 25, now 70 or above"].append((r, a))
    if n is not None and abs(n - 60) < 0.01 and (a is None or a > 365):
        buckets["Scored 60 on a stale or missing date — check the launch rule"].append((r, a))
    if r["score"] == "None" or r["activity"] == "Unknown":
        buckets["Cleared to Unknown — nothing datable found anywhere"].append((r, a))
    if r["page"] and n is not None and n > 0:
        buckets["Scored on the page alone — no repo, feed or social signal"].append((r, a))

print("%d record(s) in %s\n" % (len(records), LOG))
for title, rows in buckets.items():
    print("=== %s: %d ===" % (title, len(rows)))
    for r, a in rows[:12]:
        age = "no date" if a is None else "%.1f yrs" % (a / 365.25)
        print("  %-42s %-6s %-16s %s" % (r["name"][:42], r["score"], r["activity"], age))
        if r["page"]:
            print("       via %s" % r["page"][:88])
        print("       %s" % (PROFILE % r["id"]))
    if len(rows) > 12:
        print("  ... and %d more" % (len(rows) - 12))
    print()
