import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

CFG_PATH = Path(__file__).parent / "config.json"


def _cfg() -> dict:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


class MuteGuard(Star):
    """群禁言期间不调用 LLM：禁言通知实时记账，未知状态按需查一次成员信息。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # gid -> {"muted_until": timestamp, "checked_at": timestamp}
        self._state: dict[str, dict] = {}

    def _muted(self, gid: str) -> bool:
        st = self._state.get(gid)
        return bool(st) and float(st.get("muted_until") or 0) > time.time()

    def _set(self, gid: str, muted_until: float) -> None:
        self._state[gid] = {"muted_until": muted_until, "checked_at": time.time()}

    def _on_notice(self, event: AstrMessageEvent, raw: dict) -> None:
        if raw.get("notice_type") != "group_ban":
            return
        gid = str(raw.get("group_id") or "")
        if not gid:
            return
        target = str(raw.get("user_id") or "")
        if target != str(event.get_self_id()):
            return
        if raw.get("sub_type") == "lift_ban":
            self._set(gid, 0)
            logger.info(f"[mute_guard] 群 {gid} 解除禁言，恢复接话")
            return
        duration = int(raw.get("duration") or 0)
        until = time.time() + (duration if duration > 0 else 1800)
        self._set(gid, until)
        logger.info(f"[mute_guard] 群 {gid} 被禁言 {duration or 1800}s，这期间不调 LLM")

    async def _refresh(self, event: AstrMessageEvent, gid: str) -> None:
        try:
            info = await event.bot.call_action(
                "get_group_member_info",
                group_id=int(gid),
                user_id=int(event.get_self_id()),
                no_cache=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[mute_guard] 查询禁言状态失败 {e}")
            return
        if not isinstance(info, dict):
            return
        try:
            ts = int(info.get("shut_up_timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        self._set(gid, ts if ts > time.time() else 0)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def guard(self, event: AstrMessageEvent):
        cfg = _cfg()
        if not cfg.get("enabled", True):
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("post_type") == "notice":
            self._on_notice(event, raw)
            return

        gid = str(event.get_group_id() or "")
        if not gid:
            return

        if self._muted(gid):
            logger.info(f"[mute_guard] 群 {gid} 仍在禁言，跳过本条（不调 LLM）")
            event.stop_event()
            return

        st = self._state.get(gid)
        refresh = int(cfg.get("refresh_seconds", 300) or 300)
        if st is None or time.time() - float(st.get("checked_at") or 0) > refresh:
            await self._refresh(event, gid)
            if self._muted(gid):
                logger.info(f"[mute_guard] 群 {gid} 处于禁言（接口确认），跳过本条")
                event.stop_event()
