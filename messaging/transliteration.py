"""
transliteration.py
--------------------
Converts Romanized Hinglish text into Devanagari script before it
reaches Piper's Hindi voice model. The Hindi Piper voice
(hi_IN-rohan-medium) was trained on Devanagari phonemes; feeding it raw
Latin-script text (what our LLM-generated Hinglish messages look like)
is exactly why synthesis has sounded slurred/robotic -- the model has
no real mapping for Latin characters.

This does not touch what the app displays, generates, or logs -- the
dashboard, the audit trail's `message_sent` field, and the
`/api/simulate` JSON response all continue to show the original
Roman-script Hinglish exactly as before. This module exists solely to
give Piper text it can actually pronounce.

Strategy (word-by-word, fail-safe at every level):
    1. Currency amounts ("₹1,499") are rewritten as "1,499 रुपये" --
       natural spoken-Hindi word order -- with the digits themselves
       left untouched, since Piper's espeak-ng-based Hindi backend
       already has its own number-to-words handling for digit strings.
    2. Each remaining word is checked against a hand-curated
       DOMAIN_LEXICON of common fintech/product loanwords first --
       these render far more naturally than generic letter-by-letter
       transliteration would (e.g. "Monthly" mangled by ITRANS rules
       vs. the correct "मंथली").
    3. Anything not in the lexicon is passed through
       `indic_transliteration`'s ITRANS -> Devanagari scheme.
    4. If any single word fails to transliterate, that WORD falls back
       to its original Latin-script spelling and processing continues
       for the rest of the message -- one bad token never breaks the
       whole line.
    5. If something totally unexpected happens, the ORIGINAL, untouched
       text is returned rather than raising -- voice synthesis degrades
       gracefully to imperfect Latin-script input rather than crashing
       the message pipeline.

IMPORTANT -- read this before treating this as a "real" transliterator:
    ITRANS is a formal Sanskrit-derived romanization scheme, not a
    parser for colloquial Hinglish spelling. It gets common, simple
    Hindi words right ("aapka" -> "आपका") but has no concept of
    word-sense disambiguation and will produce a rough phonetic
    approximation -- not real Hindi words -- for ANY English word not
    in DOMAIN_LEXICON. This means messages that lean more heavily
    English (which the LLM sometimes produces) will still sound rough
    even after this fix, since only ~25 common loanwords are covered.
    Regenerating a message a few times and picking a more Hindi-heavy
    result for a demo recording is a legitimate, easy way to work
    around this -- see README.md "Limitations" section.
"""

from __future__ import annotations

import re
from typing import List

try:
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate
    _TRANSLITERATION_AVAILABLE = True
except ImportError:
    _TRANSLITERATION_AVAILABLE = False


# Domain lexicon: curated fintech/product loanwords that Piper's Hindi
# voice pronounces far more naturally when mapped to a specific,
# hand-picked Devanagari spelling than when run through generic
# letter-by-letter ITRANS transliteration. Keys are lowercase; matching
# is case-insensitive and preserves the original token's surrounding
# punctuation.
DOMAIN_LEXICON = {
    "payment": "पेमेंट",
    "payments": "पेमेंट्स",
    "fail": "फ़ेल",
    "failed": "फ़ेल",
    "pro": "प्रो",
    "monthly": "मंथली",
    "update": "अपडेट",
    "updated": "अपडेट",
    "customer": "कस्टमर",
    "subscription": "सब्सक्रिप्शन",
    "otp": "ओटीपी",
    "card": "कार्ड",
    "bank": "बैंक",
    "app": "ऐप",
    "account": "अकाउंट",
    "rupees": "रुपये",
    "rupee": "रुपया",
    "id": "आईडी",
    "sms": "एसएमएस",
    "email": "ईमेल",
    "link": "लिंक",
    "gateway": "गेटवे",
    "retry": "रीट्राई",
    "recovery": "रिकवरी",
    "razorpay": "रेज़रपे",
}

