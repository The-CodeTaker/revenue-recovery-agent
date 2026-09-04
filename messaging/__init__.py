"""
messaging package
------------------
Exposes the Revenue Recovery Agent's messaging-template public API:

    from messaging import (
        MESSAGE_TEMPLATES,
        render_message,
        ESCALATION_STAGES_IN_ORDER,
        ESCALATION_STAGE_DAY_OFFSETS,
    )

Note: `HinglishAgent` and `VoiceAgent` are intentionally NOT re-exported
here. They pull in extra runtime/system dependencies (the `requests`
library and a local `piper` executable, respectively) that not every
caller needs -- keeping them out of this package's `__init__` means
importing `messaging` for its templates alone never requires those
optional dependencies to be present. Import them directly from their
submodules when needed, e.g.:

    from messaging.hinglish_agent import HinglishAgent
    from messaging.voice_agent import VoiceAgent
"""

from messaging.templates import (
    ESCALATION_STAGE_DAY_OFFSETS,
    ESCALATION_STAGES_IN_ORDER,
    MESSAGE_TEMPLATES,
    render_message,
)

__all__ = [
    "MESSAGE_TEMPLATES",
    "render_message",
    "ESCALATION_STAGES_IN_ORDER",
    "ESCALATION_STAGE_DAY_OFFSETS",
]
