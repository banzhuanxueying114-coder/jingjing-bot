"""小鲸面板：一个 tkinter 桌面窗口，管 QQ 机器人的 token 消耗与附加功能。

Run it with ``pythonw 面板.py`` (or double-click ``启动面板.bat``). Every
setting is read from and written back to the file that owns it: the framework
settings live in ``cmd_config.json``, each plugin keeps its own ``config.json``,
and the mood and voice state keep their own JSON. Only files whose values
actually changed are written, and each write keeps a timestamped copy of the
old content in ``_面板备份``.

Standard library only. Nothing is exposed to the network.
"""

import json
import re
import socket
import sqlite3
import sys
import tkinter as tk
import tkinter.font as tkfont
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent
QQBOT = ROOT / "qqbot" / "AstrBot"
DATA = QQBOT / "data"
CFG = DATA / "cmd_config.json"
DB = DATA / "data_v4.db"
PLUGINS = DATA / "plugins"
MOOD = PLUGINS / "mood" / "mood.json"
PACE = PLUGINS / "liang_time" / "pace.json"
# The plugins resolve the voice runtime themselves; the panel only reads it to
# tell the user why singing would fail before they spend a turn finding out.
VOICE_ENV = ROOT / "qqbot" / "voice-env"
VOICE_RVC = VOICE_ENV / "src" / "rvc"
VOICE_WEIGHTS = VOICE_RVC / "assets" / "weights"
NOTES = ROOT / "面板备注.json"
PRICES = ROOT / "面板价格.json"
BACKUP = ROOT / "_面板备份"
ERRORLOG = ROOT / "面板错误.log"

# Keep the backup folder from growing without bound: newest N per file stem.
BACKUP_KEEP = 20

# Edge TTS voice ids look like zh-CN-XiaoyiNeural; anything weirder is a typo.
VOICE_ID = re.compile(r"^[a-z]{2,3}-[A-Z]{2,4}-[A-Za-z0-9]{3,40}$")

# A small offline set of Chinese Edge voices, so the field is pickable without
# a network call. Any other valid id can still be typed in.
VOICES = [
    "zh-CN-XiaoyiNeural",
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
]

# Reference prices in yuan per million tokens. DeepSeek's permanent price cut
# put cached input at 0.025, uncached at 3 and output at 6. They are a guess for
# any other model, so the panel lets you change them and says so on screen.
PRICE_DEFAULTS = {"miss": 3.0, "cached": 0.025, "output": 6.0}

BASE_REPLY = ("provider_ltm_settings", "active_reply", "possibility_reply")
MOOD_BASE = {"valence": 12.0, "arousal": 40.0, "anger": 0.0}

# One flat palette. The page is the darkest layer, the sidebar and cards sit
# above it, and fields are darker than the card they live on so the eye reads
# them as holes to type into rather than buttons.
P = {
    "page": "#0e1014",
    "panel": "#151920",
    "card": "#1b2029",
    "card_alt": "#232935",
    "field": "#10141a",
    "border": "#2a3140",
    "accent": "#3b6ef0",
    "accent_dim": "#2f52b0",
    "text": "#e8ebf2",
    "muted": "#8e98ad",
    "faint": "#5d6679",
    "good": "#4fbf7a",
    "warn": "#e0b155",
    "bad": "#e06c75",
}


def pick_font(root: tk.Misc) -> str:
    """Pick a UI font that actually has the Chinese glyphs we draw.

    Args:
        root: Any widget, used to ask Tk for the font list.

    Returns:
        The family name to build fonts from.
    """
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        return "Microsoft YaHei UI"
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Noto Sans CJK SC"):
        if name in available:
            return name
    return "TkDefaultFont"


# Kinds: bool, int, float, str, list, choice, providers.
def F(key, label, kind, default, hint="", lo=None, hi=None, choices=None):
    """Describe one editable control.

    Args:
        key: Key inside the owning JSON object, or a tuple path for framework keys.
        label: Chinese label shown in the window.
        kind: One of bool/int/float/str/list/choice/providers.
        default: Fallback when the file has no value.
        hint: Short grey note shown next to the control.
        lo: Smallest accepted number, or None.
        hi: Largest accepted number, or None.
        choices: Allowed strings for kind ``choice``.

    Returns:
        The field description dict.
    """
    return {
        "key": key, "label": label, "kind": kind, "default": default,
        "hint": hint, "lo": lo, "hi": hi, "choices": choices,
    }


PLUGIN_FIELDS = {
    "stickers": [
        F("random_p", "随机附带表情概率", "float", 0.19, "每次回复后补一张的概率", 0, 1),
        F("group_cooldown", "随机表情冷却（秒）", "int", 120, "同一群两次随机的最小间隔", 0, 86400),
        F("max_per_hour", "每群每小时上限", "int", 30, "", 0, 200),
        F("keyword_cooldown", "关键词冷却（秒）", "int", 15, "说“来个表情包”之类的冷却", 0, 3600),
    ],
    "owner_boost": [
        F("owner_probability", "管理员插话概率", "float", 0.5, "管理员没 @ 时被接话的概率", 0, 1),
    ],
    "clause_split": [
        F("max_parts", "最多拆几段", "int", 6, "超过就整条发", 1, 12),
        F("max_part_len", "每段最长字数", "int", 12, "", 2, 60),
        F("max_total", "整条最长字数", "int", 40, "超过就不拆", 4, 200),
    ],
    "websearch": [
        F("max_results", "搜索结果条数", "int", 3, "越多越费 token", 1, 10),
        F("snippet_chars", "每条摘要字数", "int", 110, "", 20, 400),
        F("max_payload", "一次搜索返回上限（字）", "int", 700, "", 100, 4000),
    ],
    "quote_reply": [
        F("stale_seconds", "群消息过期秒数", "int", 1800, "这么久没动静就忘掉最新消息", 60, 86400),
    ],
    "poke_reply": [
        F("cooldown", "拍一拍冷却（秒）", "int", 20, "", 0, 3600),
    ],
    "memorize": [
        F("max_notes", "每人最多记几条短记忆", "int", 4, "", 0, 20),
        F("max_note_len", "单条短记忆字数", "int", 60, "", 10, 300),
        F("profile_cap", "每人印象档案上限（字）", "int", 2500, "", 200, 20000),
        F("inject_tail", "平时注入印象长度（字）", "int", 500, "每轮都会花这么多 token", 0, 4000),
        F("inject_tail_deep", "被问“印象”时注入长度（字）", "int", 1200, "", 0, 6000),
        F("auto_impression", "自动积累个人印象", "bool", True, "开关也会花模型调用"),
        F("auto_every", "每几条消息提取一次", "int", 20, "", 1, 500),
        F("auto_cooldown", "同一人提取冷却（秒）", "int", 7200, "", 60, 604800),
        F("auto_group", "自动积累群印象", "bool", True, ""),
        F("group_every", "群聊每几条提取一次", "int", 40, "", 1, 500),
        F("group_cooldown", "群印象提取冷却（秒）", "int", 14400, "", 60, 604800),
        F("group_cap", "一个群最多记几条", "int", 4, "", 0, 20),
        F("group_window", "提取时回看多少条群聊", "int", 30, "", 1, 500),
        F("group_line_len", "群印象单条字数", "int", 60, "", 10, 300),
    ],
    "voice": [
        F("enabled", "启用语音", "bool", True, "关掉就只发文字，也不翻唱"),
        F("voice", "音色", "str", "blend_teto_403525_mixlty",
          "weights 目录里的 .pth 名，不含后缀"),
        F("voice_index", "检索索引", "str",
          "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2",
          "weights 目录里的 .index 名；混合音色借谁的索引就被往谁那边拉"),
        F("timeout", "换声超时（秒）", "int", 900, "", 30, 3600),
        F("cache_limit_bytes", "翻唱缓存上限（字节）", "int", 2147483648, "默认 2 GB", 0, 10**12),
    ],
    "crowd_guard": [
        F("enabled", "启用人多降频", "bool", True, ""),
        F("base_size", "超过多少人开始减半", "int", 400, "", 10, 5000),
        F("cache_seconds", "人数缓存（秒）", "int", 600, "", 30, 86400),
    ],
    "censor_fallback": [
        F("group_id", "生效的群号", "str", "", "只在这个群把拒答换成一句短的；留空则哪都不生效"),
        F("fallback", "替换成什么", "str", "懒得喷", ""),
    ],
    "illust": [
        F("enabled", "启用搬图", "bool", True, ""),
        F("cooldown_seconds", "搬图冷却（秒）", "int", 10, "", 0, 3600),
        F("max_per_hour", "搬图每小时上限", "int", 20, "", 0, 200),
        F("llm_with_image", "搬图后配一句话（新网关）", "bool", True, "会多花一次模型调用"),
    ],
    "mute_guard": [
        F("enabled", "启用禁言守卫", "bool", True, "被禁言时直接不回，不花钱"),
        F("refresh_seconds", "禁言状态刷新（秒）", "int", 300, "", 30, 3600),
    ],
    "repeat_follow": [
        F("enabled", "启用跟读", "bool", True, ""),
        F("min_people", "几个人一起说才跟", "int", 3, "", 2, 10),
        F("cooldown_seconds", "跟读冷却（秒）", "int", 120, "", 0, 3600),
        F("window_seconds", "统计窗口（秒）", "int", 90, "", 10, 3600),
        F("max_text_len", "跟读最长字数", "int", 60, "", 4, 300),
    ],
    "liang_time": [
        F("enabled", "启用梁文峰时间", "bool", True, "上班时段概率减半"),
        F("fast_multiplier", "新网关概率倍数", "float", 2.0, "和减半可叠加", 0.1, 10),
    ],
}

