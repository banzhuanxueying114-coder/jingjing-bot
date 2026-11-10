"""Fetch a normal-rated Pixiv illustration on request and drop it in the chat.

Pixiv itself is not reachable from this machine, so the ranking/search metadata
comes from the public lolicon setu API (``r18=0`` only) and the picture itself
from the i.pixiv.re mirror that API hands back. The request is triggered by the
way people actually ask in a group -- "来张猫图", "整点风景图" -- and never by a
bare "图" in ordinary chatter.
"""

import asyncio
import json
import re
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star

CFG_PATH = Path(__file__).parent / "config.json"
CACHE_DIR = Path(__file__).parent / "cache"

API = "https://api.lolicon.app/setu/v2"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
META_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 40
MAX_TRIES = 4

# The setu API's r18=0 is only a tag flag, and "cat girl in a micro bikini"
# passes it, so candidates are screened by their tags before download. QQ
# screens the picture itself once it is posted.
BLOCK_TAGS = (
    "水着", "ビキニ", "マイクロビキニ", "下着", "パンツ", "ブラ", "裸", "半裸",
    "全裸", "ヌード", "尻", "お尻", "おっぱい", "巨乳", "爆乳", "胸", "谷間",
    "太もも", "腋", "エロ", "官能", "誘惑", "フェチ", "縛り", "触手", "スク水",
    "競泳水着", "レオタード", "ボンデージ", "露出", "乳首", "えっち", "お色気",
    "パンチラ", "密着", "催眠", "寝取", "風俗", "着エロ", "寝そべり",
    "女体", "极上女体", "極上女体", "着衣巨乳", "爆乳", "下乳", "横乳", "谷間見せ",
    "ランジェリー", "ガーター", "透け", "濡れ透け", "ボディペイント",
)
MIN_WIDTH = 500

DEFAULT_CFG = {
    "enabled": True,
    "cooldown_seconds": 10,
    "max_per_hour": 20,
    "cache_keep": 60,
}

# Explicit phrasings only: a stray "图" in normal chatter must not fire a fetch.
TRIGGERS = (
    "来张图",
    "来点图",
    "来几张图",
    "来图",
    "整点图",
    "整张图",
    "整几张图",
    "发张图",
    "发点图",
    "给张图",
    "上点图",
    "换张图",
    "有没有图",
    "有图吗",
    "来张美图",
    "来点美图",
    "整点美图",
    "来点好图",
    "来张好图",
    "图来",
)

# Topic words that are not topics at all, and quantifiers left over from
# "来只猫" / "来点图" — both would otherwise become the search tag.
_NO_TAG = {"", "点", "些", "张", "美", "好", "新", "一", "几", "两", "这", "那"}
_MEASURE = ("只", "条", "部", "首", "件", "份", "点", "些", "个")

# Words that mean the sender wants something other than a picture. "来首歌"
# and "整点活" fit the request shape, so they are rejected by what they ask for.
_OTHER_WANT = {
    "歌",
    "首歌",
    "音乐",
    "唱歌",
    "歌单",
    "笑话",
    "段子",
    "相声",
    "乐子",
    "活",
    "活路",
    "活干",
    "消息",
    "语音",
    "视频",
    "厕所",
    "人",
    "个人",
    "一发",
    "发",
}

# Pixiv tags are mostly Japanese; map the Chinese words people actually type.
TAG_MAP = {
    "猫": "猫",
    "猫咪": "猫",
    "狗": "犬",
    "风景": "風景",
    "天空": "空",
    "星空": "星空",
    "女生": "女の子",
    "女孩": "女の子",
    "男生": "男の子",
    "机甲": "メカ",
    "机械": "メカ",
    "赛博": "サイバー",
    "古风": "中国風",
    "雪": "雪",
    "海": "海",
    "花": "花",
    "夜景": "夜景",
    "雨": "雨",
}