# Greetings need special-casing: sentence-initial "Hi" is an English
# greeting (-> हाय), not the Hindi emphasis particle "hi" (ही) that
# generic transliteration rules would otherwise produce.
_GREETING_MAP = {"hi": "हाय", "hello": "हैलो", "hey": "हे"}

# Currency and numeric tokens must never be run through ITRANS -- digits
# and symbols aren't part of any romanization scheme and would either
# be silently dropped or transliterated character-by-character into
# nonsense. These are isolated and protected before anything else runs.
_CURRENCY_SYMBOL_PATTERN = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")
_NUMBER_TOKEN_PATTERN = re.compile(r"^[\d,]+(?:\.\d+)?%?$")

# Splits on whitespace while KEEPING the whitespace as its own token
# (via the capturing group), so reassembly with "".join() preserves
# original spacing exactly.
_WORD_SPLIT_PATTERN = re.compile(r"(\s+)")

# Extracts (leading punctuation, core word, trailing punctuation) from
# a single token, e.g. "hai." -> ("", "hai", "."). Tokens with embedded
# punctuation (e.g. hyphenated customer IDs, "didn't") won't match this
# strict single-word pattern and are deliberately left untouched rather
# than partially mangled -- see README.md "Limitations".
_TOKEN_PATTERN = re.compile(r"^([^\w]*)(\w+)([^\w]*)$", re.UNICODE)


def to_devanagari_for_voice(text: str) -> str:
    """
    Converts Romanized Hinglish text into Devanagari script, for Piper
    voice synthesis only.

    Args:
        text: Romanized Hinglish text (e.g. "Hi Rohan, aapka payment
            fail ho gaya hai. Amount: ₹1,499").

    Returns:
        Best-effort Devanagari-script text, or the original `text`
        unchanged if transliteration is unavailable or fails entirely.
        Never raises.
    """
    if not text or not text.strip():
        return text

    if not _TRANSLITERATION_AVAILABLE:
        print(
            "[Transliteration] WARNING: `indic-transliteration` is not "
            "installed; returning original Latin-script text unchanged. "
            "Install with: pip install indic-transliteration"
        )
        return text

    try:
        working_text = _CURRENCY_SYMBOL_PATTERN.sub(r"\1 रुपये", text)

        tokens = _WORD_SPLIT_PATTERN.split(working_text)
        converted_tokens: List[str] = [
            _convert_token(token) if token.strip() else token
            for token in tokens
        ]
        return "".join(converted_tokens)

    except Exception as exc:  # noqa: BLE001 - fail-safe: never break the message pipeline
        print(
            f"[Transliteration] WARNING: Unexpected error during "
            f"transliteration ({exc}). Returning original text unchanged."
        )
        return text


def _convert_token(token: str) -> str:
    """Converts a single whitespace-delimited token, preserving any
    leading/trailing punctuation attached to the word itself."""
    match = _TOKEN_PATTERN.match(token)
    if not match:
        # Pure punctuation, an emoji, a hyphenated ID, a contraction, or
        # something else with embedded punctuation -- nothing safe to
        # transliterate, so it's left exactly as-is.
        return token

    prefix, word, suffix = match.groups()
    lower_word = word.lower()

    if _NUMBER_TOKEN_PATTERN.match(word):
        return token  # digits: leave untouched, see module docstring

    if lower_word in _GREETING_MAP:
        return prefix + _GREETING_MAP[lower_word] + suffix

    if lower_word in DOMAIN_LEXICON:
        return prefix + DOMAIN_LEXICON[lower_word] + suffix

    try:
        converted = transliterate(word, sanscript.ITRANS, sanscript.DEVANAGARI)
        return prefix + converted + suffix
    except Exception as exc:  # noqa: BLE001 - fail-safe: one bad word must not break the message
        print(
            f"[Transliteration] WARNING: Could not transliterate word "
            f"{word!r} ({exc}). Keeping original spelling for this word."
        )
        return token
