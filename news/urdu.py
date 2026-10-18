import re

_ARABIC_SCRIPT = re.compile("[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
# Diacritics, tatweel, and invisible joiners: they change how text looks, not what it says
_REMOVE = re.compile("[\u064B-\u065F\u0670\u0640\u200c\u200d]")
# Arabic and Urdu keyboards produce different codes for look-alike letters
_LETTERS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ه": "ہ", "ۃ": "ہ"})


def has_urdu(text):
    """True if the text contains Urdu (Arabic-script) letters."""
    return bool(_ARABIC_SCRIPT.search(text or ""))


def normalize(text):
    """Make different spellings of the same Urdu letters identical, and lowercase English."""
    text = _REMOVE.sub("", text or "")
    return text.translate(_LETTERS).lower()