# Words people type for a topic, longest first so "猫耳" beats "猫" and
# "双马尾" beats "马尾". The splitter uses them to cut one run ("白丝黑丝",
# "猫耳女仆") into the words the sender actually meant, and the strict shape
# "猫图" is only a request when every word in it is one of these.
_WORDS = tuple(
    sorted(
        {
            *TAG_MAP,
            "白丝", "黑丝", "白丝袜", "黑丝袜", "丝袜", "吊带袜", "过膝袜",
            "猫耳", "猫娘", "猫猫", "狗狗", "兽耳", "女仆", "女仆装", "制服",
            "水手服", "和服", "汉服", "浴衣", "泳装", "萝莉", "少女", "御姐",
            "正太", "双马尾", "马尾", "长发", "短发", "眼镜", "帽子", "围巾",
            "白毛", "银发", "金发", "黑发", "红瞳", "蓝瞳", "侧脸", "背影",
            "睡颜", "笑脸", "烟花", "樱花", "月亮", "日落", "黄昏", "雨天",
            "晴天", "冬天", "夏天", "秋天", "春天", "海边", "沙滩", "森林",
            "城市", "街道", "屋顶", "教室", "图书馆", "咖啡", "甜点",
        },
        key=len,
        reverse=True,
    )
)
MAX_WORDS = 3

# Compounds that end in 图 but are a noun of their own, not a topic: "发张截图"
# asks for a screenshot, and 截 is not what anyone wants a picture of.
_NOT_TOPIC = (
    "截图",
    "地图",
    "原图",
    "全图",
    "大图",
    "高清图",
    "动图",
    "表情包",
    "头像",
    "壁纸",
)

_KNOWN = frozenset(_WORDS)

_ASK_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我|能|能不能|可以|可不可以)?"
    r"(?:来|整|发|给|上|换|搞|要)"
    r"(?:一|两|几|个)?(?:张|点|些|个|只|条)?(?P<tag>.*?)$"
)
_HAVE_RE = re.compile(
    r"^有(?:没有|没)?(?:什么|啥)?(?P<tag>.*?)(?:图片|照片|美图|图)(?:吗|么|嘛|没)?$"
)
# The topic in front of the noun: "猫图来一张", "白丝图", "猫耳女仆图整两张".
_PREFIX_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我|能|能不能|可以|可不可以|求求|求)?"
    r"(?P<tag>.+?)(?:图片|照片|美图|图)"
    r"(?:来|整|发|给|上|换|搞|要)?(?:一|两|几)?(?:张|点|些|个)?"
    r"(?:吧|呗|呀|啊|嘛|谢谢|了)?"
    r"(?:有没有|有吗|有么)?$"
)

# "有没有风景图" / "有猫图吗" — the noun sits in the middle, so the tag has to
# be captured by what comes before it rather than by the end of the string.
_HAVE_MID_RE = re.compile(
    r"^有(?:没有|没)?(?:什么|啥)?(?P<tag>.+?)(?:图片|照片|美图|图)"
    r"(?:吗|么|嘛|没)?$"
)
_TAIL_RE = re.compile(r"(?:图片|照片|美图|好图|图|照)(?:吧|呗|嘛|啊|呀)?$")
_FILLER = {"好看", "好看点", "涩", "色", "可爱", "漂亮", "刺激", "带劲", "点赞"}

# Trailing politeness stays out of the topic: "来张猫图谢谢" asks for 猫.
_POLITE_SUFFIX = (
    "谢谢啦",
    "谢谢你",
    "谢谢",
    "谢了",
    "感谢",
    "辛苦了",
    "辛苦",
    "拜托了",
    "拜托",
    "求求你了",
    "求求你",
    "球球",
    "求了",
    "求",
    "一下吧",
    "一下",
    "大佬",
    "大哥",
    "老哥",
    "铁子",
    "呗",
    "吧",
    "鸭",
    "哇",
    "捏",
    "喵",
)


def load_cfg() -> dict:
    """Read the plugin config, falling back to defaults on a broken file."""
    try:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return dict(DEFAULT_CFG)
        cfg = dict(DEFAULT_CFG)
        cfg.update(data)
        return cfg
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_CFG)


def _strip_polite(tag: str) -> str:
    """Trim trailing politeness so it does not become part of the topic.

    Args:
        tag: The words captured after the verb, e.g. ``"猫图谢谢"``.

    Returns:
        The tag without a trailing 谢谢/吧/大佬 and with a trailing 图 removed,
        e.g. ``"猫"``. An input that is only politeness comes back as ``""``.
    """
    changed = True
    while changed and tag:
        changed = False
        for word in _POLITE_SUFFIX:
            if tag.endswith(word) and len(tag) > len(word):
                tag = tag[: -len(word)].strip()
                changed = True
                break
    return _TAIL_RE.sub("", tag).strip()


