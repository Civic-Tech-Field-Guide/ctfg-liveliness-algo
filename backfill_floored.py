#!/usr/bin/env python3
"""
Re-score the listings still carrying the old floor of 25.

Those records were scored before the floor was replaced by an Unknown verdict,
so they keep a number that was never measured until the normal queue reaches
them. At BATCH_SIZE=200 a day against 16k+ records that is roughly eighty days,
hence this one-off pass.

Runs timeliness_check.py over the affected ids in chunks. Every id that finishes
is appended to a state file, so an interrupted run picks up where it stopped
rather than re-checking everything.

    AIRTABLE_PAT=...  GITHUB_TOKEN=$(gh auth token)  python3 backfill_floored.py

    --score N      which score to sweep (default 25)
    --redo-state   re-run the ids already in the state file and clear it, for
                   when the scorer changed after they were processed
    --chunk N      ids per timeliness_check invocation (default 25)
    --limit N      stop after N records, for a small trial run
    --dry-run      list what would be re-scored and exit without writing
    --state FILE   where to record finished ids (default .backfill-state)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = "appYHxsLYleU2RVYk"
TABLE = "Listings"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "timeliness_check.py")


def fetch_ids(pat, score):
    """Every record id whose Liveliness score is exactly `score`."""
    ids, offset = [], None
    while True:
        # The same gates fetch_batch() applies. --records bypasses is_excluded()
        # by design, so that a curator can force a re-check of one listing; run
        # in bulk without this filter it re-scores records the daily queue is
        # deliberately leaving alone and overwrites their Activity status.
        formula = ('AND({Liveliness score} = %s,'
                   ' {Status} != "Inactive", {Status} != "N/A",'
                   ' NOT({False inactive}))' % score)
        params = [("filterByFormula", formula),
                  ("pageSize", "100"),
                  ("fields[]", "Project name")]
        if offset:
            params.append(("offset", offset))
        url = "https://api.airtable.com/v0/%s/%s?%s" % (
            BASE, urllib.parse.quote(TABLE), urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + pat})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        for rec in data.get("records", []):
            ids.append((rec["id"], (rec.get("fields") or {}).get("Project name", "?")))
        offset = data.get("offset")
        if not offset:
            return ids


def scoreable(pat, ids):
    """Of these ids, the ones the daily queue would not skip."""
    keep = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        formula = ('AND(OR(%s), {Status} != "Inactive", {Status} != "N/A",'
                   ' NOT({False inactive}))'
                   % ",".join('RECORD_ID()="%s"' % r for r in chunk))
        params = [("filterByFormula", formula), ("pageSize", "100"),
                  ("fields[]", "Project name")]
        url = "https://api.airtable.com/v0/%s/%s?%s" % (
            BASE, urllib.parse.quote(TABLE), urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + pat})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        keep.extend((rec["id"], (rec.get("fields") or {}).get("Project name", "?"))
                    for rec in data.get("records", []))
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", type=float, default=25)
    ap.add_argument("--redo-state", action="store_true")
    ap.add_argument("--chunk", type=int, default=25)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", default=".backfill-state")
    args = ap.parse_args()

    pat = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_API_KEY")
    if not pat:
        sys.exit("set AIRTABLE_PAT")
    if not os.environ.get("GITHUB_TOKEN"):
        print("Warning: no GITHUB_TOKEN. GitHub allows 60 calls/hour without one and the\n"
              "         checker stops the run when it hits that. Try GITHUB_TOKEN=$(gh auth token).\n",
              file=sys.stderr)

    done = set()
    if os.path.exists(args.state):
        with open(args.state) as fh:
            done = {ln.strip() for ln in fh if ln.strip()}
        print("state file holds %d already-finished id(s)" % len(done))

    if args.redo_state:
        if not done:
            sys.exit("--redo-state needs a state file with ids in it")
        print("re-running %d id(s) from %s, minus any now excluded ..." % (len(done), args.state))
        found = scoreable(pat, sorted(done))
        os.rename(args.state, args.state + ".redone")
        done = set()
        print("state file moved aside to %s.redone" % args.state)
    else:
        print("fetching listings scored %g ..." % args.score)
        found = fetch_ids(pat, args.score)
    remaining = [(rid, name) for rid, name in found if rid not in done]
    todo = remaining[:args.limit] if args.limit else remaining

    print("scored %g: %d   already done: %d   still to do: %d   this run: %d\n"
          % (args.score, len(found), len(found) - len(remaining),
             len(remaining), len(todo)))

    if args.dry_run:
        for rid, name in todo[:40]:
            print("  %s  %s" % (rid, name[:60]))
        if len(todo) > 40:
            print("  ... and %d more" % (len(todo) - 40))
        return

    if not todo:
        print("nothing to do")
        return

    chunks = [todo[i:i + args.chunk] for i in range(0, len(todo), args.chunk)]
    started = time.time()
    for n, chunk in enumerate(chunks, 1):
        ids = [rid for rid, _ in chunk]
        elapsed = time.time() - started
        rate = elapsed / max(1, (n - 1) * args.chunk) if n > 1 else 0
        eta = ("  eta %.0f min" % ((len(todo) - (n - 1) * args.chunk) * rate / 60)) if rate else ""
        print("\n=== chunk %d/%d  (%d records)%s ===" % (n, len(chunks), len(ids), eta), flush=True)

        r = subprocess.run([sys.executable, SCRIPT, "--records"] + ids)
        if r.returncode != 0:
            print("\ntimeliness_check exited %d — stopping here. Finished ids are in %s, "
                  "so rerunning continues from this chunk." % (r.returncode, args.state),
                  file=sys.stderr)
            sys.exit(r.returncode)

        with open(args.state, "a") as fh:
            for rid in ids:
                fh.write(rid + "\n")

    print("\ndone: %d record(s) re-scored in %.0f min"
          % (len(todo), (time.time() - started) / 60))


if __name__ == "__main__":
    main()