# Friendly headings for the plugin page. The key on the left is also the folder
# name, and that file path is shown as the card's grey note.
PLUGIN_TITLES = {
    "stickers": "表情包",
    "owner_boost": "管理员接话",
    "clause_split": "分句发送",
    "websearch": "联网搜索",
    "quote_reply": "引用回复",
    "poke_reply": "拍一拍",
    "memorize": "记忆",
    "voice": "翻唱",
    "crowd_guard": "人多降频",
    "illust": "搬图",
    "mute_guard": "禁言守卫",
    "repeat_follow": "跟读",
    "censor_fallback": "拒答替换",
    "liang_time": "节奏（上班时段）",
}

MOOD_FIELDS = [
    F("mood_step_cap", "单轮心情/火气最大变化", "float", 20.0, "", 0.5, 100),
    F("grudge_raw", "记仇进脏档的分数", "float", 1.0, "", 0.05, 20),
    F("grudge_sour", "记仇进损档的分数", "float", 0.4, "", 0.05, 20),
    F("grudge_decay_days", "记仇淡出天数", "float", 7.0, "", 0.5, 90),
    F("grudge_heat", "记仇抬高火气", "float", 6.0, "", 0, 30),
    F("grudge_mood", "记仇压低心情", "float", 3.0, "", 0, 30),
    F("raw_anger", "脏档火气门槛", "float", 26.0, "", 0, 100),
    F("raw_valence", "脏档心情门槛", "float", -12.0, "", -100, 100),
    F("sour_anger", "损档火气门槛", "float", 10.0, "", 0, 100),
    F("sour_valence", "损档心情门槛", "float", -6.0, "", -100, 100),
    F("cold_valence", "冷档心情上限", "float", 2.0, "", -100, 100),
    F("cold_arousal", "冷档劲头上限", "float", 26.0, "", 0, 100),
    F("hi_arousal", "嗨档劲头下限", "float", 62.0, "", 0, 100),
    F("hit_full", "两小时内被骂几次就翻脸", "int", 4, "", 1, 20),
    F("hit_window_s", "挨骂计数窗口（秒）", "int", 7200, "", 60, 86400),
    F("ledger_keep_days", "账本保留天数", "int", 7, "", 1, 365),
    F("pick_base", "主动开口基础概率", "float", 0.04, "低于框架的 0.5% 时才起作用", 0, 1),
    F("pick_max", "主动开口概率上限", "float", 0.15, "", 0, 1),
    F("pick_cooldown_s", "主动开口冷却（秒）", "int", 120, "", 0, 7200),
    F("wake_min_s", "被叫醒的最短间隔（秒）", "int", 3600, "", 60, 86400),
]

SPEAK_FIELDS = [
    F("enabled", "开关", "bool", True, "关掉就只发文字"),
    F("calm_chance", "平常概率", "float", 0.1, "0~1，0.1 即 10%", 0, 1),
    F("angry_chance", "火气大时概率", "float", 0.5, "火气到门槛后用这个", 0, 1),
    F("angry_anger", "火气门槛", "float", 26.0, "默认 26，跟脏档同一条线", 0, 100),
    F("pitch", "音高（半音）", "int", 5, "+5 在她唱歌的音域内", -12, 12),
    F("max_chars", "最长字数", "int", 60, "超过就改发文字", 4, 200),
    F("voice", "底嗓", "choice", "zh-CN-XiaoyiNeural", "Edge TTS 的 voice id", choices=VOICES),
]

# Framework knobs, with the path into cmd_config.json.
FRAMEWORK_FIELDS = [
    F(("agent_runner", "config", "model", "provider_id"), "主模型", "providers", "",
      "只能选 provider 里已有的 id，不会打错"),
    F(("agent_runner", "config", "model", "fallback_provider_ids"), "备用模型（逗号分隔）", "list",
      [], "主模型失败时依次尝试，越少越省钱"),
    F(("agent_runner", "config", "model", "request_max_retries"), "失败重试次数", "int", 2,
      "每多一次都可能多花一次钱", 1, 10),
    F(("agent_runner", "config", "compression", "max_turns"), "上下文保留轮数", "int", 40,
      "超过就截断，截断后前缀缓存失效一次", 2, 200),
    F(("agent_runner", "config", "compression", "trim_turns"), "截断后保留轮数", "int", 16,
      "比 max_turns 小，越小越省也越容易忘", 1, 200),
    F(("agent_runner", "config", "compression", "overflow_strategy"), "截断策略", "choice",
      "truncate_by_turns", "llm_compress 会多调一次模型", choices=["truncate_by_turns", "llm_compress"]),
    F(("agent_runner", "config", "misc", "max_steps"), "工具调用上限", "int", 128, "", 1, 512),
    F(("agent_runner", "config", "misc", "tool_schema_mode"), "工具描述模式", "choice", "full",
      "skills_like 更省 token", choices=["full", "skills_like"]),
    F(("agent_runner", "config", "misc", "tool_call_timeout"), "工具超时（秒）", "int", 120, "", 5, 600),
    F(("agent_runner", "config", "persona", "safety_mode"), "安全模式", "bool", False,
      "开了每次请求多塞一段提示"),
    F(("provider_settings", "streaming_response"), "流式输出", "bool", False, ""),
    F(("provider_settings", "display_reasoning_text"), "显示思考文字", "bool", False, ""),
    F(("provider_settings", "datetime_system_prompt"), "注入当前时间", "bool", True,
      "每轮都会带一行时间"),
    F(("provider_settings", "web_search"), "框架联网搜索", "bool", False,
      "关掉，用 websearch 插件那套"),
    F(("provider_settings", "image_compress_options", "max_size"), "图片压缩上限（像素）", "int", 512,
      "越小越省", 64, 2048),
    F(("provider_ltm_settings", "group_context_window"), "群聊窗口（条/轮）", "int", 60,
      "每轮都重注一遍，永远不进缓存；60 条约 1600 token", 1, 200),
    F(("provider_ltm_settings", "group_message_max_cnt"), "群内存缓冲条数", "int", 1000,
      "窗口能往回看多远的上限", 1, 5000),
    F(("provider_ltm_settings", "group_message_history_max_cnt"), "群记录保存条数", "int", 700,
      "platform_message_history 表里每组留多少条", 0, 10000),
    F(("provider_ltm_settings", "image_caption"), "图片转文字", "bool", True,
      "每张图都要一次模型调用"),
    F(("provider_ltm_settings", "group_icl_enable"), "群内上下文学习", "bool", True, ""),
    F(("platform_settings", "reply_with_quote"), "框架引用回复", "bool", False,
      "关掉后由 quote_reply 插件决定"),
]


