"""Removes the visible Gemini "sparkle" watermark from generated images.

Server-side port of the alpha-unblend technique used by browser extensions:
the watermark is a semi-transparent white 4-point star baked in with
    px_out = px_in * (1 - a) + a * 255
so it can be reversed with
    px_in  = (px_out - a * 255) / (1 - a)
where `a` is the per-pixel alpha template (36 / 48 / 96 px variants).

Detection is template based (normalized cross-correlation on luminance and on
Sobel gradients).  If no candidate scores high enough the image is returned
untouched, so non-Gemini images and clean images are never damaged.
"""

from __future__ import annotations

import base64
import io
import logging
import zlib
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

log = logging.getLogger("gemini_wm")

# ---------------------------------------------------------------- alpha maps
# zlib+base64 compressed float32 little-endian, n*n values, signed alpha.
_ALPHA_B64 = {
    "36": (
    "eNq1mL1uE0EUhf0IFBQUFDwCBd1mRzwABQUFBQUFJQWPsO5CCMGRnOCEEJwQQuiCBD30RHIkkBwJJEeCDiR4A/ytc7zj2Vnv7GYT6Srj2Zm5Z+"
    "8992e2nbQW2jXl86eb6f/jwW1zOrpv3Pmyfe0GdNu/W+O/94fX441ezyCM20kSaa29pwkM87AJz+loFL/Z2zOvd3cNY+bq6U6iJrB9+3rHgAdh"
    "fFE2KPddEv37+yhe63aN7DPx2WFqo9n39r27O19unyK7C8/HDzdSLK+2t83LrS3zdn/fa6P8OUW6izGJIz4es+/x4mL8fH09xQMWBBu92Nyc2i"
    "iMR7JTdf4IC+M/vx+adwcHUywSbPTj+z2TnV/kExdHNTx2TK12Lpmdfj8V7CEsjDW/0btiZKPMTvN0VrORsBwPBjG6XSxwCGEOvzF3Mhw63M5j"
    "qpIbbA5x7hhD/GxlJfWJOGyL5sAEr1g7jjkPprCYsm3rYuHcp8vLKRbpdPG482B6srTkwVSXu5O92F128WGYJ+xhL34WR6rmb9mE8U7/2vRdQz"
    "FgI9uf2ksccKbOLuK5jXeyNonIIdRszhU/fT5yeaQ17jxnEAPkCXKXHU9neWQGg3CMc0pMjiX3i7dFXCnjjzsPHnIWeZTcTr2Z1T/BQ59AbaaP"
    "EQ7ZxKenaOyud59pTvWO2odO+DnmRWozcikYeK51tp4QHLbusvW2vdElAcPRl1tGNlHPoPpYxUdVRWfjP7s3UJ3BVvKX+gfFRBPccZ/LF2CY9L"
    "qjM3+5fG4twDG4BufgnlsXQnjj+y0+w01wEDPEsI/Pdl5WnBGTxCZnuNyuE1uKd3IIMYyO4jrir1f8J4dxXlFuDom17H2umvKe0V/nsl5nUi9W"
    "O53S2uV7xh72Ui/K7FFW74tqe6jvWEsNnl/j64lqPO/qq2V2ffD1G26t8tkhvL/N92PqlX0cgreM5aOift7mq69XD+uNkgheotNXSxWPa93LM/"
    "3q+e+n+b7f7ud//XyQ85s4Q54I5Wz1O3U+Bnlvcoh4In8xJo+Su8r65ua/MSQROVbxpj6J3N7UXbzqPYx8r9yr/oF6k7/nhPnror4nFN1ry7h9"
    "Xmz29xbE/t4SYgP/XXx2HIox81n2PUo9XtgdtN5dI0ROhnfT3q7udwEb739/+zJ6"
    ),
    "48": (
    "eNrVWktSFEEQLSM4iPewq/AoLjzIjDFE4A7xAgNeZMAN7BAvMKDuES8gZXV1ffJlVnZPDyFGVNA9/XuZ+fJbLhemWy4Wb/wy1T/ut/z3eF7/5v"
    "/+uL93d9uty+9JK323Pr7YvO24+8M1HjOHE12rr8dz//dodfCE2xz2y4XfTVc/TzGarpSHXq/lk2RI30vvTdfr83T/p5OTAb8/pu8rMcX3eWwl"
    "viQTtku4B3EGf0/mQrw/8Cbg//3w4DBuxJda/5xNktwt/XO85+1lcu4UHMrtmWxoCB+SHSQ+Dd/rWjrX4I7HZ+s1wX+x2TiJl8g+UYbcPlof5H"
    "xEsqE//nh8bIHu+7WwSAeYN7pFdZnrwkC5kWzxGOk+ri/n567kTnpHHRf18pTcyf1Kfld+Xziudf9h+QrJ4fJn6+/mnC/5Y1j5OE7l12tdI1/J"
    "Yw63vB9oeFFj5+2DZCjtW/Ortl1/3sQe1932nSvfl8fQXM9G1DsXV3IdoHgHbGO12GM+eLKDLeO/KRbOOzgX8LqmsTTFpuEdVsMbaoN/z1iEoZ"
    "3HSh+V8m4rD3+/vR2NvfQFudbR4B+ZW4dnQjychj2uEG+pfvga1LD1JycH8g0pzk+RQYcf+y8Xb5g8aXu7HypivXp9u7lxfRyAeauXZ/B1FNNb"
    "tY+PGZyv7oo/+nTIgQtVvuRqG6R/H7N93JuLM9I6Wq0c7ccMrOX4GDTI6xBf9r36vtNy+QHVAxXX7RzxhVtavvXxyXL8QX2rpO85eD49zhqH+z"
    "3jfI/6XPzelVd9b+1l6abk/v9lpfnMy8SfZkuBPy9BFs/xjD+oTnZz1gZzrT6mOC7+gNgP4+eU+LNLzPIY+pys6GVpHza1vp9P39PnEZV9nkWG"
    "P4+P7tfP95Nwc/k4q/esj1n74oq3c+orW31MGz9XV4Rady98sfo+rD2Lleb/UoySbOCv1df9u8o5D8WJ5kDaup/Oh8LzuQw5pjH4Y/8Yav4FW2"
    "eWtbPpZDsZMPfCdvt6eel267kMqe3pTJTW/vw+wYCNzL5QT3q2fi36NGeLur6neEi/3uV5FusXz5TpDK+cW15fXdnxtWucRdffo30XP/+mcxV5"
    "zsjP83x/OQK7Q3PnevZX813yxbF5As37NH0m3Y8s97+kPSlpD2JsvsP2Mk7DG9p/m07W5RR83B5L/d1ST1JeyGIl2Sdq7xPupmfKObaOtRrd6/"
    "d8W/hb/onsx3/Py+FzAp+ndHvUejk0+DFfcbwLfgDivpP3G+fC3/IZGnfQPq6vg3HMwfvVfHwfj5/bV27Lm84/n54O+MOxrn8aj18jH93rwPVK"
    "IZ9L88wDJ9mP+38W0+tnfn+pVWvn+cDzhstXyI+w3eXzv9C38aM="
    ),
    "96": (
    "eNqtXc+vZUURbtEFCxfzJ8zChQsX7s+9Bxb+KSxc+Ef0DEMyJJoAYoI7QEBMNAHE/Yi4R8QEd4hDookmiLh36p3b71RXfV919XnzkpN3f5x7z7"
    "nV3VVffVVddafW0+8fPH26c/mvD3mtPPrTz7f/5fTaq69e/W+HnLc9ros+2vnt8+0xOvbz2vf137Ud2/W+ePjM+t+vvlr//vnn67//9aO1Xau/"
    "j3K5Zj21x/a36Gvv51Z9z8v+/v699p7td7bvQTLdZFeVPP319OvmGov+Lf191aXJrVz+0Oftvcv96PeL+vPPr651fvT/KTnu3vnG1fHiC7fO/b"
    "gVcO/9a0Zei/1ce96ua+YEkW8h8/nBtdzN/wXNGTQP+/u097vL3j7unyO57PeqZd6P4/7ZN9/4zqrlL///+OEPVn+u/fz2evv9ep6isbPv67mi"
    "fjdYS9XM7+KuZ3+vvm7TLf37u56xc9vKC8vcjYtd94uWgV0DWiZvvvHGauf/o3tekb4CY9LJxcpY60ArS6IvlD4p3fq242xfa/Jt96ffV59z8m"
    "s2QV/L6n+7ZoEO6eYR0GVwPchrXzx8eC3/dogt0DoUPcZz081TvdY7eaCxsHqT6SC97sy4LNrGRuun2WOrf/S923XBdZD7Pct+b0Vdq3Rz87l7"
    "31qbzrHH8/efXLne78bE6Qs7/kweWv/4tbOPG7K7dwDWsfPbji2ehwXNlcXONf37teyxLdznvbFFnc0Q3WPl357/9KWX1t5+FjC3+7HZ5Yn1Tz"
    "RODLtZe8vGG+ErjMcKnENW19j12NsQJxdo0zHu3D8jeFPLXz9uOgjJ38oPYMcF3SPGlQXiOKTrrf7i2LecMAayGL4Ym4nGpBDfANs3q3vNmry+"
    "z1+9/d3VyrzZ3/Zc9JP+PoTT2bxFvks/Fz3OY+Pr7XLpdJjWu3oOajtn5sDCbaX3r7C+8ofF38y+yPHb996Duke/9ocPPli1vmO+ENF9i/VZzD"
    "kDjFqBjAv0BQJ8GswBP0eBvlqYD+x1WaXyt7b6+fv3z8jm2vWw2WE51/sa2L+y94n1ntVpzPdC+NbqLq/7CvADnW5Z/H0j33L/HRZrMjtDcEB3"
    "jQ3fY7lb+T93797qfZPi8DvRuwuyiQZfnzxOcethQfob8BDdb0V+qvVhCM/icKz1LRBm8D6avt72WOaz1TPR/L/Y4TPmFHqZa8zO7A57L8C6xM"
    "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"
    "mRqa8p7eQ8lyS2diOK0WdNT/i+/pLa72VYb3x70DC+0FFNXot7rNy5nX5lX9pJbRGLTfKXkEjatDnEMmZ0jLfusXVikeRjkHM3XlM9hlVLfJ9r"
    "2L+ktGujHigTKYQnPWiOvJ5lrr99t+Uez327rzeH5FNjmK5Qb13E665914LcGePJDfHtcnqkHdpl0XIjw0K/+tV2EdcvL8ccnIJtm3qg7rXfG+"
    "PrQ/Faq5C31zi0nx2ipqDdw/SyxkVBMx4uUsl4/2YfnfX12NvBjL+54aGf8M1+8oMP4yqr0e2Idlxi+x92nzKSJMal9rc9/OdStvUp+c1roe6Y"
    "zsvvg851lPuH9QhT3WkC/C6jBG8c4tZnPrrPek2fwuNvfFfus8TN4LsEC9aOMrWRw34ytEPB/j9tj8QL2PGGfKaoSwGIasgcx+F8sx2Pr5cZ+D"
    "QvslZOJ7mb4AI06ejZ3tNc57lkW9rmtK/qj/hsRrLEc68ge2vejlFPM8hepb7wfX06gGUBxLLyH+iPEk6wNXID8d18Kh/QdPUV8zXR96VItP6/"
    "3IXrI6Uig/mfHRGX3OOdYy1Ie2Z5iuUeD789oezgXWZ2K+Nu47vOe9Z3NjWxwX9e3FvShQbxLbkzcXq2VjPopTRbUo0b2yvqnB3F9svlyGA9ff"
    "p+sgMHvQ+u1EfTFx/9ES5tGwvFrW0zlhN5aoRt+I00B2eFzvCed/ReNgrruO+n/1NRdcDv4S5EctvE90pX1VMj1hsvPL3m/Ql3mJ+puM+iyQ/U"
    "1U/2t9EcUqL7U7pzBhL3/U83Tcl2fc29avKV9PusA1GvGgPg4Q1vCGteoj7gjhQckBZ3rf2t1R/h/Pz8F9Qpj/zvguxqex/B/uV9dBXzVa04bE"
    "u2jfs2FMX/NyFvts+yIK5UKYfR/VFw/qxHQ9ZFHvaJufH+FPtIcGzUePZ0tXPxT0oVrGsXzMQ6LHtpburnviXADzm0IcoXt+8D4IvIY5qj/OOf"
    "16OI6v743leUW9/jJ4yK4/25NTjkt/ZrBfDfXZ8/6HsQO0hj/o3xX25xrx0SyfZRAzgz1kUb/gmZwtxtXZMZf9+1b+wvFzfrDSPrEAByxx/zvW"
    "Fy3qV15OUU/zKLY/znvntiGf5zLmVWwNQKt/Lj3fIRcQ9wEp1I/xfaF9bUi2l4L0zj6hvlEjPtDKCPUbQ32r43VYqf2N8t7b+5IvJLkSYgukhg"
    "vJ9V54H76azq3kvS3rKVcfvctt6NbG/wH9DF8E"
    ),
    "96-20260520": (
    "eNrlnT+sZVUVxl9hYUFBYWFhMYWFhcUUFiZz7g2FhYWFhYWFxRQWFhYWFhQWTwQccMABAQEBB0QEHBBQcUBN3iSaDIkmkGgCiSZDogkmmkCiCS"
    "SaeH/n3O/dfffZa6+1z7nvzaA3OXn3vvN/7b2/9a0/e+1LBzd0X9/fP7W3+uR/9eH3Ztvr2Db7ht/an5+/fW7pfP3POn98/UsHNywePX9i+a9/"
    "fnX5jZtuWq5+L1979bOLvcInvV/k+t776ZjtZx8fn8vPuv+lXv7j58tluC3LvaTNyvIvyTO/T/oOpfbO9/Od573r3LnFb3/zqeVtZ84sv33nnc"
    "s3Xv/88tHz5xfs2zzT+Hz7+uP3s57Pen/vffL3z8+x7l/7SB65vGt9u3y/7fPy49LPpYOD7rVXX138+U9fWH7vwQeXDz/00PIff//y8m9vvbV4"
    "88qVzrqO9XzWsXuNH1+mlnzy/236c3ksl9rfvlc+/vPtUoZ/9X65f+rNK6d77Pnmrbcun3/uuX7j+w8f/+iSffV3G+OijbOl8eufb12r9E42Xu"
    "531r225am22sa/zZgY9gkXSmPCOr+Er/x+5fLlxQrrlw/cf//ypYsX++279923fOXyp1d64GBRxusy3pfxw3+/tB9a+mQbHyz5bONSRH/k+qIm"
    "T//5N2Oghs3JPTpwBrxH/r948cV+Q/5//MPnegzK+46nT8bj2cP/sny869f4QEQH1MdrWX9PwVHrQxvBcR5+6CMrOX9pee899xziD9/5H/s4Jh"
    "0/c+5ZO791n3e8h01R/WrhZrl/7HUeRupY9O4Ka3rsueXmm/s+/+Onn+43vsNDGQMcwxiw+qL9XHtdef9eF5GJxfc8fTLWjz4/z/VFzoHqeO6P"
    "xzGeglEH3YrfLN55+yvLO86e7fHnR0880cuf7/wPe4BjONbCi/y3j6fj/tD+/Jv+VuPfKV6P72m1ddl+qfOfMr7aPGOve/65kz3vge/DO9l+8N"
    "hj/Yb8+b2yC5YcAwZZ94/htc2Dys9fG8OR+9fxpz5+7PGRt4fF18o8ZdtGBFd+/7vPYGetuObjvby//8gj/aa2YB/4BEeSHi71qZoMPC4ZOd/j"
    "r559WsPLku4tt2/JVhjjbeSdV8cu7jh73RLseerJJ/tNthcb38Ei2uW9d2/sx8Dq2IX1fHPsg6idMOX6Hv+07S2P326fn++v+Wv4Tt//9a+65b"
    "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"
    "eMhpwCdBTYt8fceYvpmW7xmRfzR/6r/smP+J"
    ),
}