def _known(word: str) -> bool:
    """Whether a word is a topic this plugin knows how to search for."""
    return word in _KNOWN


def split_words(tag: str) -> list[str]:
    """Cut a run of topic words into the words the sender meant.

    People type the topic as one run with no separators: "白丝黑丝" is two
    topics, "猫耳女仆" is two, "猫猫" is one. The longest known word wins at
    each position, so "猫耳" is not read as "猫" + "耳".

    Args:
        tag: The run of topic words, already stripped of polite padding.

    Returns:
        Up to :data:`MAX_WORDS` words, in the order they were typed. Text that
        matches no known word is returned as a single word, so an unknown topic
        still gets searched for as typed.
    """
    left = tag.strip("的 ")
    words = []
    while left and len(words) < MAX_WORDS:
        for word in _WORDS:
            if left.startswith(word):
                words.append(word)
                left = left[len(word):]
                break
        else:
            break
    if not words:
        return [tag.strip("的 ")] if tag.strip("的 ") else []
    left = left.strip("的 ")
    if left and len(words) < MAX_WORDS:
        words.append(left)
    return words


def find_tag(text: str) -> list[str] | None:
    """Return the requested topics, an empty list for "any picture", or None.

    Three shapes count as a request: the trigger verbs ("来张猫图"), the noun
    in front ("猫图来一张", "猫图"), and 有没有 ("有没有猫图"). The topic may
    sit on either side of 图 -- "来张图猫" asks for 猫 just as "来张猫图" does.

    Args:
        text: The raw message text.

    Returns:
        ``None`` when the message is not a picture request, ``[]`` when it asks
        for a picture without a topic, otherwise the words to search for.
    """
    body = (text or "").strip().strip("。！？!?~…，, ")
    if not body:
        return None
    if body in TRIGGERS:
        return []
    match = _ASK_RE.match(body)
    tag = ""
    if match:
        tag = match.group("tag").strip()
        # The topic may sit after the noun: "来张图猫", "来张图片猫", "来张照片
        # 猫". The noun itself is not a topic, and "来张图片" / "来张照片" alone
        # ask for anything.
        for noun in ("照片", "图片"):
            if tag.startswith(noun):
                tag = tag[len(noun):].strip()
                break
        else:
            if tag.startswith("图") and len(tag) > 1:
                tag = _TAIL_RE.sub("", tag[1:]).strip()
        if tag in _POLITE_SUFFIX:
            # "来张图吧" asks for anything; 吧 is not a topic.
            return []
    else:
        match = _HAVE_MID_RE.match(body) or _PREFIX_RE.match(body)
        if not match:
            return None
        tag = match.group("tag").strip()
    tag = _TAIL_RE.sub("", tag).strip()
    tag = _strip_polite(tag).strip("的 ")
    # "来只猫" leaves the measure word in front of the topic.
    while len(tag) > 1 and tag[0] in _MEASURE:
        tag = tag[1:].strip("的 ")
    if tag in _FILLER or tag in _NO_TAG:
        return []
    if tag in _OTHER_WANT:
        # "来首歌" / "整点活" look like a request but ask for something else.
        return None
    if any(not_topic in body for not_topic in _NOT_TOPIC):
        # "发张截图" / "求个地图": the compound is the topic itself, not 截.
        return None
    words = split_words(tag)
    if not words:
        return []
    # "猫图来一张" is unambiguous, but a bare "猫图" is not a request on its
    # own unless every word in it is a topic we know ("猫图" yes, "地图" no).
    strict = match.re is _PREFIX_RE and "来" not in body and "整" not in body
    if strict and not all(_known(w) for w in words):
        return None
    if len(tag) > 6 and not all(_known(w) for w in words):
        return None
    # Without the word "图" the request must be short and concrete ("来张猫"),
    # otherwise ordinary chatter such as "给大家看" would start a fetch.
    if not any(w in body for w in ("图", "照", "美图")) and len(tag) > 2:
        if not all(_known(w) for w in words):
            return None
    return words


_EXTRA_SPLIT = re.compile(r"[\s,，、+／/]+")
_EXTRA_STOP = {"谢谢", "谢了", "辛苦", "快点", "快", "赶紧", "吧", "啊", "呀", "呗", "嘛", "求"}


