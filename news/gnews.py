from urllib.parse import quote_plus

from .rss import fetch_feed, is_recent
from .sources import DOMAINS, GOOGLE_SITE_LIMIT, SYNONYMS

GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-PK&gl=PK&ceid=PK:en"


def term_for(keyword):
    """'ai' becomes (ai OR "artificial intelligence"); phrases are put in quotes."""
    options = SYNONYMS.get(keyword)
    if options:
        quoted = [f'"{o}"' if " " in o else o for o in options]
        return "(" + " OR ".join(quoted) + ")"
    return f'"{keyword}"' if " " in keyword else keyword


def google_news_query(keywords, joiner, days):
    terms = [term_for(k) for k in keywords]
    text = " ".join(terms) if joiner == "AND" else "(" + " OR ".join(terms) + ")"
    sites = " OR ".join(f"site:{domain}" for domain in DOMAINS[:GOOGLE_SITE_LIMIT])
    query = f"{text} ({sites})"
    if days:
        query += f" when:{days}d"
    return query


def google_news_url(keywords, joiner, days):
    return GOOGLE_NEWS_URL.format(query=quote_plus(google_news_query(keywords, joiner, days)))


def tidy(item):
    """Google titles look like 'Headline - Outlet'. Split them apart."""
    title = item["title"]
    if " - " in title:
        headline, outlet = title.rsplit(" - ", 1)
        headline = headline.strip()
        return dict(item, title=headline, description=headline,
                    source={"name": outlet.strip()})
    return dict(item, description=title)


def search_google_news(keywords, joiner, since, days):
    """Search Google News for the keywords, limited to the Pakistani outlets."""
    if not keywords:
        return []  # headlines mode is already covered by the direct feeds
    url = google_news_url(keywords, joiner, days)
    items = [tidy(item) for item in fetch_feed("Google News", url)]
    return [item for item in items if not since or is_recent(item, since)]