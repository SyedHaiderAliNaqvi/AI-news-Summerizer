from django.core.management.base import BaseCommand

from news.summarizer import summarize_pending_articles


class Command(BaseCommand):
    help = "Summarize articles that don't have a summary yet"

    def handle(self, *args, **options):
        count = summarize_pending_articles()
        self.stdout.write(f"Created {count} new summaries")