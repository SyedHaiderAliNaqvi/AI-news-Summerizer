import re
from datetime import datetime, timedelta, timezone

import requests

from .fetcher import fetch_articles
from .gnews import search_google_news
from .rss import keyword_in_text, search_feeds
from .sources import (
    DOMAINS, STOPWORDS, SYNONYMS, URDU_FEEDS, URDU_PHRASES, URDU_STOPWORDS,
    URDU_TO_ENGLISH, USE_GOOGLE_NEWS, USE_NEWSAPI
)
from .urdu import has_urdu, normalize

MAX_RESULTS = 30        # how many articles one search may return (it used to be 5)
NEWSAPI_MAX_DAYS = 28   # the free NewsAPI plan only covers roughly the last month

# The Urdu word lists, written the same way the question will be written
URDU_STOP = {normalize(word) for word in URDU_STOPWORDS}
URDU_MAP = {normalize(word): english for word, english in URDU_TO_ENGLISH.items()}
URDU_PHRASE_MAP = {normalize(phrase): english for phrase, english in URDU_PHRASES.items()}


def extract_keywords(question):
    """Reduce an English sentence to its important words. Can be empty (e.g. 'latest news')."""
    words = re.findall(r"\w+", question.lower())
    keywords = [w for w in words if len(w) >= 2 and w not in STOPWORDS]
    return keywords[:6]


def split_urdu_question(question):
    """For a question containing Urdu, return (urdu_words, english_words).

    The Urdu words search the Urdu feeds. The English words (Latin-script words the person
    typed, plus dictionary translations) search the English sources.
    """
    text = normalize(question)
    urdu_words, english_words = [], []

    def add_english(words):
        for word in words:
            if word not in english_words:
                english_words.append(word)

    for phrase, english in URDU_PHRASE_MAP.items():
        if phrase in text:
            add_english(english)

    for word in re.findall(r"\w+", text):
        if len(word) < 2 or word in STOPWORDS or word in URDU_STOP:
            continue
        if has_urdu(word):
            if word not in urdu_words:
                urdu_words.append(word)
            add_english(URDU_MAP.get(word, []))
        else:
            add_english([word])
    return urdu_words[:6], english_words[:6]


def newsapi_query(keywords, joiner):
    """Build a NewsAPI query, e.g. (ai OR "artificial intelligence") AND regulation."""
    terms = []
    for word in keywords:
        options = SYNONYMS.get(word)
        if options:
            quoted = [f'"{o}"' if " " in o else o for o in options]
            terms.append("(" + " OR ".join(quoted) + ")")
        else:
            terms.append(f'"{word}"' if " " in word else word)
    return f" {joiner} ".join(terms)


def search_newsapi(keywords, joiner, since):
    """Search NewsAPI (Pakistani outlets only). Any failure returns [] so RSS can still answer."""
    if not USE_NEWSAPI:
        return []

    from_date = None
    if since:
        oldest_allowed = datetime.now(timezone.utc) - timedelta(days=NEWSAPI_MAX_DAYS)
        from_date = max(since, oldest_allowed).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        return fetch_articles(
            newsapi_query(keywords, joiner),
            max_articles=MAX_RESULTS * 2,
            sort_by="relevancy" if keywords else "publishedAt",
            search_in="title,description" if keywords else None,
            domains=",".join(DOMAINS),
            from_date=from_date,
        )
    except KeyError:  # the NEWS_API_KEY variable is not set
        print("NewsAPI skipped: NEWS_API_KEY is not set")
        return []
    except requests.RequestException as error:
        print(f"NewsAPI problem: {error}")
        return []


def normalize_title(title):
    return re.sub(r"\W+", " ", title.lower()).strip()


def remove_duplicates(items):
    """Drop articles with the same link or the same headline."""
    seen_urls, seen_titles, unique = set(), set(), []
    for item in items:
        url = (item.get("url") or "").split("?")[0].rstrip("/")
        title = normalize_title(item.get("title") or "")
        if not url or url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        unique.append(item)
    return unique


def relevance_score(item, keywords):
    """Headline matches count double compared to description matches."""
    title = (item.get("title") or "").lower()
    description = (item.get("description") or "").lower()
    score = 0
    for keyword in keywords:
        if keyword_in_text(keyword, title):
            score += 2
        if keyword_in_text(keyword, description):
            score += 1
    return score


def published_time(item):
    try:
        return datetime.fromisoformat(item.get("publishedAt") or "")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def search_english_sources(keywords, since, days):
    """NewsAPI + English RSS + Google News. Strict search first, broader one if nothing is found."""
    joiners = ["AND", "OR"] if len(keywords) > 1 else ["AND"]
    items = []
    for joiner in joiners:
        items = list(search_newsapi(keywords, joiner, since))  # list() makes a safe copy
        items += search_feeds(keywords, since, require_all=(joiner == "AND"))
        if USE_GOOGLE_NEWS:
            items += search_google_news(keywords, joiner, since, days)
        items = remove_duplicates(items)
        if items:
            break
    return items


def search_urdu_sources(urdu_words, since):
    """Urdu RSS feeds only (NewsAPI and Google are used through the English translation)."""
    joiners = ["AND", "OR"] if len(urdu_words) > 1 else ["AND"]
    items = []
    for joiner in joiners:
        items = remove_duplicates(
            search_feeds(urdu_words, since, require_all=(joiner == "AND"), feeds=URDU_FEEDS)
        )
        if items:
            break
    return items


def search_news(question, days=7):
    """Find articles for a question written in English or Urdu; merge, de-duplicate, rank."""
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    urdu_question = has_urdu(question)
    if urdu_question:
        urdu_words, english_words = split_urdu_question(question)
    else:
        urdu_words, english_words = [], extract_keywords(question)

    # No search words at all means "just show me the latest headlines"
    headlines_mode = not urdu_words and not english_words

    items = []
    if urdu_words or (headlines_mode and urdu_question):
        items += search_urdu_sources(urdu_words, since)
    # An Urdu word with no English translation must not trigger an English headlines flood
    if english_words or headlines_mode:
        items += search_english_sources(english_words, since, days)
    items = remove_duplicates(items)

    words = urdu_words + english_words
    ranked = sorted(
        items,
        key=lambda item: (relevance_score(item, words), published_time(item)),
        reverse=True,
    )
    return ranked[:MAX_RESULTS]