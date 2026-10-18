import os
import time

from django.db.models.functions import Length
from google import genai

from .models import Article, Summary

MAX_CHARS = 6000  # limit how much article text we send
MIN_CHARS = 200   # skip articles with too little text

PROMPT_TEMPLATE = """You are a news summarizer for a general audience.

Summarize the article below in AT MOST 3 short, simple sentences.
Rules:
- Use ONLY information found in the article text. Do not add outside facts.
- Never include opinions, predictions, or details that are not stated in the article.
- Use plain language that a high-school student can understand.
- If the text is too short or unclear to summarize, reply exactly: INSUFFICIENT TEXT

Title: {title}

Article text:
{text}
"""


class DailyQuotaExceeded(Exception):
    """Raised when the free daily limit is used up."""


def summarize_text(title, text, retries=3):
    """Send one article to Gemini and return (summary, model_name).

    Retries on temporary errors (503 busy, per-minute 429).
    Stops immediately if the daily quota is used up.
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    prompt = PROMPT_TEMPLATE.format(title=title, text=text[:MAX_CHARS])

    for attempt in range(retries):
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return (response.text or "").strip(), model
        except Exception as error:
            if "PerDay" in str(error):
                raise DailyQuotaExceeded("Daily free quota used up. Try again tomorrow.")

            is_temporary = "503" in str(error) or "429" in str(error)
            if is_temporary and attempt < retries - 1:
                wait = 10 * (attempt + 1)  # wait 10s, then 20s
                print(f"Busy, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def summarize_pending_articles(limit=5, delay=7, topic=None):
    """Summarize articles that don't have a summary yet. Returns how many were done."""
    pending = (
        Article.objects.filter(summary__isnull=True)
        .annotate(text_length=Length("text"))
        .filter(text_length__gte=MIN_CHARS)
    )
    if topic:
        pending = pending.filter(topic=topic)
    pending = pending[:limit]

    done = 0
    for article in pending:
        try:
            summary_text, model = summarize_text(article.title, article.text)
        except DailyQuotaExceeded as error:
            print(error)
            break  # no point trying the remaining articles
        except Exception as error:
            print(f"Failed on '{article.title}': {error}")
            continue

        if not summary_text or "INSUFFICIENT TEXT" in summary_text:
            continue

        Summary.objects.create(article=article, text=summary_text, model_name=model)
        done += 1
        time.sleep(delay)  # pause so we stay under the per-minute limit

    return done