_MIN_ALPHA = 3.0 / 255.0
_MAX_ALPHA = 0.99
_CACHE: Dict[str, np.ndarray] = {}


def _alpha(size: int, variant: Optional[str] = None) -> Optional[np.ndarray]:
    """Return the native-resolution alpha template for a size/variant.

    Only 36 / 48 / 96 templates ship in the extension data; any other size is
    served by the closest template of the same variant and resampled later by
    ``_resize_alpha``.
    """
    key = str(size) if not variant else "%d-%s" % (size, variant)
    if key in _CACHE:
        return _CACHE[key]
    raw = _ALPHA_B64.get(key)
    if raw is None:
        candidates = [k for k in _ALPHA_B64 if (k.endswith("-" + variant) if variant
                                                else "." not in k and "-" not in k)]
        if not candidates:
            return None
        if variant:
            key = candidates[0]
        else:
            key = min(candidates, key=lambda k: abs(int(k) - size))
        raw = _ALPHA_B64[key]
    buf = zlib.decompress(base64.b64decode(raw))
    arr = np.frombuffer(buf, dtype="<f4").astype(np.float32)
    n = int(round(len(arr) ** 0.5))
    if n * n != len(arr):
        return None
    arr = arr.reshape(n, n)
    _CACHE[key] = arr
    return arr


