"""Turn a model refusal into a flat "懒得喷" instead of relaying the refusal."""

import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star

# Only one group needs this: the one where the model is most likely to be
# pushed into a refusal. An empty id keeps the plugin quiet everywhere.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}
TARGET_GROUP = str(_CFG.get("group_id") or "")
FALLBACK = str(_CFG.get("fallback") or "懒得喷")

# 模型被安全策略拦住时常见的开头
MARKERS = (
    "抱歉",
    "对不起",
    "我不能",
    "无法",
    "作为 AI",
    "作为AI",
    "换个话题",
    "不合适",
    "请注意",
    "不便提供",
    "不能提供",
    "拒绝",
    "无法协助",
    "不能协助",
)


class CensorFallback(Star):
    """在指定群里，把拒答换成一句短的。"""

    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_llm_response()
    async def replace_refusal(self, event: AstrMessageEvent, response: LLMResponse):
        if not TARGET_GROUP or str(event.get_group_id() or "") != TARGET_GROUP:
            return
        text = (response.completion_text or "").strip()
        hit = (not text) or any(m in text for m in MARKERS)
        if not hit:
            return
        logger.info(f"censor_fallback: 拦截到拒答，改成「{FALLBACK}」")
        response.completion_text = FALLBACK
