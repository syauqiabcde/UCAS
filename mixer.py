"""
Paint mixer for the OTTO liquid handler.

Given a target color (hex) and a maximum number of dyes to use (n),
find the integer drop ratio across all combinations of n dyes drawn
from the available base colors that produces the closest match.

Two forward models are provided:
  - "rgb_avg" : drops-weighted average of RGB values.
                Simple, fast, and empirically close for saturated
                dyes in dilute solution.
  - "subtractive" : convert each dye to CMY, take drops-weighted
                    average of CMY, convert back to RGB. Physically
                    more correct for pigment/dye mixing, but requires
                    the base colors to be reasonably saturated.

Closeness is measured with Delta-E 76 in CIE Lab, which is closer to
human perception than raw RGB distance.

USAGE
-----
    from paint_mixer import suggest_recipes

    # target hex code, up to 3 dyes, total drops up to 10
    hits = suggest_recipes("#8d738a", n_max=3, total_drops_max=10)
    for hit in hits[:5]:
        print(hit)
"""

from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

# =============================================================
# Base colors (edit these to match YOUR physical dye bottles)
# =============================================================
# Hex codes from the user. Violet looks suspiciously pale (#F4DDFF is
# a very light lavender) - probably a transcription slip. If your
# physical violet bottle is dark, replace the hex here with something
# closer to #4A1F8C. Black is a placeholder; measure yours.
BASE_COLORS: dict[str, str] = {
    "yellow": "#FFEC49",
    "orange": "#FF8B38",
    "pink":   "#FF5B78",
    "red":    "#A8210C",
    "green":  "#549447",
    "blue":   "#0082BF",
    "violet": "#F4DDFF",   # <-- CHECK THIS BOTTLE, likely wrong
    "black":  "#000000",   # <-- placeholder, measure yours
}


