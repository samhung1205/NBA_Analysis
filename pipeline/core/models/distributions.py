"""
預測分佈（Phase C.5E）：點預測 → 殘差分佈 → 整數比分的機率質量 → 任意盤口線的機率
------------------------------------------------------------
純函式庫，不碰 DB。

模型：Y（實際分差 / 總分 / 上半場分差 / 上半場總分，整數）= pred + ε，ε 的連續分佈 G 由「樣本外（walk-forward）殘差」估計。

  gaussian           ε ~ N(μ, σ²)
  empirical          ε = μ + σ·z，z 的分佈 = 標準化樣本外殘差的核平滑 ECDF（Silverman 帶寬，存成固定格點上的 CDF）
  *_scaled           σ 依情境改變：log σ = b0 + Σ b_j·x_j（高斯概似 + 岭懲罰，懲罰把係數收縮回全域 σ）

離散化（continuity correction）：P(Y = k) = G(k + 0.5 − pred) − G(k − 0.5 − pred)，k 為整數；
支撐範圍兩端吸收尾巴機率，總和恰為 1。
分差（全場）沒有平手（延長賽）：P(Y = 0) 以「條件於非平手」重新正規化（不把平手質量硬塞給 ±1）。
上半場分差可以平手（0 有質量）。

盤口線（任意實數）：
  above = P(Y > line)，push = P(Y = line)（只有整數線才可能 > 0），below = P(Y < line)，三者和為 1。
  四分之一線（例如 −2.25）不是整數 → push = 0；拆成兩個半注是 bookmaker adapter 的事（quarter_line_components 只回傳組成線）。
這裡**不**計算 edge / EV / 賠率；那是 Phase D。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

DIST_SCHEMA_VERSION = 1
Z_GRID = np.round(np.linspace(-12.0, 12.0, 2401), 6)          # 標準化格點（0.01 間距）
SUPPORT_HALF_WIDTH = {"margin": 80, "total": 100, "h1_margin": 55, "h1_total": 65}
NO_ZERO_TARGETS = {"margin"}                                   # 全場分差不可能 0（延長賽）
SCALE_CLIP = (0.5, 2.0)                                        # σ(x)/σ_global 的上下限（安全閥）


# ------------------------------------------------------------------ #
# 情境特徵（尺度模型）                                                    #
# ------------------------------------------------------------------ #

# 每個 target 的情境特徵在看任何評測結果之前就固定（C.5E 報告 §3）
SCALE_FEATURES = {
    "margin": ["abs_pred", "early", "inj_unknown", "playoffs"],
    "h1_margin": ["abs_pred", "early", "inj_unknown", "playoffs"],
    "total": ["pred_level", "early", "inj_unknown", "playoffs"],
    "h1_total": ["pred_level", "early", "inj_unknown", "playoffs"],
}
CONTINUOUS_FEATURES = {"abs_pred", "pred_level"}               # 這兩個以擬合集的平均 / 標準差標準化；其餘是 0~1


def context_features(pred: np.ndarray, min_gp: np.ndarray, inj_both_known: np.ndarray,
                     is_playoffs: np.ndarray) -> dict[str, np.ndarray]:
    """early：本季前 10 場的線性斜坡 (10 − min_gp)/10（第 1 場 = 1，第 10 場起 = 0）。"""
    pred = np.asarray(pred, float)
    return {"abs_pred": np.abs(pred), "pred_level": pred,
            "early": np.clip((10.0 - np.asarray(min_gp, float)) / 10.0, 0.0, 1.0),
            "inj_unknown": 1.0 - np.nan_to_num(np.asarray(inj_both_known, float), nan=0.0),
            "playoffs": np.nan_to_num(np.asarray(is_playoffs, float), nan=0.0)}


# ------------------------------------------------------------------ #
# 分佈                                                                 #
# ------------------------------------------------------------------ #

@dataclass
class FittedDistribution:
    target: str
    kind: str                      # "gaussian" | "empirical"
    mu: float                      # 位置（樣本外平均殘差 = 點預測偏誤）
    sigma: float                   # 全域尺度（樣本外殘差標準差）
    scale_model: dict[str, Any] | None = None    # {"features", "intercept", "coef", "center", "scale", "lambda"}
    z_cdf: np.ndarray | None = None              # empirical：Z_GRID 上的標準化 CDF
    bandwidth: float | None = None
    n_fit: int = 0
    fit_start_utc: str | None = None
    fit_end_utc: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.kind + ("_scaled" if self.scale_model else "")

    # ---- 尺度 ---- #
    def scales(self, ctx: dict[str, np.ndarray] | None, n: int) -> np.ndarray:
        if not self.scale_model:
            return np.full(n, self.sigma)
        sm = self.scale_model
        logs = np.full(n, sm["intercept"], dtype=float)
        for f in sm["features"]:
            x = np.asarray(ctx[f], float)
            logs = logs + sm["coef"][f] * (x - sm["center"][f]) / sm["scale"][f]
        s = np.exp(logs)
        return np.clip(s, SCALE_CLIP[0] * self.sigma, SCALE_CLIP[1] * self.sigma)

    # ---- 殘差 CDF ---- #
    def resid_cdf(self, x: np.ndarray, scale: np.ndarray) -> np.ndarray:
        z = (np.asarray(x, float) - self.mu) / scale
        if self.kind == "gaussian":
            return ndtr(z)
        return np.interp(z, Z_GRID, self.z_cdf, left=0.0, right=1.0)

    # ---- 序列化 ---- #
    def to_state(self) -> dict[str, Any]:
        return {"schema": DIST_SCHEMA_VERSION, "target": self.target, "kind": self.kind, "mu": float(self.mu),
                "sigma": float(self.sigma), "scale_model": self.scale_model,
                "z_cdf": None if self.z_cdf is None else [float(v) for v in self.z_cdf],
                "z_grid": None if self.z_cdf is None else {"lo": float(Z_GRID[0]), "hi": float(Z_GRID[-1]),
                                                           "n": int(len(Z_GRID))},
                "bandwidth": self.bandwidth, "n_fit": int(self.n_fit), "fit_start_utc": self.fit_start_utc,
                "fit_end_utc": self.fit_end_utc, "meta": self.meta}

    @classmethod
    def from_state(cls, st: dict[str, Any]) -> "FittedDistribution":
        if st.get("schema") != DIST_SCHEMA_VERSION:
            raise ValueError(f"distribution schema {st.get('schema')} ≠ {DIST_SCHEMA_VERSION}")
        if st["kind"] not in ("gaussian", "empirical"):
            raise ValueError(f"unknown distribution kind {st['kind']!r}")
        z = None
        if st["kind"] == "empirical":
            g = st.get("z_grid") or {}
            if (g.get("lo"), g.get("hi"), g.get("n")) != (float(Z_GRID[0]), float(Z_GRID[-1]), len(Z_GRID)):
                raise ValueError("empirical 分佈的標準化格點與目前程式不同")
            z = np.asarray(st["z_cdf"], float)
            if len(z) != len(Z_GRID) or np.any(np.diff(z) < -1e-12) or not (0 <= z[0] <= z[-1] <= 1):
                raise ValueError("empirical CDF 不合法（長度 / 單調性）")
        if st["scale_model"] is not None and set(st["scale_model"]["features"]) - set(SCALE_FEATURES[st["target"]]):
            raise ValueError("尺度模型的情境特徵與目前程式不同")
        return cls(st["target"], st["kind"], float(st["mu"]), float(st["sigma"]), st["scale_model"], z,
                   st.get("bandwidth"), int(st["n_fit"]), st.get("fit_start_utc"), st.get("fit_end_utc"),
                   st.get("meta") or {})


# ------------------------------------------------------------------ #
# 擬合                                                                 #
# ------------------------------------------------------------------ #

def silverman_bandwidth(z: np.ndarray) -> float:
    z = np.asarray(z, float)
    iqr = np.subtract(*np.percentile(z, [75, 25]))
    s = min(np.std(z, ddof=1), iqr / 1.349) if iqr > 0 else np.std(z, ddof=1)
    return float(0.9 * s * len(z) ** (-0.2))


def kde_cdf_on_grid(z: np.ndarray, h: float, grid: np.ndarray = Z_GRID, chunk: int = 2000) -> np.ndarray:
    z = np.sort(np.asarray(z, float))
    out = np.zeros(len(grid))
    for i in range(0, len(z), chunk):
        out += ndtr((grid[:, None] - z[None, i:i + chunk]) / h).sum(axis=1)
    out /= len(z)
    out[0], out[-1] = max(out[0], 0.0), min(out[-1], 1.0)
    return np.maximum.accumulate(np.clip(out, 0.0, 1.0))


def fit_scale_model(resid: np.ndarray, ctx: dict[str, np.ndarray], features: list[str], lam: float,
                    mu: float) -> dict[str, Any]:
    """log σ_i = b0 + Σ b_j·x̃_ij；以高斯 NLL + (lam/2)·Σ b_j² 擬合（b0 不懲罰）。
    連續特徵標準化（擬合集平均/標準差）；0~1 特徵不標準化，lam 的單位約等於「虛擬觀測數」：
    出現次數遠少於 lam 的情境（例如傷病未知）係數會被收縮到接近 0（= 回到全域 σ）。"""
    r2 = (np.asarray(resid, float) - mu) ** 2
    cols, center, scale = [], {}, {}
    for f in features:
        x = np.asarray(ctx[f], float)
        if f in CONTINUOUS_FEATURES:
            c, s = float(x.mean()), float(x.std()) or 1.0
        else:
            c, s = 0.0, 1.0
        center[f], scale[f] = c, s
        cols.append((x - c) / s)
    X = np.column_stack(cols) if cols else np.zeros((len(r2), 0))
    b0_init = 0.5 * math.log(r2.mean())

    def f(theta):
        b0, b = theta[0], theta[1:]
        eta = b0 + X @ b
        inv = np.exp(-2 * eta)
        val = np.sum(eta + 0.5 * r2 * inv) + 0.5 * lam * np.sum(b ** 2)
        g_eta = 1.0 - r2 * inv
        grad = np.concatenate([[g_eta.sum()], X.T @ g_eta + lam * b])
        return val, grad

    res = minimize(f, np.concatenate([[b0_init], np.zeros(X.shape[1])]), jac=True, method="L-BFGS-B",
                   options={"maxiter": 500, "gtol": 1e-9})
    theta = res.x
    return {"features": list(features), "intercept": float(theta[0]),
            "coef": {f: float(v) for f, v in zip(features, theta[1:])}, "center": center, "scale": scale,
            "lambda": float(lam), "converged": bool(res.success)}


def fit_distribution(target: str, resid: np.ndarray, *, kind: str, scaled: bool = False,
                     ctx: dict[str, np.ndarray] | None = None, lam: float = 200.0,
                     times: Iterable | None = None, fix_mu: float | None = None) -> FittedDistribution:
    """resid 必須是**樣本外**殘差（實際 − walk-forward 預測）。fix_mu：只供敏感度分析（固定位置參數）。"""
    resid = np.asarray(resid, float)
    if len(resid) < 50:
        raise ValueError(f"{target}：樣本外殘差太少（{len(resid)}）")
    mu = float(resid.mean()) if fix_mu is None else float(fix_mu)
    sigma = float(np.sqrt(((resid - mu) ** 2).sum() / (len(resid) - 1))) if fix_mu is not None else float(resid.std(ddof=1))
    sm = fit_scale_model(resid, ctx, SCALE_FEATURES[target], lam, mu) if scaled else None
    d = FittedDistribution(target, kind, mu, sigma, sm, n_fit=len(resid))
    if times is not None:
        ts = sorted(str(t) for t in times)
        d.fit_start_utc, d.fit_end_utc = ts[0], ts[-1]
    if kind == "empirical":
        s = d.scales(ctx, len(resid)) if scaled else np.full(len(resid), sigma)
        z = (resid - mu) / s
        h = silverman_bandwidth(z)
        d.z_cdf, d.bandwidth = kde_cdf_on_grid(z, h), h
    elif kind != "gaussian":
        raise ValueError(kind)
    if scaled:
        # 尺度模型的 σ 中位數相對全域（報告用）
        d.meta["scale_ratio_q"] = [float(v) for v in np.quantile(d.scales(ctx, len(resid)) / sigma, [.05, .5, .95])]
    return d


# ------------------------------------------------------------------ #
# 整數結果的機率質量                                                     #
# ------------------------------------------------------------------ #

def outcome_pmf(dist: FittedDistribution, pred: np.ndarray, ctx: dict[str, np.ndarray] | None = None
                ) -> tuple[np.ndarray, np.ndarray]:
    """回傳 (support[n, K] 整數, pmf[n, K])；每列總和 = 1。"""
    pred = np.atleast_1d(np.asarray(pred, float))
    n = len(pred)
    R = SUPPORT_HALF_WIDTH[dist.target]
    base = np.round(pred).astype(int)
    support = base[:, None] + np.arange(-R, R + 1)[None, :]
    scale = dist.scales(ctx, n)[:, None]
    upper = dist.resid_cdf(support + 0.5 - pred[:, None], scale)
    upper[:, -1] = 1.0
    lower = np.concatenate([np.zeros((n, 1)), upper[:, :-1]], axis=1)
    pmf = np.clip(upper - lower, 0.0, None)
    if dist.target in NO_ZERO_TARGETS:
        pmf = np.where(support == 0, 0.0, pmf)
    pmf = pmf / pmf.sum(axis=1, keepdims=True)
    return support, pmf


@dataclass(frozen=True)
class LineProbability:
    target: str
    line: float
    probability_above: float
    probability_push: float
    probability_below: float
    center: float                 # pred + μ（分佈中心）
    scale: float
    distribution: str


def line_probabilities(dist: FittedDistribution, pred: float, lines: Iterable[float],
                       ctx: dict[str, Any] | None = None) -> list[LineProbability]:
    """單場、多條線。決定性：同輸入 → 逐位元相同。"""
    c = None if ctx is None else {k: np.atleast_1d(np.asarray(v, float)) for k, v in ctx.items()}
    support, pmf = outcome_pmf(dist, np.array([pred], float), c)
    s, p = support[0], pmf[0]
    cum = np.cumsum(p)
    scale = float(dist.scales(c, 1)[0])
    out = []
    for L in lines:
        L = float(L)
        idx_le = np.searchsorted(s, math.floor(L), side="right") - 1        # 最後一個 k ≤ floor(L)
        below_le = float(cum[idx_le]) if idx_le >= 0 else 0.0
        push = 0.0
        if float(L).is_integer():
            j = int(L) - int(s[0])
            if 0 <= j < len(p):
                push = float(p[j])
        below = below_le - push
        above = 1.0 - below_le
        out.append(LineProbability(dist.target, L, max(above, 0.0), push, max(below, 0.0), float(pred + dist.mu),
                                   scale, dist.name))
    return out


def quarter_line_components(line: float) -> tuple[float, ...]:
    """四分之一線 → 兩條組成線（例如 −2.25 → (−2.0, −2.5)）；整數 / 半分線回傳自身。只做數學拆解，不含任何結算規則。"""
    frac = abs(line) % 1.0
    if math.isclose(frac, 0.25) or math.isclose(frac, 0.75):
        return (line - 0.25, line + 0.25)
    return (line,)


def central_interval(dist: FittedDistribution, pred: float, level: float, ctx: dict[str, Any] | None = None
                     ) -> tuple[int, int]:
    """整數結果的中央預測區間 [lo, hi]：lo = 最小 k 使 F(k) ≥ (1−level)/2，hi = 最小 k 使 F(k) ≥ (1+level)/2。"""
    c = None if ctx is None else {k: np.atleast_1d(np.asarray(v, float)) for k, v in ctx.items()}
    s, p = outcome_pmf(dist, np.array([pred]), c)
    cum = np.cumsum(p[0])
    lo = int(s[0][np.searchsorted(cum, (1 - level) / 2)])
    hi = int(s[0][min(np.searchsorted(cum, (1 + level) / 2), len(cum) - 1)])
    return lo, hi


# ------------------------------------------------------------------ #
# 評估                                                                 #
# ------------------------------------------------------------------ #

def score_outcomes(support: np.ndarray, pmf: np.ndarray, y: np.ndarray, *, seed: int = 11) -> dict[str, np.ndarray]:
    """逐場：RPS（離散 CRPS）、NLL（離散對數機率）、隨機化 PIT。"""
    y = np.asarray(y, float)
    n, K = pmf.shape
    cdf = np.cumsum(pmf, axis=1)
    ind = (support >= y[:, None]).astype(float)               # 1[y ≤ k]
    rps = ((cdf - ind) ** 2).sum(axis=1)
    j = np.clip((y - support[:, 0]).astype(int), 0, K - 1)
    py = pmf[np.arange(n), j]
    inside = (y >= support[:, 0]) & (y <= support[:, -1])
    py = np.where(inside, py, 0.0)
    nll = -np.log(np.maximum(py, 1e-12))
    f_below = np.where(j > 0, cdf[np.arange(n), np.maximum(j - 1, 0)], 0.0)
    u = np.random.default_rng(seed).random(n)
    pit = f_below + u * py
    return {"rps": rps, "nll": nll, "pit": pit}


COVERAGE_LEVELS = (0.5, 0.8, 0.9, 0.95)


def coverage(pit: np.ndarray, levels: Iterable[float] = COVERAGE_LEVELS) -> dict[str, float]:
    """中央預測區間的實際覆蓋率（以隨機化 PIT 計算，對離散結果是正確的檢定量）。"""
    pit = np.asarray(pit, float)
    return {f"{int(round(c * 100))}%": float(((pit >= (1 - c) / 2) & (pit <= (1 + c) / 2)).mean()) for c in levels}


def pit_histogram(pit: np.ndarray, bins: int = 10) -> dict[str, Any]:
    h, _ = np.histogram(np.asarray(pit, float), bins=bins, range=(0, 1))
    freq = h / h.sum()
    return {"freq": [float(v) for v in freq], "max_abs_dev": float(np.abs(freq - 1 / bins).max()),
            "chi2": float(((h - h.sum() / bins) ** 2 / (h.sum() / bins)).sum())}


def synthetic_lines(pred: np.ndarray, offsets: Iterable[float]) -> np.ndarray:
    """半分線：round(pred) + 0.5 + d（d 為整數 offset）→ 不會有 push。回傳 [n, len(offsets)]。"""
    base = np.floor(np.asarray(pred, float)) + 0.5
    return base[:, None] + np.asarray(list(offsets), float)[None, :]


def probability_above_matrix(support: np.ndarray, pmf: np.ndarray, lines: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """每場多條線的 (P(Y > L), P(Y = L))；lines[n, m]。"""
    cdf = np.cumsum(pmf, axis=1)
    n, m = lines.shape
    above = np.empty((n, m))
    push = np.zeros((n, m))
    for j in range(m):
        L = lines[:, j]
        k = np.floor(L).astype(int)
        idx = np.clip(k - support[:, 0], -1, support.shape[1] - 1)
        le = np.where(idx >= 0, cdf[np.arange(n), np.maximum(idx, 0)], 0.0)
        is_int = np.isclose(L, np.round(L))
        pk = np.where((idx >= 0) & is_int, pmf[np.arange(n), np.maximum(idx, 0)], 0.0)
        above[:, j] = 1.0 - le
        push[:, j] = pk
    return above, push


def reliability(p: np.ndarray, outcome: np.ndarray, edges: Iterable[float] | None = None) -> dict[str, Any]:
    p, o = np.asarray(p, float).ravel(), np.asarray(outcome, float).ravel()
    edges = np.asarray(list(edges) if edges is not None else np.linspace(0, 1, 21))
    idx = np.clip(np.digitize(p, edges) - 1, 0, len(edges) - 2)
    rows, ece = [], 0.0
    for b in range(len(edges) - 1):
        m = idx == b
        if not m.any():
            continue
        gap = float(p[m].mean() - o[m].mean())
        ece += m.mean() * abs(gap)
        se = math.sqrt(max(o[m].mean() * (1 - o[m].mean()), 1e-9) / m.sum())
        rows.append({"bin": f"{edges[b]:.2f}-{edges[b + 1]:.2f}", "n": int(m.sum()), "mean_p": float(p[m].mean()),
                     "observed": float(o[m].mean()), "gap": gap, "se": se})
    return {"ece": float(ece), "brier": float(((p - o) ** 2).mean()), "bins": rows}
