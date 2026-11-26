"""Absorbing Markov chain of the claim life cycle (shared building block for Q3, Q4 and Q5).

States (DESIGN.md section 6)::

    transient : SUBMITTED, IN_REVIEW, PENDED, DENIED, APPEALED
    absorbing : PAID, WRITTEN_OFF

What is estimated
-----------------
The *embedded jump chain*: the probability that the next status of a claim is ``j`` given that
its current status is ``i``. Time spent in a state is not part of this model (cashflow.py
handles timing with a payment-lag distribution).

* Transition counts ``n_ij`` from consecutive events of each claim in ``core.fact_claim_event``
  that happened on or before the cutoff date.
* Maximum-likelihood estimate of each row: ``P_ij = n_ij / n_i`` (a multinomial MLE). Rows with
  fewer than ``min_row_count`` observed exits for a payer are replaced by the pooled row
  (all payers together) and flagged.
* Canonical form ``P = [[Q, R], [0, I]]``; fundamental matrix ``N = (I - Q)^-1`` (expected visits
  to each transient state); absorption probabilities ``B = N R``; expected number of
  transitions to absorption ``t = N 1`` with variance ``(2N - I) t - t*t`` (Kemeny and Snell).
* A claim-level bootstrap gives a percentile interval for P(PAID) and P(WRITTEN_OFF) from
  SUBMITTED.

Censoring at the cutoff
-----------------------
A claim that entered a state shortly before the cutoff and has not left it yet contributes no
exit, and quick exits are over-represented among recent entries (a pend is decided in half the
review time). ``maturity_days`` therefore counts exits only from visits that began at least that
many days before the cutoff (default 180, longer than nearly every planted sojourn). The
evaluation reports the effect of setting it to 0.

The Markov assumption and where these data break it
---------------------------------------------------
Memorylessness says the next status depends only on the current status. Two violations are
measured by :func:`markov_diagnostics`: (1) what happens after DENIED depends on the denial
reason (the hospital writes off non-covered denials far more often than administrative ones),
and the appeal outcome depends on the reason of the denial being appealed; (2) claims differ
(service line, amount), so a claim that has been denied once is more likely to be denied again
when resubmitted. The pooled chain still estimates the population-average (visit-weighted)
transition matrix consistently; per-claim predictions inherit the heterogeneity.

Public API used by other modules: :func:`fit`, :class:`ChainFit`, :func:`claims_asof`,
:func:`absorbing_quantities`, :func:`ensure_sql`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .. import FIGURES_DIR, SQL_DIR, labels

STATES: list[str] = ["SUBMITTED", "IN_REVIEW", "PENDED", "DENIED", "APPEALED", "PAID", "WRITTEN_OFF"]
TRANSIENT: list[str] = STATES[:5]
ABSORBING: list[str] = STATES[5:]
IDX: dict[str, int] = {s: i for i, s in enumerate(STATES)}
POOLED = "ALL"
SQL_FILE = SQL_DIR / "models" / "40_chain.sql"

DEFAULTS = {
    "min_row_count": 50,
    "maturity_days": 180,
    "n_boot": 500,
    "fit_cutoff": None,              # None = as-of date
    "holdout_cutoff": "2025-06-30",  # fit here, check predictions against outcomes observed later
}


# ------------------------------------------------------------------------------------------
# helpers shared with cashflow.py and credit.py
# ------------------------------------------------------------------------------------------
def to_date(x: Any) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def run_sql_file(conn, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))


def ensure_sql(conn) -> None:
    """(Re)create the point-in-time functions and the chain output tables."""
    run_sql_file(conn, SQL_FILE)


def query_df(conn, sql: str, params: tuple | dict | None = None) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def claims_asof(conn, cutoff: Any) -> pd.DataFrame:
    """One row per claim submitted on or before ``cutoff`` with its state at the cutoff.

    Uses only data dated on or before the cutoff (see sql/models/40_chain.sql).
    """
    df = query_df(conn, "select * from mart.chain_claim_asof(%s)", (to_date(cutoff),))
    for c in ("billed", "paid_to_date"):
        df[c] = df[c].astype(float)
    for c in ("submission_date", "state_since", "first_paid_event_date", "first_payment_date"):
        df[c] = pd.to_datetime(df[c])
    return df


def events_asof(conn, cutoff: Any) -> pd.DataFrame:
    df = query_df(conn, "select * from mart.chain_events_asof(%s) order by claim_id, seq", (to_date(cutoff),))
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df


def label_table(conn, table: str, label: str, note: str = "") -> None:
    """Register a mart table's label (thin wrapper over ``labels.label_table``, kept so the
    cashflow and credit modules have one import point)."""
    labels.label_table(conn, table, label, note)


def mpl():
    """Matplotlib with a print-friendly (grey-scale readable) style."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"figure.dpi": 120, "savefig.dpi": 150, "font.size": 9, "axes.grid": True,
                         "grid.color": "0.85", "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False})
    return plt


