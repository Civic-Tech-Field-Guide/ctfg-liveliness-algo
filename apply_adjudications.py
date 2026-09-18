#!/usr/bin/env python3
"""
What happens to a reading once adjudicate.mjs has made one.

    python apply_adjudications.py              # triage; reads, writes no record
    python apply_adjudications.py --commit     # write the decided readings back

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
or Matt sets each to one of accept / reject / n_a / graveyard, and --commit
writes the ones that write something. See DECISIONS below for what each means.

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
    LISTINGS_TABLE, at_patch, at_get_record,
    F_LIVELINESS, F_ACTIVITY_STATUS, F_STATUS, F_BREAKDOWN, F_CATEGORIES,
    F_POSTMORTEM,
    CLOSED_CAP, CLOSURE_SOURCE_READING, closure_sentence,
    ADJUDICATION_DIR, get_category_slugs,
)

VERDICTS_FILE  = os.path.join(ADJUDICATION_DIR, "verdicts.jsonl")
REVIEW_FILE    = os.path.join(ADJUDICATION_DIR, "review.jsonl")
COMMITTED_FILE = os.path.join(ADJUDICATION_DIR, "committed.jsonl")

# Four outcomes, because a reading of "finished" is not one thing. The model
# only ever says the page reads as finished; what that means for the listing is
# a curator's call, and three of the four calls below write something different.
#
#   accept     the project ran and has stopped. Cap the score, Status Inactive.
#   reject     the reading is wrong. Write nothing, ask no more.
#   n_a        timeliness does not apply to this record at all: a dataset with a
#              fixed span, an exhibition that ran for a week, a conference held
#              every year. Status N/A and nothing else. Capping the score here
#              would assert the thing went stale, which is a different and wrong
#              claim about a record that was never meant to stay current.
#   graveyard  the project is dead and the listing should say so. The dead-link
#              triage codebook writes that as one unit, so this does too:
#              Graveyard category, Status Inactive, capped score. A record left
#              with one third of it has a state the codebook has no name for.
DECISIONS = ("accept", "reject", "n_a", "graveyard")

# The decisions that reach Airtable. A rejection is a decision and is recorded
# as one, but it sends no request.
WRITES = ("accept", "n_a", "graveyard")


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


_graveyard_id = None


def graveyard_category_id():
    """The Categories record id whose slug is "graveyard", looked up once."""
    global _graveyard_id
    if _graveyard_id is None:
        for cid, slug in get_category_slugs().items():
            if slug == "graveyard":
                _graveyard_id = cid
                break
        else:
            raise SystemExit("No category with slug \"graveyard\" in the Categories "
                             "table, so a graveyard decision cannot be written.")
    return _graveyard_id


def fields_for(row, decision):
    """The Airtable fields one decision writes. Empty dict means write nothing."""
    if decision == "n_a":
        # Status alone. The score and the breakdown stay as the sweep left them,
        # because N/A is a statement about the record, not about the project.
        return {F_STATUS: "N/A"}

    fields = {
        F_LIVELINESS:      row["closed_score"],
        F_ACTIVITY_STATUS: row.get("closed_activity_status"),
        F_BREAKDOWN:       rebuild_breakdown(row),
    }
    if row.get("closed_status"):
        fields[F_STATUS] = row["closed_status"]

    if decision == "graveyard":
        live = at_get_record(LISTINGS_TABLE, row["id"]).get("fields", {})

        # Added to whatever categories the record already carries rather than
        # replacing them: F_CATEGORIES is a link field and a bare write would
        # drop every other category the record is filed under.
        current = live.get(F_CATEGORIES) or []
        gid = graveyard_category_id()
        fields[F_CATEGORIES] = current if gid in current else list(current) + [gid]
        fields[F_STATUS] = "Inactive"

        # Postmortem is never derived, only carried. It takes a substantive
        # account of why the project ended: who ran it, when it stopped, what it
        # achieved, what becomes of the work, where people should go now. A
        # shutdown banner, a redirect notice, a "this site has moved" page or an
        # archive snapshot of the homepage are none of those, and most dead
        # projects never write the real thing, so the field stays empty far more
        # often than it gets filled. Set "postmortem" on the review row by hand
        # when a page has genuinely earned it; leaving it out is the norm, not
        # an omission. Never overwrite a link a curator already chose.
        postmortem = (row.get("postmortem") or "").strip()
        if postmortem and not (live.get(F_POSTMORTEM) or "").strip():
            fields[F_POSTMORTEM] = postmortem

    return fields


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
    accepted = [r for r in rows if r.get("decision") in WRITES]

    print(f"{len(rulings)} ruling(s) read from {VERDICTS_FILE}.\n")
    print(f"{len(settled)} read as running or unclear. The sweep's score already stands for "
          f"those,\n  so nothing is written for them and they are done.\n")
    print(f"{len(finished)} read as finished. Those change a score and retire a listing, so "
          f"they wait\n  for a decision in {REVIEW_FILE} ({added} added this run, "
          f"{len(pending)} still undecided, {len(accepted)} decided and not yet written).\n")

    if pending:
        print("Undecided. Set \"decision\" on each to one of:\n")
        print("  accept     it ran and has stopped      → capped score, Status Inactive")
        print("  reject     the reading is wrong        → nothing written")
        print("  n_a        timeliness does not apply   → Status N/A, score untouched")
        print("  graveyard  dead, and say so            → Graveyard category, Inactive, capped\n")
        for r in pending:
            print(describe(r))
            print()
        print("accept and graveyard both write Status Inactive, and is_excluded() then skips that")
        print("record for good: nothing will ever re-score it, so nothing will correct a wrong one.")
        print("n_a is skipped for good too, and says the record was never a timeliness question.")
    if accepted:
        print(f"\n{len(accepted)} decided and waiting to be written. "
              f"Run: python apply_adjudications.py --commit")
    return 0


def commit():
    """Write the accepted readings to Airtable, one record at a time."""
    rows = read_jsonl(REVIEW_FILE)
    if not rows:
        print(f"No {REVIEW_FILE}. Run the triage pass first: python apply_adjudications.py")
        return 1

    decided = [r for r in rows if r.get("decision") in WRITES]
    if not decided:
        undecided = sum(1 for r in rows if r.get("decision") not in DECISIONS)
        print(f"Nothing to write in {REVIEW_FILE} "
              f"({undecided} undecided, {len(rows) - undecided} decided). Nothing written.")
        return 0

    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written, skipped = [], []

    for r in decided:
        decision = r["decision"]

        # A record with nothing checkable cannot be queued in the first place,
        # because queueing needs a website that answered, so this should not
        # happen. If it does, the reading has nothing to cap, and writing a
        # guessed score would be worse than leaving the record alone. N/A is
        # exempt: it writes no score, so it has none to be missing.
        if decision != "n_a" and r.get("closed_score") is None:
            skipped.append((r, "the run found no signal to cap, so there is no score to write"))
            continue

        fields = fields_for(r, decision)

        # One record per request, written as it is decided rather than batched:
        # an interrupted commit leaves a committed.jsonl that matches Airtable,
        # and the next run picks up from there instead of re-sending.
        at_patch(LISTINGS_TABLE, [{"id": r["id"], "fields": fields}])
        append_jsonl(COMMITTED_FILE, [{
            "id": r["id"], "name": r.get("name"), "url": r.get("url"),
            "committed": when, "model": r.get("model"), "decision": decision,
            "verdict": r["verdict"], "evidence": r.get("evidence"),
            "was": {"score": r.get("score"), "activity_status": r.get("activity_status"),
                    "status": r.get("status")},
            "wrote": {"score": fields.get(F_LIVELINESS),
                      "activity_status": fields.get(F_ACTIVITY_STATUS),
                      "status": fields.get(F_STATUS),
                      "graveyard": decision == "graveyard"},
        }])
        written.append(r)
        wrote_score = ("no score change" if F_LIVELINESS not in fields
                       else "%g" % fields[F_LIVELINESS])
        print(f"  {r['id']}  {r.get('name')} [{decision}] → {wrote_score}, "
              f"{fields.get(F_STATUS) or 'Status unchanged'}")

    # Drop what was written from the review file, and drop the rejections with
    # it: a rejection is a decision and does not need asking again. What stays
    # is whatever is still undecided, plus anything decided that could not be
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
