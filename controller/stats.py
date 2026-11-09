"""Deterministic statistics. No randomness anywhere in decision paths (constraint 4)."""
from __future__ import annotations

import math
from dataclasses import dataclass


def norm_sf(z: float) -> float:
    """P(Z >= z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def binom_sf(k: int, n: int, p: float) -> float:
    """Exact P(X >= k) for X ~ Binomial(n, p). Used for hallucination-flag spikes (small counts)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    log_c = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    for i in range(k, n + 1):
        term = math.exp(log_c + i * lp + (n - i) * lq)
        total += term
        if term < 1e-15 * max(total, 1e-300) and i > k + 5:
            break
        log_c += math.log(n - i) - math.log(i + 1) if i < n else 0.0
    return min(total, 1.0)


@dataclass
class Diff:
    """Stratified risk difference between arm A (challenger) and arm B (control)."""
    n_a: int
    n_b: int
    rate_a: float
    rate_b: float
    delta: float
    se: float
    z: float

    @property
    def rel(self) -> float:
        return self.delta / self.rate_b if self.rate_b > 0 else 0.0

    def ci(self, z: float = 1.645) -> tuple[float, float]:
        return self.delta - z * self.se, self.delta + z * self.se

    def as_dict(self) -> dict:
        lo, hi = self.ci()
        return {"n_challenger": self.n_a, "n_control": self.n_b, "rate_challenger": round(self.rate_a, 4),
                "rate_control": round(self.rate_b, 4), "delta": round(self.delta, 4),
                "rel_lift": round(self.rel, 4), "ci90": [round(lo, 4), round(hi, 4)], "z": round(self.z, 2)}


def stratified_diff(strata: list[tuple[int, int, int, int]]) -> Diff | None:
    """Cochran–Mantel–Haenszel style risk difference.

    `strata` holds (n_a, x_a, n_b, x_b) per stratum. A stratum is a set of calls that were randomised under the
    *same* allocation (same config version and cohort), so the comparison inside it is a clean A/B. Pooling
    across strata with weights n_a*n_b/(n_a+n_b) avoids Simpson's paradox when allocations change over time.
    """
    num = den = var = 0.0
    na = xa = nb = xb = 0
    for n1, x1, n0, x0 in strata:
        if n1 <= 0 or n0 <= 0:
            continue
        w = n1 * n0 / (n1 + n0)
        p1, p0 = x1 / n1, x0 / n0
        pp = (x1 + x0) / (n1 + n0)  # pooled under H0: stabler variance for small strata
        num += w * (p1 - p0)
        den += w
        var += w * w * pp * (1 - pp) * (1 / n1 + 1 / n0)
        na += n1; xa += x1; nb += n0; xb += x0  # noqa: E702
    if den <= 0 or na == 0 or nb == 0:
        return None
    delta = num / den
    se = math.sqrt(var) / den if var > 0 else 1e-9
    return Diff(na, nb, xa / na, xb / nb, delta, se, delta / se if se > 0 else 0.0)


def two_prop(n_a: int, x_a: int, n_b: int, x_b: int) -> Diff | None:
    return stratified_diff([(n_a, x_a, n_b, x_b)])


def shrink_rate(x: float, n: float, prior: float, k: float) -> float:
    """Empirical-Bayes shrinkage of x/n towards `prior` with pseudo-count k."""
    return (x + k * prior) / (n + k) if (n + k) > 0 else prior


def chi2_sf(x: float, k: int) -> float:
    """P(X >= x) for chi-square with k dof (Wilson–Hilferty normal approximation; accurate for k >= 3)."""
    if k <= 0:
        return 1.0
    z = ((x / k) ** (1 / 3) - (1 - 2 / (9 * k))) / math.sqrt(2 / (9 * k))
    return norm_sf(z)


def bonferroni_z(alpha: float, m: int) -> float:
    """One-sided z threshold for family-wise alpha over m tests."""
    p = alpha / max(m, 1)
    # inverse survival via bisection (deterministic, dependency-free)
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if norm_sf(mid) > p:
            lo = mid
        else:
            hi = mid
    return hi


def min_n_per_arm(p: float, rel_lift: float, z_alpha: float, z_power: float = 0.84) -> int:
    """Connected calls per arm to detect `rel_lift` on base rate p (equal arms)."""
    d = p * rel_lift
    if d <= 0:
        return 10 ** 9
    return math.ceil(2 * p * (1 - p) * ((z_alpha + z_power) / d) ** 2)
