"""Blend several RVC voices into one model by weighted parameter averaging.

The checkpoints share the same 457 tensor names, but the two decoder upsampling
kernels differ between sample rates (40k models use 16, 48k models use 24), so
tensors whose shapes differ are skipped rather than mixed. Everything else is
averaged with the requested weights, and the result keeps the architecture and
sample rate of the first named voice.
"""

import json
import sys
from pathlib import Path

import torch

# The weights live beside this plugin's runtime, which the install keeps at
# <root>/qqbot/voice-env; this file is <root>/qqbot/AstrBot/data/plugins/voice,
# so four levels up lands on <root>/qqbot.
W = Path(__file__).resolve().parents[4] / "voice-env" / "src" / "rvc" / "assets" / "weights"
WEIGHTS = {"woyanisha": 0.65, "ganyu": 0.10, "sangduonie": 0.10, "furina": 0.15}
OUT = W / "blend.pth"

# Usage: blend.py [out_name.pth] [voice=weight ...] so ratios can be swept.
if len(sys.argv) > 1:
    OUT = W / sys.argv[1]
if len(sys.argv) > 2:
    WEIGHTS = {k: float(v) for k, v in (a.split("=") for a in sys.argv[2:])}

first = next(iter(WEIGHTS))
ckpts = {n: torch.load(W / f"{n}.pth", map_location="cpu", weights_only=False) for n in WEIGHTS}
base = ckpts[first]
print("checkpoint keys:", list(base.keys()))

merged = {}
skipped = 0
for key, tensor in base["weight"].items():
    if tensor.is_floating_point():
        acc = torch.zeros_like(tensor, dtype=torch.float32)
        total = 0.0
        for name, w in WEIGHTS.items():
            other = ckpts[name]["weight"][key]
            if other.shape != tensor.shape:
                continue
            acc += other.float() * w
            total += w
        if total == 0:
            merged[key] = tensor.clone()
            skipped += 1
            continue
        merged[key] = (acc / total).to(tensor.dtype)
    else:
        merged[key] = tensor.clone()

print(f"blended {len(merged)} tensors, {skipped} kept as-is (shape mismatch)")

out = {
    "weight": merged,
    "config": base["config"],
    "info": f"blend 65/10/10/15 {json.dumps(WEIGHTS)}",
    "f0": base.get("f0", 1),
    "version": base.get("version", "v2"),
    "sr": base.get("sr", "40k"),
}
torch.save(out, OUT)
print("wrote", OUT, OUT.stat().st_size, "bytes")
print("sr:", out["sr"], "f0:", out["f0"])
