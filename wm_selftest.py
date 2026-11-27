#!/usr/bin/env python3
"""Self-test: stamp the Gemini watermark onto a synthetic image, remove it again.

    python wm_selftest.py

Prints PSNR for the watermark region.  Anything above ~35 dB means the
watermark is gone and the picture underneath was rebuilt almost losslessly.
This needs no network and no Gemini key.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

import gemini_wm as W


def make_base(w: int = 1024, h: int = 1024, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    r = (x / w * 255).astype(np.uint8)
    g = (y / h * 255).astype(np.uint8)
    b = ((np.sin(x / 37.0) + np.cos(y / 23.0)) * 60 + 128).astype(np.uint8)
    img = np.dstack([r, g, b]).astype(np.float32)
    img += rng.normal(0, 4, img.shape)
    return np.clip(img, 0, 255)


def stamp(img: np.ndarray, size: int, mr: int, mb: int, variant=None, gain: float = 1.0):
    a = W._resize_alpha(W._alpha(size, variant), size)
    a = np.minimum(np.abs(a), 0.99) * gain
    h, w = img.shape[:2]
    x, y = w - mr - size, h - mb - size
    out = img.copy()
    region = out[y:y + size, x:x + size]
    out[y:y + size, x:x + size] = region * (1 - a[..., None]) + a[..., None] * 255.0
    return np.clip(out, 0, 255), (x, y)


def to_png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def psnr(a: np.ndarray, b: np.ndarray, box=None) -> float:
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    if box:
        x, y, s = box
        a, b = a[y:y + s, x:x + s], b[y:y + s, x:x + s]
    mse = ((a - b) ** 2).mean()
    return 99.0 if mse < 1e-9 else 10 * np.log10(255 ** 2 / mse)


def main() -> int:
    ok = True
    print("== watermark variants")
    for size, mr, mb, variant in [
        (96, 64, 64, None),
        (96, 192, 192, "20260520"),
        (48, 32, 32, None),
        (48, 96, 96, None),
    ]:
        base = make_base()
        marked, pos = stamp(base, size, mr, mb, variant)
        out, info = W.remove_watermark(to_png(marked))
        got = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"))
        score = psnr(base, got, (pos[0], pos[1], size))
        good = info["applied"] and score >= 35
        ok &= good
        print(f"  size={size:<3} margin={mr:<4} applied={info['applied']!s:<5} "
              f"region_psnr={score:5.1f}dB {'OK' if good else 'FAIL'}")

    print("== weaker alpha (gain 0.8)")
    base = make_base()
    marked, pos = stamp(base, 96, 64, 64, gain=0.8)
    out, info = W.remove_watermark(to_png(marked))
    got = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"))
    score = psnr(base, got, (pos[0], pos[1], 96))
    good = info["applied"] and score >= 32
    ok &= good
    print(f"  gain={info.get('alpha_gain')} region_psnr={score:5.1f}dB {'OK' if good else 'FAIL'}")

    print("== clean image must stay byte-identical")
    clean = to_png(make_base(seed=11))
    out, info = W.remove_watermark(clean)
    untouched = out == clean and not info["applied"]
    ok &= untouched
    print(f"  applied={info['applied']} conf={info['confidence']:.3f} "
          f"{'OK' if untouched else 'FAIL'}")

    print("\nRESULT:", "ALL OK" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