# ------------------------------------------------------------------------------------------
# estimation
# ------------------------------------------------------------------------------------------
def extract_transitions(events: pd.DataFrame, cutoff: Any, maturity_days: int = 0) -> tuple[pd.DataFrame, dict]:
    """Consecutive status pairs per claim.

    ``events`` needs columns claim_id, payer_id, seq, status, event_date (already limited to the
    cutoff). Returns (transitions, notes). Self-repeats (the same status reported twice) and
    events after an absorbing state are dropped and counted in ``notes``.
    """
    cutoff = pd.Timestamp(to_date(cutoff))
    e = events.sort_values(["claim_id", "seq"]).reset_index(drop=True)
    nxt = e.groupby("claim_id").shift(-1)
    pairs = pd.DataFrame({
        "claim_id": e["claim_id"], "payer_id": e["payer_id"], "from_state": e["status"],
        "to_state": nxt["status"], "from_date": e["event_date"], "to_date": nxt["event_date"],
    })
    notes = {"n_events": int(len(e)), "n_claims": int(e["claim_id"].nunique())}
    open_visits = pairs["to_state"].isna() & pairs["from_state"].isin(TRANSIENT)
    pairs = pairs[pairs["to_state"].notna()]
    from_abs = pairs["from_state"].isin(ABSORBING)
    self_rep = pairs["from_state"] == pairs["to_state"]
    unknown = ~pairs["from_state"].isin(STATES) | ~pairs["to_state"].isin(STATES)
    notes["dropped_after_absorbing"] = int(from_abs.sum())
    notes["dropped_self_repeat"] = int((self_rep & ~from_abs).sum())
    notes["dropped_unknown_state"] = int((unknown & ~from_abs & ~self_rep).sum())
    pairs = pairs[~from_abs & ~self_rep & ~unknown]
    if maturity_days:
        limit = cutoff - pd.Timedelta(days=int(maturity_days))
        immature = pairs["from_date"] > limit
        notes["dropped_immature_exits"] = int(immature.sum())
        notes["censored_mature_visits"] = int((open_visits & (e["event_date"] <= limit)).sum())
        pairs = pairs[~immature]
    notes["n_transitions"] = int(len(pairs))
    return pairs.reset_index(drop=True), notes


def count_matrix(pairs: pd.DataFrame) -> np.ndarray:
    """5 x 7 matrix of transition counts (rows: transient states, columns: all states).

    If ``pairs`` has a column ``w`` (claim weights, e.g. billed dollars rescaled to mean 1) the
    counts are weighted sums.
    """
    C = np.zeros((len(TRANSIENT), len(STATES)))
    if len(pairs):
        g = (pairs.groupby(["from_state", "to_state"])["w"].sum() if "w" in pairs
             else pairs.groupby(["from_state", "to_state"]).size())
        for (a, b), n in g.items():
            C[IDX[a], IDX[b]] += n
    return C


