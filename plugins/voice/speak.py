"""Say a line in the bot's own voice instead of texting it.

Edge TTS gives a clean neutral reading; the resident worker in
``speak_server.py`` then repaints it with the same RVC voice the covers use, so
a spoken line and a sung line sound like the same character. The worker is
started on demand and kept alive, because loading the model costs about five
seconds and converting a sentence afterwards costs a third of one.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from astrbot.api import logger

HERE = Path(__file__).resolve().parent
ENV = HERE.parents[3] / "voice-env"
PYTHON = ENV / "Scripts" / "python.exe"
FFMPEG = ENV / "bin" / "ffmpeg.exe"
RVC = ENV / "src" / "rvc"
SERVER = HERE / "speak_server.py"
CONFIG = HERE / "speak.json"
PORT = 17890
BASE = f"http://127.0.0.1:{PORT}"

# Her singing sits around 380-415 Hz; a neutral reading lands near 250-315 Hz
# depending on the line, so the reading is lifted to land inside that band.
# Five semitones put both probe lines at 382-412 Hz and scored highest against a
# finished cover with the speaker network (0.68-0.71, versus 0.66-0.67 at seven
# semitones, where the f0 reaches 452 Hz and overshoots her range).
DEFAULTS = {
    "enabled": True,
    "calm_chance": 0.1,
    "angry_chance": 0.5,
    "angry_anger": 26.0,
    "pitch": 5,
    "voice": "zh-CN-XiaoyiNeural",
    "max_chars": 60,
}


def settings() -> dict:
    """Load the tunables, falling back to the defaults for anything missing.

    Returns:
        A dict with every key in ``DEFAULTS`` present and type-checked.
    """
    loaded = {}
    try:
        loaded = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in loaded.items() if k in DEFAULTS})
    try:
        out["calm_chance"] = min(1.0, max(0.0, float(out["calm_chance"])))
        out["angry_chance"] = min(1.0, max(0.0, float(out["angry_chance"])))
        out["angry_anger"] = max(0.0, float(out["angry_anger"]))
        out["pitch"] = int(out["pitch"])
        out["max_chars"] = int(out["max_chars"])
        out["enabled"] = bool(out["enabled"])
        out["voice"] = str(out["voice"] or DEFAULTS["voice"])
    except (TypeError, ValueError):
        return dict(DEFAULTS)
    return out


def _post(path: str, payload: dict, timeout: float = 60.0) -> dict | None:
    """Call the resident worker.

    Args:
        path: Route on the worker, for example ``/convert``.
        payload: JSON body.
        timeout: Seconds to wait for the reply.

    Returns:
        The decoded reply, or ``None`` when the worker is unreachable.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


async def _alive() -> bool:
    """Check whether the worker answers."""
    return await asyncio.to_thread(_post, "/ping", {}, 2.0) is not None


