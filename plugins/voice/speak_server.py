"""Resident RVC worker that turns a spoken wav into the bot's own voice.

Loading the model costs about five seconds; converting a sentence afterwards
costs a third of a second. The bot therefore keeps this process alive and posts
each sentence here instead of paying the load cost per line. Run it from the
voice environment root so RVC can find its locale files::

    cd qqbot\\voice-env\\src\\rvc
    qqbot\\voice-env\\Scripts\\python.exe <path>\\speak_server.py

Protocol: POST /convert with JSON {"src": "<wav path>", "out": "<wav path>",
"pitch": <semitones>, "voice": "<stem>"} answers {"ok": true, "seconds": <float>}
or {"ok": false, "error": "..."}. POST /ping answers {"ok": true, "loaded":
<bool>}. The server is single threaded on purpose: one GPU, one conversion at a
time.
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# The RVC tree sits beside the bot rather than inside it; this file is
# <root>/qqbot/AstrBot/data/plugins/voice, so four levels up is <root>/qqbot.
RVC = Path(__file__).resolve().parents[4] / "voice-env" / "src" / "rvc"
ENV = RVC.parents[2]
WEIGHTS = RVC / "assets" / "weights"
PORT = 17890


# The name is shared with main.py through config.json, so switching voice is a
# single edit there rather than two files that can drift apart.
try:
    _CFG = json.loads(
        (Path(__file__).parent / "config.json").read_text(encoding="utf-8-sig")
    )
except (OSError, ValueError):
    _CFG = {}
if not isinstance(_CFG, dict):
    _CFG = {}
VOICE = str(_CFG.get("voice") or "blend_teto_403525_mixlty")
INDEX = str(
    _CFG.get("voice_index") or "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2"
)


def _prepare_env() -> None:
    """Put the RVC tree and ffmpeg on the path the loaders expect."""
    os.chdir(RVC)
    if str(RVC) not in sys.path:
        sys.path.insert(0, str(RVC))
    os.environ["PATH"] = str(ENV / "bin") + os.pathsep + os.environ.get("PATH", "")
    os.environ["weight_root"] = str(WEIGHTS)
    os.environ["rmvpe_root"] = str(RVC / "assets" / "rmvpe")
    os.environ["RVC_CUDA_GRAPH"] = "0"
    # Config.arg_parse() reads the command line; leave it nothing to eat.
    sys.argv = [sys.argv[0]]


class Converter:
    """One or more loaded models, reused for every request."""

    def __init__(self) -> None:
        self.models: dict = {}

    def load(self) -> None:
        import torch
        from configs.config import Config

        self.cfg = Config()
        self.get(VOICE)
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def get(self, voice: str):
        """Load a voice by name, keeping each one around for later requests."""
        from infer.vc.modules import VC

        if voice in self.models:
            return self.models[voice]
        vc = VC(self.cfg)
        vc.get_vc(f"{voice}.pth")
        self.models[voice] = vc
        return vc

    def convert(self, src: Path, out: Path, pitch: int, voice: str) -> float:
        import numpy as np
        import soundfile as sf

        vc = self.get(voice)
        index = INDEX if voice == VOICE else None
        status, result = vc.vc_single(
            0, str(src), pitch, "rmvpe", index, 0.5, 0, 1.0, 0.33
        )
        if result is None:
            raise RuntimeError(f"conversion failed: {status}")
        sr, audio = result
        audio = np.asarray(audio, dtype="float32")
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / peak
        sf.write(str(out), audio, sr, subtype="PCM_16")
        return len(audio) / float(sr)


def make_handler(conv: Converter, loaded: dict) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to the shared converter."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            # The bot only needs the reply; stderr noise hides real errors.
            return

        def _reply(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/ping":
                self._reply({"ok": True, "loaded": bool(loaded.get("ok"))})
            else:
                self._reply({"ok": False, "error": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as exc:
                self._reply({"ok": False, "error": f"bad json: {exc}"}, 400)
                return

            if self.path.rstrip("/") == "/ping":
                self._reply({"ok": True, "loaded": bool(loaded.get("ok"))})
                return
            if self.path.rstrip("/") != "/convert":
                self._reply({"ok": False, "error": "not found"}, 404)
                return

            src = Path(str(body.get("src", "")))
            out = Path(str(body.get("out", "")))
            if not src.is_file():
                self._reply({"ok": False, "error": f"missing src: {src}"}, 400)
                return
            if not str(out):
                self._reply({"ok": False, "error": "missing out"}, 400)
                return

            started = time.time()
            try:
                seconds = conv.convert(
                    src, out, int(body.get("pitch", 0)), str(body.get("voice", VOICE))
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash the loop
                self._reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._reply(
                {
                    "ok": True,
                    "seconds": round(seconds, 2),
                    "took": round(time.time() - started, 2),
                }
            )

    return Handler


def main() -> None:
    # Refuse to start when the files the loader needs are not there: the model
    # load would otherwise fail after the port is up, leaving a server that
    # answers /ping but can never convert.
    for path in (
        WEIGHTS / f"{VOICE}.pth",
        WEIGHTS / f"{INDEX}.index",
        RVC / "assets" / "hubert_base",
        RVC / "assets" / "rmvpe",
    ):
        if not path.exists():
            print(f"[voice] cannot start: missing {path}", file=sys.stderr, flush=True)
            sys.exit(2)
    _prepare_env()
    conv = Converter()
    loaded = {"ok": False}
    if "--lazy" not in sys.argv:
        started = time.time()
        conv.load()
        loaded["ok"] = True
        print(f"model loaded in {time.time() - started:.1f}s", flush=True)

    server = HTTPServer(("127.0.0.1", PORT), make_handler(conv, loaded))
    print(f"speak server on http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
