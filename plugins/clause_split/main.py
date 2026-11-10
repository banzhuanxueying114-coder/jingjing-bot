"""Burst short replies at their commas: drop the comma, send two bubbles.

A comma inside a short reply is usually just a breath mark. Real people typing
in a group chat split there instead of sending one long line, so this turns
"不是哥们，你这也不行啊" into two messages. Only short replies are touched --
splitting a long paragraph into confetti would read worse than leaving it whole.
"""

import json
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

import re

# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

# More parts than this and it stops looking like typing and starts looking like spam.
MAX_PARTS = _CFG.get("max_parts", 6)
# Each clause has to be short enough to stand alone as one bubble.
MAX_PART_LEN = _CFG.get("max_part_len", 12)
# Above this length the reply is a paragraph; keep it in one piece.
MAX_TOTAL = _CFG.get("max_total", 40)
SEP = "，"
# Some models put each line of a burst on its own line instead of using commas.
SPLIT_RE = re.compile(r"[，\n]+")


def split_at_commas(text: str) -> list[str]:
    """Split a short reply at its commas (and line breaks) and drop them.

    Args:
        text: The reply text.

    Returns:
        The clauses to send separately, or ``[text]`` when the reply should stay whole.
    """
    body = (text or "").strip()
    if len(body) > MAX_TOTAL:
        return [text]
    if SEP not in body and "\n" not in body:
        return [text]
    parts = [p.strip() for p in SPLIT_RE.split(body)]
    if not 2 <= len(parts) <= MAX_PARTS:
        return [text]
    if any(not p or len(p) > MAX_PART_LEN for p in parts):
        return [text]
    return parts


class ClauseSplit(Star):
    """Turn comma-joined short clauses into separate bubbles."""

    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_decorating_result()
    async def burst(self, event: AstrMessageEvent):
        """Rewrite the outgoing chain so each short clause becomes its own bubble.

        Args:
            event: The message event carrying the model result.
        """
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        if not result.is_llm_result():
            return

        chain = []
        changed = False
        for comp in result.chain:
            if isinstance(comp, Plain):
                parts = split_at_commas(getattr(comp, "text", "") or "")
                if len(parts) > 1:
                    changed = True
                    chain.extend(Plain(p) for p in parts)
                    continue
            chain.append(comp)
        if changed:
            result.chain = chain
