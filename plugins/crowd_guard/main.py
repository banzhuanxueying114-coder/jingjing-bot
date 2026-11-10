"""Scale the reply probability down as a group gets bigger.

在几百人的群里，6% 的主动接话一晚上也能刷出几十条。这个插件按群人数打折：
每 400 人把「非 @"的回复概率再砍一半（400 人 0.5、800 人 0.25……），
被 @ 的那条永远放行。人数有缓存，不会每条都去问协议端。
"""

import json
import random
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star

CFG_PATH = Path(__file__).parent / "config.json"


def load_cfg() -> dict:
    """Read the plugin config, falling back to safe defaults."""
    try:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def factor_for(count: int, base: int) -> float:
    """Return the multiplier applied to reply odds for a group of ``count``.

    Args:
        count: Number of members in the group.
        base: How many members cost one halving.

    Returns:
        ``1.0`` below one base, otherwise ``0.5 ** (count // base)``.
    """
    if base <= 0 or count < base:
        return 1.0
    return 0.5 ** (count // base)


def at_self(event: AstrMessageEvent) -> bool:
    """Tell whether the message really mentions the bot (not a faked wake).

    Args:
        event: The incoming message event.

    Returns:
        ``True`` when the raw chain contains an At addressed to the bot.
    """
    self_id = str(event.get_self_id() or "")
    if not self_id:
        return False
    for comp in event.message_obj.message or []:
        if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
            return True
    return False


class CrowdGuard(Star):
    """把大群的「非 @」回复概率按人数折半。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # gid -> (member_count, checked_at)
        self._size: dict[str, tuple[int, float]] = {}

    async def _members(self, event: AstrMessageEvent, gid: str, ttl: int) -> int:
        """Return the group size, cached for ``ttl`` seconds.

        Args:
            event: Event used to reach the protocol adapter.
            gid: Group id as a string.
            ttl: How long a cached number stays valid.

        Returns:
            The member count, or ``0`` when it could not be fetched.
        """
        cached = self._size.get(gid)
        if cached and time.time() - cached[1] < ttl:
            return cached[0]
        try:
            info = await event.bot.call_action("get_group_info", group_id=int(gid))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[crowd_guard] 查群人数失败 {e}")
            return cached[0] if cached else 0
        count = 0
        if isinstance(info, dict):
            try:
                count = int(info.get("member_count") or 0)
            except (TypeError, ValueError):
                count = 0
        if count <= 0:
            return cached[0] if cached else 0
        self._size[gid] = (count, time.time())
        return count

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-10)
    async def guard(self, event: AstrMessageEvent):
        """将本条消息的接话概率按群规模折算，没过就掐掉。"""
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            return
        gid = str(event.get_group_id() or "")
        if not gid:
            return
        # 只有本来就会走到模型的消息才需要折算，省掉一次查询。
        if not event.is_at_or_wake_command:
            return
        if at_self(event):
            return
        base = int(cfg.get("base_size", 400) or 400)
        ttl = int(cfg.get("cache_seconds", 600) or 600)
        count = await self._members(event, gid, ttl)
        factor = factor_for(count, base)
        if factor >= 1.0:
            return
        if random.random() < factor:
            logger.info(
                f"[crowd_guard] 群 {gid} {count} 人，本条放行（概率 ×{factor:.3f}）"
            )
            return
        logger.info(f"[crowd_guard] 群 {gid} {count} 人，本条掐掉（概率 ×{factor:.3f}）")
        event.stop_event()
