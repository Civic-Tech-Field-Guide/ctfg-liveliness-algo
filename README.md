# ctfg-liveliness-algo

Works out whether a project listed on the [Civic Tech Field Guide](https://civictech.guide)
is still alive, by checking the signals its listing points at and writing a score back to
Airtable.

## How it runs

`.github/workflows/timeliness_check.yml` runs `timeliness_check.py` once a day on a
`0 9 * * *` cron, and can be triggered by hand from the Actions tab. GitHub delays scheduled
runs, so the real start time drifts a few hours past 09:00 UTC.

Each run handles `BATCH_SIZE` (200) records and works through the whole table over time. At
that size a daily run clears the 8,700 eligible records that have never been checked in about
six weeks. The queue is ordered by never-checked records first, oldest-created first within
that pool, then by oldest `Last timeliness check`. Every record is written to Airtable as
soon as it finishes, so a run that stalls or gets killed keeps its progress. A record that
takes longer than
`RECORD_TIME_BUDGET_S` (120s) is abandoned and stamped as checked so it cannot block the queue.

Secrets used: `AIRTABLE_PAT` (required), `GITHUB_TOKEN` and `YOUTUBE_API_KEY` (optional, both
raise the ceiling on what can be checked).

If GitHub refuses a call because the rate limit is spent, the run stops instead of scoring.
A refusal is not an absence: scoring through one would write "no code activity" for a project
that has plenty, and would exit green having done it. The record being checked and the rest of
the batch are left unstamped, so they stay at the head of the queue for the next run. A repo
now costs 4 to 6 calls, so a batch of 200 can need 1,200. With `GITHUB_TOKEN` set the ceiling is
5000 an hour; without it, 60, which one batch can exhaust on its own.

## Signals

| Source | What is checked |
| --- | --- |
| Website | Responds at all. 403 and 429 count as indeterminate, not dead, because they are usually bot blocking. A hard connection error is retried once before the site is called dead. An article URL is followed to find the project homepage. If the Website URL already points at an archive snapshot, the original URL is extracted and tried first, and the listing counts as live if the original answers. Liveness is judged by where a fetch lands, not by the status code alone: a domain whose owner has pointed it at a snapshot of itself answers 200 from an archive host, and does not count as live. |
| GitHub | Last push, latest release, last commit, maintainer activity on issues and PRs, how much of the recent issue traffic got resolved, and whether the repo is archived. A profile URL is resolved to that account's most recently pushed repo. |
| Blog | Latest entry in an RSS or Atom feed, either from the Airtable field or discovered on the homepage. |
| Social | Last post date for YouTube, Bluesky, Medium, Reddit, Substack and Mastodon. Twitter/X, LinkedIn, Facebook and Instagram are checked for reachability only, since neither exposes a post date. Links come from the Airtable Links table and from scraping the homepage. |
| The page itself | What the site says about its own age: schema.org `dateModified` and `datePublished`, the usual meta tags, `time` elements, and visible lines such as "Last updated: 20 March 2026". A footer copyright naming this year or last is read separately as weak evidence that the site is being kept up. A page that says the thing has closed is read as an ending rather than a date. |

Maintainer activity on issues counts merged PRs, anything opened by an owner, member or
collaborator, and an outsider's issue closed by somebody other than its author. An outsider
merely filing an issue does not count, because a dead repo keeps collecting those.

The resolution rate answers a different question from that date: not when the tracker was last
touched, but whether what came in is being dealt with. Of the 20 most recently updated issues
and PRs, it counts the ones updated in the last 180 days and how many of those were closed or
merged inside the same window. It is read from the request the issue dates already need, so it
costs no extra API calls. Below 5 items in the window there is too little traffic to read and
nothing is applied, which keeps a live two-person project with three issues a year from being
marked unattended for having nothing to close.

The window matters. An all-time ratio of closed to open issues never decays, so a repo that
closed 900 issues between 2015 and 2020 and nothing since still reads as well maintained, which
is the opposite of what this tool is for.

Until September 2026 the site was fetched only to see whether it answered, and the page
itself was thrown away. A project whose homepage plainly stated when it was last updated
produced no date at all, and scored as though nothing had been found. Across thirty sampled
listings, 52% of reachable sites state a date that can now be read.

The server's `Last-Modified` header is believed only when it is at least two days old. A page
built fresh for each request answers with the time of the request, so one listing reached the
top of the scale on a header that echoed the current second. A real file timestamp is days or
months old; a server clock never is.

About one reachable site in ten serves a shell to a plain fetch and paints its text afterwards
in the browser. Those are re-read through a headless browser, and only those. One listing's
page carried twenty-seven characters of static text, and the notice that it had stopped taking
reports existed only once its script had run. Where a page cannot be read even after that, the
listing is recorded as unread rather than as a page that said nothing, and its footer
copyright does not count: a shell's footer is not the project's.

Any date more than a day in the future is discarded. Commit dates, RSS pubDates and social
post dates are all set by whoever published them, so a wrong clock or a deliberate stamp can
otherwise make a stale project score full marks forever.

## Scoring

A GitHub or blog date sets a base score by age: 85 within 90 days, 80 within 180, 70 within a
year, 55 within eighteen months, 35 within three years, 15 within five, 5 beyond that. That
boundary sat at two years until September 2026, which meant a project whose last blog post was
in October 2024 still read as Likely Active most of the way through 2026. A date the page
states about itself uses the same shape capped at 70: under the 85 a commit earns, because a
content system stamps `dateModified` when a template changes and a hand-written "last updated"
line goes stale in place, and over the 55 a social post earns, because it is the project
talking about itself on its own site. Social dates use the same
brackets capped at 55 and drop to 0 past a year. An archived GitHub repo caps its own
contribution at 15, since the maintainers said in as many words that they stopped.

The best single date sets the base score, then the website adjusts it: a live site adds 15 when
the newest dated signal is within a year and 5 when it is older or absent, a dead one subtracts
50, and a listing a curator has already pointed at an archive snapshot, whose original URL no
longer answers, is capped at 10. A homepage that loads is evidence of current work only
alongside something dated and recent, so on its own it earns the reduced bonus. Reachable social links add 10 in total,
however many there are.

A live site with no dated signal at all used to get a floor of 25. That floor was never a
measurement, and 25 sits in the Possibly Inactive band, so a working site with no
machine-readable timestamp anywhere was published as possibly inactive on no evidence. In
September 2026 that was 64% of every scored listing. It now reports Unknown instead, and the
breakdown says nothing dated was found rather than implying the project was measured and found
wanting.

Three things count as evidence in that otherwise empty case, and each is a floor rather than a
bonus, so none of them stacks on top of stale evidence to lift an old project into Active:

- A footer copyright naming this year or last floors the score at 45. The site is being kept
  up even though nothing on it is dated.
- A listing added to the directory within the last nine months whose "New launch?" field is
  filled in floors at 60. It does not apply where the site failed to answer or the address is
  an archive snapshot: being added in March says nothing about a domain that stopped answering
  in August, and the archive cap exists for that reason and was being undone by the floor.
- A page that says the thing has closed caps the score at 10 instead, the same as an archive
  snapshot, and outranks everything above it including a recent launch. Something added in
  March and closed in July was both.

The issue tracker adjusts the score by 5 either way, and only at the ends of the range: closing
or merging at least half of the recent traffic adds 5, and closing none of it subtracts 5. A
backlog of old issues left open on its own is worth nothing in either direction, since a project
that triages carefully carries one and a project running a stale bot does not. The adjustment is
small because it overlaps the issue and PR date that may already have set the base score, and
because a stale bot closing everything untouched for 60 days inflates it. Telling a bot's
closure from a maintainer's costs one API call per issue, which a 200-record batch cannot
afford. An archived repo is skipped: nothing can be closed in one.

A social link counts for reachability only, and that is capped at 10 per listing because a page
that loads says nothing about whether anything was posted to it. Posting recency is scored
separately and is worth up to 55. Before the cap, three loading social pages were worth 30,
enough to lift a listing with no dated signal anywhere to 45 and report it as Likely Active.

| Score | Activity status |
| --- | --- |
| 70 and above | Active |
| 45 to 69 | Likely Active |
| 20 to 44 | Possibly Inactive |
| below 20 | Inactive |
| nothing checkable | Unknown |

## What gets written to Airtable

`Liveliness score`, `Activity status`, `Last activity date`, `Last timeliness check`,
`Score breakdown`, and `Status` where the reading is unambiguous.

`Score breakdown` is a plain-text account of how the score was reached, written on every run
and shown to the public on the project's profile page. It names the signal that set the base
score, lists the dated signals that lost to it, and gives each adjustment with its points:

```
Strongest signal: GitHub push, 8 months ago (70)
Also found: Bluesky post 30 days ago (55)
11 of the 20 issues and pull requests active in the last 6 months were closed or merged (+5)
Website is responding (+15)
1 social account reachable (+10)
Total: 100 out of 100 - Active
```

It is built as the score is calculated rather than reconstructed from the final number, which
cannot be done: 70 is a 300-day-old commit on one listing and social posting plus a live site
on another.

Each line carries the points actually applied rather than the points the rule nominally
offers, so the figures always add up to the total. The two differ when the 100 ceiling or the
0 floor bites, and the line says so: a listing already on 100 reads `no change, the score
cannot go above 100` against its social accounts, and a 35-point listing whose website is down
loses `-35, because the score cannot go below 0` rather than the full 50.

The `Status` field (Active / Inactive / N/A) was under human control alone until September
2026. The scoring pass had always computed a definitive verdict and thrown it away, on purpose,
until the scoring had been checked against enough edge cases to be trusted with it.

It is written now, but only at the ends of the range: `Active` at 70 and above, `Inactive`
below 20, and nothing at all in between. A listing reading Likely Active or Possibly Inactive
keeps whatever a curator gave it. That is what the score is for. Collapsing a middling reading
into a binary is exactly the judgement this tool should not be making on its own.

`Inactive` carries a further condition, because the word means the thing is dead or
unreachable rather than old. A great many listings are static resources, a guide or a dataset
or a reference site, unchanged for years and perfectly usable because they are still up. A low
score is the right reading of how much is happening there, and `Activity status` keeps
carrying it, but it is the wrong basis for declaring the thing gone. So `Inactive` is written
only where the listing cannot be reached, or where its own page says it has finished. A
project announcing that it has closed is the clearest signal there is and counts on its own,
however well the site still serves. Where the score is low and neither applies, the breakdown
says so on the profile rather than leaving an unexplained gap.

The asymmetry is deliberate. Writing `Active` is reversible. Writing `Inactive` is not: a
record whose Status is `Inactive` is skipped by the scoring queue from then on, so nothing
re-scores it and a wrong ruling has no later run to correct it.

Books and documents are still marked `N/A` at the start of each run, because a book does not
have activity to measure.

`Website URL` is read and never written. Replacing it with an archive.org snapshot is the
graveyard ruling from the dead-link triage codebook, and that ruling is written as one unit:
the snapshot as `Website URL`, the Graveyard category added alongside the categories the
listing already has, and `Status: Inactive`. It is only reached after relink has been ruled
out, meaning a curator has checked that the project has not simply moved somewhere still live,
which is the common case. A failed fetch cannot tell relink from graveyard. A scorer writing
just the URL leaves a record with an archive link, no Graveyard tag and `Status: Active`, which
is a state the codebook has no name for. This tool wrote that state until 2026-09-08; it no
longer does.

## Dead links are not triaged here

A dead link is four situations in the codebook and each gets a different ruling: skip a mirror
or fork, relink a project that moved, graveyard a project that is gone from everywhere, or
leave a link that is actually fine (a redirect, a slow host, a 403 from a bot check). This
tool scores recency and reachability. It does not choose between those four, and a low score
is not a graveyard ruling. Triage lives with the curator tooling.

## Records that are skipped

A record is passed over if `Status` is already Inactive or N/A, if it has no categories
assigned, if its category is graveyard, if it is a document or a book, if it launched this year
and is flagged as a new launch, or if it has been marked exempt by a curator.

## The profile page widget

`liveliness-embed-snippet.html` is the meter shown on each project's profile page: a gradient
strip from grey to green with a marker at the score, the last activity date, the breakdown
above, and the date of the last check. It is a single self-contained file pasted into a Softr
custom-code block or into the page's own header code, and this repo holds the source of truth
for it. See the comment at the top of the file for what the block has to expose.

Pasted into header code the browser leaves the widget at the top of the body, above every
block, so it moves itself under the first Item details block that appears and waits for one,
since blocks arrive after the script runs. A widget already sitting inside a block is left
where it is.

Three states it deliberately does not present as an ordinary score. A record with **False
inactive** ticked sits at 100 because a curator overruled the algorithm, not because it earned
it, so the widget says so instead of drawing a full bar. A record reporting Unknown shows
"Not enough to tell" with no number and no marker, and gives the reasons underneath: the
scorer clears the score on purpose in that case, so a missing score there is a verdict rather
than a gap. Only a record never checked at all renders nothing.

## Correcting a wrong verdict

The algorithm gets listings wrong, usually by calling a live site dead when a single request
failed. Two Airtable fields fix that, and the curator only needs the first one.

**To report a wrong score,** set the listing's `Activity status` to `Claude scored wrong`.

The next run picks it up and:

1. Writes the listing's own `Status` value back into `Activity status`, so it reads Active
   again. `Status` only holds Active, Inactive and N/A, so N/A and blank restore to a blank
   `Activity status` rather than inventing a value.
2. Sets `Liveliness score` to 100.
3. Ticks the **False inactive** checkbox.

That checkbox is what makes the correction stick. Any record carrying it is skipped by every
later run, so nothing overwrites the restored value. Without it the next day's run would
re-score the listing and write the same wrong verdict back. To put a listing back under the
algorithm, untick it.

Corrections are listed at the top of the run log:

```
Restoring 1 wrongly scored record(s)...
  Data.go.kr → Active, score 100 (was scored 0; exempt from future checks)
```

## Running it locally

`feedparser` is required, not optional. Without it every blog check returns nothing, so a
project that posts weekly is scored as having no blog and that verdict is written to Airtable.
The script refuses to start rather than scoring around a missing checker. On a PEP 668 system
Python it will not install globally, so use a venv:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python timeliness_check.py
```

```sh
pip install -r requirements.txt
export AIRTABLE_PAT=...        # required
export GITHUB_TOKEN=...        # optional, raises the GitHub rate limit
export YOUTUBE_API_KEY=...     # optional, enables YouTube post dates

python timeliness_check.py                       # next batch of 200
python timeliness_check.py --records recABC123   # named records only
```

Both write to Airtable. `check_record.py` is the diagnostic counterpart: it prints every
signal found for one listing and writes nothing, so it is the one to reach for when a score
looks wrong. It takes no arguments, so set `RECORD_ID` and the URLs in the block near the
bottom of the file before running it.

## Licence

MIT, see `LICENSE`.
