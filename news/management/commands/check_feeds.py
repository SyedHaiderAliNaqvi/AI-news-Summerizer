from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand

from news.rss import fetch_feed
from news.sources import FEEDS, URDU_FEEDS

ALL_FEEDS = (
    [(name, url, "English") for name, url in FEEDS]
    + [(name, url, "Urdu") for name, url in URDU_FEEDS]
)


class Command(BaseCommand):
    help = "Test every RSS feed listed in sources.py (in parallel)"

    def handle(self, *args, **options):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(
                lambda feed: (feed[0], feed[2], fetch_feed(feed[0], feed[1])), ALL_FEEDS
            ))

        working = 0
        for name, language, entries in results:
            if entries:
                working += 1
                self.stdout.write(f"{name:<28} {language:<8} OK ({len(entries)} items)")
            else:
                self.stdout.write(f"{name:<28} {language:<8} FAILED or empty")
        self.stdout.write(f"\n{working} of {len(ALL_FEEDS)} feeds are working.")