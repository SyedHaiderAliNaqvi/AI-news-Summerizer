import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from io import StringIO
from urllib.parse import urlparse

from django.core.management import call_command

from .gnews import google_news_query
from .sources import GOOGLE_SITE_LIMIT

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .chat import answer_question, ask_llm, build_sources, fetch_texts, parse_reply, DEEP_READ, resolve_language
from .fetcher import extract_text, fetch_articles
from .models import Article, Conversation, Message, Summary, Topic, Usage
from .rss import clean_html, fetch_feed, keyword_in_text, parse_entry, search_feeds
from .search import extract_keywords, newsapi_query, search_news, search_newsapi, MAX_RESULTS, split_urdu_question
from .sources import DEFAULT_PERIOD, DOMAINS, FEEDS, TIME_RANGES, URDU_FEEDS, URDU_STOPWORDS, URDU_TO_ENGLISH
from .summarizer import DailyQuotaExceeded, summarize_pending_articles
from .gnews import google_news_query, google_news_url, search_google_news, tidy
from .urdu import has_urdu, normalize

User = get_user_model()
PASSWORD = "S3cure-Pass-9876"

# ---------- shared sample data ----------

FAKE_RESULT = {
    "answer": "Test answer [1]",
    "articles": [{
        "id": 1, "title": "Headline", "source": "Example News",
        "url": "https://example.com/a", "published_at": "2026-10-09T10:00:00Z",
        "summary": "Short summary.",
    }],
}

SOURCES = [
    {"id": 1, "title": "First", "source": "A", "url": "https://example.com/1",
     "published_at": "2026-10-09T10:00:00Z", "snippet": "Snippet one.", "text": "Full text one."},
    {"id": 2, "title": "Second", "source": "B", "url": "https://example.com/2",
     "published_at": "", "snippet": "Snippet two.", "text": "Full text two."},
]
AI_REPLY = '{"answer": "Answer [1]", "articles": [{"id": 1, "summary": "AI summary."}]}'
NONE_RELEVANT = '{"answer": "Nothing relevant.", "articles": []}'

SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>AI rules arrive</title><link>https://example.com/1</link>
<description>New rules.</description>
<pubDate>Fri, 09 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

TEST_FEEDS = [("Test Feed", "https://example.com/feed")]


def make_item(title, url=None, days_ago=1, description=""):
    """Build an article dictionary in the shape NewsAPI and our RSS code use."""
    published = ""
    if days_ago is not None:
        published = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "title": title,
        "url": url or "https://example.com/" + title.replace(" ", "-"),
        "description": description, "content": "",
        "source": {"name": "Test Feed"}, "publishedAt": published,
    }


def post_chat(client, body):
    """Send a raw JSON string to the chat endpoint."""
    return client.post(reverse("chat_api"), data=body, content_type="application/json")


# ---------- configuration ----------

class ConfigTests(TestCase):
    """Catch typos in sources.py early."""

    def test_default_period_exists(self):
        self.assertIn(DEFAULT_PERIOD, TIME_RANGES)

    def test_news_domains_are_pakistani_outlets(self):
        self.assertIn("dawn.com", DOMAINS)

    def test_feeds_are_well_formed(self):
        self.assertTrue(FEEDS)
        for name, url in FEEDS:
            self.assertTrue(name)
            self.assertTrue(url.startswith("http"), name)

    def test_feed_names_and_addresses_are_unique(self):
        names = [name for name, url in FEEDS]
        urls = [url for name, url in FEEDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(urls), len(set(urls)))

    def test_domains_are_unique(self):
        self.assertEqual(len(DOMAINS), len(set(DOMAINS)))

    def test_every_feed_outlet_is_also_searched_through_newsapi(self):
        for name, url in FEEDS:
            host = urlparse(url).hostname
            self.assertTrue(
                any(host == d or host.endswith("." + d) for d in DOMAINS),
                f"{name} ({host}) is missing from DOMAINS",
            )

    def test_all_feeds_are_well_formed_and_unique(self):
        everything = FEEDS + URDU_FEEDS
        self.assertTrue(URDU_FEEDS)
        names = [name for name, url in everything]
        urls = [url for name, url in everything]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(urls), len(set(urls)))
        for name, url in everything:
            self.assertTrue(url.startswith("http"), name)

    def test_no_dictionary_word_is_also_a_stopword(self):
        stop = {normalize(word) for word in URDU_STOPWORDS}
        for word in URDU_TO_ENGLISH:
            self.assertNotIn(normalize(word), stop, word)


# ---------- older topic pages ----------

