"""Question 4 (payer actuary): how much is still owed on claims incurred but not yet paid?

Method
------
1. Development triangle (SQL, ``mart.dev_triangle_at``): paid amounts by accident (service)
   month and development lag in months, using only claims and postings known on the as-of date.
2. Chain-ladder. Volume-weighted age-to-age factors ``f_j = sum_i C[i,j+1] / sum_i C[i,j]`` over
   accident months observed at both lags; cumulative factor to ultimate ``F_j = prod_{k>=j} f_k``;
   ``ultimate_i = C[i, latest_i] * F_latest_i`` and ``unpaid_i = ultimate_i - C[i, latest_i]``. No
   tail factor beyond the oldest observed lag (assumption, checked by the oldest periods).
3. Uncertainty. Mack (1993) standard errors, and an over-dispersed Poisson bootstrap of Pearson
   residuals (England and Verrall 2002) with gamma process noise, for a prediction interval.
4. Frequency-severity view. Monthly claim counts by payer: Poisson GLM with a business-day
   exposure, Pearson dispersion, Cameron-Trivedi test and negative binomial likelihood-ratio test.
   Severity of paid claims: gamma GLM vs lognormal, by lag of first payment. An alternative unpaid
   estimate = chain-ladder future paid-claim counts x severity of claims paid that late.
5. Retrospective test. Re-estimate at earlier as-of dates with only data known then, and compare
   with what was actually paid afterwards (and with the projected next 3 and 6 months of cash).

Everything is estimated from the database; nothing here reads generator outputs.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .. import FIGURES_DIR, REPO_ROOT, labels

SQL_FILE = REPO_ROOT / "sql" / "models" / "50_reserve_triangle.sql"
N_BOOT = 2000
N_BOOT_RETRO = 1000
RETRO_AS_OF = ("2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30")
LAG_BANDS = ((0, 0, "0"), (1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 5, "4-5"), (6, 999, "6+"))


# ------------------------------------------------------------------------------------------
# triangle algebra (unit tested on hand-computed examples)
# ------------------------------------------------------------------------------------------
def to_cumulative(inc: np.ndarray) -> np.ndarray:
    """Cumulative triangle from incremental; NaN (future) cells stay NaN."""
    out = np.nancumsum(inc, axis=1)
    out[np.isnan(inc)] = np.nan
    return out


def latest_lags(tri: np.ndarray) -> np.ndarray:
    """Index of the last observed (non-NaN) lag in each row."""
    obs = ~np.isnan(tri)
    return np.array([np.flatnonzero(r).max() if r.any() else -1 for r in obs])


def chain_ladder(cum: np.ndarray) -> dict[str, np.ndarray]:
    """Volume-weighted chain-ladder on a cumulative triangle (rows = accident, cols = lag).

    Returns factors f (len n_dev-1), cdf (len n_dev, cdf[j] = prod f[j:]), latest diagonal,
    ultimate, unpaid, number of rows behind each factor, and the completed square.
    """
    n_acc, n_dev = cum.shape
    f = np.ones(n_dev - 1)
    n_used = np.zeros(n_dev - 1, dtype=int)
    for j in range(n_dev - 1):
        m = ~np.isnan(cum[:, j]) & ~np.isnan(cum[:, j + 1])
        n_used[j] = int(m.sum())
        den = cum[m, j].sum()
        if m.any() and den != 0:
            f[j] = cum[m, j + 1].sum() / den
    cdf = np.ones(n_dev)
    for j in range(n_dev - 2, -1, -1):
        cdf[j] = cdf[j + 1] * f[j]
    L = latest_lags(cum)
    latest = np.array([cum[i, L[i]] if L[i] >= 0 else 0.0 for i in range(n_acc)])
    ult = np.array([latest[i] * cdf[L[i]] if L[i] >= 0 else 0.0 for i in range(n_acc)])
    full = cum.copy()
    for i in range(n_acc):
        for j in range(L[i] + 1, n_dev):
            full[i, j] = full[i, j - 1] * f[j - 1]
    return {"f": f, "cdf": cdf, "latest": latest, "ultimate": ult, "unpaid": ult - latest,
            "latest_lag": L, "n_used": n_used, "full": full}


def mack(cum: np.ndarray, cl: dict | None = None) -> dict[str, Any]:
    """Mack (1993) standard errors of the chain-ladder reserve, per row and in total."""
    cl = cl or chain_ladder(cum)
    n_acc, n_dev = cum.shape
    f, full, L = cl["f"], cl["full"], cl["latest_lag"]
    sig2 = np.full(n_dev - 1, np.nan)
    for j in range(n_dev - 1):
        m = ~np.isnan(cum[:, j]) & ~np.isnan(cum[:, j + 1]) & (cum[:, j] != 0)
        k = int(m.sum())
        if k >= 2:
            ratio = cum[m, j + 1] / cum[m, j]
            sig2[j] = float(np.sum(cum[m, j] * (ratio - f[j]) ** 2) / (k - 1))
    # extrapolate the variance parameter where fewer than two rows exist (Mack's rule)
    for j in range(n_dev - 1):
        if np.isnan(sig2[j]):
            if j >= 2 and not np.isnan(sig2[j - 1]) and not np.isnan(sig2[j - 2]) and sig2[j - 2] > 0:
                sig2[j] = min(sig2[j - 1] ** 2 / sig2[j - 2], sig2[j - 2], sig2[j - 1])
            else:
                sig2[j] = 0.0
    # denominator of f_k: sum of C[i,k] over rows observed at both k and k+1
    colsum = np.array([cum[~np.isnan(cum[:, j]) & ~np.isnan(cum[:, j + 1]), j].sum() for j in range(n_dev - 1)])
    mse = np.zeros(n_acc)
    for i in range(n_acc):
        s = 0.0
        for k in range(L[i], n_dev - 1):
            if f[k] == 0 or full[i, k] == 0 or colsum[k] == 0:
                continue
            s += sig2[k] / f[k] ** 2 * (1.0 / full[i, k] + 1.0 / colsum[k])
        mse[i] = full[i, -1] ** 2 * s
    total = mse.sum()
    for i in range(n_acc):
        for l_ in range(i + 1, n_acc):
            s = 0.0
            for k in range(max(L[i], L[l_]), n_dev - 1):
                if f[k] != 0 and colsum[k] != 0:
                    s += 2 * sig2[k] / f[k] ** 2 / colsum[k]
            total += full[i, -1] * full[l_, -1] * s
    return {"sigma2": sig2, "se": np.sqrt(mse), "se_total": float(math.sqrt(max(total, 0.0)))}


def fitted_incrementals(cum: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Back-cast fitted cumulative values from the latest diagonal and difference them (ODP fit)."""
    n_acc, n_dev = cum.shape
    L = latest_lags(cum)
    fit = np.full_like(cum, np.nan)
    for i in range(n_acc):
        if L[i] < 0:
            continue
        fit[i, L[i]] = cum[i, L[i]]
        for j in range(L[i] - 1, -1, -1):
            fit[i, j] = fit[i, j + 1] / f[j] if f[j] != 0 else 0.0
    m = np.diff(np.concatenate([np.zeros((n_acc, 1)), fit], axis=1), axis=1)
    m[np.isnan(cum)] = np.nan
    return m


