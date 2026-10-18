import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait

from google import genai
from google.genai import types

from .fetcher import extract_text
from .search import extract_keywords, search_news  # noqa: F401 (extract_keywords is re-exported)
from .summarizer import DailyQuotaExceeded
from .urdu import has_urdu

MAX_CHARS_PER_SOURCE = 1500   # text sent to the AI for an article read in full
SHALLOW_CHARS = 400           # text sent for the rest (headline + description)
DEEP_READ = 8                 # how many articles get their whole page downloaded
HISTORY_MESSAGES = 6
SCRAPE_PAGES = True           # set to False for much faster (but less detailed) answers
PAGE_DEADLINE = 8             # seconds to wait for article pages, all downloaded in parallel

MESSAGES = {
    "en": {
        "none": (
            "I couldn't find recent Pakistani news about that. "
            "Try broader keywords or a longer time period."
        ),
        "none_for": (
            'I couldn\'t find recent Pakistani news specifically about "{question}". '
            "Try broader keywords or a longer time period."
        ),
    },
    "ur": {
        "none": "مجھے اس بارے میں حالیہ پاکستانی خبریں نہیں ملیں۔ دوسرے الفاظ آزمائیں یا وقت کی مدت بڑھائیں۔",
        "none_for": "مجھے \"{question}\" کے بارے میں حالیہ پاکستانی خبریں نہیں ملیں۔ دوسرے الفاظ آزمائیں یا وقت کی مدت بڑھائیں۔",
    },
}

LANGUAGE_RULES = {
    "en": (
        'Write the "answer" and every "summary" in simple English. '
        "The sources may be in Urdu; translate them faithfully."
    ),
    "ur": (
        'Write the "answer" and every "summary" in simple, natural Urdu using Urdu script. '
        "The sources may be in English; translate them faithfully. "
        "Keep names, numbers, and the source markers such as [1] exactly as they are, "
        "using ordinary digits 0-9 for the markers."
    ),
}

PROMPT_TEMPLATE = """You are a careful news assistant.
Use ONLY the numbered sources below. They come from Pakistani news outlets.

Reply with JSON only. The JSON must have two keys:
"answer": a string.
"articles": a list of objects, each with "id" (the source number, as a number) and "summary" (a string).

Rules:
- {language_rule}
- "articles": include a source ONLY if it is directly about the user's topic. Skip anything loosely related or off-topic. "summary" is ONE simple sentence (max 25 words) saying what that article reports.
- "answer": at most 5 short sentences in plain language that respond to the user's latest message. After each claim, cite the source number in square brackets, like [1] or [2][3]. Mention only facts from the sources you included in "articles".
- Some sources contain only a headline and a short description. You may cite them for what the headline says, but not for more.
- If the user asks for the latest news or headlines in general, include the sources that are the most important or recent.
- Use only facts found in the sources. Never add outside facts, opinions, or predictions.
- If no source is directly about the topic, return an empty "articles" list and a one-sentence "answer" saying no relevant recent articles were found.
- Use the conversation so far only to understand follow-up questions.

Conversation so far:
{history}

User's latest message: {question}

Sources:
{sources}
"""


def resolve_language(language, question):
    """'auto' follows the language of the question; 'en' and 'ur' are explicit choices."""
    if language in ("en", "ur"):
        return language
    return "ur" if has_urdu(question) else "en"


def fetch_texts(urls):
    """Download several article pages at the same time. Empty addresses and slow pages are skipped."""
    texts = [""] * len(urls)
    wanted = [(index, url) for index, url in enumerate(urls) if url]
    if not wanted or not SCRAPE_PAGES:
        return texts

    executor = ThreadPoolExecutor(max_workers=len(wanted))
    futures = {executor.submit(extract_text, url): index for index, url in wanted}
    wait(list(futures), timeout=PAGE_DEADLINE)
    for future, index in futures.items():
        if future.done() and not future.exception():
            texts[index] = future.result()
    executor.shutdown(wait=False)  # don't wait for stragglers
    return texts


