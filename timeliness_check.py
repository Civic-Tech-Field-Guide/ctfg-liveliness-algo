#!/usr/bin/env python3
"""
Timeliness checker for CTFG civic tech project listings.
Processes BATCH_SIZE projects per run, cycling through all 16k+ records over time.

Usage:
    export AIRTABLE_PAT=your_personal_access_token
    export GITHUB_TOKEN=your_github_token      # optional but strongly recommended
    export YOUTUBE_API_KEY=your_yt_api_key     # optional, enables YouTube last-video date
    python timeliness_check.py

Dependencies:
    pip install requests feedparser

Airtable fields written:
    - Liveliness score    (0–100 numeric)
    - Activity status     (Active / Likely Active / Possibly Inactive / Inactive / Unknown)
    - Last activity date  (most recent signal found)
    - Last timeliness check (today's date, used to advance the rolling queue)

Social recency is checked (most recent post date) for:
    YouTube (needs YOUTUBE_API_KEY), Bluesky, Medium, Reddit, Substack,
    Mastodon / fediverse (any /@username URL on a non-major-platform domain).
    Twitter/X, LinkedIn, Facebook, Instagram: URL-alive only (no post date).

Social links are sourced from the Airtable Links table and also auto-discovered
by scraping the project's homepage for known social media domains.
"""

import os
import sys
import re
import signal
import time
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

import requests

# Hard requirement, not an optional extra. Without feedparser every blog check
# returns None, so a project that posts weekly is scored as if it has no blog at
# all and that verdict is written to Airtable. A missing checker is a fact about
# the run, never a fact about the project, so refuse to run rather than warn and
# score anyway. Pinned in requirements.txt.
try:
    import feedparser
except ImportError:
    sys.exit(
        "Error: feedparser is not installed, so blog feed checks would silently\n"
        "return nothing and every feed-only project would be scored as inactive.\n"
        "Install it with: pip install -r requirements.txt\n"
        "On a PEP 668 system Python, use a venv:\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
        "  .venv/bin/python timeliness_check.py"
    )


# ── Config ────────────────────────────────────────────────────────────────────

AIRTABLE_PAT = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_API_KEY")
if not AIRTABLE_PAT:
    sys.exit("Error: set AIRTABLE_PAT environment variable")

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
BASE_ID       = "appYHxsLYleU2RVYk"
LISTINGS_TABLE = "tblELFP9tGX07UZDo"
LINKS_TABLE    = "tblpRr3lPFncgTS8y"
BATCH_SIZE     = 200

# ── Airtable field IDs — Listings ─────────────────────────────────────────────

F_NAME        = "fldc8kUYwodsQJvIy"  # Project name
F_WEBSITE     = "fldiaFhY8seaUpS6j"  # Website URL
F_GITHUB      = "fldp5H4hR4wDAc43i"  # Github URL
F_BLOG_1      = "fldd2Z5guupRDy19d"  # Blog feed URL
F_BLOG_2      = "fldsO2OQvw1mxqc6l"  # Blog feed (second field)
F_NEW_LAUNCH  = "fldDItqXavISnFFTy"  # New launch? (multilineText, "x" = new this year)
F_LAUNCH_DATE = "fldOymIEO9nuUItWv"  # Requested launch date
F_LINKS       = "flduR7cgq36S9SM4H"  # Linked records in Links table
F_TYPE        = "fld85Qsj7sU56liv9"  # Type (multipleSelects)
F_FORMATS     = "fldSJjJ4rbDBS8849"  # Formats (multipleRecordLinks → Format table)
F_CATEGORIES  = "fldXGB674po9h9xtB"  # Categories (multipleRecordLinks)

FORMAT_TABLE      = "tblDRByL15hmKr0sJ"
FORMAT_F_NAME     = "fldSJg1rpaHBme1FE"

CATEGORIES_TABLE  = "tblxtu4Bm8QCcuuek"
CATEGORY_F_SLUG   = "fldhK3tIIHmy0gq5B"  # Slug (singleLineText)

# Output fields
F_STATUS          = "fldw9vTztFwBOrcue"  # Status — existing field (Active / Inactive / N/A)
F_ACTIVITY_STATUS = "fld8cGHyyU0CffP2s"  # Activity status — full scale (created 2026-04-01)
F_LIVELINESS      = "fldbzhgtmZEjH7yaK"  # Liveliness score (created 2026-04-01)
F_LAST_ACTIVITY   = "fldeRTiBRsmhQJhxx"  # Last activity date (created 2026-04-01)
F_LAST_CHECK      = "fld8pjefvIqtFYxxO"  # Last timeliness check (created 2026-04-01)
F_BREAKDOWN       = "fldf1lqGIoBc7qMWF"  # Score breakdown (created 2026-09-08)

# Human correction of a wrong verdict. Curator sets Activity status to
# AS_SCORED_WRONG; the next run restores the record's own Status value and
# ticks F_FALSE_INACTIVE, which exempts it from all future scoring.
F_FALSE_INACTIVE  = "fldaMAxokw1rgdwO5"  # False inactive (checkbox)
AS_SCORED_WRONG   = "Claude scored wrong"
EXEMPT_SCORE      = 100  # score written to an exempted record

# What the public breakdown says once a curator has overruled the algorithm.
# It replaces the reasoning from the run that got it wrong, which would
# otherwise sit in Airtable forever: the False inactive checkbox stops the
# record ever being scored again, so nothing would come along to correct it.
CURATOR_BREAKDOWN = {
    "Active":   "A Field Guide curator reviewed this listing and confirmed the "
                "project is still going, so it is no longer scored automatically.",
    "Inactive": "A Field Guide curator reviewed this listing and recorded the "
                "project as inactive, so it is no longer scored automatically.",
    "blank":    "A Field Guide curator reviewed this listing, so it is no longer "
                "scored automatically.",
}

# ── Airtable field IDs — Links table ─────────────────────────────────────────

FL_URL     = "fldfJ5N0rECMxNiw5"  # Link URL
FL_LISTING = "fldQ9ByIfzzFqvJcy"  # Parent Listing (linked records)
FL_TYPE    = "fldZD5TbZ8P2cp39U"  # Link type (singleSelect)

# ── Airtable REST helpers ─────────────────────────────────────────────────────

AT_BASE    = f"https://api.airtable.com/v0/{BASE_ID}"
AT_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_PAT}",
    "Content-Type":  "application/json",
}