def odp_bootstrap(inc: np.ndarray, n_boot: int = N_BOOT, seed: int = 0) -> dict[str, Any]:
    """Over-dispersed Poisson bootstrap of the chain-ladder reserve (England & Verrall 2002).

    Pearson residuals r = (C - m) / sqrt(|m|) are scaled by sqrt(n / (n - p)), resampled with
    replacement into a pseudo triangle C* = m + r* sqrt(|m|), the chain-ladder is refitted and
    future incrementals are drawn from a gamma with mean |m*| and variance phi |m*| (sign kept).
    Negative fitted cells are handled by the absolute value (a known approximation).
    """
    rng = np.random.default_rng(seed)
    cum = to_cumulative(inc)
    cl = chain_ladder(cum)
    m = fitted_incrementals(cum, cl["f"])
    obs = ~np.isnan(inc)
    n_acc, n_dev = inc.shape
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(obs & (np.abs(m) > 0), (inc - m) / np.sqrt(np.abs(m)), np.nan)
    L = cl["latest_lag"]
    # the two corner cells (oldest row's last lag, newest row's first lag) have zero residual by construction
    usable = ~np.isnan(r)
    usable[0, L[0]] = False
    usable[n_acc - 1, 0] = False
    n_cells = int(obs.sum())
    p = n_acc + n_dev - 1
    dof = max(n_cells - p, 1)
    phi = float(np.nansum(r[usable] ** 2) / dof)
    pool = r[usable] * math.sqrt(n_cells / dof)
    sqrt_m = np.sqrt(np.abs(np.nan_to_num(m)))
    mfill = np.nan_to_num(m)
    totals = np.zeros(n_boot)
    by_row = np.zeros((n_boot, n_acc))
    by_cal = np.zeros((n_boot, n_dev))   # future calendar months 1..n_dev-1 ahead
    for b in range(n_boot):
        rs = rng.choice(pool, size=inc.shape)
        star = np.where(obs, mfill + rs * sqrt_m, np.nan)
        cstar = to_cumulative(star)
        fs = np.ones(n_dev - 1)
        for j in range(n_dev - 1):
            mm = obs[:, j] & obs[:, j + 1]
            den = cstar[mm, j].sum()
            if mm.any() and den != 0:
                fs[j] = cstar[mm, j + 1].sum() / den
        for i in range(n_acc):
            c_prev = cstar[i, L[i]]
            for j in range(L[i] + 1, n_dev):
                c_next = c_prev * fs[j - 1]
                mu = c_next - c_prev
                if mu != 0 and phi > 0:
                    draw = math.copysign(rng.gamma(abs(mu) / phi, phi), mu)
                else:
                    draw = mu
                by_row[b, i] += draw
                by_cal[b, j - L[i]] += draw
                c_prev = c_next
        totals[b] = by_row[b].sum()
    return {"phi": phi, "n_cells": n_cells, "n_params": p, "totals": totals, "by_row": by_row,
            "by_calendar": by_cal, "n_residuals": int(usable.sum())}


