"""Build-time: convert an animated WebP into a MicroPython-loadable frame bundle.

Reads the source WebP, resizes each frame to a canonical square, drops
sub-threshold-alpha pixels (they're background), and encodes the rest
as horizontal runs of (y, x, length) triples — one byte per field.
That keeps each frame to a few hundred bytes, so the full animation
fits in a small `.py` module that rides along with the rest of the
device bundle (no extra binary upload path, no on-device PNG decode).

Output: `device/burst_frames.py` containing WIDTH/HEIGHT/COLOR/
FRAME_MS constants and a `FRAMES` tuple of bytes objects. Each bytes
is the concatenated run-triples for one animation frame.

Rendering on-device is a loop of `fillRect(x, y, length, 1, COLOR)`
calls — portable, no drawPng dependency, and fast enough on the
ILI9342 to animate at ~8 fps once the first frame is hot in the
MicroPython opcode cache.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image


_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DEST = _ROOT / "device" / "burst_frames.py"

# Canonical size chosen to fit every on-device placement (launcher
# upper-right, Hello upper-right, Snake game-over centered, Buddy
# idle right side). 72 px gives ~36 px radius, the same reach the
# old procedural burst used.
_OUT = 72

# Alpha cutoff: pixels below this are treated as transparent and
# dropped. Chosen high enough to keep the silhouette clean on a
# black background without chewing through bytes on anti-alias edges.
_ALPHA_MIN = 120


def _encode_frame(img: Image.Image) -> tuple[bytes, int, int, int, int]:
    """Return (runs, r_sum, g_sum, b_sum, count) for one frame.

    `runs` is the concatenation of (y, x, length) byte triples for
    every horizontal span of opaque pixels. The color statistics are
    returned so the caller can compute a single dominant color across
    the whole animation — encoding per-pixel color would blow the
    byte budget and the shape is effectively single-hued anyway.
    """
    img = img.convert("RGBA").resize((_OUT, _OUT), Image.LANCZOS)
    px = img.load()
    out = bytearray()
    r_sum = g_sum = b_sum = cnt = 0

    for y in range(_OUT):
        x = 0
        while x < _OUT:
            _, _, _, a = px[x, y]
            if a < _ALPHA_MIN:
                x += 1
                continue
            start = x
            while x < _OUT and px[x, y][3] >= _ALPHA_MIN:
                r, g, b, _ = px[x, y]
                r_sum += r
                g_sum += g
                b_sum += b
                cnt += 1
                x += 1
            length = x - start
            # Each byte field is 0-255; our rows are <= _OUT (72) so
            # no run will ever overflow — keeping the encoding flat
            # lets the device-side decoder stay a three-byte loop.
            out += bytes((y, start, length))

    return bytes(out), r_sum, g_sum, b_sum, cnt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--src",
        type=Path,
        required=True,
        help="Path to the animated WebP to encode (e.g. waiting.webp).",
    )
    ap.add_argument("--dest", type=Path, default=_DEFAULT_DEST)
    ap.add_argument("--frame-ms", type=int, default=80)
    args = ap.parse_args()

    if not args.src.is_file():
        sys.stderr.write("source WebP not found: {}\n".format(args.src))
        return 2

    im = Image.open(args.src)
    n_frames = getattr(im, "n_frames", 1)
    frames = []
    r_total = g_total = b_total = total_cnt = 0
    total_run_bytes = 0

    for i in range(n_frames):
        im.seek(i)
        runs, rs, gs, bs, cnt = _encode_frame(im)
        frames.append(runs)
        total_run_bytes += len(runs)
        r_total += rs
        g_total += gs
        b_total += bs
        total_cnt += cnt

    if total_cnt == 0:
        sys.stderr.write("no opaque pixels found — is alpha threshold too high?\n")
        return 2

    r_avg = r_total // total_cnt
    g_avg = g_total // total_cnt
    b_avg = b_total // total_cnt
    color = (r_avg << 16) | (g_avg << 8) | b_avg

    lines = [
        '"""Generated burst animation — do NOT edit by hand.',
        "",
        "Source: {}".format(args.src.name),
        "Canonical size: {}x{}  ({} frames)".format(_OUT, _OUT, n_frames),
        "Encoded runs: {} bytes total".format(total_run_bytes),
        'Regenerate with `python3 scripts/gen_burst_frames.py`.',
        '"""',
        "",
        "WIDTH = {}".format(_OUT),
        "HEIGHT = {}".format(_OUT),
        "COLOR = 0x{:06X}".format(color),
        "FRAME_MS = {}".format(args.frame_ms),
        "FRAMES = (",
    ]
    for runs in frames:
        # Bytes literal repr is compact and MicroPython-compatible.
        lines.append("    {!r},".format(runs))
    lines.append(")")
    lines.append("")

    args.dest.write_text("\n".join(lines))
    sys.stderr.write(
        "wrote {} ({} frames, {} bytes of run data, color 0x{:06X})\n".format(
            args.dest, n_frames, total_run_bytes, color
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