class NewsTests(TestCase):
    """Topic pages, refresh button, and the batch summarizer."""

    def setUp(self):
        self.topic = Topic.objects.create(name="Test Topic")
        self.with_summary = Article.objects.create(
            topic=self.topic, title="Has summary",
            url="https://example.com/1", source="Example", text="x" * 300,
        )
        Summary.objects.create(
            article=self.with_summary, text="Short summary.", model_name="test-model",
        )
        self.without_summary = Article.objects.create(
            topic=self.topic, title="No summary",
            url="https://example.com/2", source="Example", text="y" * 300,
        )

    def test_home_shows_chat_page(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "AI News Chat")

    def test_topic_page_shows_summary_and_source_link(self):
        response = self.client.get(reverse("topic_detail", args=[self.topic.id]))
        self.assertContains(response, "Short summary.")
        self.assertContains(response, "https://example.com/1")

    def test_topic_page_hides_articles_without_summary(self):
        response = self.client.get(reverse("topic_detail", args=[self.topic.id]))
        self.assertNotContains(response, "No summary")

    def test_unknown_topic_returns_404(self):
        response = self.client.get(reverse("topic_detail", args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_refresh_requires_post(self):
        response = self.client.get(reverse("refresh_topic", args=[self.topic.id]))
        self.assertEqual(response.status_code, 405)

    @patch("news.views.summarize_pending_articles", return_value=2)
    @patch("news.views.save_articles_for_topic", return_value=3)
    def test_refresh_shows_result_message(self, mock_fetch, mock_summarize):
        response = self.client.post(
            reverse("refresh_topic", args=[self.topic.id]), follow=True
        )
        self.assertContains(response, "Found 3 new articles and created 2 summaries.")

    @patch("news.summarizer.time.sleep")
    @patch("news.summarizer.summarize_text", return_value=("A summary.", "test-model"))
    def test_summarizer_saves_summary(self, mock_summarize, mock_sleep):
        count = summarize_pending_articles(limit=5, delay=0)
        self.assertEqual(count, 1)
        self.assertTrue(Summary.objects.filter(article=self.without_summary).exists())


# ---------- chat endpoint ----------

class ChatApiTests(TestCase):
    """POST /api/chat/ (the real AI pipeline is faked)."""

    def test_chat_requires_post(self):
        self.assertEqual(self.client.get(reverse("chat_api")).status_code, 405)

    def test_rejects_empty_message(self):
        self.assertEqual(post_chat(self.client, '{"message": ""}').status_code, 400)

    def test_rejects_invalid_json(self):
        self.assertEqual(post_chat(self.client, "this is not json").status_code, 400)

    def test_rejects_json_that_is_not_an_object(self):
        self.assertEqual(post_chat(self.client, "[1, 2, 3]").status_code, 400)

    def test_rejects_too_long_message(self):
        body = '{"message": "%s"}' % ("a" * 201)
        self.assertEqual(post_chat(self.client, body).status_code, 400)

    def test_rejects_non_integer_conversation_id(self):
        body = '{"message": "hello", "conversation_id": "abc"}'
        self.assertEqual(post_chat(self.client, body).status_code, 400)

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_creates_conversation_and_saves_both_messages(self, mock_answer):
        response = post_chat(self.client, '{"message": "ai news"}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["conversation"]["title"], "ai news")
        self.assertEqual(data["reply"]["articles"][0]["url"], "https://example.com/a")
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_follow_up_receives_history(self, mock_answer):
        first = post_chat(self.client, '{"message": "ai news"}').json()
        conversation_id = first["conversation"]["id"]
        post_chat(self.client, '{"message": "tell me more", "conversation_id": %d}' % conversation_id)

        history = mock_answer.call_args[0][1]  # second argument of the last call
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(Conversation.objects.count(), 1)  # same chat reused
        self.assertEqual(Message.objects.count(), 4)

    def test_cannot_post_into_someone_elses_conversation(self):
        other = Conversation.objects.create(session_key="someone-else", title="Not mine")
        body = '{"message": "hello", "conversation_id": %d}' % other.id
        self.assertEqual(post_chat(self.client, body).status_code, 404)

    @patch("news.views.answer_question", side_effect=DailyQuotaExceeded("limit"))
    def test_daily_quota_returns_429_and_saves_nothing(self, mock_answer):
        response = post_chat(self.client, '{"message": "ai news"}')
        self.assertEqual(response.status_code, 429)
        self.assertIn("daily", response.json()["error"].lower())
        self.assertEqual(Conversation.objects.count(), 0)

    @patch("news.views.answer_question", side_effect=Exception("secret internal detail"))
    def test_unexpected_error_returns_500_without_details(self, mock_answer):
        response = post_chat(self.client, '{"message": "ai news"}')
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret internal detail", response.json()["error"])
        self.assertEqual(Conversation.objects.count(), 0)


class ChatPeriodTests(TestCase):
    """The time-period option sent by the page."""

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_period_becomes_a_number_of_days(self, mock_answer):
        post_chat(self.client, '{"message": "ai", "period": "24h"}')
        self.assertEqual(mock_answer.call_args[0][2], 1)

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_default_period_is_used_when_missing(self, mock_answer):
        post_chat(self.client, '{"message": "ai"}')
        self.assertEqual(mock_answer.call_args[0][2], TIME_RANGES[DEFAULT_PERIOD]["days"])

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_any_time_means_no_day_limit(self, mock_answer):
        post_chat(self.client, '{"message": "ai", "period": "any"}')
        self.assertIsNone(mock_answer.call_args[0][2])

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_old_region_field_is_ignored(self, mock_answer):
        response = post_chat(self.client, '{"message": "ai", "region": "usa"}')
        self.assertEqual(response.status_code, 200)

    def test_rejects_unknown_period(self):
        response = post_chat(self.client, '{"message": "ai", "period": "forever"}')
        self.assertEqual(response.status_code, 400)

    def test_rejects_non_text_period(self):
        response = post_chat(self.client, '{"message": "ai", "period": ["x"]}')
        self.assertEqual(response.status_code, 400)

    def test_home_page_has_period_dropdown_and_no_region_dropdown(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Last 7 days")
        self.assertContains(response, "Pakistan")
        self.assertNotContains(response, "Worldwide")

    def test_home_page_lists_the_sources_covered(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Business Recorder")
        self.assertContains(response, "Sources covered")


# ---------- saved chats ----------

class ConversationApiTests(TestCase):
    """Listing, opening, and deleting saved chats."""

    def setUp(self):
        self.client.get(reverse("home"))  # creates a session for this visitor
        self.key = self.client.session.session_key
        self.mine = Conversation.objects.create(session_key=self.key, title="Mine")
        Message.objects.create(conversation=self.mine, role="user", content="Hello")
        Message.objects.create(
            conversation=self.mine, role="assistant", content="Hi there", articles=[]
        )
        self.other = Conversation.objects.create(session_key="someone-else", title="Not mine")

    def test_list_shows_only_my_conversations(self):
        response = self.client.get(reverse("conversation_list"))
        titles = [c["title"] for c in response.json()["conversations"]]
        self.assertEqual(titles, ["Mine"])

    def test_detail_returns_messages_in_order(self):
        response = self.client.get(reverse("conversation_detail", args=[self.mine.id]))
        roles = [m["role"] for m in response.json()["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_cannot_open_someone_elses_conversation(self):
        response = self.client.get(reverse("conversation_detail", args=[self.other.id]))
        self.assertEqual(response.status_code, 404)

    def test_delete_my_conversation(self):
        response = self.client.delete(reverse("conversation_detail", args=[self.mine.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Conversation.objects.filter(id=self.mine.id).exists())
        self.assertEqual(Message.objects.count(), 0)  # messages deleted with it

    def test_cannot_delete_someone_elses_conversation(self):
        response = self.client.delete(reverse("conversation_detail", args=[self.other.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Conversation.objects.filter(id=self.other.id).exists())

    def test_list_includes_last_updated_time(self):
        response = self.client.get(reverse("conversation_list"))
        self.assertIn("updated_at", response.json()["conversations"][0])


# ---------- accounts ----------

class AuthTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ali", password=PASSWORD)

    def login(self, client=None):
        return (client or self.client).post(
            reverse("login"), {"username": "ali", "password": PASSWORD}
        )

    def test_register_page_loads(self):
        response = self.client.get(reverse("register"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create account")

    def test_register_creates_user_and_signs_in(self):
        response = self.client.post(reverse("register"), {
            "username": "newuser", "password1": PASSWORD, "password2": PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username="newuser").exists())
        self.assertIn("_auth_user_id", self.client.session)

    def test_register_rejects_mismatched_passwords(self):
        response = self.client.post(reverse("register"), {
            "username": "newuser", "password1": PASSWORD, "password2": "different-Pass-1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_login_with_correct_password(self):
        self.assertEqual(self.login().status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_wrong_password(self):
        response = self.client.post(reverse("login"), {"username": "ali", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_requires_post(self):
        self.login()
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

    def test_logout_signs_you_out(self):
        self.login()
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_home_shows_sign_in_links_for_guests(self):
        self.assertContains(self.client.get(reverse("home")), "Sign in")

    def test_home_shows_username_when_signed_in(self):
        self.login()
        response = self.client.get(reverse("home"))
        self.assertContains(response, "ali")
        self.assertContains(response, "Log out")

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_chats_made_while_signed_in_belong_to_the_account(self, mock_answer):
        self.login()
        post_chat(self.client, '{"message": "ai news"}')
        self.assertEqual(Conversation.objects.get().user, self.user)

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_account_chats_follow_you_to_another_browser(self, mock_answer):
        self.login()
        post_chat(self.client, '{"message": "ai news"}')

        other_browser = Client()
        self.login(other_browser)
        response = other_browser.get(reverse("conversation_list"))
        titles = [c["title"] for c in response.json()["conversations"]]
        self.assertEqual(titles, ["ai news"])

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_guest_chats_move_into_the_account_on_sign_in(self, mock_answer):
        post_chat(self.client, '{"message": "guest question"}')
        self.assertIsNone(Conversation.objects.get().user)

        self.login()
        self.assertEqual(Conversation.objects.get().user, self.user)

    def test_users_cannot_see_each_others_chats(self):
        User.objects.create_user("sara", password=PASSWORD)
        chat = Conversation.objects.create(user=self.user, session_key="x", title="Private")

        sara = Client()
        sara.post(reverse("login"), {"username": "sara", "password": PASSWORD})
        self.assertEqual(sara.get(reverse("conversation_list")).json()["conversations"], [])
        detail = sara.get(reverse("conversation_detail", args=[chat.id]))
        self.assertEqual(detail.status_code, 404)

    def test_guests_cannot_see_account_chats(self):
        self.client.get(reverse("home"))
        guest_key = self.client.session.session_key
        Conversation.objects.create(user=self.user, session_key=guest_key, title="Private")
        response = self.client.get(reverse("conversation_list"))
        self.assertEqual(response.json()["conversations"], [])


# ---------- answering questions ----------

class ChatPipelineTests(TestCase):
    """Relevance filtering and generation logic in news/chat.py."""

    @patch("news.chat.ask_llm")
    @patch("news.chat.build_sources", return_value=[])
    def test_no_sources_skips_the_ai_call(self, mock_build, mock_ask):
        result = answer_question("something obscure")
        self.assertIn("couldn't find", result["answer"])
        self.assertEqual(result["articles"], [])
        mock_ask.assert_not_called()  # saves quota

    @patch("news.chat.ask_llm", return_value=AI_REPLY)
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_only_relevant_articles_become_cards(self, mock_build, mock_ask):
        result = answer_question("my question")
        self.assertEqual(result["answer"], "Answer [1]")
        self.assertEqual(len(result["articles"]), 1)  # source 2 was not marked relevant
        self.assertEqual(result["articles"][0]["summary"], "AI summary.")
        self.assertNotIn("text", result["articles"][0])  # full text stays on the server

    @patch("news.chat.ask_llm", return_value=NONE_RELEVANT)
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_no_relevant_articles_gives_friendly_message(self, mock_build, mock_ask):
        result = answer_question("economy")
        self.assertEqual(result["articles"], [])
        self.assertIn("couldn't find", result["answer"])

    @patch("news.chat.ask_llm", return_value=NONE_RELEVANT)
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_follow_up_keeps_the_model_answer(self, mock_build, mock_ask):
        history = [{"role": "user", "content": "earlier question"}]
        result = answer_question("and its effect?", history)
        self.assertEqual(result["answer"], "Nothing relevant.")
        self.assertEqual(result["articles"], [])

    @patch("news.chat.ask_llm", return_value="plain text, not json")
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_invalid_json_shows_all_articles_with_snippets(self, mock_build, mock_ask):
        result = answer_question("my question")
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(result["articles"][0]["summary"], "Snippet one.")

    @patch("news.chat.ask_llm", return_value=AI_REPLY)
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_prompt_contains_question_history_and_sources(self, mock_build, mock_ask):
        history = [{"role": "user", "content": "earlier question"}]
        answer_question("my question", history)
        prompt = mock_ask.call_args[0][0]
        self.assertIn("my question", prompt)
        self.assertIn("earlier question", prompt)
        self.assertIn("Full text one.", prompt)

    @patch("news.chat.build_sources", return_value=[])
    def test_answer_question_passes_days_along(self, mock_build):
        answer_question("q", None, 1)
        mock_build.assert_called_once_with("q", 1)

    # ----- parse_reply -----

    def test_parse_reply_reads_valid_json(self):
        answer, summaries = parse_reply(AI_REPLY)
        self.assertEqual(answer, "Answer [1]")
        self.assertEqual(summaries, {1: "AI summary."})

    def test_parse_reply_handles_code_fences(self):
        answer, summaries = parse_reply('```json\n{"answer": "Hi", "articles": []}\n```')
        self.assertEqual(answer, "Hi")
        self.assertEqual(summaries, {})

    def test_parse_reply_returns_none_summaries_for_plain_text(self):
        answer, summaries = parse_reply("just plain text")
        self.assertEqual(answer, "just plain text")
        self.assertIsNone(summaries)

    def test_parse_reply_ignores_malformed_article_entries(self):
        raw = '{"answer": "Hi", "articles": [{"id": "x"}, {"summary": "no id"}, {"id": 2, "summary": "ok"}]}'
        answer, summaries = parse_reply(raw)
        self.assertEqual(summaries, {2: "ok"})

    # ----- downloading article pages -----

    def test_fetch_texts_keeps_order(self):
        with patch("news.chat.extract_text", side_effect=lambda url: "text for " + url):
            texts = fetch_texts(["a", "b", "c"])
        self.assertEqual(texts, ["text for a", "text for b", "text for c"])

    def test_fetch_texts_survives_a_failing_page(self):
        def fake_extract(url):
            if "bad" in url:
                raise ValueError("boom")
            return "ok"

        with patch("news.chat.extract_text", side_effect=fake_extract):
            texts = fetch_texts(["https://good.com", "https://bad.com"])
        self.assertEqual(texts, ["ok", ""])

    def test_fetch_texts_with_no_urls(self):
        self.assertEqual(fetch_texts([]), [])

    @patch("news.chat.extract_text")
    def test_fetch_texts_skips_downloads_when_scraping_is_off(self, mock_extract):
        with patch("news.chat.SCRAPE_PAGES", False):
            texts = fetch_texts(["a", "b"])
        self.assertEqual(texts, ["", ""])
        mock_extract.assert_not_called()

    # ----- the AI call -----

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
    @patch("news.chat.genai.Client")
    def test_ask_llm_raises_on_daily_quota(self, mock_client):
        mock_client.return_value.models.generate_content.side_effect = Exception(
            "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )
        with self.assertRaises(DailyQuotaExceeded):
            ask_llm("prompt")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
    @patch("news.chat.time.sleep")
    @patch("news.chat.genai.Client")
    def test_ask_llm_retries_after_busy_error(self, mock_client, mock_sleep):
        good_response = MagicMock()
        good_response.text = "  ok  "
        mock_client.return_value.models.generate_content.side_effect = [
            Exception("503 UNAVAILABLE"),
            good_response,
        ]
        self.assertEqual(ask_llm("prompt"), "ok")
        mock_sleep.assert_called_once()


class BuildSourcesTests(TestCase):
    @patch("news.chat.extract_text", return_value="")
    @patch("news.chat.search_news")
    def test_filters_bad_articles(self, mock_search, mock_extract):
        mock_search.return_value = [
            {"title": "[Removed]", "url": "https://example.com/removed",
             "description": "d" * 100, "source": {"name": "A"}},
            {"title": "No link", "url": None, "description": "d" * 100, "source": {"name": "B"}},
            {"title": "Too short", "url": "https://example.com/short",
             "description": "tiny", "source": {"name": "C"}},
            {"title": "Good one", "url": "https://example.com/good",
             "description": "d" * 100, "source": {"name": "D"},
             "publishedAt": "2026-10-09T10:00:00Z"},
        ]
        sources = build_sources("anything", 7)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], 1)
        self.assertEqual(sources[0]["title"], "Good one")
        self.assertEqual(sources[0]["published_at"], "2026-10-09T10:00:00Z")

    @patch("news.chat.extract_text", return_value="")
    @patch("news.chat.search_news", return_value=[])
    def test_passes_days_to_search(self, mock_search, mock_extract):
        build_sources("ai", 1)
        mock_search.assert_called_once_with("ai", 1)


# ---------- RSS ----------

class RssTests(TestCase):
    def setUp(self):
        cache.clear()

    def entries(self):
        return [
            make_item("AI rules arrive", days_ago=1),
            make_item("AI old story", days_ago=20),
            make_item("Cricket results", days_ago=1),
            make_item("AI undated", days_ago=None),
        ]

    def test_clean_html(self):
        self.assertEqual(clean_html("<b>AI</b> &amp;  more"), "AI & more")

    def test_parse_entry_builds_newsapi_shape(self):
        entry = {
            "title": "<b>AI</b> news", "link": "https://x.com/a",
            "summary": "<p>Hello &amp; welcome</p>",
            "published_parsed": time.struct_time((2026, 10, 9, 10, 0, 0, 0, 0, 0)),
        }
        item = parse_entry(entry, "My Feed")
        self.assertEqual(item["title"], "AI news")
        self.assertEqual(item["description"], "Hello & welcome")
        self.assertEqual(item["source"]["name"], "My Feed")
        self.assertTrue(item["publishedAt"].startswith("2026-10-09T10:00:00"))

    def test_keyword_matches_whole_words_only(self):
        self.assertTrue(keyword_in_text("ai", "new ai rules"))
        self.assertFalse(keyword_in_text("ai", "he said it"))

    def test_keyword_synonym_matches(self):
        self.assertTrue(keyword_in_text("ai", "artificial intelligence boom"))

    @patch("news.rss.requests.get")
    def test_fetch_feed_parses_and_caches(self, mock_get):
        mock_get.return_value = MagicMock(content=SAMPLE_RSS)
        first = fetch_feed("Test Feed", "https://example.com/feed")
        second = fetch_feed("Test Feed", "https://example.com/feed")
        self.assertEqual(first[0]["title"], "AI rules arrive")
        self.assertEqual(first[0]["source"]["name"], "Test Feed")
        self.assertEqual(second, first)
        self.assertEqual(mock_get.call_count, 1)  # second call came from the cache

    @patch("news.rss.requests.get")
    def test_fetch_feed_sends_a_browser_like_user_agent(self, mock_get):
        mock_get.return_value = MagicMock(content=SAMPLE_RSS)
        fetch_feed("Test Feed", "https://example.com/feed")
        self.assertIn("Mozilla", mock_get.call_args[1]["headers"]["User-Agent"])

    @patch("news.rss.requests.get", side_effect=requests.RequestException("down"))
    def test_fetch_feed_returns_empty_list_on_failure(self, mock_get):
        self.assertEqual(fetch_feed("Test Feed", "https://example.com/feed"), [])

    def test_search_feeds_filters_by_keyword_and_date(self):
        since = datetime.now(timezone.utc) - timedelta(days=7)
        with patch("news.rss.FEEDS", TEST_FEEDS), \
                patch("news.rss.fetch_feed", return_value=self.entries()):
            found = search_feeds(["ai"], since)
        self.assertEqual([i["title"] for i in found], ["AI rules arrive"])

    def test_search_feeds_without_time_limit_keeps_old_and_undated(self):
        with patch("news.rss.FEEDS", TEST_FEEDS), \
                patch("news.rss.fetch_feed", return_value=self.entries()):
            found = search_feeds(["ai"], None)
        self.assertEqual(len(found), 3)

    def test_search_feeds_and_vs_or(self):
        with patch("news.rss.FEEDS", TEST_FEEDS), \
                patch("news.rss.fetch_feed", return_value=self.entries()):
            strict = search_feeds(["ai", "cricket"], None, require_all=True)
            broad = search_feeds(["ai", "cricket"], None, require_all=False)
        self.assertEqual(strict, [])
        self.assertEqual(len(broad), 4)

    def test_search_feeds_without_keywords_returns_all_recent_headlines(self):
        since = datetime.now(timezone.utc) - timedelta(days=7)
        with patch("news.rss.FEEDS", TEST_FEEDS), \
                patch("news.rss.fetch_feed", return_value=self.entries()):
            found = search_feeds([], since)
        titles = sorted(i["title"] for i in found)
        self.assertEqual(titles, ["AI rules arrive", "Cricket results"])

    def test_search_feeds_skips_a_feed_that_is_too_slow(self):
        two_feeds = [
            ("Slow", "https://slow.example.com"),
            ("Fast", "https://fast.example.com"),
        ]

        def fake_fetch(name, url):
            if name == "Slow":
                time.sleep(0.5)
            return [make_item(name + " AI story")]

        with patch("news.rss.FEEDS", two_feeds), \
                patch("news.rss.fetch_feed", side_effect=fake_fetch), \
                patch("news.rss.FEED_DEADLINE", 0.1):
            found = search_feeds(["ai"], None)
        self.assertEqual([i["title"] for i in found], ["Fast AI story"])


# ---------- NewsAPI requests ----------

class FetchArticlesTests(TestCase):
    def fake_response(self, articles):
        response = MagicMock()
        response.json.return_value = {"articles": articles}
        return response

    @patch.dict(os.environ, {"NEWS_API_KEY": "fake-key"})
    @patch("news.fetcher.requests.get")
    def test_query_is_sent_when_given(self, mock_get):
        mock_get.return_value = self.fake_response([1, 2])
        result = fetch_articles("economy", domains="dawn.com", from_date="2026-10-01T00:00:00")
        params = mock_get.call_args[1]["params"]
        self.assertEqual(params["q"], "economy")
        self.assertEqual(params["domains"], "dawn.com")
        self.assertEqual(params["from"], "2026-10-01T00:00:00")
        self.assertEqual(result, [1, 2])

    @patch.dict(os.environ, {"NEWS_API_KEY": "fake-key"})
    @patch("news.fetcher.requests.get")
    def test_query_is_left_out_for_a_headlines_search(self, mock_get):
        mock_get.return_value = self.fake_response([])
        fetch_articles("", domains="dawn.com")
        self.assertNotIn("q", mock_get.call_args[1]["params"])


# ---------- searching ----------
@patch("news.search.USE_GOOGLE_NEWS", False)
class SearchTests(TestCase):
    def test_extract_keywords_removes_filler_words(self):
        keywords = extract_keywords("What is the latest news about AI?")
        self.assertEqual(keywords, ["ai"])

    def test_pakistan_is_not_a_keyword_because_every_source_is_pakistani(self):
        self.assertEqual(extract_keywords("AI in Pakistan"), ["ai"])

    def test_general_requests_have_no_keywords(self):
        self.assertEqual(extract_keywords("Latest headlines"), [])
        self.assertEqual(extract_keywords("latest news in Pakistan"), [])

    def test_newsapi_query_expands_synonyms(self):
        query = newsapi_query(["ai", "regulation"], "AND")
        self.assertEqual(query, '(ai OR "artificial intelligence") AND regulation')

    def test_newsapi_query_is_empty_without_keywords(self):
        self.assertEqual(newsapi_query([], "AND"), "")

    @patch("news.search.fetch_articles", return_value=[{"title": "x"}])
    def test_search_newsapi_only_asks_pakistani_outlets(self, mock_fetch):
        search_newsapi(["economy"], "AND", None)
        kwargs = mock_fetch.call_args[1]
        self.assertIn("dawn.com", kwargs["domains"])
        self.assertIsNone(kwargs["from_date"])
        self.assertEqual(kwargs["sort_by"], "relevancy")

    @patch("news.search.fetch_articles", return_value=[])
    def test_search_newsapi_headlines_mode_sorts_by_newest(self, mock_fetch):
        search_newsapi([], "AND", None)
        self.assertEqual(mock_fetch.call_args[0][0], "")
        self.assertEqual(mock_fetch.call_args[1]["sort_by"], "publishedAt")
        self.assertIsNone(mock_fetch.call_args[1]["search_in"])

    @patch("news.search.fetch_articles", return_value=[])
    def test_search_newsapi_never_asks_further_back_than_the_free_plan(self, mock_fetch):
        since = datetime.now(timezone.utc) - timedelta(days=90)
        search_newsapi(["ai"], "AND", since)
        sent = datetime.strptime(mock_fetch.call_args[1]["from_date"], "%Y-%m-%dT%H:%M:%S")
        sent = sent.replace(tzinfo=timezone.utc)
        self.assertGreater(sent, datetime.now(timezone.utc) - timedelta(days=29))

    @patch("news.search.fetch_articles", side_effect=requests.RequestException("limit"))
    def test_search_newsapi_failure_returns_empty_list(self, mock_fetch):
        self.assertEqual(search_newsapi(["ai"], "AND", None), [])

    @patch("news.search.search_feeds")
    @patch("news.search.search_newsapi")
    def test_search_news_merges_and_removes_duplicates(self, mock_api, mock_feeds):
        mock_api.return_value = [make_item("AI rules", url="https://x.com/1")]
        mock_feeds.return_value = [
            make_item("AI rules", url="https://x.com/1?utm=tracking"),  # same link
            make_item("AI rules", url="https://y.com/2"),               # same headline
            make_item("AI jobs report", url="https://z.com/3"),
        ]
        self.assertEqual(len(search_news("AI rules")), 2)

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi")
    def test_search_news_ranks_headline_matches_first(self, mock_api, mock_feeds):
        mock_api.return_value = [
            make_item("Weather today", url="https://x.com/1"),
            make_item("AI regulation news", url="https://x.com/2"),
        ]
        result = search_news("AI regulation")
        self.assertEqual(result[0]["title"], "AI regulation news")

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi")
    def test_search_news_falls_back_to_or(self, mock_api, mock_feeds):
        mock_api.side_effect = [[], [make_item("Climate story")]]
        result = search_news("climate change")
        self.assertEqual(len(result), 1)
        self.assertEqual(mock_api.call_args_list[0][0][1], "AND")
        self.assertEqual(mock_api.call_args_list[1][0][1], "OR")

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_search_news_single_keyword_does_not_retry(self, mock_api, mock_feeds):
        search_news("economy")
        self.assertEqual(mock_api.call_count, 1)

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_search_news_drops_pakistan_from_the_search_words(self, mock_api, mock_feeds):
        search_news("AI in Pakistan")
        self.assertEqual(mock_api.call_args[0][0], ["ai"])

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_time_period_becomes_a_cutoff_date(self, mock_api, mock_feeds):
        search_news("economy", 1)
        self.assertIsNotNone(mock_api.call_args[0][2])
        search_news("economy", None)
        self.assertIsNone(mock_api.call_args[0][2])

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi")
    def test_headlines_mode_returns_newest_first(self, mock_api, mock_feeds):
        mock_api.return_value = [
            make_item("Older story", url="https://x.com/1", days_ago=3),
            make_item("Newest story", url="https://x.com/2", days_ago=1),
        ]
        result = search_news("latest headlines")
        self.assertEqual(mock_api.call_args[0][0], [])  # no keywords
        self.assertEqual(result[0]["title"], "Newest story")

    @patch("news.search.search_feeds")
    @patch("news.search.search_newsapi", return_value=[])
    def test_newsapi_failure_still_returns_rss_results(self, mock_api, mock_feeds):
        mock_feeds.return_value = [make_item("AI from RSS")]
        self.assertEqual(search_news("ai")[0]["title"], "AI from RSS")


# ---------- downloading one article page ----------

class ExtractTextTests(TestCase):
    """Downloading and cleaning one article page (network is faked)."""

    @patch("news.fetcher.trafilatura.extract", return_value="Article body")
    @patch("news.fetcher.requests.get")
    def test_returns_clean_text(self, mock_get, mock_extract):
        mock_get.return_value = MagicMock(text="<html>page</html>")
        self.assertEqual(extract_text("https://example.com"), "Article body")

    @patch("news.fetcher.requests.get", side_effect=requests.RequestException("timeout"))
    def test_returns_empty_string_when_download_fails(self, mock_get):
        self.assertEqual(extract_text("https://example.com"), "")

    @patch("news.fetcher.trafilatura.extract", return_value=None)
    @patch("news.fetcher.requests.get")
    def test_returns_empty_string_when_nothing_extracted(self, mock_get, mock_extract):
        mock_get.return_value = MagicMock(text="<html></html>")
        self.assertEqual(extract_text("https://example.com"), "")

class GoogleNewsTests(TestCase):
    def test_query_is_limited_to_pakistani_outlets_and_the_time_window(self):
        query = google_news_query(["superior", "university"], "AND", 7)
        self.assertIn("superior university", query)
        self.assertIn("site:dawn.com", query)
        self.assertTrue(query.endswith("when:7d"))

    def test_or_query_uses_synonyms_and_no_time_limit_for_any_time(self):
        query = google_news_query(["ai", "jobs"], "OR", None)
        self.assertIn('((ai OR "artificial intelligence") OR jobs)', query)
        self.assertNotIn("when:", query)

    def test_url_is_encoded(self):
        url = google_news_url(["economy"], "AND", 7)
        self.assertTrue(url.startswith("https://news.google.com/rss/search?q="))
        self.assertNotIn(" ", url)

    def test_tidy_splits_headline_and_outlet(self):
        item = tidy(make_item("Superior University opens campus - Dawn"))
        self.assertEqual(item["title"], "Superior University opens campus")
        self.assertEqual(item["source"]["name"], "Dawn")
        self.assertEqual(item["description"], "Superior University opens campus")

    def test_tidy_keeps_titles_without_an_outlet(self):
        self.assertEqual(tidy(make_item("Plain headline"))["title"], "Plain headline")

    @patch("news.gnews.fetch_feed")
    def test_search_returns_only_recent_tidied_items(self, mock_fetch):
        mock_fetch.return_value = [
            make_item("New rules - Dawn", days_ago=1),
            make_item("Old story - Dawn", days_ago=30),
        ]
        since = datetime.now(timezone.utc) - timedelta(days=7)
        found = search_google_news(["rules"], "AND", since, 7)
        self.assertEqual([i["title"] for i in found], ["New rules"])

    @patch("news.gnews.fetch_feed")
    def test_no_keywords_means_no_google_search(self, mock_fetch):
        self.assertEqual(search_google_news([], "AND", None, None), [])
        mock_fetch.assert_not_called()

    def test_google_query_uses_only_the_most_prominent_outlets(self):
        query = google_news_query(["economy"], "AND", 7)
        self.assertEqual(query.count("site:"), GOOGLE_SITE_LIMIT)
        self.assertIn("site:dawn.com", query)
        self.assertNotIn("site:pakshowbiz.com", query)


class GoogleNewsMergeTests(TestCase):
    @patch("news.search.USE_GOOGLE_NEWS", True)
    @patch("news.search.search_google_news")
    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_google_results_are_merged_in(self, mock_api, mock_feeds, mock_google):
        mock_google.return_value = [make_item("Superior University news", url="https://g.com/1")]
        result = search_news("Superior University")
        self.assertEqual(result[0]["title"], "Superior University news")

    @patch("news.search.USE_GOOGLE_NEWS", False)
    @patch("news.search.search_google_news")
    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_google_is_skipped_when_switched_off(self, mock_api, mock_feeds, mock_google):
        search_news("Superior University")
        mock_google.assert_not_called()


class GoogleSourceTextTests(TestCase):
    @patch("news.chat.fetch_texts", return_value=[""])
    @patch("news.chat.search_news")
    def test_google_news_links_are_not_downloaded(self, mock_search, mock_fetch_texts):
        mock_search.return_value = [{
            "title": "Superior University opens a new campus in Lahore city",
            "url": "https://news.google.com/rss/articles/abc",
            "description": "", "source": {"name": "Dawn"}, "publishedAt": "",
        }]
        sources = build_sources("superior university", 7)
        mock_fetch_texts.assert_called_once_with([""])
        self.assertEqual(len(sources), 1)
        self.assertIn("Superior University", sources[0]["text"])

class CheckFeedsCommandTests(TestCase):
    @patch("news.management.commands.check_feeds.fetch_feed")
    def test_reports_how_many_feeds_work(self, mock_fetch):
        mock_fetch.side_effect = lambda name, url: [make_item("x")] if name == "Dawn" else []
        out = StringIO()
        call_command("check_feeds", stdout=out)
        output = out.getvalue()
        self.assertIn("Dawn", output)
        self.assertIn("1 of %d feeds are working" % (len(FEEDS) + len(URDU_FEEDS)), output)

class UrduTextTests(TestCase):
    def test_detects_urdu_letters(self):
        self.assertTrue(has_urdu("پاکستان کی معیشت"))
        self.assertTrue(has_urdu("Karachi میں بارش"))
        self.assertFalse(has_urdu("economy"))

    def test_different_spellings_of_the_same_letters_match(self):
        self.assertEqual(normalize("كرکٹ"), normalize("کرکٹ"))    # Arabic kaf vs Urdu kaf
        self.assertEqual(normalize("کِرکٹ"), normalize("کرکٹ"))   # with a diacritic

    def test_urdu_keywords_match_whole_words_only(self):
        self.assertTrue(keyword_in_text("کرکٹ", "پاکستان کی کرکٹ ٹیم"))
        self.assertFalse(keyword_in_text("کرک", "کرکٹ ٹیم"))

    def test_question_is_split_into_urdu_and_english_words(self):
        urdu, english = split_urdu_question("پاکستان کی معیشت کے بارے میں بتائیں")
        self.assertEqual(urdu, ["معیشت"])
        self.assertEqual(english, ["economy"])

    def test_filler_only_question_has_no_keywords(self):
        self.assertEqual(split_urdu_question("تازہ ترین خبریں"), ([], []))

    def test_latin_words_in_an_urdu_question_are_kept(self):
        urdu, english = split_urdu_question("Karachi میں بارش")
        self.assertEqual(urdu, ["بارش"])
        self.assertEqual(english, ["karachi", "rain"])

    def test_phrases_are_translated(self):
        urdu, english = split_urdu_question("آئی ایم ایف کی شرائط")
        self.assertIn("imf", english)

    def test_unknown_urdu_word_has_no_english_equivalent(self):
        self.assertEqual(split_urdu_question("عجیب"), (["عجیب"], []))


@patch("news.search.USE_GOOGLE_NEWS", False)
class UrduSearchTests(TestCase):
    @patch("news.search.search_feeds")
    @patch("news.search.search_newsapi")
    def test_urdu_question_searches_urdu_feeds_and_the_english_sources(self, mock_api, mock_feeds):
        def fake_feeds(keywords, since, require_all=True, feeds=None):
            return [make_item("اردو خبر", url="https://urdu.example/1")] if feeds else []

        mock_feeds.side_effect = fake_feeds
        mock_api.return_value = [make_item("Economy news", url="https://en.example/1")]

        result = search_news("معیشت")

        self.assertEqual({i["title"] for i in result}, {"اردو خبر", "Economy news"})
        self.assertEqual(mock_api.call_args[0][0], ["economy"])
        urdu_calls = [c for c in mock_feeds.call_args_list if c[1].get("feeds") is URDU_FEEDS]
        self.assertEqual(urdu_calls[0][0][0], ["معیشت"])

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_unknown_urdu_word_does_not_trigger_english_headlines(self, mock_api, mock_feeds):
        search_news("عجیب")
        mock_api.assert_not_called()
        self.assertTrue(mock_feeds.called)

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_urdu_headlines_request_searches_both_languages(self, mock_api, mock_feeds):
        search_news("تازہ ترین خبریں")
        self.assertEqual(mock_api.call_args[0][0], [])
        self.assertTrue(any(c[1].get("feeds") is URDU_FEEDS for c in mock_feeds.call_args_list))

    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi", return_value=[])
    def test_english_question_never_touches_the_urdu_feeds(self, mock_api, mock_feeds):
        search_news("economy")
        for call in mock_feeds.call_args_list:
            self.assertNotIn("feeds", call[1])


class ManyArticlesTests(TestCase):
    @patch("news.search.USE_GOOGLE_NEWS", False)
    @patch("news.search.search_feeds", return_value=[])
    @patch("news.search.search_newsapi")
    def test_search_returns_up_to_the_limit(self, mock_api, mock_feeds):
        mock_api.return_value = [
            make_item("Economy story %d" % n, url="https://x.com/%d" % n)
            for n in range(MAX_RESULTS + 10)
        ]
        self.assertEqual(len(search_news("economy")), MAX_RESULTS)

    @patch("news.chat.fetch_texts")
    @patch("news.chat.search_news")
    def test_only_the_best_articles_are_downloaded_in_full(self, mock_search, mock_fetch_texts):
        mock_search.return_value = [
            make_item("Story %d" % n, url="https://example.com/%d" % n, description="d" * 100)
            for n in range(DEEP_READ + 4)
        ]
        mock_fetch_texts.side_effect = lambda urls: [""] * len(urls)

        sources = build_sources("economy", 7)

        urls = mock_fetch_texts.call_args[0][0]
        self.assertTrue(all(urls[:DEEP_READ]))
        self.assertEqual(urls[DEEP_READ:], [""] * 4)
        self.assertEqual(len(sources), DEEP_READ + 4)  # the rest are still used, from their descriptions

    def test_fetch_texts_skips_empty_addresses(self):
        with patch("news.chat.extract_text", return_value="page text") as mock_extract:
            texts = fetch_texts(["", "https://example.com"])
        self.assertEqual(texts, ["", "page text"])
        mock_extract.assert_called_once_with("https://example.com")

    def test_newsapi_query_quotes_phrases(self):
        self.assertEqual(newsapi_query(["prime minister", "budget"], "AND"), '"prime minister" AND budget')


class LanguageTests(TestCase):
    def test_auto_follows_the_question(self):
        self.assertEqual(resolve_language("auto", "معیشت"), "ur")
        self.assertEqual(resolve_language("auto", "economy"), "en")

    def test_explicit_choice_wins(self):
        self.assertEqual(resolve_language("en", "معیشت"), "en")
        self.assertEqual(resolve_language("ur", "economy"), "ur")

    @patch("news.chat.ask_llm", return_value=AI_REPLY)
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_prompt_asks_for_urdu_when_needed(self, mock_build, mock_ask):
        answer_question("economy", None, 7, "ur")
        self.assertIn("Urdu script", mock_ask.call_args[0][0])

    @patch("news.chat.ask_llm", return_value=AI_REPLY)
    @patch("news.chat.build_sources", return_value=SOURCES)
    def test_prompt_asks_for_english_by_default(self, mock_build, mock_ask):
        answer_question("economy")
        self.assertIn("simple English", mock_ask.call_args[0][0])

    @patch("news.chat.build_sources", return_value=[])
    def test_no_results_message_is_in_urdu_for_urdu_questions(self, mock_build):
        self.assertTrue(has_urdu(answer_question("معیشت")["answer"]))

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_language_is_passed_on(self, mock_answer):
        post_chat(self.client, '{"message": "economy", "language": "ur"}')
        self.assertEqual(mock_answer.call_args[0][3], "ur")

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_language_defaults_to_auto(self, mock_answer):
        post_chat(self.client, '{"message": "economy"}')
        self.assertEqual(mock_answer.call_args[0][3], "auto")

    def test_rejects_unknown_language(self):
        response = post_chat(self.client, '{"message": "economy", "language": "fr"}')
        self.assertEqual(response.status_code, 400)

    def test_rejects_non_text_language(self):
        response = post_chat(self.client, '{"message": "economy", "language": ["ur"]}')
        self.assertEqual(response.status_code, 400)

    def test_home_page_has_language_choices_and_lists_urdu_outlets(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "اردو")
        self.assertContains(response, "Auto")
        self.assertContains(response, "Daily Jang")


@override_settings(FAIR_USE_PER_HOUR=2, SITE_DAILY_LIMIT=100)
class FairUseTests(TestCase):
    def ask(self, client=None):
        return post_chat(client or self.client, '{"message": "economy"}')

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_visitor_is_limited_per_hour(self, mock_answer):
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.ask().status_code, 200)
        response = self.ask()
        self.assertEqual(response.status_code, 429)
        self.assertIn("try again later", response.json()["error"].lower())
        self.assertEqual(mock_answer.call_count, 2)  # the AI was not called for the blocked question

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_other_visitors_are_not_blocked(self, mock_answer):
        self.ask()
        self.ask()
        self.assertEqual(self.ask(Client()).status_code, 200)

    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_questions_older_than_an_hour_do_not_count(self, mock_answer):
        self.client.get(reverse("home"))
        visitor = "session:" + self.client.session.session_key
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        for _ in range(2):
            usage = Usage.objects.create(visitor=visitor)
            Usage.objects.filter(pk=usage.pk).update(created_at=two_hours_ago)
        self.assertEqual(self.ask().status_code, 200)

    @patch("news.views.answer_question", side_effect=Exception("boom"))
    def test_failed_attempts_still_count(self, mock_answer):
        self.client.get(reverse("home"))   # like a real visitor: opening the page creates their session
        self.assertEqual(self.ask().status_code, 500)
        self.assertEqual(self.ask().status_code, 500)
        self.assertEqual(self.ask().status_code, 429)

    def test_invalid_requests_do_not_count(self):
        post_chat(self.client, '{"message": ""}')
        self.assertEqual(Usage.objects.count(), 0)


@override_settings(FAIR_USE_PER_HOUR=100, SITE_DAILY_LIMIT=2)
class SiteLimitTests(TestCase):
    @patch("news.views.answer_question", return_value=FAKE_RESULT)
    def test_whole_site_has_a_daily_cap(self, mock_answer):
        for _ in range(2):
            self.assertEqual(post_chat(Client(), '{"message": "economy"}').status_code, 200)
        response = post_chat(Client(), '{"message": "economy"}')
        self.assertEqual(response.status_code, 429)
        self.assertIn("daily limit", response.json()["error"].lower())


class NewsApiSwitchTests(TestCase):
    @patch("news.search.fetch_articles")
    @patch("news.search.USE_NEWSAPI", False)
    def test_newsapi_is_skipped_when_switched_off(self, mock_fetch):
        self.assertEqual(search_newsapi(["economy"], "AND", None), [])
        mock_fetch.assert_not_called()

    @patch("news.search.fetch_articles", side_effect=KeyError("NEWS_API_KEY"))
    def test_a_missing_key_is_not_fatal(self, mock_fetch):
        self.assertEqual(search_newsapi(["economy"], "AND", None), [])