def bornhuetter_ferguson(cl: dict, exposure: np.ndarray) -> dict[str, Any]:
    """Bornhuetter-Ferguson unpaid with a Cape Cod (Stanard-Buhlmann) expected ratio.

    ELR = sum_i paid_i / sum_i (exposure_i / CDF_i): paid to date over "used-up" exposure.
    unpaid_i = ELR * exposure_i * (1 - 1 / CDF_i). Unlike chain-ladder, an immature period's own
    noisy paid amount does not get multiplied by a large CDF.
    """
    cdf_i = cl["cdf"][cl["latest_lag"]]
    used = exposure / cdf_i
    elr = float(cl["latest"].sum() / used.sum()) if used.sum() else 0.0
    unpaid = elr * exposure * (1.0 - 1.0 / cdf_i)
    return {"elr": elr, "unpaid": unpaid, "prior_ultimate": elr * exposure}


def calendar_projection(full: np.ndarray, latest_lag: np.ndarray, months_ahead: int) -> float:
    """Chain-ladder projected payments in the next ``months_ahead`` calendar months."""
    tot = 0.0
    n_acc, n_dev = full.shape
    for i in range(n_acc):
        for k in range(1, months_ahead + 1):
            j = latest_lag[i] + k
            if j < n_dev:
                tot += full[i, j] - full[i, j - 1]
    return float(tot)


# ------------------------------------------------------------------------------------------
# data access
# ------------------------------------------------------------------------------------------
def _frame(conn, query: str, params: tuple = ()) -> pd.DataFrame:
    cur = conn.execute(query, params)
    df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for c in df.columns:
        if df[c].dtype == object and len(df) and type(df[c].dropna().iloc[0]).__name__ == "Decimal":
            df[c] = df[c].astype(float)
    return df


def triangle_long(conn, as_of: str) -> pd.DataFrame:
    df = _frame(conn, "select * from mart.dev_triangle_at(%s::date)", (as_of,))
    df["accident_month"] = pd.to_datetime(df["accident_month"])
    return df


def pivot(long: pd.DataFrame, col: str) -> tuple[np.ndarray, list[pd.Timestamp]]:
    tab = long.pivot(index="accident_month", columns="dev_lag", values=col).sort_index()
    return tab.to_numpy(dtype=float), list(tab.index)


# ------------------------------------------------------------------------------------------
# frequency-severity
# ------------------------------------------------------------------------------------------
def _business_days(month: pd.Timestamp) -> int:
    from pandas.tseries.holiday import USFederalHolidayCalendar

    end = month + pd.offsets.MonthEnd(0)
    hol = USFederalHolidayCalendar().holidays(month, end)
    return int(len(pd.bdate_range(month, end).difference(hol)))


def frequency_test(counts: pd.DataFrame) -> dict[str, Any]:
    """Poisson vs negative binomial for claim counts per payer x service month."""
    import statsmodels.api as sm
    import statsmodels.formula.api as smf
    from scipy import stats

    d = counts.copy()
    d["log_exposure"] = np.log(d["business_days"])
    pois = smf.glm("n ~ C(payer_id)", data=d, family=sm.families.Poisson(), offset=d["log_exposure"]).fit()
    mu = pois.fittedvalues.to_numpy()
    y = d["n"].to_numpy(float)
    pearson = float(np.sum((y - mu) ** 2 / mu))
    df_resid = int(pois.df_resid)
    # Cameron & Trivedi (1990): regress ((y-mu)^2 - y)/mu on mu with no intercept; alpha > 0 => NB2
    aux = sm.OLS(((y - mu) ** 2 - y) / mu, mu).fit()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # alpha -> 0 at the boundary makes the optimiser complain
        nb = smf.negativebinomial("n ~ C(payer_id)", data=d, offset=d["log_exposure"]).fit(disp=0, maxiter=500)
    alpha = float(nb.params["alpha"])
    lr = max(0.0, 2 * (float(nb.llf) - float(pois.llf)))
    return {
        "n_cells": int(len(d)),
        "poisson_pearson_chi2": pearson, "df_resid": df_resid,
        "pearson_dispersion": pearson / df_resid,
        "pearson_p_value_overdispersion": float(stats.chi2.sf(pearson, df_resid)),
        "cameron_trivedi_alpha": float(aux.params[0]), "cameron_trivedi_t": float(aux.tvalues[0]),
        "cameron_trivedi_p_one_sided": float(stats.t.sf(aux.tvalues[0], aux.df_resid)),
        "nb2_alpha": alpha,
        "lr_stat_nb_vs_poisson": lr,
        "lr_p_value_boundary": float(0.5 * stats.chi2.sf(lr, 1)) if lr > 0 else 0.5,
        "aic_poisson": float(pois.aic), "aic_nb2": float(nb.aic),
        "chosen": "negative_binomial" if (0.5 * stats.chi2.sf(lr, 1) < 0.05 and lr > 0) else "poisson",
    }


