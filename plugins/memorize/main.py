import asyncio
import json
import re
import time
import uuid
from collections import deque
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

DATA = Path(__file__).parent / "notes.json"

# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

MAX_NOTES = _CFG.get("max_notes", 4)
MAX_NOTE_LEN = _CFG.get("max_note_len", 60)
PROFILE_CAP = _CFG.get("profile_cap", 2500)  # 每人印象档案上限（字符）
INJECT_TAIL = _CFG.get("inject_tail", 500)  # 平时注入印象的末尾长度
INJECT_TAIL_DEEP = _CFG.get("inject_tail_deep", 1200)  # 被问到“印象/了解我”时的注入长度

# 自动印象：每 N 条消息给这个人做一次极短提取（一次约 250 token）
AUTO_IMPRESSION = _CFG.get("auto_impression", True)
AUTO_EVERY = _CFG.get("auto_every", 20)
AUTO_COOLDOWN = _CFG.get("auto_cooldown", 7200)  # 同一人两小时内最多提取一次

# 群印象：这一群人最近在聊什么、发生过什么事。人记在 users，群记在 groups。
# 提取比个人印象更省着来——群的题材变得慢，聊四十条才值得看一眼。
AUTO_GROUP = _CFG.get("auto_group", True)
GROUP_EVERY = _CFG.get("group_every", 40)
GROUP_COOLDOWN = _CFG.get("group_cooldown", 14400)  # 同一个群四小时内最多提取一次
GROUP_CAP = _CFG.get("group_cap", 4)  # 一个群最多记几条
GROUP_WINDOW = _CFG.get("group_window", 30)  # 提取时往回看多少条群聊
GROUP_LINE_LEN = _CFG.get("group_line_len", 60)  # 单条上限

DEEP_HINTS = ("印象", "了解我", "我是谁", "还记得", "记得我", "关于我", "啥样的人")

NAME_RE = re.compile(r"我叫\s*([\u4e00-\u9fa5A-Za-z0-9_]{1,8})")
NAME_STOP = ("啥", "什么", "谁", "哪", "吗", "嘛", "？", "?")


