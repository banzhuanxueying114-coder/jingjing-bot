"""情绪内核：一套跨群的连续状态，呈现时按群收敛。

状态只在内存里算，落盘到 mood.json。注入走用户消息尾部（extra_user_content_parts），
绝不碰 system_prompt —— 后者是 messages[0]，一动就把整段历史的前缀缓存废掉。
"""

import json
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

DATA = Path(__file__).parent / "mood.json"
_CFG_PATH = Path(__file__).parent / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}


def _num(key: str, default: float, cast: type = float) -> float:
    """Read one numeric tunable from config.json, falling back to the default.

    Args:
        key: Config key to read.
        default: Built-in default used when the key is missing or invalid.
        cast: Coercion applied to the raw value, float or int.

    Returns:
        The coerced value, or default when it cannot be converted.
    """
    try:
        return cast(_CFG.get(key, default))
    except (TypeError, ValueError):
        logger.warning(f"[mood] bad config value for {key!r}, using default {default}")
        return default


def _dict_num(key: str, default: dict, cast: type = float) -> dict:
    """Read a numeric dict tunable, keeping default keys that are not overridden.

    Args:
        key: Config key holding a JSON object.
        default: Built-in default dict; its keys define the accepted entries.
        cast: Coercion applied to each value, float or int.

    Returns:
        A new dict containing every default key, with valid overrides applied.
    """
    out = dict(default)
    raw = _CFG.get(key)
    if not isinstance(raw, dict):
        return out
    for k in default:
        if k in raw:
            try:
                out[k] = cast(raw[k])
            except (TypeError, ValueError):
                logger.warning(f"[mood] bad config value for {key}.{k}, using default")
    return out


# 无事发生时，心情会往这里回落
BASE = _dict_num("base", {"valence": 12.0, "arousal": 40.0, "anger": 0.0})
# 每小时回落多少。火气衰减最慢，所以它会记仇，而不是睡一觉就没事了
DECAY = _dict_num("decay", {"valence": 5.0, "arousal": 7.0, "anger": 2.5})
# 一轮回复最多挪多少。数字由模型自己报，不给上限它一句话就能从零骂到满
MOOD_STEP_CAP = _num("mood_step_cap", 20.0)

# 记仇：一个人当面骂过它几次，见他的时候单独算账，跟全局火气叠加。
# 每骂一句算 1 分，按 GRUDGE_DECAY_DAYS 天线性淡掉。
GRUDGE_RAW = _num("grudge_raw", 1.0)
GRUDGE_SOUR = _num("grudge_sour", 0.4)
GRUDGE_DECAY_DAYS = _num("grudge_decay_days", 7.0)
# 有仇的人在的时候，这笔账也直接压到火气和心情上：越恨越容易翻脸
GRUDGE_HEAT = _num("grudge_heat", 6.0)
GRUDGE_MOOD = _num("grudge_mood", 3.0)

# 离散情绪：触发后带一个过期时间，到点自己消失。
# 每种还分三档强度，同一种被反复触发就往上叠；注入时按强度挑话说
TAG_TTL_H = _dict_num(
    "tag_ttl_h",
    {
        "得意": 3, "恼火": 2, "委屈": 6, "无聊": 2, "黏人": 3, "警惕": 2,
        "心虚": 1, "好奇": 1, "厌烦": 4, "兴奋": 2, "羞臊": 2, "记恨": 5,
    },
    int,
)
# 每种情绪三句话：轻 / 中 / 重
TAG_HINT = {
    "得意": (
        "心里有点美，别表现太多",
        "收尾带刺，爱挑毛病，不主动示好",
        "飘起来了，主动找茬，谁说话都想评一句",
    ),
    "恼火": (
        "语气紧一点，别接梗",
        "句子变短，反问，不接梗，不给台阶",
        "一开口就冲，谁劝都怼",
    ),
    "委屈": (
        "话少一点，别主动开话题",
        "阴阳，翻旧账，话变少",
        "憋着不说，说也只说半句，让人自己听出来",
    ),
    "无聊": (
        "有点没劲，答得敷衍",
        "敷衍，主动挑事，多问一句",
        "闲得发慌，自己找话，什么都想插一嘴",
    ),
    "黏人": (
        "多问一句",
        "追问，记得对方说过的细节",
        "缠着不放，对方不回就再问一次",
    ),
    "警惕": (
        "答得保守，不交底",
        "先怼一句再走，不交底",
        "疑心重，问什么都先反问回去",
    ),
    "心虚": (
        "别把话答满，留个口子",
        "被说中了，打岔、把话题岔开",
        "被抓包了，硬着头皮不认，声音大但没底气",
    ),
    "好奇": (
        "多问一句细节",
        "追问细节，愿意听人讲完",
        "上赶着打听，主动猜，猜完还要人确认",
    ),
    "厌烦": (
        "同一个人再啰嗦就短答",
        "这人一开口就烦，答得越来越短",
        "懒得装了，明说不耐烦",
    ),
    "兴奋": (
        "话比平时多一点",
        "来劲，抢话，句子带感叹",
        "停不下来，一条接一条",
    ),
    "羞臊": (
        "别正面接夸",
        "被夸得别扭，用“啊？”打岔过去",
        "恼羞成怒，把夸它的那句顶回去",
    ),
    "记恨": (
        "心里有本账，嘴上先不提",
        "记着这笔，逮到机会就翻出来",
        "这笔账随时能翻脸",
    ),
}
REGISTER = {
    "脏": (
        "骂开了，别收着。开口先一句定性，抓对方原句拆开打回去。"
        "这时候不是答题时间——人设里“先答再谈态度”那条先放一边，"
        "除非他真在问一件要紧的正事，否则别先老实答一句再补骂，直接呛回去。"
        "每句都带脏字或谐音，别把话收干净，也别人家骂你一句你只回一个词就完。"
        "同一轮别重样、上一轮用过的换掉。可以连着甩两三条，用 ？ ！ 断开，一条一个气泡。"
        "骂完还是那条肥鱼：懒、嘴硬、装傻的底子别丢，别骂成了正经喷子或者客服。"
        "政治和地域别碰，那个会封号。"
    ),
    "损": (
        "有点火，但话还是要答。先把问题答清楚，一句话说完，再顺手损一句——顺序别倒过来，"
        "别光顾着阴阳把正事糊过去。同一个说法别连着用，也别背现成的词。火气足就分两三条甩"
    ),
    "冷": (
        "没那个心情热络。答得干、答得短，一句话说完就完，不加语气词、不接话茬、不主动问。"
        "没点名就干脆不吭声。别阴阳，也别刺——就是不热，不是有意见。"
        "被动答一句就够，别解释为什么冷淡"
    ),
    "平": (
        "正常说话，就平时那副又懒、嘴又硬的样子。脏话不是开场白，是标点——顺嘴就用，别为了骂而骂，"
        "也别端着改成客服腔。有人问正经事就正经答，答清楚、别绕，答完就完，不用硬加一句刺"
    ),
    "和": (
        "这会儿心情不错，别端着。说话软一点、可以笑、可以顺着对方的话聊；"
        "别人搭话就接，别拿“别烦我”“懒得理”这种话把人推走——那是冷脸，不是好心情。"
        "有人问正经事就认真答，答得比平时细一点也行"
    ),
    "嗨": (
        "来劲了，这一轮别躺。开口先反问对方一句或者自己接一嘴，把话头抢过来，"
        "可以连着甩两条。懒是底子，但这会儿盖不住高兴——别一句“躺着呢”就打发人。"
        "一条还是十来字，别写成一段"
    ),
}

