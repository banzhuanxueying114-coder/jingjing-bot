import json
import random
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Face, Image
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

POOL = Path(__file__).parent / "pool"
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

LIANG_CFG = Path(__file__).parent.parent / "liang_time" / "config.json"


def _liang() -> bool:
    """当前是否处于梁文峰时间（上班时段），是则概率减半。"""
    try:
        cfg = json.loads(LIANG_CFG.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return False
    if not cfg.get("enabled", True):
        return False
    now = __import__("datetime").datetime.now()
    cur = now.hour * 60 + now.minute

    def _m(s: str) -> int:
        h, mi = str(s).split(":")
        return int(h) * 60 + int(mi)

    try:
        for a_s, b_s in cfg.get("windows", []):
            a, b = _m(a_s), _m(b_s)
            if a <= b and a <= cur < b:
                return True
            if a > b and (cur >= a or cur < b):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False

# 没有图片时用 QQ 原生表情兜底
FACE_IDS = [14, 13, 178, 212, 264, 307, 179, 182]

RANDOM_P = _CFG.get("random_p", 0.19)  # 每次回复后附带表情的概率
GROUP_COOLDOWN = _CFG.get("group_cooldown", 120)  # 同一群两次随机表情的最小间隔（秒）
MAX_PER_HOUR = _CFG.get("max_per_hour", 30)  # 每群每小时最多几次随机表情
_KW = _CFG.get("keywords")
KEYWORDS = (
    tuple(_KW)
    if isinstance(_KW, list) and _KW and all(isinstance(k, str) for k in _KW)
    else ("表情包", "发个表情", "来个表情", "出个表情", "斗图", "发图")
)
# Seconds a group must wait between keyword-triggered stickers.
KEYWORD_COOLDOWN = _CFG.get("keyword_cooldown", 15)


class Stickers(Star):
    """让机器人发 QQ 表情 / 本地表情包。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self._last: dict[str, float] = {}
        self._hours: dict[str, list[float]] = {}

    def _images(self) -> list[Path]:
        if not POOL.exists():
            return []
        # Pool is grouped into sub-folders per source, so walk the whole tree.
        return [p for p in POOL.rglob("*") if p.suffix.lower() in EXTS and p.is_file()]

    def _pick(self):
        files = self._images()
        if files:
            return Image.fromFileSystem(str(random.choice(files)))
        return Face(id=random.choice(FACE_IDS))

    def _allowed(self, gid: str) -> bool:
        now = time.time()
        if now - self._last.get(gid, 0) < GROUP_COOLDOWN:
            return False
        hist = [t for t in self._hours.get(gid, []) if now - t < 3600]
        self._hours[gid] = hist
        return len(hist) < MAX_PER_HOUR

    def _mark(self, gid: str) -> None:
        now = time.time()
        self._last[gid] = now
        self._hours.setdefault(gid, []).append(now)

    # 明确要表情包：直接发，不劳烦模型
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=0)
    async def on_keyword(self, event: AstrMessageEvent):
        text = event.message_str or ""
        if not any(k in text for k in KEYWORDS):
            return
        gid = str(event.get_group_id() or "")
        if time.time() - self._last.get(gid, 0) < KEYWORD_COOLDOWN:
            return
        if _liang() and random.random() < 0.5:
            return
        self._mark(gid)
        yield event.chain_result([self._pick()])
        event.stop_event()

    # 普通回复后，偶尔补一张
    @filter.on_llm_response()
    async def maybe_attach(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        gid = str(event.get_group_id() or "")
        if not gid or not self._allowed(gid):
            return
        prob = RANDOM_P * (0.5 if _liang() else 1.0)
        if random.random() > prob:
            return
        try:
            comp = self._pick()
            if response.result_chain is None:
                response.result_chain = MessageChain(chain=[])
            response.result_chain.chain.append(comp)
            self._mark(gid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"stickers: 附加表情失败 {e}")