class Memorize(Star):
    """按 QQ 号分别记忆：短记忆 + 印象档案（最多 2500 字），可自动积累。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.data = self._load()
        self._counts: dict[str, int] = {}
        self._recent: dict[str, deque] = {}
        self._last_extract: dict[str, float] = {}
        self._group_counts: dict[str, int] = {}
        self._group_recent: dict[str, deque] = {}
        self._last_group: dict[str, float] = {}

    # ---------- 存取 ----------

    def _load(self) -> dict:
        try:
            raw = json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            return {"users": {}, "groups": {}}
        users = {}
        for uid, val in (raw.get("users") or {}).items():
            if isinstance(val, list):  # 兼容旧格式
                users[uid] = {"notes": val, "profile": ""}
            elif isinstance(val, dict):
                users[uid] = {
                    "notes": list(val.get("notes") or []),
                    "profile": str(val.get("profile") or ""),
                }
        groups = {}
        for gid, val in (raw.get("groups") or {}).items():
            groups[gid] = (
                list(val) if isinstance(val, list) else list(val.get("notes") or [])
            )
        return {"users": users, "groups": groups}

    def _save(self) -> None:
        try:
            DATA.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"memorize: 保存失败 {e}")

    def _user(self, uid: str) -> dict:
        return self.data["users"].setdefault(uid, {"notes": [], "profile": ""})

    def _add_note(self, uid: str, text: str) -> None:
        notes = self._user(uid)["notes"]
        if text not in notes:
            notes.append(text)
            del notes[:-MAX_NOTES]
            self._save()

    def _append_profile(self, uid: str, text: str) -> int:
        text = text.strip()
        if not text:
            return len(self._user(uid)["profile"])
        u = self._user(uid)
        prof = (u["profile"] + "\n" + text).strip()
        if len(prof) > PROFILE_CAP:
            prof = prof[-PROFILE_CAP:]
        u["profile"] = prof
        self._save()
        return len(prof)

    def _add_group_note(self, gid: str, text: str) -> None:
        """Add one durable fact about a group, dropping the oldest when full."""
        text = text.strip()[:GROUP_LINE_LEN]
        if not text:
            return
        lst = self.data["groups"].setdefault(gid, [])
        if text in lst:
            return
        lst.append(text)
        del lst[:-GROUP_CAP]
        self._save()

    # ---------- 命令 ----------

    @filter.command("记住")
    async def remember(self, event: AstrMessageEvent):
        text = re.sub(r"^/?记住\s*", "", event.message_str.strip()).strip()
        if not text:
            yield event.plain_result("记住啥？说清楚。")
            return
        self._add_note(str(event.get_sender_id()), text[:MAX_NOTE_LEN])
        yield event.plain_result("记下了。")

    @filter.command("记住群")
    async def remember_group(self, event: AstrMessageEvent):
        if event.role != "admin" or not event.get_group_id():
            yield event.plain_result("你谁啊，也配改群记忆。")
            return
        text = re.sub(r"^/?记住群\s*", "", event.message_str.strip()).strip()
        if not text:
            yield event.plain_result("说事。")
            return
        lst = self.data["groups"].setdefault(str(event.get_group_id()), [])
        if text not in lst:
            lst.append(text[:MAX_NOTE_LEN])
            del lst[:-MAX_NOTES]
            self._save()
        yield event.plain_result("行，记群上了。")

    @filter.command("印象")
    async def impression(self, event: AstrMessageEvent):
        text = re.sub(r"^/?印象\s*", "", event.message_str.strip()).strip()
        uid = str(event.get_sender_id())
        if text:
            n = self._append_profile(uid, text[:500])
            yield event.plain_result(f"记你账上了。（{n}/{PROFILE_CAP} 字）")
            return
        prof = self._user(uid)["profile"]
        if not prof:
            yield event.plain_result("对你能有什么印象，一片空白。")
            return
        yield event.plain_result(f"（{len(prof)}/{PROFILE_CAP} 字）……{prof[-200:]}")

    @filter.command("忘掉")
    async def forget(self, event: AstrMessageEvent):
        kw = re.sub(r"^/?忘掉\s*", "", event.message_str.strip()).strip()
        uid = str(event.get_sender_id())
        u = self._user(uid)
        old_notes, old_prof = len(u["notes"]), len(u["profile"])
        if kw:
            u["notes"] = [x for x in u["notes"] if kw not in x]
            u["profile"] = "\n".join(
                ln for ln in u["profile"].splitlines() if kw not in ln
            )
        if len(u["notes"]) == old_notes and len(u["profile"]) == old_prof:
            yield event.plain_result("没这条。")
            return
        self._save()
        yield event.plain_result("忘了。")

    # ---------- 自动 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_name(self, event: AstrMessageEvent):
        m = NAME_RE.search(event.message_str or "")
        if not m:
            return
        name = m.group(1)
        if any(stop in name for stop in NAME_STOP):
            return
        uid = str(event.get_sender_id())
        self._user(uid)["notes"] = [
            x for x in self._user(uid)["notes"] if not x.startswith("自称叫")
        ]
        self._add_note(uid, f"自称叫{name}")

    @filter.on_llm_response()
    async def auto_impression(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        if not AUTO_IMPRESSION:
            return
        uid = str(event.get_sender_id())
        self._counts[uid] = self._counts.get(uid, 0) + 1
        # 缓存这个人最近几条发言，提取时一起看，避免漏掉事实
        buf = self._recent.setdefault(uid, deque(maxlen=6))
        if event.message_str:
            buf.append(event.message_str[:80])
        if self._counts[uid] % AUTO_EVERY != 0:
            return
        now = time.time()
        if now - self._last_extract.get(uid, 0) < AUTO_COOLDOWN:
            return
        self._last_extract[uid] = now
        asyncio.create_task(
            self._extract(
                event.unified_msg_origin,
                uid,
                event.get_sender_name(),
                "\n".join(buf),
                response,
            )
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def watch_group(self, event: AstrMessageEvent):
        """Track what a group is talking about, whether or not the bot answers."""
        if not AUTO_GROUP:
            return
        gid = str(event.get_group_id() or "")
        text = (event.message_str or "").strip()
        if not gid or not text or text.startswith("/"):
            return
        uid = str(event.get_sender_id() or "")
        # Messages the bot itself sent would otherwise dominate the buffer.
        if not uid or uid == str(event.get_self_id() or ""):
            return
        buf = self._group_recent.setdefault(gid, deque(maxlen=GROUP_WINDOW))
        # Poke notices carry the QQ number as the nickname, and a group memory
        # full of digits is unreadable; skip the name rather than write it down.
        nick = str(event.get_sender_name() or "").strip()
        if nick.isdigit() or nick == uid:
            nick = ""
        buf.append(f"{nick or '有人'}：{text[:60]}")
        self._group_counts[gid] = self._group_counts.get(gid, 0) + 1
        if self._group_counts[gid] % GROUP_EVERY != 0:
            return
        now = time.time()
        if now - self._last_group.get(gid, 0) < GROUP_COOLDOWN:
            return
        self._last_group[gid] = now
        asyncio.create_task(
            self._extract_group(event.unified_msg_origin, gid, list(buf))
        )

    async def _extract_group(self, umo: str, gid: str, lines: list[str]) -> None:
        """One short call: what is this group about lately.

        Args:
            umo: Unified message origin, used to pick the provider for this chat.
            gid: Group id the notes belong to.
            lines: Buffered group chatter, newest last.
        """
        try:
            provider = await self.context.get_using_provider_async(umo)
            if not provider:
                return
            known = "；".join(self.data["groups"].get(gid) or []) or "（还没有）"
            prompt = (
                f"这个群已知的事：{known}\n"
                f"最近这群人在聊：\n" + "\n".join(lines) + "\n"
                "用不超过 40 字写一条关于这个群的事：这帮人是什么人、常聊什么、"
                "最近在提什么。只写一条，具体，别写“气氛融洽”这种空话，别复述原话。"
            )
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=uuid.uuid4().hex,
                system_prompt="你是一个安静的观察者，只输出一条简短、具体的信息。",
            )
            self._add_group_note(gid, resp.completion_text or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"memorize: 群印象失败 {e}")

    async def _extract(
        self, umo: str, uid: str, name: str, text: str, response
    ) -> None:
        """一次极短调用：把这次互动压成一条印象。"""
        try:
            provider = await self.context.get_using_provider_async(umo)
            if not provider:
                return
            reply = (response.completion_text or "").strip()[:150]
            prompt = (
                f"群友「{name}」最近说过：\n{(text or '')[:400]}\n"
                f"你回复：{reply}\n"
                "用不超过 40 字，提取关于这个人的事实（名字、所在地、喜好、近况）和性格，"
                "只写一条，不要评价好坏，不要复述原话。"
            )
            resp = await provider.text_chat(
                prompt=prompt,
                session_id=uuid.uuid4().hex,
                system_prompt="你是一个安静的观察者，只输出一条简短、具体的信息。",
            )
            self._append_profile(uid, (resp.completion_text or "")[:120])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"memorize: 自动印象失败 {e}")

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        uid = str(event.get_sender_id())
        gid = str(event.get_group_id() or "")
        name = str(event.get_sender_name() or "").strip()
        # A poke notice names the member by QQ number; writing that into the
        # prompt invites the model to read the digits out loud.
        if not name or name.isdigit() or name == uid:
            name = "有人"

        # 给当前这条消息打上发言人标识，避免多个群友被混成一个人
        if req.prompt and not req.prompt.startswith("["):
            req.prompt = f"[{name}({uid})] {req.prompt}"

        u = self._user(uid)
        deep = any(h in event.message_str for h in DEEP_HINTS)

        lines = []
        if u["notes"]:
            lines.append("关于这个人：" + "；".join(u["notes"]))
        if u["profile"]:
            tail = INJECT_TAIL_DEEP if deep else INJECT_TAIL
            lines.append("你对他的印象：" + u["profile"][-tail:])
        if gid and self.data["groups"].get(gid):
            lines.append("这个群：" + "；".join(self.data["groups"][gid]))
        if not lines:
            return

        # Memory is per speaker, so it belongs to the tail of the current user
        # message. Writing it into the system prompt (messages[0]) makes every
        # turn differ at position 0 and wipes the history prefix cache.
        # mark_as_temp: keeping it out of the saved history also stops old turns
        # from replaying "how you feel about <someone else>" every turn.
        req.extra_user_content_parts.append(
            TextPart(
                text="[你记得的事]\n"
                + "\n".join(lines)
                + "\n自然地用上，别生硬复述。"
            ).mark_as_temp()
        )
