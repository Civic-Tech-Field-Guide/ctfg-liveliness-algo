#!/usr/bin/env python3
"""
What happens to a reading once adjudicate.mjs has made one.

    python apply_adjudications.py              # triage; reads, writes no record
    python apply_adjudications.py --commit     # write the accepted readings back

Two kinds of ruling come out of the local pass and they are not treated alike.

A "running" or "unclear" ruling changes nothing. The sweep already scored that
record as though no reading existed, and the reading agrees there is nothing to
add, so the score standing in Airtable is the right one. Those are resolved here
and no request is made for them: a write that sets a field to what it already
holds is not a no-op to anyone reading the record's history.

A "finished" ruling does change something, and what it changes is the one thing
in this system that cannot be taken back. It caps the score at CLOSED_CAP, which
puts Status at Inactive, and is_excluded() skips an Inactive record for good,
so nothing ever comes back to re-score it and a wrong one stays wrong with
nothing to correct it. That is a decision for a person. Those rulings are
written to adjudication/review.jsonl with decision: null, a Claude Code session
or Matt sets each to "accept" or "reject", and --commit writes only the accepted
ones.

    adjudication/verdicts.jsonl   in:  rulings from adjudicate.mjs
    adjudication/review.jsonl     out: the finished ones, awaiting a decision
    adjudication/committed.jsonl  out: what was actually sent to Airtable
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from timeliness_check import (
    LISTINGS_TABLE, at_patch,
    F_LIVELINESS, F_ACTIVITY_STATUS, F_STATUS, F_BREAKDOWN,
    CLOSED_CAP, CLOSURE_SOURCE_READING, closure_sentence,
    ADJUDICATION_DIR,
)

VERDICTS_FILE  = os.path.join(ADJUDICATION_DIR, "verdicts.jsonl")
REVIEW_FILE    = os.path.join(ADJUDICATION_DIR, "review.jsonl")
COMMITTED_FILE = os.path.join(ADJUDICATION_DIR, "committed.jsonl")

DECISIONS = ("accept", "reject")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_per_id(rows, key="ruled"):
    """The newest row per record id, so a re-run of the pass supersedes."""
    by = {}
    for row in rows:
        prev = by.get(row.get("id"))
        if not prev or str(row.get(key) or "") >= str(prev.get(key) or ""):
            by[row["id"]] = row
    return list(by.values())


def rebuild_breakdown(row):
    """
    The public breakdown this record would carry if the reading were accepted.

    Built from the reasons the sweep had already written when it reached the
    page, plus the two lines a closure decides: the closure itself, and the
    total. Not built by editing the breakdown that is in Airtable now, which
    may end with "Nothing dated was found ... Reported as Unknown rather than
    scored": a closure answers that rather than qualifying it.
    """
    why = list(row.get("reasons") or [])
    raw = row.get("raw_score")
    closed = row.get("closed_score")
    capped = ""
    if raw is not None and closed is not None and closed < raw:
        capped = " (capped at %g)" % CLOSED_CAP
    why.append(closure_sentence(CLOSURE_SOURCE_READING,
                                row.get("evidence") or row.get("reason") or "", capped))
    why.append("Total: %g out of 100 - %s" % (closed, row.get("closed_activity_status")))
    return "\n".join(why)


def describe(row):
    """One record's proposed change, as a short block for a person to read."""
    was = "no score" if row.get("score") is None else "%g" % row["score"]
    now = "no score" if row.get("closed_score") is None else "%g" % row["closed_score"]
    status_move = "%s → %s" % (row.get("status") or "(unset)",
                               row.get("closed_status") or "(unset)")
    return ("  %s  %s\n    %s\n    score %s → %s, activity %s → %s, Status %s\n"
            "    quoted: %s"
            % (row["id"], row.get("name") or "(unnamed)", row.get("url") or "",
               was, now,
               row.get("activity_status") or "(unset)",
               row.get("closed_activity_status") or "(unset)",
               status_move,
               row.get("evidence") or row.get("reason") or "(nothing quoted)"))


