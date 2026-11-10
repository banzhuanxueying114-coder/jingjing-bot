import json
from pathlib import Path
import random

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

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

# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

# 管理员在群里“没有 @ 机器人”时被主动回复的概率。
# 普通群友的概率由 WebUI 配置里的“主动回复 / 回复概率”控制（当前 0.1）。
OWNER_PROBABILITY = _CFG.get("owner_probability", 0.5)


class OwnerBoost(Star):
    """让管理员（AstrBot 配置中的 admins_id）在群里没 @ 机器人时也有更高概率被回复。"""

    def __init__(self, context: Context):
        super().__init__(context)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def boost_owner(self, event: AstrMessageEvent):
        """管理员发言未 @ 时，按概率把消息标记为“已唤醒”，交给默认 LLM 链路回复。"""
        if event.is_at_or_wake_command:
            return
        if getattr(event, "role", "member") != "admin":
            return
        if not event.message_str.strip():
            return
        prob = OWNER_PROBABILITY * (0.5 if _liang() else 1.0)
        if random.random() < prob:
            event.is_at_or_wake_command = True
            # 同样是插话而不是被叫。不标的话下游注入会写“跟你说话的是某人”，
            # 模型就把别人的问题当成问自己。
            event.set_extra("_interject", True)
            logger.info(
                f"owner_boost: 管理员 {event.get_sender_id()} 的群消息触发回复",
            )
