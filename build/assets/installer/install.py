"""鲸鲸bot 安装向导。

由 安装.bat 调起，用包里自带的 runtime\\python 运行。它只做三件事：问清楚这台
机器和这个人的情况，把答案写成配置文件，再把数据库和人设建起来。整个过程可以
重跑，重跑不会覆盖已经填好的东西（除非明确选择重新配置）。

用法::

    runtime\\python\\python.exe tools\\install.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "qqbot" / "AstrBot"
DATA = APP / "data"
PLUGINS = DATA / "plugins"
NAPCAT = ROOT / "qqbot" / "napcat"
VOICE_ENV = ROOT / "qqbot" / "voice-env"
RUNTIME = ROOT / "runtime" / "python"
ASSETS = Path(__file__).resolve().parent / "assets"
FACTORY = ASSETS / "factory"
MARKER = ROOT / "安装记录.json"

PORTS = ((6185, "机器人管理面板"), (6199, "机器人通信端口"), (6099, "NapCat 管理端"), (17890, "语音换声服务"))


def say(text: str = "") -> None:
    """Print one line, flushing so the console keeps up with the prompts.

    Args:
        text: Line to show.
    """
    print(text, flush=True)


def rule(title: str = "") -> None:
    """Print a separator, optionally with a title.

    Args:
        title: Section title.
    """
    say()
    if title:
        say("=" * 62)
        say(f"  {title}")
        say("=" * 62)
    else:
        say("-" * 62)


def ask(label: str, default: str = "", required: bool = False) -> str:
    """Ask for one value.

    Args:
        label: Prompt text.
        default: Value used when the answer is empty.
        required: Whether an empty answer is refused.

    Returns:
        The trimmed answer, or the default.
    """
    hint = f"（默认 {default}）" if default else ""
    while True:
        try:
            raw = input(f"{label}{hint}: ").strip()
        except EOFError:
            raw = ""
        if raw:
            return raw
        if default:
            return default
        if not required:
            return ""
        say("  这一项要填，再试一次。")


def ask_yes(label: str, default: bool = True) -> bool:
    """Ask a yes/no question.

    Args:
        label: Prompt text.
        default: Answer used when the user just presses Enter.

    Returns:
        The answer.
    """
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"{label} [{hint}]: ").strip().lower()
        except EOFError:
            raw = ""
        if not raw:
            return default
        if raw in ("y", "yes", "是", "1"):
            return True
        if raw in ("n", "no", "否", "0"):
            return False
        say("  回 y 或 n。")


def ask_choice(label: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """Ask the user to pick one numbered option.

    Args:
        label: Prompt text.
        options: ``(value, description)`` pairs.
        default: 1-based index used when the answer is empty.

    Returns:
        The 0-based index of the chosen option.
    """
    say(f"{label}")
    for i, (_, desc) in enumerate(options, 1):
        say(f"  {i}. {desc}")
    while True:
        try:
            raw = input(f"选一个（默认 {default}）: ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return default - 1
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        say("  输入上面的序号。")


def read_json(path: Path) -> dict:
    """Load a JSON object, tolerating a byte order mark.

    Args:
        path: File to read.

    Returns:
        The parsed object, or an empty dict.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: object) -> None:
    """Write JSON without a byte order mark.

    Args:
        path: Destination file.
        data: Object to serialise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def port_free(port: int) -> bool:
    """Tell whether a TCP port can be bound on loopback.

    Args:
        port: Port to test.

    Returns:
        True when nothing is listening.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def check_environment(ignore_ports: bool = False) -> list[str]:
    """Check the things that make the install impossible, not merely awkward.

    Args:
        ignore_ports: Skip the port check, for building and testing on a machine
            where the bot is already running.

    Returns:
        One line per blocking problem; empty means good to go.
    """
    problems: list[str] = []
    if not (RUNTIME / "python.exe").is_file():
        problems.append(f"缺少运行时：{RUNTIME / 'python.exe'}")
    if not (APP / "main.py").is_file():
        problems.append(f"缺少机器人程序：{APP / 'main.py'}")
    if not (NAPCAT / "NapCatWinBootMain.exe").is_file() and not (
        NAPCAT / "launcher.bat"
    ).is_file():
        problems.append(f"缺少 QQ 协议端：{NAPCAT}")
    free = shutil.disk_usage(ROOT).free / 1024**3
    if free < 3:
        problems.append(f"磁盘只剩 {free:.1f} GB，至少要留 3 GB")
    if ignore_ports:
        return problems
    for port, name in PORTS:
        if not port_free(port):
            problems.append(f"端口 {port}（{name}）已被占用，先关掉占用的程序")
    return problems


