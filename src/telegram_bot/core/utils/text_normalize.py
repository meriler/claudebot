"""User-input text normalization applied before a prompt reaches the engine.

Currently only the thinking-trigger fixup: when dictating by voice, "ultrathink"
(Claude Code's max thinking-budget keyword) gets transcribed as two words
"ultra think", which Claude Code no longer recognizes as the trigger. We glue it
back together on the way in. Latin-only, any case, one or more whitespace chars
between the two parts.
"""

from __future__ import annotations

import re

# `ultra` + whitespace + `think`, case-insensitive, latin only. \b guards avoid
# matching inside larger words. \s+ also folds an accidental double space or a
# line break landing between the two parts.
_THINKING_TRIGGER_RE = re.compile(r"\bultra\s+think\b", re.IGNORECASE)


def normalize_thinking_trigger(text: str) -> str:
    """Glue a dictated "ultra think" back into the "ultrathink" trigger.

    Replacement is always lowercase "ultrathink" — that is the literal keyword
    Claude Code matches. Already-correct "ultrathink" (no space) is left as-is.
    """
    return _THINKING_TRIGGER_RE.sub("ultrathink", text)