def parse_windows(text: str) -> list[list[str]]:
    """Parse ``09:00-12:00, 14:00-18:00`` into the list liang_time stores.

    Args:
        text: Comma separated ranges using ``-`` or ``~`` between the ends.

    Returns:
        A list of ``[start, end]`` pairs in HH:MM form.

    Raises:
        ValueError: When a range is malformed or the times are not valid.
    """
    out = []
    for chunk in re.split(r"[,，;；]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = re.split(r"[-~—到]", chunk)
        if len(parts) != 2:
            raise ValueError(f"时段「{chunk}」要写成 09:00-12:00 这种")
        pair = []
        for part in parts:
            part = part.strip()
            if not re.fullmatch(r"\d{1,2}:\d{2}", part):
                raise ValueError(f"时段「{chunk}」里的时间要写成 HH:MM")
            hour, minute = (int(x) for x in part.split(":"))
            if hour > 23 or minute > 59:
                raise ValueError(f"时段「{chunk}」里的时间不存在")
            pair.append(f"{hour:02d}:{minute:02d}")
        out.append(pair)
    return out


def format_windows(raw) -> str:
    """Render liang_time's window list back into one editable line."""
    pairs = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append(f"{item[0]}-{item[1]}")
    return ", ".join(pairs)


def read_json(path: Path) -> dict:
    """Read a JSON object, returning an empty dict when missing or broken.

    Args:
        path: The file to read.

    Returns:
        The parsed object, or ``{}``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict, backup: bool = True, label: str = "") -> None:
    """Write a JSON object atomically, keeping a timestamped copy of the old one.

    Args:
        path: The file to write.
        data: The object to store.
        backup: Whether to keep the previous content in ``_面板备份``.
        label: Name of the backup file stem. Plugin configs are all called
            ``config.json``, so without this every plugin would share one pile
            of backups with no way to tell which file came from where.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = label or path.stem
    if backup and path.exists():
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (BACKUP / f"{stem}.{stamp}.json").write_bytes(path.read_bytes())
        for stale in sorted(BACKUP.glob(f"{stem}.*.json"))[:-BACKUP_KEEP]:
            stale.unlink(missing_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def walk(data: dict, path: tuple, default=None):
    """Read a nested value by key path.

    Args:
        data: The root object.
        path: Keys to follow.
        default: Returned when any key is missing.

    Returns:
        The value found, or ``default``.
    """
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def put(data: dict, path: tuple, value) -> None:
    """Write a nested value by key path, creating intermediate objects.

    Args:
        data: The root object, modified in place.
        path: Keys to follow.
        value: The value to store.
    """
    cur = data
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def voice_status() -> tuple[bool, str]:
    """Tell whether the voice runtime on this machine is complete.

    The panel cannot import the plugin, so the same file list is checked here
    once: a missing interpreter, ffmpeg, model or voice otherwise only shows up
    as a failed song after a whole minute of work.

    Returns:
        ``(usable, reason)``; the reason names the first missing piece.
    """
    if not VOICE_ENV.is_dir():
        return False, f"没有语音运行时目录 {VOICE_ENV}"
    checks = (
        (VOICE_ENV / "Scripts" / "python.exe", "语音运行时解释器"),
        (VOICE_ENV / "bin" / "ffmpeg.exe", "ffmpeg"),
        (VOICE_RVC / "infer" / "cli.py", "RVC 推理入口"),
        (VOICE_RVC / "assets" / "hubert_base", "HuBERT 模型"),
        (VOICE_RVC / "assets" / "rmvpe", "RMVPE 音高模型"),
    )
    for path, label in checks:
        if not path.exists():
            return False, f"缺{label}：{path}"
    cfg = read_json(PLUGINS / "voice" / "config.json")
    if not cfg.get("enabled", True):
        return False, "语音在翻唱的 config.json 里被关掉了"
    voice = str(cfg.get("voice") or "")
    index = str(cfg.get("voice_index") or "")
    if voice and not (VOICE_WEIGHTS / f"{voice}.pth").is_file():
        return False, f"缺音色文件 {voice}.pth"
    if index and not (VOICE_WEIGHTS / f"{index}.index").is_file():
        return False, f"缺检索索引 {index}.index"
    return True, ""


def bot_running() -> bool:
    """Guess whether AstrBot is up by probing its dashboard port.

    Returns:
        True when something answers on 127.0.0.1:6185.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", 6185)) == 0


def usage_rows(since: datetime | None) -> list[dict]:
    """Summarise token usage per model from the framework's own stats table.

    Args:
        since: Only rows at or after this UTC moment, or None for everything.

    Returns:
        One dict per model, newest data first, plus a ``__total`` row.
    """
    if not DB.exists():
        return []
    sql = (
        "select provider_model, count(*), sum(token_input_other), "
        "sum(token_input_cached), sum(token_output) from provider_stats"
    )
    args: tuple = ()
    if since is not None:
        sql += " where created_at >= ?"
        args = (since.strftime("%Y-%m-%d %H:%M:%S"),)
    sql += " group by 1 order by 2 desc"
    try:
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    total = ["合计", 0, 0, 0, 0]
    for model, calls, miss, cached, output in rows:
        item = [model or "未知", calls, miss or 0, cached or 0, output or 0]
        out.append(item)
        for i in range(1, 5):
            total[i] += item[i]
    if total[1]:
        out.append(total)
    return out


def money(row: list, price: dict) -> float:
    """Estimate what one usage row cost.

    Args:
        row: ``[model, calls, miss, cached, output]``.
        price: Yuan per million tokens with keys miss/cached/output.

    Returns:
        The estimated cost in yuan.
    """
    return (row[2] * price["miss"] + row[3] * price["cached"] + row[4] * price["output"]) / 1e6


class Toggle(tk.Canvas):
    """A pill shaped switch bound to a BooleanVar."""

    def __init__(self, master, variable: tk.BooleanVar) -> None:
        super().__init__(master, width=46, height=24, highlightthickness=0, bd=0,
                         bg=P["card"], cursor="hand2")
        self.variable = variable
        self.bind("<Button-1>", self._flip)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw(hover=False))
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _flip(self, _event=None) -> None:
        self.variable.set(not self.variable.get())

    def _draw(self, hover: bool = False) -> None:
        on = bool(self.variable.get())
        track = P["accent"] if on else ("#333c4c" if hover else "#2b3341")
        self.delete("all")
        self.create_rectangle(12, 4, 34, 20, fill=track, outline="")
        self.create_oval(2, 4, 22, 20, fill=track, outline="")
        self.create_oval(24, 4, 44, 20, fill=track, outline="")
        knob = 34 if on else 12
        self.create_oval(knob - 8, 4, knob + 8, 20,
                         fill="#ffffff" if on else "#9aa4b8", outline="")


class Panel(tk.Tk):
    """The main window: a sidebar of pages, each holding a set of settings."""

    NAV = ["群白名单", "回复概率", "token 与模型", "用量", "附加功能", "心情", "语音"]

    def __init__(self) -> None:
        super().__init__()
        self.title("小鲸面板")
        self.geometry("1020x740")
        self.minsize(880, 620)
        self.configure(bg=P["page"])
        self.font = pick_font(self)
        self._style()
        self.specs: list[dict] = []
        self.vars: dict[tuple, tk.Variable] = {}
        self.loaded: dict[tuple, object] = {}
        self.combos: dict[tuple, ttk.Combobox] = {}
        self.pages: list[tk.Frame] = []
        self.nav_items: list[tuple] = []
        self.page_index = 0
        self.status = tk.StringVar(value="")
        self.note_var = tk.StringVar()
        self._notes: dict = {}
        self._header()
        self._footer()
        body = tk.Frame(self, bg=P["page"])
        body.pack(fill="both", expand=True)
        self.nav = tk.Frame(body, bg=P["panel"], width=172)
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)
        self.content = tk.Frame(body, bg=P["page"])
        self.content.pack(side="left", fill="both", expand=True)
        self._tab_whitelist()
        self._tab_reply()
        self._tab_tokens()
        self._tab_usage()
        self._tab_plugins()
        self._tab_mood()
        self._tab_voice()
        for index, label in enumerate(self.NAV):
            self._nav_item(label, index)
        self.show_page(0)
        self.reload()
        self.refresh_usage()
        self._poll()

    def _header(self) -> None:
        """Draw the title bar with the whale badge."""
        head = tk.Frame(self, bg=P["panel"])
        head.pack(fill="x")
        inner = tk.Frame(head, bg=P["panel"])
        inner.pack(fill="x", padx=16, pady=10)
        badge = tk.Canvas(inner, width=38, height=38, bg=P["panel"], highlightthickness=0)
        badge.pack(side="left")
        badge.create_oval(1, 1, 37, 37, fill=P["accent_dim"], outline="")
        badge.create_text(19, 19, text="鲸", fill="#ffffff", font=(self.font, 13, "bold"))
        texts = tk.Frame(inner, bg=P["panel"])
        texts.pack(side="left", padx=10)
        tk.Label(texts, text="小鲸面板", bg=P["panel"], fg=P["text"],
                 font=(self.font, 13, "bold")).pack(anchor="w")
        tk.Label(texts, text="QQ 机器人 · token 消耗与附加功能", bg=P["panel"], fg=P["muted"],
                 font=(self.font, 8)).pack(anchor="w")
        tk.Frame(head, bg=P["border"], height=1).pack(fill="x")

    def _footer(self) -> None:
        """Draw the bottom bar holding the save buttons and live status."""
        foot = tk.Frame(self, bg=P["panel"])
        foot.pack(side="bottom", fill="x")
        tk.Frame(foot, bg=P["border"], height=1).pack(fill="x")
        inner = tk.Frame(foot, bg=P["panel"])
        inner.pack(fill="x", padx=16, pady=10)
        self.save_btn = tk.Button(
            inner, text="保存全部", command=self.save_all, bg=P["accent"], fg="#ffffff",
            activebackground=P["accent_dim"], activeforeground="#ffffff", bd=0,
            relief="flat", padx=18, pady=6, cursor="hand2", font=(self.font, 9, "bold"),
        )
        self.save_btn.pack(side="left")
        reload_btn = tk.Button(
            inner, text="重新读取", command=self.reload, bg=P["field"], fg=P["text"],
            activebackground=P["card_alt"], activeforeground=P["text"], bd=0,
            relief="flat", padx=16, pady=6, cursor="hand2", font=(self.font, 9),
        )
        reload_btn.pack(side="left", padx=8)
        tk.Label(inner, textvariable=self.status, bg=P["panel"], fg=P["good"],
                 font=(self.font, 9), anchor="w").pack(side="left", padx=10)
        self.running_label = tk.Label(inner, text="", bg=P["card_alt"], fg=P["warn"],
                                      font=(self.font, 8), padx=10, pady=3)
        self.running_label.pack(side="right")

    def _style(self) -> None:
        """Configure ttk widgets to match the dark palette."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=(self.font, 10), background=P["card"],
                        foreground=P["text"], fieldbackground=P["field"],
                        bordercolor=P["border"], focuscolor=P["accent"])
        style.configure("TFrame", background=P["card"])
        style.configure("TLabel", background=P["card"], foreground=P["text"])
        style.configure("Ghost.TButton", background=P["field"], foreground=P["text"],
                        borderwidth=0, padding=(14, 6), relief="flat")
        style.map("Ghost.TButton",
                  background=[("disabled", P["field"]), ("active", P["card_alt"])],
                  foreground=[("disabled", P["faint"])])
        style.configure("Danger.TButton", background="#3a2124", foreground=P["bad"],
                        borderwidth=0, padding=(14, 6), relief="flat")
        style.map("Danger.TButton",
                  background=[("disabled", P["field"]), ("active", "#4a272b")],
                  foreground=[("disabled", P["faint"])])
        # A readonly Combobox otherwise paints its field white and its text
        # white, which reads as an empty box on a dark card.
        style.configure(
            "Dark.TCombobox", fieldbackground=P["field"], background=P["field"],
            foreground=P["text"], arrowcolor=P["muted"], bordercolor=P["border"],
            lightcolor=P["border"], darkcolor=P["border"], borderwidth=1,
            padding=(8, 5), relief="flat",
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", P["field"]), ("disabled", P["field"])],
            background=[("readonly", P["field"]), ("active", P["field"])],
            foreground=[("readonly", P["text"]), ("disabled", P["faint"])],
            selectbackground=[("readonly", P["field"])],
            selectforeground=[("readonly", P["text"])],
            arrowcolor=[("active", P["text"])],
        )
        self.option_add("*TCombobox*Listbox.background", P["card_alt"])
        self.option_add("*TCombobox*Listbox.foreground", P["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", P["accent_dim"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style.configure("Treeview", background=P["card"], fieldbackground=P["card"],
                        foreground=P["text"], borderwidth=0, rowheight=28,
                        font=(self.font, 9))
        style.configure("Treeview.Heading", background=P["field"], foreground=P["muted"],
                        borderwidth=0, relief="flat", padding=(6, 7),
                        font=(self.font, 9, "bold"))
        style.map("Treeview", background=[("selected", P["accent_dim"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Thin.Vertical.TScrollbar", background=P["border"],
                        troughcolor=P["page"], borderwidth=0, arrowsize=0, width=8)
        style.map("Thin.Vertical.TScrollbar", background=[("active", P["faint"])])

    def _nav_item(self, text: str, index: int) -> None:
        """Add one sidebar entry."""
        holder = tk.Frame(self.nav, bg=P["panel"], cursor="hand2")
        holder.pack(fill="x")
        bar = tk.Frame(holder, bg=P["panel"], width=3)
        bar.pack(side="left", fill="y")
        label = tk.Label(holder, text=text, bg=P["panel"], fg=P["muted"], anchor="w",
                         font=(self.font, 10), padx=14, pady=10, cursor="hand2")
        label.pack(side="left", fill="x", expand=True)
        self.nav_items.append((holder, bar, label))
        for widget in (holder, label):
            widget.bind("<Button-1>", lambda e, i=index: self.show_page(i))
            widget.bind("<Enter>", lambda e, i=index: self._nav_hover(i, True))
            widget.bind("<Leave>", lambda e, i=index: self._nav_hover(i, False))

    def _nav_hover(self, index: int, on: bool) -> None:
        """Light the sidebar entry under the pointer, except the active one."""
        if index == self.page_index:
            return
        holder, bar, label = self.nav_items[index]
        colour = P["card"] if on else P["panel"]
        holder.config(bg=colour)
        label.config(bg=colour)

    def show_page(self, index: int) -> None:
        """Switch to one page and mark its sidebar entry."""
        for i, page in enumerate(self.pages):
            if i == index:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for i, (holder, bar, label) in enumerate(self.nav_items):
            active = i == index
            colour = P["card_alt"] if active else P["panel"]
            holder.config(bg=colour)
            label.config(bg=colour, fg=P["accent"] if active else P["muted"])
            bar.config(bg=P["accent"] if active else colour)
        self.page_index = index

    def _page(self, title: str, subtitle: str) -> tk.Frame:
        """Add a scrollable page and return the frame rows go into.

        Args:
            title: Large heading at the top of the page.
            subtitle: Grey explanation under it.

        Returns:
            The frame card contents are packed into.
        """
        outer = tk.Frame(self.content, bg=P["page"])
        self.pages.append(outer)
        head = tk.Frame(outer, bg=P["page"])
        head.pack(fill="x", padx=22, pady=(18, 10))
        tk.Label(head, text=title, bg=P["page"], fg=P["text"], font=(self.font, 16, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(head, text=subtitle, bg=P["page"], fg=P["muted"], font=(self.font, 9),
                 anchor="w", justify="left", wraplength=740).pack(fill="x", pady=(3, 0))
        canvas = tk.Canvas(outer, bg=P["page"], highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                            style="Thin.Vertical.TScrollbar")
        body = tk.Frame(canvas, bg=P["page"])
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def wheel(event):
            # Only scroll the page under the pointer; a global binding would
            # drag every page at once.
            if canvas.winfo_containing(event.x_root, event.y_root) is not None:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel, add="+"))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return body

    def _card(self, parent: tk.Frame, title: str = "", note: str = "") -> tk.Frame:
        """Add a bordered card and return its grid for rows.

        Args:
            parent: The page body.
            title: Card heading, or empty for none.
            note: Grey line under the heading.

        Returns:
            A frame already laid out as a three column grid.
        """
        border = tk.Frame(parent, bg=P["card"], highlightbackground=P["border"],
                          highlightthickness=1, bd=0)
        border.pack(fill="x", padx=22, pady=(0, 12))
        pad = tk.Frame(border, bg=P["card"])
        pad.pack(fill="x", padx=16, pady=12)
        if title:
            tk.Label(pad, text=title, bg=P["card"], fg=P["text"], font=(self.font, 11, "bold"),
                     anchor="w").pack(fill="x")
        if note:
            tk.Label(pad, text=note, bg=P["card"], fg=P["muted"], font=(self.font, 9),
                     anchor="w", justify="left", wraplength=720).pack(fill="x", pady=(3, 6))
        grid = tk.Frame(pad, bg=P["card"])
        grid.pack(fill="x", pady=(6, 0))
        # Column 0 holds labels, 1 the control, 2 the hint. The hint must wrap
        # rather than ask for its natural width, or the card grows past the page
        # and the tail is clipped by the window edge.
        grid.columnconfigure(0, minsize=170, weight=0)
        grid.columnconfigure(1, minsize=250, weight=0)
        grid.columnconfigure(2, weight=1)
        return grid

    def _field(self, parent: tk.Frame, text: str, width: int = 24) -> tk.Entry:
        """Add a dark flat entry to ``parent``."""
        return tk.Entry(parent, textvariable=text, bg=P["field"], fg=P["text"],
                        insertbackground=P["text"], relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=P["border"],
                        highlightcolor=P["accent"], font=(self.font, 10), width=width)

    def _row(self, parent: tk.Frame, spec: dict, key: tuple, value, choices=None) -> None:
        """Add one labelled control and remember it for save/reload.

        Args:
            parent: The card grid to add the row to.
            spec: The field description.
            key: Unique key for this control in ``self.vars``.
            value: The current value read from disk.
            choices: Provider ids for kind ``providers``.
        """
        row = parent.grid_size()[1]
        tk.Label(parent, text=spec["label"], bg=P["card"], fg=P["text"],
                 font=(self.font, 10), anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        kind = spec["kind"]
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            Toggle(parent, var).grid(row=row, column=1, sticky="w", pady=4)
        elif kind in ("choice", "providers"):
            var = tk.StringVar(value=str(value if value is not None else ""))
            items = list(spec["choices"] or choices or [])
            # A hand-edited id that is no longer in the provider list must still
            # show up, otherwise opening and saving would silently drop it.
            if kind == "providers" and str(value) not in items:
                items.insert(0, str(value))
            box = ttk.Combobox(parent, textvariable=var, values=items, width=24,
                               state="readonly", style="Dark.TCombobox")
            box.grid(row=row, column=1, sticky="w", pady=4)
            self.combos[key] = box
        else:
            text = value
            if kind == "list":
                text = ", ".join(str(v) for v in (value or []))
            elif value is None:
                text = ""
            var = tk.StringVar(value=str(text))
            self._field(parent, var).grid(row=row, column=1, sticky="w", ipady=4, pady=4)
        if spec["hint"]:
            # The hint gets whatever is left of the card. Wrapping it to a fixed
            # width would push the card wider than the page and clip the tail.
            wrap = 240 if len(spec["hint"]) > 18 else 400
            tk.Label(parent, text=spec["hint"], bg=P["card"], fg=P["faint"],
                     font=(self.font, 9), anchor="w", justify="left", wraplength=wrap).grid(
                row=row, column=2, sticky="w", padx=(12, 0), pady=4)
        self.vars[key] = var
        self.specs.append({**spec, "bind": key})

    def _tab_whitelist(self) -> None:
        body = self._page(
            "群白名单",
            "只允许这些群里的消息进入机器人；列表空表示所有群都能用。白名单改动要重启机器人。",
        )
        card = self._card(body, "入口开关")
        for key, label, hint in (
            ("enable_id_white_list", "启用白名单", "关掉就等于所有群都能进"),
            ("wl_ignore_admin_on_group", "管理员在群里豁免", "管理员说话不受白名单限制"),
            ("wl_ignore_admin_on_friend", "管理员私聊豁免", ""),
        ):
            spec = F(("platform_settings", key), label, "bool", False, hint)
            self._row(card, spec, ("fw", "platform_settings", key),
                      read_json(CFG).get("platform_settings", {}).get(key, False))

        card = self._card(body, "允许的群", "选中一行可以改备注。备注只存在面板自己的文件里。")
        frame = tk.Frame(card, bg=P["field"], highlightbackground=P["border"],
                         highlightthickness=1, bd=0)
        frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(2, 10))
        self.wl_box = tk.Listbox(frame, height=6, bg=P["field"], fg=P["text"],
                                 selectbackground=P["accent_dim"], selectforeground="#ffffff",
                                 bd=0, highlightthickness=0, activestyle="none",
                                 font=(self.font, 10))
        self.wl_box.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        wl_bar = ttk.Scrollbar(frame, orient="vertical", command=self.wl_box.yview,
                               style="Thin.Vertical.TScrollbar")
        wl_bar.pack(side="right", fill="y")
        self.wl_box.config(yscrollcommand=wl_bar.set)
        self.wl_box.bind("<<ListboxSelect>>", self._wl_pick)

        tools = tk.Frame(card, bg=P["card"])
        tools.grid(row=1, column=0, columnspan=3, sticky="w")
        self.wl_entry = tk.StringVar()
        self._field(tools, self.wl_entry, 18).pack(side="left", ipady=4)
        ttk.Button(tools, text="添加", command=self.wl_add,
                   style="Ghost.TButton").pack(side="left", padx=(8, 6))
        ttk.Button(tools, text="删除选中", command=self.wl_del,
                   style="Danger.TButton").pack(side="left")

        notes = tk.Frame(card, bg=P["card"])
        notes.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tk.Label(notes, text="备注", bg=P["card"], fg=P["muted"], font=(self.font, 9),
                 anchor="w").pack(side="left", padx=(0, 8))
        self._field(notes, self.note_var, 34).pack(side="left", ipady=4)
        ttk.Button(notes, text="保存备注", command=self.wl_note,
                   style="Ghost.TButton").pack(side="left", padx=(8, 0))

    def _tab_reply(self) -> None:
        body = self._page("回复概率", "框架的主动回复基准概率。面板会同时写 cmd_config.json 和 pace.json。")
        card = self._card(
            body, "基准",
            "只改 cmd_config.json 那一处会被节奏插件按已知倍数误判成「算出来的值」再改回去，"
            "所以面板两个文件一起写。",
        )
        self._row(card, F(BASE_REPLY, "基准概率", "float", 0.005, "0 到 1，0.06 即 6%", 0, 1),
                  ("reply",), 0.005)
        self.pace_hint = tk.StringVar(value="")
        tk.Label(card, textvariable=self.pace_hint, bg=P["card"], fg=P["faint"],
                 font=(self.font, 9), anchor="w").grid(row=1, column=0, columnspan=3,
                                                       sticky="w", pady=(4, 0))

        card = self._card(
            body, "节奏插件（liang_time）",
            "梁文峰时间内概率乘 0.5，走新网关时乘下面的倍数，两者能叠加。"
            "时段按 HH:MM-HH:MM 写，多个用逗号隔开。",
        )
        self.window_var = tk.StringVar()
        tk.Label(card, text="梁文峰时间", bg=P["card"], fg=P["text"],
                 font=(self.font, 10), anchor="w").grid(row=0, column=0, sticky="w", pady=4)
        window_entry = self._field(card, self.window_var, 34)
        window_entry.grid(row=0, column=1, sticky="w", ipady=4, pady=4)
        tk.Label(card, text="留空表示没有这个时段", bg=P["card"], fg=P["faint"],
                 font=(self.font, 9), anchor="w").grid(row=0, column=2, sticky="w",
                                                       padx=(12, 0), pady=4)
        self._row(card, F("fast_source_id", "新网关来源 id", "str", "",
                          "provider_sources 里的那条"),
                  ("plugin", "liang_time", "fast_source_id"), "")

        card = self._card(body, "此刻", "按现在的钟点和正在用的网关算，每 5 秒刷新。")
        self.effective = tk.StringVar(value="")
        self.effective_label = tk.Label(
            card, textvariable=self.effective, bg=P["card"], fg=P["accent"],
            font=(self.font, 12, "bold"), anchor="w", justify="left", wraplength=720,
        )
        self.effective_label.grid(row=0, column=0, columnspan=3, sticky="w", pady=2)

    def _tab_tokens(self) -> None:
        body = self._page("token 与模型", "这些项直接决定每次调用花多少 token。改完需要重启机器人。")
        cfg = read_json(CFG)
        self.providers = [str(item.get("id")) for item in cfg.get("provider") or []
                          if item.get("provider_type") in (None, "chat_completion")]
        groups = (
            ("模型与重试", 0, 3, "换模型、失败重试。备用模型越少越省钱。"),
            ("上下文与截断", 3, 6, "超过保留轮数就截断，截断后前缀缓存失效一次。"),
            ("工具与提示", 6, 11, "工具描述模式和超时可以省下不少 token。"),
            ("请求与群聊", 11, len(FRAMEWORK_FIELDS), "这些每轮都在花钱：群聊窗口永远进不了缓存。"),
        )
        for title, start, stop, note in groups:
            card = self._card(body, title, note)
            for spec in FRAMEWORK_FIELDS[start:stop]:
                path = spec["key"]
                self._row(card, spec, ("fw",) + path, walk(cfg, path, spec["default"]),
                          choices=self.providers)

    def _tab_usage(self) -> None:
        body = self._page("用量", "读的是框架自己记的 provider_stats。时间按 UTC 存，这里已经换算成今天。")
        card = self._card(body)
        self.range_var = tk.StringVar(value="今天")
        chips = tk.Frame(card, bg=P["card"])
        chips.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.range_btns = {}
        for text in ("今天", "近 7 天", "全部"):
            btn = tk.Button(
                chips, text=text, command=lambda t=text: self.refresh_usage(t),
                bg=P["field"], fg=P["muted"], activebackground=P["accent_dim"],
                activeforeground="#ffffff", bd=0, relief="flat", padx=14, pady=5,
                cursor="hand2", font=(self.font, 9),
            )
            btn.pack(side="left", padx=(0, 6))
            self.range_btns[text] = btn
        wrap = tk.Frame(card, bg=P["field"], highlightbackground=P["border"],
                        highlightthickness=1, bd=0)
        wrap.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.tree = ttk.Treeview(
            wrap, columns=("model", "calls", "miss", "cached", "out", "hit", "cost"),
            show="headings", height=7,
        )
        for col, title, width in (
            ("model", "模型", 200), ("calls", "调用", 64), ("miss", "未命中", 92),
            ("cached", "命中", 92), ("out", "输出", 80), ("hit", "命中率", 70),
            ("cost", "估价", 80),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="e" if col != "model" else "w")
        self.tree.pack(fill="x", padx=1, pady=1)
        self.usage_tail = tk.StringVar(value="")
        tk.Label(card, textvariable=self.usage_tail, bg=P["card"], fg=P["muted"],
                 font=(self.font, 9), anchor="w", justify="left", wraplength=720).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        card = self._card(body, "参考单价（元 / 百万 token）",
                          "默认值按 DeepSeek 定价填，换成别的模型记得改。估价只是估算，以账单为准。")
        price = read_json(PRICES) or {}
        self.price_vars = {}
        row = tk.Frame(card, bg=P["card"])
        row.grid(row=0, column=0, columnspan=3, sticky="w")
        for key, label in (("miss", "未命中"), ("cached", "命中"), ("output", "输出")):
            tk.Label(row, text=label, bg=P["card"], fg=P["text"], font=(self.font, 9),
                     anchor="w").pack(side="left", padx=(0, 6))
            var = tk.StringVar(value=str(price.get(key, PRICE_DEFAULTS[key])))
            self._field(row, var, 10).pack(side="left", ipady=4, padx=(0, 16))
            self.price_vars[key] = var
        ttk.Button(row, text="保存单价", command=self.save_prices,
                   style="Ghost.TButton").pack(side="left")

    def _tab_plugins(self) -> None:
        body = self._page("附加功能", "每个插件的 config.json。改完需要重启机器人。")
        for plugin, fields in PLUGIN_FIELDS.items():
            title = PLUGIN_TITLES.get(plugin, plugin)
            card = self._card(body, title, f"{plugin}/config.json")
            cfg = read_json(PLUGINS / plugin / "config.json")
            for spec in fields:
                self._row(card, spec, ("plugin", plugin, spec["key"]),
                          cfg.get(spec["key"], spec["default"]))

    def _tab_mood(self) -> None:
        body = self._page("心情", "它自己报心情涨落，这里只是看状态和调阈值。机器人在跑时不能重置。")
        card = self._card(body, "此刻", "每 5 秒刷新。心情 -100 到 100，劲头和火气 0 到 100。")
        self.mood_lines = tk.StringVar(value="")
        tiles = tk.Frame(card, bg=P["card"])
        tiles.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        self.mood_stats: dict[str, dict] = {}
        for index, (key, label, low, high, colour) in enumerate((
            ("valence", "心情", -100.0, 100.0, P["accent"]),
            ("arousal", "劲头", 0.0, 100.0, "#8f6ef0"),
            ("anger", "火气", 0.0, 100.0, P["bad"]),
        )):
            tile = tk.Frame(tiles, bg=P["card_alt"], highlightbackground=P["border"],
                            highlightthickness=1)
            tile.grid(row=0, column=index, sticky="ew", padx=(0, 10) if index < 2 else 0)
            tiles.columnconfigure(index, weight=1)
            tk.Label(tile, text=label, bg=P["card_alt"], fg=P["muted"],
                     font=(self.font, 9), anchor="w").pack(fill="x", padx=12, pady=(8, 0))
            value = tk.Label(tile, text="—", bg=P["card_alt"], fg=colour,
                             font=(self.font, 20, "bold"), anchor="w")
            value.pack(fill="x", padx=12)
            bar = tk.Canvas(tile, height=5, bg=P["card_alt"], highlightthickness=0, bd=0)
            bar.pack(fill="x", padx=12, pady=(2, 10))
            tile_spec = {
                "label": value, "bar": bar, "colour": colour,
                "low": low, "high": high, "value": low, "ratio": 0.0,
            }
            # The bar has no width until the window is laid out, so redraw on
            # every resize instead of only in the five second poll.
            bar.bind("<Configure>", lambda e, t=tile_spec: self._draw_bar(t))
            self.mood_stats[key] = tile_spec
        tk.Label(card, textvariable=self.mood_lines, bg=P["card"], fg=P["muted"],
                 font=(self.font, 9), anchor="w", justify="left", wraplength=720).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 10))
        row = tk.Frame(card, bg=P["card"])
        row.grid(row=2, column=0, columnspan=3, sticky="w")
        self.mood_btns = []
        for text, action, style in (
            ("重置心情", "reset", "Danger.TButton"),
            ("只熄火", "cool", "Ghost.TButton"),
            ("清空口头禅", "forget", "Ghost.TButton"),
        ):
            b = ttk.Button(row, text=text, command=lambda a=action: self.mood_action(a),
                           style=style)
            b.pack(side="left", padx=(0, 8))
            self.mood_btns.append(b)

        card = self._card(body, "记仇账本", "只列真的记过仇的人。")
        self.grudge_box = tk.Text(
            card, height=6, bg=P["field"], fg=P["text"], bd=0, relief="flat",
            highlightthickness=1, highlightbackground=P["border"],
            highlightcolor=P["border"], font=(self.font, 9), wrap="word",
            insertbackground=P["text"],
        )
        self.grudge_box.grid(row=0, column=0, columnspan=3, sticky="ew", ipady=6)

        card = self._card(body, "阈值与记仇参数", "阈值决定进损档、脏档、冷档、嗨档的线。")
        cfg = read_json(PLUGINS / "mood" / "config.json")
        for spec in MOOD_FIELDS:
            self._row(card, spec, ("plugin", "mood", spec["key"]),
                      cfg.get(spec["key"], spec["default"]))

    def _tab_voice(self) -> None:
        body = self._page("语音", "有些话会直接发成语音，用她自己的音色。火气上来了更爱开口。这一页改完不用重启。")
        card = self._card(body, "运行时状态", "翻唱和开口都要用到这套运行时，缺一件整条链都用不了。")
        self.voice_status_label = tk.Label(
            card, text="检查中…", bg=P["card"], fg=P["muted"], font=(self.font, 10),
            anchor="w", justify="left", wraplength=720,
        )
        self.voice_status_label.grid(row=0, column=0, columnspan=3, sticky="w")
        card = self._card(body, "开口与音色")
        speak = read_json(PLUGINS / "voice" / "speak.json")
        for spec in SPEAK_FIELDS:
            self._row(card, spec, ("speak", spec["key"]),
                      speak.get(spec["key"], spec["default"]))

    def collect(self, spec: dict):
        """Read one control and validate it.

        Args:
            spec: The field description, including ``bind``.

        Returns:
            The typed value.

        Raises:
            ValueError: With a Chinese message naming the field.
        """
        var = self.vars[spec["bind"]]
        kind = spec["kind"]
        if kind == "bool":
            return bool(var.get())
        text = str(var.get()).strip()
        if kind == "int":
            try:
                value = int(float(text))
            except ValueError:
                raise ValueError(f"{spec['label']} 要填整数") from None
        elif kind == "float":
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{spec['label']} 要填数字") from None
        elif kind == "list":
            return [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
        else:
            value = text
        if spec["lo"] is not None and value < spec["lo"]:
            raise ValueError(f"{spec['label']} 不能小于 {spec['lo']}")
        if spec["hi"] is not None and value > spec["hi"]:
            raise ValueError(f"{spec['label']} 不能大于 {spec['hi']}")
        if kind == "choice" and spec["choices"] and value not in spec["choices"]:
            raise ValueError(f"{spec['label']} 只能是 {' / '.join(spec['choices'])}")
        return value

    def save_all(self) -> None:
        """Validate every control, then write only the files that changed."""
        try:
            values = {spec["bind"]: self.collect(spec) for spec in self.specs}
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        if not VOICE_ID.match(str(values[("speak", "voice")])):
            messagebox.showerror("保存失败", "底嗓要写成 Edge TTS 的 voice id，例如 zh-CN-XiaoyiNeural")
            return
        main_model = str(values[("fw", "agent_runner", "config", "model", "provider_id")])
        known = set(self.providers)
        fallback = values[("fw", "agent_runner", "config", "model", "fallback_provider_ids")]
        stray = [pid for pid in [main_model, *fallback] if pid not in known]
        if stray:
            messagebox.showerror("保存失败", f"这些模型 id 不在 provider 列表里：{'、'.join(stray)}\n"
                                            f"现有的只有：{'、'.join(sorted(known))}")
            return
        try:
            windows = parse_windows(self.window_var.get())
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        written = []

        cfg = read_json(CFG)
        if not cfg:
            messagebox.showerror("保存失败", "读不到 cmd_config.json")
            return
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        for key, value in values.items():
            if key[0] == "fw":
                put(cfg, key[1:], value)
        if values[("reply",)] != self.loaded.get(("reply",)):
            put(cfg, BASE_REPLY, values[("reply",)])
        if json.dumps(cfg, sort_keys=True, ensure_ascii=False) != before:
            write_json(CFG, cfg)
            written.append("cmd_config.json")
        if values[("reply",)] != self.loaded.get(("reply",)):
            write_json(PACE, {"base": values[("reply",)]}, backup=False)
            written.append("pace.json")

        per_plugin: dict[str, dict] = {}
        for key, value in values.items():
            if key[0] == "plugin" and value != self.loaded.get(key):
                per_plugin.setdefault(key[1], {})[key[2]] = value
        # The window list is edited as one text line, so it does not go through
        # the generic per-field comparison.
        current_windows = read_json(PLUGINS / "liang_time" / "config.json").get("windows")
        if windows != current_windows:
            per_plugin.setdefault("liang_time", {})["windows"] = windows
        for plugin, updates in per_plugin.items():
            path = PLUGINS / plugin / "config.json"
            current = read_json(path)
            current.update(updates)
            write_json(path, current, label=f"{plugin}.config")
            written.append(f"{plugin}/config.json")

        speak_now = read_json(PLUGINS / "voice" / "speak.json")
        speak_new = dict(speak_now)
        for spec in SPEAK_FIELDS:
            value = values[("speak", spec["key"])]
            if value != self.loaded.get(("speak", spec["key"])):
                speak_new[spec["key"]] = value
        if speak_new != speak_now:
            write_json(PLUGINS / "voice" / "speak.json", speak_new, label="speak")
            written.append("speak.json")

        self.reload()
        if written:
            self.status.set(f"{datetime.now():%H:%M:%S} 已存 {len(written)} 个文件："
                            + "、".join(written))
        else:
            self.status.set(f"{datetime.now():%H:%M:%S} 没有改动，什么都没写。")

    def save_prices(self) -> None:
        """Store the reference prices used by the usage estimate."""
        data = {}
        try:
            for key, var in self.price_vars.items():
                value = float(str(var.get()).strip())
                if value < 0:
                    raise ValueError
                data[key] = value
        except ValueError:
            messagebox.showerror("保存失败", "单价要填非负数字")
            return
        write_json(PRICES, data, backup=False)
        self.refresh_usage()
        self.status.set("单价已保存。")

    def refresh_usage(self, label: str | None = None) -> None:
        """Reload the usage table for the selected range.

        Args:
            label: One of 今天 / 近 7 天 / 全部. Left out to reuse the current
                selection, which is what the chips and the poll timer do.
        """
        if label:
            self.range_var.set(label)
        for name, btn in getattr(self, "range_btns", {}).items():
            active = name == self.range_var.get()
            btn.config(bg=P["accent"] if active else P["field"],
                       fg="#ffffff" if active else P["muted"])
        now = datetime.now().astimezone()
        label = self.range_var.get()
        if label == "全部":
            since = None
        elif label == "近 7 天":
            since = now - timedelta(days=7)
        else:
            # "Today" means the local day, while created_at is stored in UTC.
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = since.astimezone(timezone.utc) if since else None
        rows = usage_rows(since)
        price = read_json(PRICES) or {}
        price = {k: float(price.get(k, PRICE_DEFAULTS[k])) for k in PRICE_DEFAULTS}
        self.tree.delete(*self.tree.get_children())
        total = rows[-1] if rows and rows[-1][0] == "合计" else None
        for row in rows[:-1] if total else rows:
            hit = row[3] / (row[3] + row[2]) * 100 if (row[3] + row[2]) else 0.0
            self.tree.insert("", "end", values=(
                row[0], row[1], f"{row[2]:,}", f"{row[3]:,}", f"{row[4]:,}",
                f"{hit:.0f}%", f"¥{money(row, price):.2f}",
            ))
        if total:
            hit = total[3] / (total[3] + total[2]) * 100 if (total[3] + total[2]) else 0.0
            self.tree.insert("", "end", values=(
                "合计", total[1], f"{total[2]:,}", f"{total[3]:,}", f"{total[4]:,}",
                f"{hit:.0f}%", f"¥{money(total, price):.2f}",
            ))
            self.usage_tail.set(
                f"{label}：{total[1]} 次调用，未命中 {total[2]:,} token，命中 {total[3]:,}，"
                f"命中率 {hit:.0f}%，估价 ¥{money(total, price):.2f}。"
                f"命中率低于 50% 基本就是每轮历史都在全价重付。"
            )
        else:
            self.usage_tail.set("这个范围还没有记录。")

    def reload(self) -> None:
        """Re-read every file and push the values back into the controls."""
        cfg = read_json(CFG)
        self.loaded = {}
        for spec in self.specs:
            key = spec["bind"]
            if key[0] == "fw":
                value = walk(cfg, key[1:], spec["default"])
            elif key == ("reply",):
                value = walk(cfg, BASE_REPLY, 0.005)
            elif key[0] == "plugin":
                file = PLUGINS / key[1] / "config.json"
                value = read_json(file).get(key[2], spec["default"])
            else:
                value = read_json(PLUGINS / "voice" / "speak.json").get(key[1], spec["default"])
            self.loaded[key] = value
            var = self.vars[key]
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            elif isinstance(var, tk.StringVar):
                if spec["kind"] == "list":
                    var.set(", ".join(str(v) for v in (value or [])))
                else:
                    var.set("" if value is None else str(value))
        pace = read_json(PACE).get("base")
        self.pace_hint.set(
            "pace.json 当前记的基准："
            + (str(pace) if pace is not None else "（没有，插件会回落到 cmd_config）")
        )
        liang = read_json(PLUGINS / "liang_time" / "config.json")
        self.window_var.set(format_windows(liang.get("windows")))
        ids = (cfg.get("platform_settings") or {}).get("id_whitelist") or []
        self._notes = read_json(NOTES)
        self.wl_box.delete(0, "end")
        for i in ids:
            note = str(self._notes.get(str(i)) or "")
            self.wl_box.insert("end", f"{i}　{note}".rstrip())
        self.status.set("")

    def _wl_pick(self, _event=None) -> None:
        """Show the note of the selected group."""
        sel = self.wl_box.curselection()
        if not sel:
            return
        gid = self.wl_box.get(sel[0]).split("　")[0].strip()
        self.note_var.set(str(self._notes.get(gid) or ""))

    def wl_note(self) -> None:
        """Save the note for the selected group."""
        sel = self.wl_box.curselection()
        if not sel:
            messagebox.showinfo("提示", "先在列表里选一个群。")
            return
        gid = self.wl_box.get(sel[0]).split("　")[0].strip()
        notes = read_json(NOTES)
        notes[gid] = self.note_var.get().strip()[:60]
        write_json(NOTES, notes, backup=False)
        self.reload()
        self.status.set("备注已保存。")

    def wl_add(self) -> None:
        """Add a group id to the whitelist after checking it is numeric."""
        text = self.wl_entry.get().strip()
        if not text.isdigit() or not 4 <= len(text) <= 12:
            messagebox.showerror("添加失败", "群号应该是 4 到 12 位数字。")
            return
        cfg = read_json(CFG)
        ids = [str(i) for i in (cfg.get("platform_settings") or {}).get("id_whitelist") or []]
        if text in ids:
            return
        ids.append(text)
        self._save_whitelist(ids)
        self.wl_entry.set("")

    def wl_del(self) -> None:
        """Remove the selected group id from the whitelist."""
        sel = self.wl_box.curselection()
        if not sel:
            return
        cfg = read_json(CFG)
        ids = [str(i) for i in (cfg.get("platform_settings") or {}).get("id_whitelist") or []]
        if sel[0] < len(ids):
            ids.pop(sel[0])
        self._save_whitelist(ids)

    def _save_whitelist(self, ids: list[str]) -> None:
        """Store a new whitelist, keeping the original string style."""
        cfg = read_json(CFG)
        if not cfg:
            messagebox.showerror("保存失败", "读不到 cmd_config.json")
            return
        put(cfg, ("platform_settings", "id_whitelist"), ids)
        write_json(CFG, cfg)
        self.reload()
        self.status.set("白名单已保存。重启机器人后生效。")

    def mood_action(self, action: str) -> None:
        """Reset or cool the mood, refusing while the bot is running."""
        if bot_running():
            messagebox.showwarning("不能重置", "机器人还在跑，重置会被它在几秒内写回去。先停机器人。")
            return
        if action == "reset" and not messagebox.askyesno("确认", "把心情、劲头、火气都归零，确定？"):
            return
        data = read_json(MOOD)
        if not data:
            messagebox.showerror("失败", "没找到 mood.json")
            return
        if action == "reset":
            data.update(MOOD_BASE)
            data["tags"] = {}
            data["hits"] = []
            data["said"] = []
            data["last_pick"] = 0
        elif action == "cool":
            data["anger"] = 0.0
            data["valence"] = max(float(data.get("valence") or 0), MOOD_BASE["valence"])
            data["tags"] = {}
        elif action == "forget":
            data["said"] = []
        data["tick"] = datetime.now().isoformat()
        write_json(MOOD, data)
        self.status.set("心情已更新。")

    def effective_reply(self) -> str:
        """Describe what the reply probability is right now, mirroring liang_time.

        Returns:
            One line to show on the reply tab.
        """
        liang = read_json(PLUGINS / "liang_time" / "config.json")
        if not liang.get("enabled", True):
            return "节奏插件已关掉，任何时候都用基准值。"
        factor = 1.0
        why = []
        stored = liang.get("windows")
        windows = parse_windows(format_windows(stored)) if stored else []
        now = datetime.now().time()
        inside = False
        for start, end in windows:
            a = datetime.strptime(start, "%H:%M").time()
            b = datetime.strptime(end, "%H:%M").time()
            if (a <= now < b) if a <= b else (now >= a or now < b):
                inside = True
                break
        if inside:
            factor *= 0.5
            why.append("梁文峰时间 ×0.5")
        cfg = read_json(CFG)
        pid = walk(cfg, ("agent_runner", "config", "model", "provider_id"), None)
        source = None
        for item in cfg.get("provider") or []:
            if item.get("id") == pid:
                source = item.get("provider_source_id")
                break
        fast = str(liang.get("fast_source_id") or "")
        if fast and source == fast:
            try:
                mult = float(liang.get("fast_multiplier", 2.0) or 2.0)
            except (TypeError, ValueError):
                mult = 2.0
            factor *= mult
            why.append(f"新网关 ×{mult:g}")
        try:
            base = float(self.vars[("reply",)].get())
        except (ValueError, KeyError):
            base = 0.0
        tail = "、".join(why) if why else "没有调整"
        return f"此刻实际概率约 {round(base * factor, 4):g}（基准 {base:g}，{tail}）"

    def _draw_bar(self, tile: dict) -> None:
        """Redraw one mood bar at its current width.

        Args:
            tile: One entry of ``self.mood_stats``.
        """
        tile["bar"].delete("all")
        width = tile["bar"].winfo_width()
        if width < 4:
            return
        tile["bar"].create_rectangle(0, 0, width, 5, fill=P["field"], outline="")
        span = tile["high"] - tile["low"]
        ratio = min(1.0, max(0.0, (tile["value"] - tile["low"]) / span)) if span else 0.0
        tile["ratio"] = ratio
        if ratio > 0:
            tile["bar"].create_rectangle(
                0, 0, max(3, int(width * ratio)), 5, fill=tile["colour"], outline="")

    def _poll(self) -> None:
        """Refresh live status every few seconds without touching edits."""
        running = bot_running()
        self.running_label.config(text="机器人运行中" if running else "机器人没在跑")
        for b in self.mood_btns:
            b.config(state="disabled" if running else "normal")
        try:
            self.effective.set(self.effective_reply())
        except (ValueError, AttributeError):
            self.effective.set("")
        ok, reason = voice_status()
        if ok:
            self.voice_status_label.config(text="语音运行时齐全，翻唱和开口都能用。", fg=P["good"])
        else:
            self.voice_status_label.config(text=f"语音用不了：{reason}", fg=P["warn"])
        data = read_json(MOOD)
        if data:
            for key, tile in self.mood_stats.items():
                tile["value"] = float(data.get(key) or 0.0)
                tile["label"].config(text=f"{tile['value']:.1f}".rstrip("0").rstrip("."))
                self._draw_bar(tile)
            people = len(data.get("people") or {})
            grudges = sum(
                1 for person in (data.get("people") or {}).values()
                if any(e.get("hurt") for e in (person.get("ledger") or []))
            )
            self.mood_lines.set(
                f"记得的人 {people}　记过仇的 {grudges}　"
                f"上次存档 {str(data.get('tick') or '')[:19].replace('T', ' ')}"
            )
            lines = []
            for uid, person in (data.get("people") or {}).items():
                hurts = [e for e in (person.get("ledger") or []) if e.get("hurt")]
                if hurts:
                    last = str(hurts[-1].get("s") or "")[:30]
                    lines.append(f"{uid}　{len(hurts)} 次　最近：{last}")
            self.grudge_box.delete("1.0", "end")
            self.grudge_box.insert("1.0", "\n".join(lines[-8:]) or "还没跟谁结过梁子。")
        self.after(5000, self._poll)


def main() -> None:
    """Open the panel window, logging any crash since pythonw has no console."""
    try:
        Panel().mainloop()
    except Exception:
        ERRORLOG.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