def lag_band(lag: int) -> str:
    for lo, hi, name in LAG_BANDS:
        if lo <= lag <= hi:
            return name
    return LAG_BANDS[-1][2]


def severity_models(sev: pd.DataFrame) -> dict[str, Any]:
    """Gamma GLM (log link) vs lognormal for net paid per claim, by first-payment lag band."""
    import statsmodels.api as sm
    import statsmodels.formula.api as smf
    from scipy import stats

    d = sev[sev["net_paid"] > 0].copy()
    d["band"] = d["first_paid_lag"].map(lag_band)
    d["y"] = d["net_paid"].astype(float)
    form = "y ~ C(band, Treatment(reference='0'))"
    gam = smf.glm(form, data=d, family=sm.families.Gamma(sm.families.links.Log())).fit(scale="X2")
    shape = 1.0 / float(gam.scale)
    mu = gam.fittedvalues.to_numpy()
    ll_gamma = float(np.sum(stats.gamma.logpdf(d["y"], a=shape, scale=mu / shape)))
    d["ly"] = np.log(d["y"])
    ols = smf.ols(form.replace("y ~", "ly ~"), data=d).fit()
    sigma = float(np.sqrt(ols.mse_resid))
    ll_lognormal = float(np.sum(stats.norm.logpdf(d["ly"], loc=ols.fittedvalues, scale=sigma)) - d["ly"].sum())
    k = len(gam.params) + 1
    bands = [b[2] for b in LAG_BANDS]
    grid = pd.DataFrame({"band": bands})
    gm = gam.predict(grid).to_numpy()
    lm = np.exp(ols.predict(grid).to_numpy() + sigma ** 2 / 2)
    emp = d.groupby("band")["y"].agg(["size", "mean", "median"]).reindex(bands)
    return {
        "n_claims": int(len(d)),
        "gamma_shape": shape, "gamma_dispersion": float(gam.scale),
        "lognormal_sigma": sigma,
        "loglik_gamma": ll_gamma, "loglik_lognormal": ll_lognormal,
        "aic_gamma": 2 * k - 2 * ll_gamma, "aic_lognormal": 2 * k - 2 * ll_lognormal,
        "better_by_aic": "gamma" if ll_gamma > ll_lognormal else "lognormal",
        "by_band": [{"band": b, "n": int(emp.loc[b, "size"]) if not pd.isna(emp.loc[b, "size"]) else 0,
                     "empirical_mean": float(emp.loc[b, "mean"]) if not pd.isna(emp.loc[b, "mean"]) else None,
                     "empirical_median": float(emp.loc[b, "median"]) if not pd.isna(emp.loc[b, "median"]) else None,
                     "gamma_mean": float(gm[i]), "lognormal_mean": float(lm[i])} for i, b in enumerate(bands)],
        "_gamma_mean_by_band": dict(zip(bands, gm)),
        "_lognormal_mean_by_band": dict(zip(bands, lm)),
    }


def freq_sev_unpaid(count_cum: np.ndarray, mean_by_band: dict[str, float]) -> tuple[np.ndarray, np.ndarray, dict]:
    """Future first-paid claim counts (chain-ladder on counts) x severity of claims paid at that lag."""
    cl = chain_ladder(count_cum)
    full, L = cl["full"], cl["latest_lag"]
    n_acc, n_dev = count_cum.shape
    unpaid = np.zeros(n_acc)
    for i in range(n_acc):
        for j in range(L[i] + 1, n_dev):
            unpaid[i] += (full[i, j] - full[i, j - 1]) * mean_by_band[lag_band(j)]
    return unpaid, cl["ultimate"], cl


