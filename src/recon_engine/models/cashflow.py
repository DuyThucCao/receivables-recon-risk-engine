"""Question 3 (Treasurer): will we be short of cash in the next 13 weeks?

Method, in the order a treasurer would explain it
------------------------------------------------
1. **Payment lag** ``T`` = days from claim submission to the payer's payment (the PAID event in
   the life-cycle feed, which is the payment run date). Claims still open at the forecast date
   are right-censored. Because some open claims will never be paid, each open claim enters the
   risk set with weight ``w = P(eventually PAID | state, time in state)`` from the Markov chain
   (markov.py); written-off claims get weight 0. The weighted Kaplan-Meier estimator

       S(t) = prod_{t_j <= t} (1 - d_j / n_j),   d_j = weighted payments at t_j, n_j = weighted risk set

   then estimates the lag distribution *of claims that will be paid*. Log-normal, gamma and
   Weibull distributions are fitted to the same censored, weighted data by maximum likelihood
   (log L = sum w_i [d_i log f(t_i) + (1 - d_i) log S(t_i)]) and compared by AIC and by the
   largest distance to the Kaplan-Meier curve.
2. **Simulation curve.** Kaplan-Meier where the weighted risk set has at least ``min_risk_set``
   claims; beyond that, the tail of the best parametric fit (conditional on surviving to the
   join point).
3. **Deposit lag** from payer payment date to bank posting: empirical distribution per payer
   from reconciled payments (recon.remit_bank_match).
4. **Monte Carlo of weekly receipts** for ``horizon_weeks`` weeks after the forecast date:
   * claims open at the forecast date: claim i is paid with probability p_i (Markov), at a time
     drawn from S conditional on T > age_i, plus a deposit lag; amount = billed x a paid-to-billed
     ratio resampled from recently paid claims of the same payer;
   * new claims: Poisson arrivals per payer at the rate of the trailing ``arrival_window_days``
     (given the count, arrival days are uniform: the order-statistics property of a Poisson
     process); each pays with P(PAID | SUBMITTED) after a lag drawn from the same curve;
   * remittances already received but not yet deposited (in transit) are counted in week 1.
5. **Shortfall.** Position_k = opening bank balance + cumulative receipts - k x weekly cash need.
   Shortfall = max(0, minimum balance - min_k Position_k). Reported: P(shortfall > 0), VaR95 and
   CVaR95 (mean of the worst 5%) of the shortfall, and cash-at-risk of 13-week receipts
   (mean minus 5th percentile).
6. **Backtest** at past forecast dates using only data available on each date (every input
   query is cut at the forecast date): empirical coverage of the 80% and 95% intervals.

All dollar outputs are model estimates on simulated data. The weekly cash need and minimum
balance are financial assumptions (see ``assumptions`` in the result).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import optimize, stats

from .. import FIGURES_DIR, SQL_DIR, labels
from . import markov
from .markov import ABSORBING, TRANSIENT, label_table, mpl, query_df, to_date

SQL_FILE = SQL_DIR / "models" / "41_cash_forecast.sql"

DEFAULTS = {
    "forecast_date": None,            # None = as-of date
    "horizon_weeks": 13,
    "n_sims": 2000,
    "lookback_days": 365,             # claims used for the lag curve and paid ratios
    "arrival_window_days": 91,        # trailing window for the new-claim arrival rate
    "min_risk_set": 30.0,
    "max_lag_days": 730,
    "backtest_start": "2024-09-02",     # first date with eight months of history
    "backtest_end": "2025-09-29",     # last date whose 13-week window ends before billing stops
    "backtest_step_days": 7,          # weekly origins
    "backtest_n_sims": 500,
    "backtest_lag_models": ["hybrid", "parametric"],
}

# Financial assumptions used when config/calibration.yaml does not define them (request R-CCC-2).
ASSUMPTION_DEFAULTS = {
    "cash_need_factor": {
        "value": 0.95, "label": labels.FINANCIAL_ASSUMPTION,
        "source": "Assumption (module default, requested for config/calibration.yaml in docs/requests.md R-CCC-2). "
                  "Weekly cash need = this factor x the median weekly payer receipts of the 26 weeks before the "
                  "forecast date. Reasoning: disbursements funded from the collection account (wholesaler invoices, "
                  "payroll share, other operating costs) are not in the simulated bank statement, so the need is "
                  "anchored to what the account normally collects, with a 5% operating margin. The result is a "
                  "financial_assumption, not a measurement; the shortfall curve shows the answer for factors 0.70-1.20."},
    "minimum_operating_balance_usd": {
        "value": 1_000_000.0, "label": labels.FINANCIAL_ASSUMPTION,
        "source": "Assumption (module default, requested in docs/requests.md R-CCC-2). Balance the treasurer "
                  "will not go below; a shortfall is the extra funding needed to stay above it."},
}

FAMILIES = ("lognormal", "gamma", "weibull")


# ------------------------------------------------------------------------------------------
# survival estimation
# ------------------------------------------------------------------------------------------
def weighted_km(t: np.ndarray, d: np.ndarray, w: np.ndarray | None = None) -> pd.DataFrame:
    """Weighted Kaplan-Meier estimator.

    ``t`` durations, ``d`` 1 = event (payment) observed, 0 = censored, ``w`` weights (default 1).
    Returns one row per distinct event time with the weighted risk set ``n``, weighted events
    ``d`` and the survival ``S`` just after that time. Ties: censored observations at an event
    time are still at risk at that time (the usual convention).
    """
    t = np.asarray(t, float)
    d = np.asarray(d, float)
    w = np.ones_like(t) if w is None else np.asarray(w, float)
    keep = w > 0
    t, d, w = t[keep], d[keep], w[keep]
    times = np.unique(t[d > 0])
    order = np.argsort(t)
    ts, ws = t[order], w[order]
    cum_w_from_right = np.concatenate([np.cumsum(ws[::-1])[::-1], [0.0]])
    idx = np.searchsorted(ts, times, side="left")
    n = cum_w_from_right[idx]
    ev = pd.Series(w * d).groupby(t).sum().reindex(times).to_numpy()
    S = np.cumprod(1.0 - ev / n)
    return pd.DataFrame({"t": times, "n": n, "d": ev, "S": S})


def km_on_grid(km: pd.DataFrame, max_days: int) -> tuple[np.ndarray, np.ndarray]:
    """Survival S(x) = P(T > x) and the weighted risk set at x for integer days x = 0..max_days."""
    grid = np.arange(max_days + 1)
    if len(km) == 0:
        return np.ones(max_days + 1), np.zeros(max_days + 1)
    k = np.searchsorted(km["t"].to_numpy(), grid, side="right")   # number of event times <= x
    S = np.concatenate([[1.0], km["S"].to_numpy()])[k]
    # risk set at x: n at the first event time >= x (approximation between event times)
    kk = np.minimum(np.searchsorted(km["t"].to_numpy(), grid, side="left"), len(km) - 1)
    n = km["n"].to_numpy()[kk]
    n[grid > km["t"].max()] = 0.0
    return S, n


def _dist(family: str, theta: np.ndarray):
    a, b = np.exp(theta)
    if family == "lognormal":           # theta = (log sigma, log median)
        return stats.lognorm(s=a, scale=b)
    if family == "gamma":               # theta = (log shape, log scale)
        return stats.gamma(a, scale=b)
    if family == "weibull":             # theta = (log shape, log scale)
        return stats.weibull_min(a, scale=b)
    raise ValueError(family)


def fit_parametric(t: np.ndarray, d: np.ndarray, w: np.ndarray, family: str) -> dict[str, Any]:
    """Weighted maximum likelihood with right censoring: sum w [d log f(t) + (1-d) log S(t)]."""
    t = np.maximum(np.asarray(t, float), 0.5)
    d = np.asarray(d, float)
    w = np.asarray(w, float)
    keep = w > 0
    t, d, w = t[keep], d[keep], w[keep]
    te = t[d > 0]
    m, s = np.log(te).mean(), max(np.log(te).std(), 0.1)
    if family == "lognormal":
        x0 = np.log([s, np.exp(m)])
    elif family == "gamma":
        mean, var = te.mean(), te.var()
        x0 = np.log([mean ** 2 / var, var / mean])
    else:
        x0 = np.log([1.2 / s, np.exp(m + 0.3)])

    def nll(theta):
        if np.any(np.abs(theta) > 15):
            return 1e12
        dist = _dist(family, theta)
        ll = d * dist.logpdf(t) + (1 - d) * dist.logsf(t)
        v = -(w * ll).sum()
        return v if np.isfinite(v) else 1e12

    res = optimize.minimize(nll, x0, method="Nelder-Mead", options={"xatol": 1e-6, "fatol": 1e-6, "maxiter": 4000})
    a, b = np.exp(res.x)
    names = {"lognormal": ("sigma", "median_days"), "gamma": ("shape", "scale_days"), "weibull": ("shape", "scale_days")}[family]
    dist = _dist(family, res.x)
    return {"family": family, "params": {names[0]: float(a), names[1]: float(b)}, "theta": res.x,
            "loglik": float(-res.fun), "aic": float(2 * 2 + 2 * res.fun), "converged": bool(res.success),
            "mean_days": float(dist.mean()), "median_days": float(dist.median())}


# ------------------------------------------------------------------------------------------
# inputs at a forecast date (all cut at the date)
# ------------------------------------------------------------------------------------------
def lag_data(claims: pd.DataFrame, chain: markov.ChainFit, f: date, lookback_days: int,
             ratios: dict | None = None) -> pd.DataFrame:
    """Durations, event flags and weights for the lag model (claims submitted in the lookback).

    ``w`` is the claim-count weight (1 for paid, P(eventually PAID) for open, 0 for written off).
    ``w_dollar`` multiplies it by the dollars at stake (paid amount for paid claims, billed x
    expected paid-to-billed ratio for open claims), so the curve describes when *dollars* arrive:
    large claims are denied more often and therefore paid later, which a count-weighted curve
    misses (the first backtest over-forecast receipts by more than half because of this).
    """
    F = pd.Timestamp(f)
    c = claims[claims["submission_date"] > F - pd.Timedelta(days=lookback_days)].copy()
    paid = c["first_paid_event_date"].notna()
    c["t"] = np.where(paid, (c["first_paid_event_date"] - c["submission_date"]).dt.days,
                      (F - c["submission_date"]).dt.days).astype(float)
    c["d"] = paid.astype(float)
    open_ = c["state"].isin(TRANSIENT) & ~paid
    days_in_state = (F - c["state_since"]).dt.days.clip(lower=0)
    w = np.where(paid, 1.0, 0.0)
    if open_.any():
        w[open_.to_numpy()] = chain.p_paid_open(c.loc[open_, "state"].tolist(), c.loc[open_, "payer_id"].tolist(),
                                                days_in_state[open_].tolist())
    c["w"] = w
    if ratios is not None:
        r = np.array([expected_ratio(ratios, p_, b) for p_, b in zip(c["payer_id"], c["billed"])])
        dollars = np.where(paid, np.maximum(c["paid_to_date"], 0.0), c["billed"] * r)
        # paid claims whose remittance was not matched carry no paid amount: use the expectation
        dollars = np.where(paid & (c["paid_to_date"] <= 0), c["billed"] * r, dollars)
        c["w_dollar"] = w * dollars
    else:
        c["w_dollar"] = w
    return c[["claim_id", "payer_id", "t", "d", "w", "w_dollar", "state", "billed"]]


def lag_models(ld: pd.DataFrame, prm: dict, weight: str = "w_dollar") -> dict[str, dict[str, Any]]:
    """Per payer: KM on a daily grid, parametric fits, and the hybrid simulation CDF.

    The KM and the fits use ``weight`` (dollar weights by default). The minimum risk set is
    checked on the claim-count weights so that "enough data" means enough claims.
    """
    M = int(prm["max_lag_days"])
    out = {}
    for payer, g in ld.groupby("payer_id"):
        ww = g[weight].to_numpy()
        scale = g["w"].sum() / max(ww.sum(), 1e-12)          # rescale dollars to claim units (AIC comparable)
        km = weighted_km(g["t"], g["d"], ww * scale)
        S, _ = km_on_grid(km, M)
        _, n = km_on_grid(weighted_km(g["t"], g["d"], g["w"]), M)
        fits = {fam: fit_parametric(g["t"].to_numpy(), g["d"].to_numpy(), ww * scale, fam) for fam in FAMILIES}
        ok = n >= prm["min_risk_set"]
        tau = int(np.max(np.where(ok)[0])) if ok.any() else 0
        grid = np.arange(M + 1)
        for fam, ft in fits.items():
            Fp = _dist(fam, ft["theta"]).cdf(np.maximum(grid, 1e-9))
            ft["max_abs_diff_to_km"] = float(np.max(np.abs(Fp[: tau + 1] - (1 - S[: tau + 1]))))
        best = min(fits, key=lambda k: fits[k]["aic"])
        Fb = _dist(best, fits[best]["theta"]).cdf(np.maximum(grid, 1e-9))
        Fkm = 1 - S
        hybrid = Fkm.copy()
        if tau < M:
            tail = (Fb[tau + 1:] - Fb[tau]) / max(1 - Fb[tau], 1e-12)
            hybrid[tau + 1:] = Fkm[tau] + (1 - Fkm[tau]) * tail
        hybrid = np.maximum.accumulate(np.clip(hybrid, 0, 1))
        hybrid[-1] = 1.0
        param = np.maximum.accumulate(np.clip(Fb, 0, 1))
        param[-1] = 1.0
        out[payer] = {"km_S": S, "risk_set": n, "tau": tau, "fits": fits, "best": best,
                      "cdf": {"hybrid": hybrid, "parametric": param},
                      "n_claims": int(len(g)), "n_paid": int(g["d"].sum()),
                      "km_median": float(np.argmax(S <= 0.5)) if (S <= 0.5).any() else None}
    return out


def deposit_lag_pmfs(conn, f: date, lookback_days: int, max_lag: int = 21) -> dict[str, np.ndarray]:
    df = query_df(conn, "select * from mart.cash_deposit_lag_asof(%s, %s)", (f, lookback_days))
    out = {}
    pooled = np.bincount(df["lag_days"].clip(0, max_lag).astype(int), minlength=max_lag + 1).astype(float) if len(df) else np.zeros(max_lag + 1)
    if pooled.sum() == 0:
        pooled[1] = 1.0
    pooled /= pooled.sum()
    out["ALL"] = pooled
    for payer, g in df.groupby("payer_id"):
        h = np.bincount(g["lag_days"].clip(0, max_lag).astype(int), minlength=max_lag + 1).astype(float)
        out[payer] = h / h.sum() if h.sum() >= 20 else pooled
    return out


def weekly_probabilities(cdf: np.ndarray, age: int, dep_pmf: np.ndarray, horizon_days: int) -> np.ndarray:
    """P(cash lands in bank in week k | unpaid at ``age``, will be paid), k = 1..H/7.

    Payment day offset u >= 1 has pmf q(u) = [F(a+u) - F(a+u-1)] / (1 - F(a)); bank offset is
    u + L with L the deposit lag.
    """
    M = len(cdf) - 1
    a = min(int(age), M)
    s_a = 1.0 - cdf[a]
    if s_a <= 1e-12:
        return np.zeros(horizon_days // 7)
    idx = np.minimum(a + np.arange(0, horizon_days + 1), M)
    q = np.diff(cdf[idx]) / s_a                       # u = 1..H
    v = np.convolve(q, dep_pmf)[: horizon_days]       # bank offsets 1..H (index 0 -> offset 1)
    return v.reshape(-1, 7).sum(axis=1)


N_BANDS = 10


def paid_ratios(claims: pd.DataFrame, f: date, lookback_days: int) -> dict[str, dict[str, np.ndarray]]:
    """Paid-to-billed ratios of recently paid claims, by payer and billed-amount band.

    Bands are payer-specific deciles of billed amount, because the ratio falls with claim size
    (drug and procedure claims carry larger chargemaster mark-ups). Within a band, ratios are
    resampled with probability proportional to billed amount, so the expected amount is
    billed x (ratio of sums in the band), not billed x (mean of ratios), which overstated
    receipts from large claims by about 10% in the first backtest. Also returns the paid amounts
    themselves (used for new claims).
    """
    F = pd.Timestamp(f)
    c = claims[(claims["first_paid_event_date"] > F - pd.Timedelta(days=lookback_days)) & (claims["paid_to_date"] > 0)
               & (claims["billed"] > 0)]
    out = {}
    for payer, g in list(c.groupby("payer_id")) + [("ALL", c)]:
        r = (g["paid_to_date"] / g["billed"]).clip(0, 1.5).to_numpy()
        b = g["billed"].to_numpy()
        edges = np.quantile(b, np.linspace(0, 1, N_BANDS + 1)[1:-1]) if len(b) >= 10 * N_BANDS else np.array([])
        band = np.searchsorted(edges, b, side="right")
        pools = []
        for k in range(len(edges) + 1):
            m = band == k
            pools.append((r[m], b[m] / b[m].sum()) if m.any() else (np.array([]), np.array([])))
        out[payer] = {"edges": edges, "by_band": pools,
                      "paid_amounts": g["paid_to_date"].to_numpy(), "ratio_of_sums": float(g["paid_to_date"].sum() / g["billed"].sum())}
    return out


def _ratio_pool(ratios: dict, payer: str, billed: float) -> tuple[np.ndarray, np.ndarray]:
    """(ratios, resampling probabilities) for a claim of this payer and billed amount."""
    rp = ratios.get(payer, ratios["ALL"])
    k = int(np.searchsorted(rp["edges"], billed, side="right"))
    pool = rp["by_band"][k]
    return pool if len(pool[0]) else ratios["ALL"]["by_band"][0]


def expected_ratio(ratios: dict, payer: str, billed: float) -> float:
    r, w = _ratio_pool(ratios, payer, billed)
    return float((r * w).sum())


# ------------------------------------------------------------------------------------------
# forecast
# ------------------------------------------------------------------------------------------
def assumptions(cfg: dict) -> dict[str, dict]:
    fa = (cfg or {}).get("financial_assumptions", {})
    return {k: (fa[k] if k in fa else v) for k, v in ASSUMPTION_DEFAULTS.items()}


def forecast(conn, f: Any, cfg: dict, prm: dict | None = None, *, n_sims: int | None = None, seed: int = 0,
             lag_model: str = "hybrid", keep_inputs: bool = False) -> dict[str, Any]:
    """Distribution of weekly payer receipts for the weeks after ``f``, using only data dated <= f."""
    p = dict(DEFAULTS)
    p.update(prm or {})
    f = to_date(f)
    H = int(p["horizon_weeks"]) * 7
    n_sims = int(n_sims or p["n_sims"])
    rng = np.random.default_rng(seed)
    claims = markov.claims_asof(conn, f)
    chain = markov.fit_from_events(markov.events_asof(conn, f), f, maturity_days=180)
    ratios = paid_ratios(claims, f, p["lookback_days"])
    ld = lag_data(claims, chain, f, p["lookback_days"], ratios)
    lags = lag_models(ld, p)
    dep = deposit_lag_pmfs(conn, f, p["lookback_days"])
    F = pd.Timestamp(f)
    K = H // 7
    sims = np.zeros((n_sims, K))
    exp_open, exp_new = np.zeros(K), np.zeros(K)

    def lag_for(payer):
        return lags[payer] if payer in lags else lags[max(lags, key=lambda k: lags[k]["n_claims"])]

    # ---- parameter uncertainty in the average claim value: one bootstrap level factor per
    # payer and simulation (mean of a resample of the pool divided by the pool mean)
    def level_factors(values: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
        n = len(values)
        if n < 2:
            return np.ones(n_sims)
        idx = rng.integers(0, n, size=(n_sims, n)) if n * n_sims <= 2e7 else rng.integers(0, n, size=(n_sims, min(n, 4000)))
        if weights is None:
            return values[idx].mean(axis=1) / values.mean()
        return (values[idx] * weights[idx]).sum(axis=1) / weights[idx].sum(axis=1) / ((values * weights).sum() / weights.sum())

    # ---- claims open at f
    op = claims[claims["state"].isin(TRANSIENT) & claims["first_paid_event_date"].isna()].copy()
    op["age"] = (F - op["submission_date"]).dt.days.clip(lower=0)
    op["days_in_state"] = (F - op["state_since"]).dt.days.clip(lower=0)
    op["p_paid"] = chain.p_paid_open(op["state"].tolist(), op["payer_id"].tolist(), op["days_in_state"].tolist())
    for payer, g in op.groupby("payer_id"):
        L = lag_for(payer)
        cdf = L["cdf"][lag_model]
        dp = dep.get(payer, dep["ALL"])
        W = np.array([weekly_probabilities(cdf, a, dp, H) for a in g["age"]]) * g["p_paid"].to_numpy()[:, None]
        cum = np.cumsum(W, axis=1)
        billed = g["billed"].to_numpy()
        rp = ratios.get(payer, ratios["ALL"])
        band = np.searchsorted(rp["edges"], billed, side="right")
        pools = [_ratio_pool(ratios, payer, b) for b in billed]
        exp_open += (W * (billed * np.array([(r_ * w_).sum() for r_, w_ in pools]))[:, None]).sum(axis=0)
        lev = level_factors(np.concatenate([pl[0] for pl in rp["by_band"]]))
        for s0 in range(0, n_sims, 250):
            s1 = min(s0 + 250, n_sims)
            ns = s1 - s0
            u = rng.random((ns, len(g)))
            wk = (u[:, :, None] > cum[None, :, :]).sum(axis=2)          # 0..K-1 = week, K = later/never
            R = np.empty((ns, len(g)))
            for k in np.unique(band):
                cols = np.where(band == k)[0]
                r_, w_ = _ratio_pool(ratios, payer, billed[cols[0]])
                j = np.searchsorted(np.cumsum(w_), rng.random((ns, len(cols))) * w_.sum(), side="right")
                R[:, cols] = r_[np.minimum(j, len(r_) - 1)]
            amt = billed[None, :] * R * lev[s0:s1, None]
            hit = wk < K
            rows = np.repeat(np.arange(ns)[:, None], len(g), axis=1)
            np.add.at(sims[s0:s1], (rows[hit], wk[hit]), amt[hit])

    # ---- new claims (Poisson arrivals at the trailing rate)
    win = claims[claims["submission_date"] > F - pd.Timedelta(days=p["arrival_window_days"])]
    arrival = {}
    for payer, g in win.groupby("payer_id"):
        lam = len(g) / p["arrival_window_days"]
        arrival[payer] = lam
        L = lag_for(payer)
        cdf = L["cdf"][lag_model]
        dp = dep.get(payer, dep["ALL"])
        paid_amts = ratios.get(payer, ratios["ALL"])["paid_amounts"]
        p0 = chain.p_paid("SUBMITTED", payer)
        counts = rng.poisson(lam * H, size=n_sims)
        tot = counts.sum()
        sim_id = np.repeat(np.arange(n_sims), counts)
        day = rng.integers(1, H + 1, size=tot)
        paid = rng.random(tot) < p0
        T = np.searchsorted(cdf, rng.random(tot), side="left")
        Ld = rng.choice(len(dp), size=tot, p=dp)
        off = day + T + Ld
        lev = level_factors(paid_amts)
        amt = rng.choice(paid_amts, size=tot) * lev[sim_id]   # amount given paid: resampled recent paid claims
        ok = paid & (off <= H)
        np.add.at(sims, (sim_id[ok], (off[ok] - 1) // 7), amt[ok])
        # expected value by week (analytic): sum over arrival days of P(bank offset in week k)
        pm = np.diff(np.concatenate([[0.0], cdf]))[: H + 1]
        bank = np.convolve(pm, dp)[: H + 1]           # pmf of T + L, offsets 0..H
        e = np.zeros(H + 1)
        for dd in range(1, H + 1):
            e[dd:] += bank[: H + 1 - dd]
        e_week = e[1:].reshape(-1, 7).sum(axis=1) * lam * p0 * paid_amts.mean()
        exp_new += e_week

    # ---- in transit (remitted, not yet deposited): week 1
    it = query_df(conn, "select * from mart.cash_in_transit_asof(%s)", (f,))
    in_transit = float(it["amount"].astype(float).sum()) if len(it) else 0.0
    sims[:, 0] += in_transit
    exp_transit = np.zeros(K)
    exp_transit[0] = in_transit

    out = {"forecast_date": f, "lag_model": lag_model, "n_sims": n_sims, "seed": seed, "sims": sims,
           "expected": {"open_claims": exp_open, "new_claims": exp_new, "in_transit": exp_transit},
           "n_open_claims": int(len(op)), "open_billed": float(op["billed"].sum()),
           "open_expected_paid": float(sum(b * pp * expected_ratio(ratios, py, b) for b, pp, py in zip(op["billed"], op["p_paid"], op["payer_id"]))),
           "arrival_rate_per_day": arrival, "in_transit_traces": int(len(it))}
    if keep_inputs:
        out.update({"lags": lags, "deposit_pmf": dep, "chain": chain, "claims": claims, "lag_data": ld, "ratios": ratios})
    return out


def actual_weekly(conn, f: Any, weeks: int) -> np.ndarray:
    f = to_date(f)
    df = query_df(conn, "select * from mart.cash_payer_receipts(%s, %s)", (f + timedelta(days=1), f + timedelta(days=7 * weeks)))
    a = np.zeros(weeks)
    for d, amt in zip(df["posting_date"], df["amount"]):
        a[((d - f).days - 1) // 7] += float(amt)
    return a


def quantiles(sims: np.ndarray) -> dict[str, np.ndarray]:
    q = np.percentile(sims, [2.5, 5, 10, 50, 90, 95, 97.5], axis=0)
    return {"p02_5": q[0], "p05": q[1], "p10": q[2], "p50": q[3], "p90": q[4], "p95": q[5], "p97_5": q[6],
            "mean": sims.mean(axis=0)}


def shortfall_metrics(sims: np.ndarray, opening: float, need: float, min_balance: float) -> dict[str, Any]:
    """Funding shortfall over the horizon (see module docstring)."""
    K = sims.shape[1]
    pos = opening + np.cumsum(sims, axis=1) - need * np.arange(1, K + 1)
    worst = pos.min(axis=1)
    short = np.maximum(0.0, min_balance - worst)
    var95 = float(np.percentile(short, 95))
    tail = short[short >= var95]
    total = sims.sum(axis=1)
    return {"p_shortfall": float((short > 0).mean()), "expected_shortfall_mean": float(short.mean()),
            "shortfall_var95": var95, "shortfall_cvar95": float(tail.mean()) if len(tail) else 0.0,
            "receipts_13w_mean": float(total.mean()), "receipts_13w_p05": float(np.percentile(total, 5)),
            "cash_at_risk_95": float(total.mean() - np.percentile(total, 5)),
            "p_below_min_by_week": (pos < min_balance).mean(axis=0)}


def cash_need(conn, f: Any, factor: float, weeks: int = 26) -> tuple[float, float]:
    """(weekly cash need, median weekly receipts) from the ``weeks`` weeks ending on ``f``."""
    f = to_date(f)
    past = actual_weekly(conn, f - timedelta(days=7 * weeks), weeks)
    med = float(np.median(past))
    return factor * med, med


def shortfall_curve(sims: np.ndarray, opening: float, median_receipts: float, min_balance: float,
                    factors=tuple(np.round(np.arange(0.70, 1.2001, 0.05), 2))) -> list[dict]:
    """P(shortfall), mean shortfall and CVaR95 as the weekly cash need varies (factor x median receipts)."""
    out = []
    for fac in factors:
        m = shortfall_metrics(sims, opening, fac * median_receipts, min_balance)
        out.append({"need_factor": float(fac), "weekly_cash_need": round(fac * median_receipts, 2),
                    "p_shortfall": round(m["p_shortfall"], 4), "expected_shortfall_mean": round(m["expected_shortfall_mean"], 2),
                    "shortfall_var95": round(m["shortfall_var95"], 2), "shortfall_cvar95": round(m["shortfall_cvar95"], 2)})
    return out


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    """Wilson score 95% interval for a binomial proportion k/n."""
    if n == 0:
        return [float("nan"), float("nan")]
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(float(c - h), 3), round(float(c + h), 3)]


# ------------------------------------------------------------------------------------------
# backtest
# ------------------------------------------------------------------------------------------
def backtest(conn, cfg: dict, prm: dict, seed: int) -> dict[str, Any]:
    dates = []
    d = to_date(prm["backtest_start"])
    while d <= to_date(prm["backtest_end"]):
        dates.append(d)
        d += timedelta(days=int(prm["backtest_step_days"]))
    K = int(prm["horizon_weeks"])
    rows, totals = [], []
    for lm in prm["backtest_lag_models"]:
        for i, f in enumerate(dates):
            fc = forecast(conn, f, cfg, prm, n_sims=prm["backtest_n_sims"], seed=seed + i, lag_model=lm)
            q = quantiles(fc["sims"])
            act = actual_weekly(conn, f, K)
            for k in range(K):
                rows.append({"lag_model": lm, "forecast_date": f, "week_no": k + 1, "mean": q["mean"][k],
                             "p02_5": q["p02_5"][k], "p10": q["p10"][k], "p90": q["p90"][k], "p97_5": q["p97_5"][k],
                             "actual": act[k], "in_80": q["p10"][k] <= act[k] <= q["p90"][k],
                             "in_95": q["p02_5"][k] <= act[k] <= q["p97_5"][k]})
            tot = fc["sims"].sum(axis=1)
            t10, t90, t025, t975 = np.percentile(tot, [10, 90, 2.5, 97.5])
            totals.append({"lag_model": lm, "forecast_date": f, "mean": float(tot.mean()), "p02_5": float(t025),
                           "p10": float(t10), "p90": float(t90), "p97_5": float(t975), "actual": float(act.sum()),
                           "in_80": bool(t10 <= act.sum() <= t90), "in_95": bool(t025 <= act.sum() <= t975)})
    wk = pd.DataFrame(rows)
    tt = pd.DataFrame(totals)
    summary = {}

    def cov(x: pd.Series) -> dict:
        k, n = int(x.sum()), int(len(x))
        return {"coverage": round(k / n, 3) if n else None, "n": n, "wilson_95": wilson(k, n)}

    for lm, g in wk.groupby("lag_model"):
        gt = tt[tt["lag_model"] == lm].reset_index(drop=True)
        step = max(1, int(round(7 * K / int(prm["backtest_step_days"]))))
        nonov = gt.iloc[::step]                                  # origins whose 13-week windows do not overlap
        g1 = g[g["week_no"] == 1]                                # week-1 targets are distinct weeks
        summary[lm] = {
            "n_origins": int(gt.shape[0]), "origin_step_days": int(prm["backtest_step_days"]),
            "note": "Wilson intervals treat points as independent. Overlapping 13-week windows (and the 13 weeks of "
                    "one origin) are dependent, so those intervals are too narrow; the week-1 and non-overlapping "
                    "rows use distinct target periods.",
            "weekly_80": cov(g["in_80"]), "weekly_95": cov(g["in_95"]),
            "week1_80": cov(g1["in_80"]), "week1_95": cov(g1["in_95"]),
            "total_13w_80_all_origins": cov(gt["in_80"]), "total_13w_95_all_origins": cov(gt["in_95"]),
            "total_13w_80_non_overlapping": cov(nonov["in_80"]), "total_13w_95_non_overlapping": cov(nonov["in_95"]),
            "weekly_mape": round(float((np.abs(g["mean"] - g["actual"]) / g["actual"]).mean()), 3),
            "weekly_bias_mean_minus_actual": round(float((g["mean"] - g["actual"]).mean()), 2),
            "total_13w_mape": round(float((np.abs(gt["mean"] - gt["actual"]) / gt["actual"]).mean()), 3),
            "total_13w_bias_pct": round(float(((gt["mean"] - gt["actual"]) / gt["actual"]).mean()), 3),
            "weekly_coverage_80_by_horizon": {int(k): round(float(v), 3) for k, v in g.groupby("week_no")["in_80"].mean().items()},
        }
    return {"dates": [str(x) for x in dates], "weekly": wk, "totals": tt, "summary": summary}


# ------------------------------------------------------------------------------------------
# evaluation against planted parameters (called by tests / run script with the truth file)
# ------------------------------------------------------------------------------------------
def first_pass_lag_quantiles(conn, cutoff: Any, lookback_days: int = 10_000) -> dict[str, dict[str, float]]:
    """Weighted-KM quantiles of the first-pass payment lag (submission -> payment with no denial).

    First pass = never denied (pends allowed), the generator's definition. Events: such claims
    paid by the cutoff. Censored: claims still in SUBMITTED, IN_REVIEW or PENDED with no denial,
    weighted by the probability that they are paid without a denial, from the chain's first step
    given time in state (IN_REVIEW: P(next = PAID); PENDED: P(next = IN_REVIEW) x the IN_REVIEW
    share of PAID among adjudications).
    """
    from .markov import IDX
    cutoff = to_date(cutoff)
    claims = markov.claims_asof(conn, cutoff)
    chain = markov.fit_from_events(markov.events_asof(conn, cutoff), cutoff, maturity_days=180)
    F = pd.Timestamp(cutoff)
    c = claims[(~claims["ever_denied"]) & (claims["submission_date"] > F - pd.Timedelta(days=lookback_days))].copy()
    paid = c["state"].eq("PAID") & c["first_paid_event_date"].notna()
    open_ = c["state"].isin(["SUBMITTED", "IN_REVIEW", "PENDED"])
    c = c[paid | open_]
    paid = paid[c.index]
    t = np.where(paid, (c["first_paid_event_date"] - c["submission_date"]).dt.days, (F - c["submission_date"]).dt.days)
    w = np.ones(len(c))
    for k, (s_, g, d) in enumerate(zip(c["state"], c["payer_id"], (F - c["state_since"]).dt.days)):
        if paid.iloc[k]:
            continue
        d = float(max(d, 0))
        if s_ == "PENDED":
            P = chain.P[chain._g(g)]
            pay_share = P[IDX["IN_REVIEW"], IDX["PAID"]] / (P[IDX["IN_REVIEW"], IDX["PAID"]] + P[IDX["IN_REVIEW"], IDX["DENIED"]])
            w[k] = chain.first_step_given_duration("PENDED", g, d)[IDX["IN_REVIEW"]] * pay_share
        else:
            w[k] = chain.first_step_given_duration("IN_REVIEW", g, d if s_ == "IN_REVIEW" else 0.0)[IDX["PAID"]]
    out = {}
    for payer in sorted(c["payer_id"].unique()):
        m = (c["payer_id"] == payer).to_numpy()
        km = weighted_km(t[m], paid.to_numpy()[m].astype(float), w[m])
        S, _ = km_on_grid(km, 400)
        q = {}
        for name, lev in (("p25", 0.25), ("median", 0.5), ("p75", 0.75), ("p90", 0.9)):
            q[name] = float(np.argmax(1 - S >= lev))
        q["mean_restricted_400d"] = float(S.sum())
        out[payer] = q
    return out


def evaluate_against_planted(conn, planted: dict, cutoffs: tuple = ("2026-03-31", "2025-06-30")) -> dict[str, Any]:
    out = {}
    for cut in cutoffs:
        est = first_pass_lag_quantiles(conn, cut)
        per, worst = {}, 0.0
        for payer, q in est.items():
            tr = planted["payers"][payer]["realised_first_pass_payment_lag_days"]
            err = {k: round(q[k] - tr[k], 2) for k in ("p25", "median", "p75", "p90")}
            err["mean"] = round(q["mean_restricted_400d"] - tr["mean"], 2)
            per[payer] = {"estimated": {k: round(v, 2) for k, v in q.items()},
                          "planted_realised": {k: tr[k] for k in ("p25", "median", "p75", "p90", "mean")},
                          "error_days": err}
            worst = max(worst, max(abs(v) for k, v in err.items() if k in ("p25", "median", "p75")))
        out[str(cut)] = {"per_payer": per, "max_abs_error_quartiles_days": worst}
    return out


# ------------------------------------------------------------------------------------------
# marts, figures, run
# ------------------------------------------------------------------------------------------
def write_marts(conn, f: date, q: dict, fc: dict, need: float, p_below: np.ndarray, actual: np.ndarray | None,
                bt: dict, lags: dict) -> None:
    K = len(q["mean"])
    conn.execute("delete from mart.cash_forecast where forecast_date = %s", (f,))
    conn.execute("delete from mart.cash_forecast_lag where forecast_date = %s", (f,))
    conn.execute("truncate mart.cash_forecast_backtest")
    with conn.cursor() as cur:
        cur.executemany(
            "insert into mart.cash_forecast values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'model_estimate')",
            [(f, k + 1, f + timedelta(days=7 * k + 1), f + timedelta(days=7 * k + 7), round(q["mean"][k], 2),
              round(q["p02_5"][k], 2), round(q["p10"][k], 2), round(q["p50"][k], 2), round(q["p90"][k], 2),
              round(q["p97_5"][k], 2), round(fc["expected"]["open_claims"][k], 2), round(fc["expected"]["new_claims"][k], 2),
              round(fc["expected"]["in_transit"][k], 2), need, float(p_below[k]),
              None if actual is None else round(float(actual[k]), 2)) for k in range(K)])
        w = bt["weekly"]
        cur.executemany(
            "insert into mart.cash_forecast_backtest values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'model_estimate')",
            [(r.lag_model, r.forecast_date, int(r.week_no), round(r.mean, 2), round(r.p02_5, 2), round(r.p10, 2),
              round(r.p90, 2), round(r.p97_5, 2), round(r.actual, 2), bool(r.in_80), bool(r.in_95)) for r in w.itertuples()])
        rows = []
        for payer, L in lags.items():
            for dday in range(0, 366):
                rows.append((f, payer, dday, float(L["km_S"][dday]), float(L["cdf"]["hybrid"][dday]), float(L["risk_set"][dday])))
        cur.executemany("insert into mart.cash_forecast_lag values (%s,%s,%s,%s,%s,%s,'model_estimate')", rows)
    label_table(conn, "mart.cash_forecast", labels.MODEL_ESTIMATE,
                "Weekly payer-receipt forecast distribution (Monte Carlo) after forecast_date; cash_need is a financial_assumption; actual is synthetic_record.")
    label_table(conn, "mart.cash_forecast_backtest", labels.MODEL_ESTIMATE,
                "Backtest of weekly receipt forecasts at past dates using only data available then; actual is synthetic_record.")
    label_table(conn, "mart.cash_forecast_lag", labels.MODEL_ESTIMATE,
                "Weighted Kaplan-Meier payment-lag survival by payer and the KM/parametric curve used in simulation.")


def figures(f: date, q: dict, need: float, lags: dict, bt: dict, fig_dir: Path = FIGURES_DIR) -> list[str]:
    plt = mpl()
    fig_dir.mkdir(parents=True, exist_ok=True)
    out = []
    styles = ["-", "--", ":", "-.", (0, (5, 1)), (0, (3, 1, 1, 1))]
    greys = ["0.0", "0.25", "0.45", "0.6", "0.1", "0.35"]
    # 1. lag curves
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    for i, (payer, L) in enumerate(sorted(lags.items())):
        x = np.arange(0, 181)
        ax.plot(x, 1 - L["km_S"][x], ls=styles[i % 6], color=greys[i % 6], lw=1.3,
                label=f"{payer} (best fit: {L['best']})")
    ax.set_xlabel("days from submission to payment")
    ax.set_ylabel("share of eventually-paid dollars paid")
    ax.set_title("Payment lag, dollar-weighted Kaplan-Meier by payer (right-censored)\nmodel_estimate, simulated data", fontsize=9)
    ax.legend(fontsize=7, loc="lower right")
    fig.tight_layout()
    p = fig_dir / "cashflow_lag_km.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    # 2. fan chart at headline date
    K = len(q["mean"])
    x = np.arange(1, K + 1)
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    ax.fill_between(x, q["p02_5"] / 1e3, q["p97_5"] / 1e3, color="0.85", label="95% interval")
    ax.fill_between(x, q["p10"] / 1e3, q["p90"] / 1e3, color="0.65", label="80% interval")
    ax.plot(x, q["p50"] / 1e3, color="black", lw=1.5, label="median")
    ax.axhline(need / 1e3, color="black", ls="--", lw=1, label="weekly cash need (assumption)")
    ax.set_xlabel(f"week after forecast date {f}")
    ax.set_ylabel("payer receipts, $ thousand")
    ax.set_title("13-week cash receipts forecast (Monte Carlo)\nmodel_estimate; simulated hospital stops billing after 2025-12-31",
                 fontsize=9)
    ax.legend(fontsize=7)
    fig.tight_layout()
    p = fig_dir / "cashflow_forecast_fan.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    # 3. backtest totals
    tt = bt["totals"]
    tt = tt[tt["lag_model"] == "hybrid"]
    fig, ax = plt.subplots(figsize=(6.6, 3.6))
    xd = pd.to_datetime(tt["forecast_date"])
    ax.vlines(xd, tt["p02_5"] / 1e6, tt["p97_5"] / 1e6, color="0.75", lw=6, label="95% interval")
    ax.vlines(xd, tt["p10"] / 1e6, tt["p90"] / 1e6, color="0.4", lw=2.5, label="80% interval")
    ax.scatter(xd, tt["mean"] / 1e6, marker="o", s=12, color="0.2", zorder=4, label="forecast mean")
    ax.scatter(xd, tt["actual"] / 1e6, marker="x", color="black", zorder=5, label="actual (bank deposits)")
    ax.set_xlabel("forecast date")
    ax.set_ylabel("13-week payer receipts, $ million")
    ax.set_title("Backtest: 13-week receipts forecast vs actual\nmodel_estimate vs synthetic_record", fontsize=9)
    ax.legend(fontsize=7)
    fig.autofmt_xdate()
    fig.tight_layout()
    p = fig_dir / "cashflow_backtest.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    return out


def curve_figure(curve: list[dict], f: date, base_factor: float, fig_dir: Path = FIGURES_DIR) -> str:
    plt = mpl()
    x = [c["need_factor"] for c in curve]
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    ax.plot(x, [c["p_shortfall"] for c in curve], color="black", marker="o", ms=3, label="P(shortfall)")
    ax.set_xlabel("weekly cash need / median weekly receipts of trailing 26 weeks")
    ax.set_ylabel("probability of a shortfall in 13 weeks")
    ax.set_ylim(-0.02, 1.02)
    ax2 = ax.twinx()
    ax2.plot(x, [c["shortfall_cvar95"] / 1e6 for c in curve], color="0.5", ls="--", marker="s", ms=3, label="CVaR95 of shortfall")
    ax2.set_ylabel("CVaR95 of shortfall, $ million")
    ax2.spines["right"].set_visible(True)
    ax2.grid(False)
    ax.axvline(base_factor, color="0.3", ls=":", lw=1)
    ax.text(base_factor, 0.95, " base case (assumption)", fontsize=7)
    ax.set_title(f"Shortfall risk across cash-need assumptions, forecast date {f}\n"
                 "model_estimate; need is financial_assumption",
                 fontsize=9)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="center left")
    fig.tight_layout()
    p = fig_dir / "cashflow_shortfall_curve.png"
    fig.savefig(p)
    plt.close(fig)
    return str(p)


def _params(cfg: dict) -> dict:
    p = dict(DEFAULTS)
    p.update((cfg or {}).get("cashflow", {}))
    return p


def run(conn, cfg: dict) -> dict:
    prm = _params(cfg)
    seed = int(cfg.get("seed", 0))
    as_of = to_date(cfg["as_of_date"])
    f = to_date(prm["forecast_date"] or as_of)
    markov.ensure_sql(conn)
    markov.run_sql_file(conn, SQL_FILE)
    A = assumptions(cfg)
    factor = float(A["cash_need_factor"]["value"])
    min_bal = float(A["minimum_operating_balance_usd"]["value"])
    K = int(prm["horizon_weeks"])
    # headline date (as-of): run-off of the open book
    fc = forecast(conn, f, cfg, prm, seed=seed, keep_inputs=True)
    q = quantiles(fc["sims"])
    need, med = cash_need(conn, f, factor)
    opening = float(query_df(conn, "select mart.cash_opening_balance(%s) as b", (f,))["b"].iloc[0])
    sm = shortfall_metrics(fc["sims"], opening, need, min_bal)
    observed = f + timedelta(days=7 * K) <= as_of
    actual = actual_weekly(conn, f, K) if observed else None
    bt = backtest(conn, cfg, prm, seed)
    claims_f = fc["claims"]
    trailing_year_rate = float((claims_f["submission_date"] > pd.Timestamp(f) - pd.Timedelta(days=365)).sum() / 365)
    # the Treasurer's question at a date when the simulated hospital is still billing, with the outcome
    f_ss = to_date(prm["backtest_end"])
    fc_ss = forecast(conn, f_ss, cfg, prm, seed=seed + 1000)
    need_ss, med_ss = cash_need(conn, f_ss, factor)
    open_ss = float(query_df(conn, "select mart.cash_opening_balance(%s) as b", (f_ss,))["b"].iloc[0])
    sm_ss = shortfall_metrics(fc_ss["sims"], open_ss, need_ss, min_bal)
    act_ss = actual_weekly(conn, f_ss, K)
    pos_ss = open_ss + np.cumsum(act_ss) - need_ss * np.arange(1, K + 1)
    steady = {"forecast_date": str(f_ss), "opening_bank_balance": round(open_ss, 2),
              "median_weekly_receipts_trailing_26w": round(med_ss, 2), "need_factor": factor,
              "weekly_cash_need": round(need_ss, 2),
              **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in sm_ss.items() if k != "p_below_min_by_week"},
              "actual_13w_receipts": round(float(act_ss.sum()), 2),
              "actual_shortfall_at_assumed_need": round(float(max(0.0, min_bal - pos_ss.min())), 2),
              "actual_receipts_percentile_in_forecast": round(float((fc_ss["sims"].sum(axis=1) < act_ss.sum()).mean()), 3)}
    curve_ss = shortfall_curve(fc_ss["sims"], open_ss, med_ss, min_bal)
    curve_asof = shortfall_curve(fc["sims"], opening, med, min_bal)
    write_marts(conn, f, q, fc, need, sm["p_below_min_by_week"], actual, bt, fc["lags"])
    figs = figures(f, q, need, fc["lags"], bt)
    figs.append(curve_figure(curve_ss, f_ss, factor))
    M, E, FA = labels.MODEL_ESTIMATE, labels.SYNTHETIC_RECORD, labels.FINANCIAL_ASSUMPTION
    lag_summary = {}
    for payer, L in sorted(fc["lags"].items()):
        lag_summary[payer] = {
            "n_claims": L["n_claims"], "n_paid": L["n_paid"], "km_median_days": L["km_median"],
            "km_reliable_to_day": L["tau"], "best_parametric": L["best"],
            "fits": {fam: {"params": {k: round(v, 4) for k, v in ft["params"].items()}, "aic": round(ft["aic"], 1),
                           "max_abs_diff_to_km": round(ft["max_abs_diff_to_km"], 4), "converged": ft["converged"],
                           "median_days": round(ft["median_days"], 2)} for fam, ft in L["fits"].items()},
        }
    weeks = [{"week_no": k + 1, "week_start": str(f + timedelta(days=7 * k + 1)), "week_end": str(f + timedelta(days=7 * k + 7)),
              "mean": round(float(q["mean"][k]), 2), "p10": round(float(q["p10"][k]), 2), "p50": round(float(q["p50"][k]), 2),
              "p90": round(float(q["p90"][k]), 2), "p02_5": round(float(q["p02_5"][k]), 2), "p97_5": round(float(q["p97_5"][k]), 2),
              "from_open_claims": round(float(fc["expected"]["open_claims"][k]), 2),
              "from_new_claims": round(float(fc["expected"]["new_claims"][k]), 2),
              "from_in_transit": round(float(fc["expected"]["in_transit"][k]), 2),
              "p_balance_below_minimum": round(float(sm["p_below_min_by_week"][k]), 4)} for k in range(K)]
    bts = bt["summary"]
    return {
        "module": "recon_engine.models.cashflow",
        "question": "Q3 Treasurer: will we be short of cash in the next 13 weeks?",
        "label_note": "Simulated data for a fictional hospital. Results show parameter recovery and that the decision logic "
                      "behaves correctly; they are not findings about real payers or banks.",
        "inputs": {"tables": ["core.fact_claim_event", "core.fact_claim_line", "core.fact_remit_line", "core.fact_bank_txn",
                              "recon.claim_remit_match", "recon.remit_duplicate", "recon.remit_trace", "recon.remit_bank_match"],
                   "sql": ["sql/models/40_chain.sql", "sql/models/41_cash_forecast.sql"], "params": prm},
        "seed": seed,
        "cutoff": str(f),
        "forecast_date": str(f),
        "forecast_is_run_off": labels.stamp(
            bool(sum(fc["arrival_rate_per_day"].values()) < 0.25 * trailing_year_rate), E,
            note="The simulated hospital bills no services after 2025-12-31, so at the as-of date there are almost no new "
                 "claims: the headline forecast is a run-off of the open book, not a going-concern forecast. "
                 "The backtest is where the method is validated."),
        "assumptions": A,
        "opening_bank_balance": labels.stamp(round(opening, 2), E, note="running balance of the collection account at the forecast date; excess is swept to a concentration account, so this is not total liquidity"),
        "open_claims": labels.stamp({"n": fc["n_open_claims"], "billed": round(fc["open_billed"], 2),
                                     "expected_eventual_payment": round(fc["open_expected_paid"], 2)}, M),
        "arrival_rate_claims_per_day": labels.stamp({k: round(v, 3) for k, v in fc["arrival_rate_per_day"].items()}, M),
        "in_transit_remitted_not_deposited": labels.stamp({"traces": fc["in_transit_traces"], "amount": round(float(fc["expected"]["in_transit"][0]), 2)}, E),
        "weekly_forecast": labels.stamp(weeks, M),
        "shortfall": labels.stamp(steady, M,
                                  note="The Treasurer's question asked at the last backtest date, while the simulated hospital is "
                                       "still billing. shortfall = max(0, minimum balance - lowest cumulative position over 13 weeks); "
                                       "cash_at_risk_95 = mean minus 5th percentile of 13-week receipts; actual_* use the bank "
                                       "deposits that followed (synthetic_record); weekly_cash_need is a financial_assumption."),
        "shortfall_curve": labels.stamp(curve_ss, M, note="same date; weekly cash need = factor x median weekly receipts of the trailing 26 weeks"),
        "shortfall_as_of_run_off": labels.stamp(
            {"forecast_date": str(f), "median_weekly_receipts_trailing_26w": round(med, 2), "weekly_cash_need": round(need, 2),
             **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in sm.items() if k != "p_below_min_by_week"},
             "curve": curve_asof}, M,
            note="Run-off: the simulated hospital bills nothing after 2025-12-31, so receipts fall towards zero and any "
                 "need anchored to past receipts produces a shortfall. Shown for completeness; not a going-concern answer."),
        "trailing_year_arrival_rate_per_day": labels.stamp(round(trailing_year_rate, 3), E),
        "lag_model": labels.stamp(lag_summary, M),
        "deposit_lag_mean_days": labels.stamp({k: round(float((np.arange(len(v)) * v).sum()), 2) for k, v in fc["deposit_pmf"].items()}, M),
        "backtest": labels.stamp({"dates": bt["dates"], "summary": bts,
                                  "totals_13w": [{k: (str(v) if k == "forecast_date" else (round(v, 2) if isinstance(v, float) else v))
                                                  for k, v in r.items()} for r in bt["totals"].to_dict(orient="records")]}, M,
                                 note="each forecast uses only rows dated on or before its forecast date; actual = payer deposits (synthetic_record)"),
        "marts": ["mart.cash_forecast", "mart.cash_forecast_backtest", "mart.cash_forecast_lag"],
        "figures": figs,
    }
