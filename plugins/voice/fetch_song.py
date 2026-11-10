"""Resolve a song request to a local audio file.

A title is looked up on the music platforms first, which carry the studio
recording; bilibili is the fallback, and the only place a plain video id can
be resolved. Both paths end in the same shape, so the plugin does not care
which one answered.
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
# A public instance of the gdstudio music API. It answers with the platform's
# own catalogue and hands back a direct audio URL, which is the whole reason
# for using it: the platforms' first-party APIs rate limit, and most of the
# good masters are paid there.
MUSIC = "https://music-api.gdstudio.xyz/api.php"
# The platform hands back plain audio hosted on its own CDN, which wants a
# browser-shaped request and no bilibili referer.
MUSIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}
OUT = Path(__file__).resolve().parent / "covers"
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_TAGS = re.compile(r"<[^>]+>")
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Anything longer than this is a loop or a compilation, not a song.
MIN_SECONDS = 45
MAX_SECONDS = 600
# Tagged versions that are not the studio recording. Kept separate from the
# bilibili list because the wording differs: a platform entry that says
# "原唱：某人" is by definition somebody else's take on it.
_COVERISH = (
    "原唱",
    "伴奏",
    "纯音乐",
    "钢琴",
    "吉他",
    "片段",
    "remix",
    "cover",
    "翻自",
    "翻唱",
    "女声版",
    "男声版",
    "童声",
    "抖音",
    "铃声",
    "ktv",
    "KTV",
    "DJ版",
    "Live版",
    "现场版",
    "弹唱",
    "哼唱",
)
# Results that merely mention the song: tutorials, scores, covers. A request
# for a song means the recording itself, so these only win when nothing else
# came back.
_DETOUR = (
    "鼓谱",
    "谱",
    "教程",
    "教学",
    "翻唱",
    "翻自",
    "伴奏",
    "纯音乐",
    "钢琴",
    "吉他",
    "尤克里里",
    "古筝",
    "笛",
    "二胡",
    "小提琴",
    "整活",
    "鬼畜",
    "混剪",
    "串烧",
    "合集",
    "reaction",
    "cover",
    "instrumental",
    "karaoke",
    "remix",
    "nightcore",
    "8d",
    "ai",
)


def open_url(url: str, tries: int = 4):
    """Open a URL through the shared session, retrying the HTML wall.

    Args:
        url: Address to fetch.
        tries: Number of attempts before giving up.

    Returns:
        The open response handle.
    """
    last = None
    for _ in range(tries):
        try:
            return _OPENER.open(urllib.request.Request(url, headers=HEADERS), timeout=60)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3)
    raise RuntimeError(f"failed: {url} ({last})")


def get_json(url: str) -> dict:
    """Read one JSON API response.

    Args:
        url: API address.

    Returns:
        The decoded body.
    """
    with open_url(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def warm() -> None:
    """Pick up the cookies the search API wants before answering."""
    with open_url("https://www.bilibili.com/") as resp:
        resp.read(1)


def clean_title(raw: str) -> str:
    """Strip the highlight markup bilibili wraps around matched words.

    Args:
        raw: Title as returned by the search API.

    Returns:
        Plain text.
    """
    return html.unescape(_TAGS.sub("", raw or "")).strip()


def safe_name(title: str, limit: int = 60) -> str:
    """Turn a title into something Windows accepts as a file name.

    Args:
        title: Plain song title.
        limit: Longest allowed stem, in characters.

    Returns:
        A file name without extension.
    """
    name = _ILLEGAL.sub("", title).strip(" .")
    return (name[:limit].rstrip(" .") or "song")


def _query_pages(name: str, pages: int) -> list[dict]:
    """Search bilibili and normalise the hits that are song length.

    Args:
        name: What to put in the keyword box.
        pages: How many result pages to read.

    Returns:
        One dict per usable hit, with an empty list when nothing came back.
    """
    hits: list[dict] = []
    for page in range(1, pages + 1):
        url = (
            "https://api.bilibili.com/x/web-interface/search/type"
            f"?search_type=video&keyword={urllib.parse.quote(name)}&page={page}"
        )
        data = get_json(url)
        for item in (data.get("data") or {}).get("result") or []:
            duration = str(item.get("duration", ""))
            seconds = 0
            for part in duration.split(":"):
                seconds = seconds * 60 + int(part or 0)
            if not MIN_SECONDS <= seconds <= MAX_SECONDS:
                continue
            title = clean_title(item.get("title", ""))
            hits.append(
                {
                    "bvid": item.get("bvid", ""),
                    "title": title,
                    "duration": seconds,
                    "play": int(item.get("play") or 0),
                }
            )
    return hits


def _best(hits: list[dict], wanted: list[str]) -> dict | None:
    """Pick the most played hit whose title carries every wanted word.

    Args:
        hits: Normalised search hits.
        wanted: Words that all have to appear in the title.

    Returns:
        The winning hit, or ``None`` when nothing carries the name.
    """
    if not hits:
        return None
    matching = [
        h
        for h in hits
        if wanted and all(w.lower() in h["title"].replace(" ", "").lower() for w in wanted)
    ]
    if not matching:
        # Nothing carries the name: the search engine gave up and answered
        # with something unrelated. Better to report a miss than to sing a
        # different song than the one asked for.
        return None
    direct = [h for h in matching if not any(word in h["title"].lower() for word in _DETOUR)]
    pool = direct or matching
    return max(pool, key=lambda h: h["play"])


def _retries(name: str) -> list[tuple[str, list[str]]]:
    """List the ways a request may be retried when the literal text finds nothing.

    "周杰伦的晴天" is a singer plus a title and has to be split, but "夜的第
    七章" and "我的滑板鞋" keep the 的 inside the title. Nothing in the string
    says which one this is, so the literal query always goes first and these
    only run once it has failed.

    Args:
        name: Song request as typed.

    Returns:
        ``(query, wanted)`` pairs to try after the literal one, in order.
    """
    out: list[tuple[str, list[str]]] = []
    if "的" not in name:
        return out
    head, tail = name.rsplit("的", 1)
    if len(tail) >= 2 and head:
        # "周杰伦的晴天" -> search 晴天, which the title certainly carries.
        out.append((tail, [tail]))
    parts = [p for p in name.split("的") if p]
    if len(parts) > 1:
        # "周杰伦的晴天" -> "周杰伦 晴天", which is how the uploader writes it.
        out.append((" ".join(parts), parts))
    return out


def search(name: str, pages: int = 2) -> dict | None:
    """Find the video that best matches a song name.

    Ranking is by play count among the results whose title actually contains
    what was asked for, because the top hit for a bare name is often a cover
    or a片段 rather than the song itself.

    Args:
        name: Song name as the user typed it.
        pages: How many result pages to consider.

    Returns:
        A dict with ``bvid``, ``title`` and ``duration``, or ``None``.
    """
    warm()
    # Every word has to appear, in any order: people type "周杰伦 晴天" as
    # often as "晴天 周杰伦", and the uploader's title is the other way round
    # half the time.
    wanted = [w for w in name.replace("　", " ").split() if w]
    found = _best(_query_pages(name, pages), wanted)
    if found:
        return found
    for query, words in _retries(name):
        if query == name:
            continue
        found = _best(_query_pages(query, pages), words)
        if found:
            return found
    return None


def fetch(bvid: str, name: str) -> Path:
    """Download the best audio stream of one video.

    Args:
        bvid: Video id.
        name: File stem to save under.

    Returns:
        Path to the saved m4a.
    """
    warm()
    view = get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    cid = view["data"]["pages"][0]["cid"]
    play = get_json(
        f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&fnval=16&fourk=1"
    )
    dash = (play.get("data") or {}).get("dash") or {}
    streams = sorted(dash.get("audio") or [], key=lambda s: s.get("bandwidth", 0))
    if not streams:
        raise RuntimeError(f"no audio stream for {bvid}")
    out = OUT / f"{safe_name(name)}.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open_url(streams[-1]["baseUrl"]) as resp, open(out, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    return out


def _music_get(**params) -> dict | list:
    """Call the music platform API, retrying the throttling it answers with.

    The instance refuses roughly every other request with a 503 rather than
    queueing, so a few quick retries are the difference between a lookup that
    works and one that reports a song as missing.

    Args:
        **params: Query parameters for the API.

    Returns:
        The decoded body, which is a list for searches and an object otherwise.
    """
    url = f"{MUSIC}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=MUSIC_HEADERS), timeout=15
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            # A refusal comes back in about a second, so even six rounds with
            # the backoff below stay inside the three minutes the caller waits.
            time.sleep(1.0 + attempt * 0.7)
    raise RuntimeError(f"music api failed: {last}")


def _music_search(name: str, count: int = 10) -> list[dict]:
    """Read the platform's search hits into a common shape.

    Args:
        name: Search text.
        count: How many hits to ask for.

    Returns:
        One dict per hit, empty when the platform answered with nothing.
    """
    got = _music_get(types="search", source="netease", name=name, count=count, pages=1)
    if not isinstance(got, list):
        return []
    out = []
    for item in got:
        out.append(
            {
                "id": str(item.get("id", "")),
                "title": clean_title(str(item.get("name", ""))),
                "artists": [clean_title(str(a)) for a in item.get("artist") or []],
                "album": str(item.get("album") or ""),
            }
        )
    return out


def _music_url(song: dict) -> tuple[str, float]:
    """Ask for a direct audio URL and the length it implies.

    The API reports a byte size and a bitrate rather than a duration, and the
    size is what an earlier request already had to allocate for, so it is the
    cheap way to tell a full song from a 30 second片段 before downloading.

    Args:
        song: One hit from :func:`_music_search`.

    Returns:
        ``(url, seconds)``, both empty when the track cannot be played.
    """
    got = _music_get(types="url", source="netease", id=song["id"], br=320)
    if not isinstance(got, dict):
        return "", 0.0
    url = str(got.get("url") or "")
    if not url:
        return "", 0.0
    size = float(got.get("size") or 0)
    br = float(got.get("br") or 0)
    return url, (size * 8 / br / 1000 if br else 0.0)


def _platform_pick(name: str) -> list[dict]:
    """Rank platform hits that carry the requested words and are the recording.

    A hit passes when every word of the request appears somewhere in its title
    or artist, so "周杰伦 晴天" still matches a track named just 晴天 when the
    singer field says 周杰伦. Tagged takes are dropped: on a platform an entry
    that says "原唱：某人" is by definition somebody else's version.

    Args:
        name: What the user asked for.

    Returns:
        The acceptable hits, best first, as the platform ordered them.
    """
    wanted = [w for w in name.replace("　", " ").split() if w]
    out = []
    for item in _music_search(name):
        text = f"{item['title']} {' '.join(item['artists'])}".replace(" ", "").lower()
        if wanted and not all(w.lower() in text for w in wanted):
            continue
        # Compared lowercase on both sides: platform titles write "Live版" as
        # often as "live版", and a plain substring test would miss one of them.
        if any(word.lower() in item["title"].lower() for word in _COVERISH):
            continue
        out.append(item)
    return out


def _platform(query: str) -> dict | None:
    """Find and download a song from the music platform.

    Args:
        query: Song request as typed.

    Returns:
        A dict with ``title``, ``path`` and ``source``, or ``None`` when the
        platform has nothing acceptable.
    """
    tried = [query] + [q for q, _ in _retries(query) if q != query]
    for text in tried:
        hits = _platform_pick(text)
        for item in hits[:3]:
            url, seconds = _music_url(item)
            if not url or not MIN_SECONDS <= seconds <= MAX_SECONDS:
                continue
            out = OUT / f"{safe_name(item['title'])}.mp3"
            out.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers=MUSIC_HEADERS)
            with urllib.request.urlopen(req, timeout=180) as resp, open(out, "wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
            return {"title": item["title"], "path": str(out), "source": "netease"}
    return None


def resolve(query: str) -> dict:
    """Turn a name or an id into a downloaded file.

    A title goes to the music platforms first: they carry the studio master at
    a bitrate a re-upload does not, and the separated vocal keeps the top
    octave that a re-encoded video has already lost. bilibili is the fallback,
    and the only target for an id, which names a video rather than a song.

    Args:
        query: Song name, or a ``BV`` id.

    Returns:
        A dict with ``title``, ``path`` and ``source``, plus ``bvid`` when the
        file came from bilibili.

    Raises:
        RuntimeError: When neither source has the song.
    """
    query = query.strip()
    if re.fullmatch(r"[Bb][Vv][0-9A-Za-z]{10}", query):
        view = get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={query}")
        title = clean_title(view["data"]["title"])
        path = fetch(query, title)
        return {"bvid": query, "title": title, "path": str(path), "source": "bilibili"}
    try:
        found = _platform(query)
    except Exception as exc:  # noqa: BLE001
        # A platform that is down must not cost the request its bilibili try.
        print(f"platform unavailable: {exc}", file=sys.stderr)
        found = None
    if found:
        return found
    hit = search(query)
    if not hit:
        raise RuntimeError(f"no song for {query}")
    path = fetch(hit["bvid"], hit["title"])
    return {
        "bvid": hit["bvid"],
        "title": hit["title"],
        "path": str(path),
        "source": "bilibili",
    }


def main() -> None:
    """Print the resolution result as one JSON line."""
    if len(sys.argv) < 2:
        raise SystemExit("usage: fetch_song.py <song name or BV id>")
    # The caller reads this pipe as UTF-8. The console default is the local
    # code page, and a title with a character outside it would abort the run.
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(resolve(sys.argv[1]), ensure_ascii=False))


if __name__ == "__main__":
    main()
