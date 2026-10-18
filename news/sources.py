"""Where news comes from. Edit this file to add outlets, feeds, or time ranges."""

import os

TIME_RANGES = {
    "24h": {"label": "Last 24 hours", "days": 1},
    "7d": {"label": "Last 7 days", "days": 7},
    "30d": {"label": "Last 30 days", "days": 30},
    "any": {"label": "Any time", "days": None},
}
DEFAULT_PERIOD = "7d"

# Words that mean the same thing: searching "ai" also finds "artificial intelligence"
SYNONYMS = {"ai": ["ai", "artificial intelligence"]}

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "in", "on", "at", "of",
    "for", "to", "from", "with", "and", "or", "about", "what", "which", "who", "how",
    "why", "when", "where", "tell", "me", "give", "show", "any", "some", "there", "this",
    "that", "it", "its", "do", "does", "did", "can", "could", "should", "would", "please",
    "latest", "recent", "news", "today", "update", "updates", "happening", "happened",
    "going", "more", "info", "information", "headlines", "headline", "breaking",
    # every source is Pakistani, so these words say nothing about the topic
    "pakistan", "pakistani",
}

# NewsAPI searches only these outlets. The most prominent come first, because Google News
# can only handle the first GOOGLE_SITE_LIMIT of them in one search.
DOMAINS = [
    "dawn.com", "tribune.com.pk", "thenews.com.pk", "geo.tv", "brecorder.com",
    "arynews.tv", "nation.com.pk", "pakistantoday.com.pk", "samaa.tv", "dunyanews.tv",
    "app.com.pk", "dailytimes.com.pk", "pakobserver.net", "thefridaytimes.com",
    "arabnews.pk", "propakistani.pk", "bolnews.com", "92newshd.tv", "gnnhd.tv",
    "thefrontierpost.com", "hamariweb.com", "thefinancialdaily.com",
    "pakistangulfeconomist.com", "timesofislamabad.com", "lhrtimes.com",
    "dailythepatriot.com", "dailynewspk.com", "thepakistan.pk", "thecurrent.pk",
    "chitraltimes.com", "inp.net.pk", "pakshowbiz.com",
]
GOOGLE_SITE_LIMIT = 12

# RSS feeds: (name, address). Run "python manage.py check_feeds" to test them.
FEEDS = [
    ("Dawn", "https://www.dawn.com/feeds/home"),
    ("Express Tribune", "https://tribune.com.pk/feed/home"),
    ("The News", "https://www.thenews.com.pk/rss/1/1"),
    ("Geo News", "https://www.geo.tv/rss/1/0"),
    ("Business Recorder", "https://www.brecorder.com/feeds/latest-news"),
    ("The Nation", "https://www.nation.com.pk/rss/newspaper"),
    ("Pakistan Today", "https://www.pakistantoday.com.pk/feed"),
    ("ARY News", "https://arynews.tv/feed/"),
    ("Arab News Pakistan", "https://www.arabnews.pk/rss.xml"),
    ("ProPakistani", "https://propakistani.pk/feed/"),
    ("BOL News", "https://www.bolnews.com/feed/"),
    ("92 News", "https://92newshd.tv/feed"),
    ("GNN", "https://gnnhd.tv/rss/latest"),
    ("The Frontier Post", "https://thefrontierpost.com/feed/"),
    ("Hamariweb News", "https://enews.hamariweb.com/feed/"),
    ("The Financial Daily", "https://thefinancialdaily.com/feed/"),
    ("Pakistan & Gulf Economist", "https://www.pakistangulfeconomist.com/feed/"),
    ("Times of Islamabad", "https://timesofislamabad.com/rss/latest-news"),
    ("The Lahore Times", "https://www.lhrtimes.com/feed/"),
    ("Daily Patriot", "https://dailythepatriot.com/feed/"),
    ("Daily NewsPK", "https://dailynewspk.com/feed/"),
    ("The Pakistan", "https://thepakistan.pk/feed/"),
    ("The Current", "https://thecurrent.pk/feed/"),
    ("Chitral Times", "https://chitraltimes.com/feed"),
    ("Independent News Pakistan", "https://www.inp.net.pk/inp/rss"),
    ("PakShowbiz", "https://www.pakshowbiz.com/feed/"),
]

# ---------- Urdu support ----------

# Urdu RSS feeds, searched only when the question is written in Urdu.
# These addresses are untested: run "python manage.py check_feeds" and delete failures.
URDU_FEEDS = [
    ("Express News (Urdu)", "https://www.express.pk/feed/"),
    ("Daily Jang", "https://feeds.feedburner.com/com/Yeor"),
    ("Daily Khabrain", "https://dailykhabrain.com.pk/feed/"),
    ("92 News (Urdu)", "https://urdu.92newshd.tv/feed"),
    ("Aaj News", "https://www.aaj.tv/feeds/latest-news"),
    ("BOL News (Urdu)", "https://www.bolnewsurdu.com/feed/"),
    ("Daily Ummat", "https://ummat.net/feed/"),
    ("Daily Qudrat", "https://dailyqudrat.pk/feed/"),
    ("Mashriq", "https://mashriqtv.pk/feed/"),
    ("Nai Baat", "https://naibaat.pk/feed/"),
    ("PNI (Urdu)", "https://pni.net.pk/feed/"),
]

