import os

import requests
import trafilatura
from django.utils.dateparse import parse_datetime

from .models import Article

API_URL = "https://newsapi.org/v2/everything"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsSummarizer/1.0)"}


def fetch_articles(query, max_articles=5, sort_by="publishedAt", search_in=None,
                   domains=None, from_date=None):
    """Ask NewsAPI for articles. An empty query means 'everything from these domains'."""
    params = {
        "language": "en",
        "sortBy": sort_by,
        "pageSize": max_articles,
    }
    if query:
        params["q"] = query
    if search_in:
        params["searchIn"] = search_in
    if domains:
        params["domains"] = domains    # e.g. "dawn.com,geo.tv" (only these outlets)
    if from_date:
        params["from"] = from_date     # e.g. "2026-10-04T00:00:00" (only newer articles)

    response = requests.get(
        API_URL,
        params=params,
        headers={"X-Api-Key": os.environ["NEWS_API_KEY"]},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("articles", [])


def extract_text(url):
    """Download a web page (with a short timeout) and return only the article text."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=(3, 5))
        response.raise_for_status()
    except requests.RequestException:
        return ""
    return trafilatura.extract(response.text[:300000]) or ""


def save_articles_for_topic(topic, max_articles=5):
    """Fetch articles for a topic and save the new ones. Returns how many were new."""
    new_count = 0
    for item in fetch_articles(topic.name, max_articles=max_articles):
        url = item.get("url")
        if not url or Article.objects.filter(url=url).exists():
            continue

        text = extract_text(url) or item.get("content") or item.get("description") or ""

        Article.objects.create(
            topic=topic,
            title=item.get("title") or "Untitled",
            url=url,
            source=(item.get("source") or {}).get("name", ""),
            published_at=parse_datetime(item.get("publishedAt") or ""),
            text=text,
        )
        new_count += 1
    return new_count