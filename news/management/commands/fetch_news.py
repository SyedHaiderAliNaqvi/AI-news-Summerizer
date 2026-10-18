from django.core.management.base import BaseCommand

from news.fetcher import save_articles_for_topic
from news.models import Topic


class Command(BaseCommand):
    help = "Fetch the latest news for every topic"

    def handle(self, *args, **options):
        for topic in Topic.objects.all():
            count = save_articles_for_topic(topic)
            self.stdout.write(f"{topic.name}: {count} new articles")