async def ensure_server() -> bool:
    """Start the resident worker if it is not already running.

    Returns:
        True when the worker answers afterwards.
    """
    if await _alive():
        return True
    # main.py imports this module lazily inside its hooks, so importing it here
    # cannot close a cycle; the check itself lives there because the singing
    # path needs exactly the same list of files.
    from . import main as voice_main

    ok, reason = voice_main.voice_status()
    if not ok:
        logger.error(f"[voice] cannot speak: {reason}")
        return False
    if not (SERVER.is_file() and PYTHON.is_file()):
        logger.error("[voice] speak worker or interpreter missing")
        return False

    logger.info("[voice] starting the speak worker")
    kwargs: dict = {
        "cwd": str(RVC),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    env = {
        **os.environ,
        "PATH": f"{ENV / 'bin'};{os.environ.get('PATH', '')}",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        subprocess.Popen([str(PYTHON), str(SERVER)], env=env, **kwargs)
    except OSError as exc:
        logger.error(f"[voice] cannot start the speak worker: {exc}")
        return False

    # Loading the model takes a few seconds; give it a moment before giving up.
    for _ in range(60):
        await asyncio.sleep(1.0)
        if await _alive():
            logger.info("[voice] speak worker is up")
            return True
    logger.error("[voice] speak worker did not come up")
    return False


def _to_24k(src: Path, dst: Path) -> None:
    """Resample to the 24 kHz mono PCM the voice encoder accepts.

    Args:
        src: Audio to convert.
        dst: Where to write the result.
    """
    subprocess.run(
        [
            str(FFMPEG), "-y", "-v", "quiet", "-i", str(src),
            "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(dst),
        ],
        check=True,
        capture_output=True,
    )


def _synthesize(text: str, work: Path, voice: str) -> Path:
    """Read the line aloud with Edge TTS and hand back 24 kHz mono PCM.

    Args:
        text: What to say.
        work: Scratch directory owned by the caller.
        voice: Edge TTS voice id.

    Returns:
        Path to the 24 kHz mono wav.
    """
    import edge_tts

    mp3 = work / "raw.mp3"
    wav = work / "base.wav"
    asyncio.run(edge_tts.Communicate(text, voice).save(str(mp3)))
    _to_24k(mp3, wav)
    return wav


async def say(text: str) -> Path | None:
    """Render a line in her voice.

    Args:
        text: The line to speak.

    Returns:
        Path to a 24 kHz mono wav ready for the voice encoder, or ``None`` when
        the line cannot be voiced.
    """
    conf = settings()
    if not conf["enabled"]:
        return None
    line = (text or "").strip()
    if not line or len(line) > conf["max_chars"]:
        return None
    if not await ensure_server():
        return None

    work = Path(tempfile.mkdtemp(prefix="speak_", dir=HERE))
    base = work / "base.wav"
    voiced = work / "voiced.wav"
    final = work / "final.wav"
    try:
        await asyncio.to_thread(_synthesize, line, work, conf["voice"])
        reply = await asyncio.to_thread(
            _post,
            "/convert",
            {"src": str(base), "out": str(voiced), "pitch": conf["pitch"]},
            120.0,
        )
        if not reply or not reply.get("ok") or not voiced.is_file():
            logger.error(f"[voice] speak worker refused: {reply}")
            shutil.rmtree(work, True)
            return None
        # The silk encoder only takes 24 kHz mono, and the worker returns
        # 40 kHz, so the clip is resampled once more here.
        await asyncio.to_thread(_to_24k, voiced, final)
        for extra in ("raw.mp3", "base.wav", "voiced.wav"):
            (work / extra).unlink(missing_ok=True)
        return final
    except Exception as exc:  # noqa: BLE001 - a missing voice must not eat the reply
        logger.error(f"[voice] cannot speak: {type(exc).__name__}: {exc}")
        shutil.rmtree(work, True)
        return None


def speak_chance(mood: dict) -> float:
    """Decide how keen she is to send a voice message instead of text.

    Anger alone was a two-value switch: 10% normally, 50% once the temper line
    was crossed, and nothing in between. The mood plugin already keeps three
    continuous numbers plus a set of discrete emotions, so the chance follows
    them instead of a single threshold: a wound-up bot speaks sooner, a flat one
    almost never does, and a sulky one keeps it short and icy.

    Args:
        mood: The state dict read from the mood plugin, with ``anger``,
            ``arousal``, ``valence`` and ``tags``.

    Returns:
        Probability of speaking this turn, clamped to 0..1.
    """
    conf = settings()
    if not mood:
        # Could not read the mood file at all. Falling through the curve would
        # treat "unknown" as "flat and tired" and quietly cut the rate to 4%.
        return conf["calm_chance"]
    try:
        anger = float(mood.get("anger") or 0.0)
        arousal = float(mood.get("arousal") or 0.0)
        valence = float(mood.get("valence") or 0.0)
    except (TypeError, ValueError):
        return conf["calm_chance"]
    chance = conf["calm_chance"]
    if anger >= conf["angry_anger"]:
        chance = conf["angry_chance"]
    # Arousal is how awake she is. Below the halfway mark she is barely in the
    # room, so the clip mostly stays text; above it the line gets shorter and
    # quicker to say out loud.
    if arousal >= 70:
        chance += 0.12
    elif arousal <= 26:
        chance *= 0.4
    # A good mood makes her want to be heard, a bad one makes her want to bite.
    if valence >= 45:
        chance += 0.05
    elif valence <= -20:
        chance += 0.08
    tags = mood.get("tags") if isinstance(mood.get("tags"), dict) else {}
    for name, weight in (("兴奋", 0.10), ("黏人", 0.06), ("恼火", 0.06)):
        entry = tags.get(name)
        if isinstance(entry, dict):
            level = float(entry.get("lvl") or 0.0)
            chance += weight * min(level, 3.0) / 3.0
    if "无聊" in tags:
        chance *= 0.5
    return min(1.0, max(0.0, chance))


def mood_state(path: Path) -> dict:
    """Read the mood state the mood plugin last saved.

    Args:
        path: Path to ``mood.json``.

    Returns:
        The state dict, or an empty one when the file cannot be read.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def should_speak(mood: dict) -> bool:
    """Roll the dice for this turn.

    Args:
        mood: Current mood state.

    Returns:
        True when this reply goes out as a voice clip.
    """
    return random.random() < speak_chance(mood)
