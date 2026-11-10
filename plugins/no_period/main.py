import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star


def strip_periods(text: str) -> str:
    """去掉句号：行尾直接删，句中换行，避免一逗到底读起来不通顺。"""
    if not text:
        return text
    t = re.sub(r"[。.]+(?=\n|$)", "", text)
    t = t.replace("。", "\n")
    t = re.sub(r"，(?=\n|$)", "", t)
    t = re.sub(r"[ \t]+(?=\n)", "", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


class NoPeriod(Star):
    """所有回复去掉句号（句中人机感太重）。"""

    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_llm_response()
    async def clean(self, event: AstrMessageEvent, response: LLMResponse):
        if response.completion_text:
            response.completion_text = strip_periods(response.completion_text)
        chain = getattr(response, "result_chain", None)
        if chain and getattr(chain, "chain", None):
            for comp in chain.chain:
                if isinstance(comp, Plain) and getattr(comp, "text", None):
                    comp.text = strip_periods(comp.text)
