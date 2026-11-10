import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

CFG_PATH = Path(__file__).parent / "config.json"
DEFAULT_CFG = {
    "enabled": True,
    "min_people": 3,
    "cooldown_seconds": 120,
    "window_seconds": 90,
    "max_text_len": 60,
}

# 纯符号/表情的消息不参与复读
_MEANINGFUL = re.compile(r"[\w\u4e00-\u9fff]")


def load_cfg() -> dict:
    try:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return dict(DEFAULT_CFG)
        cfg = dict(DEFAULT_CFG)
        cfg.update(data)
        return cfg
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_CFG)


def normalize(text: str) -> str:
    """复读判定用：去首尾空白、压缩空格、不区分中英文标点差异。"""
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def should_follow(
    store: dict,
    group: str,
    user: str,
    text: str,
    now: float,
    cfg: dict,
) -> tuple[bool, str]:
    """记录一条消息，判断该不该跟着复读。返回 (要不要发, 发什么)。"""
    if not cfg.get("enabled", True):
        return False, ""
    key = normalize(text)
    if not key or not _MEANINGFUL.search(key):
        return False, ""
    if len(key) > int(cfg.get("max_text_len", 60)):
        return False, ""
    if key.startswith("/"):
        return False, ""

    window = float(cfg.get("window_seconds", 90))
    rec = store.setdefault((group, key), {})
    # 丢掉过期的发言人
    for uid in [u for u, ts in rec.items() if now - ts > window]:
        rec.pop(uid, None)
    rec[user] = now

    if len(rec) < int(cfg.get("min_people", 3)):
        return False, ""
    return True, key


class RepeatFollow(Star):
    """三个以上的人在同一群发同一句时，跟着复读一次（每群两分钟冷却）。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self._store: dict[tuple[str, str], dict[str, float]] = {}
        self._last_follow: dict[str, float] = {}

    def _cleanup(self, now: float) -> None:
        window = float(load_cfg().get("window_seconds", 90))
        dead = [k for k, users in self._store.items() if not users or all(now - ts > window for ts in users.values())]
        for k in dead:
            self._store.pop(k, None)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            group = event.get_group_id()
            if not group:
                return
            cfg = load_cfg()
            if not cfg.get("enabled", True):
                return
            uid = str(event.get_sender_id())
            if uid == str(event.get_self_id()):
                return  # 自己发的不算
            if getattr(event, "is_at_or_wake_command", False):
                return  # 已经被 @ 了，交给正常回复，避免双重回复

            now = time.time()
            group = str(group)
            self._cleanup(now)

            ok, text = should_follow(self._store, group, uid, event.message_str, now, cfg)
            if not ok:
                return
            cooldown = float(cfg.get("cooldown_seconds", 120))
            if now - self._last_follow.get(group, 0) < cooldown:
                return

            self._last_follow[group] = now
            self._store.pop((group, normalize(text)), None)
            logger.info(f"repeat_follow: 群 {group} 复读「{text}」")
            yield event.plain_result(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"repeat_follow: {e}")
