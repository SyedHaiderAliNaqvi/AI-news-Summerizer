import json

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .chat import answer_question
from .fetcher import save_articles_for_topic
from .models import Conversation, Message, Topic, Usage
from .sources import DEFAULT_PERIOD, FEEDS, TIME_RANGES, URDU_FEEDS
from .summarizer import DailyQuotaExceeded, summarize_pending_articles

HISTORY_LIMIT = 6


def get_session_key(request):
    """Return this visitor's session key, creating a session if needed."""
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def owned_conversations(request):
    """Chats this visitor may see: their account's chats, or this browser's guest chats."""
    if request.user.is_authenticated:
        return Conversation.objects.filter(user=request.user)
    return Conversation.objects.filter(
        session_key=get_session_key(request), user__isnull=True
    )


def adopt_guest_chats(old_session_key, user):
    """After sign-in, move the chats made as a guest into the account."""
    if old_session_key:
        Conversation.objects.filter(
            session_key=old_session_key, user__isnull=True
        ).update(user=user)


def visitor_id(request):
    """Who is asking: the signed-in account, or this browser's session."""
    if request.user.is_authenticated:
        return f"user:{request.user.id}"
    return f"session:{get_session_key(request)}"


def fair_use_error(request):
    """Return a message if this question would break a limit, otherwise None.

    It counts attempts, not only successes, because a failed request can still use quota.
    """
    now = timezone.now()
    per_hour = getattr(settings, "FAIR_USE_PER_HOUR", 10)
    per_day = getattr(settings, "SITE_DAILY_LIMIT", 15)

    Usage.objects.filter(created_at__lt=now - timedelta(days=2)).delete()  # tidy old rows

    asked_this_hour = Usage.objects.filter(
        visitor=visitor_id(request), created_at__gte=now - timedelta(hours=1)
    ).count()
    if asked_this_hour >= per_hour:
        return f"You've asked {per_hour} questions in the last hour. Please try again later."

    asked_today = Usage.objects.filter(created_at__gte=now - timedelta(days=1)).count()
    if asked_today >= per_day:
        return "This free demo has reached its daily limit of AI answers. Please try again tomorrow."
    return None


def conversation_to_dict(conversation):
    return {
        "id": conversation.id,
        "title": conversation.title,
        "updated_at": conversation.updated_at.isoformat(),
    }

def message_to_dict(message):
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "articles": message.articles,
    }


# ---------- chat pages and API ----------

def home(request):
    get_session_key(request)
    return render(request, "news/chat.html", {
        "periods": [(key, period["label"]) for key, period in TIME_RANGES.items()],
        "default_period": DEFAULT_PERIOD,
        "sources": [name for name, url in FEEDS],
        "urdu_sources": [name for name, url in URDU_FEEDS],
    })


@require_GET
def conversation_list(request):
    conversations = owned_conversations(request)[:50]
    return JsonResponse({"conversations": [conversation_to_dict(c) for c in conversations]})


@require_http_methods(["GET", "DELETE"])
def conversation_detail(request, conversation_id):
    conversation = owned_conversations(request).filter(id=conversation_id).first()
    if conversation is None:
        return JsonResponse({"error": "Chat not found."}, status=404)

    if request.method == "DELETE":
        conversation.delete()
        return JsonResponse({"ok": True})

    return JsonResponse({
        "conversation": conversation_to_dict(conversation),
        "messages": [message_to_dict(m) for m in conversation.messages.all()],
    })


@require_POST
def chat_api(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid request."}, status=400)

    question = (data.get("message") or "").strip()
    if not question:
        return JsonResponse({"error": "Please type a message."}, status=400)
    if len(question) > 200:
        return JsonResponse({"error": "Please keep your message under 200 characters."}, status=400)

    period = data.get("period") or DEFAULT_PERIOD
    if not isinstance(period, str) or period not in TIME_RANGES:
        return JsonResponse({"error": "Invalid request."}, status=400)
    days = TIME_RANGES[period]["days"]
    language = data.get("language") or "auto"
    if not isinstance(language, str) or language not in ("auto", "en", "ur"):
        return JsonResponse({"error": "Invalid request."}, status=400)

    conversation = None
    history = []
    conversation_id = data.get("conversation_id")
    if conversation_id is not None:
        if not isinstance(conversation_id, int):
            return JsonResponse({"error": "Invalid request."}, status=400)
        conversation = owned_conversations(request).filter(id=conversation_id).first()
        if conversation is None:
            return JsonResponse({"error": "Chat not found."}, status=404)
        recent = conversation.messages.order_by("-id")[:HISTORY_LIMIT]
        history = [{"role": m.role, "content": m.content} for m in reversed(list(recent))]

    limit_message = fair_use_error(request)
    if limit_message:
        return JsonResponse({"error": limit_message}, status=429)
    Usage.objects.create(visitor=visitor_id(request))

    try:
        result = answer_question(question, history, days, language)
    except DailyQuotaExceeded:
        return JsonResponse(
            {"error": "The free daily AI limit has been reached. Please try again tomorrow."},
            status=429,
        )
    except Exception as error:
        print(f"Chat error: {error}")  # details go to your terminal only
        return JsonResponse(
            {"error": "Something went wrong. Please try again in a moment."}, status=500
        )

    # Save only after success, so failed requests don't leave half-empty chats
    if conversation is None:
        conversation = Conversation.objects.create(
            user=request.user if request.user.is_authenticated else None,
            session_key=get_session_key(request),
            title=question[:50],
        )
    Message.objects.create(conversation=conversation, role="user", content=question)
    reply = Message.objects.create(
        conversation=conversation,
        role="assistant",
        content=result["answer"],
        articles=result["articles"],
    )
    conversation.save(update_fields=["updated_at"])

    return JsonResponse({
        "conversation": conversation_to_dict(conversation),
        "reply": message_to_dict(reply),
    })


# ---------- accounts ----------

def register(request):
    if request.user.is_authenticated:
        return redirect("home")

    old_session_key = request.session.session_key  # remember it: login changes the key
    form = UserCreationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        adopt_guest_chats(old_session_key, user)
        return redirect("home")

    return render(request, "news/auth.html", {
        "form": form, "title": "Create account", "button": "Create account",
        "alt_text": "Already have an account?", "alt_link": "Sign in",
        "alt_url": "login",
    })


def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")

    old_session_key = request.session.session_key
    form = AuthenticationForm(request, data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        adopt_guest_chats(old_session_key, request.user)
        return redirect("home")

    return render(request, "news/auth.html", {
        "form": form, "title": "Sign in", "button": "Sign in",
        "alt_text": "New here?", "alt_link": "Create an account",
        "alt_url": "register",
    })


@require_POST
def logout_view(request):
    logout(request)
    return redirect("home")


# ---------- older topic pages ----------

def topic_detail(request, topic_id):
    topic = get_object_or_404(Topic, id=topic_id)
    articles = topic.articles.filter(summary__isnull=False).select_related("summary")
    return render(request, "news/topic.html", {"topic": topic, "articles": articles})


@require_POST
def refresh_topic(request, topic_id):
    topic = get_object_or_404(Topic, id=topic_id)
    try:
        new_articles = save_articles_for_topic(topic)
        new_summaries = summarize_pending_articles(limit=3, delay=5, topic=topic)
        messages.success(
            request,
            f"Found {new_articles} new articles and created {new_summaries} summaries.",
        )
    except Exception as error:
        messages.error(request, f"Refresh failed: {error}")
    return redirect("topic_detail", topic_id=topic.id)