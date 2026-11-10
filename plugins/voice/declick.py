"""Declick helper, kept separate so cover.py can import it cheaply."""

from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
STEP = 0.40
RATIO = 6.0
PAD = 8


def find_clicks(x: np.ndarray, sr: int = SR) -> list[int]:
    """Return sample indices that follow a discontinuity.

    A real music transient moves the waveform quickly, but never by close to
    full scale in a single sample at 44.1 kHz. Anything that does, and that
    towers over the local rate of change, is a splice or a digital dropout.

    Args:
        x: Mono signal.
        sr: Sample rate.

    Returns:
        Indices of the step that follows each discontinuity.
    """
    d = np.abs(np.diff(x))
    win = int(0.02 * sr)
    hits = []
    for i in np.where(d > STEP)[0]:
        local = float(np.median(d[max(0, i - win) : i + win]))
        if d[i] > RATIO * max(local, 1e-6):
            hits.append(int(i))
    return hits


def bridge(x: np.ndarray, hits: list[int], pad: int = PAD) -> tuple[np.ndarray, int]:
    """Fill each discontinuity with a short linear ramp.

    Only a handful of samples per event is replaced, so the surrounding
    music keeps its phase, level and spectrum.

    Args:
        x: Mono signal.
        hits: Indices from find_clicks().
        pad: Samples replaced on either side of the step.

    Returns:
        The repaired signal and the number of spans that were bridged.
    """
    out = x.copy()
    spans = 0
    for i in hits:
        left = max(1, i - pad)
        right = min(len(out) - 2, i + pad)
        if right - left < 3:
            continue
        out[left:right] = np.linspace(out[left - 1], out[right], right - left, dtype=out.dtype)
        spans += 1
    return out, spans


def declick_file(src: Path, dst: Path) -> tuple[int, int]:
    """Declick a stereo file, writing 16 bit where the signal allows it.

    The input is read into memory before anything is written, so ``src`` and
    ``dst`` may be the same path. Samples are loaded as float32 and the repair
    only replaces a few frames per event, so the 16 bit container loses
    nothing; a file that overs full scale is kept as float to avoid clipping.

    Args:
        src: Input audio.
        dst: Output audio; the suffix decides the container.

    Returns:
        A tuple of (events found, spans bridged).
    """
    data, sr = sf.read(str(src), always_2d=True, dtype="float32")
    fixed = np.empty_like(data)
    found = spans = 0
    for ch in range(data.shape[1]):
        hits = find_clicks(data[:, ch].astype(np.float64), sr)
        fixed[:, ch], n = bridge(data[:, ch], hits)
        found += len(hits)
        spans += n
    # A separator can hand back samples past full scale on a heavily limited
    # master. Writing those as 16 bit would clip them, and a clipped sample is
    # the same crackle this function is here to remove, so such a file stays
    # float.
    subtype = "PCM_16" if np.max(np.abs(fixed)) <= 1.0 else "FLOAT"
    sf.write(str(dst), fixed, sr, subtype=subtype)
    return found, spans