def mle_matrix(counts: np.ndarray, pooled_counts: np.ndarray | None = None,
               min_row_count: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise multinomial MLE ``P_ij = n_ij / n_i`` with a pooled fallback for sparse rows.

    Returns the full 7 x 7 matrix (absorbing rows are identity) and a boolean flag per transient
    row saying whether the pooled row was used.
    """
    k = len(TRANSIENT)
    P = np.zeros((len(STATES), len(STATES)))
    pooled_used = np.zeros(k, dtype=bool)
    n = counts.sum(axis=1)
    for i in range(k):
        if n[i] >= min_row_count or pooled_counts is None:
            P[i] = counts[i] / n[i] if n[i] > 0 else 0.0
        else:
            pn = pooled_counts[i].sum()
            P[i] = pooled_counts[i] / pn if pn > 0 else 0.0
            pooled_used[i] = True
        if P[i].sum() == 0:  # never observed anywhere: treat as written off (conservative)
            P[i, IDX["WRITTEN_OFF"]] = 1.0
    for s in ABSORBING:
        P[IDX[s], IDX[s]] = 1.0
    return P, pooled_used


def absorbing_quantities(P: np.ndarray, n_transient: int | None = None) -> dict[str, np.ndarray]:
    """Q, R, N = (I - Q)^-1, B = N R, expected steps t = N 1 and their standard deviation.

    ``P`` is in canonical order: transient states first. Works for any size, so tests can use a
    hand-built 3-state chain.
    """
    k = n_transient if n_transient is not None else len(TRANSIENT)
    Q = P[:k, :k]
    R = P[:k, k:]
    I = np.eye(k)
    N = np.linalg.inv(I - Q)
    B = N @ R
    t = N @ np.ones(k)
    var_t = (2 * N - I) @ t - t * t
    return {"Q": Q, "R": R, "N": N, "B": B, "t": t, "sd_t": np.sqrt(np.maximum(var_t, 0.0))}


def _claim_count_vectors(pairs: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Per-claim transition counts flattened to 35 columns (5 transient rows x 7 states)."""
    if not len(pairs):
        return np.zeros((0, 35)), []
    codes = pairs["from_state"].map(IDX).to_numpy() * len(STATES) + pairs["to_state"].map(IDX).to_numpy()
    claims, inv = np.unique(pairs["claim_id"].to_numpy(), return_inverse=True)
    M = np.zeros((len(claims), len(TRANSIENT) * len(STATES)))
    np.add.at(M, (inv, codes), pairs["w"].to_numpy() if "w" in pairs else 1.0)
    return M, list(claims)


@dataclass
class ChainFit:
    """Result of :func:`fit`. Groups are payer ids plus ``"ALL"`` (pooled)."""

    cutoff: date
    by: str
    min_row_count: int
    maturity_days: int
    counts: dict[str, np.ndarray]
    P: dict[str, np.ndarray]
    pooled_rows: dict[str, np.ndarray]
    quantities: dict[str, dict[str, np.ndarray]]
    boot: dict[str, dict[str, float]] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    sojourns: pd.DataFrame | None = None      # payer_id, from_state, to_state, days (mature exits)
    min_duration_n: int = 30
    _dur_cache: dict = field(default_factory=dict, repr=False)

    @property
    def groups(self) -> list[str]:
        return list(self.P)

    def _g(self, group: str | None) -> str:
        return group if group in self.P else POOLED

    def B(self, group: str | None = None) -> np.ndarray:
        return self.quantities[self._g(group)]["B"]

    def p_paid(self, state: str, group: str | None = None) -> float:
        if state == "PAID":
            return 1.0
        if state == "WRITTEN_OFF":
            return 0.0
        return float(self.B(group)[IDX[state], 0])

    def p_written_off(self, state: str, group: str | None = None) -> float:
        return 1.0 - self.p_paid(state, group)

    def _duration_table(self, state: str, group: str):
        """Sorted sojourn days and suffix counts per destination for (state, group)."""
        key = (state, group)
        if key not in self._dur_cache:
            so = self.sojourns
            sub = so[so["from_state"] == state] if group == POOLED else so[(so["from_state"] == state) & (so["payer_id"] == group)]
            sub = sub.sort_values("days")
            days = sub["days"].to_numpy(dtype=float)
            dest = sub["to_state"].map(IDX).to_numpy()
            onehot = np.zeros((len(days) + 1, len(STATES)))
            if len(days):
                onehot[np.arange(len(days)), dest] = sub["w"].to_numpy() if "w" in sub else 1.0
            suffix = onehot[::-1].cumsum(axis=0)[::-1]       # suffix[k] = counts among visits k..end
            self._dur_cache[key] = (days, suffix)
        return self._dur_cache[key]

    def first_step_given_duration(self, state: str, group: str | None, days_in_state: float) -> np.ndarray:
        """P(next status | current status, already ``days_in_state`` days in it).

        Semi-Markov correction: uses only past visits to ``state`` that lasted longer than
        ``days_in_state``. Payer-specific when at least ``min_duration_n`` such visits exist,
        else pooled; if even the pooled set is that thin, the ``min_duration_n`` longest pooled
        visits are used. With days_in_state = 0 this is (up to maturity filtering) the MLE row.
        """
        for g in ([self._g(group), POOLED] if self._g(group) != POOLED else [POOLED]):
            days, suffix = self._duration_table(state, g)
            k = int(np.searchsorted(days, days_in_state, side="right"))
            if len(days) - k >= self.min_duration_n and suffix[k].sum() > 0:
                return suffix[k] / suffix[k].sum()
        days, suffix = self._duration_table(state, POOLED)
        k = max(len(days) - self.min_duration_n, 0)
        if suffix[k].sum() == 0:
            return self.P[self._g(group)][IDX[state]]
        return suffix[k] / suffix[k].sum()

    def p_paid_open(self, states, groups, days_in_state) -> np.ndarray:
        """Duration-adjusted P(eventually PAID) for open claims (vectorised over claims).

        p = sum_j P(j | state, time already in state) * B[j, PAID], with B[PAID]=1, B[WRITTEN_OFF]=0.
        """
        if self.sojourns is None:
            return np.array([self.p_paid(s, g) for s, g in zip(states, groups)])
        out = np.empty(len(states))
        cache: dict = {}
        for k, (s, g, d) in enumerate(zip(states, groups, days_in_state)):
            if s in ABSORBING:
                out[k] = 1.0 if s == "PAID" else 0.0
                continue
            key = (s, g, int(d))
            if key not in cache:
                step = self.first_step_given_duration(s, g, float(int(d)))
                bp = np.array([self.p_paid(t, g) for t in STATES])
                cache[key] = float(step @ bp)
            out[k] = cache[key]
        return out

    def absorption_table(self) -> pd.DataFrame:
        """payer_id x state -> p_paid, p_written_off, expected_steps (absorbing states included)."""
        rows = []
        for g, q in self.quantities.items():
            for i, s in enumerate(TRANSIENT):
                rows.append({"payer_id": g, "state": s, "p_paid": float(q["B"][i, 0]),
                             "p_written_off": float(q["B"][i, 1]), "expected_steps": float(q["t"][i]),
                             "sd_steps": float(q["sd_t"][i])})
            rows.append({"payer_id": g, "state": "PAID", "p_paid": 1.0, "p_written_off": 0.0,
                         "expected_steps": 0.0, "sd_steps": 0.0})
            rows.append({"payer_id": g, "state": "WRITTEN_OFF", "p_paid": 0.0, "p_written_off": 1.0,
                         "expected_steps": 0.0, "sd_steps": 0.0})
        return pd.DataFrame(rows)

    def matrix_frame(self, group: str) -> pd.DataFrame:
        return pd.DataFrame(self.P[group][: len(TRANSIENT)], index=TRANSIENT, columns=STATES)


def fit_from_events(events: pd.DataFrame, cutoff: Any, *, min_row_count: int = 50, maturity_days: int = 180,
                    n_boot: int = 0, seed: int | None = 0, claim_weights: pd.Series | None = None) -> ChainFit:
    """Estimate the chain by payer (plus pooled) from an events frame (see :func:`events_asof`).

    ``claim_weights`` (indexed by claim_id, e.g. billed amount) turns the count MLE into a
    dollar-weighted one: each transition counts with its claim's weight, rescaled to mean 1 so
    that the sparse-row threshold still reads as "about this many claims". The dollar-weighted
    chain answers "what share of *dollars* end PAID", which matters when large claims are denied
    more often (credit.py uses it for PD).
    """
    cutoff = to_date(cutoff)
    if maturity_days and len(events):
        # short history (early cutoffs): never use more than half of the available history as maturity
        history = (pd.Timestamp(cutoff) - events["event_date"].min()).days
        maturity_days = int(min(maturity_days, max(history // 2, 0)))
    pairs, notes = extract_transitions(events, cutoff, maturity_days)
    notes["maturity_days_used"] = int(maturity_days)
    if claim_weights is not None:
        w = pairs["claim_id"].map(claim_weights).fillna(0.0).astype(float)
        pairs["w"] = w / w.mean() if w.mean() > 0 else 1.0
        notes["weighted"] = True
    pooled = count_matrix(pairs)
    counts, P, flags, quant, boot = {}, {}, {}, {}, {}
    rng = np.random.default_rng(seed)
    Pp, _ = mle_matrix(pooled, None, min_row_count)
    groups = [POOLED] + sorted(pairs["payer_id"].dropna().unique().tolist())
    for g in groups:
        sub = pairs if g == POOLED else pairs[pairs["payer_id"] == g]
        C = pooled if g == POOLED else count_matrix(sub)
        Pg, fl = (Pp, np.zeros(len(TRANSIENT), dtype=bool)) if g == POOLED else mle_matrix(C, pooled, min_row_count)
        counts[g], P[g], flags[g], quant[g] = C, Pg, fl, absorbing_quantities(Pg)
        if n_boot:
            M, _ = _claim_count_vectors(sub)
            n = len(M)
            W = rng.multinomial(n, np.full(n, 1.0 / n), size=n_boot) if n else np.zeros((n_boot, 0))
            Cb = (W @ M).reshape(n_boot, len(TRANSIENT), len(STATES))
            pp = np.empty(n_boot)
            for b in range(n_boot):
                Pb, _ = mle_matrix(Cb[b], pooled, min_row_count)
                pp[b] = absorbing_quantities(Pb)["B"][0, 0]
            lo, hi = np.percentile(pp, [2.5, 97.5])
            boot[g] = {"p_paid_from_submitted_lo": float(lo), "p_paid_from_submitted_hi": float(hi),
                       "p_paid_from_submitted_boot_sd": float(pp.std(ddof=1)), "n_boot": int(n_boot),
                       "n_claims": int(n)}
    soj = pd.DataFrame({"payer_id": pairs["payer_id"], "from_state": pairs["from_state"], "to_state": pairs["to_state"],
                        "days": (pairs["to_date"] - pairs["from_date"]).dt.days.astype(float),
                        "w": pairs["w"] if "w" in pairs else 1.0})
    return ChainFit(cutoff=cutoff, by="payer", min_row_count=min_row_count, maturity_days=maturity_days,
                    counts=counts, P=P, pooled_rows=flags, quantities=quant, boot=boot, notes=notes, seed=seed,
                    sojourns=soj)


def fit(conn, cutoff: Any, by: str = "payer", *, min_row_count: int = 50, maturity_days: int = 180,
        n_boot: int = 0, seed: int | None = 0) -> ChainFit:
    """Estimate the life-cycle chain from events on or before ``cutoff`` (reusable entry point).

    ``by`` is accepted for readability; only "payer" (with the pooled "ALL" group) is supported.
    """
    if by != "payer":
        raise ValueError("only by='payer' is supported")
    ensure_sql(conn)
    return fit_from_events(events_asof(conn, cutoff), cutoff, min_row_count=min_row_count,
                           maturity_days=maturity_days, n_boot=n_boot, seed=seed)


# ------------------------------------------------------------------------------------------
# diagnostics of the Markov assumption
# ------------------------------------------------------------------------------------------
def _chi2_homogeneity(table: pd.DataFrame) -> dict[str, float]:
    from scipy.stats import chi2_contingency

    t = table.loc[table.sum(axis=1) > 0, table.sum(axis=0) > 0]
    if t.shape[0] < 2 or t.shape[1] < 2:
        return {"chi2": float("nan"), "dof": 0, "p_value": float("nan")}
    chi2, p, dof, _ = chi2_contingency(t.to_numpy())
    return {"chi2": round(float(chi2), 2), "dof": int(dof), "p_value": float(p)}


def markov_diagnostics(events: pd.DataFrame) -> dict[str, Any]:
    """Tests of memorylessness that the data can answer.

    1. Next status after DENIED, by the denial reason category (chi-square test of homogeneity).
    2. Appeal outcome (APPEALED -> PAID vs DENIED) by the reason of the denial being appealed.
    3. Probability of denial when leaving IN_REVIEW, first visit versus later visits.
    Under the Markov assumption each of these rates would not depend on the extra information.
    """
    e = events.sort_values(["claim_id", "seq"]).copy()
    g = e.groupby("claim_id")
    e["next_status"] = g["status"].shift(-1)
    e["prev_reason"] = g["reason_category"].shift(1)
    e["visit_no"] = e.groupby(["claim_id", "status"]).cumcount() + 1
    out: dict[str, Any] = {}

    d = e[(e["status"] == "DENIED") & e["next_status"].notna() & e["reason_category"].notna()]
    t1 = pd.crosstab(d["reason_category"], d["next_status"])
    out["after_denied_by_reason"] = {
        "rates": (t1.div(t1.sum(axis=1), axis=0).round(3)).to_dict(orient="index"),
        "n": t1.sum(axis=1).astype(int).to_dict(), **_chi2_homogeneity(t1)}

    a = e[(e["status"] == "APPEALED") & e["next_status"].isin(["PAID", "DENIED"]) & e["prev_reason"].notna()]
    t2 = pd.crosstab(a["prev_reason"], a["next_status"])
    out["appeal_outcome_by_reason"] = {
        "p_paid": (t2.get("PAID", 0) / t2.sum(axis=1)).round(3).to_dict(),
        "n": t2.sum(axis=1).astype(int).to_dict(), **_chi2_homogeneity(t2)}

    r = e[(e["status"] == "IN_REVIEW") & e["next_status"].isin(["PAID", "DENIED", "PENDED"])].copy()
    r["visit"] = np.where(r["visit_no"] == 1, "first", "later")
    t3 = pd.crosstab(r["visit"], r["next_status"])
    out["in_review_outcome_by_visit"] = {
        "p_denied": (t3.get("DENIED", 0) / t3.sum(axis=1)).round(3).to_dict(),
        "n": t3.sum(axis=1).astype(int).to_dict(), **_chi2_homogeneity(t3)}
    return out


# ------------------------------------------------------------------------------------------
# out-of-sample calibration (uses outcomes observed after the fit cutoff; no ground truth)
# ------------------------------------------------------------------------------------------
def holdout_calibration(conn, chain: ChainFit, eval_date: Any, new_claim_window_days: int = 120) -> dict[str, Any]:
    """Compare predicted P(PAID) with outcomes observed between the fit cutoff and ``eval_date``.

    Two groups: claims open at the cutoff (prediction from their state at the cutoff) and claims
    submitted after the cutoff (prediction from SUBMITTED). Claims still open at ``eval_date``
    have no observed outcome and are excluded (their number is reported: excluding them biases
    the observed rate towards fast outcomes).
    """
    at_cut = claims_asof(conn, chain.cutoff)
    at_end = claims_asof(conn, eval_date)
    end_state = at_end.set_index("claim_id")["state"]
    open_cut = at_cut[at_cut["state"].isin(TRANSIENT)].copy()
    open_cut["group"] = "open_at_cutoff"
    open_cut["days_in_state"] = (pd.Timestamp(chain.cutoff) - open_cut["state_since"]).dt.days
    cut = pd.Timestamp(chain.cutoff)
    new = at_end[(at_end["submission_date"] > cut)
                 & (at_end["submission_date"] <= cut + pd.Timedelta(days=new_claim_window_days))].copy()
    new["state"] = "SUBMITTED"
    new["days_in_state"] = 0
    new["group"] = "submitted_after_cutoff"
    cols = ["claim_id", "payer_id", "state", "days_in_state", "group"]
    df = pd.concat([open_cut[cols], new[cols]], ignore_index=True)
    df["p_memoryless"] = [chain.p_paid(s, p) for s, p in zip(df["state"], df["payer_id"])]
    df["p_duration"] = chain.p_paid_open(df["state"].tolist(), df["payer_id"].tolist(), df["days_in_state"].tolist())
    df["end_state"] = df["claim_id"].map(end_state)
    resolved = df[df["end_state"].isin(ABSORBING)].copy()
    resolved["y"] = (resolved["end_state"] == "PAID").astype(float)

    def summ(x: pd.DataFrame, col: str) -> dict[str, float]:
        n = len(x)
        pbar, ybar = float(x[col].mean()), float(x["y"].mean())
        se = float(np.sqrt(max((x[col] * (1 - x[col])).sum(), 1e-12)) / n)
        return {"n": n, "predicted_p_paid": round(pbar, 4), "observed_p_paid": round(ybar, 4),
                "difference": round(ybar - pbar, 4), "z": round((ybar - pbar) / se, 2) if se > 0 else None}

    out = {"fit_cutoff": str(chain.cutoff), "eval_date": str(to_date(eval_date)),
           "new_claim_window_days": new_claim_window_days,
           "n_claims_in_holdout": int(len(df)), "n_still_open_at_eval": int((~df["end_state"].isin(ABSORBING)).sum()),
           "brier_score_constant_rate": round(float(((resolved["y"].mean() - resolved["y"]) ** 2).mean()), 4)}
    for name, col in (("memoryless", "p_memoryless"), ("duration_adjusted", "p_duration")):
        r = {"brier_score": round(float(((resolved[col] - resolved["y"]) ** 2).mean()), 4),
             "overall": summ(resolved, col)}
        r["by_group"] = {k: summ(v, col) for k, v in resolved.groupby("group")}
        r["by_state_at_cutoff"] = {k: summ(v, col) for k, v in resolved[resolved["group"] == "open_at_cutoff"].groupby("state")}
        r["by_payer"] = {k: summ(v, col) for k, v in resolved.groupby("payer_id")}
        bins = pd.cut(resolved[col], [0, 0.3, 0.5, 0.7, 0.8, 0.9, 1.0], include_lowest=True)
        r["reliability_bins"] = {str(k): summ(v, col) for k, v in resolved.groupby(bins, observed=True)}
        out[name] = r
    return out


# ------------------------------------------------------------------------------------------
# evaluation against the generator's planted parameters (called by tests and the run script,
# which load the generator records themselves; run() never does)
# ------------------------------------------------------------------------------------------
def evaluate_against_planted(chain: ChainFit, planted: dict) -> dict[str, Any]:
    """Recovery of the planted visit-weighted transition matrices and absorption probabilities.

    Each cell error is also expressed in standard errors ``sqrt(p(1-p)/n_i)`` so a tolerance can
    be stated statistically.
    """
    out: dict[str, Any] = {"per_payer": {}}
    max_err, max_z = 0.0, 0.0
    for payer, pm in planted["markov_by_payer"].items():
        if payer not in chain.P:
            continue
        truth = np.array(pm["population_matrix"])          # 5 x 7, rows TRANSIENT
        est = chain.P[payer][: len(TRANSIENT)]
        n = chain.counts[payer].sum(axis=1)
        err = np.abs(est - truth)
        with np.errstate(divide="ignore", invalid="ignore"):
            se = np.sqrt(np.clip(truth * (1 - truth), 1e-12, None) / np.maximum(n, 1)[:, None])
            z = np.where(truth * (1 - truth) > 0, err / se, 0.0)
        i, j = np.unravel_index(np.argmax(err), err.shape)
        Ptrue = np.zeros((7, 7))
        Ptrue[:5] = truth
        Ptrue[5, 5] = Ptrue[6, 6] = 1
        b_pop = absorbing_quantities(Ptrue)["B"][0, 0]
        b_est = chain.p_paid("SUBMITTED", payer)
        bt = chain.boot.get(payer, {})
        visits_true = np.array([pm["expected_visits_from_submitted"][s] for s in TRANSIENT])
        visits_est = chain.quantities[payer]["N"][0]
        out["per_payer"][payer] = {
            "max_abs_error": round(float(err.max()), 4), "max_abs_error_cell": f"{TRANSIENT[i]}->{STATES[j]}",
            "mean_abs_error_nonzero_cells": round(float(err[truth > 0].mean()), 4),
            "max_z": round(float(z.max()), 2), "rows_using_pooled": [TRANSIENT[k] for k in np.where(chain.pooled_rows[payer])[0]],
            "p_paid_from_submitted_est": round(b_est, 4),
            "p_paid_planted_mean_over_claims": pm["absorption_probability_paid"],
            "p_paid_implied_by_planted_population_matrix": round(float(b_pop), 4),
            "p_paid_abs_error_vs_mean_over_claims": round(abs(b_est - pm["absorption_probability_paid"]), 4),
            "bootstrap_95": [round(bt.get("p_paid_from_submitted_lo", np.nan), 4), round(bt.get("p_paid_from_submitted_hi", np.nan), 4)],
            "planted_in_bootstrap_95": bool(bt) and bt["p_paid_from_submitted_lo"] <= pm["absorption_probability_paid"] <= bt["p_paid_from_submitted_hi"],
            "expected_visits_max_abs_error": round(float(np.abs(visits_est - visits_true).max()), 4),
        }
        max_err, max_z = max(max_err, float(err.max())), max(max_z, float(z.max()))
    out["max_abs_error_all_payers"] = round(max_err, 4)
    out["max_z_all_payers"] = round(max_z, 2)
    out["n_planted_in_bootstrap_95"] = int(sum(v["planted_in_bootstrap_95"] for v in out["per_payer"].values()))
    out["n_payers"] = len(out["per_payer"])
    return out


# ------------------------------------------------------------------------------------------
# marts and figures
# ------------------------------------------------------------------------------------------
def write_marts(conn, chain: ChainFit) -> None:
    c = chain.cutoff
    conn.execute("delete from mart.chain_transition_count where cutoff_date = %s", (c,))
    conn.execute("delete from mart.chain_matrix where cutoff_date = %s", (c,))
    conn.execute("delete from mart.chain_absorption where cutoff_date = %s", (c,))
    with conn.cursor() as cur:
        rows_c, rows_m, rows_a = [], [], []
        for g in chain.groups:
            n = chain.counts[g].sum(axis=1)
            for i, a in enumerate(TRANSIENT):
                for j, b in enumerate(STATES):
                    if chain.counts[g][i, j]:
                        rows_c.append((c, g, a, b, int(chain.counts[g][i, j])))
                    rows_m.append((c, g, a, b, float(chain.P[g][i, j]), int(n[i]), bool(chain.pooled_rows[g][i])))
                q = chain.quantities[g]
                bt = chain.boot.get(g, {}) if a == "SUBMITTED" else {}
                rows_a.append((c, g, a, float(q["B"][i, 0]), float(q["B"][i, 1]), bt.get("p_paid_from_submitted_lo"),
                               bt.get("p_paid_from_submitted_hi"), float(q["t"][i]), float(q["sd_t"][i])))
        cur.executemany("insert into mart.chain_transition_count (cutoff_date, payer_id, from_state, to_state, n) "
                        "values (%s, %s, %s, %s, %s)", rows_c)
        cur.executemany("insert into mart.chain_matrix (cutoff_date, payer_id, from_state, to_state, probability, row_n, "
                        "pooled_row) values (%s, %s, %s, %s, %s, %s, %s)", rows_m)
        cur.executemany("insert into mart.chain_absorption (cutoff_date, payer_id, from_state, p_paid, p_written_off, "
                        "p_paid_lo, p_paid_hi, expected_steps, sd_steps) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows_a)
    label_table(conn, "mart.chain_transition_count", labels.SYNTHETIC_RECORD,
                       "Counts of consecutive life-cycle statuses per payer in simulated claims, up to cutoff_date.")
    label_table(conn, "mart.chain_matrix", labels.MODEL_ESTIMATE,
                       "MLE transition matrix of the claim life cycle by payer; sparse rows use the pooled row.")
    label_table(conn, "mart.chain_absorption", labels.MODEL_ESTIMATE,
                       "Absorption probabilities B = N R and expected transitions to absorption by payer and state.")


def figures(chain: ChainFit, fig_dir: Path = FIGURES_DIR) -> list[str]:
    plt = mpl()
    fig_dir.mkdir(parents=True, exist_ok=True)
    out = []
    # 1. pooled transition matrix
    M = chain.P[POOLED][: len(TRANSIENT)]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.grid(False)
    ax.imshow(M, cmap="Greys", vmin=0, vmax=1.4, aspect="auto")
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            if M[i, j] >= 0.0005:
                ax.text(j, i, f"{M[i, j]:.3f}", ha="center", va="center", fontsize=8,
                        color="white" if M[i, j] > 0.7 else "black")
    ax.set_xticks(range(len(STATES)), [s.replace("_", "\n") for s in STATES])
    ax.set_yticks(range(len(TRANSIENT)), TRANSIENT)
    ax.set_xlabel("next status")
    ax.set_ylabel("current status")
    ax.set_title(f"Estimated transition matrix, all payers (cutoff {chain.cutoff})\n"
                 "model_estimate on synthetic_record data", fontsize=9)
    fig.tight_layout()
    p = fig_dir / "markov_transition_matrix.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    # 2. P(PAID) from SUBMITTED by payer with bootstrap interval
    payers = [g for g in chain.groups if g != POOLED]
    est = [chain.p_paid("SUBMITTED", g) for g in payers]
    lo = [chain.boot.get(g, {}).get("p_paid_from_submitted_lo", e) for g, e in zip(payers, est)]
    hi = [chain.boot.get(g, {}).get("p_paid_from_submitted_hi", e) for g, e in zip(payers, est)]
    fig, ax = plt.subplots(figsize=(6.0, 3.2))
    x = np.arange(len(payers))
    ax.errorbar(x, est, yerr=[np.subtract(est, lo), np.subtract(hi, est)], fmt="o", color="black", capsize=4)
    ax.set_xticks(x, payers)
    ax.set_ylabel("P(claim ends PAID | SUBMITTED)")
    ax.set_xlabel("payer (fictional)")
    ax.set_title("Absorption probability with 95% claim-level bootstrap interval\nmodel_estimate, simulated data",
                 fontsize=9)
    fig.tight_layout()
    p = fig_dir / "markov_absorption_by_payer.png"
    fig.savefig(p)
    plt.close(fig)
    out.append(str(p))
    return out


def recovery_figure(evaluation: dict, chain: ChainFit, planted: dict, fig_dir: Path = FIGURES_DIR) -> str:
    """Scatter of estimated against planted transition probabilities (all payers, all cells)."""
    plt = mpl()
    xs, ys = [], []
    for payer, pm in planted["markov_by_payer"].items():
        if payer in chain.P:
            t = np.array(pm["population_matrix"])
            e = chain.P[payer][: len(TRANSIENT)]
            mask = (t > 0) | (e > 0)
            xs += list(t[mask])
            ys += list(e[mask])
    fig, ax = plt.subplots(figsize=(4.2, 4.0))
    ax.plot([0, 1], [0, 1], color="0.5", lw=1, ls="--", label="perfect recovery")
    ax.scatter(xs, ys, s=14, facecolors="none", edgecolors="black", label="cell (payer x transition)")
    ax.set_xlabel("planted probability (generator)")
    ax.set_ylabel("estimated probability")
    ax.set_title(f"Transition matrix recovery\nmax abs error {evaluation['max_abs_error_all_payers']:.3f}", fontsize=9)
    ax.legend(loc="upper left", fontsize=7)
    fig.tight_layout()
    p = fig_dir / "markov_recovery.png"
    fig.savefig(p)
    plt.close(fig)
    return str(p)


# ------------------------------------------------------------------------------------------
# entry point
# ------------------------------------------------------------------------------------------
def _params(cfg: dict) -> dict:
    p = dict(DEFAULTS)
    p.update((cfg or {}).get("markov", {}))
    return p


def run(conn, cfg: dict) -> dict:
    """Fit the chain at the as-of date (and at the hold-out cutoff), write marts and figures."""
    prm = _params(cfg)
    seed = int(cfg.get("seed", 0))
    as_of = to_date(cfg["as_of_date"])
    cutoff = to_date(prm["fit_cutoff"] or as_of)
    ensure_sql(conn)
    events = events_asof(conn, cutoff)
    chain = fit_from_events(events, cutoff, min_row_count=prm["min_row_count"], maturity_days=prm["maturity_days"],
                            n_boot=prm["n_boot"], seed=seed)
    write_marts(conn, chain)
    hold_cut = to_date(prm["holdout_cutoff"])
    hold_chain = fit(conn, hold_cut, min_row_count=prm["min_row_count"], maturity_days=prm["maturity_days"], seed=seed)
    write_marts(conn, hold_chain)
    calib = holdout_calibration(conn, hold_chain, as_of)
    diag = markov_diagnostics(events)
    figs = figures(chain)
    M, E = labels.MODEL_ESTIMATE, labels.SYNTHETIC_RECORD
    per_payer = {}
    for g in chain.groups:
        q = chain.quantities[g]
        per_payer[g] = {
            "matrix": labels.stamp(chain.matrix_frame(g).round(4).to_dict(orient="index"), M),
            "exits_observed_by_state": labels.stamp(dict(zip(TRANSIENT, chain.counts[g].sum(axis=1).astype(int).tolist())), E),
            "rows_using_pooled": [TRANSIENT[k] for k in np.where(chain.pooled_rows[g])[0]],
            "fundamental_matrix_N": labels.stamp(pd.DataFrame(q["N"], index=TRANSIENT, columns=TRANSIENT).round(4).to_dict(orient="index"), M),
            "absorption_B": labels.stamp(pd.DataFrame(q["B"], index=TRANSIENT, columns=ABSORBING).round(4).to_dict(orient="index"), M),
            "expected_transitions_to_absorption": labels.stamp(dict(zip(TRANSIENT, np.round(q["t"], 3).tolist())), M),
            "sd_transitions_to_absorption": labels.stamp(dict(zip(TRANSIENT, np.round(q["sd_t"], 3).tolist())), M),
            "p_paid_from_submitted": labels.stamp(round(float(q["B"][0, 0]), 4), M),
            "p_paid_from_submitted_95": labels.stamp([round(chain.boot.get(g, {}).get("p_paid_from_submitted_lo", np.nan), 4),
                                                      round(chain.boot.get(g, {}).get("p_paid_from_submitted_hi", np.nan), 4)], M,
                                                     note="claim-level bootstrap percentile interval"),
        }
    return {
        "module": "recon_engine.models.markov",
        "question": "Shared building block: life cycle of a claim as an absorbing Markov chain (feeds Q3, Q4, Q5).",
        "label_note": "Simulated data for a fictional hospital. Numbers show parameter recovery and decision logic only.",
        "inputs": {"tables": ["core.fact_claim_event", "core.fact_claim_line", "core.fact_remit_line",
                              "recon.claim_remit_match", "recon.remit_duplicate"],
                   "sql": "sql/models/40_chain.sql", "cutoff": str(cutoff), "as_of_date": str(as_of),
                   "min_row_count": prm["min_row_count"], "maturity_days": prm["maturity_days"], "n_boot": prm["n_boot"]},
        "seed": seed,
        "cutoff": str(cutoff),
        "states": STATES,
        "transition_notes": labels.stamp(chain.notes, E),
        "per_payer": per_payer,
        "holdout_calibration": labels.stamp(calib, M, note="fit at holdout cutoff, outcomes observed up to the as-of date"),
        "markov_assumption_diagnostics": labels.stamp(diag, M, note="chi-square tests of homogeneity; small p-value = memory beyond the current status"),
        "marts": ["mart.chain_transition_count", "mart.chain_matrix", "mart.chain_absorption"],
        "figures": figs,
    }


if __name__ == "__main__":  # pragma: no cover
    from .. import db
    from ..datagen.calibration import model_config

    with db.connect() as c:
        print(json.dumps(run(c, model_config()), indent=1, default=str)[:3000])