def check_qq() -> str:
    """Look for an installed QQ client.

    Returns:
        A line describing what was found.
    """
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        )
        path, _ = winreg.QueryValueEx(key, "UninstallString")
        exe = Path(path).parent / "QQ.exe"
        return f"找到 QQ：{exe}" if exe.is_file() else "注册表里有 QQ，但没找到 QQ.exe"
    except (ImportError, OSError):
        return "没检测到 QQ。NapCat 需要它，请自己装一个（QQ 9.x）"


def build_config(answers: dict) -> dict:
    """Turn the answers into the framework config.

    Args:
        answers: Everything the wizard collected.

    Returns:
        The config to write.
    """
    cfg = read_json(FACTORY / "cmd_config.json")
    source_id = "main-src"
    cfg["provider_sources"] = [
        {
            "id": source_id,
            "provider": "openai",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": [answers["api_key"]],
            "api_base": answers["api_base"],
            "timeout": 60,
            "proxy": "",
            "custom_headers": {},
        }
    ]
    extra: dict = {}
    if answers["thinking"] == "none":
        extra = {"reasoning_effort": "none"}
    elif answers["thinking"] == "disabled":
        extra = {"thinking": {"type": "disabled"}}
    providers = [
        {
            "id": "main",
            "model": answers["model"],
            "provider_source_id": source_id,
            "enable": True,
            "provider_type": "chat_completion",
            "type": "openai_chat_completion",
            "modalities": ["text", "image"],
            "max_tokens": 160,
            "temperature": 0.8,
            "custom_extra_body": extra,
        }
    ]
    fallback = []
    if answers["backup_model"]:
        providers.append(
            {
                "id": "backup",
                "model": answers["backup_model"],
                "provider_source_id": source_id,
                "enable": True,
                "provider_type": "chat_completion",
                "type": "openai_chat_completion",
                "modalities": ["text", "image"],
                "max_tokens": 160,
                "temperature": 0.8,
                "custom_extra_body": extra,
            }
        )
        fallback = ["backup"]
    if answers["stt_model"]:
        providers.append(
            {
                "id": "stt",
                "provider": "mimo",
                "type": "mimo_stt_api",
                "provider_type": "speech_to_text",
                "enable": True,
                "api_key": answers["api_key"],
                "api_base": answers["api_base"],
                "model": answers["stt_model"],
                "timeout": "60",
                "proxy": "",
            }
        )
        cfg["provider_stt_settings"] = {"enable": True, "provider_id": "stt"}
    cfg["provider"] = providers
    cfg["platform"] = [
        {
            "id": "qq-onebot",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": 6199,
        }
    ]
    cfg["admins_id"] = [answers["owner_qq"]]
    cfg["platform_settings"]["id_whitelist"] = answers["groups"]
    cfg["dashboard"]["port"] = 6185
    cfg["timezone"] = answers["timezone"]
    runner = cfg["agent_runner"]["config"]
    runner["model"]["provider_id"] = "main"
    runner["model"]["fallback_provider_ids"] = fallback
    # Leave the config without a password: the framework generates one on the
    # first start and prints it, which is safer than a shared default.
    cfg["dashboard"]["password"] = ""
    cfg["dashboard"]["pbkdf2_password"] = ""
    cfg["dashboard"]["jwt_secret"] = ""
    return cfg


def patch_plugins(answers: dict) -> None:
    """Write the plugin configs, filling the few keys that name a person.

    Args:
        answers: Everything the wizard collected.
    """
    pairs = {
        "mood": {
            "keepers": {answers["owner_qq"]: answers["owner_nick"]},
            "gentle_keepers": [answers["second_qq"]] if answers["second_qq"] else [],
            "owner_name": answers["owner"],
        },
        "liang_time": {"fast_source_id": "main-src"},
        "illust": {
            "llm_source_id": "main-src",
            "llm_with_image": bool(answers["stt_model"]),
        },
        "voice": {"enabled": bool(answers["voice"])},
    }
    for name, overrides in pairs.items():
        factory = read_json(FACTORY / f"{name}.config.json")
        path = PLUGINS / name / "config.json"
        current = read_json(path)
        merged = {**factory, **current, **overrides}
        write_json(path, merged)
    speak = PLUGINS / "voice" / "speak.json"
    speak_cfg = read_json(speak)
    speak_cfg["enabled"] = bool(answers["voice"])
    write_json(speak, speak_cfg)


def patch_napcat() -> None:
    """Point NapCat at the bot and clear the builder's login."""
    cfg = NAPCAT / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    onebot = read_json(ASSETS / "napcat_onebot11.json")
    onebot.setdefault("network", {}).setdefault("websocketClients", [])
    write_json(cfg / "onebot11.json", onebot)
    webui = read_json(ASSETS / "napcat_webui.json")
    webui["token"] = ""
    webui["autoLoginAccount"] = ""
    write_json(cfg / "webui.json", webui)


