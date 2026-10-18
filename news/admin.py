from django.contrib import admin

from .models import Topic, Article, Summary, Conversation, Message


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("name",)


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "topic", "source", "published_at")
    list_filter = ("topic", "source")
    search_fields = ("title",)


@admin.register(Summary)
class SummaryAdmin(admin.ModelAdmin):
    list_display = ("article", "model_name", "created_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "created_at", "updated_at")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "role", "created_at")