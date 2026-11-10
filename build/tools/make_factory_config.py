"""Derive the factory configs from the live installation.

The live ``cmd_config.json`` is the best description of what this bot needs:
every key that matters is there with a value that works. This script copies it
and then strips everything the buyer must supply themselves — API keys, account
numbers, group lists, passwords — so the installer starts from a complete shape
instead of a guess.

Run it whenever the live config gains a key worth shipping::

    python build/tools/make_factory_config.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "qqbot" / "AstrBot"
DATA = APP / "data"
PLUGINS = DATA / "plugins"
ASSETS = ROOT / "build" / "assets"
FACTORY = ASSETS / "factory"


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
    """Write JSON without a byte order mark, indented like the framework does.

    Args:
        path: Destination file.
        data: Object to serialise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def factory_cmd_config() -> dict:
    """Build the framework config with every personal value removed.

    Returns:
        The config a fresh install starts from.
    """
    cfg = read_json(DATA / "cmd_config.json")
    # Nothing here can ship: the keys belong to whoever built the package, the
    # accounts and groups belong to their server.
    cfg["provider_sources"] = []
    cfg["provider"] = []
    cfg["platform"] = []
    cfg["admins_id"] = []
    settings = cfg.setdefault("platform_settings", {})
    settings["id_whitelist"] = []
    dashboard = cfg.setdefault("dashboard", {})
    for key in ("password", "pbkdf2_password", "jwt_secret"):
        dashboard[key] = ""
    dashboard["password_change_required"] = True
    # The welcome flow fills these in; starting empty keeps the package generic.
    cfg.setdefault("persona", [])
    runner = cfg.setdefault("agent_runner", {}).setdefault("config", {})
    runner.setdefault("model", {})["provider_id"] = ""
    runner["model"]["fallback_provider_ids"] = []
    runner.setdefault("persona", {})["persona_id"] = "大肥鱼"
    runner["persona"]["safety_mode"] = False
    ltm = cfg.setdefault("provider_ltm_settings", {})
    ltm["image_caption_provider_id"] = ""
    ltm["active_reply"]["possibility_reply"] = 0.05
    cfg.setdefault("provider_stt_settings", {})["enable"] = False
    cfg["provider_stt_settings"]["provider_id"] = ""
    cfg.setdefault("provider_tts_settings", {})["enable"] = False
    cfg["provider_tts_settings"]["provider_id"] = ""
    return cfg


def factory_plugin(name: str, patch: dict | None = None) -> dict:
    """Copy one plugin config, applying the overrides a fresh install needs.

    Args:
        name: Plugin directory name.
        patch: Keys to overwrite; a value of ``None`` deletes the key.

    Returns:
        The config to ship.
    """
    data = read_json(PLUGINS / name / "config.json")
    # `note` fields are the builder's own commentary, written in their voice for
    # their own machine; they read as noise in a stranger's install.
    data.pop("note", None)
    for key, value in (patch or {}).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def main() -> int:
    """Write every factory config.

    Returns:
        Process exit code.
    """
    if FACTORY.exists():
        shutil.rmtree(FACTORY)
    FACTORY.mkdir(parents=True)
    write_json(FACTORY / "cmd_config.json", factory_cmd_config())
    # The rhythm plugin boosts replies when the gateway is fast; the id points
    # at the builder's gateway, so it starts empty and the installer fills it.
    write_json(
        FACTORY / "liang_time.config.json",
        factory_plugin("liang_time", {"fast_source_id": ""}),
    )
    # Captioning an image costs one model call, so it is opt-in.
    write_json(
        FACTORY / "illust.config.json",
        factory_plugin("illust", {"llm_source_id": "", "llm_with_image": False}),
    )
    write_json(
        FACTORY / "mood.config.json",
        factory_plugin(
            "mood",
            {
                "keepers": {},
                "gentle_keepers": [],
                "owner_name": "",
            },
        ),
    )
    write_json(FACTORY / "voice.config.json", factory_plugin("voice"))
    write_json(FACTORY / "stickers.config.json", factory_plugin("stickers"))
    write_json(FACTORY / "memorize.config.json", factory_plugin("memorize"))
    write_json(FACTORY / "websearch.config.json", factory_plugin("websearch"))
    write_json(FACTORY / "crowd_guard.config.json", factory_plugin("crowd_guard"))
    write_json(FACTORY / "mute_guard.config.json", factory_plugin("mute_guard"))
    write_json(FACTORY / "repeat_follow.config.json", factory_plugin("repeat_follow"))
    write_json(FACTORY / "quote_reply.config.json", factory_plugin("quote_reply"))
    write_json(FACTORY / "poke_reply.config.json", factory_plugin("poke_reply"))
    write_json(FACTORY / "owner_boost.config.json", factory_plugin("owner_boost"))
    write_json(FACTORY / "clause_split.config.json", factory_plugin("clause_split"))
    # Ships empty on purpose: the group id is the buyer's, filled in the panel.
    write_json(
        FACTORY / "censor_fallback.config.json",
        factory_plugin("censor_fallback", {"group_id": ""}),
    )
    print(f"wrote {len(list(FACTORY.iterdir()))} files to {FACTORY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
