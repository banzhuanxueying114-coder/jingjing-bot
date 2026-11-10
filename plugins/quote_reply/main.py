"""Quote the message it answers only when the group has moved on.

框架的 platform_settings.reply_with_quote 会给每条回复都加引用，而且是在插件
钩子之后才加（result_decorate/stage.py 的引用回复分支），插件删不掉。群里
大部分时候用不着引用——它答的就是刚说完的那句，多一个引用只是多一条灰条。
这个插件把开关接管过来：全局关掉，自己记账每个群最新一条消息是谁，只有当
触发它的那条已经不是最新时（中间有人插话了，或者中途又进来几条）才补上
引用，免得答的话挂不到人。私聊不引用。
"""

import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star

# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

# 一个群多久没动静就忘掉它的最新消息（秒）。
STALE = _CFG.get("stale_seconds", 1800)


class QuoteReply(Star):
    """默认不加引用，只在答的不是最新那条时补一个。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # group id -> (latest message id, when it was seen)
        self._latest: dict[str, tuple[str, float]] = {}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2000)
    async def track(self, event: AstrMessageEvent):
        """记下这个群最新的一条消息，用来判断回复时要不要引用。

        Args:
            event: Every incoming group message.
        """
        gid = str(event.get_group_id() or "")
        if not gid:
            return
        mid = str(getattr(event.message_obj, "message_id", "") or "")
        if not mid:
            return
        now = time.time()
        # 顺手清掉很久没动静的群，免得这个字典一直长
        for key, (_, seen) in list(self._latest.items()):
            if now - seen > STALE:
                self._latest.pop(key, None)
        self._latest[gid] = (mid, now)

    def needed(self, gid: str, mid: str) -> bool:
        """Tell whether this reply should quote the message it answers.

        Args:
            gid: Group id, empty for private chats.
            mid: The message id this reply answers.

        Returns:
            ``True`` only when a newer message has been seen in the same group.
        """
        if not gid or not mid:
            return False
        latest = self._latest.get(gid)
        return bool(latest and latest[0] != mid)

    @filter.on_decorating_result()
    async def quote(self, event: AstrMessageEvent):
        """Put the quote back when the answered message is no longer the newest.

        Args:
            event: The message event carrying the model result.
        """
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        if not result.is_llm_result():
            return
        # 全局开关要是被 WebUI 又打开了，框架已经加过，别加第二个
        if any(isinstance(comp, Reply) for comp in result.chain):
            return
        gid = str(event.get_group_id() or "")
        mid = str(getattr(event.message_obj, "message_id", "") or "")
        if not self.needed(gid, mid):
            return
        result.chain.insert(0, Reply(id=mid))
        logger.debug(f"[quote_reply] {gid} 答的 {mid} 已经不是最新，补引用")
