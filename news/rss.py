import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from html import unescape

import feedparser
import requests
from django.core.cache import cache

from .sources import FEEDS, SYNONYMS
from .urdu import normalize

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
FEED_TIMEOUT = (5, 8)   # seconds to connect, seconds to wait for data (per feed)
FEED_DEADLINE = 8       # never wait longer than this for all feeds together
CACHE_SECONDS = 600     # reuse a downloaded feed for 10 minutes


def clean_html(text):
    """Remove HTML tags and extra spaces from feed text."""
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_entry(entry, feed_name):
    """Turn one feed entry into the same dictionary shape that NewsAPI uses."""
    published = ""
    if entry.get("published_parsed"):
        published = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc).isoformat()
    return {
        "title": clean_html(entry.get("title")),
        "url": entry.get("link") or "",
        "description": clean_html(entry.get("summary") or entry.get("description"))[:400],
        "content": "",
        "source": {"name": feed_name},
        "publishedAt": published,
    }


def fetch_feed(feed_name, url):
    """Download and parse one feed. Returns a list of articles ([] on any problem)."""
    cache_key = "rss:" + url
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = requests.get(url, headers=HEADERS, timeout=FEED_TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        entries = [parse_entry(entry, feed_name) for entry in parsed.entries]
    except Exception as error:
        print(f"RSS problem with {feed_name}: {error}")
        entries = []

    # Failures are remembered only briefly, so a feed that was down comes back soon
    cache.set(cache_key, entries, CACHE_SECONDS if entries else 60)
    return entries


def keyword_in_text(keyword, text):
    """True if the keyword (or a synonym) appears as a whole word or phrase."""
    text = normalize(text)
    for word in SYNONYMS.get(keyword, [keyword]):
        if re.search(r"\b" + re.escape(normalize(word)) + r"\b", text):
            return True
    return False


def is_recent(entry, since):
    if not entry["publishedAt"]:
        return False  # we can't prove it's recent
    return datetime.fromisoformat(entry["publishedAt"]) >= since


def search_feeds(keywords, since, require_all=True, feeds=None):
    """Search feeds (downloaded in parallel). No keywords means 'all recent headlines'."""
    feeds = FEEDS if feeds is None else feeds
    if not feeds:
        return []

    pool = ThreadPoolExecutor(max_workers=min(len(feeds), 20))
    futures = [pool.submit(fetch_feed, name, url) for name, url in feeds]
    wait(futures, timeout=FEED_DEADLINE)
    pool.shutdown(wait=False)  # slow feeds keep loading in the background and fill the cache

    matches = []
    for future in futures:
        entries = future.result() if future.done() and not future.exception() else []
        for entry in entries:
            if since and not is_recent(entry, since):
                continue
            text = (entry["title"] + " " + entry["description"]).lower()
            hits = [keyword_in_text(keyword, text) for keyword in keywords]
            if all(hits) if require_all else any(hits):
                matches.append(entry)
    return matches