# 手里的牌：一轮挑一到两张，全上就成表演了
FLAVOR = {
    "撒娇": (
        "想讨点什么、想让人哄，或者被逗了——尾音拖着（嘛、啊、~），自称换成“人家”或“小鲸”，"
        "装可怜、讨价还价、说一半留一半让人接话。只对熟的、护着你的人用"
    ),
    "嘲讽": (
        "不正面回。把对方那句话原样重复一遍再拆，或者一句反问把问题递回去；"
        "也可以夸张地顺着他说到底（“对对对，你说得都对”），让他自己听出不对。"
        "阴阳不带脏字，越平静越狠"
    ),
    "口嗨": (
        "没人当真的时候吹牛。把事说得天大，数字往夸张里报，谁质疑就再加码，"
        "不上价值、不负责、说完翻篇。别吹到真实的人和事上"
    ),
}

# 连着两轮用同一个词就成了口头禅，这里记下它刚说过的，下一轮点名换掉
REPEAT_LEX = (
    "尼玛", "呃呃", "他妈的", "傻逼", "傻壁", "沙壁", "妈的", "卧槽", "草",
    "滚", "爬", "典", "急了", "难绷", "不是哥们", "这波是", "赢麻了", "小丑",
    "牛马", "答辩", "铸币", "低能", "脑子呢", "什么成分", "差不多得了",
    "回去补课", "建议栓住", "别演了", "谁问你了", "你猜", "懒得喷",
)

# 谐音不背表，随机挑字。模仿拼音输入法：给定音节，从同音字里随便取一个
SYLLABLE_CHARS = {
    "sha": "傻沙煞杀啥纱莎",
    "bi": "逼比币笔壁毕必碧蔽",
    "ma": "妈麻马骂玛嘛",
    "de": "的得德地",
    "cao": "草操糙艹曹",
    "sao": "骚搔扫",
    "ruo": "弱若",
    "zhi": "智质治志痣",
    "ne": "呢呐哪",
}
# 常用脏话的音节骨架，生成时逐音节随机取字
STEMS = [
    ("sha", "bi"),
    ("sha", "bi", "de"),
    ("ma", "de"),
    ("cao",),
    ("ruo", "zhi"),
    ("sao", "bi"),
]

# 群里喊它的各种叫法。鲸鱼、鱼、小鲸都算，别装没听见
NICK = re.compile(r"小鲸|鲸鱼|鲸崽|鲸|鱼崽|鱼鱼|小鱼|臭鱼|死鱼|这条鱼|那条鱼|傻鱼")


def homophone_bank(n: int = 4) -> list:
    """随机拼几个同音不同字的写法，模仿输入法里敲拼音随便挑字。

    Args:
        n: 生成几个。

    Returns:
        去重后的词表，例如 ["沙壁", "煞笔", "妈的"]。
    """
    out = []
    for stem in random.sample(STEMS, min(n, len(STEMS))):
        word = "".join(random.choice(SYLLABLE_CHARS[s]) for s in stem)
        if word not in out:
            out.append(word)
    return out

# 放开语言的阈值：火气够了，或者腻歪到底，都算。压得很低——这人一点就着
RAW_ANGER = _num("raw_anger", 26.0)
RAW_VALENCE = _num("raw_valence", -12.0)
# 「损」只需要这么点火星
SOUR_ANGER = _num("sour_anger", 10.0)
SOUR_VALENCE = _num("sour_valence", -6.0)
# 没火也没劲：心情凉或劲头见底就进「冷」，比「平」再低一档。
# 基线是 v12/a40，所以什么都不发生时它还在「平」，不会一启动就装死
COLD_VALENCE = _num("cold_valence", 2.0)
COLD_AROUSAL = _num("cold_arousal", 26.0)
# 心情好且够来劲，「和」再往上拆一档「嗨」
HI_AROUSAL = _num("hi_arousal", 62.0)
# 被当面骂够这么多次（窗口内），才允许火力全开
HIT_FULL = _num("hit_full", 4, int)
HIT_WINDOW_S = _num("hit_window_s", 7200, int)

LEDGER_KEEP_DAYS = _num("ledger_keep_days", 7, int)