def triage():
    """Split the rulings, refresh the review file, and report. Writes no record."""
    rulings = latest_per_id(read_jsonl(VERDICTS_FILE))
    if not rulings:
        print(f"No rulings in {VERDICTS_FILE}. Run: node adjudicate.mjs rule")
        return 1

    finished = [r for r in rulings if r.get("verdict") == "finished"]
    settled  = [r for r in rulings if r.get("verdict") in ("running", "unclear")]

    # Already decided, by id, so a refresh never asks the same question twice or
    # discards a decision someone has already made.
    review = {r["id"]: r for r in read_jsonl(REVIEW_FILE)}
    committed = {r["id"] for r in read_jsonl(COMMITTED_FILE)}

    added = 0
    for r in finished:
        if r["id"] in committed:
            continue
        prior = review.get(r["id"])
        if prior and prior.get("decision") in DECISIONS:
            continue                        # a decision stands until it is committed
        row = dict(r, decision=None)
        row["would_write"] = {
            "score":           r.get("closed_score"),
            "activity_status": r.get("closed_activity_status"),
            "status":          r.get("closed_status"),
        }
        review[r["id"]] = row
        added += 1

    rows = list(review.values())
    write_jsonl(REVIEW_FILE, rows)

    pending = [r for r in rows if r.get("decision") not in DECISIONS]
    accepted = [r for r in rows if r.get("decision") == "accept"]

    print(f"{len(rulings)} ruling(s) read from {VERDICTS_FILE}.\n")
    print(f"{len(settled)} read as running or unclear. The sweep's score already stands for "
          f"those,\n  so nothing is written for them and they are done.\n")
    print(f"{len(finished)} read as finished. Those change a score and retire a listing, so "
          f"they wait\n  for a decision in {REVIEW_FILE} ({added} added this run, "
          f"{len(pending)} still undecided, {len(accepted)} accepted and not yet written).\n")

    if pending:
        print("Undecided. Set \"decision\" to \"accept\" or \"reject\" on each:\n")
        for r in pending:
            print(describe(r))
            print()
        print("Accepting one writes Status Inactive, and is_excluded() then skips that record")
        print("for good: nothing will ever re-score it, so nothing will correct a wrong one.")
    if accepted:
        print(f"\n{len(accepted)} accepted and waiting to be written. "
              f"Run: python apply_adjudications.py --commit")
    return 0


def commit():
    """Write the accepted readings to Airtable, one record at a time."""
    rows = read_jsonl(REVIEW_FILE)
    if not rows:
        print(f"No {REVIEW_FILE}. Run the triage pass first: python apply_adjudications.py")
        return 1

    accepted = [r for r in rows if r.get("decision") == "accept"]
    if not accepted:
        undecided = sum(1 for r in rows if r.get("decision") not in DECISIONS)
        print(f"Nothing accepted in {REVIEW_FILE} "
              f"({undecided} undecided, {len(rows) - undecided} decided). Nothing written.")
        return 0

    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written, skipped = [], []

    for r in accepted:
        # A record with nothing checkable cannot be queued in the first place,
        # because queueing needs a website that answered, so this should not
        # happen. If it does, the reading has nothing to cap, and writing a
        # guessed score would be worse than leaving the record alone.
        if r.get("closed_score") is None:
            skipped.append((r, "the run found no signal to cap, so there is no score to write"))
            continue

        fields = {
            F_LIVELINESS:      r["closed_score"],
            F_ACTIVITY_STATUS: r.get("closed_activity_status"),
            F_BREAKDOWN:       rebuild_breakdown(r),
        }
        if r.get("closed_status"):
            fields[F_STATUS] = r["closed_status"]

        # One record per request, written as it is decided rather than batched:
        # an interrupted commit leaves a committed.jsonl that matches Airtable,
        # and the next run picks up from there instead of re-sending.
        at_patch(LISTINGS_TABLE, [{"id": r["id"], "fields": fields}])
        append_jsonl(COMMITTED_FILE, [{
            "id": r["id"], "name": r.get("name"), "url": r.get("url"),
            "committed": when, "model": r.get("model"),
            "verdict": r["verdict"], "evidence": r.get("evidence"),
            "was": {"score": r.get("score"), "activity_status": r.get("activity_status"),
                    "status": r.get("status")},
            "wrote": {"score": r["closed_score"],
                      "activity_status": r.get("closed_activity_status"),
                      "status": r.get("closed_status")},
        }])
        written.append(r)
        print(f"  {r['id']}  {r.get('name')} → {r['closed_score']:g}, "
              f"{r.get('closed_status') or 'Status unchanged'}")

    # Drop what was written from the review file, and drop the rejections with
    # it: a rejection is a decision and does not need asking again. What stays
    # is whatever is still undecided, plus anything accepted that could not be
    # written, which still needs someone to look at it.
    held = {r["id"] for r, _ in skipped}
    keep = [r for r in rows if r.get("decision") not in DECISIONS or r["id"] in held]
    write_jsonl(REVIEW_FILE, keep)

    print(f"\n{len(written)} record(s) written and logged to {COMMITTED_FILE}.")
    rejected = sum(1 for r in rows if r.get("decision") == "reject")
    if rejected:
        print(f"{rejected} rejected reading(s) dropped without being written.")
    for r, reason in skipped:
        print(f"Left alone: {r['id']} {r.get('name')}: {reason}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Apply the readings adjudicate.mjs made of queued pages")
    parser.add_argument("--commit", action="store_true",
                        help="write the accepted readings to Airtable "
                             "(without it, nothing is sent)")
    args = parser.parse_args()
    sys.exit(commit() if args.commit else triage())


if __name__ == "__main__":
    main()
