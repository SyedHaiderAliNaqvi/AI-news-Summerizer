from django.db import models
from django.conf import settings


class Topic(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Article(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="articles")
    title = models.CharField(max_length=500)
    url = models.URLField(max_length=1000, unique=True)
    source = models.CharField(max_length=200, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    text = models.TextField(blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title


class Summary(models.Model):
    article = models.OneToOneField(Article, on_delete=models.CASCADE, related_name="summary")
    text = models.TextField()
    model_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Summary of {self.article.title}"


class Conversation(models.Model):
    session_key = models.CharField(max_length=40, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="conversations",
    )
    title = models.CharField(max_length=100, default="New chat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class Message(models.Model):
    ROLE_CHOICES = [("user", "User"), ("assistant", "Assistant")]

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    articles = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"

class Usage(models.Model):
    """One row per question asked. Used for fair-use limits that protect the free AI quota."""
    visitor = models.CharField(max_length=80, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)