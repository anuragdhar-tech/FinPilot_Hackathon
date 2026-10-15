"""Small robust-statistics helpers (stdlib only) used by the Analyzer and Forecaster."""
from __future__ import annotations

import math
from collections import Counter
from statistics import median

MAD_TO_SD = 1.4826  # scales the median absolute deviation to a standard deviation for normal data


def mad(xs: list[float]) -> float:
    m = median(xs)
    return median(abs(x - m) for x in xs)


def robust_z(x: float, others: list[float]) -> float:
    """Modified z-score of x against others (Iglewicz-Hoaglin). inf if others have no spread and x differs."""
    m, s = median(others), MAD_TO_SD * mad(others)
    if s == 0:
        return 0.0 if x == m else math.copysign(math.inf, x - m)
    return (x - m) / s


def theil_sen(ys: list[float]) -> tuple[float, float]:
    """Robust line through (0, y0), (1, y1), ...: (slope, intercept). Slope = median pairwise slope."""
    n = len(ys)
    slopes = [(ys[j] - ys[i]) / (j - i) for i in range(n) for j in range(i + 1, n)]
    slope = median(slopes) if slopes else 0.0
    return slope, median(y - slope * x for x, y in enumerate(ys))


def mann_kendall_p(ys: list[float]) -> float:
    """Two-sided p-value of the Mann-Kendall test for a monotonic trend (normal approximation, tie-corrected)."""
    n = len(ys)
    if n < 3:
        return 1.0
    s = sum((ys[j] > ys[i]) - (ys[j] < ys[i]) for i in range(n) for j in range(i + 1, n))
    ties = sum(t * (t - 1) * (2 * t + 5) for t in Counter(ys).values() if t > 1)
    var = (n * (n - 1) * (2 * n + 5) - ties) / 18
    if var <= 0 or s == 0:
        return 1.0
    z = (s - math.copysign(1, s)) / math.sqrt(var)  # continuity correction
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


def sample_sd(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
