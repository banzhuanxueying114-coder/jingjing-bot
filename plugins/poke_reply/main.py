import json
from pathlib import Path
import random
import time

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

# 同一个人在同一群两次“拍”之间的最小响应间隔（秒）
COOLDOWN = _CFG.get("cooldown", 20)


class PokeReply(Star):
    """被“拍一拍”时回应一句（带上当前群的会话，保证人设一致）。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self._last: dict[str, float] = {}

    async def _conversation(self, event: AstrMessageEvent):
        """取当前会话（没有就建一个），这样人设/上下文跟群里一致。"""
        cm = self.context.conversation_manager
        umo = event.unified_msg_origin
        cid = await cm.get_curr_conversation_id(umo)
        conv = await cm.get_conversation(umo, cid) if cid else None
        if conv is None:
            cid = await cm.new_conversation(umo, event.get_platform_id())
            conv = await cm.get_conversation(umo, cid)
        return conv

    async def _name_sender(self, event: AstrMessageEvent) -> None:
        """Fill in the poking member's real name.

        A poke arrives as a notice event, and the adapter there sets the
        nickname to the QQ number, so anything that reads the sender name ends
        up reading digits. Left alone, the model writes the number straight into
        its reply ("1831085480，你这手是拍上瘾了？"). Asking the group member
        list once per poke costs one API call and gives a real name back.

        Args:
            event: The poke event.
        """
        sender = getattr(event.message_obj, "sender", None)
        if sender is None:
            return
        name = getattr(sender, "nickname", "") or ""
        uid = str(getattr(sender, "user_id", "") or "")
        if name and not name.isdigit():
            return
        gid = str(event.get_group_id() or "")
        if not gid or not uid:
            return
        try:
            info = await event.bot.call_action(
                "get_group_member_info",
                group_id=int(gid),
                user_id=int(uid),
                no_cache=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[poke] cannot read member name: {e}")
            return
        if not isinstance(info, dict):
            return
        real = str(info.get("card") or info.get("nickname") or "").strip()
        if real:
            sender.nickname = real

    @filter.event_message_type(filter.EventMessageType.ALL, priority=0)
    async def on_poke(self, event: AstrMessageEvent):
        try:
            msgs = event.message_obj.message or []
        except Exception:
            return
        pokes = [m for m in msgs if type(m).__name__ == "Poke"]
        if not pokes:
            return

        self_id = str(event.get_self_id())
        if not any(str(getattr(p, "id", "")) == self_id for p in pokes):
            return

        key = f"{event.get_group_id() or ''}:{event.get_sender_id()}"
        now = time.time()
        if now - self._last.get(key, 0) < COOLDOWN:
            return
        self._last[key] = now

        if _liang() and random.random() < 0.5:
            return

        await self._name_sender(event)

        try:
            conv = await self._conversation(event)
            yield event.request_llm(
                prompt="（有人拍了你一下，没有任何文字）",
                conversation=conv,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"poke: 生成回复失败 {e}")
            yield event.plain_result("别拍。")