# ------------------------------------------------------------------------------------------
# figures
# ------------------------------------------------------------------------------------------
def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _fig_development(cum: np.ndarray, cl: dict, months: list, path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 4.6))
    n_dev = cum.shape[1]
    lags = np.arange(n_dev)
    for i in range(cum.shape[0]):
        ult = cl["ultimate"][i]
        if ult > 0:
            ax.plot(lags, cum[i] / ult, color="0.75", lw=0.8)
    pattern = 1.0 / cl["cdf"]
    ax.plot(lags, pattern, color="0.05", lw=2, marker="o", ms=3, label="chain-ladder pattern (1 / CDF)")
    ax.plot([], [], color="0.75", lw=0.8, label="each accident month (paid / estimated ultimate)")
    ax.set_xlim(0, min(n_dev - 1, 18))
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Development lag (months after service month)")
    ax.set_ylabel("Share of ultimate paid")
    ax.set_title("Paid development pattern by service month (simulated claims)", fontsize=10)
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    ax.grid(alpha=0.25)
    fig.text(0.01, 0.01, "label: model_estimate on synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_bootstrap(totals: np.ndarray, cl_total: float, mack_se: float, path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.hist(totals / 1e3, bins=50, color="0.7", edgecolor="0.4")
    for q, ls in ((5, ":"), (50, "-"), (95, ":")):
        v = np.percentile(totals, q) / 1e3
        ax.axvline(v, color="0.1", ls=ls, lw=1.2)
        ax.text(v, ax.get_ylim()[1] * 0.95, f" p{q}", fontsize=8)
    ax.axvline(cl_total / 1e3, color="0.1", ls="--", lw=1.2)
    ax.set_xlabel("Total unpaid at the as-of date ($ thousand)")
    ax.set_ylabel("Bootstrap replications")
    ax.set_title(f"ODP bootstrap of the chain-ladder reserve (dashed = point estimate; Mack s.e. ${mack_se/1e3:,.0f}k)",
                 fontsize=9)
    fig.text(0.01, 0.01, "label: model_estimate on synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_retro(retro: list[dict], path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 4.2))
    x = np.arange(len(retro))
    est = np.array([r["unpaid_cl"] for r in retro]) / 1e3
    lo = np.array([r["boot_p05"] for r in retro]) / 1e3
    hi = np.array([r["boot_p95"] for r in retro]) / 1e3
    act = np.array([r["paid_later_observed"] for r in retro]) / 1e3
    bf = np.array([r["unpaid_bf"] for r in retro]) / 1e3
    ax.errorbar(x - 0.12, est, yerr=[est - lo, hi - est], fmt="o", color="0.1", capsize=3,
                label="chain-ladder estimate (bootstrap 5-95%)")
    ax.plot(x, bf, "^", color="0.35", ms=7, label="Bornhuetter-Ferguson (Cape Cod)")
    ax.plot(x + 0.12, act, "s", color="0.6", ms=7, label="actually paid afterwards (observed)")
    ax.set_xticks(x)
    ax.set_xticklabels([r["retro_as_of"] for r in retro])
    ax.set_xlabel("Earlier as-of date (only data known then)")
    ax.set_ylabel("Unpaid on claims incurred to that date ($ thousand)")
    ax.set_title("Retrospective test of the reserve (simulated data)", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(alpha=0.25)
    fig.text(0.01, 0.01, "label: model_estimate vs synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_factor_stability(cum: np.ndarray, months: list, path: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 4.2))
    styles = (("0.05", "-", "o"), ("0.45", "--", "s"), ("0.65", ":", "^"))
    for (j, (c, ls, mk)) in zip((0, 1, 2), styles):
        m = ~np.isnan(cum[:, j + 1]) & (cum[:, j] > 0)
        ratio = cum[m, j + 1] / cum[m, j]
        xs = [months[i] for i in np.flatnonzero(m)]
        ax.plot(xs, ratio, color=c, ls=ls, marker=mk, ms=3, label=f"lag {j} to {j + 1}")
    ax.set_yscale("log")
    ax.set_xlabel("Service month")
    ax.set_ylabel("Individual age-to-age ratio (log scale)")
    ax.set_title("Are early development factors stable over time? (chain-ladder assumption)", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.text(0.01, 0.01, "label: model_estimate on synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_severity(sevres: dict, path: Path) -> None:
    plt = _plt()
    rows = sevres["by_band"]
    fig, ax = plt.subplots(figsize=(7, 4.2))
    x = np.arange(len(rows))
    ax.bar(x - 0.2, [r["empirical_mean"] or 0 for r in rows], width=0.4, color="0.75", edgecolor="0.3", label="empirical mean")
    ax.plot(x, [r["gamma_mean"] for r in rows], "o-", color="0.1", label="gamma GLM mean")
    ax.plot(x, [r["lognormal_mean"] for r in rows], "s--", color="0.45", label="lognormal mean")
    ax.bar(x + 0.2, [r["empirical_median"] or 0 for r in rows], width=0.4, color="white", edgecolor="0.3", hatch="//",
           label="empirical median")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{r['band']}\n(n={r['n']})" for r in rows], fontsize=8)
    ax.set_xlabel("Lag of first payment (months after service month)")
    ax.set_ylabel("Net paid per claim ($)")
    ax.set_title("Severity rises with payment lag: late-paid claims are larger", fontsize=10)
    ax.legend(fontsize=8, frameon=False)
    ax.grid(alpha=0.25, axis="y")
    fig.text(0.01, 0.01, "label: model_estimate on synthetic_record", fontsize=7, color="0.4")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------------------------------
# run
# ------------------------------------------------------------------------------------------
def _insert(conn, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    vals = [tuple((None if (isinstance(r[c], float) and math.isnan(r[c])) else
                   (r[c].item() if hasattr(r[c], "item") else r[c])) for c in cols) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})", vals)


def estimate(conn, as_of: str, n_boot: int, seed: int) -> dict[str, Any]:
    """Triangle, chain-ladder, Mack and bootstrap at one as-of date (only data known then)."""
    long = triangle_long(conn, as_of)
    inc, months = pivot(long, "incremental_paid")
    cum = to_cumulative(inc)
    cl = chain_ladder(cum)
    mk = mack(cum, cl)
    bs = odp_bootstrap(inc, n_boot=n_boot, seed=seed)
    expo = long.groupby("accident_month")["contract_allowed_reported"].max().reindex(months).to_numpy(float)
    bf = bornhuetter_ferguson(cl, expo)
    return {"long": long, "inc": inc, "cum": cum, "months": months, "cl": cl, "mack": mk, "boot": bs, "bf": bf}


def run(conn, cfg: dict) -> dict:
    conn.execute(SQL_FILE.read_text(encoding="utf-8"))
    as_of = str(cfg["as_of_date"])
    seed = int(cfg["seed"])
    ME, SR, FA = labels.MODEL_ESTIMATE, labels.SYNTHETIC_RECORD, labels.FINANCIAL_ASSUMPTION
    st = labels.stamp

    # ---- chain-ladder at the official as-of date
    E = estimate(conn, as_of, N_BOOT, seed)
    cl, mk, bs, months, cum = E["cl"], E["mack"], E["boot"], E["months"], E["cum"]
    tie = conn.execute("select (select coalesce(sum(paid), 0) from recon.claim_line_balance) - %s::numeric",
                       (round(float(np.nansum(E["inc"])), 2),)).fetchone()[0]

    # ---- frequency-severity
    cnt_cum, _ = pivot(E["long"], "cumulative_claims_first_paid")
    sev = _frame(conn, "select * from mart.paid_claim_severity_at(%s::date)", (as_of,))
    sevres = severity_models(sev)
    fs_unpaid, fs_ult_counts, fs_cl = freq_sev_unpaid(cnt_cum, sevres["_gamma_mean_by_band"])
    fs_unpaid_ln, _, _ = freq_sev_unpaid(cnt_cum, sevres["_lognormal_mean_by_band"])
    counts = _frame(conn, """select payer_id, date_trunc('month', service_date)::date as month, count(distinct claim_id) as n
                             from core.fact_claim_line where submission_date <= %s::date
                               and service_date < date_trunc('month', %s::date)
                             group by 1, 2 order by 1, 2""", (as_of, as_of))
    counts["month"] = pd.to_datetime(counts["month"])
    counts["business_days"] = counts["month"].map(_business_days)
    freq = frequency_test(counts)

    # ---- retrospective tests (each uses only data known at its date)
    later = E["long"].copy()
    later["cal"] = (later["accident_month"].dt.to_period("M") + later["dev_lag"].astype(int)).dt.to_timestamp()
    retro = []
    for r_date in RETRO_AS_OF:
        R = estimate(conn, r_date, N_BOOT_RETRO, seed)
        r_month = pd.Timestamp(r_date).to_period("M").to_timestamp()
        paid_then = float(np.nansum(R["inc"]))
        sub = later[later["accident_month"] <= r_month]
        paid_now = float(sub["incremental_paid"].sum())
        nxt = {}
        for k in (3, 6):
            hi = r_month + pd.DateOffset(months=k)
            nxt[k] = float(sub[(sub["cal"] > r_month) & (sub["cal"] <= hi)]["incremental_paid"].sum())
        tot = R["boot"]["totals"]
        cal = R["boot"]["by_calendar"]
        retro.append({
            "retro_as_of": r_date,
            "unpaid_cl": float(R["cl"]["unpaid"].sum()),
            "unpaid_bf": float(R["bf"]["unpaid"].sum()),
            "boot_p05": float(np.percentile(tot, 5)), "boot_p95": float(np.percentile(tot, 95)),
            "mack_se": R["mack"]["se_total"],
            "paid_later_observed": paid_now - paid_then,
            "ratio_estimate_to_observed": float(R["cl"]["unpaid"].sum() / (paid_now - paid_then)) if paid_now != paid_then else None,
            "ratio_bf_to_observed": float(R["bf"]["unpaid"].sum() / (paid_now - paid_then)) if paid_now != paid_then else None,
            "latest_month_unpaid_cl": float(R["cl"]["unpaid"][-1]),
            "latest_month_unpaid_bf": float(R["bf"]["unpaid"][-1]),
            "latest_month_paid_later": float(sub[sub["accident_month"] == r_month]["incremental_paid"].sum() - R["cl"]["latest"][-1]),
            "observed_inside_90pct_interval": bool(np.percentile(tot, 5) <= paid_now - paid_then <= np.percentile(tot, 95)),
            "projected_next_3m": calendar_projection(R["cl"]["full"], R["cl"]["latest_lag"], 3),
            "paid_next_3m": nxt[3],
            "projected_next_6m": calendar_projection(R["cl"]["full"], R["cl"]["latest_lag"], 6),
            "paid_next_6m": nxt[6],
            "boot_next_3m_p05_p95": [float(np.percentile(cal[:, 1:4].sum(axis=1), q)) for q in (5, 95)],
        })

    # ---- marts
    as_of_d = pd.Timestamp(as_of).date()
    n_dev = cum.shape[1]
    conn.execute("delete from mart.reserve_factors where as_of_date = %s", (as_of_d,))
    conn.execute("delete from mart.reserve_by_period where as_of_date = %s", (as_of_d,))
    fac_rows = [{"as_of_date": as_of_d, "dev_lag_from": j, "dev_lag_to": j + 1, "n_periods": int(cl["n_used"][j]),
                 "age_to_age": round(float(cl["f"][j]), 6), "cumulative_to_ultimate": round(float(cl["cdf"][j]), 6),
                 "mack_sigma2": round(float(mk["sigma2"][j]), 6), "count_age_to_age": round(float(fs_cl["f"][j]), 6)}
                for j in range(n_dev - 1)]
    _insert(conn, "mart.reserve_factors", fac_rows)
    rep = E["long"].groupby("accident_month")["n_claims_reported"].max()
    per_rows = []
    for i, mth in enumerate(months):
        L = int(cl["latest_lag"][i])
        per_rows.append({
            "as_of_date": as_of_d, "accident_month": mth.date(), "latest_lag": L,
            "paid_to_date": round(float(cl["latest"][i]), 2), "cdf_to_ultimate": round(float(cl["cdf"][L]), 6),
            "ultimate_cl": round(float(cl["ultimate"][i]), 2), "unpaid_cl": round(float(cl["unpaid"][i]), 2),
            "mack_se": round(float(mk["se"][i]), 2),
            "boot_mean": round(float(bs["by_row"][:, i].mean()), 2),
            "boot_p05": round(float(np.percentile(bs["by_row"][:, i], 5)), 2),
            "boot_p50": round(float(np.percentile(bs["by_row"][:, i], 50)), 2),
            "boot_p95": round(float(np.percentile(bs["by_row"][:, i], 95)), 2),
            "unpaid_bf": round(float(E["bf"]["unpaid"][i]), 2),
            "unpaid_freq_sev": round(float(fs_unpaid[i]), 2),
            "claims_reported": int(rep.loc[mth]), "claims_paid": int(cnt_cum[i, L]),
            "claims_paid_ultimate_cl": round(float(fs_ult_counts[i]), 2)})
    _insert(conn, "mart.reserve_by_period", per_rows)
    conn.execute("delete from mart.reserve_retro")
    _insert(conn, "mart.reserve_retro", [{k: (pd.Timestamp(v).date() if k == "retro_as_of" else round(v, 2))
                                          for k, v in r.items() if k in (
        "retro_as_of", "unpaid_cl", "boot_p05", "boot_p95", "unpaid_bf", "paid_later_observed", "projected_next_3m",
        "paid_next_3m", "projected_next_6m", "paid_next_6m")} for r in retro])
    labels.label_table(conn, "mart.dev_triangle", SR, "Paid development triangle (service month x lag in months), sums of simulated remittances.")
    labels.label_table(conn, "mart.reserve_factors", ME, "Chain-ladder age-to-age factors (paid and first-paid counts) and Mack sigma^2.")
    labels.label_table(conn, "mart.reserve_by_period", ME, "Chain-ladder ultimate and unpaid by service month, Mack s.e., ODP bootstrap percentiles, frequency-severity unpaid.")
    labels.label_table(conn, "mart.reserve_retro", ME, "Retrospective test: reserve estimated at earlier as-of dates vs what was paid afterwards.")

    # ---- figures
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    figs = {"development": FIGURES_DIR / "reserve_development.png",
            "bootstrap": FIGURES_DIR / "reserve_bootstrap.png",
            "retro": FIGURES_DIR / "reserve_retro.png",
            "factor_stability": FIGURES_DIR / "reserve_factor_stability.png",
            "severity": FIGURES_DIR / "reserve_severity.png"}
    _fig_development(cum, cl, months, figs["development"])
    _fig_bootstrap(bs["totals"], float(cl["unpaid"].sum()), mk["se_total"], figs["bootstrap"])
    _fig_retro(retro, figs["retro"])
    _fig_factor_stability(cum, months, figs["factor_stability"])
    _fig_severity(sevres, figs["severity"])

    tot = bs["totals"]
    rnd = lambda x: round(float(x), 2)  # noqa: E731
    return {
        "name": "reserve",
        "question": "Q4 Payer actuary: how much is still owed on claims incurred but not yet paid?",
        "label": ME,
        "note": ("Simulated data for a fictional hospital and payers. Results show whether chain-ladder recovers the "
                 "generator's unpaid amount and how its intervals behave; they are not findings about real payers."),
        "seed": seed,
        "as_of_date": as_of,
        "inputs": {"tables": ["core.fact_claim_line", "core.fact_remit_line", "recon.claim_remit_match",
                              "recon.claim_line_balance"],
                   "sql": "sql/models/50_reserve_triangle.sql",
                   "grain": "accident = service month; development lag = payment month - service month",
                   "calibration_sha256": cfg.get("calibration_sha256")},
        "assumptions": {
            "tail_factor": st(1.0, FA, note="no development after the oldest observed lag; the oldest accident months show no payments after lag 18"),
            "paid_definition": "remittance paid_amount on lines matched to claim lines, net of reversals and recoupments",
            "bootstrap": f"ODP Pearson-residual bootstrap, {N_BOOT} replications (retro: {N_BOOT_RETRO}), gamma process noise, seed = config seed",
            "frequency_exposure": "weekdays minus US federal holidays in the service month",
        },
        "triangle": {
            "accident_months": [m.strftime("%Y-%m") for m in months],
            "n_dev_lags": int(n_dev),
            "total_paid_to_date": st(rnd(np.nansum(E["inc"])), SR),
            "tie_to_claim_line_balance_paid": st(float(tie), SR, note="difference in dollars; 0 = triangle ties to the reconciliation core"),
        },
        "chain_ladder": {
            "age_to_age": [{"from": j, "to": j + 1, "f": round(float(cl["f"][j]), 6), "n": int(cl["n_used"][j])}
                           for j in range(min(n_dev - 1, 15))],
            "cdf_by_lag": {str(j): round(float(cl["cdf"][j]), 6) for j in range(min(n_dev, 15))},
            "total_unpaid": st(rnd(cl["unpaid"].sum()), ME),
            "total_ultimate": st(rnd(cl["ultimate"].sum()), ME),
            "mack_se_total": st(rnd(mk["se_total"]), ME),
            "bf_cape_cod_unpaid": st(rnd(E["bf"]["unpaid"].sum()), ME,
                                     note="Bornhuetter-Ferguson, exposure = contract allowed on reported claims, Cape Cod expected ratio"),
            "bf_cape_cod_elr": st(round(E["bf"]["elr"], 4), ME, note="paid / contract allowed at ultimate"),
            "mack_cv": st(round(mk["se_total"] / cl["unpaid"].sum(), 4) if cl["unpaid"].sum() else None, ME),
            "bootstrap": {"mean": st(rnd(tot.mean()), ME), "sd": st(rnd(tot.std(ddof=1)), ME),
                          "p05": st(rnd(np.percentile(tot, 5)), ME), "p50": st(rnd(np.percentile(tot, 50)), ME),
                          "p95": st(rnd(np.percentile(tot, 95)), ME), "phi": round(bs["phi"], 2),
                          "n_cells": bs["n_cells"], "n_params": bs["n_params"]},
            "by_accident_month": [{"accident_month": r["accident_month"].strftime("%Y-%m"), "latest_lag": r["latest_lag"],
                                   "paid_to_date": r["paid_to_date"], "cdf": r["cdf_to_ultimate"], "unpaid_cl": r["unpaid_cl"],
                                   "mack_se": r["mack_se"], "boot_p05": r["boot_p05"], "boot_p95": r["boot_p95"],
                                   "unpaid_bf": r["unpaid_bf"], "unpaid_freq_sev": r["unpaid_freq_sev"]} for r in per_rows],
            "label": ME,
        },
        "frequency_severity": {
            "frequency": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in freq.items()},
            "severity": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in sevres.items() if not k.startswith("_")},
            "unpaid_freq_sev_gamma": st(rnd(fs_unpaid.sum()), ME),
            "unpaid_freq_sev_lognormal": st(rnd(fs_unpaid_ln.sum()), ME),
            "note": ("Frequency-severity unpaid = chain-ladder projection of first-paid claim counts x mean severity of "
                     "claims first paid at that lag. It ignores further postings on claims already paid (partial "
                     "supplements, reversals, late adjustments)."),
            "label": ME,
        },
        "retrospective": {"rows": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in retro],
                          "observed_inside_90pct_interval": f"{sum(r['observed_inside_90pct_interval'] for r in retro)} of {len(retro)}",
                          "label": ME,
                          "note": "paid_later_observed covers payments up to the official as-of date, so for the latest retro dates a little remains unpaid even then."},
        "marts": ["mart.dev_triangle", "mart.reserve_factors", "mart.reserve_by_period", "mart.reserve_retro"],
        "figures": [str(p.relative_to(REPO_ROOT)) for p in figs.values()],
    }
