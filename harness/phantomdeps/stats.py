"""Interval estimates used in the analysis.

* Wilson score interval for a binomial proportion [Wilson 1927].
* Cluster percentile bootstrap (clusters are tasks, or (model, prompt) pairs for
  persistence), because answers to one task and declarations within one answer are not
  independent [Field and Welsh 2007].  Seed 20240628 throughout.
"""

from __future__ import annotations

import math

import numpy as np

SEED = 20240628
BOOT_B = 2000
Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float, float]:
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def cluster_bootstrap(clusters: list[tuple[int, int]], b: int = BOOT_B, seed: int = SEED,
                      alpha: float = 0.05) -> tuple[float, float, float]:
    """clusters: (successes, trials) per cluster (a task, or a (model, prompt) pair).
    Returns (p, lo, hi); with fewer than two clusters there is no interval (lo = hi = NaN)."""
    arr = np.array([c for c in clusters if c[1] > 0], dtype=float)
    if len(arr) == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = arr[:, 0].sum() / arr[:, 1].sum()
    if len(arr) < 2:
        return (float(p), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(b, len(arr)))
    k = arr[idx, 0].sum(axis=1)
    n = arr[idx, 1].sum(axis=1)
    est = k / n
    return (float(p), float(np.quantile(est, alpha / 2)), float(np.quantile(est, 1 - alpha / 2)))
