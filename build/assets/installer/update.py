"""把新版本的代码换上去，数据和配置原样留着。

更新包里只有程序本身：机器人源码、插件、面板、说明文档、安装资源。没有运行时，
没有语音模型，也没有任何人的数据。所以换一遍只要几秒。

会被保留的东西（无论更新包里有没有）：::

    qqbot/AstrBot/data/cmd_config.json      你的接口地址和 Key
    qqbot/AstrBot/data/data_v4.db           聊天记录、人设、token 统计
    qqbot/AstrBot/data/plugins/*/config.json 各插件的设置（新键会补进来）
    qqbot/AstrBot/data/plugins/*/ *.json     心情、记忆、节奏这些运行时状态
    qqbot/AstrBot/data/plugins/voice/{songs,covers}  唱过的歌
    qqbot/napcat/config/                     登录状态

用法::

    更新.bat                    会去找根目录下的 更新包*.zip
    更新.bat 某个路径\\更新包.zip   指定文件
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "qqbot" / "AstrBot" / "data"
BACKUP = ROOT / "_更新备份"


def say(text: str = "") -> None:
    """Print one line.

    Args:
        text: Line to show.
    """
    print(text, flush=True)


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


def find_pack(argv: list[str]) -> Path | None:
    """Locate the update archive.

    Args:
        argv: Command line arguments after the script name.

    Returns:
        The archive path, or None when nothing was found.
    """
    if len(argv) > 1:
        given = Path(argv[1])
        return given if given.is_file() else None
    found = sorted(ROOT.glob("更新包*.zip")) or sorted(ROOT.glob("*更新*.zip"))
    return found[-1] if found else None


def is_user_state(rel: Path) -> bool:
    """Tell whether a path inside the update must not overwrite local state.

    Args:
        rel: Path relative to the install root.

    Returns:
        True when the file belongs to this machine, not to the release.
    """
    text = rel.as_posix()
    if text.startswith("qqbot/AstrBot/data/"):
        rest = text[len("qqbot/AstrBot/data/"):]
        if rest == "cmd_config.json" or rest.startswith("data_v4.db"):
            return True
        if rest.startswith("plugins/"):
            parts = rest.split("/")
            if len(parts) >= 3 and parts[2] == "config.json":
                return True
            if "/songs/" in rest or "/covers/" in rest or "/work/" in rest:
                return True
            if rest.endswith(("mood.json", "notes.json", "pace.json")):
                return True
        if rest.startswith(("temp/", "attachments/", "knowledge_base/")):
            return True
    if text.startswith("qqbot/napcat/config/"):
        return True
    if text.startswith("_更新备份/") or text.startswith("安装记录.json"):
        return True
    return False


def backup(paths: list[Path]) -> Path:
    """Copy the files about to be replaced into a dated folder.

    Args:
        paths: Absolute paths that will change.

    Returns:
        The backup folder.
    """
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    dest = BACKUP / stamp
    for item in paths:
        rel = item.relative_to(ROOT)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
    say(f"  改动前的文件已备份到 {dest}")
    return dest


def merge_plugin_config(target: Path, incoming: bytes) -> None:
    """Merge a shipped plugin config under the local one.

    New keys appear with the shipped default; keys the user already set keep
    their value, so an update never resets a tuning someone made by hand.

    Args:
        target: Local config.json.
        incoming: Shipped bytes.
    """
    try:
        shipped = json.loads(incoming.decode("utf-8-sig"))
    except ValueError:
        return
    if not isinstance(shipped, dict):
        return
    if not target.exists():
        write_json(target, shipped)
        return
    write_json(target, {**shipped, **read_json(target)})


def main() -> int:
    """Run the update.

    Returns:
        Process exit code.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    pack = find_pack(sys.argv)
    if pack is None:
        say("没找到更新包。")
        say("把 更新包*.zip 放到这个文件夹里，再双击一次「更新.bat」。")
        return 1
    say(f"更新包：{pack.name}")
    with zipfile.ZipFile(pack) as archive:
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except KeyError:
            say("这不是一个更新包（里面没有 manifest.json）。")
            return 1
        say(f"  版本 {manifest.get('version', '?')}　打包于 {manifest.get('built', '?')}")
        if read_json(ROOT / "安装记录.json").get("version") == manifest.get("version"):
            say("  已经是这个版本了，重装一遍也行。")
        names = [n for n in archive.namelist() if not n.endswith("/")]
        targets: list[Path] = []
        touched: list[Path] = []
        merged: list[tuple[Path, bytes]] = []
        for name in names:
            rel = Path(name)
            # A plugin config is the one file the release and the user both own:
            # new keys come from the package, set values stay as they are.
            if rel.name == "config.json" and "plugins" in rel.parts:
                merged.append((ROOT / rel, archive.read(name)))
                continue
            if is_user_state(rel):
                continue
            target = ROOT / rel
            if target.exists():
                touched.append(target)
            targets.append(target)
        if touched:
            backup(touched)
        replaced = 0
        for name in names:
            rel = Path(name)
            if is_user_state(rel) or (rel.name == "config.json" and "plugins" in rel.parts):
                continue
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            replaced += 1
        for target, payload in merged:
            merge_plugin_config(target, payload)
            replaced += 1
    record = read_json(ROOT / "安装记录.json")
    record["version"] = manifest.get("version")
    record["updated_at"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    write_json(ROOT / "安装记录.json", record)
    say(f"  换了 {replaced} 个文件，数据和配置没动。")
    say()
    say("更新完成。如果机器人正开着，关掉它再双击「启动机器人.bat」就生效了。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("更新被取消了。已换上去的文件是新的，重跑一次就好。")
        sys.exit(130)