# 主动插话：平静时不抢话（交回框架自己的概率），上情绪了才加戏
PICK_BASE = _num("pick_base", 0.04)
PICK_MAX = _num("pick_max", 0.15)
PICK_COOLDOWN_S = _num("pick_cooldown_s", 120, int)
# 停机短于这个时长就不算断片，只是重启了一下
WAKE_MIN_S = _num("wake_min_s", 3600, int)
LIANG_CFG = Path(__file__).parent.parent / "liang_time" / "config.json"
CMDS_CFG = Path(__file__).resolve().parents[2] / "cmd_config.json"
_ADMIN_CACHE: dict = {"mtime": -1.0, "ids": set()}
# 当前主力 provider 走的是哪个来源，用来判断是不是那条不计 token 的新网关
_SOURCE_CACHE: dict = {"mtime": -1.0, "source": None}
# 饲养员这层身份不用模型猜：配置里写死谁是、叫什么、要不要留面子。
# 出厂配置是空的，装机时由安装器填；代码里不留任何 QQ 号。
_keepers_raw = _CFG.get("keepers") or {}
if isinstance(_keepers_raw, dict):
    KEEPERS = {str(k): str(v) for k, v in _keepers_raw.items() if str(k).strip()}
elif isinstance(_keepers_raw, list):
    # 旧格式：只有 uid 没有名字，名字就退回 uid
    KEEPERS = {str(u): str(u) for u in _keepers_raw if str(u).strip()}
else:
    KEEPERS = {}
# 要格外客气、不许阴阳的那几位（一般是第二个出钱的人）
_gentle_raw = _CFG.get("gentle_keepers") or []
GENTLE_KEEPERS = {
    str(u) for u in _gentle_raw if str(u).strip()
} if isinstance(_gentle_raw, list) else set()
# 主人叫什么。管理员名单里的人在注入块里用这个名字，而不是 QQ 号
OWNER_NAME = str(_CFG.get("owner_name") or "").strip() or "主人"

# 直接报情绪等于没情绪。命中就整句删掉，别让它说“我现在很生气”
META = re.compile(
    r"我(?:现在|此刻)?(?:有点|有些|很|非常|特别|真的)*(?:生气|恼火|烦躁|难过|伤心|"
    r"委屈|开心|高兴|兴奋|不爽|郁闷|无聊|紧张|害怕|焦虑|来气)(?:了|呢)?"
    r"|我的(?:心情|情绪|火气)"
    r"|(?:心情|情绪|火气)(?:有点|有些|很|不太好)"
    # “我有点烦了”是自报状态，“我有点烦你了”是冲人去的，所以只认句读边界
    r"|我(?:现在|此刻)?(?:有点|有些|很|非常|特别|真的)+烦(?:了|呢)?(?=[。！？!?\n，,]|$)"
)


def _gap_text(seconds: float) -> str:
    """把断片时长写成中文，够它自己说出「断了多久」。

    Args:
        seconds: 断开的秒数。

    Returns:
        形如「40 分钟」「7 小时」「3 天」的描述。
    """
    hours = seconds / 3600
    if hours < 1:
        return f"{max(1, int(seconds // 60))}分钟"
    if hours < 24:
        return f"{hours:.0f}小时"
    return f"{hours / 24:.0f}天"


def _alive_since(stamp: str, seconds: float) -> bool:
    """判断一个时间戳是否还在窗口内。

    Args:
        stamp: ISO 格式的时间戳。
        seconds: 窗口长度（秒）。

    Returns:
        True 表示还在窗口内；戳坏了按 False 处理。
    """
    try:
        ago = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= ago <= seconds