def at_get(table, params):
    params = {**params, "returnFieldsByFieldId": "true"}
    r = requests.get(f"{AT_BASE}/{table}", headers=AT_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def at_get_record(table, record_id):
    r = requests.get(f"{AT_BASE}/{table}/{record_id}", headers=AT_HEADERS,
                     params={"returnFieldsByFieldId": "true"}, timeout=10)
    if r.status_code == 200:
        return r.json()
    return None


def at_patch(table, records):
    r = requests.patch(
        f"{AT_BASE}/{table}",
        headers=AT_HEADERS,
        json={"records": records},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── Fetch batch ───────────────────────────────────────────────────────────────

SOURCE_FIELDS = [
    F_NAME, F_WEBSITE, F_GITHUB, F_BLOG_1, F_BLOG_2,
    F_NEW_LAUNCH, F_LAUNCH_DATE, F_LINKS,
    F_TYPE, F_FORMATS, F_CATEGORIES,
    F_STATUS, F_LAST_CHECK,
]

CURRENT_YEAR = str(datetime.now().year)

_format_name_cache = None  # {record_id: name}


def get_format_names():
    """Fetch Format table once per run and return a {record_id: name} map."""
    global _format_name_cache
    if _format_name_cache is not None:
        return _format_name_cache
    try:
        data = at_get(FORMAT_TABLE, {"pageSize": 100})
        _format_name_cache = {
            rec["id"]: (rec.get("fields", {}).get(FORMAT_F_NAME) or "").strip().lower()
            for rec in data.get("records", [])
        }
    except Exception as e:
        print(f"  Warning: could not fetch Format table: {e}", file=sys.stderr)
        _format_name_cache = {}
    return _format_name_cache


_category_slug_cache = None  # {record_id: slug}


def get_category_slugs():
    """Fetch Categories table once per run and return a {record_id: slug} map."""
    global _category_slug_cache
    if _category_slug_cache is not None:
        return _category_slug_cache
    try:
        data = at_get(CATEGORIES_TABLE, {"pageSize": 100})
        _category_slug_cache = {
            rec["id"]: (rec.get("fields", {}).get(CATEGORY_F_SLUG) or "").strip().lower()
            for rec in data.get("records", [])
        }
    except Exception as e:
        print(f"  Warning: could not fetch Categories table: {e}", file=sys.stderr)
        _category_slug_cache = {}
    return _category_slug_cache


def is_excluded(rec):
    """
    Returns True if the record should be skipped:
    - Already marked Inactive (Status field)
    - Curator has flagged a wrong verdict (False inactive)
    - Category is graveyard
    - New launch this year (marked 'x')
    - Type contains 'document'
    - Formats contains 'books'
    """
    f = rec.get("fields", {})

    # Already marked Inactive or N/A
    if f.get(F_STATUS) in ("Inactive", "N/A"):
        return True

    # A curator overruled the algorithm on this record. Leave it alone —
    # re-scoring it would just overwrite the correction again.
    if f.get(F_FALSE_INACTIVE):
        return True

    # Category is graveyard
    category_ids = f.get(F_CATEGORIES) or []
    category_slugs = get_category_slugs()
    if any(category_slugs.get(cid, "") == "graveyard" for cid in category_ids):
        return True

    # New launch this year
    new_launch  = (f.get(F_NEW_LAUNCH) or "").strip().lower()
    launch_date = (f.get(F_LAUNCH_DATE) or "")
    if new_launch == "x" and str(launch_date).startswith(CURRENT_YEAR):
        return True

    # Type = document
    type_values = f.get(F_TYPE) or []
    if any("document" in str(t).lower() for t in type_values):
        return True

    # Format = books
    format_ids    = f.get(F_FORMATS) or []
    format_names  = get_format_names()
    if any(format_names.get(fid, "") == "books" for fid in format_ids):
        return True

    # No categories assigned
    if not f.get(F_CATEGORIES):
        return True

    return False


# Pages of 100 to scan per pass before giving up. Roughly 40% of the never-checked
# pool is eligible, so filling a batch of 200 needs about 500 records scanned; the
# rest is headroom for a run that lands on a denser patch of ineligible records.
NEVER_CHECKED_MAX_PAGES = 12
CHECKED_MAX_PAGES       = 6


def fetch_batch():
    """
    Return the next BATCH_SIZE records to check.
    Priority: never-checked first (empty Last timeliness check), oldest-created first
              within that pool; then oldest-checked first.
    Excludes new launches from the current year.

    Eligibility is applied per page, before deciding whether more records are
    needed. Filtering only at the end deadlocks the queue: one full page of
    ineligible records counts as a full batch, so the top-up pass never runs
    and the batch ends up empty. The same page comes back the next day, so the
    stall is permanent once the head of the pool is ineligible.
    """
    eligible = []
    offset   = None

    # Pass 1: never checked — page through, oldest-created first within a page
    # (newer additions are more likely already active and less urgent to check)
    for _ in range(NEVER_CHECKED_MAX_PAGES):
        params = {
            "filterByFormula": f'{{{_field_name(F_LAST_CHECK)}}} = ""',
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset
        data = at_get(LISTINGS_TABLE, params)
        page = sorted(data.get("records", []), key=lambda r: r.get("createdTime", ""))
        eligible.extend(r for r in page if not is_excluded(r))
        offset = data.get("offset")
        if len(eligible) >= BATCH_SIZE or not offset:
            break

    # Pass 2: oldest checked (only if the never-checked pool came up short)
    if len(eligible) < BATCH_SIZE:
        # Airtable caps pageSize at 100 and rejects anything larger, so page
        # through rather than asking for a batch's worth in one call.
        offset2 = None
        for _ in range(CHECKED_MAX_PAGES):
            params2 = {
                "filterByFormula": f'{{{_field_name(F_LAST_CHECK)}}} != ""',
                "sort[0][field]": F_LAST_CHECK,
                "sort[0][direction]": "asc",
                "pageSize": 100,
            }
            if offset2:
                params2["offset"] = offset2
            data2 = at_get(LISTINGS_TABLE, params2)
            eligible.extend(r for r in data2.get("records", []) if not is_excluded(r))
            offset2 = data2.get("offset")
            if len(eligible) >= BATCH_SIZE or not offset2:
                break

    return eligible[:BATCH_SIZE]


def is_na_candidate(rec):
    """Returns True if record should be marked N/A (books format or document type)."""
    f = rec.get("fields", {})
    type_values = f.get(F_TYPE) or []
    if any("document" in str(t).lower() for t in type_values):
        return True
    format_ids = f.get(F_FORMATS) or []
    format_names = get_format_names()
    if any(format_names.get(fid, "") == "books" for fid in format_ids):
        return True
    return False


def restore_scored_wrong():
    """
    Undo wrong verdicts. A curator reports one by setting Activity status to
    AS_SCORED_WRONG; this restores the record's own Status value to Activity
    status and ticks False inactive so later runs leave the record alone.

    Status only carries Active / Inactive / N/A, so N/A and blank both restore
    to a blank Activity status rather than inventing a value.

    Returns a list of (name, restored_value, stale_score) for the run summary.
    """
    data = at_get(LISTINGS_TABLE, {
        "filterByFormula": f'{{{_field_name(F_ACTIVITY_STATUS)}}} = "{AS_SCORED_WRONG}"',
        "pageSize": 100,
    })
    records = data.get("records", [])
    if not records:
        return []

    restored = []
    updates  = []
    for rec in records:
        f       = rec.get("fields", {})
        status  = f.get(F_STATUS)
        restore = status if status in ("Active", "Inactive") else None
        updates.append({
            "id": rec["id"],
            "fields": {
                F_ACTIVITY_STATUS: restore,
                F_FALSE_INACTIVE:  True,
                F_LIVELINESS:      EXEMPT_SCORE,
                F_BREAKDOWN:       CURATOR_BREAKDOWN[restore or "blank"],
            },
        })
        restored.append((
            f.get(F_NAME, rec["id"]),
            restore or "(blank)",
            f.get(F_LIVELINESS),
        ))

    for i in range(0, len(updates), 10):
        at_patch(LISTINGS_TABLE, updates[i : i + 10])

    return restored


def fetch_na_candidates():
    """Return records with books format or document type that aren't yet marked N/A."""
    data = at_get(LISTINGS_TABLE, {
        "filterByFormula": (
            f'NOT(OR({{{_field_name(F_STATUS)}}} = "N/A",'
            f'       {{{_field_name(F_STATUS)}}} = "Inactive"))'
        ),
        "pageSize": 100,
    })
    return [r for r in data.get("records", []) if is_na_candidate(r)]


# Field name cache (Airtable formula filters use field names, not IDs)
_FIELD_NAME_MAP = {
    F_LAST_CHECK:      "Last timeliness check",
    F_STATUS:          "Status",
    F_ACTIVITY_STATUS: "Activity status",
    F_FALSE_INACTIVE:  "False inactive",
}

def _field_name(fid):
    return _FIELD_NAME_MAP.get(fid, fid)


# ── HTTP session ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "civictech.guide timeliness-checker/1.0 (+https://civictech.guide)"})

# Project URLs come from public submissions — cap how much of any body we
# download so one hostile/broken site can't hang or OOM the run.
MAX_FETCH_BYTES  = 1_000_000
FETCH_DEADLINE_S = 20


class _LimitedResponse:
    """Snapshot of a response with the body capped at MAX_FETCH_BYTES."""

    def __init__(self, r, body):
        self.status_code = r.status_code
        self.headers     = r.headers
        self.url         = r.url
        self.content     = body
        self._encoding   = r.encoding or "utf-8"

    @property
    def text(self):
        return self.content.decode(self._encoding, errors="replace")


def get_limited(url, timeout=10, headers=None):
    """GET that stops reading at MAX_FETCH_BYTES or FETCH_DEADLINE_S of
    wall-clock time, whichever comes first. Raises like requests.get.
    A server dripping bytes too slowly to ever fill a chunk can still hold
    the read open — the per-record SIGALRM budget is the backstop there."""
    r = SESSION.get(url, timeout=timeout, allow_redirects=True, stream=True,
                    headers=headers)
    start, size, chunks = time.monotonic(), 0, []
    try:
        for chunk in r.iter_content(chunk_size=8192):
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_FETCH_BYTES or time.monotonic() - start > FETCH_DEADLINE_S:
                break
    finally:
        r.close()
    return _LimitedResponse(r, b"".join(chunks))


# ── URL classification ────────────────────────────────────────────────────────

# Domains that are definitely not a project homepage
_ARTICLE_DOMAINS = {
    "techcrunch.com", "theverge.com", "wired.com", "bloomberg.com",
    "reuters.com", "theguardian.com", "nytimes.com", "washingtonpost.com",
    "forbes.com", "businessinsider.com", "politico.com", "npr.org",
    "bbc.com", "bbc.co.uk", "apnews.com", "axios.com", "vice.com",
    "fastcompany.com", "engadget.com", "gizmodo.com", "arstechnica.com",
    "theatlantic.com", "vox.com", "slate.com", "salon.com",
    "govtech.com", "statescoop.com", "fedscoop.com", "nextgov.com",
    "citylab.com", "smartcitiesdive.com", "govinsider.asia",
}

# Domains and patterns we should skip entirely (no useful signal)
_SKIP_DOMAINS = {"bing.com", "google.com"}

# Social media domains to look for when scanning a homepage for social links
_SOCIAL_DOMAINS = {
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "bsky.app", "medium.com", "reddit.com",
    "substack.com", "tiktok.com",
}


def resolve_duckduckgo(url):
    """
    Follow a DuckDuckGo !ducky (I'm Feeling Ducky) URL to its final destination.
    Returns the resolved URL, or None if resolution failed or stayed on DuckDuckGo.
    """
    try:
        r = get_limited(url)
        final = r.url
        if "duckduckgo.com" not in final:
            return final
    except Exception:
        pass
    return None


def classify_url(url):
    """
    Returns one of: "skip", "article", "homepage".
    "skip"     → placeholder / search engine URL, ignore completely.
    "article"  → known media domain; try to find real URL.
    "homepage" → treat as the project's actual website.
    Note: DuckDuckGo URLs are resolved before this is called.
    """
    if not url:
        return "skip"
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().lstrip("www.")

    if netloc in _SKIP_DOMAINS:
        return "skip"

    if netloc in _ARTICLE_DOMAINS:
        return "article"

    return "homepage"


# ── Homepage discovery from article pages ─────────────────────────────────────

from html.parser import HTMLParser as _HTMLParser

class _LinkExtractor(_HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []  # list of (href, anchor_text)
        self._current_anchor = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            self._current_anchor = attrs_dict.get("href", "")

    def handle_data(self, data):
        if self._current_anchor is not None:
            self.links.append((self._current_anchor, data.strip()))

    def handle_endtag(self, tag):
        if tag == "a":
            self._current_anchor = None


_SKIP_LINK_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "google.com", "apple.com", "amazon.com",
    "duckduckgo.com", "bing.com",
}
_SKIP_LINK_DOMAINS |= _ARTICLE_DOMAINS


def find_homepage_in_article(article_url):
    """
    Fetch a news article and look for external links that are likely the
    project's actual homepage. Returns a URL string or None.
    """
    try:
        r = get_limited(article_url, timeout=12)
        if r.status_code != 200:
            return None

        article_domain = urlparse(article_url).netloc.lower().lstrip("www.")
        parser = _LinkExtractor()
        parser.feed(r.text)

        candidates = []
        for href, text in parser.links:
            if not href.startswith("http"):
                continue
            parsed_link = urlparse(href)
            link_domain = parsed_link.netloc.lower().lstrip("www.")

            # Must be a different domain from the article
            if link_domain == article_domain:
                continue
            # Skip known non-project domains
            if link_domain in _SKIP_LINK_DOMAINS:
                continue
            # Skip very long paths (probably deep links, not homepages)
            path_depth = len([p for p in parsed_link.path.strip("/").split("/") if p])
            if path_depth > 3:
                continue

            # Prefer anchor text that suggests it's a project link
            text_lower = text.lower()
            priority = 0
            if any(w in text_lower for w in ("website", "site", "platform", "tool",
                                              "project", "learn more", "visit", "here")):
                priority = 2
            elif path_depth <= 1:
                priority = 1  # short path = likely homepage

            candidates.append((priority, href))

        # Sort by priority descending, then check if alive
        candidates.sort(key=lambda x: -x[0])
        seen = set()
        for _, href in candidates[:10]:
            domain = urlparse(href).netloc.lower()
            if domain in seen:
                continue
            seen.add(domain)
            if check_url_alive(href) is True:
                return href
    except Exception:
        pass
    return None


# ── Individual signal checks ──────────────────────────────────────────────────

# Hosts that serve snapshots rather than a project. Landing on one of these
# means the original did not answer, whatever status code comes back.
#
# This list is closed. Do not extend it with institutional archives, however
# archive-like the hostname reads. Membership requires a parseable snapshot URL
# grammar, because _extract_archive_original() must be able to recover the
# original and re-check it, and a host recognised here without that grammar
# lands in the recognised-but-unparseable state that rules a listing dead
# without a single fetch. Hostnames matching the intuition are also often live
# canonical homes for real items: arXiv, SSRN, Zenodo, archive.org/details.
# A host outside this list that redirects into it is already handled, by the
# effective-URL check in _try_fetch() rather than by name.
ARCHIVE_HOSTS = (
    "web.archive.org", "waybackmachine.org",
    "archive.today", "archive.ph", "archive.is",
    "archive.li", "archive.vn", "archive.md",
)


def is_archive_host(url):
    """True if the URL's host serves archive snapshots."""
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in ARCHIVE_HOSTS)


def _extract_archive_original(url):
    """
    Extract the original URL from an archive link, or None.

    web.archive.org/web/20180217125651/http://thesponge.eu/ -> http://thesponge.eu/
    archive.today/?url=http://example.org/                  -> http://example.org/
    archive.ph/newest/http://example.org/                   -> http://example.org/

    Every archive host recognised by is_archive_host() must be parseable here.
    A host recognised as an archive but not parsed returns None, and the caller
    then rules the listing dead and archived without ever fetching anything,
    which is a confident wrong answer rather than a miss.
    """
    m = re.search(r'(?:web\.archive\.org|waybackmachine\.org)/web/\d+[^/]*/(.+)', url)
    if m:
        return m.group(1)
    q = parse_qs(urlparse(url).query).get("url")
    if q and q[0]:
        return q[0]
    m = re.search(r'archive\.(?:today|ph|is|li|vn|md)/(?:newest/|\d+/)?(https?://.+)', url)
    if m:
        return m.group(1)
    return None


WEBSITE_RETRY_DELAY_S = 2


def _try_fetch(url, attempt=1):
    """
    Attempt a GET and return (is_alive, exception_type).

    A hard connection error is retried once before the site is called dead.
    Resets, DNS blips and handshakes refused to a datacenter IP are transient
    or environmental rather than evidence a site is gone, and a single one of
    them costs a live listing 50 points. Timeouts already return None, so they
    need no retry.
    """
    try:
        r = SESSION.get(url, timeout=10, allow_redirects=True, stream=True)
        r.close()
        # A dead site whose owner points the domain at a snapshot of itself
        # answers 200 from an archive host. Judge the effective URL, not the
        # requested one: thesponge.eu 302s to its own 2018 Wayback capture, so
        # reading the status code alone scores a tombstone as a live site.
        if is_archive_host(r.url) and not is_archive_host(url):
            return False, "redirected_to_archive"
        # Bot-blocking responses are indeterminate — don't treat as dead
        if r.status_code in (403, 429):
            return None, "blocked"
        return (200 <= r.status_code < 400), None
    except requests.exceptions.SSLError:
        return True, None   # broken SSL but site exists
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.TooManyRedirects:
        return None, "redirects"
    except Exception:
        if attempt == 1:
            time.sleep(WEBSITE_RETRY_DELAY_S)
            return _try_fetch(url, attempt=2)
        return False, "error"


def check_website(url):
    """
    Returns (is_alive: bool | None, is_archived: bool).
    For web archive URLs, tries the original URL first; only treats as
    archived/dead if the original URL also fails.
    None means we couldn't determine (timeout / network error).
    """
    if not url:
        return None, False

    is_archive_url = is_archive_host(url)

    if is_archive_url:
        original = _extract_archive_original(url)
        if original:
            print(f"    website  → archive URL, trying original: {original[:60]}")
            alive, _ = _try_fetch(original)
            if alive is True:
                return True, False   # original site is live — not archived
            if alive is None:
                return None, True    # couldn't determine
        # Original is down or couldn't be extracted — genuinely archived
        return False, True

    return _try_fetch(url)[0], False


# find_wayback_url() was removed with the Website URL replacement it fed. Picking
# a snapshot belongs to the curator's dead-link triage, which chooses the one
# nearest the project's last known activity rather than the newest (the newest is
# often already a parked-domain page) and rate-limits itself to Wayback's ~15
# requests/minute. Nothing in this scorer should reintroduce it.


class GitHubRateLimited(Exception):
    """
    GitHub refused a call because the rate limit is spent.

    Raised rather than returned so it cannot be mistaken for "this project has
    no repo". Nothing in the scoring path catches it; main() stops the run,
    leaving the current record and the rest of the batch unstamped so they come
    back round on the next run instead of being written up from a failed look.
    """

    def __init__(self, reset_at=None):
        self.reset_at = reset_at
        super().__init__(
            "GitHub rate limit exhausted"
            + (f", resets {reset_at:%H:%M:%S} UTC" if reset_at else "")
        )


def _gh_rate_limited(response):
    """
    True if this refusal is the rate limiter rather than an ordinary denial.

    A 403 also covers a private or blocked repo, which IS a real absence, so
    the status code alone is not enough. GitHub marks the primary limit with
    x-ratelimit-remaining: 0 and a secondary limit with retry-after.
    """
    if response.status_code not in (403, 429):
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "retry-after" in response.headers


def _gh_limit_reset(response):
    """When the limit lifts, as a datetime, or None if GitHub didn't say."""
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return datetime.fromtimestamp(int(reset), timezone.utc)
        except (ValueError, OverflowError):
            pass
    retry = response.headers.get("retry-after")
    if retry:
        try:
            return datetime.now(timezone.utc) + timedelta(seconds=int(retry))
        except ValueError:
            pass
    return None


def _gh_get(path, params=None):
    """
    GET a GitHub API path; returns parsed JSON, or None when there is genuinely
    nothing there. Raises GitHubRateLimited when the call was refused over the
    rate limit, which is a fact about the run and not about the project.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        r = requests.get(f"https://api.github.com{path}", headers=headers,
                         params=params or {}, timeout=10)
    except requests.RequestException:
        return None

    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            return None

    # A refusal over the rate limit is not an absence of data. Returning None
    # for both would score the project as having no GitHub activity at all,
    # write that to Airtable, and exit green: an active project can drop from
    # 100/Active to 25/Possibly Inactive purely on when the run hit the ceiling.
    if _gh_rate_limited(r):
        raise GitHubRateLimited(_gh_limit_reset(r))

    return None


FUTURE_GRACE_DAYS = 1


def reject_future(dt):
    """
    Drop a date more than FUTURE_GRACE_DAYS ahead of now.

    Commit dates, RSS pubDates and social post dates are all set by whoever
    published them, so any of the three can land in the future through a wrong
    clock or a deliberate stamp. recency_base_score floors age at 0 days, so a
    future date would score the maximum forever and would also be written to
    Airtable as the last activity date. The grace day absorbs clock skew.
    """
    if dt is None:
        return None
    if (dt - datetime.now(timezone.utc)).days >= FUTURE_GRACE_DAYS:
        return None
    return dt


def _gh_dt(s):
    """Parse a GitHub ISO-8601 timestamp into a timezone-aware datetime."""
    if not s:
        return None
    try:
        return reject_future(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


_GH_MAINTAINER_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}


def _gh_issue_activity(owner, repo):
    """
    Most recent *maintainer* activity on issues/PRs, or None.
    Outsiders filing or commenting on issues doesn't count — a dead repo
    still accumulates those. Counts only:
      - merged PRs (merging needs write access)
      - issues/PRs authored by the owner / org members / collaborators
      - an outsider's issue closed by someone other than its author
    """
    issues = _gh_get(f"/repos/{owner}/{repo}/issues",
                     {"state": "all", "sort": "updated", "direction": "desc", "per_page": 20})
    if not issues:
        return None

    dates = []
    outsider_closed = []  # closed, outsider-authored: self-closed or maintainer-closed?
    for it in issues:
        if (dt := _gh_dt((it.get("pull_request") or {}).get("merged_at"))):
            dates.append(dt)
        if it.get("author_association") in _GH_MAINTAINER_ROLES:
            for key in ("created_at", "closed_at"):
                if (dt := _gh_dt(it.get(key))):
                    dates.append(dt)
        elif (dt := _gh_dt(it.get("closed_at"))):
            outsider_closed.append((dt, it))

    # Check who closed the most recent outsider-authored issue, but only if
    # that could improve on what we already have (costs one extra API call).
    if outsider_closed:
        dt, it = max(outsider_closed, key=lambda pair: pair[0])
        if not dates or dt > max(dates):
            detail = _gh_get(f"/repos/{owner}/{repo}/issues/{it.get('number')}")
            closed_by = ((detail or {}).get("closed_by") or {}).get("login")
            author    = (it.get("user") or {}).get("login")
            if closed_by and closed_by != author:
                dates.append(dt)

    return max(dates) if dates else None


def _check_github_repo(owner, repo):
    """
    Gather activity signals for one repo. Returns a dict:
        best_date – most recent across all signals (or None)
        archived  – repo is archived (read-only)
        dates     – {signal: datetime} for push / release / commit / issue_pr
    or None if the repo can't be fetched.
    """
    info = _gh_get(f"/repos/{owner}/{repo}")
    if info is None:
        return None

    dates = {}
    if (dt := _gh_dt(info.get("pushed_at"))):
        dates["push"] = dt

    rel = _gh_get(f"/repos/{owner}/{repo}/releases/latest")
    if rel and (dt := _gh_dt(rel.get("published_at"))):
        dates["release"] = dt

    commits = _gh_get(f"/repos/{owner}/{repo}/commits", {"per_page": 1})
    if commits:
        commit_date = (commits[0].get("commit", {}).get("committer") or {}).get("date")
        if (dt := _gh_dt(commit_date)):
            dates["commit"] = dt

    # Issue/PR activity, restricted to signals of maintainer involvement
    if (dt := _gh_issue_activity(owner, repo)):
        dates["issue_pr"] = dt

    return {
        "best_date": max(dates.values()) if dates else None,
        "archived":  bool(info.get("archived")),
        "dates":     dates,
    }


def check_github(url):
    """
    GitHub activity signals: pushes, releases, commits, issue/PR activity,
    and archived status. Handles repo URLs (github.com/org/repo) and org/user
    profile URLs (checked via their most recently pushed repo).
    Returns the dict from _check_github_repo, or None.
    """
    if not url:
        return None

    parsed = urlparse(url)
    if "github.com" not in parsed.netloc:
        return None

    parts = [p for p in parsed.path.strip("/").split("/") if p]

    if len(parts) >= 2:
        return _check_github_repo(parts[0], re.sub(r"\.git$", "", parts[1]))

    if len(parts) == 1:
        owner = parts[0]
        for entity in ("orgs", "users"):
            repos = _gh_get(f"/{entity}/{owner}/repos",
                            {"sort": "pushed", "per_page": 1})
            if repos:
                name = repos[0].get("name")
                if name:
                    return _check_github_repo(owner, name)
                break

    return None


def check_blog_feed(url):
    """Returns most recent post date as a timezone-aware datetime, or None."""
    return _parse_feed_latest(url) if url else None


def check_url_alive(url):
    """Quick HEAD check. Returns True / False / None (error)."""
    if not url:
        return None
    try:
        r = SESSION.head(url, timeout=5, allow_redirects=True)
        return 200 <= r.status_code < 400
    except Exception:
        return None


def discover_feed_url(website_url):
    """
    Try to find an RSS/Atom feed URL for a site when none is explicitly stored.
    1. Fetch homepage and look for <link rel="alternate" type="application/rss+xml|atom+xml">
    2. Try common feed paths (/feed, /rss, /feed.xml, etc.)
    Returns a feed URL string or None.
    """
    if not website_url:
        return None
    parsed = urlparse(website_url)
    base   = f"{parsed.scheme}://{parsed.netloc}"

    # Step 1: autodiscovery link tag in homepage HTML
    try:
        r = get_limited(website_url)
        if r.status_code == 200:
            # Match <link ... type="application/rss+xml" ... href="..."> in either attribute order
            for pattern in (
                re.compile(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']', re.I),
                re.compile(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\']', re.I),
            ):
                m = pattern.search(r.text)
                if m:
                    href = m.group(1)
                    return href if href.startswith("http") else base + (href if href.startswith("/") else "/" + href)
    except Exception:
        pass

    # Step 2: try common feed paths
    for path in ("/feed", "/rss", "/feed.xml", "/atom.xml", "/rss.xml", "/blog/feed"):
        try:
            url = base + path
            r   = get_limited(url, timeout=8)
            ct  = r.headers.get("content-type", "")
            if r.status_code == 200 and any(s in ct for s in ("xml", "rss", "atom")):
                return url
        except Exception:
            continue

    # Step 3: feedparser fallback for /feed and /rss — handles platforms like
    # Substack where the page is JS-rendered (no autodiscovery link) and the
    # feed endpoint may not return an XML content-type header.
    for path in ("/feed", "/rss"):
        try:
            url = base + path
            r   = get_limited(url, timeout=8)
            if r.status_code == 200:
                feed = feedparser.parse(r.content)
                if feed.entries:
                    return url
        except Exception:
            continue

    return None


def discover_social_links(website_url):
    """
    Fetch website homepage and extract social media profile links.
    Returns a list of URL strings, one per domain (de-duplicated).
    """
    if not website_url:
        return []
    try:
        r = get_limited(website_url)
        if r.status_code != 200:
            return []
        parser = _LinkExtractor()
        parser.feed(r.text)
        found = {}
        for href, _ in parser.links:
            if not href.startswith("http"):
                continue
            netloc = urlparse(href).netloc.lower().lstrip("www.")
            if netloc in _SOCIAL_DOMAINS and netloc not in found:
                found[netloc] = href
        return list(found.values())
    except Exception:
        return []


def fetch_links_for_record(linked_ids):
    """
    Fetch URL and type from the Links table for the given linked record IDs.
    Returns list of {"url": str, "type": str}. Caps at 8 records to stay fast.
    """
    results = []
    for rid in linked_ids[:8]:
        rec = at_get_record(LINKS_TABLE, rid)
        if rec:
            f = rec.get("fields", {})
            link_url  = f.get(FL_URL)
            link_type = f.get(FL_TYPE, {})
            if isinstance(link_type, dict):
                link_type = link_type.get("name", "")
            if link_url:
                results.append({"url": link_url, "type": link_type or ""})
        time.sleep(0.1)
    return results


# ── Social recency checkers ───────────────────────────────────────────────────

def _parse_feed_latest(url, extra_headers=None):
    """Fetch a feed URL and return the most recent entry datetime, or None."""
    try:
        headers = dict(extra_headers) if extra_headers else None
        r = get_limited(url, headers=headers)
        if r.status_code != 200:
            return None
        feed   = feedparser.parse(r.content)
        latest = None
        for entry in feed.entries:
            for attr in ("published_parsed", "updated_parsed"):
                t = getattr(entry, attr, None)
                if t:
                    dt = datetime(*t[:6], tzinfo=timezone.utc)
                    if latest is None or dt > latest:
                        latest = dt
        # Fall back to channel-level date (lastBuildDate / updated) if no entry dates
        if latest is None:
            for attr in ("updated_parsed", "published_parsed"):
                t = getattr(feed.feed, attr, None)
                if t:
                    latest = datetime(*t[:6], tzinfo=timezone.utc)
                    break
        return latest
    except Exception:
        return None


def _check_youtube(parsed):
    """Return most recent video date from a YouTube channel, or None."""
    if not YOUTUBE_API_KEY:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None

    try:
        # Determine how to look up the channel
        if parts[0] == "channel" and len(parts) >= 2:
            chan_params = {"id": parts[1], "part": "contentDetails", "key": YOUTUBE_API_KEY}
        elif parts[0].startswith("@"):
            chan_params = {"forHandle": parts[0][1:], "part": "contentDetails", "key": YOUTUBE_API_KEY}
        elif parts[0] in ("c", "user") and len(parts) >= 2:
            chan_params = {"forUsername": parts[1], "part": "contentDetails", "key": YOUTUBE_API_KEY}
        else:
            return None

        r = requests.get("https://www.googleapis.com/youtube/v3/channels",
                         params=chan_params, timeout=10)
        if r.status_code != 200:
            return None
        items = r.json().get("items", [])
        if not items:
            return None

        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        r2 = requests.get("https://www.googleapis.com/youtube/v3/playlistItems",
                          params={"playlistId": uploads_id, "part": "snippet",
                                  "maxResults": 1, "key": YOUTUBE_API_KEY},
                          timeout=10)
        if r2.status_code != 200:
            return None
        items2 = r2.json().get("items", [])
        if not items2:
            return None
        pub = items2[0]["snippet"].get("publishedAt")
        if pub:
            return datetime.fromisoformat(pub.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _check_bluesky(path):
    """Return most recent post date from a Bluesky profile, or None."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "profile":
        return None
    actor = parts[1]
    try:
        r = requests.get(
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
            params={"actor": actor, "limit": 1},
            timeout=10,
        )
        if r.status_code == 200:
            feed = r.json().get("feed", [])
            if feed:
                indexed_at = feed[0].get("post", {}).get("indexedAt")
                if indexed_at:
                    return datetime.fromisoformat(indexed_at.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _check_medium(url, parsed):
    """Return most recent post date from a Medium profile/publication, or None."""
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if parts and parts[0].startswith("@"):
        feed_url = f"https://medium.com/feed/{parts[0]}"
    elif parsed.netloc != "medium.com":
        feed_url = f"{parsed.scheme}://{parsed.netloc}/feed"
    else:
        feed_url = f"https://medium.com/feed/{parsed.path.strip('/')}"
    return _parse_feed_latest(feed_url)


def _check_reddit(parsed):
    """Return most recent post date from a Reddit subreddit or user page, or None."""
    clean_path = parsed.path.rstrip("/")
    feed_url   = f"https://www.reddit.com{clean_path}.rss"
    return _parse_feed_latest(feed_url)


def _check_substack(parsed):
    """Return most recent post date from a Substack newsletter, or None."""
    feed_url = f"{parsed.scheme}://{parsed.netloc}/feed"
    return _parse_feed_latest(feed_url)


def _check_fediverse(url):
    """Return most recent post date from a Mastodon/fediverse profile, or None."""
    feed_url = url.rstrip("/") + ".rss"
    return _parse_feed_latest(feed_url)


# Domains where we know RSS / API isn't available (check alive only)
_NO_DATE_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "linkedin.com", "tiktok.com", "signal.org",
}

# Regex for fediverse-style /@username paths
_FEDIVERSE_PATH = re.compile(r"/+@[^/]+/?$")


def check_social_recency(url):
    """
    Try to retrieve the most recent post date from a social URL.
    Returns (datetime | None, platform_label: str | None).
    """
    if not url:
        return None, None

    parsed = urlparse(url)
    netloc = parsed.netloc.lower().lstrip("www.")

    if "youtube.com" in netloc or "youtu.be" in netloc:
        return _check_youtube(parsed), "YouTube"

    if "bsky.app" in netloc:
        return _check_bluesky(parsed.path), "Bluesky"

    if "medium.com" in netloc or netloc.endswith(".medium.com"):
        return _check_medium(url, parsed), "Medium"

    if "reddit.com" in netloc:
        return _check_reddit(parsed), "Reddit"

    if "substack.com" in netloc:
        return _check_substack(parsed), "Substack"

    # Fediverse: any /@username URL on a domain not in the known-no-date list
    if netloc not in _NO_DATE_DOMAINS and _FEDIVERSE_PATH.search(parsed.path):
        return _check_fediverse(url), "Fediverse"

    return None, None


# ── Scoring ───────────────────────────────────────────────────────────────────

# A social page that loads says nothing about whether anything was posted to it,
# so reachability is weak evidence and is capped to a nudge. At 10 points per
# link up to three it was worth +30, enough to carry a listing with no dated
# signal anywhere past a threshold: a live homepage and three loading social
# pages scored 45 and read Likely Active on nothing but pages existing. Posting
# recency is scored separately by social_recency_score(), which is unaffected.
SOCIAL_REACHABLE_BONUS_MAX = 10

# A live homepage is only evidence of current work alongside something dated and
# recent. A site that loads while the newest signal is over a year old earns the
# reduced bonus: enough to keep the listing off the dead-site penalty, not enough
# to carry stale work upward. Abre Alcaldias read 100/Active off a 698-day-old
# blog post plus a live site, because 55 + 15 lands exactly on the Active line.
WEBSITE_ALIVE_BONUS       = 15
WEBSITE_ALIVE_BONUS_STALE = 5
WEBSITE_BONUS_FRESH_DAYS  = 365


# Wording for the public score breakdown. The scorer keeps GitHub sub-signals
# apart because they mean different things to a reader: a release is a decision
# to ship, a push is only that somebody touched the repo.
GH_SIGNAL_LABELS = {
    "push":     "GitHub push",
    "release":  "GitHub release",
    "commit":   "GitHub commit",
    "issue_pr": "GitHub issue and pull request activity",
}


def describe_age(dt, now):
    """Age of a signal in words, for the breakdown shown on the profile page."""
    days = max(0, (now - dt).days)
    if days == 0:  return "today"
    if days == 1:  return "yesterday"
    if days < 60:  return f"{days} days ago"
    if days < 365: return f"{min(11, round(days / 30.44))} months ago"
    years = max(1, round(days / 365.25))
    return f"{years} year{'' if years == 1 else 's'} ago"


def _adjustment(label, nominal, applied):
    """
    One breakdown line, reporting the points actually applied rather than the
    points the rule nominally offers. The two differ whenever the 100 ceiling or
    the 0 floor bites, and a public breakdown whose figures do not add up to its
    own total is worse than no breakdown at all.
    """
    if applied == nominal:
        return f"{label} ({applied:+g})"
    limit = "above 100" if nominal > 0 else "below 0"
    if applied == 0:
        return f"{label} (no change, the score cannot go {limit})"
    return f"{label} ({applied:+g}, because the score cannot go {limit})"


def website_alive_bonus(best_date, now):
    """Points a reachable website earns, given the newest dated signal."""
    if best_date is None:
        return WEBSITE_ALIVE_BONUS_STALE
    if (now - best_date).days <= WEBSITE_BONUS_FRESH_DAYS:
        return WEBSITE_ALIVE_BONUS
    return WEBSITE_ALIVE_BONUS_STALE


def recency_base_score(dt, now):
    """
    Score 0–85 for GitHub / blog signals based on recency.
    Primary tunable parameter of the algorithm.
    """
    if dt is None:
        return 0
    age_days = max(0, (now - dt).days)
    if age_days <= 90:   return 85
    if age_days <= 180:  return 80
    if age_days <= 365:  return 70
    if age_days <= 730:  return 55
    if age_days <= 1095: return 35
    if age_days <= 1825: return 15
    return 5


def social_recency_score(dt, now):
    """
    Score 0–55 for social media signals. Same time brackets as recency_base_score
    but capped at 55 and zeroed beyond 1 year.
    """
    if dt is None:
        return 0
    age_days = max(0, (now - dt).days)
    if age_days <= 90:  return 55
    if age_days <= 180: return 50
    if age_days <= 365: return 45
    return 0  # beyond 1 year: no social score


def score_to_activity_status(score):
    """Full 5-value scale for the Activity status field."""
    if score >= 70: return "Active"
    if score >= 45: return "Likely Active"
    if score >= 20: return "Possibly Inactive"
    return "Inactive"


def score_to_status(score):
    """
    Definitive write to the existing Status field.
    Only returns a value when confidence is high; None = don't touch Status.
    """
    if score >= 70: return "Active"
    if score < 20:  return "Inactive"
    return None


# ── Main computation ──────────────────────────────────────────────────────────

def compute_liveliness(rec):
    """
    Runs all signal checks and returns:
      score (float), last_activity_date (ISO str | None), status (str)
    """
    fields = rec.get("fields", {})
    name   = fields.get(F_NAME, rec["id"])
    now    = datetime.now(timezone.utc)

    print(f"\n  [{name}]")

    # ── Website ──────────────────────────────────────────────────────────────
    raw_url = fields.get(F_WEBSITE)
    if raw_url and "duckduckgo.com" in raw_url and "!ducky" in raw_url:
        resolved = resolve_duckduckgo(raw_url)
        if resolved:
            print(f"    website  → resolved DDG URL to: {resolved[:70]}")
            raw_url = resolved
        else:
            print(f"    website  → DDG URL could not be resolved, skipping")
            raw_url = None
    url_class  = classify_url(raw_url)
    website_url = raw_url
    discovered_url = None

    if url_class == "skip":
        print(f"    website  → no usable URL ({(raw_url or 'none')[:60]})")
        website_alive, is_archived = None, False
    elif url_class == "article":
        print(f"    website  → article URL detected, searching for homepage…")
        discovered_url = find_homepage_in_article(raw_url)
        if discovered_url:
            print(f"    website  → found homepage: {discovered_url}")
            website_url = discovered_url
            website_alive, is_archived = check_website(discovered_url)
        else:
            print(f"    website  → could not find homepage from article")
            website_alive, is_archived = None, False
    else:
        website_alive, is_archived = check_website(raw_url)
        print(f"    website  → alive={website_alive}  archived={is_archived}")

    # A dead site is NOT looked up in the Wayback Machine and its Website URL is
    # never replaced with a snapshot. Swapping in an archive link is the
    # graveyard ruling from the dead-link triage codebook, and it is written as
    # one unit with the Graveyard category and Status: Inactive by a curator who
    # has first ruled out relink (the project moved and is live elsewhere, which
    # is the common case). A failed fetch cannot tell relink from graveyard, and
    # writing one third of the ruling leaves a record in a state the codebook
    # has no name for: an archive URL, no Graveyard tag, Status still Active.
    # A snapshot merely existing is also no evidence a project is dead.
    # is_archived stays set only by check_website, for a Website URL that a
    # curator already pointed at an archive and whose original no longer answers.

    # ── GitHub ───────────────────────────────────────────────────────────────
    gh = check_github(fields.get(F_GITHUB))
    github_date     = gh["best_date"] if gh else None
    github_archived = bool(gh and gh["archived"])
    if gh:
        detail = "  ".join(f"{k}={v.date()}" for k, v in sorted(gh["dates"].items()))
        flag   = "ARCHIVED  " if github_archived else ""
        print(f"    github   → {flag}{detail or 'no dated activity'}")
    else:
        print(f"    github   → no data")
    time.sleep(0.3)  # respect GitHub rate limits

    # ── Blog feeds ───────────────────────────────────────────────────────────
    blog_date = None
    explicit_feeds = list(filter(None, [fields.get(F_BLOG_1), fields.get(F_BLOG_2)]))
    if not explicit_feeds and website_url and url_class == "homepage":
        auto_feed = discover_feed_url(website_url)
        if auto_feed:
            print(f"    blog     → auto-discovered feed: {auto_feed}")
            explicit_feeds = [auto_feed]
    for feed_url in explicit_feeds:
        d = reject_future(check_blog_feed(feed_url))
        if d and (blog_date is None or d > blog_date):
            blog_date = d
    print(f"    blog     → latest post: {blog_date}")

    # ── Social links: recency + accessibility ────────────────────────────────
    linked_ids = [r if isinstance(r, str) else r.get("id") for r in (fields.get(F_LINKS) or [])]
    link_items = fetch_links_for_record(linked_ids) if linked_ids else []
    all_social_urls = [item["url"] for item in link_items]
    print(f"    links    → {len(link_items)} records fetched")

    # Auto-discover social links from the website homepage
    if website_url and url_class == "homepage":
        discovered_social = discover_social_links(website_url)
        if discovered_social:
            existing_domains = {urlparse(u).netloc.lower().lstrip("www.") for u in all_social_urls}
            added = []
            for su in discovered_social:
                domain = urlparse(su).netloc.lower().lstrip("www.")
                if domain not in existing_domains:
                    all_social_urls.append(su)
                    existing_domains.add(domain)
                    added.append(su)
            if added:
                print(f"    links    → auto-discovered {len(added)} social link(s) from homepage")

    accessible_count  = 0
    social_dated      = []  # list of (datetime, platform_label)

    for url in all_social_urls:
        alive = check_url_alive(url)
        if alive is True:
            accessible_count += 1
        dt, platform = check_social_recency(url)
        dt = reject_future(dt)
        if dt:
            social_dated.append((dt, platform))
            print(f"    social   → {platform}: last post {dt.date()}")

    # No cap on the count here: SOCIAL_REACHABLE_BONUS_MAX already caps what
    # reachability is worth, so capping the count too changed no score and only
    # made the breakdown under-report how many accounts were actually reached.

    # ── Combine all signals ───────────────────────────────────────────────────
    # Every dated signal becomes a (points, date, label) candidate. The score is
    # the single best of these and never their sum, which is exactly the part a
    # reader cannot infer from the final number, so the breakdown has to name
    # which signal won and show the others as also-rans.
    candidates = []

    if github_date:
        s = recency_base_score(github_date, now)
        if github_archived:
            s = min(s, 15)  # maintainers explicitly stopped work on the repo
        kind = next((k for k, v in gh["dates"].items() if v == github_date), None)
        candidates.append((s, github_date, GH_SIGNAL_LABELS.get(kind, "GitHub activity")))

    if blog_date:
        candidates.append((recency_base_score(blog_date, now), blog_date, "Blog post"))

    for dt, platform in social_dated:
        candidates.append((social_recency_score(dt, now), dt, f"{platform} post"))

    best_score = max((c[0] for c in candidates), default=0)
    best_date  = max((c[1] for c in candidates), default=None)

    score = float(best_score)

    # The public explanation, written to Airtable and shown on the profile page.
    # Built alongside the arithmetic rather than reconstructed afterwards: a 70
    # could be a 300-day-old commit or social posting plus a live site, and the
    # score on its own cannot tell those apart.
    why = []

    if candidates:
        winner = max(candidates, key=lambda c: (c[0], c[1]))
        why.append(f"Strongest signal: {winner[2]}, {describe_age(winner[1], now)} ({winner[0]})")
        others = sorted((c for c in candidates if c is not winner),
                        key=lambda c: c[0], reverse=True)
        if others:
            why.append("Also found: " + ", ".join(
                f"{c[2]} {describe_age(c[1], now)} ({c[0]})" for c in others))
        if github_archived:
            why.append("The GitHub repository is archived, so however recent its last "
                       "activity is, it counts for at most 15")
    else:
        why.append("No dated activity found in code, feeds or social posts (0)")

    # Website modifiers
    if is_archived:
        score = min(score, 10)      # almost certainly dead if pointing to web archive
        why.append("The listed address is an archive snapshot and the original no "
                   "longer answers (score capped at 10)")
    elif website_alive is True:
        bonus  = website_alive_bonus(best_date, now)
        before = score
        score  = min(score + bonus, 100)
        why.append(_adjustment(
            "Website is responding" if bonus == WEBSITE_ALIVE_BONUS
            else "Website is responding, but nothing dated and recent was found to go "
                 "with it",
            bonus, score - before))
    elif website_alive is False:
        before = score
        score  = max(score - 50, 0)  # strong signal of death
        why.append(_adjustment("Website did not respond", -50, score - before))
    elif url_class == "article":
        why.append("The listed address is an article about the project rather than the "
                   "project itself, and no homepage could be found from it")
    elif not raw_url:
        why.append("No website address on this listing to check")
    elif url_class == "skip":
        why.append("The listed address is not one that can be checked")
    else:
        why.append("Website could not be checked: it timed out or refused the request")

    # Social presence bonus, capped: a page that loads says nothing about whether
    # anything was ever posted to it.
    social_bonus = min(accessible_count * 10, SOCIAL_REACHABLE_BONUS_MAX)
    before = score
    score  = min(score + social_bonus, 100)
    if social_bonus:
        why.append(_adjustment(
            f"{accessible_count} social account"
            f"{'' if accessible_count == 1 else 's'} reachable",
            social_bonus, score - before))

    # Floor: website up but no dated signals → benefit of the doubt
    if best_date is None and website_alive is True and not is_archived:
        if score < 25:
            why.append("Nothing dated to go on, but the site is up, so the score is "
                       "given the benefit of the doubt (floor 25)")
        score = max(score, 25)

    # Fully unknown: nothing could be checked at all
    no_signals = (best_date is None and website_alive is None and accessible_count == 0)

    score  = round(score, 1)
    activity_status = "Unknown" if no_signals else score_to_activity_status(score)
    status          = None      if no_signals else score_to_status(score)

    if no_signals and is_archived:
        why = ["The address on this listing is an archive snapshot, and the original "
               "could not be reached to see whether the project is still there."]
    elif no_signals:
        why = ["Nothing on this listing could be checked: no working website address, "
               "no code repository, no feed and no reachable social accounts."]
    else:
        why.append(f"Total: {score:g} out of 100 - {activity_status}")
    breakdown = "\n".join(why)

    last_activity_str = best_date.strftime("%Y-%m-%d") if best_date else None
    print(f"    → score={score}  activity={activity_status}  status={status or '(no change)'}  last_activity={last_activity_str}")
    if discovered_url:
        print(f"    → discovered homepage: {discovered_url}  (original was article URL)")

    return {
        "score":              score,
        "last_activity_date": last_activity_str,
        "activity_status":    activity_status,
        "status":             status,
        "breakdown":          breakdown,
        "discovered_url":     discovered_url,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

# Hard wall-clock cap per record: even with capped downloads, a record with
# many slow URLs could otherwise eat the whole CI job.
RECORD_TIME_BUDGET_S = 120


class RecordTimeout(Exception):
    pass


def _record_timeout_handler(signum, frame):
    raise RecordTimeout()

def fetch_by_ids(record_ids):
    """
    Fetch specific records by ID (for targeted test runs).

    The list endpoint has no recordIds[] parameter and Airtable ignores query
    params it does not recognise, so asking that way returns the first page of
    the table: a run meant for two records scores and writes 100 unrelated
    listings instead. Fetch them one at a time.
    """
    records = []
    for rid in record_ids:
        rec = at_get_record(LISTINGS_TABLE, rid)
        if rec:
            records.append(rec)
        else:
            print(f"  Warning: record {rid} not found, skipping", file=sys.stderr)
    return records


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CTFG timeliness checker")
    parser.add_argument("--records", nargs="+", metavar="recXXX",
                        help="Specific record IDs to check (skips normal batch queue)")
    args = parser.parse_args()

    # Restore records a curator flagged as wrongly scored, before anything else
    restored = restore_scored_wrong()
    if restored:
        print(f"Restoring {len(restored)} wrongly scored record(s)...")
        for name, value, stale_score in restored:
            was = "no score" if stale_score is None else f"scored {stale_score}"
            print(f"  {name} → {value}, score {EXEMPT_SCORE} "
                  f"(was {was}; exempt from future checks)")
        print()

    # Mark books/document listings as N/A before the normal timeliness check
    na_records = fetch_na_candidates()
    if na_records:
        print(f"Marking {len(na_records)} books/document record(s) as N/A...")
        na_updates = [{"id": r["id"], "fields": {F_STATUS: "N/A"}} for r in na_records]
        for i in range(0, len(na_updates), 10):
            at_patch(LISTINGS_TABLE, na_updates[i : i + 10])
        print(f"  Done.\n")

    if args.records:
        print(f"Fetching {len(args.records)} specified records...")
        records = fetch_by_ids(args.records)
    else:
        print(f"Fetching next {BATCH_SIZE} projects to check...")
        records = fetch_batch()
    if not records:
        print("No eligible records found.")
        return

    print(f"Got {len(records)} records.\n")
    today        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updates      = []
    rate_limited = False

    signal.signal(signal.SIGALRM, _record_timeout_handler)

    for rec in records:
        signal.alarm(RECORD_TIME_BUDGET_S)
        try:
            result = compute_liveliness(rec)
        except RecordTimeout:
            name = rec.get("fields", {}).get(F_NAME, rec["id"])
            print(f"\n  [{name}] ✗ exceeded {RECORD_TIME_BUDGET_S}s budget — marking checked without a score")
            result = None
        except GitHubRateLimited as e:
            name = rec.get("fields", {}).get(F_NAME, rec["id"])
            print(f"\n  [{name}] ✗ {e} — stopping the run")
            print(f"    This record and the {len(records) - records.index(rec) - 1} after it "
                  f"are left unstamped and stay at the head of the queue.")
            if not GITHUB_TOKEN:
                print("    No GITHUB_TOKEN was set, so this run had 60 calls/hour "
                      "instead of 5000. Export one and rerun.")
            rate_limited = True
            break
        finally:
            signal.alarm(0)

        fields = {F_LAST_CHECK: today}
        if result:
            fields[F_LIVELINESS]      = result["score"]
            fields[F_ACTIVITY_STATUS] = result["activity_status"]
            fields[F_BREAKDOWN]       = result["breakdown"]
            if result["last_activity_date"]:
                fields[F_LAST_ACTIVITY] = result["last_activity_date"]
        else:
            # Timed out. Last timeliness check still advances so the record
            # cannot block the queue, which leaves the profile page saying it
            # was checked today. The old reasons were not among what was
            # checked today, so they go rather than stand under that date.
            fields[F_BREAKDOWN] = ""

        # Write each record as soon as it's done: a stalled or killed run keeps
        # its progress, and Last timeliness check always advances the queue
        # past a record that hangs.
        at_patch(LISTINGS_TABLE, [{"id": rec["id"], "fields": fields}])

        update = {"id": rec["id"], "fields": fields}
        update["_discovered_url"] = (result or {}).get("discovered_url")  # stored locally, not sent to Airtable
        updates.append(update)
        time.sleep(0.5)

    if rate_limited:
        print(f"\n✗ Stopped early — {len(updates)} record(s) written before the "
              f"GitHub rate limit was hit.\n")
    else:
        print(f"\n✓ Done — {len(updates)} record(s) written.\n")
    print(f"{'Record ID':<20} {'Score':>7}  {'Activity status':<20}  {'Status':<10}  Last activity")
    print("-" * 80)
    for u in updates:
        f       = u["fields"]
        disc    = u.get("_discovered_url") or ""
        suffix  = f"  → homepage: {disc}" if disc else ""
        print(
            f"{u['id']:<20} "
            f"{str(f.get(F_LIVELINESS, 'n/a')):>7}  "
            f"{f.get(F_ACTIVITY_STATUS, ''):<20}  "
            f"{f.get(F_STATUS, '(no change)'):<10}  "
            f"{f.get(F_LAST_ACTIVITY, 'n/a')}"
            + suffix
        )


if __name__ == "__main__":
    main()