# =============================================================
# Color-space conversions (pure Python, no numpy dependency)
# =============================================================
def hex_to_rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _srgb_to_linear(c: float) -> float:
    """sRGB (0-1) -> linear RGB (0-1). Standard sRGB gamma curve."""
    c = c / 255.0 if c > 1 else c
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_xyz(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB (0-255) -> CIE XYZ (D65)."""
    r = _srgb_to_linear(rgb[0])
    g = _srgb_to_linear(rgb[1])
    b = _srgb_to_linear(rgb[2])
    # sRGB D65 matrix
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    return x * 100, y * 100, z * 100


def xyz_to_lab(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    """CIE XYZ -> CIE Lab (D65)."""
    # D65 white point
    xn, yn, zn = 95.047, 100.0, 108.883
    x, y, z = xyz[0] / xn, xyz[1] / yn, xyz[2] / zn

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b = 200 * (fy - fz)
    return L, a, b


def rgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    return xyz_to_lab(rgb_to_xyz(rgb))


def delta_e_76(lab1: tuple[float, float, float],
               lab2: tuple[float, float, float]) -> float:
    """Perceptual color distance in Lab space. <2 = imperceptible,
    2-10 = close, >10 = clearly different."""
    return ((lab1[0] - lab2[0]) ** 2
            + (lab1[1] - lab2[1]) ** 2
            + (lab1[2] - lab2[2]) ** 2) ** 0.5


# =============================================================
# Forward mixing models
# =============================================================
def mix_rgb_avg(dye_rgbs: list[tuple[int, int, int]],
                drops: list[int]) -> tuple[float, float, float]:
    """Weighted average in sRGB space. Simple and fast."""
    total = sum(drops)
    if total == 0:
        return (255.0, 255.0, 255.0)
    r = sum(c[0] * n for c, n in zip(dye_rgbs, drops)) / total
    g = sum(c[1] * n for c, n in zip(dye_rgbs, drops)) / total
    b = sum(c[2] * n for c, n in zip(dye_rgbs, drops)) / total
    return r, g, b


def mix_subtractive(dye_rgbs: list[tuple[int, int, int]],
                    drops: list[int]) -> tuple[float, float, float]:
    """Weighted average in CMY space (subtractive mixing).
    Closer to how physical pigments mix. RGB->CMY is C=1-R, M=1-G, Y=1-B."""
    total = sum(drops)
    if total == 0:
        return (255.0, 255.0, 255.0)
    c_sum = sum((1 - rgb[0] / 255) * n for rgb, n in zip(dye_rgbs, drops)) / total
    m_sum = sum((1 - rgb[1] / 255) * n for rgb, n in zip(dye_rgbs, drops)) / total
    y_sum = sum((1 - rgb[2] / 255) * n for rgb, n in zip(dye_rgbs, drops)) / total
    return (255 * (1 - c_sum), 255 * (1 - m_sum), 255 * (1 - y_sum))


MODELS = {"rgb_avg": mix_rgb_avg, "subtractive": mix_subtractive}


# =============================================================
# Search
# =============================================================
@dataclass(frozen=True)
class Recipe:
    colors: tuple[str, ...]         # ordered names, e.g. ("yellow", "blue")
    drops: tuple[int, ...]          # matching drop counts, e.g. (3, 1)
    predicted_hex: str              # what the forward model predicts
    delta_e: float                  # perceptual distance to target
    model: str                      # which forward model was used
    total_drops: int                # sum of drops

    def as_dict(self) -> dict:
        return {
            "recipe": " + ".join(f"{n} {c}" for c, n in zip(self.colors, self.drops)),
            "predicted_hex": self.predicted_hex,
            "delta_e": round(self.delta_e, 2),
            "total_drops": self.total_drops,
            "model": self.model,
        }


def _ratios_for_k(k: int, total_max: int) -> Iterable[tuple[int, ...]]:
    """Yield every integer drop tuple of length k where each drop >= 1
    and total drops in [k, total_max]. Deduplicated by ratio: e.g.
    (2,4) and (1,2) are the same ratio; we keep the smallest form.
    Dedup makes the search faster and results easier to read."""
    from math import gcd
    from functools import reduce

    def gcd_all(nums: tuple[int, ...]) -> int:
        return reduce(gcd, nums)

    seen: set[tuple[int, ...]] = set()
    # For k dyes, we need each dye to have >= 1 drop.
    # Total drops range from k to total_max inclusive.
    def gen(remaining: int, slots: int) -> Iterable[tuple[int, ...]]:
        if slots == 1:
            if 1 <= remaining <= total_max:
                yield (remaining,)
            return
        for first in range(1, remaining - (slots - 1) + 1):
            for rest in gen(remaining - first, slots - 1):
                yield (first,) + rest

    for total in range(k, total_max + 1):
        for tup in gen(total, k):
            # reduce to smallest-integer ratio for dedup
            g = gcd_all(tup)
            reduced = tuple(n // g for n in tup)
            if reduced not in seen:
                seen.add(reduced)
                yield tup


def suggest_recipes(
    target_hex: str,
    n_max: int = 3,
    total_drops_max: int = 10,
    model: str = "rgb_avg",
    top_k: int = 10,
    base_colors: dict[str, str] | None = None,
) -> list[Recipe]:
    """Return the top_k recipes ranked by perceptual closeness to target.

    Parameters
    ----------
    target_hex     : target color, e.g. "#8d738a"
    n_max          : maximum number of different dyes in a recipe
                     (searches 1..n_max)
    total_drops_max: cap on total drops in a recipe (affects search size)
    model          : "rgb_avg" or "subtractive"
    top_k          : how many top recipes to return
    base_colors    : override BASE_COLORS if you want to test with
                     a different palette
    """
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}; use one of {list(MODELS)}")
    mix_fn = MODELS[model]
    bases = base_colors or BASE_COLORS

    target_rgb = hex_to_rgb(target_hex)
    target_lab = rgb_to_lab(target_rgb)

    # Precompute base RGBs once
    base_rgbs = {name: hex_to_rgb(hx) for name, hx in bases.items()}
    color_names = list(bases.keys())

    results: list[Recipe] = []

    for k in range(1, n_max + 1):
        for combo in combinations(color_names, k):
            dye_rgbs = [base_rgbs[c] for c in combo]
            for drops in _ratios_for_k(k, total_drops_max):
                mixed_rgb = mix_fn(dye_rgbs, list(drops))
                mixed_lab = rgb_to_lab(mixed_rgb)
                de = delta_e_76(mixed_lab, target_lab)
                results.append(Recipe(
                    colors=combo,
                    drops=drops,
                    predicted_hex=rgb_to_hex(mixed_rgb),
                    delta_e=de,
                    model=model,
                    total_drops=sum(drops),
                ))

    # Sort by perceptual distance, then prefer fewer total drops as a
    # tie-breaker (simpler recipe wins when quality is equal).
    results.sort(key=lambda r: (r.delta_e, r.total_drops, len(r.colors)))
    return results[:top_k]


# =============================================================
# CLI demo
# =============================================================
def _print_recipe(r: Recipe, target_hex: str) -> None:
    quality = ("excellent" if r.delta_e < 2 else
               "good"      if r.delta_e < 5 else
               "fair"      if r.delta_e < 10 else
               "poor")
    parts = " + ".join(f"{n} {c}" for c, n in zip(r.colors, r.drops))
    print(f"  ΔE={r.delta_e:5.2f} ({quality:9s}) | "
          f"target={target_hex} predicted={r.predicted_hex} | {parts}")


if __name__ == "__main__":
    import sys
    target = "#068a85"
    n_max  = 5
    total  = 3
    model  = "rgb_avg"

    print(f"Target color   : {target}")
    print(f"Max dyes       : {n_max}")
    print(f"Max total drops: {total}")
    print(f"Forward model  : {model}")
    print(f"Base palette   : {list(BASE_COLORS.keys())}")
    print()
    print("Top 10 recipes:")
    for r in suggest_recipes(target, n_max=n_max, total_drops_max=total, model=model):
        _print_recipe(r, target)