def parse_request(text: str) -> tuple[list[str], list[str]] | None:
    """Split a message into the topic words and any extra tags the sender typed.

    People phrase it either as one run ("来张猫图", "白丝黑丝"), as a trigger
    followed by their own tags ("来张图 白丝 黑丝"), or with the topic in front
    ("猫图 come"). The trailing words are passed straight to the API, which is
    how a poster asks for something the blocklist would otherwise drop.

    Args:
        text: The raw message text.

    Returns:
        ``(words, extras)`` where ``words`` is empty for "anything" and
        ``extras`` holds up to four extra tags, or ``None`` when the message is
        not a picture request.
    """
    tokens = [t for t in _EXTRA_SPLIT.split((text or "").strip()) if t]
    if not tokens:
        return None
    words = find_tag(tokens[0])
    if words is None:
        return None
    extras = []
    for word in tokens[1:]:
        word = word.strip("。！？!?~…，,")
        if not word or word in _FILLER or word in _EXTRA_STOP:
            continue
        if len(word) > 20:
            continue
        extras.append(word)
    return words, extras[:4]


def blocked_tag(item: dict) -> str | None:
    """Return the first disallowed tag on a candidate, if it has one.

    Args:
        item: One result from the setu API.

    Returns:
        The offending tag, or ``None`` when the tags look harmless.
    """
    tags = {str(t).lower() for t in (item.get("tags") or [])}
    for bad in BLOCK_TAGS:
        if bad.lower() in tags:
            return bad
    return None