def persona_text(answers: dict) -> str:
    """Render the persona template with the buyer's own names.

    Args:
        answers: Everything the wizard collected.

    Returns:
        The system prompt.
    """
    template = (ASSETS / "persona.txt").read_text(encoding="utf-8")
    second = ""
    if answers["second_qq"]:
        second = (
            f"\n关于{answers['second']}：{answers['second']}就是 QQ {answers['second_qq']}。"
            f"对他说话客气一档——不爆粗、不阴阳、不拿胖笨穷开涮，该给的面子给足。"
            f"他是谁、跟你什么关系，不用主动说穿。\n"
        )
    return template.format(
        name=answers["name"],
        owner=answers["owner"],
        owner_qq=answers["owner_qq"],
        owner_nick=answers["owner_nick"],
        second_keeper=second,
    )


PERSONAS_DDL = """
CREATE TABLE IF NOT EXISTS personas (
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    id INTEGER NOT NULL,
    persona_id VARCHAR(255) NOT NULL,
    system_prompt TEXT NOT NULL,
    begin_dialogs JSON,
    tools JSON,
    skills JSON,
    custom_error_message TEXT,
    folder_id VARCHAR(36),
    sort_order INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uix_persona_id UNIQUE (persona_id)
)
"""


def install_persona(answers: dict) -> str:
    """Create the database if needed and store the persona.

    The framework builds every table on its first start, but the persona has to
    be in place before that start or the bot comes up with no character at all.
    So the framework's own schema builder runs here; if it cannot be imported
    (a stripped build, a broken dependency) the one table we need is created
    by hand, and the rest still appears when the bot starts.

    Args:
        answers: Everything the wizard collected.

    Returns:
        A one line result for the console.
    """
    os.environ["ASTRBOT_ROOT"] = str(APP)
    sys.path.insert(0, str(APP))
    db_path = DATA / "data_v4.db"
    DATA.mkdir(parents=True, exist_ok=True)
    note = ""
    try:
        import asyncio

        from astrbot.core.db.sqlite import SQLiteDatabase

        asyncio.run(SQLiteDatabase(str(db_path)).initialize())
    except Exception as exc:  # noqa: BLE001 - fall back rather than abort setup
        note = f"（框架建库没跑成，只建了人设表：{type(exc).__name__}）"
    con = sqlite3.connect(db_path)
    try:
        con.execute(PERSONAS_DDL)
        con.execute("delete from personas where persona_id = ?", (answers["persona_id"],))
        con.execute(
            "insert into personas (created_at, updated_at, persona_id, system_prompt,"
            " begin_dialogs, sort_order) values (?,?,?,?,?,?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                answers["persona_id"],
                persona_text(answers),
                json.dumps(json.loads((ASSETS / "persona_meta.json").read_text(
                    encoding="utf-8"))["begin_dialogs"], ensure_ascii=False),
                0,
            ),
        )
        con.commit()
    finally:
        con.close()
    return f"人设「{answers['persona_id']}」已写入数据库{note}"


def self_check() -> list[str]:
    """Run the checks that catch a broken install before the user meets it.

    Returns:
        One line per problem; empty means healthy.
    """
    problems: list[str] = []
    result = subprocess.run(
        [str(RUNTIME / "python.exe"), "-c", "import astrbot, sys; print(astrbot.__version__)"],
        cwd=str(APP),
        capture_output=True,
    )
    if result.returncode != 0:
        problems.append("运行时导入失败：" + result.stderr.decode("utf-8", "replace")[-400:])
    if VOICE_ENV.is_dir():
        needed = [
            VOICE_ENV / "Scripts" / "python.exe",
            VOICE_ENV / "bin" / "ffmpeg.exe",
            VOICE_ENV / "src" / "rvc" / "infer" / "cli.py",
            VOICE_ENV / "src" / "rvc" / "assets" / "hubert_base",
            VOICE_ENV / "src" / "rvc" / "assets" / "rmvpe",
        ]
        for path in needed:
            if not path.exists():
                problems.append(f"语音运行时不完整：缺 {path}")
    cfg = read_json(DATA / "cmd_config.json")
    if not cfg.get("provider"):
        problems.append("cmd_config.json 里没有模型配置")
    return problems


