"""Sing a requested song in the configured RVC voice and post it as a voice clip."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain, Record
from astrbot.api.star import Context, Star

HERE = Path(__file__).resolve().parent
# The voice runtime is installed beside the bot, not inside its venv: the TTS
# and RVC stack pulls torch and ffmpeg that the bot itself has no use for.
ENV = HERE.parents[3] / "voice-env"
PYTHON = ENV / "Scripts" / "python.exe"
FFMPEG = ENV / "bin" / "ffmpeg.exe"
RVC = ENV / "src" / "rvc"
RVC_CLI = RVC / "infer" / "cli.py"
WEIGHTS = RVC / "assets" / "weights"
HUBERT = RVC / "assets" / "hubert_base"
RMVPE = RVC / "assets" / "rmvpe"
COVER = HERE / "cover.py"
FETCH = HERE / "fetch_song.py"
# Downloads are throwaway; a finished song costs a GPU pass and is worth
# reusing, so each one gets its own entry here and the folder is capped.
COVERS = HERE / "covers"
SONGS = HERE / "songs"
# Load tunables from config.json next to this file; fall back to built-in defaults.
_CFG_PATH = HERE / "config.json"
try:
    _CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}

CACHE_LIMIT = _CFG.get("cache_limit_bytes", 2 * 1024**3)

# "唱晴天" / "唱一首晴天" / "来一首晴天" / "翻唱 晴天" / "点歌 晴天".
# The leading name is optional because people write "小鲸唱首晴天", and so is
# a bit of politeness, because they also write "帮我唱首晴天".
_SING = re.compile(
    r"^(?:(?:小鲸鱼|小鲸|鲸鱼|鲸崽|鲸)[,，]?)?"
    r"(?:(?:帮我|帮忙|给我|麻烦|请|那|就)[,，]?)?"
    r"(?:给我?们?(?:来|唱)|来|唱|翻唱|整|点歌|点)"
    r"(?:(?:一(?:首|曲|个|张|下)?|首|曲|个|张)歌?)?"
    r"\s*(?P<name>.+)$"
)
# A verb followed by one of these is the start of an ordinary sentence, not a
# request: "唱歌真好听", "唱下去了", "唱完了".
_NOT_A_SONG = re.compile(r"^(?:歌|不了|不下去|不下|不出|不来|完|过|着|得|去|了)")
_PUNCT = re.compile(r"[\s，。！？、,.!?~～…\"'“”‘’《》〈〉：:]+")
# A trailing parenthesised qualifier: "唱晴天（周杰伦）", "唱晴天(升调)".
_PAREN = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]\s*$")
# Trailing pleasantries and qualifiers that ride along with a request but are
# not part of the title: "唱晴天吧", "唱 晴天 谢谢", "唱 晴天 周杰伦版".
_TAIL = re.compile(
    r"(?:谢谢|多谢|拜托|麻烦|一下|好吧|"
    r"[吧啊呀哦呗哈嘛呢]|"
    r"(?:原唱|翻唱|版本)|"
    r".{0,3}(?:版本|版|mv|live))$"
)
# Politeness before the verb is handled by _SING itself; peeling it off the
# name as well would eat titles like 就是爱你 and 那女孩对我说.
# "唱首周杰伦的歌" asks for a singer, not a title.
_A_GENRE = re.compile(r"的歌$")
# "来张图" / "来点美图" / "整几张图" ask for a picture and belong to the
# illust plugin; without this the song parser reads the trailing 图 as a
# title and goes looking for a song by that name. Only the picture verbs
# are listed, so "来一首地图" still reaches fetch_song.
_A_PICTURE = re.compile(
    r"^(?:(?:小鲸鱼|小鲸|鲸鱼|鲸崽|鲸)[,，]?)?"
    r"(?:(?:帮我|帮忙|给我|麻烦|请|那|就)[,，]?)?"
    r"(?:来|整|发|给|上|换|要)"
    r"(?:一|两|三|几)?(?:张|点|些)?"
    r"(?:美|好|猫|狗|风|风景)?图"
)
# A request that names a whole playlist or asks for a whole genre is not a
# song request; only a specific title or id can be sung.
_TOO_VAGUE = {
    "歌",
    "歌曲",
    "首",
    "点歌",
    "唱歌",
    "唱首歌",
    "随便",
    "随机",
    "你",
    "什么",
    "啥",
    "",
}

# The model asks for a song in its own reply by writing the title in brackets:
# "[唱:晴天]". A bracketed marker is used instead of reading the sentence
# because "我给你唱首晴天" and "这首歌我不会唱" differ by one character, and only
# the model knows which one it meant. Square brackets are how nobody types in
# a group chat, so a false match against ordinary text is close to impossible.
SING_TAG = re.compile(
    r"[\[【]\s*(?:唱|点歌)\s*[:：]?\s*([^\]】]{1,40}?)\s*[\]】]"
)

# One song at a time: the GPU handles one conversion at a time anyway, and a
# queue would make everyone wait for the person in front.
BUSY = "正在唱了，等这首完了再说"

# The voice and mix settings approved by ear. The pitch shift is left to
# cover.py: it measures the stem and lifts it to where this voice sounds
# natural, because a fixed shift is far too high for a female vocal.
# The name lives in config.json so an install can be customised without editing
# the code; the literals below are what it falls back to when the key is absent
# or empty.
VOICE = str(_CFG.get("voice") or "blend_teto_403525_mixlty")
# A blend borrows someone else's retrieval index, so the cache has to watch
# that file too rather than looking for a same-named one. This one has to stay
# in sync with the pair in cover.py's VOICES, or the fingerprint stops matching
# when the index changes and every cached song looks current.
VOICE_INDEX = str(
    _CFG.get("voice_index") or "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2"
)
MIX = [
    "--soft",
    "--presence-db",
    "4",
    "--presence-hz",
    "3500",
    "--air-db",
    "2.5",
    "--low-db",
    "-1",
    "--consonant-db",
    "-19",
    "--protect",
    "0.45",
    "--vocal-db",
    "-3.0",
]

# Long enough for a five minute song plus the search and download, short
# enough that a wedged run does not lock the plugin until restart.
TIMEOUT = _CFG.get("timeout", 900)

# voice_status() runs on every reply, so its answer is kept for a few seconds:
# eight stats per turn is cheap but not free, and the answer only changes when
# the install does. The key carries everything the answer depends on, so a
# config or path change is noticed at once instead of being hidden until the
# cache expires.
CHECK_TTL = 5.0
_CHECK: dict = {"at": 0.0, "key": None, "status": (True, "")}
# The same missing file would otherwise be reported on every message; each
# distinct reason is logged once per process.
_LOGGED: set = set()


def voice_status() -> tuple[bool, str]:
    """Tell whether the voice runtime can be used right now.

    Every entry point asks this before spending a subprocess on a cover or a
    spoken line, so a broken install explains itself once instead of surfacing
    as a generic failure inside the child process.

    Returns:
        A ``(usable, reason)`` pair. ``usable`` is ``True`` with an empty reason
        when everything is in place; otherwise the reason names the first
        missing piece.
    """
    key = (
        bool(_CFG.get("enabled", True)),
        VOICE,
        VOICE_INDEX,
        str(PYTHON),
        str(FFMPEG),
        str(RVC_CLI),
        str(HUBERT),
        str(RMVPE),
    )
    now = time.monotonic()
    if _CHECK["key"] == key and now - _CHECK["at"] < CHECK_TTL:
        return _CHECK["status"]

    if not key[0]:
        status = (False, "语音功能在 config.json 里被关掉了")
    elif not PYTHON.is_file():
        status = (False, f"找不到语音运行时解释器 {PYTHON}")
    elif not FFMPEG.is_file():
        status = (False, f"找不到 ffmpeg {FFMPEG}")
    elif not RVC_CLI.is_file():
        status = (False, f"语音运行时不完整：缺 {RVC_CLI}")
    elif not HUBERT.is_dir():
        status = (False, f"语音运行时不完整：缺 {HUBERT}")
    elif not RMVPE.is_dir():
        status = (False, f"语音运行时不完整：缺 {RMVPE}")
    elif not (WEIGHTS / f"{VOICE}.pth").is_file():
        expected = WEIGHTS / f"{VOICE}.pth"
        status = (False, f"找不到音色文件 {VOICE}.pth（期望 {expected}）")
    elif not (WEIGHTS / f"{VOICE_INDEX}.index").is_file():
        expected = WEIGHTS / f"{VOICE_INDEX}.index"
        status = (False, f"找不到音色索引 {VOICE_INDEX}.index（期望 {expected}）")
    else:
        status = (True, "")
    _CHECK.update(at=now, key=key, status=status)
    return status


def requested(text: str) -> str | None:
    """Pull the song name out of a request.

    Args:
        text: Message text with mentions already stripped.

    Returns:
        The requested name or id, or ``None`` when this is not a request.
    """
    body = _PUNCT.sub("", text or "").strip()
    match = _SING.match(body)
    if not match:
        return None
    name = match.group("name").strip()
    # "别人唱的歌" style chatter reaches here as "的歌"; a verb glued to a
    # sentence continuation ("唱歌真好听", "唱不下去了") is not a request either,
    # and neither is a request for a singer's whole catalogue.
    if _NOT_A_SONG.match(name) or _A_GENRE.search(name):
        return None
    # The picture request is checked against the whole message, not the name:
    # "来点图" parses as a request for a song called 点图.
    if _A_PICTURE.match(body):
        return None
    # People decorate the title: "唱晴天（周杰伦）", "唱一下晴天",
    # "唱晴天吧", "唱 晴天 谢谢", "唱 晴天 周杰伦版". Peel those off until
    # nothing moves; the bracket goes last so "晴天（周杰伦）" loses both.
    for _ in range(6):
        peeled = _TAIL.sub("", name).strip()
        peeled = _PAREN.sub("", peeled).strip()
        if peeled == name:
            break
        name = peeled
    # "唱一首周杰伦的晴天" names the song last, but "夜的第七章" carries the 的
    # inside the title, so that split waits for fetch_song, which tries the
    # whole string first.
    # "唱首歌" and "唱一个" name nothing; anything shorter than two characters
    # is a slip of the keyboard rather than a title.
    if name in _TOO_VAGUE or len(name) < 2:
        return None
    if re.fullmatch(r"[Bb][Vv][0-9A-Za-z]{10}", name):
        # Video ids are case sensitive; only the "BV" prefix is normalised.
        return "BV" + name[2:]
    return name


def tagged(text: str) -> str | None:
    """Pull a song the model asked to sing out of its own reply.

    The model cannot reach the singing code, so it asks with a marker and this
    side acts on it. Reading the sentence instead would not work: "我给你唱首
    晴天" and "这首我真不会唱" differ by one character, and only the model knows
    which it meant.

    Args:
        text: The model's reply, marker included.

    Returns:
        The requested name or id, or ``None`` when the reply carries no marker
        or names nothing singable.
    """
    match = SING_TAG.search(text or "")
    if not match:
        return None
    name = _PUNCT.sub("", match.group(1)).strip()
    # The marker is typed by a model, not a person, so the same guards apply:
    # a genre, a pronoun, or a fragment is not a title.
    if name in _TOO_VAGUE or len(name) < 2 or _A_GENRE.search(name):
        return None
    if re.fullmatch(r"[Bb][Vv][0-9A-Za-z]{10}", name):
        return "BV" + name[2:]
    return name


def at_self(event: AstrMessageEvent) -> bool:
    """Tell whether the message really mentions the bot.

    Args:
        event: The incoming message event.

    Returns:
        ``True`` when the raw chain contains an At addressed to the bot.
    """
    self_id = str(event.get_self_id() or "")
    if not self_id:
        return False
    for comp in event.message_obj.message or []:
        if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
            return True
    return False


def sanitise(title: str) -> str:
    """Turn a song title into the stem its finished file is named after.

    Args:
        title: Song title, possibly carrying characters Windows rejects.

    Returns:
        The title with reserved characters removed, capped at 40 characters.
    """
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)[:40].strip() or "song"


class Sing(Star):
    """Answer "@bot 唱<song>" with the song in the local RVC voice."""

    def __init__(self, context: Context):
        super().__init__(context)
        self._singing = False

    @filter.on_decorating_result(priority=100)
    async def speak_instead(self, event: AstrMessageEvent):
        """Send the reply as a voice clip now and then, in her own voice.

        The probability comes from the mood the reply was written in, so it has
        to run before the text is split into bubbles: a spoken line is one clip,
        and splitting it first would produce several.

        Args:
            event: The message event carrying the model result.
        """
        from . import speak

        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        if not result.is_llm_result():
            return

        anger = speak.mood_state(HERE / ".." / "mood" / "mood.json")
        if not speak.should_speak(anger):
            return

        # A spoken line needs the same runtime a cover does; without it the
        # reply stays text and the reason is logged once.
        ok, reason = voice_status()
        if not ok:
            if reason not in _LOGGED:
                _LOGGED.add(reason)
                logger.error(f"speak: {reason}")
            return

        text = "".join(
            getattr(c, "text", "") or "" for c in result.chain if isinstance(c, Plain)
        ).strip()
        if not text or len(text) > speak.settings()["max_chars"]:
            return
        # A reply that is nothing but punctuation has no words to read aloud;
        # TTS would take it as a pause and send a clip of silence.
        if not re.search(r"[\w\u4e00-\u9fff]", text):
            return
        # Only plain replies are voiced; a reply carrying an image or an at
        # would lose it when the chain is replaced.
        if any(not isinstance(c, Plain) for c in result.chain):
            return

        wav = await speak.say(text)
        if wav is None:
            return
        # The clip only has to outlive the send, and the encoder reads it right
        # after this hook, so the folder is remembered and removed afterwards.
        event.set_extra("_spoken_dir", str(wav.parent))
        result.chain = [Record.fromFileSystem(str(wav), text=text)]

    @filter.after_message_sent()
    async def drop_spoken(self, event: AstrMessageEvent):
        """Remove the temporary folder a spoken reply was rendered into.

        Args:
            event: The message event that was just delivered.
        """
        folder = event.get_extra("_spoken_dir")
        if not folder:
            return
        await asyncio.to_thread(shutil.rmtree, folder, True)

    @filter.on_llm_response(priority=20)
    async def catch_sing_tag(self, event: AstrMessageEvent, response):
        """Read a song request out of the model's own reply and take the marker out.

        The marker has to be gone before the text is sent, which happens later
        than this hook, and the song itself starts after the reply goes out so
        the model's line is not buried under "等我一下".

        Args:
            event: The message event carrying the model result.
            response: The model's reply.
        """
        found = None
        if response.completion_text:
            found = tagged(response.completion_text)
            if found:
                response.completion_text = SING_TAG.sub("", response.completion_text).strip()
        chain = getattr(response, "result_chain", None)
        others = False
        for comp in getattr(chain, "chain", None) or []:
            if not isinstance(comp, Plain):
                others = True
                continue
            text = getattr(comp, "text", "") or ""
            found = found or tagged(text)
            comp.text = SING_TAG.sub("", text).strip()
        if not found:
            return
        # A reply that was nothing but the marker would go out as an empty
        # chain, and the respond stage returns early on those -- the song would
        # never start. Give it a beat to send instead. A chain that also holds
        # a picture is left alone, so replacing the text cannot drop the image.
        if not others and not (response.completion_text or "").strip():
            response.completion_text = "……"
        event.set_extra("_sing_name", found)

    @filter.after_message_sent(priority=-10)
    async def sing_tagged(self, event: AstrMessageEvent):
        """Start the song the model asked for, once its line is out.

        A negative priority puts this behind the cleanup hooks, so a spoken
        reply's temp folder is removed before the song spends forty seconds
        on the GPU.

        Args:
            event: The message event that was just delivered.
        """
        name = event.get_extra("_sing_name")
        if not name:
            return
        try:
            if self._singing:
                await event.send(MessageChain().message(BUSY))
                return
            self._singing = True
            try:
                # The reply has already gone out, so the event must stay
                # running here: stopping it makes the respond stage skip
                # clearing the result, and the next pass over that stage sends
                # the same chain a second time.
                await self._sing(event, name, announce=False, stop=False)
            finally:
                self._singing = False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"sing: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1100)
    async def on_message(self, event: AstrMessageEvent):
        try:
            if not at_self(event):
                return
            name = requested(event.message_str)
            if not name:
                return
            # Say what is missing instead of letting the child process fail
            # later with a generic error. The answer is plain text, so it
            # costs no model call.
            ok, reason = voice_status()
            if not ok:
                event.stop_event()
                await event.send(MessageChain().message(f"唱不了：{reason}"))
                if reason not in _LOGGED:
                    _LOGGED.add(reason)
                    logger.error(f"sing: {reason}")
                return
            if self._singing:
                # Answering "wait" is better than silence, and it goes out
                # without the model so it costs nothing.
                event.stop_event()
                await event.send(MessageChain().message(BUSY))
                return
            self._singing = True
            try:
                await self._sing(event, name)
            finally:
                self._singing = False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"sing: {e}")

    async def _sing(
        self,
        event: AstrMessageEvent,
        name: str,
        announce: bool = True,
        stop: bool = True,
    ) -> None:
        """Fetch, convert and deliver one song.

        Args:
            event: The message event that asked.
            name: Song name or BV id.
            announce: Whether to send the "wait a moment" line first. The model
                asks for its own song after it has already said its line, so
                that path stays quiet instead of talking over itself.
            stop: Whether to halt the event. A song asked for by name has to
                keep the model from answering as well, but a song the model
                asked for runs after its reply went out, where stopping skips
                the respond stage's cleanup and the chain is sent twice.
        """
        if stop:
            event.stop_event()
        if announce:
            await event.send(MessageChain().message("等我一下"))
        # A title the requester typed is also the finished song's name, so it
        # can be answered before spending a download on it. A BV id cannot:
        # the uploader's title is only known once the lookup has run.
        if re.search(r"[\u4e00-\u9fff]", name):
            known = cached(sanitise(name), VOICE, fingerprint(VOICE))
            if known:
                logger.info(f"sing: reusing {known.name}")
                await self._deliver(event, known)
                return
        found = await self._resolve(name)
        if not found:
            await event.send(MessageChain().message(f"没找着「{name}」"))
            return
        # The uploader's title is full of tags and punctuation; the name the
        # requester typed is what the group will recognise.
        stem = name if re.search(r"[\u4e00-\u9fff]", name) else found["title"]
        source = Path(found["path"])
        mp3 = None
        try:
            mp3 = await self._cover(source, stem)
        finally:
            # The download is throwaway: the cover is what gets replayed, and
            # keeping sources would grow the folder without bound.
            source.unlink(missing_ok=True)
        if not mp3:
            await event.send(MessageChain().message("唱砸了，下次再说"))
            return
        await self._deliver(event, mp3)

    async def _deliver(self, event: AstrMessageEvent, mp3: Path) -> None:
        """Send the finished song as a voice message.

        The whole song goes out when the platform takes it. Some clients cap
        voice at a minute, so a rejected send falls back to a single excerpt,
        and a rejected excerpt to the file itself.

        Args:
            event: The message event that asked.
            mp3: Finished cover.
        """
        for seconds in (0, 60):
            source = await asyncio.to_thread(voice_source, mp3, seconds)
            # The clip only has to outlive the send. Converting takes a fifth
            # of a second, so caching it on disk buys nothing and costs more
            # space than the songs it is derived from.
            scratch = source.parent
            try:
                chain = MessageChain([Record.fromFileSystem(str(source))])
                await event.send(chain)
                await self._remember(event, chain)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"sing: voice refused ({e})")
            finally:
                await asyncio.to_thread(shutil.rmtree, scratch, True)
        await event.send(MessageChain().message(f"唱好了，文件在 {mp3.name}"))

    async def _resolve(self, name: str) -> dict | None:
        """Download a song by name or id.

        Args:
            name: Title to search for, or a BV id.

        Returns:
            A dict with ``title`` and ``path``, or ``None`` when nothing matched.
        """
        proc = await asyncio.create_subprocess_exec(
            str(PYTHON),
            str(FETCH),
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(HERE),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        if proc.returncode != 0:
            logger.warning(f"sing: fetch failed ({proc.returncode}) {err.decode(errors='ignore')[-300:]}")
            return None
        try:
            return json.loads(out.decode("utf-8").strip().splitlines()[-1])
        except UnicodeDecodeError:
            # The pipe inherits the console code page on Windows; the title is
            # Chinese, so a child that ignored PYTHONIOENCODING answers in GBK.
            return json.loads(out.decode("gbk", errors="replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            logger.warning(f"sing: unreadable fetch output {out[:200]!r}")
            return None

    async def _cover(self, song: Path, title: str) -> Path | None:
        """Run the cover chain on a downloaded song.

        Args:
            song: Source audio.
            title: Song title, used for the output name.

        Returns:
            Path to the finished mp3, or ``None`` when the chain failed.
        """
        stem = sanitise(title)
        mark = fingerprint(VOICE)
        out = SONGS / f"{stem}--{VOICE}.mp3"
        known = cached(stem, VOICE, mark)
        if known:
            logger.info(f"sing: reusing {known.name}")
            return known
        proc = await asyncio.create_subprocess_exec(
            str(PYTHON),
            str(COVER),
            str(song),
            "-v",
            VOICE,
            *MIX,
            "--out",
            str(out),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(HERE),
            # Song titles are Chinese: without this the child prints in the
            # console code page and any non-GBK character aborts it.
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            log, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("sing: cover timed out")
            return None
        if proc.returncode != 0 or not out.is_file():
            logger.warning(f"sing: cover failed ({proc.returncode}) {log.decode(errors='ignore')[-600:]}")
            return None
        out.with_suffix(".json").write_text(
            json.dumps({"fingerprint": mark, "voice": VOICE, "title": title}, ensure_ascii=False),
            encoding="utf-8",
        )
        evict()
        return out

    async def _remember(self, event: AstrMessageEvent, chain: MessageChain) -> None:
        """Record the clip in history as the bot's own line.

        The pipeline only writes bot messages after an LLM reply, and this
        plugin answers without the model, so the request would otherwise sit
        unanswered in history and the next turn would reply to it again.

        Args:
            event: The message event that asked for the song.
            chain: The chain that was actually sent.
        """
        try:
            settings = self.context.get_config(umo=event.unified_msg_origin).get(
                "provider_ltm_settings", {}
            )
            if not settings.get("group_message_history_enable", False):
                return
            try:
                max_messages = max(1, int(settings.get("group_message_history_max_cnt", 700)))
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
            logger.debug(f"sing: history write skipped ({e})")


def fingerprint(voice: str) -> str:
    """Describe everything that decides what a finished song sounds like.

    The cache keys on the voice name, but a voice can be retrained under the
    same name, and the mix settings are tuned by ear and do change. Hashing
    the weights plus the settings means a song made with an older setup is
    never handed back as if it were current.

    Args:
        voice: Voice key, as passed to ``cover.py``.

    Returns:
        Short hex digest.
    """
    # Every song made before the bed was transposed with the vocal is out of
    # key, so the version of the chain itself belongs in the fingerprint. It
    # is bumped by hand whenever a change would alter the sound of a finished
    # song, or the library keeps serving tracks that predate the fix.
    parts = ["chain2", voice, VOICE_INDEX, *MIX]
    for suffix, stem in ((".pth", voice), (".index", VOICE_INDEX)):
        path = WEIGHTS / f"{stem}{suffix}"
        if path.is_file():
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def cached(stem: str, voice: str, mark: str) -> Path | None:
    """Look up a finished song and count the lookup as a use.

    Args:
        stem: Song stem, already sanitised for the filesystem.
        voice: Voice key.
        mark: Fingerprint the entry has to carry to count as current.

    Returns:
        The mp3 when a current entry exists, otherwise ``None``.
    """
    song = SONGS / f"{stem}--{voice}.mp3"
    side = song.with_suffix(".json")
    if not song.is_file():
        return None
    try:
        kept = json.loads(side.read_text(encoding="utf-8")).get("fingerprint")
    except FileNotFoundError:
        # No sidecar: nothing vouches for this file, but it is not wrong
        # either, so it is left for the rebuild to overwrite rather than
        # deleted on a half-written run.
        return None
    except (OSError, ValueError):
        forget(song)
        return None
    if kept != mark:
        # The voice or the mix changed under the same name: the old file is
        # not what this request would produce, so drop it rather than serve
        # a stale take.
        forget(song)
        return None
    # Recency is what decides eviction, so a hit has to refresh the date. The
    # clips are touched too: they are part of the same entry, and a stale one
    # would be rebuilt on the next send anyway.
    now = time.time()
    for entry in entry_files(song):
        try:
            os.utime(entry, (now, now))
        except OSError:
            pass
    return song


def entry_files(song: Path) -> list[Path]:
    """List every file that belongs to one cached song.

    A song is an mp3 plus its sidecar; they are only useful together, and they
    are evicted together. The wav handed to the platform is a temporary file
    and never lives here.

    Args:
        song: The entry's mp3.

    Returns:
        Existing files, mp3 first.
    """
    files = [song]
    side = song.with_suffix(".json")
    if side.is_file():
        files.append(side)
    return files


def forget(song: Path) -> None:
    """Delete a cached song and everything derived from it.

    Args:
        song: The mp3 to remove.
    """
    for path in entry_files(song):
        try:
            path.unlink()
        except OSError:
            pass


def evict() -> None:
    """Drop the least recently used songs until the folder fits its budget.

    The budget covers the mp3s and their sidecars. Older versions kept a wav
    per song here, which had to be counted too because it was larger than the
    mp3; that file is now a temporary one, so anything left over with a
    different suffix is an orphan.
    """
    entries: dict[Path, list[Path]] = {}
    for path in SONGS.glob("*.mp3"):
        if path.is_file():
            entries[path] = entry_files(path)
    # Orphans: a sidecar whose mp3 is gone, or a wav from a version that
    # cached the clip, can never be served again.
    for path in SONGS.iterdir():
        if path.is_file() and path.suffix != ".mp3":
            if not any(path in files for files in entries.values()):
                try:
                    path.unlink()
                except OSError:
                    pass
    total = sum(p.stat().st_size for files in entries.values() for p in files)
    if total <= CACHE_LIMIT:
        return
    for song in sorted(entries, key=lambda p: p.stat().st_mtime):
        if total <= CACHE_LIMIT:
            break
        total -= sum(p.stat().st_size for p in entries[song])
        forget(song)
        logger.info(f"sing: evicted {song.name}")


def voice_source(mp3: Path, seconds: int = 0) -> Path:
    """Prepare the audio QQ wants for a voice message.

    The silk encoder takes 24 kHz mono PCM, and the platform hands it whatever
    the ``Record`` points at, so the conversion happens here rather than
    letting the generic path re-encode a 320 kb/s stereo mp3.

    Args:
        mp3: Finished cover.
        seconds: Length of the excerpt; ``0`` keeps the whole song.

    Returns:
        Path to a 24 kHz mono wav in a throwaway directory.
    """
    import subprocess
    import tempfile

    ffmpeg = FFMPEG
    suffix = "" if not seconds else f"-clip{seconds}"
    # The clip is written to a scratch directory rather than beside the mp3:
    # encoding takes about 0.2 s, so a cached wav saves nothing and used to
    # add more bytes to the folder than the songs themselves. The caller
    # removes the directory once the message is out.
    dst = Path(tempfile.mkdtemp(prefix="voice_", dir=HERE)) / f"{mp3.stem}{suffix}.wav"
    args = [str(ffmpeg), "-y"]
    if seconds:
        # A quarter of the way in: the intro is usually accompaniment, and by
        # then the hook has started.
        total = probe_seconds(ffmpeg, mp3)
        if total <= seconds + 4:
            start = 0.0
        else:
            start = max(0.0, min(total * 0.25, total - seconds - 1))
        args += ["-ss", f"{start:.2f}", "-t", str(seconds)]
    args += ["-i", str(mp3), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(dst)]
    try:
        subprocess.run(args, check=True, capture_output=True)
    except BaseException:
        # Nothing will remove the scratch directory on this path, so do it
        # before the exception takes the only reference to it away.
        shutil.rmtree(dst.parent, True)
        raise
    return dst


def probe_seconds(ffmpeg: Path, path: Path) -> float:
    """Read the duration of an audio file from ffmpeg's banner.

    Args:
        ffmpeg: ffmpeg executable.
        path: Audio file to inspect.

    Returns:
        Duration in seconds, or ``0`` when it cannot be read.
    """
    import subprocess

    done = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        errors="ignore",
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", done.stderr)
    if not match:
        return 0.0
    hours, minutes, secs = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(secs)
