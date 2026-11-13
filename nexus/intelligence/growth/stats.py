"""Small, dependency-light statistics for growth experiments (numpy + math only).

Every comparison returns the effect size with a confidence interval and a verdict in
{"winner", "no_difference_detected", "inconclusive", "insufficient_data"}. "inconclusive" is a normal result,
not an error. A winner needs (a) every arm at or above its minimum sample, (b) a corrected p-value below alpha and
(c) a confidence interval that excludes zero. Nothing here predicts future views.
"""
from __future__ import annotations

import math

import numpy as np

ALPHA = 0.05
POWER = 0.8


# ---------------------------------------------------------------- distributions
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |error| < 1.2e-9 after one Newton step)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    elif p > 1 - lo:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    else:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    e = norm_cdf(x) - p
    return x - e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def t_sf2(t: float, df: float) -> float:
    """Two-sided p-value of Student's t."""
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def t_ppf(p: float, df: float) -> float:
    """Quantile of Student's t by bisection on t_sf2 (p > 0.5)."""
    target = 2 * (1 - p)
    lo, hi = 0.0, 1e3
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_sf2(mid, df) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- power / sample size
def n_two_proportions(p1: float, p2: float, alpha: float = ALPHA, power: float = POWER) -> int:
    """Trials per arm to detect p1 vs p2 with a two-sided two-proportion z-test."""
    if p1 == p2:
        raise ValueError("p1 and p2 must differ")
    za, zb = norm_ppf(1 - alpha / 2), norm_ppf(power)
    pbar = (p1 + p2) / 2
    n = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / (p1 - p2) ** 2
    return int(math.ceil(n))


def n_two_means(d: float, alpha: float = ALPHA, power: float = POWER) -> int:
    """Units per arm to detect a standardised mean difference d (Cohen's d) with a two-sided test (normal approx. + 1)."""
    if d <= 0:
        raise ValueError("d must be > 0")
    za, zb = norm_ppf(1 - alpha / 2), norm_ppf(power)
    return int(math.ceil(2 * (za + zb) ** 2 / d ** 2)) + 1


# ---------------------------------------------------------------- tests
def two_proportions(x1: int, n1: int, x2: int, n2: int, alpha: float = ALPHA) -> dict:
    """B vs A: difference p2 - p1, Wald CI, pooled z-test p-value, relative lift."""
    if min(n1, n2) <= 0:
        return {"test": "two_proportion_z", "p_value": None, "effect": None, "ci": None, "n": [n1, n2]}
    p1, p2 = x1 / n1, x2 / n2
    pool = (x1 + x2) / (n1 + n2)
    se0 = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    z = (p2 - p1) / se0 if se0 > 0 else 0.0
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    zc = norm_ppf(1 - alpha / 2)
    diff = p2 - p1
    return {"test": "two_proportion_z", "rates": [round(p1, 6), round(p2, 6)], "effect": round(diff, 6),
            "ci": [round(diff - zc * se, 6), round(diff + zc * se, 6)], "p_value": round(2 * (1 - norm_cdf(abs(z))), 6),
            "relative_lift": round(diff / p1, 4) if p1 > 0 else None, "n": [n1, n2], "successes": [x1, x2]}


def welch(a, b, alpha: float = ALPHA) -> dict:
    """B vs A: mean difference, Welch-Satterthwaite CI and p-value, Hedges-free Cohen's d (pooled SD)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return {"test": "welch_t", "p_value": None, "effect": None, "ci": None, "n": [n1, n2]}
    m1, m2, v1, v2 = a.mean(), b.mean(), a.var(ddof=1), b.var(ddof=1)
    se2 = v1 / n1 + v2 / n2
    diff = float(m2 - m1)
    if se2 <= 0:
        return {"test": "welch_t", "means": [float(m1), float(m2)], "effect": diff, "ci": [diff, diff], "p_value": 1.0 if diff == 0 else 0.0,
                "n": [n1, n2], "cohens_d": None}
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    t = diff / math.sqrt(se2)
    tc = t_ppf(1 - alpha / 2, df)
    sp = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    return {"test": "welch_t", "means": [round(float(m1), 6), round(float(m2), 6)], "effect": round(diff, 6),
            "ci": [round(diff - tc * math.sqrt(se2), 6), round(diff + tc * math.sqrt(se2), 6)], "p_value": round(t_sf2(t, df), 6),
            "df": round(df, 2), "cohens_d": round(diff / sp, 4) if sp > 0 else None, "n": [n1, n2]}


def bootstrap_ci(a, b, stat: str = "median", n_boot: int = 4000, alpha: float = ALPHA, seed: int = 7) -> dict:
    """Percentile bootstrap CI of stat(b) - stat(a) (stat = mean | median). Deterministic for a given seed."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return {"test": f"bootstrap_{stat}", "effect": None, "ci": None, "n": [len(a), len(b)]}
    f = np.median if stat == "median" else np.mean
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, len(a), (n_boot, len(a)))
    ib = rng.integers(0, len(b), (n_boot, len(b)))
    d = f(b[ib], axis=1) - f(a[ia], axis=1)
    lo, hi = np.quantile(d, [alpha / 2, 1 - alpha / 2])
    p = 2 * min((d <= 0).mean(), (d >= 0).mean())
    return {"test": f"bootstrap_{stat}", "effect": round(float(f(b) - f(a)), 6), "ci": [round(float(lo), 6), round(float(hi), 6)],
            "p_value": round(float(min(1.0, p)), 6), "n": [len(a), len(b)], "n_boot": n_boot}


def holm(pvalues: list[float | None]) -> list[float | None]:
    """Holm-Bonferroni adjusted p-values (None stays None and does not count as a test)."""
    idx = sorted((p, i) for i, p in enumerate(pvalues) if p is not None)
    m = len(idx)
    out: list[float | None] = [None] * len(pvalues)
    run = 0.0
    for k, (p, i) in enumerate(idx):
        run = max(run, min(1.0, (m - k) * p))
        out[i] = round(run, 6)
    return out


def verdict(result: dict, n_min: int, n: list[int], alpha: float = ALPHA, p_adj: float | None = None) -> dict:
    """Attach a verdict; refuses to call a winner below the minimum sample in any arm."""
    r = dict(result)
    p = p_adj if p_adj is not None else r.get("p_value")
    r["p_adjusted"] = p
    r["min_samples"] = n_min
    if min(n) < n_min:
        r["verdict"] = "insufficient_data"
        r["why"] = f"smallest arm has {min(n)} samples; at least {n_min} per arm are required before any call"
    elif p is None or r.get("ci") is None:
        r["verdict"] = "inconclusive"
        r["why"] = "no usable test statistic"
    elif p < alpha and (r["ci"][0] > 0 or r["ci"][1] < 0):
        r["verdict"] = "winner"
        r["why"] = f"adjusted p={p:.4g} < {alpha} and the {int(100 * (1 - alpha))}% CI excludes 0"
    else:
        r["verdict"] = "inconclusive"
        r["why"] = f"adjusted p={p:.4g}; CI {r['ci']} includes 0 or p >= {alpha} - no reliable difference at this sample size"
    return r