# Filler words in Urdu questions (like "what", "is", "about", "latest news").
URDU_STOPWORDS = {
    "کیا", "ہے", "ہیں", "کے", "کی", "کا", "کو", "میں", "اور", "سے", "پر", "نے", "یہ", "وہ",
    "تھا", "تھی", "تھے", "ہو", "ہوا", "ہوئی", "ہوئے", "کر", "کرنے", "بارے", "مجھے", "بتائیں",
    "بتاؤ", "دکھائیں", "آج", "تازہ", "ترین", "خبریں", "خبر", "نیوز", "پاکستان", "پاکستانی",
    "کوئی", "کچھ", "کہ", "بھی", "تو", "ہی", "ایک", "اس", "ان", "جو", "کون", "کب", "کہاں",
    "کیوں", "کیسے", "لیے", "لئے", "والی", "والا", "والے", "ہوگا", "سکتا", "سکتی", "گا", "گی",
    "گے", "ہم", "آپ", "میرے", "ہمارے", "اپنے", "ساتھ", "بعد", "پہلے", "اب",
}

# Urdu word -> English search words, so an Urdu question can also find English articles.
# Please review this list (you can read Urdu!) and add the words you expect to be used.
URDU_TO_ENGLISH = {
    # economy and business
    "معیشت": ["economy"], "معاشی": ["economy"], "اقتصاد": ["economy"], "اقتصادی": ["economy"],
    "مہنگائی": ["inflation"], "بجٹ": ["budget"], "ٹیکس": ["tax"], "پیٹرول": ["petrol"],
    "پٹرول": ["petrol"], "ڈالر": ["dollar"], "روپیہ": ["rupee"], "روپے": ["rupee"],
    "تجارت": ["trade"], "برآمدات": ["exports"], "سرمایہ": ["investment"], "بینک": ["bank"],
    "اسٹاک": ["stock"], "سٹاک": ["stock"],
    # politics, law, security
    "سیاست": ["politics"], "سیاسی": ["politics"], "حکومت": ["government"],
    "وزیراعظم": ["prime minister"], "وزیر": ["minister"], "الیکشن": ["election"],
    "انتخابات": ["election"], "عدالت": ["court"], "کورٹ": ["court"], "پولیس": ["police"],
    "فوج": ["army"], "کرپشن": ["corruption"],
    # society
    "تعلیم": ["education"], "یونیورسٹی": ["university"], "امتحان": ["exam"],
    "امتحانات": ["exam"], "طلبہ": ["students"], "صحت": ["health"], "ہسپتال": ["hospital"],
    # weather and disasters
    "موسم": ["weather"], "بارش": ["rain"], "سیلاب": ["floods"], "زلزلہ": ["earthquake"],
    "حادثہ": ["accident"],
    # sports
    "کرکٹ": ["cricket"], "ہاکی": ["hockey"], "فٹبال": ["football"], "کھیل": ["sports"],
    # technology and energy
    "ٹیکنالوجی": ["technology"], "انٹرنیٹ": ["internet"], "موبائل": ["mobile"],
    "بجلی": ["electricity"], "گیس": ["gas"], "پانی": ["water"],
    # places
    "کراچی": ["karachi"], "لاہور": ["lahore"], "پشاور": ["peshawar"], "کوئٹہ": ["quetta"],
    "پنجاب": ["punjab"], "سندھ": ["sindh"], "بلوچستان": ["balochistan"], "بھارت": ["india"],
    "انڈیا": ["india"], "افغانستان": ["afghanistan"], "چین": ["china"], "امریکہ": ["america"],
    "امریکا": ["america"],
}

# Multi-word Urdu phrases that mean one English thing.
URDU_PHRASES = {
    "اسلام آباد": ["islamabad"],
    "سپریم کورٹ": ["supreme court"],
    "آئی ایم ایف": ["imf"],
    "مصنوعی ذہانت": ["ai"],
    "اسٹاک ایکسچینج": ["stock exchange"],
    "پی ٹی آئی": ["pti"],
    "وزیر اعظم": ["prime minister"],
}

# Also search Google News (limited to the first GOOGLE_SITE_LIMIT outlets in DOMAINS).
# Set to False to turn it off.
USE_GOOGLE_NEWS = True

# NewsAPI's free plan is for development only, so switch it OFF on a public site.
USE_NEWSAPI = os.environ.get("USE_NEWSAPI", "true").lower() == "true"