def _resize_alpha(a: np.ndarray, size: int) -> np.ndarray:
    if a.shape[0] == size:
        return a
    im = Image.fromarray(a.astype(np.float32), mode="F")
    im = im.resize((size, size), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)


# ---------------------------------------------------------------- helpers
def _to_gray(rgb: np.ndarray) -> np.ndarray:
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0


def _sobel(g: np.ndarray) -> np.ndarray:
    p = np.pad(g, 1, mode="edge")
    gx = (-p[:-2, :-2] - 2 * p[1:-1, :-2] - p[2:, :-2]
          + p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:])
    gy = (-p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]
          + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
    return np.sqrt(gx * gx + gy * gy)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape or a.size == 0:
        return 0.0
    a = a.astype(np.float64).ravel()
    b = b.astype(np.float64).ravel()
    a -= a.mean()
    b -= b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    if denom < 1e-8:
        return 0.0
    return float((a * b).sum() / denom)


def _ncc_masked(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    """Pearson correlation restricted to the watermark mask."""
    if a.shape != b.shape or a.size == 0 or not mask.any():
        return 0.0
    av = a[mask].astype(np.float64)
    bv = b[mask].astype(np.float64)
    av -= av.mean()
    bv -= bv.mean()
    denom = np.sqrt((av * av).sum() * (bv * bv).sum())
    if denom < 1e-9:
        return 0.0
    return float((av * bv).sum() / denom)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


_SOBEL_CACHE: Dict[int, np.ndarray] = {}


def _alpha_grad(size: int, variant: Optional[str]) -> Optional[np.ndarray]:
    key = "%d:%s" % (size, variant or "")
    if key in _SOBEL_CACHE:
        return _SOBEL_CACHE[key]
    a = _alpha(size, variant)
    if a is None:
        return None
    g = _sobel(a)
    _SOBEL_CACHE[key] = g
    return g


# ---------------------------------------------------------------- configs
def _configs(w: int, h: int) -> List[Tuple[int, int, int, Optional[str]]]:
    """(logo_size, margin_right, margin_bottom, alpha_variant) candidates."""
    cfgs: List[Tuple[int, int, int, Optional[str]]] = []
    fixed = {
        (2752, 1536): [(48, 89, 89, None)],
        (1408, 768): [(48, 32, 32, None)],
    }
    cfgs.extend(fixed.get((w, h), []))
    tier = "0.5k" if min(w, h) <= 640 else "1k"
    if tier == "0.5k":
        cfgs += [(48, 32, 32, None), (36, 96, 96, "v2"), (48, 96, 96, None)]
    else:
        cfgs += [
            (96, 64, 64, None),
            (96, 192, 192, "20260520"),
            (48, 96, 96, None),
            (48, 32, 32, None),
        ]
    out, seen = [], set()
    for c in cfgs:
        if c in seen:
            continue
        seen.add(c)
        s, mr, mb, _ = c
        if w - mr - s < 0 or h - mb - s < 0:
            continue
        if _alpha(s, c[3]) is None:
            continue
        out.append(c)
    return out


def _score(gray: np.ndarray, grad: np.ndarray, cfg, x: int, y: int, size: int,
           alpha_map: np.ndarray, alpha_grad: np.ndarray) -> Dict[str, float]:
    h, w = gray.shape
    if x < 0 or y < 0 or x + size > w or y + size > h:
        return {}
    patch = gray[y:y + size, x:x + size]
    gpatch = grad[y:y + size, x:x + size]
    spatial = _ncc(patch, alpha_map)
    gradient = _ncc(gpatch, alpha_grad)

    var_score = 0.0
    if y > 8:
        ref_h = min(size, y - max(0, y - size))
        if ref_h > 8:
            ref = gray[max(0, y - size):max(0, y - size) + ref_h, x:x + size]
            if ref.size:
                inner_sd = float(patch.std())
                ref_sd = float(ref.std())
                if ref_sd > 1e-8:
                    var_score = _clamp(1.0 - inner_sd / ref_sd)

    conf = _clamp(max(0.0, spatial) * 0.5 + max(0.0, gradient) * 0.3 + var_score * 0.2)
    return {"confidence": conf, "spatial": spatial, "gradient": gradient,
            "variance": var_score}


def _best_in_window(gray, grad, cfg, x0, y0, size, alpha_map, alpha_grad,
                    spread: int, step: int) -> Optional[Dict[str, Any]]:
    best = None
    h, w = gray.shape
    for dy in range(-spread, spread + 1, step):
        for dx in range(-spread, spread + 1, step):
            x, y = x0 + dx, y0 + dy
            if x < 0 or y < 0 or x + size > w or y + size > h:
                continue
            sc = _score(gray, grad, cfg, x, y, size, alpha_map, alpha_grad)
            if not sc:
                continue
            sc.update({"x": x, "y": y, "size": size, "cfg": cfg,
                       "variant": cfg[3]})
            if best is None or sc["confidence"] > best["confidence"]:
                best = sc
    return best


def _estimate_gain(rgb: np.ndarray, alpha_map: np.ndarray, x: int, y: int,
                   size: int) -> float:
    """Find the alpha gain that best cancels the watermark.

    Removing with gain g leaves a residual that correlates with the template;
    the correlation decreases monotonically with g and crosses zero at the
    true gain, so the |residual correlation| minimum is the estimate.
    """
    patch = _to_gray(rgb[y:y + size, x:x + size])
    mask = np.abs(alpha_map) > 0.05
    if _ncc(patch, alpha_map) <= 0:
        return 1.0
    def residuals(gains):
        out = []
        for gain in gains:
            cleaned = _unblend(rgb, alpha_map, x, y, size, gain=float(gain))
            region = _to_gray(cleaned[y:y + size, x:x + size])
            out.append((abs(_ncc_masked(region, alpha_map, mask)), float(gain)))
        return out

    coarse = residuals(np.arange(0.50, 1.401, 0.05))
    best_residual, best_gain = min(coarse)
    fine = residuals(np.arange(max(0.30, best_gain - 0.05),
                              min(1.50, best_gain + 0.05) + 1e-9, 0.01))
    fine_residual, fine_gain = min(fine)
    if fine_residual <= best_residual:
        best_residual, best_gain = fine_residual, fine_gain
    return round(best_gain, 3)


def _unblend(rgb: np.ndarray, alpha_map: np.ndarray, x: int, y: int, size: int,
             gain: float = 1.0) -> np.ndarray:
    """Return a copy of rgb with the watermark region reversed."""
    out = rgb.astype(np.float32).copy()
    region = out[y:y + size, x:x + size]
    a = np.minimum(np.abs(alpha_map) * gain, _MAX_ALPHA)
    active = (np.maximum(0.0, np.abs(alpha_map) - _MIN_ALPHA) * gain) >= 0.002
    a3 = a[..., None]
    fixed = (region - a3 * 255.0) / (1.0 - a3)
    region[active] = fixed[active]
    out = np.clip(out, 0.0, 255.0)
    return out


# ---------------------------------------------------------------- public API
def remove_watermark(data: bytes, *, min_confidence: float = 0.32,
                     refine: bool = True, gain: Optional[float] = None) -> Tuple[bytes, Dict[str, Any]]:
    """Strip the Gemini sparkle watermark from encoded image bytes.

    Returns (bytes, info).  When nothing is detected the original bytes are
    returned unchanged and info["applied"] is False.
    """
    info: Dict[str, Any] = {"applied": False, "confidence": 0.0, "reason": ""}
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001
        info["reason"] = "decode-failed: %s" % exc
        return data, info

    fmt = (img.format or "PNG").upper()
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info)
    base = img.convert("RGBA" if has_alpha else "RGB")
    rgb = np.asarray(base, dtype=np.uint8)[..., :3]
    h, w = rgb.shape[:2]

    gray = _to_gray(rgb)
    grad = _sobel(gray)

    best: Optional[Dict[str, Any]] = None
    for cfg in _configs(w, h):
        size, mr, mb, variant = cfg
        x0, y0 = w - mr - size, h - mb - size
        amap = _resize_alpha(_alpha(size, variant), size)  # type: ignore[arg-type]
        agrad = _sobel(amap)
        sc = _score(gray, grad, cfg, x0, y0, size, amap, agrad)
        if not sc:
            continue
        sc.update({"x": x0, "y": y0, "size": size, "cfg": cfg, "variant": variant})
        if best is None or sc["confidence"] > best["confidence"]:
            best = sc

    if best is None:
        info["reason"] = "no-candidate"
        return data, info

    # refine position / scale around the winning candidate
    if refine and best["confidence"] >= min_confidence * 0.8:
        size0, mr, mb, variant = best["cfg"]
        amap = _resize_alpha(_alpha(size0, variant), size0)  # type: ignore[arg-type]
        agrad = _sobel(amap)
        refined = _best_in_window(gray, grad, best["cfg"], best["x"], best["y"],
                                  size0, amap, agrad, spread=8, step=2)
        # also try slightly larger / smaller logos
        for delta in (-4, 4):
            alt_size = size0 + delta
            if alt_size < 24 or w - mr - alt_size < 0 or h - mb - alt_size < 0:
                continue
            am2 = _resize_alpha(_alpha(alt_size, variant), alt_size)  # type: ignore[arg-type]
            ag2 = _sobel(am2)
            r2 = _best_in_window(gray, grad, (alt_size, mr, mb, variant),
                                 best["x"], best["y"], alt_size, am2, ag2,
                                 spread=4, step=2)
            if r2 and refined and r2["confidence"] > refined["confidence"]:
                refined = r2
        if refined and refined["confidence"] >= best["confidence"]:
            best = refined

    info.update({
        "confidence": round(best["confidence"], 4),
        "spatial": round(best["spatial"], 4),
        "gradient": round(best["gradient"], 4),
        "size": best["size"],
        "position": [best["x"], best["y"]],
        "variant": best.get("variant") or best["cfg"][3],
        "image_size": [w, h],
    })

    if best["confidence"] < min_confidence:
        info["reason"] = "below-threshold"
        return data, info

    amap = _resize_alpha(_alpha(best["size"], best.get("variant")), best["size"])  # type: ignore[arg-type]
    used_gain = gain if gain else _estimate_gain(rgb, amap, best["x"], best["y"], best["size"])
    info["alpha_gain"] = used_gain
    cleaned = _unblend(rgb, amap, best["x"], best["y"], best["size"], gain=used_gain)

    # sanity check: the watermark correlation must drop, otherwise undo
    after = _to_gray(cleaned)[best["y"]:best["y"] + best["size"],
                              best["x"]:best["x"] + best["size"]]
    after_score = _ncc_masked(after, amap, np.abs(amap) > 0.05)
    info["post_spatial"] = round(after_score, 4)
    if abs(after_score) > max(0.20, best["spatial"] * 0.5):
        info["reason"] = "removal-not-confirmed"
        return data, info

    out_img = np.array(base, dtype=np.uint8, copy=True)
    out_img[..., :3] = cleaned.astype(np.uint8)
    result = Image.fromarray(out_img, mode="RGBA" if has_alpha else "RGB")

    buf = io.BytesIO()
    if fmt in ("JPEG", "JPG"):
        result.convert("RGB").save(buf, format="JPEG", quality=95, subsampling=0)
    elif fmt == "WEBP":
        result.save(buf, format="WEBP", quality=95, method=4)
    else:
        result.save(buf, format="PNG", optimize=True)
    info["applied"] = True
    info["format"] = fmt
    return buf.getvalue(), info
