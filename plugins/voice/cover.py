"""Turn a song into a cover sung by one of the local RVC voices.

The chain is: separate vocals from the song, convert the vocals into the
target voice with RVC, then mix the converted vocals back over the
instrumental.

Run it with the dedicated voice environment, not the bot's own venv::

    qqbot\\voice-env\\Scripts\\python.exe cover.py <song> -v <voice>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import declick

HERE = Path(__file__).resolve().parent
ENV = HERE.parents[3] / "voice-env"
PYTHON = ENV / "Scripts" / "python.exe"
RVC = ENV / "src" / "rvc"
FFMPEG = ENV / "bin" / "ffmpeg.exe"
WEIGHTS = RVC / "assets" / "weights"
# Separation is a quality plateau here. Two different bs-roformer checkpoints
# (ep_317 and hyperacev2) measured within 0.1 dB of each other on bleed into the
# vocal band, energy kept, and residual singer in the bed, so there is nothing to
# gain by swapping. hyperacev2 is half the size (289 MB against 639 MB) and runs
# at RTF 0.04 against 0.06, which is the reason it is the one used.
SEPARATOR = "bs_roformer_voc_hyperacev2.ckpt"
SEPARATOR_DIR = HERE / "models" / "separate"
# Chunk length for the conversion pass; 8 GB of VRAM only holds about a
# minute of audio through HuBERT at once.
CHUNK_S = 30.0
SEARCH_S = 3.0
# Music sits far louder than speech, so aim at a typical streaming level
# rather than the -14 LUFS that podcast pipelines default to.
TARGET_LUFS = -10.5

# Model key -> (checkpoint stem, index stem). A blend must borrow the index of
# whichever speaker dominates it: index_rate replaces most of each frame's
# features with nearest neighbours from that index, so pairing the voice with
# the wrong speaker's features makes the timbre flicker frame to frame, which
# is heard as an electrical ring. blend_g65 is 65% ganyu, hence ganyu.index.
VOICES = {
    "ganyu": ("ganyu", "ganyu"),
    "furina": ("furina", "furina"),
    "sangduonie": ("sangduonie", "sangduonie"),
    "woyanisha": ("woyanisha", "woyanisha"),
    "feiyu": ("FeiyuVoice-RVC-v1", "FeiyuVoice-RVC-v1"),
    "paimeng": ("paimeng", "paimeng"),
    "blend_g65": ("blend_g65", "ganyu"),
    # The three trained voices and the one blend in use. Each trained voice
    # carries an index built from its own corpus; the blend borrows Luotianyi's
    # because Teto, its largest share, is still under half.
    "luotianyi": ("luotianyi", "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2"),
    "hatsune_miku2": ("hatsune_miku2", "hatsune_miku2_added_IVF4149_Flat_nprobe_1_hatsune_miku2_v2"),
    "kasane_teto": ("kasane_teto", "kasane_teto_added_IVF3261_Flat_nprobe_1_kasane_teto_v2"),
    "blend_teto_403525": (
        "blend_teto_403525",
        "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2",
    ),
    # Community models downloaded in November. The 48k entries cannot be
    # averaged with the 40k ones: their upsample strides differ, so the tensors
    # have incompatible shapes. They are alternatives, not ingredients.
    "luotianyi_hf": ("luotianyi_hf", "luotianyi_hf"),
    "hatsune_miku_99U": ("hatsune_miku_99U", "hatsune_miku_99U"),
    "blend_teto_403525_mixlty": (
        "blend_teto_403525_mixlty",
        "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2",
    ),
    "blend_teto_403525_hflty": (
        "blend_teto_403525_hflty",
        "luotianyi_added_IVF1541_Flat_nprobe_1_luotianyi_v2",
    ),
}


def run(cmd: list[str], cwd: Path | None = None) -> None:
    """Run a command and abort the whole chain if it fails.

    Args:
        cmd: Argument vector, resolved through PATH.
        cwd: Working directory for the child process.
    """
    print("$", " ".join(cmd), flush=True)
    env = os.environ.copy()
    env["PATH"] = str(ENV / "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(RVC)
    env["PYMSS_MODEL_DIR"] = str(SEPARATOR_DIR)
    # RVC captures a CUDA graph per input shape and keeps only eight of them.
    # A song chunked into nine parts overflows that cache, and from then on
    # every call pays a full graph capture plus eviction of the last entry:
    # a four minute song took eleven minutes instead of twenty seconds.
    env["RVC_CUDA_GRAPH"] = "0"
    done = subprocess.run(cmd, cwd=str(cwd or RVC), env=env)
    if done.returncode != 0:
        sys.exit(f"step failed with exit code {done.returncode}")


def pick(folder: Path, part: str) -> Path:
    """Return the single separation stem whose name contains the given part.

    Matching ignores case and accepts partial words, because separators disagree
    on naming: one writes ``_Vocals``/``_Instrumental``, another ``_vocals``/
    ``_instrument``.

    Args:
        folder: Directory holding the separated stems.
        part: Word to look for, e.g. ``vocal`` or ``instrument``.

    Returns:
        The matching file path.
    """
    hits = [
        p
        for p in folder.iterdir()
        if part.lower() in p.stem.lower() and not p.stem.endswith("_dc")
    ]
    if not hits:
        sys.exit(f"no {part} stem produced in {folder}")
    return hits[0]


def clean_bed(stem: Path) -> Path:
    """Bridge the sample-level splices the source file carries.

    The uploaded track has a few moments where adjacent samples jump by close
    to full scale. No drum transient does that at 44.1 kHz, and once the
    converted vocal sits on top they read as crackle. Separation passes them
    through untouched, so they have to be repaired before mixing.

    The repair is written back over the stem rather than beside it: a copy
    costs another 56 MB of writes and reads for a file that only ever gets
    read once, and the stems are scratch space that is deleted after the run.

    Args:
        stem: Instrumental stem as produced by the separator.

    Returns:
        The same path, repaired in place.
    """
    found, spans = declick.declick_file(stem, stem)
    print(f"  declick: {found} splices found, {spans} bridged", flush=True)
    return stem


def separate(song: Path, out: Path) -> None:
    """Split a song into a vocal stem and an instrumental stem.

    Args:
        song: Source audio file.
        out: Directory receiving the stems.
    """
    run(
        [
            str(PYTHON),
            "-m",
            "tools.pymss.cli",
            "infer",
            SEPARATOR,
            "-i",
            str(song),
            "-o",
            str(out),
            "--device",
            "cuda",
            # The separator writes float32 by default, which doubles every stem
            # for no audible gain: 16 bit already carries more dynamic range
            # than the source has. Two 4 minute stems cost 112 MB each that way
            # against 56 MB here, and they are the bulk of a run's disk writes.
            "--wav-bit-depth",
            "PCM_16",
        ]
    )


# Conversion knobs, overridable from the command line. RVC's defaults are
# tuned for short speech clips: a high index_rate makes singing stiff, a
# rms_mix_rate of 1.0 flattens the singer's dynamics, and a low protect
# turns breathy consonants into metal.
#
# index_rate 0.50 measured a 0.86% -> 0.70% consonant loss against 0.75, small
# enough to be inaudible, while moving the timbre noticeably further from the
# retrieval corpus and toward the model's own voice.
TUNING = {"index_rate": 0.50, "rms_mix_rate": 1.0, "protect": 0.33}


def convert(vocals: str | Path, voice: str, pitch: int, out: str | Path) -> None:
    """Re-sing a vocal stem in the target voice.

    Args:
        vocals: Isolated vocal stem, or a folder of chunks to convert in one
            pass. A folder loads the model once instead of once per chunk.
        voice: Key from ``VOICES``.
        pitch: Semitone shift applied before conversion.
        out: Destination wav file, or folder when ``vocals`` is a folder.
    """
    pth, index = VOICES[voice]
    run(
        [
            str(PYTHON),
            "infer/cli.py",
            "--model",
            str(WEIGHTS / f"{pth}.pth"),
            "--index",
            str(WEIGHTS / f"{index}.index"),
            "--input",
            str(vocals),
            "--output",
            str(out),
            "--f0-method",
            "rmvpe",
            "--pitch",
            str(pitch),
            "--index-rate",
            str(TUNING["index_rate"]),
            "--rms-mix-rate",
            str(TUNING["rms_mix_rate"]),
            "--protect",
            str(TUNING["protect"]),
            "--overwrite",
        ]
    )


def convert_long(vocals: Path, voice: str, pitch: int, out: Path, work: Path) -> None:
    """Convert a full song by cutting it into chunks the GPU can hold.

    RVC feeds the whole file through HuBERT in one pass, and the attention
    matrix grows with the square of the length, so anything past a minute or
    so runs out of memory on an 8 GB card. Each chunk is cut at a quiet point
    so the joins fall in breaths instead of mid-note.

    The chunks go through the CLI as one folder rather than one process each:
    the model load dominates otherwise, and a four minute song needs nine
    chunks.

    Args:
        vocals: Isolated vocal stem.
        voice: Key from ``VOICES``.
        pitch: Semitone shift applied before conversion.
        out: Destination wav file.
        work: Scratch directory for the chunks.
    """
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(str(vocals), always_2d=True)
    mono = data.mean(axis=1)
    total = len(mono)
    if total <= int(CHUNK_S * sr):
        convert(vocals, voice, pitch, out)
        return

    # Walk forward in CHUNK_S steps, then pull each cut back to the quietest
    # frame within reach so the seam lands in a gap between phrases.
    cuts = [0]
    while total - cuts[-1] > int(CHUNK_S * sr):
        target = cuts[-1] + int(CHUNK_S * sr)
        lo = max(cuts[-1] + sr, target - int(SEARCH_S * sr))
        hi = min(total - sr, target + int(SEARCH_S * sr))
        window = np.abs(mono[lo:hi])
        step = 512
        quiet = np.argmin(
            np.array([window[i : i + step].mean() for i in range(0, len(window) - step, step)])
        )
        cuts.append(lo + quiet * step)
    cuts.append(total)
    # A sliver at the end is too short for RVC to find any frame to work on,
    # so fold it into the previous chunk.
    if len(cuts) > 2 and cuts[-1] - cuts[-2] < int(3 * sr):
        cuts.pop(-2)

    # ffmpeg's concat demuxer is shaky with non-ASCII paths in its listing
    # file, so the chunks live in a fixed ASCII scratch folder.
    chunks = HERE / "work" / "_chunks"
    for part in (chunks / "in", chunks / "out"):
        part.mkdir(parents=True, exist_ok=True)
        for old in part.glob("*.wav"):
            old.unlink()
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        part = chunks / "in" / f"chunk_{i:03d}.wav"
        sf.write(str(part), data[a:b], sr)
        print(f"  chunk {i + 1}/{len(cuts) - 1}  {(b - a) / sr:.1f}s", flush=True)

    convert(chunks / "in", voice, pitch, chunks / "out")

    made = sorted((chunks / "out").glob("*.wav"))
    if len(made) != len(cuts) - 1:
        sys.exit(f"expected {len(cuts) - 1} converted chunks, found {len(made)}")
    listing = chunks / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in made), encoding="utf-8")
    run(
        [
            str(FFMPEG),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(out),
        ]
    )


def shelf(x, sr: int, freq: float, gain_db: float, kind: str = "high"):
    """Tilt one end of a signal with a shelving filter.

    Args:
        x: Mono signal.
        sr: Sample rate.
        freq: Shelf corner frequency in Hz.
        gain_db: Boost in dB.
        kind: "high" lifts everything above freq, "low" lifts below it.

    Returns:
        The filtered signal.
    """
    import numpy as np
    from scipy.signal import sosfilt

    amp = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / sr
    cos_w = np.cos(w0)
    alpha = np.sin(w0) / 2 * np.sqrt(2)
    root = 2 * np.sqrt(amp)
    if kind == "low":
        b = np.array(
            [
                amp * ((amp + 1) - (amp - 1) * cos_w + root * alpha),
                2 * amp * ((amp - 1) - (amp + 1) * cos_w),
                amp * ((amp + 1) - (amp - 1) * cos_w - root * alpha),
            ]
        )
        d = np.array(
            [
                (amp + 1) + (amp - 1) * cos_w + root * alpha,
                -2 * ((amp - 1) + (amp + 1) * cos_w),
                (amp + 1) + (amp - 1) * cos_w - root * alpha,
            ]
        )
    else:
        b = np.array(
            [
                amp * ((amp + 1) + (amp - 1) * cos_w + root * alpha),
                -2 * amp * ((amp - 1) + (amp + 1) * cos_w),
                amp * ((amp + 1) + (amp - 1) * cos_w - root * alpha),
            ]
        )
        d = np.array(
            [
                (amp + 1) - (amp - 1) * cos_w + root * alpha,
                2 * ((amp - 1) - (amp + 1) * cos_w),
                (amp + 1) - (amp - 1) * cos_w - root * alpha,
            ]
        )
    b = b / d[0]
    d = d / d[0]
    return sosfilt(np.array([[b[0], b[1], b[2], d[0], d[1], d[2]]]), x)


def bell(x, sr: int, freq: float, gain_db: float, q: float = 1.0):
    """Lift a band around freq with a peaking filter.

    RVC regenerates the waveform from a mel spectrogram, which costs the
    vocal a lot of its 2-5 kHz consonant energy; that band is what makes
    words read as crisp rather than hoarse.

    Args:
        x: Mono signal.
        sr: Sample rate.
        freq: Centre frequency in Hz.
        gain_db: Boost in dB.
        q: Bandwidth; higher is narrower.

    Returns:
        The filtered signal.
    """
    import numpy as np
    from scipy.signal import sosfilt

    amp = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 + alpha * amp, -2 * np.cos(w0), 1 - alpha * amp])
    d = np.array([1 + alpha / amp, -2 * np.cos(w0), 1 - alpha / amp])
    b = b / d[0]
    d = d / d[0]
    return sosfilt(np.array([[b[0], b[1], b[2], d[0], d[1], d[2]]]), x)


def unvoiced_gate(x: np.ndarray, sr: int, width: int = 2, smooth: int = 5) -> np.ndarray:
    """Build a sample-rate gain that is 1 on unvoiced frames and 0 on voiced ones.

    Used to confine the consonant layer to the frames it is meant to repair.
    Sibilants live where the model has no pitch to work from, so those frames
    are the only place the source's high band is contributing something the
    conversion lost rather than something it invented.

    Args:
        x: Signal to analyse, normally the converted vocal.
        sr: Sample rate.
        width: Frames of dilation either side of an unvoiced run, because a
            sibilant straddles the voicing boundary.
        smooth: Frames of averaging, so the gate opens without a click.

    Returns:
        Gain array the same length as ``x``.
    """
    from scipy.ndimage import binary_dilation, uniform_filter1d

    import librosa
    import numpy as np

    _, voiced, _ = librosa.pyin(
        x, fmin=80, fmax=1100, sr=sr, frame_length=2048, hop_length=512
    )
    unvoiced = (~np.nan_to_num(voiced, nan=0.0).astype(bool)).astype(np.float64)
    if width:
        unvoiced = binary_dilation(unvoiced, iterations=width).astype(np.float64)
    unvoiced = uniform_filter1d(unvoiced, size=smooth)
    frames = np.repeat(unvoiced, 512)
    if len(frames) < len(x):
        frames = np.pad(frames, (0, len(x) - len(frames)), mode="edge")
    return frames[: len(x)]


def mix(
    vocals: Path,
    instrumental: Path,
    out: Path,
    vocal_db: float,
    air_db: float,
    target_lufs: float = TARGET_LUFS,
    low_db: float = 0.0,
    presence_db: float = 0.0,
    presence_hz: float = 3000.0,
    presence_q: float = 1.0,
    vocal_ratio: float = 3.0,
    bus_ratio: float = 2.0,
    consonant_db: float = 0.0,
    consonant_hz: float = 4500.0,
    source_vocal: Path | None = None,
) -> None:
    """Lay the converted vocals over the original instrumental.

    Separation leaves both stems peaking above full scale. Adding them and
    then squashing the sum with a limiter pumps the whole track, which is
    heard as crackle, so instead each stem is trimmed to a known peak, the
    vocal is placed at a fixed level against the bed, and the finished sum is
    scaled once as a whole. A uniform gain change is inaudible; a limiter is
    not.

    Args:
        vocals: Converted vocal track.
        instrumental: Instrumental stem from the song.
        out: Destination file; mp3 when the suffix says so.
        vocal_db: Vocal level against the instrumental bed, in dB.
        air_db: High shelf boost on the vocal, in dB.
        target_lufs: Loudness target for the finished track.
        low_db: Low shelf boost on the vocal at 200 Hz, in dB.
        presence_db: Peaking boost on the vocal, in dB.
        presence_hz: Centre of the presence lift, in Hz.
        presence_q: Width of the presence lift; lower is wider and softer.
        vocal_ratio: Compression ratio on the vocal bus.
        bus_ratio: Compression ratio on the full mix.
        consonant_db: Level of the source consonants mixed back in, in dB.
        consonant_hz: High-pass corner for that consonant layer.

        source_vocal: Separate vocal stem the conversion came from.
    """
    import numpy as np
    import soundfile as sf
    import soxr

    bed, sr = sf.read(str(instrumental), always_2d=True, dtype="float32")
    # RVC output keeps the raw dynamics of the source take, which leaves the
    # vocal much peakier than a mixed record. A gentle compressor on the
    # vocal bus brings the average up without touching the bed.
    squash = HERE / "work" / "_voc_comp.wav"
    run(
        [
            str(FFMPEG),
            "-y",
            "-i",
            str(vocals),
            "-af",
            f"acompressor=threshold=-18dB:ratio={vocal_ratio}:attack=8:release=150",
            str(squash),
        ]
    )
    voc, vsr = sf.read(str(squash), dtype="float32")
    if vsr != sr:
        voc = soxr.resample(voc, vsr, sr)
    if voc.ndim == 2:
        voc = voc.mean(axis=1)
    count = min(len(bed), len(voc))
    bed = bed[:count]
    voc = voc[:count]

    # RVC rebuilds the waveform from a mel spectrogram, and unvoiced
    # consonants are the first thing that envelope loses. Sibilants are
    # noise, so the source's top octaves can be layered back without
    # pulling the timbre back toward the original singer.
    #
    # The layer has to be gated to unvoiced frames, though. On a breathy
    # source the same top octaves are mostly breath, and breath sits under
    # every vowel rather than on the consonants: measured on a raspy
    # source, adding the layer everywhere cost 2.9 dB of harmonic-to-noise,
    # which is the whole spread between the best and worst finished tracks.
    # Gating keeps about 70% of the consonant energy and gives back most of
    # that 2.9 dB.
    if consonant_db and source_vocal is not None:
        from scipy.signal import butter, sosfilt

        src, ssr = sf.read(str(source_vocal), dtype="float32")
        if src.ndim == 2:
            src = src.mean(axis=1)
        if ssr != sr:
            src = soxr.resample(src, ssr, sr)
        count = min(len(src), len(voc))
        src = src[:count]
        voc = voc[:count]
        bed = bed[:count]
        sos = butter(4, consonant_hz / (sr / 2), btype="high", output="sos")
        src = sosfilt(sos, src)
        src *= (
            10 ** (consonant_db / 20)
            * float(np.sqrt(np.mean(voc**2)))
            / max(float(np.sqrt(np.mean(src**2))), 1e-9)
        )
        voc = voc + src * unvoiced_gate(voc, sr)

    if low_db:
        voc = shelf(voc, sr, 200.0, low_db, kind="low")
    if presence_db:
        voc = bell(voc, sr, presence_hz, presence_db, q=presence_q)
    if air_db:
        voc = shelf(voc, sr, 8000.0, air_db)

    bed *= 0.89 / max(float(np.max(np.abs(bed))), 1e-9)
    target = float(np.sqrt(np.mean(bed**2))) * 10 ** (vocal_db / 20)
    voc *= target / max(float(np.sqrt(np.mean(voc**2))), 1e-9)

    summed = bed + np.stack([voc, voc], axis=1)
    peak = float(np.max(np.abs(summed)))
    if peak > 0.95:
        summed *= 0.95 / peak
        peak = 0.95

    scratch = HERE / "work" / "_mix.wav"
    sf.write(str(scratch), summed, sr, subtype="PCM_16")

    # A record is mastered, a raw sum is not: without a little bus
    # compression the average level sits several dB under the source track,
    # and pushing the gain instead just clips the transients.
    master = HERE / "work" / "_mix_master.wav"
    run(
        [
            str(FFMPEG),
            "-y",
            "-i",
            str(scratch),
            "-af",
            f"acompressor=threshold=-16dB:ratio={bus_ratio}:attack=20:release=250",
            str(master),
        ]
    )

    # Bring the track to a normal listening level. The gain has to be a
    # single static number, otherwise quiet passages get pumped up and the
    # whole thing sounds squashed.
    gain_db = 0.0
    probe = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-i",
            str(master),
            "-af",
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    try:
        blob = probe.stderr[probe.stderr.rindex("{") : probe.stderr.rindex("}") + 1]
        measured = json.loads(blob)
        gain_db = target_lufs - float(measured["input_i"])
        # Leave a decibel of headroom so the encoder's overshoot does not
        # turn into inter-sample clipping, which is audible as crackle.
        tops = float(np.max(np.abs(sf.read(str(master), dtype="float32")[0])))
        gain_db = min(gain_db, 20 * math.log10(0.89 / max(tops, 1e-9)))
    except (ValueError, KeyError):
        print("  loudness probe failed, leaving the level alone", flush=True)

    run(
        [
            str(FFMPEG),
            "-y",
            "-i",
            str(master),
            "-af",
            f"volume={gain_db:.2f}dB",
            "-c:a",
            "libmp3lame" if out.suffix.lower() == ".mp3" else "pcm_s16le",
            "-b:a",
            "320k",
            str(out),
        ]
    )


def median_f0(path: Path) -> float:
    """Find the median pitch of a vocal stem.

    A fixed semitone shift cannot suit every song: a female vocal already sits
    where the model is comfortable, while a male one has to come up an octave.
    Shifting both by the same amount is what made the covers sound shrill, so
    the shift is derived from the stem instead.

    Args:
        path: Vocal stem.

    Returns:
        Median fundamental in Hz, or ``0.0`` when nothing usable was found.
    """
    import librosa
    import numpy as np

    y, sr = librosa.load(str(path), sr=16000, mono=True)
    f0 = librosa.yin(y, fmin=60, fmax=1000, sr=sr, frame_length=1024, hop_length=256)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=256)[0]
    n = min(len(f0), len(rms))
    f0, rms = f0[:n], rms[:n]
    # Quiet frames are breaths and room noise; their pitch estimate is noise
    # too, and the median would drift towards it.
    loud = rms > np.percentile(rms, 60)
    good = f0[(f0 > 60) & (f0 < 1000) & loud & np.isfinite(f0)]
    return float(np.median(good)) if len(good) else 0.0


def auto_pitch(path: Path, target: float) -> int:
    """Pick the semitone shift that brings a stem to the model's sweet spot.

    Args:
        path: Vocal stem.
        target: Median fundamental the voice sounds natural at, in Hz.

    Returns:
        Shift in semitones, clamped to the range the converter accepts.
    """
    found = median_f0(path)
    if not found:
        return 0
    shift = round(12 * math.log2(target / found))
    return max(-12, min(12, shift))


def shift_key(path: Path, semitones: int) -> Path:
    """Move an accompaniment into the key the vocal is being sung in.

    The converter transposes the vocal and leaves the bed where it was, so a
    cover sung a couple of semitones up was accompanied in the original key.
    Measured on a deliberately large shift, the vocal landed on the bed's
    strong pitch classes 0.34 of the time left alone against 0.92 when the bed
    moved with it; on real covers the same test reads 0.21-0.51 today, which is
    the "same song, wrong key" sound.

    rubberband is a phase-vocoder shift, so the pitch moves while the tempo and
    the length stay. asetrate with a matching atempo hits the same frequency
    but drags every formant with it, which makes cymbals and strings sound as
    though they were played down a paper tube.

    A phase vocoder is not time-invariant: the shifted bed comes back a few
    milliseconds out of place, and the further out the larger the shift.
    Measured against the original with clicks every 250 ms, +3 st leads by
    10 ms, +5 by 17 ms and +7 by 26 ms. Leading, not trailing: the resampler
    pulls the first onset early rather than delaying it. Either way it smears
    the drums against the vocal, so the offset is measured and cancelled,
    trimming the head when the bed runs late and padding it when it runs early.

    Args:
        path: Instrumental stem.
        semitones: Interval already applied to the vocal.

    Returns:
        Path to the shifted bed, or ``path`` itself when nothing moves.
    """
    if not semitones:
        return path
    out = path.with_name(f"{path.stem}_key{semitones:+d}{path.suffix}")
    run(
        [
            str(FFMPEG),
            "-y",
            "-i",
            str(path),
            "-af",
            f"rubberband=pitch={2 ** (semitones / 12):.6f}",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    lag = key_lag(path, out)
    if lag:
        aligned = out.with_name(f"{out.stem}_aligned.wav")
        # atrim drops it when it leads; adelay pads it when it trails. Both
        # leave the sound otherwise untouched, and either is a sample-exact
        # move, which is why the offset is measured rather than assumed.
        move = (
            f"atrim=start_sample={lag}"
            if lag > 0
            else f"adelay=delays={-lag}S:all=1"
        )
        run(
            [
                str(FFMPEG),
                "-y",
                "-i",
                str(out),
                "-af",
                f"{move},asetpts=N/SR/TB",
                "-c:a",
                "pcm_s16le",
                str(aligned),
            ]
        )
        out.unlink(missing_ok=True)
        aligned.rename(out)
        print(
            f"  bed transposed {semitones:+d} st, {abs(lag) / key_rate(out):.1f} ms of drift removed",
            flush=True,
        )
    else:
        print(f"  bed transposed {semitones:+d} st", flush=True)
    return out


def key_rate(path: Path) -> int:
    """Return a file's sample rate without decoding it.

    Args:
        path: Audio file.

    Returns:
        Sample rate in Hz, or 1 when the header cannot be read.
    """
    import soundfile as sf

    try:
        return int(sf.info(str(path)).samplerate) or 1
    except (OSError, RuntimeError):
        return 1


def key_lag(original: Path, shifted: Path) -> int:
    """Measure how many samples a transposed bed trails the original.

    Cross-correlates the first minute, which is long enough for the result to be
    stable and short enough that the search stays cheap: a 4 GB musical bed put
    through a full-length correlation takes seconds nobody wants to wait for.

    Args:
        original: Bed before transposition.
        shifted: Bed after transposition.

    Returns:
        Lag in samples: positive when the shifted bed trails the original.
    """
    import numpy as np
    import soundfile as sf

    a, sr = sf.read(str(original), dtype="float32", always_2d=True)
    b, _ = sf.read(str(shifted), dtype="float32", always_2d=True)
    a, b = a.mean(axis=1), b.mean(axis=1)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if n < sr:
        return 0
    # A decimated envelope correlates just as well and is far cheaper; at
    # 5.5 kHz the resolution is 0.18 ms, well below what a few samples matter.
    hop = 8
    a = a[: n // hop * hop].reshape(-1, hop).mean(axis=1)
    b = b[: n // hop * hop].reshape(-1, hop).mean(axis=1)
    limit = min(len(a) // 4, 250)
    size = 1 << (2 * len(a) - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    window = np.concatenate([corr[-limit:], corr[: limit + 1]])
    return int((np.argmax(window) - limit) * hop)


def stabilise_pitch(f0, n: int = 5):
    """Remove the octave slips that turn a comb filter into a ring.

    The notches sit between harmonics, so their spacing is the fundamental. A
    frame that reads an octave high halves that spacing for a few milliseconds,
    and a filter whose notches jump like that is heard as a metallic buzz even
    though no noise was added. yin has no voicing decision, so breathy frames
    report a multiple of the real pitch: at 45.96 s the track jumps 385 -> 1536
    Hz, which is exactly where this was audible.

    Two passes: fold each frame onto the octave of a wide local median, then
    median-filter the log pitch so the remaining single-frame outliers go.

    Args:
        f0: Pitch per frame, in Hz; non-positive or non-finite means unvoiced.
        n: Median width, in frames.

    Returns:
        A copy with octave errors folded and smoothed.
    """
    import numpy as np
    from scipy.ndimage import median_filter

    out = np.asarray(f0, dtype=np.float64).copy()
    voiced = np.isfinite(out) & (out > 0)
    if voiced.sum() < 3:
        return out
    work = np.where(voiced, out, np.nan)
    # A wide median is the reference register: the sung melody moves slowly
    # compared to a single frame, so anything far off it is an octave error.
    ref = median_filter(np.where(voiced, work, 0.0), size=41, mode="nearest")
    ref = np.where(ref > 0, ref, np.nanmedian(work))
    ratio = np.where(voiced, work / ref, 1.0)
    for step in (4.0, 2.0, 0.5, 0.25):
        near = np.abs(np.log2(ratio / step)) < 0.35
        work = np.where(near, work / step, work)
        ratio = np.where(near, ratio / step, ratio)
    filled = np.where(voiced, work, 0.0)
    smooth = median_filter(filled, size=n, mode="nearest")
    out = np.where(voiced, smooth, 1.0)
    return np.where(out > 0, out, 1.0)


def dehusk(
    x,
    sr: int,
    depth_db: float = -14.0,
    lo: float = 500.0,
    hi: float = 8000.0,
    width: float = 0.32,
    n_fft: int = 2048,
    hop: int = 512,
):
    """Soften the noise sitting between harmonics.

    A husky tone is not a timbre that EQ can reach: the cords never close all
    the way, so every cycle leaks a puff of aperiodic energy that lands in the
    gaps between harmonics. Attenuating those gaps changes the ratio without
    touching the harmonic peaks, which is why this does not dull the voice the
    way a shelf would. Frames are tracked with yin rather than pyin: pyin's
    voicing decision buys another 3 dB at four times the cost, and the cheap
    track was chosen by ear.

    Args:
        x: Mono audio.
        sr: Sample rate.
        depth_db: Attenuation applied to the deepest point of each gap.
        lo: Lower edge of the band this touches, in Hz.
        hi: Upper edge of the band, in Hz.
        width: Gap width as a fraction of the fundamental; larger is broader.
        n_fft: STFT size.
        hop: STFT hop.

    Returns:
        Filtered audio at the same length.
    """
    import librosa
    import numpy as np

    spec = librosa.stft(x, n_fft=n_fft, hop_length=hop)
    mag, phase = np.abs(spec), np.angle(spec)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    f0 = librosa.yin(
        x, fmin=80, fmax=1600, sr=sr, frame_length=n_fft, hop_length=hop, trough_threshold=0.1
    )
    frames = min(mag.shape[1], len(f0))
    # The track has to be steady, or the moving notches are audible as ring.
    pitch = stabilise_pitch(f0[:frames])
    band = (freqs >= lo) & (freqs < hi)
    band_freqs = freqs[band]
    atten = 10 ** (depth_db / 20)
    # In blocks: the distance grid is bins by frames, which for a whole song
    # would be hundreds of megabytes.
    for start in range(0, frames, 1024):
        stop = min(start + 1024, frames)
        cur = pitch[start:stop]
        nearest = np.rint(band_freqs[:, None] / cur[None, :]) * cur[None, :]
        dist = np.abs(band_freqs[:, None] - nearest) / cur[None, :]
        near = np.exp(-((dist / width) ** 2))
        mag[band, start:stop] *= atten + (1.0 - atten) * near
    out = librosa.istft(mag * np.exp(1j * phase), hop_length=hop, length=len(x))
    return out.astype(np.float32)


def dehusk_file(src: Path, dst: Path) -> Path:
    """Apply :func:`dehusk` to a file.

    Args:
        src: Input audio.
        dst: Destination wav.

    Returns:
        ``dst``.
    """
    import soundfile as sf

    x, sr = sf.read(str(src), dtype="float32")
    if x.ndim == 2:
        x = x.mean(axis=1)
    sf.write(str(dst), dehusk(x, sr), sr, subtype="PCM_16")
    return dst


def clear(work: Path) -> None:
    """Delete a song's scratch folder once its cover is finished.

    Only the finished mp3 is worth keeping: the stems cost about 150 MB a song
    and re-separating takes seconds, so caching them turns a small library into
    tens of gigabytes for no gain. The shared scratch files are rewritten on
    every run, so they go too.

    Args:
        work: The song's folder under ``work``.
    """
    import shutil

    if work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
        print(f"  cleared {work}", flush=True)
    scratch = HERE / "work"
    for name in ("_chunks", "_voc_comp.wav", "_mix.wav", "_mix_master.wav"):
        path = scratch / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)


def main() -> None:
    """Parse arguments and drive the three stages."""
    parser = argparse.ArgumentParser(description="Sing a song in a local RVC voice.")
    parser.add_argument("song", help="Audio or video file to cover.")
    parser.add_argument("-v", "--voice", default="ganyu", choices=sorted(VOICES))
    parser.add_argument(
        "--pitch",
        type=int,
        default=None,
        help="Semitones, -12 to 12. Defaults to whatever suits the stem.",
    )
    parser.add_argument(
        "--target-f0",
        type=float,
        default=385.0,
        help="Median vocal pitch the voice sounds natural at, in Hz.",
    )
    parser.add_argument(
        "--vocal-db",
        type=float,
        default=-3.0,
        help="Vocal level against the bed. -3 puts the bed roughly a tenth "
        "under the vocal; the original mixes sit near -4.6 but that buries "
        "the voice.",
    )
    parser.add_argument("--air-db", type=float, default=2.0, help="High shelf on the vocal.")
    parser.add_argument("--low-db", type=float, default=0.0, help="Low shelf at 200 Hz on the vocal.")
    parser.add_argument("--presence-db", type=float, default=0.0, help="Peaking lift on the vocal.")
    parser.add_argument("--presence-hz", type=float, default=3000.0, help="Centre of the presence lift.")
    parser.add_argument("--soft", action="store_true", help="Gentler compression on vocal and mix.")
    parser.add_argument("--consonant-db", type=float, default=-18.0, help="Layer source consonants back in.")
    parser.add_argument("--consonant-hz", type=float, default=4500.0, help="High-pass corner for that layer.")
    parser.add_argument("--no-declick", action="store_true", help="Skip repair of source splices.")
    parser.add_argument("--lufs", type=float, default=TARGET_LUFS, help="Target loudness.")
    parser.add_argument("--index-rate", type=float, default=TUNING["index_rate"])
    parser.add_argument("--rms-mix-rate", type=float, default=TUNING["rms_mix_rate"])
    parser.add_argument("--protect", type=float, default=TUNING["protect"])
    parser.add_argument(
        "--dehusk",
        type=float,
        default=-14.0,
        help="Attenuation of the gaps between harmonics, in dB; 0 disables.",
    )
    parser.add_argument("--out", help="Output file, defaults to covers/<song>-<voice>.mp3")
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep the stems and intermediate wavs under work/, for debugging.",
    )
    args = parser.parse_args()
    TUNING.update(
        index_rate=args.index_rate, rms_mix_rate=args.rms_mix_rate, protect=args.protect
    )

    song = Path(args.song).expanduser().resolve()
    if not song.is_file():
        sys.exit(f"no such file: {song}")

    work = HERE / "work" / song.stem
    work.mkdir(parents=True, exist_ok=True)
    stems = work / "stems"

    # The stems are deleted once a cover is finished, so there is nothing to
    # reuse; a leftover folder only means an earlier run died before clearing.
    if stems.is_dir():
        import shutil

        shutil.rmtree(stems, ignore_errors=True)
    print(f"[1/3] separating stems -> {stems}", flush=True)
    separate(song, stems)
    vocal_stem = pick(stems, "vocal")
    instrumental = pick(stems, "instrument")
    if not args.no_declick:
        instrumental = clean_bed(instrumental)

    pitch = args.pitch
    if pitch is None:
        pitch = auto_pitch(vocal_stem, args.target_f0)
        median = median_f0(vocal_stem)
        print(
            f"  pitch: vocal median {median:.0f} Hz -> {pitch:+d} st "
            f"(target {args.target_f0:.0f} Hz)",
            flush=True,
        )

    converted = work / "vocals_converted.wav"
    print(f"[2/3] converting vocals to {args.voice}", flush=True)
    convert_long(vocal_stem, args.voice, pitch, converted, work)

    # The bed goes up with the vocal, or the cover is sung in one key over
    # music in another. Done after declick so the repair runs on the source
    # sample rate, and before mixing so the consonant layer is unaffected.
    instrumental = shift_key(instrumental, pitch)

    if args.dehusk:
        # The husk is in the model's output, so the filter goes right after it
        # and before anything else touches the signal.
        husked = work / "vocals_dehusk.wav"
        print(f"  dehusk {args.dehusk:+.0f} dB", flush=True)
        dehusk_file(converted, husked)
        converted = husked

    # Resolve before handing it to ffmpeg, which runs with the RVC folder as
    # its working directory and would otherwise write a relative path there.
    out = (Path(args.out) if args.out else HERE / "covers" / f"{song.stem}-{args.voice}.mp3").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[3/3] mixing -> {out}", flush=True)
    mix(
        converted,
        instrumental,
        out,
        args.vocal_db,
        args.air_db,
        args.lufs,
        args.low_db,
        args.presence_db,
        args.presence_hz,
        1.0 if not args.soft else 0.8,
        2.0 if args.soft else 3.0,
        1.5 if args.soft else 2.0,
        args.consonant_db,
        args.consonant_hz,
        vocal_stem,
    )
    print(f"done: {out}")
    if not args.keep_work:
        clear(work)


if __name__ == "__main__":
    main()