def build_sources(question, days=7):
    """Retrieval step: find articles, then read the best DEEP_READ of them in full."""
    items = [
        item for item in search_news(question, days)
        if item.get("url") and item.get("title") != "[Removed]"
    ]
    # Downloading pages is the slow part, so only the best-ranked articles are fetched in full.
    # Google News links are redirect pages with no article text, so they are never downloaded.
    urls = [
        item["url"] if index < DEEP_READ and "news.google.com" not in item["url"] else ""
        for index, item in enumerate(items)
    ]
    scraped_texts = fetch_texts(urls)

    sources = []
    for item, scraped in zip(items, scraped_texts):
        text = scraped or item.get("content") or item.get("description") or ""
        if len(text) < 80:
            text = f"{item.get('title') or ''}. {text}".strip()  # headline + short description
        if len(text) < 40:
            continue
        sources.append({
            "id": len(sources) + 1,
            "title": item.get("title") or "Untitled",
            "source": (item.get("source") or {}).get("name", ""),
            "url": item["url"],
            "published_at": item.get("publishedAt") or "",
            "snippet": (item.get("description") or "")[:250],
            "text": text[:MAX_CHARS_PER_SOURCE if scraped else SHALLOW_CHARS],
        })
    return sources


def format_history(history):
    """Turn recent messages into plain text for the prompt."""
    lines = []
    for message in history[-HISTORY_MESSAGES:]:
        who = "User" if message["role"] == "user" else "Assistant"
        lines.append(f"{who}: {message['content'][:500]}")
    return "\n".join(lines) or "(none)"


def ask_llm(prompt, retries=3):
    """Generation step: one request to Gemini, asking for a JSON reply."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    config = types.GenerateContentConfig(response_mime_type="application/json")

    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            return (response.text or "").strip()
        except Exception as error:
            if "PerDay" in str(error):
                raise DailyQuotaExceeded("Daily free quota used up.")
            temporary = "503" in str(error) or "429" in str(error)
            if temporary and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
            else:
                raise


def parse_reply(raw):
    """Turn the model's JSON reply into (answer, {article_id: summary}).

    If the reply is not valid JSON, returns (raw_text, None).
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):  # remove accidental code fences
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return raw, None
    if not isinstance(data, dict):
        return raw, None

    answer = str(data.get("answer") or "").strip()
    summaries = {}
    for item in data.get("articles") or []:
        try:
            summaries[int(item["id"])] = str(item.get("summary") or "").strip()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return answer or "Sorry, I couldn't put together an answer. Please try again.", summaries


def answer_question(question, history=None, days=7, language="auto"):
    """Full pipeline: retrieve, build the prompt, generate. Returns a dict."""
    history = history or []
    lang = resolve_language(language, question)

    sources = build_sources(question, days)
    if not sources:
        return {"answer": MESSAGES[lang]["none"], "articles": []}

    sources_block = "\n\n".join(
        f"[{s['id']}] {s['title']} ({s['source']})\n{s['text']}" for s in sources
    )
    prompt = PROMPT_TEMPLATE.format(
        language_rule=LANGUAGE_RULES[lang],
        history=format_history(history),
        question=question,
        sources=sources_block,
    )
    answer, summaries = parse_reply(ask_llm(prompt))

    if summaries is None:  # reply wasn't valid JSON: show everything we found
        relevant, summaries = sources, {}
    else:                  # keep only the articles the AI marked as relevant
        relevant = [s for s in sources if s["id"] in summaries]

    if not relevant and not history:
        return {"answer": MESSAGES[lang]["none_for"].format(question=question), "articles": []}

    articles = [
        {
            "id": s["id"],
            "title": s["title"],
            "source": s["source"],
            "url": s["url"],
            "published_at": s["published_at"],
            "summary": summaries.get(s["id"]) or s["snippet"],
        }
        for s in relevant
    ]
    return {"answer": answer, "articles": articles}