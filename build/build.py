"""Build the 鲸鲸bot distribution from this working installation.

The live tree carries real API keys, a logged-in QQ account, a populated
database and hours of cached songs. This script assembles a clean tree with all
of that removed, and then refuses to pack until a scan finds nothing that looks
like a secret or a personal identifier.

Target layout::

    鲸鲸bot/
      qqbot/
        AstrBot/          the bot, data/ created on first run
        napcat/           protocol side, logged out
        voice-env/        relocatable CPython + RVC, plugins expect it here
      runtime/
        python/           relocatable CPython running the bot
      启动机器人.bat  启动面板.bat  面板.py  使用说明.md

Usage:
    python build/build.py --stage
    python build/build.py --stage --pack
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT.parent
ASSETS = ROOT / "assets"
FACTORY = ASSETS / "factory"
STAGE = ROOT / "out"
DIST = ROOT / "dist"

QQBOT = SRC / "qqbot"
APP = QQBOT / "AstrBot"
VOICE_ENV = QQBOT / "voice-env"
NAPCAT = QQBOT / "napcat"

SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")

PRODUCT = "鲸鲸bot"
VERSION = "1.0"

# Directories and files that never ship: version control, caches, the builder's
# data, and model zoos the inference path does not read.
APP_SKIP = (
    ".git",
    ".github",
    "__pycache__",
    "*.pyc",
    ".venv",
    "data/temp",
    "data/attachments",
    "data/config",
    "data/site-packages",
    "data/plugins/voice/covers",
    "data/plugins/voice/songs",
    "data/plugins/voice/work",
    "data/plugins/voice/datasets",
    "data/plugins/voice/logs",
    "data/plugins/voice/data",
    "data/plugins/voice/models/base",
    "data/plugins/illust/cache",
    "data/plugins/mood/mood.json",
    "data/plugins/memorize/notes.json",
    "data/plugins/liang_time/pace.json",
    "data/data_v4.db",
    "data/data_v4.db-wal",
    "data/data_v4.db-shm",
    "data/data_v4.before-flavor.db",
    "data/cmd_config.json",
    "docs",
    "tests",
    "changelogs",
    "k8s",
    "scripts",
    "samples",
    "openspec",
    "typings",
    "dashboard/src",
)

# The default voice is a three-way blend, and blend.py has to be able to find
# every part, so all of them ship.
VOICE_FILES = (
    "blend_teto_403525_mixlty.pth",
    "kasane_teto.pth",
    "luotianyi.pth",
    "luotianyi_hf.pth",
    "hatsune_miku2.pth",
    "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2.index",
)

SEPARATOR = "bs_roformer_voc_hyperacev2"

PY_SKIP = ("__pycache__", "*.pyc", "idlelib", "turtledemo", "test", "tests")

# Patterns that must not survive into the finished tree. The personal ones live
# in ``secret_patterns.py`` beside this file, which is git-ignored: publishing
# the list would defeat the point of scanning. Copy the example to get started.
SECRETS = (
    ("a DeepSeek key", re.compile(r"sk-[A-Za-z0-9]{32}")),
    ("a gateway key", re.compile(r"sk-[A-Za-z0-9_\-]{30,}")),
)

try:
    from secret_patterns import EXTRA_SECRETS
except ImportError:
    EXTRA_SECRETS = ()

SECRETS = SECRETS + tuple(EXTRA_SECRETS)

# A scan of these extensions is worth the time; binaries are skipped.
SCANNABLE = {
    ".py",
    ".json",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".bat",
    ".ps1",
    ".js",
    ".mjs",
    ".cfg",
    ".ini",
    ".toml",
}


def log(msg: str) -> None:
    """Print one progress line.

    Args:
        msg: Text to show.
    """
    print(f"[build] {msg}", flush=True)


def pruned(rel: Path, patterns: tuple[str, ...]) -> bool:
    """Tell whether a relative path should be skipped.

    A pattern containing a slash matches the whole relative path; one without a
    slash matches any single component, so ``.git`` drops at every depth.

    Args:
        rel: Path relative to the tree being copied.
        patterns: Skip patterns.

    Returns:
        True when the entry is excluded.
    """
    text = rel.as_posix()
    for pat in patterns:
        if "/" in pat:
            if text == pat or text.startswith(pat + "/"):
                return True
        elif any(Path(part).match(pat) for part in rel.parts):
            return True
    return False


def copy_tree(src: Path, dst: Path, skip: tuple[str, ...]) -> None:
    """Copy a directory, dropping everything matching the skip list.

    Args:
        src: Source directory.
        dst: Destination directory.
        skip: Skip patterns, see :func:`pruned`.
    """
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if pruned(rel, skip):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def fresh(path: Path) -> Path:
    """Remove a directory when present and return it.

    Args:
        path: Directory to reset.

    Returns:
        The same path, existing and empty.
    """
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict:
    """Load a JSON file, tolerating a byte order mark.

    Args:
        path: File to load.

    Returns:
        The parsed object, or an empty dict when unreadable.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: object) -> None:
    """Write JSON with plain indentation and no byte order mark.

    Args:
        path: Destination file.
        data: Object to serialise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def copy_interpreter(dest: Path) -> None:
    """Lay down a relocatable CPython at ``dest``.

    The live venv cannot move: its ``pyvenv.cfg`` and every console script embed
    absolute paths. A plain CPython copy works because the interpreter derives
    its prefix from its own location, so ``Scripts/python.exe`` finds ``Lib``
    and ``DLLs`` one level up.

    Args:
        dest: Directory that becomes the interpreter root.
    """
    base = Path(sys.base_prefix)
    fresh(dest)
    copy_tree(base, dest, PY_SKIP)
    for junk in ("pyvenv.cfg", "Lib/site-packages/sitecustomize.py"):
        target = dest / junk
        if target.exists():
            target.unlink()
    scripts = dest / "Scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copy2(base / "python.exe", scripts / "python.exe")
    for dll in (
        "python3.dll",
        "python312.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    ):
        item = base / dll
        if item.exists():
            shutil.copy2(item, scripts / dll)


def stage_bot_runtime(stage: Path) -> None:
    """Copy the interpreter and packages the bot runs on.

    Args:
        stage: Staging root.
    """
    dest = stage / "runtime" / "python"
    log(f"interpreter: {Path(sys.base_prefix)} -> runtime/python")
    copy_interpreter(dest)
    site = APP / ".venv" / "Lib" / "site-packages"
    log(f"bot packages: {site}")
    copy_tree(site, dest / "Lib" / "site-packages", PY_SKIP)
    # An editable install records this machine's source tree and would break
    # every import on the target machine. Any .pth that is a bare absolute path
    # is one of those; the rest point at real package directories.
    for pth in (dest / "Lib" / "site-packages").glob("*.pth"):
        text = pth.read_text(encoding="utf-8", errors="ignore").strip()
        if re.fullmatch(r"[A-Za-z]:[\\/].*|/.*", text):
            pth.unlink()
            log(f"dropped editable install {pth.name}")
    # PEP 610 metadata from that editable install names the build machine too,
    # and nothing reads it once the package is a plain directory.
    for dist in (dest / "Lib" / "site-packages").glob("astrbot-*.dist-info"):
        item = dist / "direct_url.json"
        if item.exists():
            item.unlink()
            log(f"dropped {dist.name}/direct_url.json")


def stage_voice_runtime(stage: Path, with_voice: bool) -> None:
    """Copy the voice runtime into ``qqbot/voice-env``.

    The plugins resolve this path themselves, so it is not negotiable: the
    interpreter, ffmpeg, the RVC tree and the default voice all live there.

    Args:
        stage: Staging root.
        with_voice: Whether the voice package is being included.
    """
    if not with_voice:
        return
    dest = stage / "qqbot" / "voice-env"
    log("interpreter: -> qqbot/voice-env")
    copy_interpreter(dest)
    site = VOICE_ENV / "Lib" / "site-packages"
    log("voice packages (this is the slow part)")
    copy_tree(site, dest / "Lib" / "site-packages", PY_SKIP)
    bin_dst = dest / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)
    for item in (VOICE_ENV / "bin").iterdir():
        if item.is_file():
            shutil.copy2(item, bin_dst / item.name)
            log(f"  ffmpeg {item.name} ({item.stat().st_size / 1048576:.0f} MB)")
    rvc_src = VOICE_ENV / "src" / "rvc"
    rvc_dst = dest / "src" / "rvc"
    rvc_skip = (
        "__pycache__",
        "*.pyc",
        "logs",
        "datasets",
        "train",
        "RVCRealtimeVST",
        "docs",
        "i18n",
        "webui.py",
        "realtime_gui.py",
        "*.bat",
        "pretrained",
        "assets/weights",
        "assets/indices",
        "assets/pymss_weights",
    )
    log("rvc code and analysis models")
    copy_tree(rvc_src, rvc_dst, rvc_skip)
    weights_dst = rvc_dst / "assets" / "weights"
    weights_dst.mkdir(parents=True, exist_ok=True)
    for name in VOICE_FILES:
        item = rvc_src / "assets" / "weights" / name
        if not item.is_file():
            raise SystemExit(f"missing voice weight: {item}")
        shutil.copy2(item, weights_dst / name)
        log(f"  voice {name} ({item.stat().st_size / 1048576:.0f} MB)")
    sep_src = APP / "data" / "plugins" / "voice" / "models" / "separate"
    sep_dst = APP / "data" / "plugins" / "voice" / "models" / "separate"
    for item in sep_src.rglob(f"{SEPARATOR}*"):
        if item.is_file():
            target = stage / "qqbot" / "AstrBot" / "data" / "plugins" / "voice" / "models" / "separate" / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            log(f"  separator {item.name} ({item.stat().st_size / 1048576:.0f} MB)")
    del sep_dst


def stage_app(stage: Path) -> None:
    """Copy the bot application without the builder's data.

    Args:
        stage: Staging root.
    """
    dest = fresh(stage / "qqbot" / "AstrBot")
    log("AstrBot")
    copy_tree(APP, dest, APP_SKIP)
    (dest / "data" / "temp").mkdir(parents=True, exist_ok=True)


def stage_extras(stage: Path) -> None:
    """Lay down the launcher scripts, the installer and the panel.

    Args:
        stage: Staging root.
    """
    log("launchers and installer")
    for name in ("安装.bat", "启动机器人.bat", "启动面板.bat", "更新.bat"):
        shutil.copy2(ASSETS / "installer" / name, stage / name)
    (stage / "tools").mkdir(parents=True, exist_ok=True)
    stale = stage / "tools" / "__pycache__"
    if stale.exists():
        shutil.rmtree(stale)
    for name in ("install.py", "update.py"):
        shutil.copy2(ASSETS / "installer" / name, stage / "tools" / name)
    install_assets = stage / "tools" / "assets"
    if install_assets.exists():
        shutil.rmtree(install_assets)
    copy_tree(ASSETS / "factory", install_assets / "factory", ())
    for name in ("persona.txt", "persona_meta.json", "napcat_onebot11.json", "napcat_webui.json"):
        shutil.copy2(ASSETS / name, install_assets / name)
    shutil.copy2(SRC / "面板.py", stage / "面板.py")
    shutil.copy2(ASSETS / "使用说明.md", stage / "使用说明.md")
    shutil.copy2(ASSETS / "README.txt", stage / "README.txt")
    copy_tree(ASSETS / "LICENSES", stage / "LICENSES", ())


def stage_configs(stage: Path) -> None:
    """Put the factory plugin configs where the plugins look for them.

    The framework's own config is left out on purpose: the installer writes it,
    so the buyer's keys never sit in a file that shipped.

    Args:
        stage: Staging root.
    """
    log("plugin configs")
    for item in FACTORY.glob("*.config.json"):
        plugin = item.name.split(".")[0]
        target = stage / "qqbot" / "AstrBot" / "data" / "plugins" / plugin / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def stage_napcat(stage: Path) -> None:
    """Copy the protocol side with an empty login.

    Args:
        stage: Staging root.
    """
    dest = fresh(stage / "qqbot" / "napcat")
    log("NapCat")
    skip = ("config", "cache", "logs", "worker", "*.db", "*.db-shm", "*.db-wal")
    copy_tree(NAPCAT, dest, skip)
    cfg = dest / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    for name in ("napcat.json",):
        item = NAPCAT / "config" / name
        if item.is_file():
            shutil.copy2(item, cfg / name)
    shutil.copy2(ASSETS / "napcat_onebot11.json", cfg / "onebot11.json")
    shutil.copy2(ASSETS / "napcat_webui.json", cfg / "webui.json")


def scan(stage: Path) -> list[str]:
    """Search the staged tree for secrets and personal identifiers.

    Args:
        stage: Staging root.

    Returns:
        One readable line per hit; empty means the tree is clean.
    """
    hits: list[str] = []
    for item in stage.rglob("*"):
        if not item.is_file() or item.suffix.lower() not in SCANNABLE:
            continue
        try:
            text = item.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRETS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(
                    f"{item.relative_to(stage).as_posix()}:{line}: {label} {match.group(0)!r}"
                )
    return hits


def report(hits: list[str]) -> None:
    """Print the leak report.

    Args:
        hits: Lines returned by :func:`scan`.
    """
    print(f"\n{len(hits)} leak(s):")
    for line in hits[:60]:
        print("  " + line)


def pack(stage: Path) -> int:
    """Compress the staged tree and attach the self-extracting stub.

    Args:
        stage: Staged product directory.

    Returns:
        Process exit code.
    """
    if not SEVEN_ZIP.is_file():
        print(f"no 7-Zip at {SEVEN_ZIP}")
        return 1
    sfx = Path(r"C:\Program Files\7-Zip\7z.sfx")
    if not sfx.is_file():
        print(f"no 7-Zip SFX stub at {sfx}")
        return 1
    DIST.mkdir(parents=True, exist_ok=True)
    payload = DIST / "payload.7z"
    if payload.exists():
        payload.unlink()
    total = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
    log(f"compressing {total / 1073741824:.2f} GB (this takes a while)")
    result = subprocess.run(
        [
            str(SEVEN_ZIP),
            "a",
            "-t7z",
            "-m0=lzma2",
            "-mx=7",
            "-md=256m",
            "-ms=on",
            "-mmt=on",
            str(payload),
            f"{stage.name}\\*",
        ],
        cwd=str(stage.parent),
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", "replace")[-1500:])
        return 1
    size = payload.stat().st_size / 1073741824
    log(f"payload {size:.2f} GB")
    config = DIST / "config.txt"
    config.write_text(
        ";!@Install@!UTF-8!\n"
        f'Title="{PRODUCT} {VERSION}"\n'
        f'BeginPrompt="把 {PRODUCT} 装到当前文件夹？"\n'
        'RunProgram="安装.bat"\n'
        ";!@InstallEnd@!\n",
        encoding="utf-8",
        newline="\r\n",
    )
    exe = DIST / f"{PRODUCT}_v{VERSION}_安装包.exe"
    with exe.open("wb") as out:
        out.write(sfx.read_bytes())
        out.write(config.read_bytes())
        out.write(payload.read_bytes())
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (DIST / "SHA256SUMS.txt").write_text(
        f"{digest}  {exe.name}\n", encoding="utf-8"
    )
    log(f"{exe} ({exe.stat().st_size / 1073741824:.2f} GB)")
    log(f"sha256 {digest}")
    return pack_update(stage)


def pack_update(dest: Path) -> int:
    """Build the small archive used to refresh an existing install.

    Only code ships: the runtime, the voice models and every file that holds
    someone's data stay behind. That keeps the download at a few megabytes and
    makes the update a file swap instead of a reinstall.

    Args:
        dest: The staged product directory, used as the source of files.

    Returns:
        Process exit code.
    """
    if not dest.is_dir():
        print("stage the tree first (--stage) so the update has something to pack")
        return 1
    DIST.mkdir(parents=True, exist_ok=True)
    archive = DIST / f"更新包_v{VERSION}.zip"
    if archive.exists():
        archive.unlink()
    roots = [
        "qqbot/AstrBot/astrbot",
        "qqbot/AstrBot/main.py",
        "qqbot/AstrBot/runtime_bootstrap.py",
        "qqbot/AstrBot/pyproject.toml",
        "qqbot/AstrBot/data/plugins",
        "qqbot/AstrBot/dashboard/dist",
        "qqbot/napcat/launcher.bat",
        "tools",
        "LICENSES",
        "面板.py",
        "README.txt",
        "使用说明.md",
    ]
    skip = (
        "qqbot/AstrBot/data/plugins/voice/songs",
        "qqbot/AstrBot/data/plugins/voice/covers",
        "qqbot/AstrBot/data/plugins/voice/work",
        "qqbot/AstrBot/data/plugins/voice/models",
        "qqbot/AstrBot/data/plugins/illust/cache",
        "qqbot/AstrBot/data/plugins/mood/mood.json",
        "qqbot/AstrBot/data/plugins/memorize/notes.json",
        "qqbot/AstrBot/data/plugins/liang_time/pace.json",
        "qqbot/AstrBot/data/plugins/voice/speak.json",
        "__pycache__",
        "*.pyc",
    )
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        out.writestr(
            "manifest.json",
            json.dumps(
                {"product": PRODUCT, "version": VERSION, "built": f"{datetime.now():%Y-%m-%d %H:%M}"},
                ensure_ascii=False,
                indent=2,
            ),
        )
        for root in roots:
            base = dest / root
            if not base.exists():
                continue
            items = [base] if base.is_file() else list(base.rglob("*"))
            for item in items:
                if not item.is_file():
                    continue
                rel = item.relative_to(dest)
                if pruned(rel, skip):
                    continue
                # A plugin config is merged, not replaced, so shipping it is
                # safe; the installer-side state files above are not.
                out.write(item, rel.as_posix())
                count += 1
    log(f"update {archive.name}: {count} files, {archive.stat().st_size / 1048576:.1f} MB")
    return 0


def main() -> int:
    """Run the build.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", action="store_true", help="assemble the tree")
    parser.add_argument("--pack", action="store_true", help="also pack it")
    parser.add_argument("--keep", action="store_true", help="reuse an existing stage")
    parser.add_argument(
        "--no-voice", action="store_true", help="leave the voice runtime out"
    )
    parser.add_argument("--scan-only", action="store_true", help="just scan")
    parser.add_argument(
        "--pack-only", action="store_true", help="pack an already staged tree"
    )
    args = parser.parse_args()
    if not (args.stage or args.pack or args.scan_only or args.pack_only):
        parser.error("nothing to do; pass --stage, --pack or --scan-only")
    dest = STAGE / PRODUCT
    print(f"{PRODUCT} {VERSION}")
    if args.stage or (args.pack and not args.pack_only):
        if not args.keep:
            fresh(dest)
        stage_app(dest)
        stage_napcat(dest)
        stage_voice_runtime(dest, not args.no_voice)
        stage_bot_runtime(dest)
        stage_configs(dest)
        stage_extras(dest)
    if (args.pack or args.pack_only) and not args.scan_only:
        hits = scan(dest)
        if hits:
            report(hits)
            return 1
        return pack(dest)
    hits = scan(dest)
    if hits:
        report(hits)
        return 1
    print("scan clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
