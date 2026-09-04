
"""
templates.py
------------
Strict, hand-written fallback message templates for the Revenue Recovery
Agent's customer-facing communications, in natural Hinglish.

Why hardcoded templates (and not an LLM) live here:
    This module is deliberately NOT LLM-generated content. It's the
    deterministic fallback tier that:
      1. Always works, even if the (future) Ollama/Piper voice pipeline
         is down, rate-limited, or not yet integrated.
      2. Gives legal/compliance a fixed, auditable set of strings that
         will never unpredictably vary between customers or runs.
      3. Acts as a stylistic anchor -- when the LLM-based generation
         layer is added later, these templates are the "known good"
         examples it should sound like.

Escalation model:
    Each failed payment that isn't recovered quickly moves through an
    escalating sequence of tone, urgency, and incentive as time passes
    without resolution:

        Day 1  -> soft_reminder   : friendly, assumes it's an honest mistake
        Day 3  -> firm_reminder   : more direct, names consequences plainly
        Day 7  -> offer_discount  : de-escalates via incentive to win back
                                     engagement before churn risk increases
        Day 14 -> final_notice    : last-chance, clearly states the
                                     subscription will be suspended/cancelled

Placeholder contract:
    Every template uses EXACTLY these three placeholders (via Python's
    str.format()):
        {name}   - the customer's first name / display name
        {amount} - the failed charge amount, pre-formatted by the caller
                   (e.g. "499" or "1,499.00" -- this module does not
                   perform currency formatting itself)
        {plan}   - the subscription plan name (e.g. "Pro Monthly")

Usage:
    from messaging.templates import MESSAGE_TEMPLATES, render_message

    message = render_message(
        stage="soft_reminder",
        name="Rohan",
        amount="1,499",
        plan="Pro Monthly",
    )
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# The four escalation-stage templates.
#
# Style notes (why these read the way they do):
#   - Natural code-switching (Hinglish), not a literal word-for-word
#     translation of an English template -- phrases like "fail ho gaya
#     hai", "thoda check kar lijiyega", "hum samajhte hain" are how this
#     actually gets said/written colloquially, not stiff textbook Hindi.
#   - Tone escalates stage over stage: soft_reminder is warm and assumes
#     good faith ("shayad", "ho sakta hai"); firm_reminder is direct and
#     names the consequence plainly; offer_discount re-opens the
#     relationship with empathy + incentive; final_notice is unambiguous
#     about what happens next, while still remaining respectful.
#   - Professional, not casual/slangy -- suitable for a real customer
#     communication channel (SMS/WhatsApp/email/voice script), not chat
#     banter.
# --------------------------------------------------------------------------

MESSAGE_TEMPLATES: dict[str, str] = {
    "soft_reminder": (
        "Hi {name}, aapka {plan} ka payment of ₹{amount} fail ho gaya hai. "
        "Ho sakta hai ye ek chhoti si technical ya bank-side dikkat ho. "
        "Koi baat nahi -- hum jaldi hi dobara try karenge, lekin aap chahein "
        "to abhi apna payment method bhi check kar sakte hain. Kisi bhi "
        "sawaal ke liye hum yahin hain!"
    ),
    "firm_reminder": (
        "Hi {name}, aapka {plan} subscription ka ₹{amount} ka payment "
        "pichle kuch dino se pending hai aur ab tak resolve nahi ho paya "
        "hai. Hum samajhte hain ki kabhi-kabhi ye cheezein miss ho jaati "
        "hain, lekin agar ye jaldi update nahi hota to aapki service mein "
        "rukaavat aa sakti hai. Kripya apna payment method jald se jald "
        "update kar lijiye taaki aapka {plan} bina kisi interruption ke "
        "chalta rahe."
    ),
    "offer_discount": (
        "Hi {name}, hum nahi chahte ki aap apna {plan} kho dein. Aapka "
        "₹{amount} ka payment abhi bhi pending hai, isliye is baar hum "
        "aapke liye ek special discount laaye hain agar aap abhi apna "
        "payment complete kar lein. Ye ek chhota sa thank-you hai aapke "
        "saath bane rehne ke liye -- bas neeche diye gaye link se apna "
        "payment update kariye aur discount turant apply ho jaayega."
    ),
    "final_notice": (
        "Hi {name}, ye aapke {plan} subscription ke liye final reminder "
        "hai. ₹{amount} ka payment abhi tak fail hi hai, aur agar ye "
        "agle 48 ghanton mein resolve nahi hota to aapka subscription "
        "temporarily suspend kar diya jaayega. Hum aapko is process se "
        "guzarte nahi dekhna chahte -- kripya abhi apna payment method "
        "update karein taaki aapki service bina kisi rukaavat ke jaari "
        "rahe."
    ),
}

# Ordered list of valid stage keys, useful for callers that need to
# iterate through the escalation sequence in order (e.g. a scheduler that
# decides which stage a given payment has reached).
ESCALATION_STAGES_IN_ORDER: tuple[str, ...] = (
    "soft_reminder",
    "firm_reminder",
    "offer_discount",
    "final_notice",
)

# Maps each stage to the number of days-since-failure it's intended to be
# sent on. Kept alongside the templates (rather than only living in a
# scheduler module) so the "Day N" intent of each template is always
# discoverable directly from this file.
ESCALATION_STAGE_DAY_OFFSETS: dict[str, int] = {
    "soft_reminder": 1,
    "firm_reminder": 3,
    "offer_discount": 7,
    "final_notice": 14,
}


def escalation_stage_for_attempts(prior_attempts: int) -> str:
    """
    Maps a payment's `prior_attempts` count to the escalation stage it
    should be messaged at, clamped to the valid stage range. Shared by
    `core/simulator.py` (batch run) and `dashboard/app.py` (live
    single-customer run) so the two call sites can never drift apart on
    which stage a given prior_attempts count maps to.
    """
    index = max(0, min(int(prior_attempts), len(ESCALATION_STAGES_IN_ORDER) - 1))
    return ESCALATION_STAGES_IN_ORDER[index]


def render_message(stage: str, name: str, amount: str, plan: str) -> str:
    """
    Renders the message template for a given escalation stage with the
    provided customer details substituted in.

    Args:
        stage: One of "soft_reminder", "firm_reminder", "offer_discount",
            "final_notice" (see ESCALATION_STAGES_IN_ORDER).
        name: Customer's display name, e.g. "Rohan".
        amount: Pre-formatted amount string, e.g. "1,499" or "499.00".
            This function does not format currency itself -- pass in
            exactly the string you want to appear after the ₹ symbol.
        plan: Subscription plan name, e.g. "Pro Monthly".

    Returns:
        The fully rendered message string, ready to send.

    Raises:
        KeyError: if `stage` is not a recognized escalation stage.
    """
    if stage not in MESSAGE_TEMPLATES:
        valid_stages = ", ".join(ESCALATION_STAGES_IN_ORDER)
        raise KeyError(
            f"Unknown escalation stage '{stage}'. Must be one of: {valid_stages}."
        )

    template = MESSAGE_TEMPLATES[stage]
    return template.format(name=name, amount=amount, plan=plan)