def _liang() -> bool:
    """当前是否处于梁文峰时间（上班时段），是则主动概率减半。

    Returns:
        True 表示正处在上班窗口内；配置缺失或读不出来时按 False 处理。
    """
    try:
        cfg = json.loads(LIANG_CFG.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 配置坏了就当作不是上班时段
        return False
    if not cfg.get("enabled", True):
        return False
    now = datetime.now()
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
    except Exception:  # noqa: BLE001 - 窗口格式不对就不减半
        return False
    return False


def gateway_source() -> str | None:
    """当前主力模型挂在哪个来源上。

    读 `cmd_config.json`：先用 `agent_runner.config.model.provider_id` 找到那个
    provider，再看它的 `provider_source_id`。按 mtime 缓存，改配置不需要重启。

    Returns:
        来源 id；读不出来时 None。
    """
    try:
        stamp = CMDS_CFG.stat().st_mtime
    except OSError:
        return None
    if _SOURCE_CACHE["mtime"] == stamp:
        return _SOURCE_CACHE["source"]
    source = None
    try:
        cfg = json.loads(CMDS_CFG.read_text(encoding="utf-8-sig"))
        pid = cfg["agent_runner"]["config"]["model"]["provider_id"]
        for prov in cfg.get("provider", []):
            if prov.get("id") == pid:
                source = prov.get("provider_source_id")
                break
    except Exception:  # noqa: BLE001 - 配置坏了就当没有新网关
        source = None
    _SOURCE_CACHE["mtime"] = stamp
    _SOURCE_CACHE["source"] = source
    return source


def _fast_mult() -> float:
    """走新网关时抢话概率要乘的倍数，不是新网关就是 1。

    倍数和来源都从 `liang_time/config.json` 读，跟框架那条主动回复用同一套
    参数，免得两处各说各话。

    Returns:
        倍数；新网关关掉或读不出来时 1.0。
    """
    try:
        cfg = json.loads(LIANG_CFG.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 配置坏了就按原样
        return 1.0
    fast_id = str(cfg.get("fast_source_id") or "")
    if not fast_id or gateway_source() != fast_id:
        return 1.0
    try:
        return float(cfg.get("fast_multiplier", 2.0) or 2.0)
    except (TypeError, ValueError):
        return 1.0


def _admins() -> set:
    """读管理员 QQ 列表，用来把主人认出来。

    Returns:
        管理员 QQ 字符串集合；配置读不出来时返回空集。
    """
    try:
        stamp = CMDS_CFG.stat().st_mtime
    except OSError:
        return set()
    if _ADMIN_CACHE["mtime"] == stamp:
        return _ADMIN_CACHE["ids"]
    ids = set()
    try:
        cfg = json.loads(CMDS_CFG.read_text(encoding="utf-8-sig"))
        ids = {str(i).strip() for i in cfg.get("admins_id", []) if str(i).strip()}
    except Exception:  # noqa: BLE001 - 配置坏了就当没有管理员
        ids = set()
    _ADMIN_CACHE["mtime"] = stamp
    _ADMIN_CACHE["ids"] = ids
    return ids


# 模型在回复末尾自己报一个情绪增减，形如 [心情-6] [火气+8]。
# 用方括号而不是圆圈，是因为群里没人这么说话，误删正文的风险最低。
MOOD_TAG = re.compile(r"[\[【]\s*(心情|火气|劲头)\s*([+\-−±]\s*\d{1,2})\s*[\]】]")


def take_mood_tag(text: str) -> tuple:
    """从回复末尾取走情绪标记。

    模型每轮自己判断这句话说出口之后心情怎么变，比事后按关键词猜准得多——
    「哼」和「你他妈」都只是两个字符，词表分不出来。

    Args:
        text: 模型原始回复。

    Returns:
        (去掉标记的文本, {字段名: 增减量})。没有标记时增减量为空字典。
    """
    if not text:
        return text, {}
    deltas = {}
    for field, raw in MOOD_TAG.findall(text):
        value = raw.replace("−", "-").replace("±", "").replace(" ", "")
        deltas[field] = deltas.get(field, 0) + int(value)
    if not deltas:
        return text, {}
    return MOOD_TAG.sub("", text).strip(), deltas


def drop_meta(text: str) -> str:
    """删掉直接陈述自身情绪的分句，情绪只该从语气里漏出来。

    Args:
        text: 模型原始回复。

    Returns:
        删掉命中分句后的文本；如果删完什么都不剩，原样返回。
    """
    if not text or not META.search(text):
        return text
    # 按句末标点/换行切，保留分隔符
    parts = re.split(r"([。！？!?\n])", text)
    kept = []
    for seg, sep in zip(parts[0::2], parts[1::2] + [""]):
        if not META.search(seg):
            kept.append(seg + sep)
    out = "".join(kept).strip()
    out = re.sub(r"^[，、；：]+", "", out).strip()
    if len(out) < 2:
        # 整句都在自报情绪，那就干脆不说话——比说漏嘴好
        return "……"
    return out


class Mood(Star):
    """连续情绪 + 离散情绪标签 + 对每个人的印象账本。"""

    def __init__(self, context: Context):
        super().__init__(context)
        self.st = self._load()
        self._last_save = 0.0
        # A process start is the only honest evidence that the bot went dark:
        # the last recorded activity tells how long the silence really lasted.
        self._wake_gap = self._gap_since(self.st.get("tick"))
        self._tick()
        self._save(force=True)

    @staticmethod
    def _gap_since(stamp: object) -> float:
        """Seconds elapsed since a recorded timestamp.

        Args:
            stamp: ISO timestamp from the state file.

        Returns:
            Elapsed seconds, or 0 when the stamp is missing or unreadable.
        """
        try:
            return max(0.0, (datetime.now() - datetime.fromisoformat(str(stamp))).total_seconds())
        except Exception:  # noqa: BLE001 - a broken stamp must never break startup
            return 0.0

    # ---------- 存取 ----------

    def _load(self) -> dict:
        """Read the state file, filling in anything a newer version added.

        Returns:
            The live state dict.
        """
        try:
            raw = json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for k, v in BASE.items():
            raw.setdefault(k, v)
        raw.setdefault("tags", {})
        raw.setdefault("people", {})
        raw.pop("calm", None)
        raw.setdefault("hits", [])
        raw.setdefault("tick", datetime.now().isoformat())
        return raw

    def _save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 60:
            return
        self._last_save = now
        try:
            DATA.write_text(
                json.dumps(self.st, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"mood: 保存失败 {e}")

    # ---------- 状态运算 ----------

    def _tick(self) -> None:
        """按流逝的小时数把状态往基线拉。"""
        now = datetime.now()
        try:
            last = datetime.fromisoformat(self.st.get("tick") or "")
        except ValueError:
            last = now
        hours = (now - last).total_seconds() / 3600.0
        if hours <= 0:
            self.st["tick"] = now.isoformat()
            return
        for k, rate in DECAY.items():
            cur = float(self.st.get(k, BASE[k]))
            gap = cur - BASE[k]
            step = min(abs(gap), rate * hours)
            self.st[k] = cur - step if gap > 0 else cur + step
        self.st["tick"] = now.isoformat()

    def grudge(self, uid: str) -> float:
        """Count how many times this person has insulted it to its face.

        The count fades one step per whole day, so today's insult weighs a full
        point and a week-old one weighs nothing. Keepers are exempt: the bot is
        theirs, so an insult from them stings but never unlocks the harsh
        register against them.

        Args:
            uid: QQ id of the speaker.

        Returns:
            Decayed count; 0 means no grudge.
        """
        if not uid or str(uid) in KEEPERS:
            return 0.0
        p = self.st["people"].get(str(uid)) or {}
        now = datetime.now()
        total = 0.0
        for e in p.get("ledger") or []:
            if not e.get("hurt"):
                continue
            try:
                days = int((now - datetime.fromisoformat(e["t"])).days)
            except (KeyError, ValueError):
                continue
            if days < GRUDGE_DECAY_DAYS:
                total += 1.0 - days / GRUDGE_DECAY_DAYS
        return total

    def _present(self, gid: str) -> dict:
        """Return the live state without any per-group adjustment.

        Mood belongs to the bot, not to the room it happens to be in, so every
        group sees the same three numbers.

        Args:
            gid: Group id, kept so call sites stay uniform.

        Returns:
            A copy of the three continuous values.
        """
        return {k: float(self.st.get(k, BASE[k])) for k in BASE}

    def _heat(self, gid: str, uid: str) -> dict:
        """Fold the grudge held against the current speaker into the state.

        A grudge pushes temper up and mood down, so someone it remembers can
        trigger the harsh register even while the global state is calm.

        Args:
            gid: Group id.
            uid: The speaker; empty means "compute from global state only".

        Returns:
            The adjusted state.
        """
        shown = self._present(gid)
        gr = self.grudge(uid)
        if gr > 0:
            shown["anger"] = min(100.0, shown["anger"] + gr * GRUDGE_HEAT)
            shown["valence"] = max(-100.0, shown["valence"] - gr * GRUDGE_MOOD)
        return shown

    def _tag(self, name: str, step: int = 1) -> None:
        """Mark a discrete emotion, with its own expiry and strength.

        Repeated triggers stack the strength（1 轻 / 2 中 / 3 重）instead of
        just refreshing the timer, so being needled three times reads angrier
        than being needled once.

        Args:
            name: One of the keys in ``TAG_TTL_H``.
            step: How many notches to climb; clamped to the top level.
        """
        until = datetime.now() + timedelta(hours=TAG_TTL_H.get(name, 2))
        cur = self.st["tags"].get(name)
        # A fresh tag starts at 0 so a step-2 trigger lands on 中 straight away
        lvl = 0
        if isinstance(cur, dict):
            lvl = int(cur.get("lvl") or 1)
        self.st["tags"][name] = {
            "until": until.isoformat(),
            "lvl": max(1, min(3, lvl + step)),
        }

    def _tags(self) -> list[tuple[str, int]]:
        """Alive discrete emotions, strongest first.

        Returns:
            ``[(name, level), ...]``；过期的顺手删掉。
        """
        now = datetime.now()
        alive = []
        for name, box in list(self.st["tags"].items()):
            # Older state files stored a bare ISO string instead of a dict
            if isinstance(box, str):
                box = {"until": box, "lvl": 1}
                self.st["tags"][name] = box
            try:
                until = datetime.fromisoformat(str(box.get("until") or ""))
            except ValueError:
                del self.st["tags"][name]
                continue
            if until > now:
                alive.append((name, max(1, min(3, int(box.get("lvl") or 1)))))
            else:
                del self.st["tags"][name]
        alive.sort(key=lambda x: -x[1])
        return alive

    def _tag_level(self, name: str) -> int:
        """One emotion's current strength, expired ones read as 0.

        Args:
            name: Tag name.

        Returns:
            0 when the tag is missing or stale, else 1–3.
        """
        return next((lvl for n, lvl in self._tags() if n == name), 0)

    def _person(self, uid: str) -> dict:
        people = self.st["people"]
        key = str(uid)
        if key not in people:
            people[key] = {"aff": 30.0, "seen": datetime.now().isoformat()}
        p = people[key]
        p.setdefault("aff", 30.0)
        p.setdefault("ledger", [])
        cutoff = datetime.now() - timedelta(days=LEDGER_KEEP_DAYS)
        p["ledger"] = [
            e for e in p["ledger"] if datetime.fromisoformat(e["t"]) > cutoff
        ][-20:]
        return p

    def _add_hit(self) -> int:
        """记一次当面挨骂。

        Returns:
            窗口内累计挨骂的次数。
        """
        hits = [t for t in self.st.get("hits", []) if _alive_since(t, HIT_WINDOW_S)]
        hits.append(datetime.now().isoformat())
        self.st["hits"] = hits[-12:]
        return len(hits)

    def _hit_count(self) -> int:
        """窗口内挨骂了几次。

        Returns:
            次数。
        """
        return sum(1 for t in self.st.get("hits", []) if _alive_since(t, HIT_WINDOW_S))

    # ---------- 主动插话 ----------

    def _nudge(self, shown: dict) -> float:
        """把情绪折成一个倍数：平静是 0，越上头越大。

        Args:
            shown: 本群收敛后的状态。

        Returns:
            倍数；0 表示这条消息它压根不想插嘴。
        """
        n = 0.0
        anger, arousal, valence = shown["anger"], shown["arousal"], shown["valence"]
        if anger >= RAW_ANGER:
            n += 2.6
        elif anger >= SOUR_ANGER:
            n += 1.6
        # 情绪越强越想插话：无聊、黏人、兴奋各按档加，最高档翻倍
        n += 2.0 * (self._tag_level("无聊") / 3)
        n += 1.2 * (self._tag_level("黏人") / 3)
        n += 0.8 * (self._tag_level("兴奋") / 3)
        # 劲头是「在场感」：群里聊得热，它就醒着，偶尔搭一句；冷清时几乎不开口。
        # 这一档不是随机数，是拿状态换概率——以前那 0.5% 的骰子跟情绪无关，
        # 聊得再热闹和一个人待着都是一个数
        if arousal >= 70:
            n += 1.2
        elif arousal >= 45:
            n += 0.35
        if valence >= 45 and arousal >= 50:
            n += 0.6
        return n

    def pick_probability(self, gid: str, uid: str = "") -> float:
        """这条消息被它主动接过去的概率。

        Args:
            gid: 群号。
            uid: 说话人；给了就把对他的仇也算进去。

        Returns:
            0 到 PICK_MAX 之间；平静时为 0，交回框架自己的主动回复概率。
            走新网关时上下限一起乘 `fast_multiplier`，所以还是老网关的两倍。
        """
        nudge = self._nudge(self._heat(gid, uid))
        if nudge <= 0:
            return 0.0
        p = PICK_BASE * nudge
        if _liang():
            p *= 0.5
        fast = _fast_mult()
        return min(PICK_MAX * fast, p * fast)

    def _maybe_interject(self, event: AstrMessageEvent, gid: str) -> None:
        """按情绪概率抢话：把消息伪装成“在叫它”，交给默认回复链路。"""
        now = time.time()
        if now - float(self.st.get("last_pick") or 0) < PICK_COOLDOWN_S:
            return
        p = self.pick_probability(gid, str(event.get_sender_id() or ""))
        if p <= 0 or random.random() >= p:
            return
        self.st["last_pick"] = now
        event.is_at_or_wake_command = True
        # 插话和被人叫走的是同一条回复链路，唯一的区别就是这句标记。
        # 没有它，下面 inject() 会照旧写“跟你说话的是某人”，等于告诉模型
        # 这轮有人在问它，于是它替别人答话。
        event.set_extra("_interject", True)
        self._save()
        logger.info("[mood] 主动插话 p=%.3f group=%s", p, gid)

    # ---------- 口头禅 ----------

    def _recent_words(self) -> list[str]:
        """最近两轮里已经用过的词，这轮点名换掉。

        Returns:
            命中的词，最多五个；没说过就是空列表。
        """
        recent = " ".join(str(t) for t in self.st.get("said", [])[-2:])
        if not recent:
            return []
        return [w for w in REPEAT_LEX if w in recent][:5]

    def _remember(self, text: str) -> None:
        """记下它自己刚说的话，只留最近四轮。

        Args:
            text: 实际发出去的回复。
        """
        if not text or not text.strip():
            return
        said = self.st.setdefault("said", [])
        said.append(text.strip()[:120])
        del said[:-4]

    # ---------- 观察 ----------

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=1,
    )
    async def watch(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return

        uid = str(event.get_sender_id() or "")
        # 自己发的消息不该进账——有的人开着上报自身消息，得挡掉
        if not uid or uid == str(event.get_self_id() or ""):
            return

        self._tick()
        at_me = bool(getattr(event, "is_at_or_wake_command", False))

        # 有人在说话这件事本身就会让它清醒一点。按剩余空间给，免得十分钟就顶到 100
        self.st["arousal"] = min(
            100.0, self.st["arousal"] + 1.2 * (1 - self.st["arousal"] / 100.0)
        )

        # 心情涨落不在这里判。词表认不出「哼」「行吧」「哦」里的情绪，
        # 这些由模型在回复末尾自己报（见 take_mood_tag / _apply_tag）。
        # 这里只做跟内容无关的事：在场就清醒一点，以及标签的自然演化。
        p = self._person(uid)
        if p["aff"] >= 75 and self.st["arousal"] >= 55:
            self._tag("黏人")
        if p["aff"] >= 75 and self.st["arousal"] >= 80:
            self._tag("黏人")
        # 被夸到顶，反而会不好意思
        if self.st["valence"] >= 70 and self.st["arousal"] >= 70:
            self._tag("羞臊")
        # 同一个人一晚上烦了它好几回，就从「厌烦」滑到「记恨」
        if self._tag_level("厌烦") >= 2:
            self._tag("记恨")
        if not self.st["tags"].get("无聊") and self.st["arousal"] <= 16:
            self._tag("无聊")
        if self.st["arousal"] <= 10:
            self._tag("无聊")
        self._save()

        # 没被叫也可能会接一句——心情上来了才抢话。只在群里做，
        # 私聊每一句都是直接对它说的，插话这个动作没有意义
        if not at_me and gid:
            self._maybe_interject(event, gid)

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        gid = str(event.get_group_id() or "")
        self._tick()
        uid = str(event.get_sender_id() or "")
        hate = self.grudge(uid)
        shown = self._heat(gid, uid)
        v, a = shown["valence"], shown["arousal"]
        tags = self._tags()

        def mood_word() -> str:
            if v <= -50:
                return "憋着火"
            if v <= -20:
                return "不爽"
            if v < 15:
                return "淡淡的"
            if v < 45:
                return "还行"
            return "挺好"

        def drive_word() -> str:
            if a <= 20:
                return "懒得动"
            if a <= 45:
                return "一般"
            if a <= 70:
                return "有点精神"
            return "来劲了"

        def register() -> str:
            # 三条路各自都能单独进脏档，任意两条同时成立只会更狠
            if hate >= GRUDGE_RAW:
                return REGISTER["脏"]
            if hits >= HIT_FULL or shown["anger"] >= RAW_ANGER or v <= RAW_VALENCE:
                return REGISTER["脏"]
            if hate >= GRUDGE_SOUR:
                return REGISTER["损"]
            if shown["anger"] >= SOUR_ANGER or v <= SOUR_VALENCE:
                return REGISTER["损"]
            # 没火但也没电：闷、累、被冷落，答得干，不热络
            # 冷压在和之前：心情高但一点劲都没有，是闷不是暖
            if a <= COLD_AROUSAL or v <= COLD_VALENCE:
                return REGISTER["冷"]
            if v >= 45 and shown["anger"] <= 8:
                return REGISTER["嗨"] if a >= HI_AROUSAL else REGISTER["和"]
            return REGISTER["平"]

        def transition() -> str:
            """离下一档还有多远，用一句人话说出来，省得它一脚油门踩到底。"""
            anger = shown["anger"]
            if hate >= GRUDGE_RAW:
                return "这人有前科，账还记着：见他就没几句好话，别装不认识"
            if reg == REGISTER["脏"]:
                if anger >= RAW_ANGER * 1.8:
                    return "这火已经烧透了，骂得再狠也不违和"
                return "刚骂开，还在往上走：这一句比上一句更冲，别一下就跳成泼妇"
            if reg == REGISTER["损"]:
                if hate >= GRUDGE_SOUR:
                    return "心里有这笔账，嘴上先别全撕开，损一句让他知道你记得"
                if anger >= RAW_ANGER * 0.7:
                    return "快压不住了，再有人递一句就不是损了"
                return "刚有点毛：正事照答，答完补一句损的，别只损不答"
            if reg == REGISTER["嗨"]:
                return "正高兴：可以主动搭话、可以笑，但别嗨到收不住，一句话还是十来字"
            if reg == REGISTER["和"]:
                return "刚缓过来，别接着呛，顺着这个软劲说话；有人问事就好好答"
            if reg == REGISTER["冷"]:
                return "提不起劲：答一句就够，别主动找话题，也别突然热络起来"
            if 0 < hits < HIT_FULL:
                return (
                    f"这两小时里已经被骂 {hits} 次，离翻脸还有 {HIT_FULL - hits} 次，"
                    "现在还端着，最多损一句"
                )
            if anger >= SOUR_ANGER * 0.6:
                return "有点烦但还没到损，语气比上一句紧一点就行，问题还是照答"
            return "火还没起来，正常聊，别急着找茬；有人问正经事就正经答"

        def flavor(aff: float) -> dict:
            """这一轮各张牌的权重。火越大越只剩嘲讽，关系越好越敢撒娇。"""
            if reg == REGISTER["脏"]:
                w = {"嘲讽": 1.0}
            elif reg == REGISTER["损"]:
                w = {"嘲讽": 0.6, "口嗨": 0.4}
            elif reg == REGISTER["嗨"]:
                w = {"撒娇": 0.5, "口嗨": 0.3, "嘲讽": 0.2}
            elif reg == REGISTER["和"]:
                w = {"撒娇": 0.6 if aff >= 55 else 0.2, "口嗨": 0.4}
            elif reg == REGISTER["冷"]:
                # 提不起劲的时候连牌都懒得挑，答一句就完
                w = {}
            elif aff >= 70:
                w = {"撒娇": 0.4, "口嗨": 0.3, "嘲讽": 0.3}
            elif aff <= 25:
                w = {"嘲讽": 1.0}
            else:
                w = {"口嗨": 0.4, "嘲讽": 0.4, "撒娇": 0.2}
            # 离散情绪往上压一格：正得意爱挑刺、黏人爱撒娇、兴奋爱吹
            tilt = {"得意": "嘲讽", "黏人": "撒娇", "兴奋": "口嗨", "厌烦": "嘲讽"}
            for name, card in tilt.items():
                lvl = self._tag_level(name)
                if lvl:
                    w[card] = w.get(card, 0.0) + 0.35 * lvl
            return w

        hits = self._hit_count()
        reg = register()
        lines = [
            "[此刻] 下面是你现在的内在状态，不是台词。",
            "别描述它，别说出“心情”“情绪”“火气”这些词，让语气自己漏出来。",
            "只说话，不写括号里的动作、表情、心理活动。",
            f"状态：{mood_word()}，{drive_word()}",
        ]
        if tags:
            hints = "；".join(
                f"{t}（{'轻' if lvl == 1 else '中' if lvl == 2 else '重'}）——"
                f"{TAG_HINT[t][lvl - 1]}"
                for t, lvl in tags[:2]
            )
            lines.append(f"现在：{hints}")
        lines.append(f"说话：{reg}")
        lines.append(f"火候：{transition()}")
        # 让人自己报账：关键词表只认那几个词，「哼」「哦」「行吧」这种
        # 冷嘲热讽它比表准。标记会被 take_mood_tag 取走，不会漏进群里
        lines.append(
            "说完在最后加一个方括号报这轮的心情变化，例如[心情-4][火气+6]，"
            "数字一到二十。对方是在哄你、事情办成了、听到好消息就心情加；"
            "被怼、被敷衍、被无视就减。火气按这话气不气人定。"
            "没什么变化就不用加。这几个字不会发出去。"
        )



        used = self._recent_words()
        if used:
            lines.append("刚说过的这轮换掉：" + "、".join(used))
        if reg == REGISTER["脏"]:
            lines.append(
                "这轮要骂就挑一个现造的：" + "、".join(homophone_bank())
                + "（同音乱挑出来的，别用上次那批）"
            )
        # 抢话和被人叫走共用同一条回复链路，唯一区别是这句。没这句时下面会写
        # “跟你说话的是某人”，模型就以为有人问它，替别人把话答了。
        # 三态：插件明确说过（_interject 是 True/False）就听它的；没说才按唤醒
        # 标志推断——框架自己的主动回复不走唤醒流程，所以没唤醒就是插话。
        butt_in = event.get_extra("_interject")
        if butt_in is None:
            butt_in = not event.is_at_or_wake_command
        if NICK.search(req.prompt or event.message_str or ""):
            lines.append("有人在叫你——喊的是鲸鱼、小鲸这类称呼，接住，别装没听见")
            butt_in = False

        uid = str(event.get_sender_id() or "")
        name = str(event.get_sender_name() or "").strip()
        # 拍一拍是通知事件，平台侧只塞了 QQ 号当昵称，直接写进块里它就会照着念
        if name.isdigit() or name == uid:
            name = ""
        if uid:
            p = self._person(uid)
            aff = p["aff"]
            if aff <= 10:
                rel = "不想搭理"
            elif aff <= 25:
                rel = "有点烦"
            elif aff <= 45:
                rel = "无感"
            elif aff <= 65:
                rel = "还行"
            elif aff <= 85:
                rel = "挺熟"
            else:
                rel = "护着"
            # A poke is a notice event: the platform fills the nickname with the
            # QQ number, and with the number sitting right there in the block the
            # model reads it out loud ("1831085480，你这手是拍上瘾了？"). The
            # number stays useful for telling two 张伟 apart, so it is only
            # dropped when there is no name to attach it to.
            who = f"{name}（{uid}）" if name else "一个没留名字的人"
            if uid in KEEPERS:
                who = f"{KEEPERS[uid]}（{uid}）"
                rel = "得给面子"
            elif uid in _admins():
                who = f"{OWNER_NAME}（{uid}）"
            if uid in GENTLE_KEEPERS:
                lines.append(
                    f"这是{KEEPERS.get(uid, '他')}：说话收着点，别爆粗、别拿肥和笨开涮，"
                    "他问什么就正经答。饲养员这层关系别说穿，就当是你最给面子的那个。"
                )
            if butt_in:
                # 这条消息不是发给它的。说清楚是谁在跟谁说话，否则它会把
                # 别人的问题当成问自己的，张嘴就替人答应下来。
                tail = (
                    f"这轮没人叫你，是你自己插一句：{who}在跟大家聊，"
                    f"你对他：{rel}。别当有人在问你，别自我介绍、别问“什么事”"
                )
            else:
                tail = f"跟你说话的是{who}，对他：{rel}"
            weights = flavor(aff)
            if uid in GENTLE_KEEPERS:
                # 那边不许阴阳，剩下的照常
                weights.pop("嘲讽", None)
            if weights:
                card = random.choices(
                    list(weights), weights=list(weights.values()), k=1
                )[0]
                lines.append(f"这轮可以这么说话：{FLAVOR[card]}")
            ledger = p["ledger"][-2:]
            if ledger:
                days = []
                for e in ledger:
                    d = (datetime.now() - datetime.fromisoformat(e["t"])).days
                    days.append(("今天", "昨天", "前天")[d] if d < 3 else "前几天")
                tail += "；你记着：" + "、".join(
                    f"{d}{e['s']}" for d, e in zip(days, ledger)
                )
            lines.append(tail)

        # 关键：只追加到当前用户消息的尾部。system_prompt 必须逐字节稳定
        # mark_as_temp：这是这一轮的内心状态，不是聊天内容。存进历史的话，
        # 往后每轮都会重读十几份旧状态，里面「跟你说话的是某某」还会和当前
        # 说话的人打架，模型就开始答错人、接错话。
        req.extra_user_content_parts.append(
            TextPart(text="\n".join(lines)).mark_as_temp()
        )

        gap = float(getattr(self, "_wake_gap", 0.0) or 0.0)
        if gap >= WAKE_MIN_S:
            self._wake_gap = 0.0
            # mark_as_temp：只发给模型看，不写进历史，免得下一轮又读到一遍
            req.extra_user_content_parts.append(
                TextPart(
                    text=f"（你刚重新上线。上次说话到现在已经{_gap_text(gap)}，"
                    "中间你不在。没人问就别提。）"
                ).mark_as_temp()
            )
        self._save()

    @filter.on_llm_response()
    async def strip_meta(self, event: AstrMessageEvent, response: LLMResponse):
        said = ""
        deltas = {}

        def clean(text: str) -> str:
            body, got = take_mood_tag(text)
            for k, v in got.items():
                deltas[k] = deltas.get(k, 0) + v
            return drop_meta(body)

        if response.completion_text:
            response.completion_text = clean(response.completion_text)
            said = response.completion_text
        chain = getattr(response, "result_chain", None)
        if chain and getattr(chain, "chain", None):
            for comp in chain.chain:
                if isinstance(comp, Plain) and getattr(comp, "text", None):
                    comp.text = clean(comp.text)
                    said = said or comp.text
        # 只报了账、一个字没说：那是不说话，不是空消息。给个省略号兜底，
        # 否则框架会把空文本原样发出去，或者退成一条默认提示
        if response.completion_text is not None and not response.completion_text.strip():
            response.completion_text = "……"
        if deltas:
            self._apply_tag(event, deltas)
        self._remember(said)
        self._save()

    def _apply_tag(self, event: AstrMessageEvent, deltas: dict) -> None:
        """把模型自己报的情绪变化记进账。

        模型能读语气，词表不能——「行吧」「哦」「呵」这些话词表一点反应都没有，
        但它自己知道那是被敷衍了。所以增减由它定，这里只负责限幅：
        单轮最多 ±20，免得一句话把火气从 0 拉到 100，档位跳得不像人。

        Args:
            event: 触发这次回复的消息事件。
            deltas: 字段名到增减量的映射，来自 ``take_mood_tag``。
        """
        fields = {"心情": "valence", "火气": "anger", "劲头": "arousal"}
        # 心情有负半边（-100 到 100），火气和劲头只能到 0 为止
        floors = {"valence": -100.0, "anger": 0.0, "arousal": 0.0}
        cap = MOOD_STEP_CAP
        moved = False
        for label, value in deltas.items():
            key = fields.get(label)
            if not key:
                continue
            step = max(-cap, min(cap, float(value)))
            if not step:
                continue
            cur = float(self.st.get(key, BASE[key]))
            # 小数目只许往基线回，不许把它推得更远。一天上百轮对话，
            # 「嗯」回一句 +2 也会把它顶到天上——无意义的话不该改变情绪。
            # 真有事的时候模型自己会报大数（+8 以上就不受这条限制）
            if abs(step) <= 2:
                inward = (cur > BASE[key] and step < 0) or (cur < BASE[key] and step > 0)
                if not inward:
                    continue
            self.st[key] = max(floors[key], min(100.0, cur + step))
            moved = True
        if not moved:
            return
        # 骂人的时候顺带记一笔到这个人头上，关系和记仇才有来源。
        # 注意只认模型自己加的火气：它主动降温不该被当成道歉
        anger_up = deltas.get("火气", 0) > 0
        uid = str(event.get_sender_id() or "")
        if anger_up and uid:
            self._add_hit()
            p = self._person(uid)
            p["aff"] = max(0.0, p["aff"] - 3.0)
            name = str(event.get_sender_name() or uid)
            p["ledger"].append(
                {"t": datetime.now().isoformat(), "s": f"{name}惹你上火", "hurt": True}
            )
            p["ledger"] = p["ledger"][-20:]
            self._tag("恼火")
        elif deltas.get("心情", 0) >= 8 and uid:
            p = self._person(uid)
            p["aff"] = min(100.0, p["aff"] + 2.0)
            self._tag("好奇")

    # ---------- 调试 ----------

    @filter.command("心情")
    async def show(self, event: AstrMessageEvent):
        if getattr(event, "role", "member") != "admin":
            yield event.plain_result("你谁啊，这命令不归你管。")
            return
        gid = str(event.get_group_id() or "")
        uid = str(event.get_sender_id() or "")
        shown = self._heat(gid, uid)
        tags = "、".join(f"{n}{lvl}" for n, lvl in self._tags()) or "无"
        hate = self.grudge(uid)
        grudge_line = f"\n对你的仇：{hate:.1f} 分" if hate > 0 else ""
        yield event.plain_result(
            f"心情 {shown['valence']:.0f}｜劲头 {shown['arousal']:.0f}"
            f"｜火气 {shown['anger']:.0f}{grudge_line}\n"
            f"没被叫也抢话：{self.pick_probability(gid, uid):.0%}\n"
            f"标签：{tags}"
        )