def main() -> int:
    """Run the wizard.

    Returns:
        Process exit code.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    say("鲸鲸bot 安装向导")
    say("跟着提示往下填就行，每一步都有默认值，直接回车就是默认。")
    say("填错的可以重跑一次安装向导，已经填好的内容会带出来。")
    rule("第 1 步 / 7　环境检查")
    problems = check_environment()
    if problems:
        for line in problems:
            say("  [X] " + line)
        say()
        say("上面这些问题要先解决再装。")
        return 1
    say("  [OK] 运行时、程序文件、磁盘空间、端口都正常")
    say("  " + check_qq())

    previous = read_json(MARKER)
    rule("第 2 步 / 7　这台电脑上的你和它")
    answers: dict = {}
    answers["owner_qq"] = ask("你自己的 QQ 号（管理员）", str(previous.get("owner_qq", "")), True)
    answers["owner"] = ask("你希望她怎么称呼你", str(previous.get("owner", "主人")), True)
    answers["owner_nick"] = ask("你在群里的昵称", str(previous.get("owner_nick", answers["owner"])), True)
    answers["name"] = ask("机器人叫什么", str(previous.get("name", "小鲸鱼")), True)
    answers["second"] = ""
    answers["second_qq"] = ""
    if ask_yes("要不要再设一个你信得过的人（对她说话客气一档）", False):
        answers["second"] = ask("这个人怎么称呼", "", True)
        answers["second_qq"] = ask(f"{answers['second']}的 QQ 号", "", True)

    rule("第 3 步 / 7　大模型服务")
    say("  支持任何 OpenAI 兼容接口：DeepSeek 官方、各家转发网关、本地 one-api 都行。")
    answers["api_base"] = ask(
        "接口地址（要以 /v1 结尾）",
        str(previous.get("api_base", "https://api.deepseek.com/v1")),
        True,
    )
    answers["api_key"] = ask("API Key", str(previous.get("api_key", "")), True)
    answers["model"] = ask("主模型名", str(previous.get("model", "deepseek-chat")), True)
    answers["backup_model"] = ask(
        "备用模型名（可留空，主模型失败时顶上）", str(previous.get("backup_model", ""))
    )
    thinking = ask_choice(
        "如果这个模型默认会思考，选一种关掉的方式（关不掉会又慢又容易被截断）",
        [
            ("none", "reasoning_effort = none（多数网关用这个）"),
            ("disabled", "thinking.type = disabled（部分网关用这个）"),
            ("off", "不用管，模型本来就快"),
        ],
        1,
    )
    answers["thinking"] = ("none", "disabled", "off")[thinking]

    rule("第 4 步 / 7　语音")
    say("  语音识别：把群里发的语音转成文字，需要接口支持 audio 输入。")
    answers["stt_model"] = ask(
        "语音识别模型名（留空就不开语音识别）", str(previous.get("stt_model", ""))
    )
    answers["voice"] = False
    if VOICE_ENV.is_dir():
        say("  语音包已经装好了：她会用自带音色翻唱，火气上来时也会开口说话。")
        answers["voice"] = ask_yes("启用语音（翻唱 + 开口）", True)
    else:
        say("  这个包里没带语音包，翻唱和开口都用不了，只发文字。")

    rule("第 5 步 / 7　群")
    say("  只在这些群里理人。别的群被 @ 也不会回。")
    groups = ask("群号，多个用逗号隔开（可以留空，之后在面板里加）")
    answers["groups"] = [g.strip() for g in groups.replace("，", ",").split(",") if g.strip()]

    rule("第 6 步 / 7　写配置")
    answers["timezone"] = "Asia/Shanghai"
    answers["persona_id"] = "大肥鱼"
    write_json(DATA / "cmd_config.json", build_config(answers))
    say("  已写 cmd_config.json")
    patch_plugins(answers)
    say("  已写各插件配置")
    patch_napcat()
    say("  已写 NapCat 配置")
    say("  " + install_persona(answers))
    write_json(MARKER, answers)

    rule("第 7 步 / 7　自检")
    problems = self_check()
    if problems:
        for line in problems:
            say("  [!] " + line)
        say()
        say("能启动，但上面几项要修一下。")
    else:
        say("  [OK] 全部通过")

    rule("装完了")
    say("接下来：")
    say("  1. 双击「启动机器人.bat」，等两个窗口都起来")
    say("  2. 第一次要扫码登录 QQ：手机 QQ → 扫一扫，扫 NapCat 窗口里的码")
    say("  3. 浏览器打开 http://127.0.0.1:6185 ，用启动日志里打印的账号密码登录")
    say("  4. 双击「启动面板.bat」，改回复概率、心情、语音这些")
    say()
    say("QQ 登录成功后建议挂一晚上再拉进群：新号新机器前几小时容易被风控。")
    say("详细说明在「使用说明.md」。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("装到一半被取消了。配置文件可能是半截的，重跑一次就好。")
        sys.exit(130)