class Illust(Star):
    """Look up a Pixiv picture and post it when someone asks for one."""
    def __init__(self, context: Context):
        super().__init__(context)
        self._last: dict[str, float] = {}
        self._hours: dict[str, list[float]] = {}
        self._seen: list[int] = []

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1100)
    async def on_message(self, event: AstrMessageEvent):
        try:
            group = event.get_group_id()
            if not group:
                return
            cfg = load_cfg()
            if not cfg.get("enabled", True):
                return
            parsed = parse_request(event.message_str)
            if parsed is None:
                return
            words, extras = parsed
            now = time.time()
            group = str(group)
            if now - self._last.get(group, 0) < float(cfg["cooldown_seconds"]):
                return
            hours = [t for t in self._hours.get(group, []) if now - t < 3600]
            self._hours[group] = hours
            if len(hours) >= int(cfg["max_per_hour"]):
                return
            found = await self._fetch(words, extras)
            if not found:
                logger.warning(
                    "illust: no picture for words=%s extras=%s group=%s",
                    words,
                    extras,
                    group,
                )
                # Nothing came back: leave the event alone so the model can
                # answer the request, but do not touch the mood — a lookup
                # that found nothing is not something to be angry about.
                return
            self._last[group] = now
            self._hours.setdefault(group, []).append(now)
            # What the sender actually named, extras first: that is the only
            # thing worth remarking on. A random stand-in for a topic that found
            # nothing must stay silent, or the line would talk about a picture
            # that is not the one on screen.
            wanted = [] if found.get("fallback") else (list(extras) or list(words or []))
            if wanted and await self._llm_allowed(event, cfg):
                # The remark is about what was asked for, not about the picture:
                # nobody wants a caption describing the pixels. A bare "来张图"
                # names nothing, so there is nothing to remark on.
                await self._deliver(event, found)
                if event.is_stopped():
                    return
                conv = await self._conversation(event)
                # The sender did ask for this picture, so the remark is a reply
                # to them, not the bot butting in. Say so: mood's injected block
                # otherwise has to guess from the wake flag.
                event.set_extra("_interject", False)
                yield event.request_llm(
                    prompt=(
                        "（你刚往群里丢了一张图，这次说的是「"
                        + "、".join(wanted)
                        + "」，图已经发出去了，没人要你描述画面）"
                        "就这个说法顺口接一句，二十字以内。别描述图里有什么，"
                        "别说好坏、别说好看，就着这几个字本身接话。"
                    ),
                    conversation=conv,
                )
                return
            event.stop_event()
            await self._deliver(event, found)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"illust: {e}")

    async def _llm_allowed(self, event: AstrMessageEvent, cfg: dict) -> bool:
        """Whether the current provider is allowed to spend a call on banter.

        Args:
            event: The message event being handled.
            cfg: Plugin config.

        Returns:
            True when the active provider comes from the free gateway.
        """
        source = str(cfg.get("llm_source_id", "") or "")
        if not source or not cfg.get("llm_with_image", True):
            return False
        try:
            prov = await self.context.get_using_provider_async(
                event.unified_msg_origin
            )
            if prov is None:
                return False
            return (prov.provider_config or {}).get("provider_source_id") == source
        except Exception as e:  # noqa: BLE001
            logger.debug("illust: provider lookup failed (%s)", e)
            return False

    async def _conversation(self, event: AstrMessageEvent):
        """Return the group's current conversation so the reply keeps persona.

        Args:
            event: The message event being handled.

        Returns:
            The conversation record, opening one when the group has none yet.
        """
        cm = self.context.conversation_manager
        umo = event.unified_msg_origin
        cid = await cm.get_curr_conversation_id(umo)
        conv = await cm.get_conversation(umo, cid) if cid else None
        if conv is None:
            cid = await cm.new_conversation(umo, event.get_platform_id())
            conv = await cm.get_conversation(umo, cid)
        return conv

    async def _deliver(self, event: AstrMessageEvent, found: dict) -> None:
        """Send the picture; if the platform rejects it, send its ids instead.

        QQ screens pictures on upload and refuses some of them silently, in
        which case the adapter raises. The ids are the useful fallback: whoever
        asked can look the picture up himself.

        Args:
            event: The message event to answer.
            found: Result of :meth:`_fetch`.
        """
        chain = MessageChain([Image.fromFileSystem(str(found["path"]))])
        try:
            await event.send(chain)
            await self._remember(event, chain)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("illust: send refused (%s), falling back to ids", e)
        text = f"图被拦了。pid {found['pid']}，uid {found['uid']}"
        fallback = MessageChain().message(text)
        try:
            await event.send(fallback)
            await self._remember(event, fallback)
        except Exception as e:  # noqa: BLE001
            logger.warning("illust: fallback text also refused (%s)", e)

    async def _remember(self, event: AstrMessageEvent, chain: MessageChain) -> None:
        """Record the picture in the group's history as the bot's own line.

        The normal pipeline only writes bot messages after an LLM reply, and
        this plugin answers without the model, so without this the request
        would sit in history unanswered -- the next turn would see "来张图"
        followed by a brand new message and reply to the wrong one.

        Args:
            event: The message event that asked for the picture.
            chain: The chain that was actually sent.
        """
        try:
            settings = self.context.get_config(
                umo=event.unified_msg_origin
            ).get("provider_ltm_settings", {})
            if not settings.get("group_message_history_enable", False):
                return
            try:
                max_messages = max(
                    1, int(settings.get("group_message_history_max_cnt", 700))
                )
            except (TypeError, ValueError):
                max_messages = 700
            await self.context.message_history_manager.insert_message_chain(
                platform_id=event.get_platform_id(),
                user_id=event.unified_msg_origin,
                message_chain=chain,
                role="bot",
                sender_id=event.get_self_id() or "bot",
                sender_name="bot",
                max_messages=max_messages,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"illust: history write failed ({e})")

    async def _query(
        self,
        session: aiohttp.ClientSession,
        words: list[str] | None = None,
        extras: list[str] | None = None,
        num: int = 20,
        loose: bool = False,
    ) -> list[dict]:
        """Ask the setu API for a batch of normal-rated candidates.

        Two indexes are asked in turn. ``tag`` matches whole tags and supports
        AND between array items and OR inside one item with ``|``; each item is
        split into 2-3 character n-grams, so unspaced runs like "白丝黑丝" match
        nothing. ``keyword`` is a fuzzy index over title, author and tags, which
        is what catches those runs and near-misses ("猫咪" vs the tag "猫娘").

        Args:
            session: Open HTTP session.
            words: Topic words, empty for anything.
            extras: Extra tags the sender typed; they are searched for as-is.
            num: How many candidates to ask for (the API caps at 20).
            loose: Ask only the fuzzy index, for when the strict one ran dry.

        Returns:
            The candidate list, empty when the call failed.
        """
        base = {
            "r18": "0",
            "num": str(num),
            "size": "regular",
            "excludeAI": "true",
        }
        wanted = list(words or []) + list(extras or [])
        if not wanted:
            try:
                async with session.get(
                    API,
                    params=base,
                    timeout=aiohttp.ClientTimeout(total=META_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        return []
                    payload = await resp.json(content_type=None)
            except Exception as e:  # noqa: BLE001
                logger.warning("illust: query failed (%s)", e)
                return []
            return list(payload.get("data") or [])
        if loose:
            # One string, not an array: an array ANDs, and a phrase that the
            # API cannot match whole would then return nothing at all.
            attempts = [{"keyword": " ".join(wanted[:3])}]
        else:
            attempts = [
                {"tag": TAG_MAP.get(w, w) for w in wanted[:3]},
                {"tag": [TAG_MAP.get(w, w) for w in wanted[:3]]},
                {"keyword": " ".join(wanted[:3])},
            ]
        for extra in attempts:
            try:
                async with session.get(
                    API,
                    params={**base, **extra},
                    timeout=aiohttp.ClientTimeout(total=META_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json(content_type=None)
            except Exception as e:  # noqa: BLE001
                logger.warning("illust: query failed for %s (%s)", extra, e)
                continue
            data = list(payload.get("data") or [])
            if data:
                return data
        return []

    async def _download(self, session: aiohttp.ClientSession, url: str) -> bytes | None:
        """Pull the picture bytes from the mirror."""
        try:
            async with session.get(
                url,
                headers={"Referer": "https://www.pixiv.net/"},
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"illust: download failed ({e})")
            return None

    async def _fetch(
        self, words: list[str] | None = None, extras: list[str] | None = None
    ) -> dict | None:
        """Resolve one acceptable picture to a local file.

        One API call returns 20 candidates. The ones whose tags give them away
        are dropped before anything is downloaded; the rest are tried in order
        until one downloads cleanly. QQ screens the picture after it is posted.

        Args:
            words: Topic words to search for, empty for a random picture.
            extras: Tags the sender typed himself. They are searched for as-is
                and they switch the tag blocklist off -- whoever names a tag
                has said what he wants.

        Returns:
            ``{"path", "pid", "uid", "fallback"}`` for the file that was saved,
            or ``None`` when nothing usable came back. ``fallback`` is set when
            the requested topic was too rare and a random picture was used
            instead, i.e. the topic in the answer no longer matches the picture.
        """
        extras = list(extras or [])
        words = [w for w in (words or []) if w]
        cfg = load_cfg()
        fallback = False
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            pool = await self._query(session, words, extras)
            if not pool and words and not extras:
                # The strict index missed: ask the fuzzy one before giving up
                # on the topic, which is how an unspaced run like "白丝黑丝"
                # still finds something.
                pool = await self._query(session, words, extras, loose=True)
            if not pool and not extras and words:
                pool = await self._query(session, [])  # too rare: random
                fallback = True
            candidates = [
                item
                for item in pool
                if (extras or not blocked_tag(item))
                and int(item.get("aiType") or 0) == 0
                and int(item.get("width") or 0) >= MIN_WIDTH
                and int(item.get("pid") or 0) not in self._seen
            ]
            for item in candidates[:MAX_TRIES]:
                pid = int(item.get("pid") or 0)
                urls = item.get("urls") or {}
                url = urls.get("regular") or urls.get("original") or ""
                if not url:
                    continue
                data = await self._download(session, url)
                if not data or len(data) < 8_000:
                    continue
                path = CACHE_DIR / f"{pid}_{int(item.get('p') or 1)}.jpg"
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_bytes, data)
                self._seen.append(pid)
                del self._seen[:-20]
                self._prune(int(cfg["cache_keep"]))
                logger.info(
                    "illust: fetched pid=%s words=%s extras=%s size=%sK",
                    pid,
                    words or "-",
                    extras,
                    len(data) // 1024,
                )
                return {
                    "path": path,
                    "pid": pid,
                    "uid": int(item.get("uid") or 0),
                    "fallback": fallback,
                }
        return None

    def _prune(self, keep: int) -> None:
        """Drop the oldest cache files so the folder cannot grow forever."""
        try:
            files = sorted(CACHE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for old in files[:-keep] if keep > 0 else []:
                old.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
