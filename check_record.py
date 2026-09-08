#!/usr/bin/env python3
"""Timeliness check for a single CTFG listing record."""

import os
import sys
import requests
import re
import json
from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

TIMEOUT = 10
TODAY = date.today().isoformat()
CURRENT_YEAR = str(date.today().year)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CTFGBot/1.0; timeliness-check)"
}

results = {}

def log(label, value):
    print(f"  [{label}] {value}")
    results[label] = value

def head_check(url):
    """Return (alive, last_modified_date_str_or_None)."""
    if not url:
        return False, None
    try:
        r = requests.head(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)
        alive = r.status_code < 400
        lm = r.headers.get("last-modified")
        lm_date = None
        if lm:
            try:
                lm_date = parsedate_to_datetime(lm).date().isoformat()
            except Exception:
                pass
        if not alive:
            r2 = requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True, stream=True)
            alive = r2.status_code < 400
        return alive, lm_date
    except Exception as e:
        return False, None

def gh_api(path, params=None):
    """GET a GitHub API path; returns parsed JSON or None."""
    headers = {**HEADERS, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(f"https://api.github.com{path}", timeout=TIMEOUT,
                         headers=headers, params=params or {})
    except Exception:
        return None

    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            return None

    # Say so out loud. This script exists to explain a score, so reporting a
    # refused call as "no data" would hide the one fact that explains it.
    if r.status_code in (403, 429) and (
            r.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in r.headers):
        reset = r.headers.get("x-ratelimit-reset", "")
        when = ""
        if reset:
            try:
                when = " resets " + datetime.fromtimestamp(
                    int(reset), timezone.utc).strftime("%H:%M:%S UTC")
            except (ValueError, OverflowError):
                pass
        print(f"  ! GitHub rate limit hit on {path}{when} — "
              f"GitHub signals below are incomplete, not absent.",
              file=sys.stderr)

    return None

def github_check(github_url):
    """Return {"archived": bool, "dates": {signal: "YYYY-MM-DD"}} or None."""
    if not github_url:
        return None
    m = re.search(r"github\.com/([^/]+/[^/?\s#]+)", github_url)
    if not m:
        return None
    repo = m.group(1).rstrip("/")

    info = gh_api(f"/repos/{repo}")
    if info is None:
        return None

    dates = {}
    if info.get("pushed_at"):
        dates["push"] = info["pushed_at"][:10]

    rel = gh_api(f"/repos/{repo}/releases/latest")
    if rel and rel.get("published_at"):
        dates["release"] = rel["published_at"][:10]

    commits = gh_api(f"/repos/{repo}/commits", {"per_page": 1})
    if commits:
        commit_date = (commits[0].get("commit", {}).get("committer") or {}).get("date")
        if commit_date:
            dates["commit"] = commit_date[:10]

    # Issue/PR activity, restricted to signals of maintainer involvement:
    # merged PRs, maintainer-authored issues/PRs, and closures of an
    # outsider's issue by someone other than its author. Outsiders merely
    # filing issues doesn't count — a dead repo still accumulates those.
    issues = gh_api(f"/repos/{repo}/issues",
                    {"state": "all", "sort": "updated", "direction": "desc", "per_page": 20})
    maint_roles = {"OWNER", "MEMBER", "COLLABORATOR"}
    issue_dates = []
    outsider_closed = []
    for it in issues or []:
        merged = (it.get("pull_request") or {}).get("merged_at")
        if merged:
            issue_dates.append(merged)
        if it.get("author_association") in maint_roles:
            issue_dates += [d for d in (it.get("created_at"), it.get("closed_at")) if d]
        elif it.get("closed_at"):
            outsider_closed.append(it)
    if outsider_closed:
        top = max(outsider_closed, key=lambda i: i["closed_at"])
        if not issue_dates or top["closed_at"] > max(issue_dates):
            detail = gh_api(f"/repos/{repo}/issues/{top.get('number')}")
            closed_by = ((detail or {}).get("closed_by") or {}).get("login")
            author = (top.get("user") or {}).get("login")
            if closed_by and closed_by != author:
                issue_dates.append(top["closed_at"])
    if issue_dates:
        dates["issue_pr"] = max(issue_dates)[:10]

    return {"archived": bool(info.get("archived")), "dates": dates}

def parse_feed(url):
    """Return latest entry date string or None using basic XML parsing."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code >= 400:
            return None
        content = r.text
        # Try to find dates in RSS/Atom format
        # RSS: <pubDate>...</pubDate> or <lastBuildDate>...</lastBuildDate>
        # Atom: <updated>...</updated> or <published>...</published>
        date_patterns = [
            r"<pubDate>([^<]+)</pubDate>",
            r"<lastBuildDate>([^<]+)</lastBuildDate>",
            r"<updated>([^<]+)</updated>",
            r"<published>([^<]+)</published>",
            r"<dc:date>([^<]+)</dc:date>",
        ]
        found_dates = []
        for pat in date_patterns:
            for match in re.finditer(pat, content):
                raw = match.group(1).strip()
                # Try RFC 822 format
                try:
                    d = parsedate_to_datetime(raw).date()
                    found_dates.append(d)
                    continue
                except Exception:
                    pass
                # Try ISO 8601
                try:
                    d = datetime.fromisoformat(raw[:10]).date()
                    found_dates.append(d)
                    continue
                except Exception:
                    pass
        if found_dates:
            return max(found_dates).isoformat()
    except Exception:
        pass
    return None

def check_url_alive(url):
    """Return True if URL returns 200/301/302."""
    if not url:
        return False
    try:
        r = requests.head(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)
        return r.status_code < 400
    except Exception:
        return False

def youtube_latest_date(channel_url):
    """Scrape YouTube channel page for upload dates."""
    if not channel_url:
        return None
    try:
        r = requests.get(channel_url, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code >= 400:
            return None
        content = r.text
        # Look for dates in YouTube page (e.g., "2 days ago", or actual dates)
        # YouTube uses relative dates in the HTML, but sometimes has ISO dates in JSON-LD
        dates = re.findall(r'"publishedTimeText":\{"simpleText":"([^"]+)"\}', content)
        if not dates:
            # Try JSON-LD
            dates_iso = re.findall(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})"', content)
            if dates_iso:
                return max(dates_iso)
        # Can't easily parse relative dates without more context
        return None
    except Exception:
        return None

def bluesky_latest(url):
    """Try to get latest post date from Bluesky profile."""
    if not url:
        return None
    m = re.search(r"bsky\.app/profile/([^/?\s]+)", url)
    if not m:
        return None
    handle = m.group(1)
    try:
        r = requests.get(
            f"https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
            params={"actor": handle, "limit": 1},
            timeout=TIMEOUT, headers=HEADERS
        )
        if r.status_code == 200:
            feed = r.json().get("feed", [])
            if feed:
                created = feed[0].get("post", {}).get("record", {}).get("createdAt", "")
                if created:
                    return created[:10]
    except Exception:
        pass
    return None

def mastodon_latest(url):
    """Try to get latest post date from Mastodon profile."""
    if not url:
        return None
    # e.g. https://mastodon.social/@handle
    m = re.search(r"(https?://[^/]+)/@([^/?\s]+)", url)
    if not m:
        return None
    domain, handle = m.group(1), m.group(2)
    try:
        # Lookup account
        r = requests.get(f"{domain}/api/v1/accounts/lookup",
                         params={"acct": handle}, timeout=TIMEOUT, headers=HEADERS)
        if r.status_code == 200:
            acct_id = r.json().get("id")
            if acct_id:
                r2 = requests.get(f"{domain}/api/v1/accounts/{acct_id}/statuses",
                                   params={"limit": 1}, timeout=TIMEOUT, headers=HEADERS)
                if r2.status_code == 200:
                    statuses = r2.json()
                    if statuses:
                        created = statuses[0].get("created_at", "")
                        if created:
                            return created[:10]
    except Exception:
        pass
    return None

def reddit_latest(url):
    """Try to get latest post from Reddit user/subreddit."""
    if not url:
        return None
    try:
        api_url = url.rstrip("/") + ".json?limit=1"
        r = requests.get(api_url, timeout=TIMEOUT,
                         headers={**HEADERS, "Accept": "application/json"})
        if r.status_code == 200:
            data = r.json()
            # Try to find created_utc
            posts = []
            if isinstance(data, list):
                for section in data:
                    children = section.get("data", {}).get("children", [])
                    posts.extend(children)
            else:
                posts = data.get("data", {}).get("children", [])
            if posts:
                ts = posts[0].get("data", {}).get("created_utc")
                if ts:
                    return date.fromtimestamp(ts).isoformat()
    except Exception:
        pass
    return None

def substack_latest(url):
    """Try to get latest post from Substack feed."""
    if not url:
        return None
    feed_url = url.rstrip("/") + "/feed"
    return parse_feed(feed_url)

def medium_latest(url):
    """Try to get latest post from Medium feed."""
    if not url:
        return None
    m = re.search(r"medium\.com/@?([^/?\s]+)", url)
    if not m:
        return None
    handle = m.group(1)
    feed_url = f"https://medium.com/feed/@{handle}"
    return parse_feed(feed_url)

def score_from_days(days):
    if days is None:
        return 0
    if days <= 30:
        return 90
    if days <= 90:
        return 70
    if days <= 180:
        return 50
    if days <= 365:
        return 30
    return 10

def status_from_score(score):
    if score >= 70:
        return "Active"
    if score >= 40:
        return "Likely Active"
    if score >= 20:
        return "Possibly Inactive"
    if score >= 1:
        return "Inactive"
    return "Unknown"


# ── Record data ──────────────────────────────────────────────────────────────
RECORD_ID = "recFxkWBtXsEzesp1"
PROJECT_NAME = "Gauteng Department Education Online School Registration"
WEBSITE_URL = "http://web.archive.org/web/20220511151405/https://www.gdeadmissions.gov.za/"
GITHUB_URL = None
BLOG_FEED_1 = None
BLOG_FEED_2 = None
TWITTER_URL = None
FACEBOOK_URL = None
INSTAGRAM_URL = None
YOUTUBE_URL = None
LINKEDIN_URL = None
LINKED_RECORDS = []  # list of (url, type)

print(f"\n{'='*60}")
print(f"CTFG Timeliness Check — {TODAY}")
print(f"Record: {PROJECT_NAME}")
print(f"ID: {RECORD_ID}")
print(f"{'='*60}\n")

all_dates = []

# 1. Website check
print("1. Website check")
if WEBSITE_URL:
    alive, lm_date = head_check(WEBSITE_URL)
    log("website_alive", alive)
    log("website_last_modified", lm_date)
    if lm_date:
        all_dates.append(("website_last_modified", lm_date))
else:
    log("website", "no URL")

# 2. GitHub check
print("\n2. GitHub check")
if GITHUB_URL:
    gh = github_check(GITHUB_URL)
    if gh:
        log("github_archived", gh["archived"])
        for signal_name, d in sorted(gh["dates"].items()):
            log(f"github_{signal_name}", d)
            all_dates.append((f"github_{signal_name}", d))
    else:
        log("github", "unreachable")
else:
    log("github", "no URL")

# 3. Blog feeds
print("\n3. Blog feed checks")
for feed_url, label in [(BLOG_FEED_1, "blog_feed_1"), (BLOG_FEED_2, "blog_feed_2")]:
    if feed_url:
        latest = parse_feed(feed_url)
        log(label, latest)
        if latest:
            all_dates.append((label, latest))
    else:
        log(label, "no URL")

# 4. Linked records (social/other links)
print("\n4. Linked record checks")
if not LINKED_RECORDS:
    log("linked_records", "none")
else:
    for link_url, link_type in LINKED_RECORDS:
        lt = (link_type or "").lower()
        if "bluesky" in lt or "bsky" in link_url:
            d = bluesky_latest(link_url)
            log(f"bluesky({link_url[:50]})", d)
            if d:
                all_dates.append(("bluesky", d))
        elif "mastodon" in lt or "mastodon" in link_url:
            d = mastodon_latest(link_url)
            log(f"mastodon({link_url[:50]})", d)
            if d:
                all_dates.append(("mastodon", d))
        elif "reddit" in lt or "reddit" in link_url:
            d = reddit_latest(link_url)
            log(f"reddit({link_url[:50]})", d)
            if d:
                all_dates.append(("reddit", d))
        elif "substack" in lt or "substack" in link_url:
            d = substack_latest(link_url)
            log(f"substack({link_url[:50]})", d)
            if d:
                all_dates.append(("substack", d))
        elif "medium" in lt or "medium.com" in link_url:
            d = medium_latest(link_url)
            log(f"medium({link_url[:50]})", d)
            if d:
                all_dates.append(("medium", d))
        else:
            # Twitter, LinkedIn, Facebook, Instagram: just check alive
            alive = check_url_alive(link_url)
            log(f"{lt or 'link'}({link_url[:50]})", f"alive={alive}")

# 5. Direct social URLs (alive-only)
print("\n5. Social URL checks (alive only)")
for url, name in [(TWITTER_URL, "twitter"), (FACEBOOK_URL, "facebook"),
                   (INSTAGRAM_URL, "instagram"), (LINKEDIN_URL, "linkedin")]:
    if url:
        alive = check_url_alive(url)
        log(name, f"alive={alive}")
    else:
        log(name, "no URL")

# 6. YouTube
print("\n6. YouTube check")
if YOUTUBE_URL:
    yt_date = youtube_latest_date(YOUTUBE_URL)
    log("youtube_latest", yt_date)
    if yt_date:
        all_dates.append(("youtube", yt_date))
else:
    log("youtube", "no URL")

# ── Score calculation ────────────────────────────────────────────────────────
print("\n" + "="*60)
print("RESULTS")
print("="*60)

if all_dates:
    best_label, best_date_str = max(all_dates, key=lambda x: x[1])
    best_date = datetime.strptime(best_date_str, "%Y-%m-%d").date()
    days_ago = (date.today() - best_date).days
    score = score_from_days(days_ago)
    print(f"Most recent activity: {best_date_str} ({days_ago} days ago) via {best_label}")
else:
    best_date_str = None
    score = 0
    print("Most recent activity: None found")

status = status_from_score(score)

print(f"Liveliness score:     {score}")
print(f"Activity status:      {status}")
print(f"Last activity date:   {best_date_str}")
print(f"Last timeliness check: {TODAY}")

# Write final values as JSON for easy parsing
output = {
    "record_id": RECORD_ID,
    "project_name": PROJECT_NAME,
    "activity_status": status,
    "liveliness_score": score,
    "last_activity_date": best_date_str,
    "last_timeliness_check": TODAY,
}
print("\nJSON output:")
print(json.dumps(output, indent=2))
