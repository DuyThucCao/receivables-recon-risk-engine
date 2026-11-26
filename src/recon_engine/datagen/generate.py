"""Generate the seven raw documents and the planted ground truth.

How the simulation works (one pass, fixed seed):

1. Reference data: fictional payers, a chargemaster, drugs (CMS payment limits when the public
   calibration file is present), and payer contracts (fee schedules or percent of charges).
2. Claims: encounters by service line, each with 1-5 lines; a patient belongs to one payer.
3. Claim life cycle: an absorbing Markov chain SUBMITTED -> IN_REVIEW -> {PENDED, DENIED, PAID},
   DENIED -> {IN_REVIEW (corrected resubmission), APPEALED, WRITTEN_OFF}, APPEALED -> {PAID, DENIED},
   PENDED -> {IN_REVIEW, WRITTEN_OFF}. The probability of denial is a logistic function of payer,
   service line and amount; the probability that an appeal is overturned is a logistic function
   of payer, denial category, service line and amount. Paths are simulated past the as-of date
   (so the truth knows ultimate outcomes) but only events on or before it are exported.
4. Remittances: every adjudication becomes remittance lines (835-like). A re-adjudication first
   reverses the previous one. After payment a claim may get a partial payment, a reversal and
   corrected repost, a late downward adjustment, or a duplicate payment that is later recouped.
5. Payments: lines are grouped by payer payment run (one trace number per run); runs are paid by
   EFT or by check to a lockbox, where several checks form one deposit.
6. Bank statement: deposits plus card settlements, interest, fees, wholesaler payments and sweeps,
   with a running balance.
7. Drugs: periodic-review purchasing in lots with expiry dates; administrations consume lots
   first-expiry-first-out; lots that expire with stock left produce waste events.
8. Defects are injected and every one is written to ``data/truth``.

All money is handled in integer cents so every identity holds exactly.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import brentq
from scipy.special import expit

from .. import DATA_DIR, RAW_DIR
from .bizcal import add_business_days, is_business_day, next_business_day
from .calibration import Calibration, load_calibration, override_record

TRUTH_DIR = DATA_DIR / "truth"

STATES = ["SUBMITTED", "IN_REVIEW", "PENDED", "DENIED", "APPEALED", "PAID", "WRITTEN_OFF"]
TRANSIENT = STATES[:5]
ABSORBING = STATES[5:]
SI = {s: i for i, s in enumerate(STATES)}
REASONS = ["ADMINISTRATIVE", "ELIGIBILITY", "CODING", "PRIOR_AUTH", "MEDICAL_NECESSITY", "NON_COVERED"]

RAW_COLUMNS: dict[str, list[str]] = {
    "claims_export.csv": [
        "claim_id", "form_type", "bill_type", "facility_name", "facility_npi", "patient_account_no",
        "member_id", "payer_id", "payer_name", "service_line", "attending_provider_id",
        "statement_from_date", "statement_to_date", "submission_date", "claim_total_charge",
        "line_no", "line_item_control_no", "revenue_code", "procedure_code", "modifier_1",
        "ndc_code", "diagnosis_code_1", "diagnosis_code_2", "service_date", "units", "line_charge"],
    "remittance_advice.csv": [
        "remit_file_id", "payer_id", "payer_name", "payer_tax_id", "payee_name", "payee_npi",
        "trace_no", "payment_method", "payment_date", "file_payment_amount", "patient_control_no",
        "payer_claim_control_no", "claim_status_code", "claim_charge_amount", "claim_paid_amount",
        "member_id", "service_date", "procedure_code", "modifier_1", "units", "line_item_control_no",
        "line_charge_amount", "line_allowed_amount", "line_paid_amount",
        "adj_group_1", "adj_reason_1", "adj_amount_1", "adj_group_2", "adj_reason_2", "adj_amount_2",
        "adj_group_3", "adj_reason_3", "adj_amount_3", "remark_code"],
    "bank_statement.csv": [
        "account_number", "posting_date", "value_date", "transaction_type", "description",
        "debit", "credit", "running_balance", "bank_reference", "customer_reference"],
    "payer_contracts.csv": [
        "contract_id", "payer_id", "payer_name", "payer_type", "payer_tax_id", "ach_company_name",
        "procedure_scope", "procedure_code", "rate_type", "rate", "unit_basis", "effective_from",
        "effective_to", "lesser_of_charges", "notes"],
    "drug_purchase_invoices.csv": [
        "invoice_no", "invoice_date", "wholesaler_name", "wholesaler_id", "ship_to_facility",
        "po_number", "invoice_line_no", "ndc_code", "product_description", "lot_no", "expiry_date",
        "qty_packages", "billing_units_per_package", "unit_price", "extended_amount",
        "payment_terms", "due_date", "order_type"],
    "drug_dispense_log.csv": [
        "dispense_id", "event_datetime", "event_type", "facility_name", "ndc_code", "hcpcs_code",
        "drug_description", "lot_no", "billing_units", "patient_account_no", "claim_id",
        "claim_line_no", "recorded_by"],
    "claim_status_history.csv": [
        "event_id", "claim_id", "status_text", "status_datetime", "reason_code", "appeal_level",
        "source_system", "recorded_by", "note"],
}

STATUS_ALIASES = {
    "SUBMITTED": ["SUBMITTED", "Submitted", "SUBMITTED TO CLEARINGHOUSE"],
    "IN_REVIEW": ["IN_REVIEW", "In Review", "ACCEPTED - IN REVIEW"],
    "PENDED": ["PENDED", "Pend - Info Requested", "PEND - INFO REQ"],
    "DENIED": ["DENIED", "Denied", "DENIAL RECEIVED"],
    "APPEALED": ["APPEALED", "Appeal Filed", "APPEAL FILED"],
    "PAID": ["PAID", "Paid", "PAID IN FULL"],
    "WRITTEN_OFF": ["WRITTEN_OFF", "Write Off", "W/O - FINAL"],
}


# --------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------
def c_of(x: float) -> int:
    """Dollars (float from config) to integer cents, half away from zero."""
    return int(math.floor(abs(x) * 100 + 0.5)) * (1 if x >= 0 else -1)


def fmt_c(c: int) -> str:
    """Integer cents to '1234.56' (exact, no float)."""
    s = "-" if c < 0 else ""
    c = abs(c)
    return f"{s}{c // 100}.{c % 100:02d}"


def fmt_835(c: int) -> str:
    """835 style: no trailing zeros ('1234.5', '1234')."""
    s = fmt_c(c)
    if s.endswith(".00"):
        return s[:-3]
    if s.endswith("0"):
        return s[:-1]
    return s


def fmt_comma(c: int) -> str:
    s = "-" if c < 0 else ""
    c = abs(c)
    return f"{s}{c // 100:,}.{c % 100:02d}"


def round_frac(c: int, frac: float) -> int:
    """round(c * frac) in cents, half up, for a non-negative frac."""
    return int(math.floor(c * frac + 0.5))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_csv(path: Path, columns: list[str], rows: list[dict | list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            if isinstance(r, dict):
                w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
            else:
                w.writerow(["" if v is None else v for v in r])


# --------------------------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------------------------
class Generator:
    def __init__(self, cal: Calibration, n_claims: int | None = None, seed: int | None = None):
        self.cal = cal
        V = cal.values
        self.V = V
        self.seed = int(seed if seed is not None else V["simulation"]["seed"])
        self.n_claims = int(n_claims if n_claims is not None else V["simulation"]["n_claims"])
        self.scale = self.n_claims / float(V["simulation"]["n_claims"])
        self.as_of = date.fromisoformat(V["simulation"]["as_of_date"])
        self.start = date.fromisoformat(V["simulation"]["service_start"])
        self.end = date.fromisoformat(V["simulation"]["service_end"])
        streams = np.random.SeedSequence(self.seed).spawn(11)
        (self.r_ent, self.r_claims, self.r_life, self.r_remit, self.r_cash, self.r_drug,
         self.r_mess, self.r_status, self.r_orphan, self.r_twin, self.r_split) = [np.random.default_rng(s) for s in streams]
        self.payers = V["payers"]["roster"]
        self.pid = [p["payer_id"] for p in self.payers]
        self.pidx = {p: i for i, p in enumerate(self.pid)}
        self.org = V["organisation"]
        self.truth: dict[str, Any] = {}

    # ---------------------------------------------------------------- reference data
    def build_reference(self) -> None:
        V, r = self.V, self.r_ent
        self.procs: dict[str, dict] = {}
        for p in V["pricing"]["chargemaster_2024"]:
            self.procs[p["code"]] = {**p, "charge_c": c_of(p["charge"]), "is_drug": False}
        markup_lo, markup_hi = V["pricing"]["drug_charge_markup_range"]
        acq_lo, acq_hi = V["drugs"]["acquisition_cost_ratio_range"]
        pol = V["drugs"]["inventory_policy"]
        dose_tab = V["drugs"]["dose_units"]
        self.drugs: list[dict] = []
        for i, d in enumerate(V["drugs"]["reference_list"]):
            limit = float(d["payment_limit_usd"])
            dose = int(dose_tab.get(d["hcpcs"], max(1, round(5000.0 / limit))))
            pkg = dose if dose <= 10 else int(math.ceil(dose / 2))
            markup = float(r.uniform(markup_lo, markup_hi))
            drug = {
                "idx": i, "hcpcs": d["hcpcs"], "desc": d["short_description"], "dosage": d["dosage"],
                "limit": limit, "area": d.get("therapeutic_area", "oncology"), "dose_units": dose,
                "package_units": pkg, "markup": markup,
                "charge_unit_c": max(1, c_of(limit * markup)),
                "acq_ratio": float(r.uniform(acq_lo, acq_hi)),
                "overstock": float(r.uniform(*pol["overstock_range"])),
                "shelf_life_days": int(r.integers(pol["shelf_life_days_range"][0], pol["shelf_life_days_range"][1] + 1)),
                "ndc": f"9{i + 1:04d}-{pkg % 10000:04d}-01",
            }
            self.drugs.append(drug)
            self.procs[d["hcpcs"]] = {"code": d["hcpcs"], "description": d["short_description"],
                                      "service_line": "DRUG", "revenue_code": "0636",
                                      "charge_c": drug["charge_unit_c"], "is_drug": True, "drug_idx": i}
        self.drug_by_area: dict[str, list[int]] = {}
        for d in self.drugs:
            self.drug_by_area.setdefault(d["area"], []).append(d["idx"])

        # contracts: rate in cents per unit for fee schedules, basis points for percent of charge
        terms = V["pricing"]["contract_terms"]
        noise = V["pricing"]["fee_schedule_code_noise"]
        esc = V["pricing"]["contract_escalator_2025"]
        self.contract: dict[tuple[str, str], dict] = {}   # (payer, code) -> {2024: c, 2025: c}
        self.contract_pct: dict[str, int] = {}
        for p in self.pid:
            t = terms[p]
            if t["rate_type"] == "PCT_OF_CHARGE":
                self.contract_pct[p] = int(round(float(t["pct_of_charge"]) * 100))
                continue
            for code, pr in self.procs.items():
                if pr["is_drug"]:
                    d = self.drugs[pr["drug_idx"]]
                    r24 = max(1, c_of(d["limit"] * float(t["drug_pct_of_reference"])))
                else:
                    f = float(r.uniform(1 - noise, 1 + noise))
                    r24 = max(1, round_frac(pr["charge_c"], float(t["fee_schedule_factor"]) * f))
                r25 = round_frac(r24, 1.0 + esc)
                self.contract[(p, code)] = {2024: r24, 2025: r25}

    def contract_allowed(self, payer: str, code: str, sdate: date, units: int, charge_c: int) -> int:
        if payer in self.contract_pct:
            a = (self.contract_pct[payer] * charge_c + 5000) // 10000
        else:
            rate = self.contract[(payer, code)][2024 if sdate.year <= 2024 else 2025]
            a = rate * units
        return min(a, charge_c)

    # ---------------------------------------------------------------- patients and claims
    def build_claims(self) -> None:
        V, r = self.V, self.r_claims
        n = self.n_claims
        shares = np.array([p["share"] for p in self.payers], dtype=float)
        shares /= shares.sum()
        n_pat = max(10, int(round(n / float(V["simulation"]["claims_per_patient"]))))
        pat_payer = r.choice(len(self.pid), size=n_pat, p=shares)
        mem_nums = list(dict.fromkeys(r.integers(10**8, 10**9, size=n_pat * 2).tolist()))[:n_pat]
        acct_nums = list(dict.fromkeys(r.integers(10**7, 10**8, size=n_pat * 2).tolist()))[:n_pat]
        self.patients = [{"payer_idx": int(pat_payer[i]),
                          "member_id": f"{self.payers[pat_payer[i]]['member_prefix']}{mem_nums[i]}",
                          "account": f"A{acct_nums[i]}"} for i in range(n_pat)]

        sl_share = V["service_mix"]["service_line_share"]
        sl_names = list(sl_share)
        slp = np.array([sl_share[s] for s in sl_names], float)
        slp /= slp.sum()
        days = [self.start + timedelta(days=k) for k in range((self.end - self.start).days + 1)]
        w_all = np.ones(len(days))
        w_wk = np.array([1.0 if is_business_day(d) else 0.1 for d in days])
        w_all /= w_all.sum()
        w_wk /= w_wk.sum()
        sl_of = r.choice(len(sl_names), size=n, p=slp)
        day_ed = r.choice(len(days), size=n, p=w_all)
        day_wk = r.choice(len(days), size=n, p=w_wk)
        pat_of = r.integers(0, n_pat, size=n)
        tie = r.random(n)
        templates = V["service_mix"]["templates"]
        geo_p = float(V["service_mix"]["submission_lag_geometric_p"])
        inc25 = float(V["pricing"]["chargemaster_increase_2025"])
        area_w = V["drugs"]["area_weights"]
        dose_var = float(V["drugs"]["dose_variation"])
        rep_p = float(V["service_mix"]["repeat_line_probability"])

        raw_claims = []
        for k in range(n):
            sl = sl_names[sl_of[k]]
            sdate = days[day_ed[k]] if sl == "EMERGENCY" else days[day_wk[k]]
            raw_claims.append((sdate, tie[k], k, sl))
        raw_claims.sort()

        self.claims: list[dict] = []
        seq_by_year: dict[int, int] = {}
        for sdate, _, k, sl in raw_claims:
            tpl = templates[sl]
            pat = self.patients[pat_of[k]]
            payer = self.pid[pat["payer_idx"]]
            yy = sdate.year % 100
            seq_by_year[sdate.year] = seq_by_year.get(sdate.year, 0) + 1
            claim_id = f"MHS{yy:02d}{seq_by_year[sdate.year]:07d}"
            lines: list[dict] = []
            diag2 = ""
            if "drug_areas" in tpl:
                areas = [a for a in tpl["drug_areas"] if a in self.drug_by_area]
                if not areas:
                    areas = list(self.drug_by_area)
                aw = np.array([float(area_w.get(a, 0.05)) for a in areas])
                area = areas[r.choice(len(areas), p=aw / aw.sum())]
                didx = self.drug_by_area[area][r.integers(0, len(self.drug_by_area[area]))]
                drug = self.drugs[didx]
                if sl == "OPHTHALMOLOGY":
                    codes = ["67028"]
                elif area == "oncology":
                    codes = ["96413"] + (["96415"] if r.random() < 0.4 else [])
                else:
                    codes = ["96365"]
                units = drug["dose_units"]
                if units > 10:
                    units = max(1, int(round(units * r.uniform(1 - dose_var, 1 + dose_var))))
                codes_units = [(c, 1) for c in codes] + [(drug["hcpcs"], units)]
                dlist = tpl.get("diagnoses_by_area", {}).get(area) or tpl.get("diagnoses") or ["Z51.11"]
                diag1 = dlist[r.integers(0, len(dlist))]
            else:
                prim = tpl["primary"]
                pc = list(prim)
                pw = np.array([prim[c] for c in pc], float)
                codes_units = [(pc[r.choice(len(pc), p=pw / pw.sum())], 1)]
                diag1 = tpl["diagnoses"][r.integers(0, len(tpl["diagnoses"]))]
                if r.random() < 0.3:
                    diag2 = tpl["diagnoses"][r.integers(0, len(tpl["diagnoses"]))]
            for code, prob in tpl.get("ancillary", {}).items():
                if r.random() < prob and all(code != c for c, _ in codes_units):
                    codes_units.append((code, 1))
            if sl in ("EMERGENCY", "LABORATORY", "PRIMARY_CARE", "INFUSION") and r.random() < rep_p:
                lab = next((c_ for c_, _ in codes_units if c_ in ("85025", "80053")), None)
                if lab:
                    codes_units.append((lab, 1))   # same test billed twice, legitimately
            for ln, (code, units) in enumerate(codes_units, start=1):
                pr = self.procs[code]
                unit_c = pr["charge_c"] if sdate.year <= 2024 else round_frac(pr["charge_c"], 1 + inc25)
                charge = unit_c * units
                lines.append({
                    "line_no": ln, "code": code, "units": units, "charge_c": charge,
                    "revenue_code": pr["revenue_code"],
                    "ndc": self.drugs[pr["drug_idx"]]["ndc"] if pr["is_drug"] else "",
                    "is_drug": pr["is_drug"], "drug_idx": pr.get("drug_idx"),
                    "lic": f"{claim_id}-{ln:02d}",
                    "contract_allowed_c": self.contract_allowed(payer, code, sdate, units, charge),
                })
            lag = int(min(30, r.geometric(geo_p)))
            self.claims.append({
                "idx": len(self.claims), "claim_id": claim_id, "payer": payer,
                "payer_idx": self.pidx[payer], "patient": pat, "service_line": sl,
                "service_date": sdate, "submission_date": sdate + timedelta(days=lag),
                "facility": tpl["facility"], "form_type": tpl["form_type"],
                "diag1": diag1, "diag2": diag2, "lines": lines,
                "total_c": sum(l["charge_c"] for l in lines),
                "attending": f"PRV{int(r.integers(1000, 1060))}",
            })
        self.seq_by_year = seq_by_year

    def build_twins(self) -> None:
        """Same patient, same day, same single service and charge on two separate claims. The
        payer will print a transposed claim number on the original's remittances."""
        r = self.r_twin
        n = max(1, int(round(int(self.V["service_mix"]["twin_encounters"]) * self.scale)))
        cands = [c for c in self.claims if c["service_line"] in ("PRIMARY_CARE", "LABORATORY") and len(c["lines"]) == 1]
        pick = r.choice(len(cands), size=min(n, len(cands)), replace=False)
        for j in sorted(int(x) for x in pick):
            o = cands[j]
            yr = o["service_date"].year
            self.seq_by_year[yr] += 1
            cid = f"MHS{yr % 100:02d}{self.seq_by_year[yr]:07d}"
            lines = [dict(l, lic=f"{cid}-{l['line_no']:02d}") for l in o["lines"]]
            twin = dict(o, idx=len(self.claims), claim_id=cid, lines=lines,
                        submission_date=o["submission_date"] + timedelta(days=1), twin_of=o["claim_id"])
            twin.pop("force_transposed", None)
            o["force_transposed"] = True
            self.claims.append(twin)

    # ---------------------------------------------------------------- denial and overturn models
    def build_models(self) -> None:
        V = self.V
        den = V["denial"]
        tot = np.array([c["total_c"] / 100.0 for c in self.claims])
        lnc = np.log(tot)
        self.ln_center = float(lnc.mean())
        z = lnc - self.ln_center
        sl = [c["service_line"] for c in self.claims]
        b_sl = np.array([den["service_line_logit"][s] for s in sl])
        eta = b_sl + float(den["amount_logit"]) * z
        payer_idx = np.array([c["payer_idx"] for c in self.claims])
        targets = den["first_pass_denial_rate"]
        self.alpha: dict[str, float] = {}
        q = np.zeros(len(self.claims))
        for p, pi in self.pidx.items():
            m = payer_idx == pi
            if not m.any():
                self.alpha[p] = 0.0
                continue
            tgt = float(targets[p])
            a = brentq(lambda a_: float(expit(a_ + eta[m]).mean()) - tgt, -20, 20)
            self.alpha[p] = float(a)
            q[m] = expit(a + eta[m])
        self.q = q
        self.z = z

        act = den["hospital_action_by_reason"]
        self.reason_mix: dict[str, np.ndarray] = {}
        self.w_appeal: dict[str, np.ndarray] = {}
        for p in self.pid:
            mix = den["reason_mix"].get(p, den["reason_mix"]["default"])
            mv = np.array([float(mix[c]) for c in REASONS])
            mv /= mv.sum()
            self.reason_mix[p] = mv
            av = np.array([float(act[c]["appeal"]) for c in REASONS])
            self.w_appeal[p] = mv * av / float((mv * av).sum())
        g_c = np.array([float(den["overturn_reason_logit"][c]) for c in REASONS])
        g_sl = np.array([den["overturn_service_line_logit"][s] for s in sl])
        g_amt = float(den["overturn_amount_logit"])
        base = g_sl + g_amt * z     # per claim, excluding payer intercept and reason
        self.gamma: dict[str, float] = {}
        self.o_reason = np.zeros((len(self.claims), len(REASONS)))
        obar = np.zeros(len(self.claims))
        for p, pi in self.pidx.items():
            m = payer_idx == pi
            if not m.any():
                self.gamma[p] = 0.0
                continue
            tgt = float(den["overturn_target"][p])
            w = self.w_appeal[p]
            qm = q[m]

            def gap(g: float) -> float:
                ob = (expit(g + base[m][:, None] + g_c[None, :]) * w[None, :]).sum(axis=1)
                return float((qm * ob).sum() / qm.sum()) - tgt

            g = brentq(gap, -20, 20)
            self.gamma[p] = float(g)
            self.o_reason[m] = expit(g + base[m][:, None] + g_c[None, :])
            obar[m] = (self.o_reason[m] * w[None, :]).sum(axis=1)
        self.obar = obar
        self.payer_idx_arr = payer_idx

    def population_matrices(self) -> dict[str, Any]:
        """Analytic large-sample limit of the count-based transition estimator, by payer.

        For claim i with matrix P_i, the expected number of visits to each transient state from
        SUBMITTED is row 0 of N_i = (I - Q_i)^-1. The pooled MLE of P[s, t] converges to
        sum_i N_i[0, s] P_i[s, t] / sum_i N_i[0, s] (visit-weighted average).
        """
        den = self.V["denial"]
        act = den["hospital_action_by_reason"]
        wo_pend = float(den["pended_writeoff_probability"])
        out = {}
        for p, pi in self.pidx.items():
            m = np.where(self.payer_idx_arr == pi)[0]
            if len(m) == 0:
                continue
            mix = self.reason_mix[p]
            r_resub = float(sum(mix[j] * act[c]["resubmit"] for j, c in enumerate(REASONS)))
            r_app = float(sum(mix[j] * act[c]["appeal"] for j, c in enumerate(REASONS)))
            r_wo = float(sum(mix[j] * act[c]["write_off"] for j, c in enumerate(REASONS)))
            pend = float(den["pend_probability"][p])
            k = len(m)
            P = np.zeros((k, 7, 7))
            P[:, SI["SUBMITTED"], SI["IN_REVIEW"]] = 1.0
            P[:, SI["IN_REVIEW"], SI["PENDED"]] = pend
            P[:, SI["IN_REVIEW"], SI["DENIED"]] = (1 - pend) * self.q[m]
            P[:, SI["IN_REVIEW"], SI["PAID"]] = (1 - pend) * (1 - self.q[m])
            P[:, SI["PENDED"], SI["IN_REVIEW"]] = 1 - wo_pend
            P[:, SI["PENDED"], SI["WRITTEN_OFF"]] = wo_pend
            P[:, SI["DENIED"], SI["IN_REVIEW"]] = r_resub
            P[:, SI["DENIED"], SI["APPEALED"]] = r_app
            P[:, SI["DENIED"], SI["WRITTEN_OFF"]] = r_wo
            P[:, SI["APPEALED"], SI["PAID"]] = self.obar[m]
            P[:, SI["APPEALED"], SI["DENIED"]] = 1 - self.obar[m]
            P[:, 5, 5] = 1.0
            P[:, 6, 6] = 1.0
            Q = P[:, :5, :5]
            R = P[:, :5, 5:]
            N = np.linalg.inv(np.eye(5)[None, :, :] - Q)
            visits = N[:, 0, :]                       # (k, 5)
            trans = visits[:, :, None] * P[:, :5, :]  # expected transition counts
            pop = trans.sum(axis=0) / visits.sum(axis=0)[:, None]
            B = np.einsum("kij,kjl->kil", N, R)[:, 0, :]  # absorption probs from SUBMITTED
            out[p] = {
                "states": STATES,
                "transient_rows": TRANSIENT,
                "population_matrix": [[round(float(x), 6) for x in row] for row in pop],
                "mean_claim_matrix": [[round(float(x), 6) for x in row] for row in P[:, :5, :].mean(axis=0)],
                "expected_visits_from_submitted": dict(zip(TRANSIENT, [round(float(x), 6) for x in visits.mean(axis=0)])),
                "absorption_probability_paid": round(float(B[:, 0].mean()), 6),
                "absorption_probability_written_off": round(float(B[:, 1].mean()), 6),
                "n_claims": int(k),
            }
        return out

    # ---------------------------------------------------------------- payer runs
    def build_runs(self) -> None:
        horizon = self.as_of + timedelta(days=500)
        self.runs: dict[str, np.ndarray] = {}
        cyc = self.V["payers"]["payment_cycle_weekdays"]
        d0 = self.start - timedelta(days=7)
        for p in self.pid:
            wd = set(cyc[p])
            ords = []
            d = d0
            while d <= horizon:
                if d.weekday() in wd:
                    ords.append(d.toordinal())
                d += timedelta(days=1)
            self.runs[p] = np.array(ords)

    def next_run(self, payer: str, d: date) -> date:
        arr = self.runs[payer]
        i = int(np.searchsorted(arr, d.toordinal(), side="right"))
        return date.fromordinal(int(arr[min(i, len(arr) - 1)]))

    # ---------------------------------------------------------------- life cycle and remittances
    def simulate_lifecycle(self) -> None:
        V, r = self.V, self.r_life
        den, tim = V["denial"], V["timing"]
        act = den["hospital_action_by_reason"]
        wo_pend = float(den["pended_writeoff_probability"])
        codes = den["reason_codes"]
        lagg = tim["adjudication_lag_gamma"]
        u = lambda lo_hi: float(r.uniform(lo_hi[0], lo_hi[1]))  # noqa: E731
        self.events: list[tuple] = []     # (claim_idx, status, datetime, reason_code, appeal_level, source, note)
        for c in self.claims:
            i, p = c["idx"], c["payer"]
            pend = float(den["pend_probability"][p])
            mix = self.reason_mix[p]
            t = datetime.combine(c["submission_date"], datetime.min.time()) + timedelta(hours=u((8, 18)))
            ev = [(i, "SUBMITTED", t, "", "", "CLEARINGHOUSE", "")]
            t = t + timedelta(days=int(r.integers(tim["ack_days_range"][0], tim["ack_days_range"][1] + 1)),
                              hours=u((0, 6)))
            ev.append((i, "IN_REVIEW", t, "", "", "CLEARINGHOUSE", "277CA ACCEPTED"))
            state, reason, carc, n_app = "IN_REVIEW", None, None, 0
            adjs: list[dict] = []
            path = ["SUBMITTED", "IN_REVIEW"]
            steps = 0
            while state not in ABSORBING and steps < 60:
                steps += 1
                if state == "IN_REVIEW":
                    lag = timedelta(days=float(r.gamma(lagg[p]["shape"], lagg[p]["scale_days"])))
                    if r.random() < pend:
                        t += lag * 0.5
                        state = "PENDED"
                        ev.append((i, "PENDED", t, "", "", "PAYER_PORTAL", "ADDITIONAL INFORMATION REQUESTED"))
                    elif r.random() < self.q[i]:
                        t += lag
                        reason = REASONS[r.choice(6, p=mix)]
                        opts = codes[reason]
                        carc = tuple(opts[r.integers(0, len(opts))])
                        state = "DENIED"
                        ev.append((i, "DENIED", t, f"{carc[0]}-{carc[1]}", "", "PAYER_PORTAL", ""))
                        adjs.append({"kind": "DENY", "decision": t, "reason": reason, "carc": carc})
                    else:
                        t += lag
                        state = "PAID"
                        adjs.append({"kind": "PAY", "decision": t})
                elif state == "PENDED":
                    t += timedelta(days=u(tim["pend_resolution_days"]))
                    if r.random() < wo_pend:
                        state = "WRITTEN_OFF"
                        ev.append((i, "WRITTEN_OFF", t, "", "", "WORKQUEUE", "TIMELY FILING - INFO NOT RECEIVED"))
                    else:
                        state = "IN_REVIEW"
                        ev.append((i, "IN_REVIEW", t, "", "", "PAYER_PORTAL", "INFORMATION RECEIVED"))
                elif state == "DENIED":
                    a = act[reason]
                    x = r.random()
                    if x < a["resubmit"]:
                        t += timedelta(days=u(tim["resubmit_days"]))
                        state = "IN_REVIEW"
                        ev.append((i, "IN_REVIEW", t, "", "", "CLEARINGHOUSE", "CORRECTED CLAIM RESUBMITTED"))
                    elif x < a["resubmit"] + a["appeal"]:
                        t += timedelta(days=u(tim["appeal_filing_days"]))
                        n_app += 1
                        state = "APPEALED"
                        ev.append((i, "APPEALED", t, f"{carc[0]}-{carc[1]}", str(n_app), "WORKQUEUE", ""))
                    else:
                        t += timedelta(days=u(tim["write_off_days"]))
                        state = "WRITTEN_OFF"
                        ev.append((i, "WRITTEN_OFF", t, f"{carc[0]}-{carc[1]}", "", "WORKQUEUE", "FINAL DENIAL WRITE OFF"))
                elif state == "APPEALED":
                    t += timedelta(days=u(tim["appeal_decision_days"]))
                    if r.random() < self.o_reason[i, REASONS.index(reason)]:
                        state = "PAID"
                        adjs.append({"kind": "PAY", "decision": t, "overturn": True})
                    else:
                        reason = REASONS[r.choice(6, p=mix)]
                        opts = codes[reason]
                        carc = tuple(opts[r.integers(0, len(opts))])
                        state = "DENIED"
                        ev.append((i, "DENIED", t, f"{carc[0]}-{carc[1]}", str(n_app), "PAYER_PORTAL", "APPEAL UPHELD"))
                path.append(state)
            # payment run dates; a later adjudication never precedes an earlier one
            prev = None
            for a_ in adjs:
                rd = self.next_run(p, a_["decision"].date())
                if prev is not None and rd < prev:
                    rd = prev
                a_["run"] = rd
                prev = rd
            if state == "PAID":
                ev.append((i, "PAID", datetime.combine(adjs[-1]["run"], datetime.min.time()) + timedelta(hours=9),
                           "", "", "835", ""))
            c["adjudications"] = adjs
            c["path"] = path
            c["final_state"] = state
            self.events.extend(ev)

    def _pay_lines(self, c: dict, allowed_override: dict | None = None) -> list[dict]:
        out = []
        for l in c["lines"]:
            allowed = c["payer_allowed"][l["line_no"]] if allowed_override is None else allowed_override[l["line_no"]]
            pr = round_frac(allowed, c["pr_share"]) if c["pr_flag"] else 0
            paid = allowed - pr
            ca = l["charge_c"] - allowed
            adj = []
            if ca:
                adj.append(("CO", "45", ca))
            if pr:
                if l["code"].startswith("992") and c["payer"] != "P04":
                    adj.append(("PR", "3", pr))
                elif c["service_date"].month <= 3:
                    adj.append(("PR", "1", pr))
                else:
                    adj.append(("PR", "2", pr))
            out.append({"line_no": l["line_no"], "status": "1", "charge": l["charge_c"],
                        "allowed": allowed, "paid": paid, "adj": adj})
        return out

    @staticmethod
    def _negate(lines: list[dict]) -> list[dict]:
        return [{"line_no": l["line_no"], "status": "22", "charge": -l["charge"], "allowed": -l["allowed"],
                 "paid": -l["paid"], "adj": [(g, rc, -a) for g, rc, a in l["adj"]]} for l in lines]

    def build_remittances(self) -> None:
        V, r = self.V, self.r_remit
        und = V["underpayment"]["behaviour"]
        conc = float(V["underpayment"]["beta_concentration"])
        prr = V["pricing"]["patient_responsibility"]
        post = V["remit_behaviour"]["post_payment_events"]
        rb = V["remit_behaviour"]
        self.remit: list[dict] = []
        self.icn_seq = 0

        def new_icn(payer: str, run: date) -> str:
            self.icn_seq += 1
            return f"{self.pidx[payer] + 1:02d}{run:%y%j}{self.icn_seq:07d}"

        def emit(c: dict, lines: list[dict], run: date, kind: str, icn: str) -> None:
            part = len(self.remit)
            for l in lines:
                self.remit.append({"claim_idx": c["idx"], "line_no": l["line_no"], "payer": c["payer"],
                                   "run": run, "kind": kind, "status": l["status"], "charge": l["charge"],
                                   "allowed": l["allowed"], "paid": l["paid"], "adj": list(l["adj"]),
                                   "icn": icn, "part": part, "seq": len(self.remit)})

        for c in self.claims:
            p = c["payer"]
            ptype = self.payers[c["payer_idx"]]["payer_type"]
            ub = und[p]
            c["underpaid"] = bool(r.random() < ub["probability"])
            m_ = float(ub["mean_shortfall"])
            c["shortfall"] = float(r.beta(m_ * conc, (1 - m_) * conc)) if c["underpaid"] else 0.0
            pr_cfg = prr[ptype]
            c["pr_flag"] = bool(r.random() < pr_cfg["probability"])
            c["pr_share"] = float(r.uniform(*pr_cfg["share_range"])) if c["pr_flag"] else 0.0
            c["payer_allowed"] = {l["line_no"]: l["contract_allowed_c"] - round_frac(l["contract_allowed_c"], c["shortfall"])
                                  for l in c["lines"]}
            active, active_icn = None, None
            for k, a in enumerate(c["adjudications"]):
                run = a["run"]
                if active is not None:
                    emit(c, self._negate(active), run, "REVERSAL", active_icn)
                icn = new_icn(p, run)
                if a["kind"] == "DENY":
                    lines = [{"line_no": l["line_no"], "status": "4", "charge": l["charge_c"], "allowed": 0,
                              "paid": 0, "adj": [(a["carc"][0], a["carc"][1], l["charge_c"])]} for l in c["lines"]]
                    emit(c, lines, run, "DENIAL", icn)
                    active, active_icn = lines, icn
                else:
                    lines = self._pay_lines(c)
                    is_final = k == len(c["adjudications"]) - 1
                    kind = "REPOST" if active is not None else "PAYMENT"
                    if is_final:
                        pe = post[p]
                        names = ["partial", "reversal_repost", "late_adjustment", "double_payment"]
                        probs = [float(pe[n_]) for n_ in names]
                        x = r.random()
                        acc, ev = 0.0, "none"
                        for n_, pr_ in zip(names, probs):
                            acc += pr_
                            if x < acc:
                                ev = n_
                                break
                        c["post_event"] = ev
                    else:
                        ev = "none"
                    if ev == "partial":
                        f = float(r.uniform(*rb["partial_paid_fraction_range"]))
                        remainder = {}
                        for l in lines:
                            first = round_frac(l["paid"], f)
                            remainder[l["line_no"]] = l["paid"] - first
                            l["paid"] = first
                        emit(c, lines, run, "PARTIAL", icn)
                        if r.random() < float(rb["partial_supplement_probability"]):
                            run2 = self.next_run(p, run + timedelta(days=int(r.integers(14, 61))))
                            sup = [{"line_no": l["line_no"], "status": "1", "charge": cl["charge_c"], "allowed": 0,
                                    "paid": remainder[l["line_no"]], "adj": []}
                                   for l, cl in zip(lines, c["lines"]) if remainder[l["line_no"]] > 0]
                            if sup:
                                emit(c, sup, run2, "SUPPLEMENTAL", new_icn(p, run2))
                        active, active_icn = lines, icn
                        continue
                    emit(c, lines, run, kind, icn)
                    active, active_icn = lines, icn
                    if ev == "reversal_repost":
                        run2 = self.next_run(p, run + timedelta(days=int(r.integers(20, 91))))
                        emit(c, self._negate(lines), run2, "REVERSAL", icn)
                        fac = float(r.uniform(0.97, 1.03))
                        new_allowed = {l["line_no"]: min(cl["charge_c"], round_frac(c["payer_allowed"][l["line_no"]], fac))
                                       for l, cl in zip(lines, c["lines"])}
                        rep = self._pay_lines(c, new_allowed)
                        icn2 = new_icn(p, run2)
                        emit(c, rep, run2, "REPOST", icn2)
                        active, active_icn = rep, icn2
                    elif ev == "late_adjustment":
                        run2 = self.next_run(p, run + timedelta(days=int(r.integers(30, 121))))
                        j = int(r.integers(0, len(lines)))
                        l = lines[j]
                        x_ = max(1, round_frac(l["paid"], float(r.uniform(*rb["late_adjustment_fraction_range"]))))
                        if l["paid"] > x_:
                            la = [{"line_no": l["line_no"], "status": "1", "charge": c["lines"][j]["charge_c"],
                                   "allowed": -x_, "paid": -x_, "adj": [("CO", "45", x_)]}]
                            emit(c, la, run2, "LATE_ADJUSTMENT", new_icn(p, run2))
                    elif ev == "double_payment":
                        run2 = self.next_run(p, run + timedelta(days=int(r.integers(7, 31))))
                        icn2 = new_icn(p, run2)
                        dup = [dict(l, adj=list(l["adj"])) for l in lines]
                        emit(c, dup, run2, "DOUBLE_PAYMENT", icn2)
                        if r.random() < float(rb["double_payment_recoup_probability"]):
                            run3 = self.next_run(p, run2 + timedelta(days=int(r.integers(20, 81))))
                            emit(c, self._negate(dup), run3, "RECOUPMENT", icn2)

    def build_orphans(self) -> None:
        """Remittance lines for another facility's claims, paid to us by payer error."""
        r = self.r_orphan
        n = max(1, int(round(int(self.V["remit_behaviour"]["orphan_remit_claims"]) * self.scale)))
        codes = [c for c, p in self.procs.items() if not p["is_drug"]]
        self.orphans = []
        for k in range(n):
            p = self.pid[int(r.integers(0, 5))]
            run = self.next_run(p, self.start + timedelta(days=int(r.integers(60, (self.as_of - self.start).days - 60))))
            sdate = run - timedelta(days=int(r.integers(20, 60)))
            cid = f"MHN{sdate.year % 100:02d}{int(r.integers(1, 9_000_000)):07d}"
            member = f"{self.payers[self.pidx[p]]['member_prefix']}{int(r.integers(10**8, 10**9))}"
            icn = f"{self.pidx[p] + 1:02d}{run:%y%j}9{k:06d}"
            part = len(self.remit)
            for ln in range(1, int(r.integers(1, 3)) + 1):
                code = codes[int(r.integers(0, len(codes)))]
                charge = self.procs[code]["charge_c"]
                allowed = round_frac(charge, 0.5)
                self.remit.append({"claim_idx": None, "line_no": ln, "payer": p, "run": run, "kind": "ORPHAN",
                                   "status": "1", "charge": charge, "allowed": allowed, "paid": allowed,
                                   "adj": [("CO", "45", charge - allowed)], "icn": icn, "part": part,
                                   "seq": len(self.remit), "orphan": {"claim_id": cid, "member": member,
                                                                      "sdate": sdate, "code": code,
                                                                      "lic": f"{cid}-{ln:02d}"}})

    # ---------------------------------------------------------------- traces
    def build_traces(self) -> None:
        V, r = self.V, self.r_cash
        chk_share = V["payers"]["check_payment_share"]
        split_p = V["remit_behaviour"]["split_835_probability"]
        cd = V["cash_defects"]
        by_run: dict[tuple[str, date], list[dict]] = {}
        for l in self.remit:
            by_run.setdefault((l["payer"], l["run"]), []).append(l)
        self.traces: list[dict] = []
        used_ach, used_chk = set(), set()
        for p in self.pid:
            pending: list[dict] = []
            for run in sorted(d for (pp, d) in by_run if pp == p):
                lines = pending + by_run[(p, run)]
                for l in pending:
                    l["run"] = run
                    l["carried"] = True
                pending = []
                total = sum(l["paid"] for l in lines)
                if total < 0:
                    pending = lines
                    continue
                if total == 0:
                    method = "NON"
                else:
                    method = "CHK" if r.random() < float(chk_share[p]) else "ACH"
                if method == "CHK":
                    while True:
                        tn = str(int(r.integers(10**7, 10**8)))
                        if (p, tn) not in used_chk:
                            used_chk.add((p, tn))
                            break
                else:
                    while True:
                        tn = str(int(r.integers(10**9, 10**10)))
                        if tn not in used_ach:
                            used_ach.add(tn)
                            break
                tr = {"payer": p, "trace_no": tn, "run": run, "method": method, "total": total,
                      "lines": sorted(lines, key=lambda x: x["seq"]), "remit_missing": False,
                      "deposit_missing": False, "files": []}
                # split into one remittance file per facility?
                facs = {}
                for l in tr["lines"]:
                    f = self.claims[l["claim_idx"]]["facility"] if l["claim_idx"] is not None else "F01"
                    facs.setdefault(f, []).append(l)
                if total > 0 and len(facs) > 1 and r.random() < float(split_p[p]) and all(
                        sum(x["paid"] for x in v) >= 0 for v in facs.values()):
                    groups = [facs[f] for f in sorted(facs)]
                else:
                    groups = [tr["lines"]]
                for gi, g in enumerate(groups):
                    fid = f"ERA{self.pidx[p] + 1:02d}{run:%Y%m%d}{tn[-4:]}{gi + 1}"
                    amt = sum(x["paid"] for x in g)
                    tr["files"].append({"file_id": fid, "amount": amt})
                    for x in g:
                        x["file_id"] = fid
                        x["file_amount"] = amt
                if total > 0:
                    x = r.random()
                    if method == "ACH" and x < float(cd["remit_missing_trace_rate"]):
                        tr["remit_missing"] = True
                    elif r.random() < float(cd["deposit_missing_trace_rate"]):
                        tr["deposit_missing"] = True
                for l in tr["lines"]:
                    l["trace"] = tr
                self.traces.append(tr)
            # periodic interim payments for this payer (provider-level, no claim lines)
            pip = V["cash_defects"]["periodic_interim_payment"]
            if pip and pip.get("payer_id") == p:
                d = date.fromisoformat(pip["start"])
                amt = c_of(float(pip["amount"]))
                while d <= self.as_of + timedelta(days=30):
                    if d.weekday() in pip["weekdays"]:
                        while True:
                            tn = str(int(r.integers(10**9, 10**10)))
                            if tn not in used_ach:
                                used_ach.add(tn)
                                break
                        line = {"claim_idx": None, "line_no": 1, "payer": p, "run": d, "kind": "PROVIDER_LEVEL",
                                "status": "PLB", "charge": 0, "allowed": 0, "paid": amt, "adj": [], "icn": "",
                                "part": -1 - len(self.traces), "seq": 10**9 + len(self.traces), "pip": True}
                        fid = f"ERA{self.pidx[p] + 1:02d}{d:%Y%m%d}{tn[-4:]}P"
                        tr = {"payer": p, "trace_no": tn, "run": d, "method": "ACH", "total": amt, "lines": [line],
                              "remit_missing": False, "deposit_missing": False, "files": [{"file_id": fid, "amount": amt}],
                              "pip": True}
                        line["file_id"], line["file_amount"], line["trace"] = fid, amt, tr
                        self.traces.append(tr)
                    d += timedelta(days=1)
            # trailing negative balance never paid: keep as an unpaid notification trace
            if pending:
                tn = str(int(r.integers(10**9, 10**10)))
                tr = {"payer": p, "trace_no": tn, "run": pending[-1]["run"], "method": "NON",
                      "total": sum(l["paid"] for l in pending), "lines": pending, "remit_missing": False,
                      "deposit_missing": False, "files": [], "negative_carry": True}
                fid = f"ERA{self.pidx[p] + 1:02d}{tr['run']:%Y%m%d}{tn[-4:]}1"
                tr["files"].append({"file_id": fid, "amount": tr["total"]})
                for l in pending:
                    l["file_id"], l["file_amount"], l["trace"] = fid, tr["total"], tr
                self.traces.append(tr)

    # ---------------------------------------------------------------- drugs
    def build_drugs(self) -> None:
        V, r = self.V, self.r_drug
        pol = V["drugs"]["inventory_policy"]
        inc = float(V["drugs"]["acquisition_price_increase_2025"])
        terms = int(self.org["wholesaler"]["payment_terms_days"])
        admins: list[dict] = []
        for c in self.claims:
            for l in c["lines"]:
                if l["is_drug"]:
                    admins.append({"date": c["service_date"], "drug": l["drug_idx"], "units": l["units"],
                                   "claim_id": c["claim_id"], "line_no": l["line_no"],
                                   "account": c["patient"]["account"], "facility": c["facility"],
                                   "hour": float(r.uniform(8, 17))})
        admins.sort(key=lambda a: (a["date"], a["hour"]))
        days_total = (self.end - self.start).days + 1
        demand = np.zeros(len(self.drugs))
        for a in admins:
            demand[a["drug"]] += a["units"]
        rate = demand / days_total
        lots: list[list[dict]] = [[] for _ in self.drugs]
        self.lots: list[dict] = []
        self.invoices: list[dict] = []   # invoice header with lines
        self.dispense: list[dict] = []
        inv_seq = [0]
        first_review = self.start - timedelta(days=(self.start.weekday() + 14))  # a Monday two weeks before
        by_day: dict[date, list[dict]] = {}
        for a in admins:
            by_day.setdefault(a["date"], []).append(a)

        def unit_price_c(d: dict, day: date) -> int:
            f = d["acq_ratio"] * (1 + inc if day.year >= 2025 else 1.0)
            return max(1, c_of(d["limit"] * f * d["package_units"]))

        def new_lot(d: dict, day: date, packages: int, inv: dict) -> dict:
            exp = day + timedelta(days=d["shelf_life_days"] + int(r.integers(-20, 21)))
            exp = date(exp.year, exp.month, 1) + timedelta(days=32)
            exp = date(exp.year, exp.month, 1) - timedelta(days=1)       # expiry at month end
            letters = "".join(chr(65 + int(x)) for x in r.integers(0, 26, size=2))
            lot = {"lot_no": f"{letters}{int(r.integers(100000, 999999))}", "drug": d["idx"], "expiry": exp,
                   "received": day, "units": packages * d["package_units"], "remaining": packages * d["package_units"],
                   "administered": 0, "wasted": 0, "unit_price_c": unit_price_c(d, day),
                   "invoice_no": inv["invoice_no"], "packages": packages}
            inv["lines"].append(lot)
            lots[d["idx"]].append(lot)
            self.lots.append(lot)
            return lot

        def new_invoice(day: date, kind: str) -> dict:
            inv_seq[0] += 1
            inv = {"invoice_no": f"PSD{day:%y%m}{inv_seq[0]:05d}", "date": day, "kind": kind, "lines": [],
                   "po": f"PO{int(r.integers(100000, 999999))}", "due": day + timedelta(days=terms)}
            self.invoices.append(inv)
            return inv

        day = first_review
        while day <= self.as_of:
            # expiry waste first (lot unusable after its expiry date)
            for d in self.drugs:
                for lot in lots[d["idx"]]:
                    if lot["remaining"] > 0 and lot["expiry"] < day:
                        self.dispense.append({"dt": datetime.combine(lot["expiry"] + timedelta(days=1), datetime.min.time()) + timedelta(hours=7),
                                              "type": "WASTE_EXPIRED", "drug": d["idx"], "lot": lot,
                                              "units": lot["remaining"], "claim_id": "", "line_no": "",
                                              "account": "", "facility": "F02"})
                        lot["wasted"] += lot["remaining"]
                        lot["remaining"] = 0
            if (day - first_review).days % int(pol["review_interval_days"]) == 0 and day <= self.end:
                inv = None
                for d in self.drugs:
                    on_hand = sum(l_["remaining"] for l_ in lots[d["idx"]] if l_["expiry"] >= day)
                    par = rate[d["idx"]] * float(pol["cover_days"]) * d["overstock"]
                    if on_hand < par and rate[d["idx"]] > 0:
                        pk = int(math.ceil((par - on_hand) / d["package_units"]))
                        if inv is None:
                            inv = new_invoice(day, "STANDARD")
                        new_lot(d, day, pk, inv)
            for a in by_day.get(day, []):
                d = self.drugs[a["drug"]]
                need = a["units"]
                avail = sum(l_["remaining"] for l_ in lots[d["idx"]] if l_["expiry"] >= day)
                if avail < need:
                    inv = new_invoice(day, "EXPEDITED")
                    new_lot(d, day, int(math.ceil((need - avail) / d["package_units"])), inv)
                for lot in sorted((l_ for l_ in lots[d["idx"]] if l_["remaining"] > 0 and l_["expiry"] >= day),
                                  key=lambda l_: (l_["expiry"], l_["received"], l_["lot_no"])):
                    if need <= 0:
                        break
                    take = min(need, lot["remaining"])
                    lot["remaining"] -= take
                    lot["administered"] += take
                    need -= take
                    self.dispense.append({"dt": datetime.combine(day, datetime.min.time()) + timedelta(hours=a["hour"]),
                                          "type": "ADMINISTER", "drug": d["idx"], "lot": lot, "units": take,
                                          "claim_id": a["claim_id"], "line_no": a["line_no"],
                                          "account": a["account"], "facility": a["facility"]})
            day += timedelta(days=1)
        self.dispense.sort(key=lambda e: (e["dt"], e["drug"]))

    # ---------------------------------------------------------------- bank statement
    def build_bank(self) -> None:
        V, r = self.V, self.r_cash
        bank = self.org["bank"]
        cd = V["cash_defects"]
        shift_p = V["timing"]["bank_posting_shift_probability"]
        mail = V["timing"]["check_mail_days"]
        txns: list[dict] = []   # each: date, order, type, desc, amount_c, value_date, traces
        lockbox: dict[date, list[dict]] = {}
        for tr in self.traces:
            tr["deposit"] = None
            if tr["total"] <= 0 or tr["method"] == "NON" or tr["deposit_missing"]:
                continue
            p = tr["payer"]
            pay = self.payers[self.pidx[p]]
            if tr["method"] == "ACH":
                post = next_business_day(tr["run"])
                shift = 0
                if r.random() < float(shift_p[p]):
                    post = add_business_days(post, 1)
                    shift = 1
                tr["shift_bdays"] = shift
                tn = tr["trace_no"]
                base = f"ACH CREDIT {pay['ach_company_name']} HCCLAIMPMT TRN*1*{tn}*{pay['tax_id']}\\"
                tr["truncated"] = False
                tr["no_trace"] = False
                rs = self.r_split
                if tr.get("pip"):
                    pip = V["cash_defects"]["periodic_interim_payment"]
                    if rs.random() < float(pip["trace_missing_probability"]):
                        desc = f"ACH CREDIT {pay['ach_company_name']} HCCLAIMPMT"
                        tr["no_trace"] = True
                    else:
                        desc = base
                    txns.append({"date": post, "order": 1, "type": "ACH CREDIT", "desc": desc, "amount": tr["total"],
                                 "value_date": tr["run"], "traces": [tr], "kind": "PAYER_EFT_PROVIDER_LEVEL", "cust_ref": ""})
                    continue
                if tr["total"] >= 200000 and rs.random() < float(cd["split_deposit_probability"]):
                    part1 = round_frac(tr["total"], float(rs.uniform(0.3, 0.7)))
                    post2 = post if rs.random() < 0.5 else add_business_days(post, 1)
                    no2 = bool(rs.random() < float(cd["split_second_part_no_trace_probability"]))
                    desc2 = f"ACH CREDIT {pay['ach_company_name']} HCCLAIMPMT" if no2 else base
                    tr["split"], tr["split_no_trace"] = True, no2
                    txns.append({"date": post, "order": 1, "type": "ACH CREDIT", "desc": base, "amount": part1,
                                 "value_date": tr["run"], "traces": [tr], "kind": "PAYER_EFT_SPLIT", "cust_ref": ""})
                    txns.append({"date": post2, "order": 1, "type": "ACH CREDIT", "desc": desc2, "amount": tr["total"] - part1,
                                 "value_date": tr["run"], "traces": [tr], "kind": "PAYER_EFT_SPLIT", "cust_ref": ""})
                    continue
                if r.random() < float(cd["trace_missing_probability"][p]):
                    desc = f"ACH CREDIT {pay['ach_company_name']} HCCLAIMPMT"
                    tr["no_trace"] = True
                elif r.random() < float(cd["trace_truncation_probability"][p]):
                    start = base.index("TRN*1*") + 6
                    cut = start + len(tn) - int(r.integers(2, 5))
                    desc = base[:cut]
                    tr["truncated"] = True
                else:
                    desc = base
                txns.append({"date": post, "order": 1, "type": "ACH CREDIT", "desc": desc, "amount": tr["total"],
                             "value_date": tr["run"], "traces": [tr], "kind": "PAYER_EFT",
                             "cust_ref": ""})
            else:
                recv = next_business_day(tr["run"] + timedelta(days=int(r.integers(mail[0], mail[1] + 1))))
                lockbox.setdefault(recv, []).append(tr)
        for d, trs in lockbox.items():
            txns.append({"date": d, "order": 0, "type": "LOCKBOX DEPOSIT",
                         "desc": f"LOCKBOX DEPOSIT LBX {bank['lockbox_id']} ITEMS {len(trs)}",
                         "amount": sum(t["total"] for t in trs), "value_date": d, "traces": trs,
                         "kind": "LOCKBOX", "cust_ref": f"LBX{d:%m%d}"})
        # non-payer activity
        d = self.start
        lo, hi = bank["card_settlement_daily_range"]
        fee_lo, fee_hi = bank["monthly_fee_range"]
        while d <= self.as_of:
            if is_business_day(d):
                if r.random() < float(bank["card_settlement_probability"]):
                    txns.append({"date": d, "order": 2, "type": "CARD SETTLEMENT",
                                 "desc": "MERCHANT SETTLEMENT CARD DEP MID 448812", "amount": c_of(float(r.uniform(lo, hi))),
                                 "value_date": d, "traces": [], "kind": "CARD", "cust_ref": ""})
                if d.day <= 7 and next_business_day(date(d.year, d.month, 1)) == d:
                    txns.append({"date": d, "order": 5, "type": "FEE", "desc": "ACCOUNT ANALYSIS FEE",
                                 "amount": -c_of(float(r.uniform(fee_lo, fee_hi))), "value_date": d, "traces": [],
                                 "kind": "FEE", "cust_ref": ""})
                    txns.append({"date": d, "order": 3, "type": "INTEREST", "desc": "INTEREST CREDIT",
                                 "amount": None, "value_date": d, "traces": [], "kind": "INTEREST", "cust_ref": ""})
            d += timedelta(days=1)
        for inv in self.invoices:
            due = next_business_day(inv["due"])
            if due <= self.as_of:
                amt = sum(l["packages"] * l["unit_price_c"] for l in inv["lines"])
                txns.append({"date": due, "order": 6, "type": "ACH DEBIT",
                             "desc": f"ACH DEBIT PINECREST SPECIALTY INV {inv['invoice_no']}", "amount": -amt,
                             "value_date": due, "traces": [], "kind": "VENDOR", "cust_ref": inv["invoice_no"]})
        txns = [t for t in txns if t["date"] <= self.as_of]
        txns.sort(key=lambda t: (t["date"], t["order"], t["traces"][0]["trace_no"] if t["traces"] else "", t["desc"]))
        # running balance with interest and weekly sweep
        bal = c_of(float(bank["opening_balance"]))
        self.opening_balance_c = bal
        target = c_of(float(bank["sweep_target_balance"]))
        trigger = c_of(float(bank["sweep_trigger_excess"]))
        rate = float(bank["earnings_rate_annual"])
        out: list[dict] = []
        month_end_bal = bal
        cur_month = (self.start.year, self.start.month)
        i = 0
        days = sorted({t["date"] for t in txns} | {d_ for d_ in (self.start + timedelta(days=k) for k in range((self.as_of - self.start).days + 1)) if d_.weekday() == 4 and is_business_day(d_)})
        by_date: dict[date, list[dict]] = {}
        for t in txns:
            by_date.setdefault(t["date"], []).append(t)
        seq = 0
        for d in days:
            if (d.year, d.month) != cur_month:
                month_end_bal = bal
                cur_month = (d.year, d.month)
            day_txns = list(by_date.get(d, []))
            for t in day_txns:
                if t["kind"] == "INTEREST":
                    t["amount"] = max(1, round_frac(max(month_end_bal, 0), rate / 12.0))
            if d.weekday() == 4:
                pre = bal + sum(t["amount"] for t in day_txns)
                if pre > target + trigger:
                    amt = ((pre - target) // 100000) * 100000
                    day_txns.append({"date": d, "order": 9, "type": "BOOK TRANSFER",
                                     "desc": "BOOK TRANSFER TO CONCENTRATION ACCT 7730", "amount": -amt,
                                     "value_date": d, "traces": [], "kind": "SWEEP", "cust_ref": f"SWP{d:%y%m%d}"})
            for t in day_txns:
                bal += t["amount"]
                seq += 1
                t["balance"] = bal
                t["bank_ref"] = f"FHB{d:%y%m%d}{seq:06d}"
                out.append(t)
        self.bank_txns = out
        self.min_balance_c = min(t["balance"] for t in out) if out else bal

    # ---------------------------------------------------------------- writers
    def write(self, raw_dir: Path, truth_dir: Path) -> dict:
        raw_dir.mkdir(parents=True, exist_ok=True)
        truth_dir.mkdir(parents=True, exist_ok=True)
        m = self.r_mess
        V = self.V
        sd = V["staging_defects"]["counts"]
        sc = lambda k: max(1, int(round(int(sd[k]) * max(self.scale, 0.05))))  # noqa: E731
        hosp = self.org["hospital"]
        fac = hosp["facilities"]
        staging_truth: list[dict] = []

        # ---------- claims export
        crow: list[dict] = []
        for c in self.claims:
            pay = self.payers[c["payer_idx"]]
            pid_txt = c["payer"] if m.random() > 0.03 else f" {c['payer'].lower()} "
            pname = [pay["payer_name"], pay["payer_name"].upper(), pay["payer_name"].replace(" Health ", " Hlth ")][int(m.choice(3, p=[0.8, 0.15, 0.05]))]
            sub = c["submission_date"].isoformat() if m.random() < 0.85 else c["submission_date"].strftime("%m/%d/%Y")
            for l in c["lines"]:
                sdt = c["service_date"]
                sdate_txt = sdt.strftime("%m/%d/%Y") if m.random() < 0.9 else sdt.strftime("%Y%m%d")
                money = (lambda x: fmt_c(x)) if m.random() < 0.9 else (lambda x: "$" + fmt_comma(x))
                crow.append({
                    "claim_id": c["claim_id"], "form_type": c["form_type"],
                    "bill_type": "131" if c["form_type"] == "UB04" else "",
                    "facility_name": fac[c["facility"]]["name"], "facility_npi": fac[c["facility"]]["npi"],
                    "patient_account_no": c["patient"]["account"], "member_id": c["patient"]["member_id"],
                    "payer_id": pid_txt, "payer_name": pname, "service_line": c["service_line"],
                    "attending_provider_id": c["attending"],
                    "statement_from_date": sdt.strftime("%m/%d/%Y"), "statement_to_date": sdt.strftime("%m/%d/%Y"),
                    "submission_date": sub, "claim_total_charge": money(c["total_c"]),
                    "line_no": str(l["line_no"]), "line_item_control_no": l["lic"],
                    "revenue_code": l["revenue_code"], "procedure_code": l["code"],
                    "modifier_1": "JZ" if l["is_drug"] else "", "ndc_code": l["ndc"],
                    "diagnosis_code_1": c["diag1"], "diagnosis_code_2": c["diag2"],
                    "service_date": sdate_txt, "units": str(l["units"]), "line_charge": money(l["charge_c"]),
                    "_key": (c["claim_id"], l["line_no"]),
                })
        bad_lines: dict[tuple, str] = {}
        n_rows = len(crow)
        pick = m.choice(n_rows, size=sc("claims_missing_procedure") + sc("claims_bad_charge"), replace=False)
        for j, ix in enumerate(pick):
            row = crow[int(ix)]
            if j < sc("claims_missing_procedure"):
                row["procedure_code"] = ""
                bad_lines[row["_key"]] = "MISSING_PROCEDURE_CODE"
            else:
                row["line_charge"] = row["line_charge"].replace("1", "l", 1) if "1" in row["line_charge"] else "N/A"
                bad_lines[row["_key"]] = "UNPARSEABLE_AMOUNT"
        pool = np.setdiff1d(np.arange(n_rows), np.asarray(pick))
        dup_ix = sorted(int(x) for x in m.choice(pool, size=sc("claims_duplicate_rows"), replace=False))
        crow_out = crow + [dict(crow[i]) for i in dup_ix]
        for rn, row in enumerate(crow_out, start=1):
            row["_row_no"] = rn
        for row in crow_out:
            if row["_key"] in bad_lines and row["_row_no"] <= n_rows:
                staging_truth.append({"file": "claims_export.csv", "row_no": row["_row_no"], "defect": bad_lines[row["_key"]]})
        for k, i in enumerate(dup_ix):
            staging_truth.append({"file": "claims_export.csv", "row_no": n_rows + k + 1, "defect": "DUPLICATE_ROW",
                                  "duplicate_of_row": i + 1})
        self.rejected_claim_lines = set(bad_lines)
        write_csv(raw_dir / "claims_export.csv", RAW_COLUMNS["claims_export.csv"], crow_out)

        # ---------- remittance advice
        rb = V["remit_behaviour"]
        mang = rb["id_mangling"]
        echo = rb["line_control_echo_probability"]
        mfmt = rb["member_id_format"]
        export_lines = [l for tr in self.traces if tr["run"] <= self.as_of and not tr["remit_missing"]
                        for l in tr["lines"]]
        export_lines.sort(key=lambda l: (l["run"], self.pidx[l["payer"]], l["trace"]["trace_no"], l["file_id"], l["seq"]))
        part_info: dict[int, dict] = {}
        for l in export_lines:
            pi = part_info.get(l["part"])
            if pi is None and l.get("pip"):
                pi = {"pcn": "", "defect": "none", "echo": False, "lines": [], "true_cid": None}
                part_info[l["part"]] = pi
            if pi is None:
                p = l["payer"]
                if l["claim_idx"] is not None:
                    cid = self.claims[l["claim_idx"]]["claim_id"]
                else:
                    cid = l["orphan"]["claim_id"]
                mm = mang[p]
                x = m.random()
                defect, acc = "none", 0.0
                for kname in ("padding", "prefix", "punctuation", "lowercase", "transposed"):
                    acc += float(mm[kname])
                    if x < acc:
                        defect = kname
                        break
                forced = l["claim_idx"] is not None and self.claims[l["claim_idx"]].get("force_transposed")
                if forced:
                    defect = "transposed"
                pcn = cid
                if defect == "padding":
                    pcn = cid.rjust(15, "0")
                elif defect == "prefix":
                    pcn = ["PCN", "CLM#", "PCN:"][int(m.integers(0, 3))] + cid
                elif defect == "punctuation":
                    pcn = f"{cid[:3]}-{cid[3:5]}-{cid[5:]}"
                elif defect == "lowercase":
                    pcn = cid.lower()
                elif defect == "transposed":
                    digits = list(cid[5:])
                    cands = [j for j in range(len(digits) - 1) if digits[j] != digits[j + 1]]
                    if cands:
                        j = cands[int(m.integers(0, len(cands)))]
                        digits[j], digits[j + 1] = digits[j + 1], digits[j]
                        pcn = cid[:5] + "".join(digits)
                    else:
                        defect = "none"
                if forced and defect == "transposed":
                    defect = "transposed_twin"
                pi = {"pcn": pcn, "defect": defect, "echo": bool(m.random() < float(echo[p])),
                      "lines": [], "true_cid": cid}
                part_info[l["part"]] = pi
            pi["lines"].append(l)
        payee = hosp["name"]
        rrow: list[dict] = []
        rtruth: list[dict] = []
        for l in export_lines:
            pi = part_info[l["part"]]
            tr = l["trace"]
            p = l["payer"]
            pay = self.payers[self.pidx[p]]
            if l.get("pip"):
                member, sdate, code, units, lic = "", l["run"], "", "", ""
            elif l["claim_idx"] is not None:
                c = self.claims[l["claim_idx"]]
                cl = c["lines"][l["line_no"] - 1]
                member, sdate, code, units, lic = c["patient"]["member_id"], c["service_date"], cl["code"], cl["units"], cl["lic"]
            else:
                o = l["orphan"]
                member, sdate, code, units, lic = o["member"], o["sdate"], o["code"], 1, o["lic"]
            if mfmt[p] == "hyphen" and member:
                member = f"{member[:3]}-{member[3:]}"
            elif mfmt[p] == "lowercase":
                member = member.lower()
            adj = l["adj"] + [("", "", None)] * (3 - len(l["adj"]))
            clp_charge = sum(x["charge"] for x in pi["lines"])
            clp_paid = sum(x["paid"] for x in pi["lines"])
            row = {
                "remit_file_id": l["file_id"], "payer_id": p, "payer_name": pay["payer_name"].upper(),
                "payer_tax_id": pay["tax_id"], "payee_name": payee, "payee_npi": hosp["payee_npi"],
                "trace_no": tr["trace_no"], "payment_method": tr["method"], "payment_date": f"{tr['run']:%Y%m%d}",
                "file_payment_amount": fmt_835(l["file_amount"]), "patient_control_no": pi["pcn"],
                "payer_claim_control_no": l["icn"], "claim_status_code": l["status"],
                "claim_charge_amount": fmt_835(clp_charge), "claim_paid_amount": fmt_835(clp_paid),
                "member_id": member, "service_date": f"{sdate:%Y%m%d}", "procedure_code": code,
                "modifier_1": "JZ" if code.startswith("J") else "", "units": str(units),
                "line_item_control_no": lic if pi["echo"] else "",
                "line_charge_amount": fmt_835(l["charge"]), "line_allowed_amount": fmt_835(l["allowed"]),
                "line_paid_amount": fmt_835(l["paid"]),
                "adj_group_1": adj[0][0], "adj_reason_1": adj[0][1], "adj_amount_1": "" if adj[0][2] is None else fmt_835(adj[0][2]),
                "adj_group_2": adj[1][0], "adj_reason_2": adj[1][1], "adj_amount_2": "" if adj[1][2] is None else fmt_835(adj[1][2]),
                "adj_group_3": adj[2][0], "adj_reason_3": adj[2][1], "adj_amount_3": "" if adj[2][2] is None else fmt_835(adj[2][2]),
                "remark_code": "N381" if l["kind"] in ("DENIAL",) and l["adj"] and l["adj"][0][1] == "197" else "",
            }
            sibling = False
            if l.get("pip"):
                action = "PROVIDER_LEVEL"
            elif l["claim_idx"] is None:
                action = "UNMATCHABLE_ORPHAN"
            elif (self.claims[l["claim_idx"]]["claim_id"], l["line_no"]) in self.rejected_claim_lines:
                action = "UNMATCHABLE_REJECTED_CLAIM_LINE"
            else:
                action = "POST"
            if l["claim_idx"] is not None:
                cl_ = self.claims[l["claim_idx"]]["lines"]
                me = cl_[l["line_no"] - 1]
                sibling = any(o["line_no"] != me["line_no"] and o["code"] == me["code"] and o["charge_c"] == me["charge_c"] for o in cl_)
            t = {"kind": l["kind"], "payer_id": p, "trace_no": tr["trace_no"],
                 "true_claim_id": None if l["claim_idx"] is None else self.claims[l["claim_idx"]]["claim_id"],
                 "true_line_no": None if l["claim_idx"] is None else l["line_no"],
                 "id_defect": pi["defect"], "line_control_echoed": pi["echo"], "expected_action": action,
                 "has_identical_sibling": sibling,
                 "paid_cents": l["paid"], "duplicate_of_row": None}
            rrow.append(row)
            rtruth.append(t)
        # verbatim duplicates inside the same file
        n_dup = int(round(len(rrow) * float(rb["file_duplicate_line_rate"])))
        dup_set = set(int(x) for x in m.choice(len(rrow), size=n_dup, replace=False)) if n_dup else set()
        rrow2, rtruth2 = [], []
        for j, (row, t) in enumerate(zip(rrow, rtruth)):
            rrow2.append(row)
            rtruth2.append(t)
            if j in dup_set:
                t2 = dict(t, kind="FILE_DUPLICATE", expected_action="EXCLUDE_DUPLICATE", duplicate_of_row=len(rrow2))
                rrow2.append(dict(row))
                rtruth2.append(t2)
        # staging-level bad rows (unparseable amount or date)
        cand = [j for j, t in enumerate(rtruth2) if t["kind"] in ("PAYMENT", "DENIAL") and t["expected_action"] == "POST"
                and not t["has_identical_sibling"] and t["id_defect"] != "transposed_twin"
                and not (j + 1 < len(rtruth2) and rtruth2[j + 1]["kind"] == "FILE_DUPLICATE")]
        bad = m.choice(len(cand), size=sc("remit_bad_amount") + sc("remit_bad_date"), replace=False)
        for k, b in enumerate(bad):
            j = cand[int(b)]
            if k < sc("remit_bad_amount"):
                rrow2[j]["line_paid_amount"] = "N/A"
                defect = "UNPARSEABLE_AMOUNT"
            else:
                rrow2[j]["service_date"] = "20241399"
                defect = "UNPARSEABLE_DATE"
            rtruth2[j]["expected_action"] = "REJECT_STAGING"
            staging_truth.append({"file": "remittance_advice.csv", "row_no": j + 1, "defect": defect})
        for j, t in enumerate(rtruth2, start=1):
            t["remit_row_no"] = j
        write_csv(raw_dir / "remittance_advice.csv", RAW_COLUMNS["remittance_advice.csv"], rrow2)

        # ---------- bank statement
        bank = self.org["bank"]
        brow: list[dict] = []
        btruth: list[dict] = []
        for t in self.bank_txns:
            amt = t["amount"]
            brow.append({
                "account_number": bank["account_mask"], "posting_date": t["date"].strftime("%m/%d/%Y"),
                "value_date": t["value_date"].strftime("%m/%d/%Y"), "transaction_type": t["type"],
                "description": t["desc"], "debit": fmt_comma(-amt) if amt < 0 else "",
                "credit": fmt_comma(amt) if amt >= 0 else "", "running_balance": fmt_comma(t["balance"]),
                "bank_reference": t["bank_ref"], "customer_reference": t["cust_ref"],
            })
            kind = t["kind"]
            if kind == "PAYER_EFT" and t["traces"][0]["remit_missing"]:
                kind = "PAYER_EFT_NO_REMIT"
            btruth.append({"kind": kind, "traces": ";".join(f"{x['payer']}:{x['trace_no']}" for x in t["traces"]),
                           "amount_cents": amt, "duplicate_of_row": None})
        n_bdup = max(1, int(round(int(V["cash_defects"]["bank_duplicate_rows"]) * max(self.scale, 0.2))))
        bd = set(int(x) for x in m.choice(range(5, len(brow)), size=min(n_bdup, max(0, len(brow) - 5)), replace=False))
        brow2, btruth2 = [], []
        for j, (row, t) in enumerate(zip(brow, btruth)):
            brow2.append(row)
            btruth2.append(t)
            if j in bd:
                brow2.append(dict(row))
                btruth2.append(dict(t, kind="EXPORT_DUPLICATE", duplicate_of_row=len(brow2) - 1))
                staging_truth.append({"file": "bank_statement.csv", "row_no": len(brow2), "defect": "DUPLICATE_BANK_REFERENCE",
                                      "duplicate_of_row": len(brow2) - 1})
        for j, t in enumerate(btruth2, start=1):
            t["bank_row_no"] = j
        # map traces to the bank row that carried their deposit
        row_of_txn = {}
        j = 0
        for t, tt in zip(brow2, btruth2):
            j += 1
            if tt["kind"] != "EXPORT_DUPLICATE":
                row_of_txn[t["bank_reference"]] = j
        for t in self.bank_txns:
            for tr in t["traces"]:
                tr.setdefault("deposit_rows", []).append(row_of_txn[t["bank_ref"]])
                if tr.get("deposit") is None:
                    tr["deposit"] = {"row_no": row_of_txn[t["bank_ref"]], "posting_date": t["date"], "kind": t["kind"]}
        write_csv(raw_dir / "bank_statement.csv", RAW_COLUMNS["bank_statement.csv"], brow2)

        # ---------- payer contracts
        cont: list[dict] = []
        cn = 0
        terms = V["pricing"]["contract_terms"]
        periods = [(2024, date(2024, 1, 1), date(2024, 12, 31)), (2025, date(2025, 1, 1), date(2026, 12, 31))]
        for p in self.pid:
            pay = self.payers[self.pidx[p]]
            for yr, f, t in periods:
                cn += 1
                cid = f"CTR-{p}-{yr}"
                hdr = {"contract_id": cid, "payer_id": p, "payer_name": pay["payer_name"], "payer_type": pay["payer_type"],
                       "payer_tax_id": pay["tax_id"], "ach_company_name": pay["ach_company_name"],
                       "effective_from": f.isoformat() if m.random() < 0.7 else f.strftime("%m/%d/%Y"),
                       "effective_to": t.isoformat() if m.random() < 0.7 else t.strftime("%m/%d/%Y"),
                       "lesser_of_charges": "Y"}
                if p in self.contract_pct:
                    cont.append(dict(hdr, procedure_scope="ALL", procedure_code="", rate_type="PCT_OF_CHARGE",
                                     rate=f"{self.contract_pct[p] / 100:.2f}%", unit_basis="PER LINE",
                                     notes="Discount off billed charges, all services"))
                    continue
                for code in sorted(self.procs):
                    rate_c = self.contract[(p, code)][yr]
                    txt = fmt_c(rate_c) if m.random() < 0.8 else "$" + fmt_comma(rate_c)
                    cont.append(dict(hdr, procedure_scope="CODE", procedure_code=code, rate_type="FEE_SCHEDULE",
                                     rate=txt, unit_basis="PER UNIT",
                                     notes="Drug rate per HCPCS billing unit" if self.procs[code]["is_drug"] else ""))
        n_c = len(cont)
        cd_ix = sorted(int(x) for x in m.choice(n_c, size=min(n_c, sc("contract_duplicate_rows")), replace=False))
        cont_out = cont + [dict(cont[i]) for i in cd_ix]
        for k, i in enumerate(cd_ix):
            staging_truth.append({"file": "payer_contracts.csv", "row_no": n_c + k + 1, "defect": "DUPLICATE_ROW",
                                  "duplicate_of_row": i + 1})
        write_csv(raw_dir / "payer_contracts.csv", RAW_COLUMNS["payer_contracts.csv"], cont_out)

        # ---------- drug purchase invoices
        wh = self.org["wholesaler"]
        prow = []
        for inv in self.invoices:
            if inv["date"] > self.as_of:
                continue
            for ln, lot in enumerate(inv["lines"], start=1):
                d = self.drugs[lot["drug"]]
                exp = lot["expiry"].isoformat() if m.random() < 0.6 else lot["expiry"].strftime("%m/%Y")
                prow.append({"invoice_no": inv["invoice_no"], "invoice_date": inv["date"].strftime("%m/%d/%Y"),
                             "wholesaler_name": wh["name"], "wholesaler_id": wh["wholesaler_id"],
                             "ship_to_facility": fac["F02"]["name"], "po_number": inv["po"], "invoice_line_no": str(ln),
                             "ndc_code": d["ndc"], "product_description": f"{d['desc'].upper()} {d['package_units']} UNITS",
                             "lot_no": lot["lot_no"], "expiry_date": exp, "qty_packages": str(lot["packages"]),
                             "billing_units_per_package": str(d["package_units"]),
                             "unit_price": fmt_comma(lot["unit_price_c"]),
                             "extended_amount": fmt_comma(lot["unit_price_c"] * lot["packages"]),
                             "payment_terms": "NET 30", "due_date": inv["due"].strftime("%m/%d/%Y"),
                             "order_type": inv["kind"]})
        write_csv(raw_dir / "drug_purchase_invoices.csv", RAW_COLUMNS["drug_purchase_invoices.csv"], prow)

        # ---------- drug dispense log
        drow = []
        for k, e in enumerate(e for e in self.dispense if e["dt"].date() <= self.as_of):
            d = self.drugs[e["drug"]]
            lot_txt = e["lot"]["lot_no"] if (e["type"] != "ADMINISTER" or m.random() > 0.03) else ""
            cl_txt = e["claim_id"] if (e["type"] != "ADMINISTER" or m.random() > 0.05) else ""
            drow.append({"dispense_id": f"DSP{k + 1:08d}", "event_datetime": e["dt"].strftime("%Y-%m-%d %H:%M"),
                         "event_type": e["type"], "facility_name": fac[e["facility"]]["name"],
                         "ndc_code": d["ndc"], "hcpcs_code": d["hcpcs"], "drug_description": d["desc"],
                         "lot_no": lot_txt, "billing_units": str(e["units"]), "patient_account_no": e["account"],
                         "claim_id": cl_txt, "claim_line_no": str(e["line_no"]) if cl_txt else "",
                         "recorded_by": f"RPH{int(m.integers(10, 40))}"})
        badu = m.choice(len(drow), size=min(len(drow), sc("dispense_bad_units")), replace=False) if drow else []
        for b in badu:
            drow[int(b)]["billing_units"] = "unk"
            staging_truth.append({"file": "drug_dispense_log.csv", "row_no": int(b) + 1, "defect": "UNPARSEABLE_UNITS"})
        write_csv(raw_dir / "drug_dispense_log.csv", RAW_COLUMNS["drug_dispense_log.csv"], drow)

        # ---------- claim status history
        rs = self.r_status
        ev = [e for e in self.events if e[2].date() <= self.as_of]
        ev.sort(key=lambda e: (e[2], e[0], STATES.index(e[1])))
        srow = []
        for k, e in enumerate(ev):
            c = self.claims[e[0]]
            alias = STATUS_ALIASES[e[1]]
            txt = alias[int(rs.choice(len(alias), p=[0.7, 0.2, 0.1]))]
            dt_txt = e[2].strftime("%Y-%m-%d %H:%M:%S") if rs.random() < 0.8 else e[2].strftime("%m/%d/%Y %I:%M %p")
            cid = c["claim_id"] if rs.random() > 0.02 else f" {c['claim_id'].lower()}"
            srow.append({"event_id": f"EV{k + 1:09d}", "claim_id": cid, "status_text": txt, "status_datetime": dt_txt,
                         "reason_code": e[3], "appeal_level": e[4], "source_system": e[5],
                         "recorded_by": "SYSTEM" if e[5] != "WORKQUEUE" else f"RCM{int(rs.integers(100, 130))}",
                         "note": e[6]})
        n_s = len(srow)
        dups = sorted(int(x) for x in rs.choice(n_s, size=min(n_s, sc("status_duplicate_events")), replace=False))
        unk = sorted(int(x) for x in rs.choice(n_s, size=min(n_s, sc("status_unknown_text")), replace=False))
        srow2 = []
        for j, row in enumerate(srow):
            if j in unk:
                row = dict(row, status_text=["STATUS 99", "TBD", "REVIEW?"][j % 3])
                srow2.append(row)
                staging_truth.append({"file": "claim_status_history.csv", "row_no": len(srow2), "defect": "UNKNOWN_STATUS"})
            else:
                srow2.append(row)
            if j in dups:
                srow2.append(dict(row, event_id=f"EVR{j + 1:09d}"))
                staging_truth.append({"file": "claim_status_history.csv", "row_no": len(srow2),
                                      "defect": "DUPLICATE_EVENT" if j not in unk else "UNKNOWN_STATUS",
                                      "duplicate_of_row": len(srow2) - 1})
        write_csv(raw_dir / "claim_status_history.csv", RAW_COLUMNS["claim_status_history.csv"], srow2)

        # ---------- truth
        write_csv(truth_dir / "remit_line_truth.csv",
                  ["remit_row_no", "kind", "payer_id", "trace_no", "true_claim_id", "true_line_no", "id_defect",
                   "line_control_echoed", "has_identical_sibling", "expected_action", "paid_cents", "duplicate_of_row"], rtruth2)
        write_csv(truth_dir / "bank_truth.csv", ["bank_row_no", "kind", "traces", "amount_cents", "duplicate_of_row"], btruth2)
        write_csv(truth_dir / "staging_defects.csv", ["file", "row_no", "defect", "duplicate_of_row"], staging_truth)
        trows = []
        for tr in self.traces:
            dep = tr.get("deposit")
            trows.append({
                "payer_id": tr["payer"], "trace_no": tr["trace_no"], "payment_method": tr["method"],
                "payment_date": tr["run"].isoformat(), "total_cents": tr["total"],
                "remit_exported": bool(tr["run"] <= self.as_of and not tr["remit_missing"]),
                "remit_missing": tr["remit_missing"], "deposit_missing": tr["deposit_missing"],
                "deposit_bank_row_no": dep["row_no"] if dep else None,
                "deposit_posting_date": dep["posting_date"].isoformat() if dep else None,
                "deposit_kind": dep["kind"] if dep else None,
                "deposit_bank_row_nos": ";".join(str(x) for x in tr.get("deposit_rows", [])),
                "split_deposit": bool(tr.get("split")), "split_second_part_no_trace": bool(tr.get("split_no_trace")),
                "provider_level": bool(tr.get("pip")),
                "truncated_trace": bool(tr.get("truncated")), "no_trace_in_bank": bool(tr.get("no_trace")),
                "posting_shift_bdays": tr.get("shift_bdays", 0), "n_remit_files": len(tr["files"]),
                "n_lines": len(tr["lines"]),
            })
        write_csv(truth_dir / "trace_truth.csv", list(trows[0].keys()), trows)
        hidden = [{"payer_id": l["payer"], "trace_no": l["trace"]["trace_no"], "payment_date": l["run"].isoformat(),
                   "kind": l["kind"], "claim_id": self.claims[l["claim_idx"]]["claim_id"] if l["claim_idx"] is not None else None,
                   "line_no": l["line_no"], "paid_cents": l["paid"]}
                  for tr in self.traces if tr["remit_missing"] and tr["run"] <= self.as_of for l in tr["lines"]]
        write_csv(truth_dir / "withheld_remit_lines.csv",
                  ["payer_id", "trace_no", "payment_date", "kind", "claim_id", "line_no", "paid_cents"], hidden)
        # claim-line truth (as of the as-of date, and ultimate)
        comp = {}
        for l in self.remit:
            if l["claim_idx"] is None:
                continue
            key = (l["claim_idx"], l["line_no"])
            d_ = comp.setdefault(key, {"paid": 0, "ca": 0, "den": 0, "pr": 0, "allowed": 0,
                                       "paid_ult": 0, "vis_paid": 0})
            seen = l["trace"]["run"] <= self.as_of
            ca = sum(a for g, rc, a in l["adj"] if g == "CO" and rc == "45")
            pr = sum(a for g, rc, a in l["adj"] if g == "PR")
            den = sum(a for g, rc, a in l["adj"] if g == "CO" and rc != "45")
            d_["paid_ult"] += l["paid"]
            if seen:
                d_["paid"] += l["paid"]
                d_["ca"] += ca
                d_["pr"] += pr
                d_["den"] += den
                d_["allowed"] += l["allowed"]
                if not l["trace"]["remit_missing"]:
                    d_["vis_paid"] += l["paid"]
        lrows = []
        for c in self.claims:
            for l in c["lines"]:
                d_ = comp.get((c["idx"], l["line_no"]), {"paid": 0, "ca": 0, "den": 0, "pr": 0, "allowed": 0,
                                                          "paid_ult": 0, "vis_paid": 0})
                lrows.append({"claim_id": c["claim_id"], "line_no": l["line_no"], "payer_id": c["payer"],
                              "procedure_code": l["code"], "units": l["units"], "charge_cents": l["charge_c"],
                              "contract_allowed_cents": l["contract_allowed_c"],
                              "payer_allowed_cents_first_price": c["payer_allowed"][l["line_no"]],
                              "underpaid": c["underpaid"], "shortfall_fraction": round(c["shortfall"], 6),
                              "asof_paid_cents": d_["paid"], "asof_contractual_cents": d_["ca"],
                              "asof_denied_cents": d_["den"], "asof_patient_resp_cents": d_["pr"],
                              "asof_allowed_cents": d_["allowed"], "asof_paid_visible_cents": d_["vis_paid"],
                              "ultimate_paid_cents": d_["paid_ult"],
                              "staging_reject": (c["claim_id"], l["line_no"]) in self.rejected_claim_lines})
        write_csv(truth_dir / "claim_line_truth.csv", list(lrows[0].keys()), lrows)
        crows = []
        for c in self.claims:
            adjs = c["adjudications"]
            first_pay = next((a["run"] for a in adjs if a["kind"] == "PAY"), None)
            first_dec = adjs[0]["run"] if adjs else None
            crows.append({"claim_id": c["claim_id"], "payer_id": c["payer"], "service_line": c["service_line"],
                          "service_date": c["service_date"].isoformat(), "submission_date": c["submission_date"].isoformat(),
                          "total_charge_cents": c["total_c"], "q_denial": round(float(self.q[c["idx"]]), 6),
                          "obar_overturn": round(float(self.obar[c["idx"]]), 6),
                          "path": ">".join(c["path"]), "final_state_ultimate": c["final_state"],
                          "first_adjudication_kind": adjs[0]["kind"] if adjs else "",
                          "first_adjudication_run": first_dec.isoformat() if first_dec else "",
                          "first_payment_run": first_pay.isoformat() if first_pay else "",
                          "first_pass_paid": bool(adjs and adjs[0]["kind"] == "PAY"),
                          "n_denials": sum(1 for a in adjs if a["kind"] == "DENY"),
                          "post_payment_event": c.get("post_event", ""),
                          "underpaid": c["underpaid"], "shortfall_fraction": round(c["shortfall"], 6),
                          "twin_of": c.get("twin_of", ""), "remits_print_transposed_id": bool(c.get("force_transposed"))})
        write_csv(truth_dir / "claim_truth.csv", list(crows[0].keys()), crows)
        lot_rows = [{"lot_no": l["lot_no"], "hcpcs": self.drugs[l["drug"]]["hcpcs"], "invoice_no": l["invoice_no"],
                     "received": l["received"].isoformat(), "expiry": l["expiry"].isoformat(), "units": l["units"],
                     "administered_units": l["administered"], "wasted_units": l["wasted"],
                     "remaining_units": l["remaining"], "package_price_cents": l["unit_price_c"]}
                    for l in self.lots if l["received"] <= self.as_of]
        write_csv(truth_dir / "drug_lot_truth.csv", list(lot_rows[0].keys()), lot_rows)
        return {"staging_defects": len(staging_truth)}

    # ---------------------------------------------------------------- planted parameters
    def planted(self) -> dict:
        V = self.V
        den, tim = V["denial"], V["timing"]
        lag_real: dict[str, dict] = {}
        for p in self.pid:
            lags = [(next(a["run"] for a in c["adjudications"]) - c["submission_date"]).days
                    for c in self.claims if c["payer"] == p and c["adjudications"] and c["adjudications"][0]["kind"] == "PAY"]
            if lags:
                a = np.array(lags)
                lag_real[p] = {"n": int(len(a)), "mean": round(float(a.mean()), 3),
                               "p25": float(np.percentile(a, 25)), "median": float(np.median(a)),
                               "p75": float(np.percentile(a, 75)), "p90": float(np.percentile(a, 90))}
        drugs = {}
        for d in self.drugs:
            pct = {p: (V["pricing"]["contract_terms"][p].get("drug_pct_of_reference")) for p in self.pid}
            drugs[d["hcpcs"]] = {
                "description": d["desc"], "therapeutic_area": d["area"], "reference_price_per_unit_usd": d["limit"],
                "acquisition_ratio_to_reference_2024": round(d["acq_ratio"], 6),
                "acquisition_price_increase_2025": V["drugs"]["acquisition_price_increase_2025"],
                "charge_markup": round(d["markup"], 6), "dose_units": d["dose_units"],
                "package_units": d["package_units"], "overstock_factor": round(d["overstock"], 6),
                "shelf_life_days": d["shelf_life_days"], "ndc": d["ndc"],
                "payer_rate_pct_of_reference": pct,
            }
        payers = {}
        for p in self.pid:
            mix = self.reason_mix[p]
            payers[p] = {
                "first_pass_denial_rate_target": den["first_pass_denial_rate"][p],
                "denial_logit_intercept": round(self.alpha[p], 6),
                "pend_probability": den["pend_probability"][p],
                "reason_mix": dict(zip(REASONS, [round(float(x), 6) for x in mix])),
                "reason_share_among_appealed": dict(zip(REASONS, [round(float(x), 6) for x in self.w_appeal[p]])),
                "overturn_target": den["overturn_target"][p],
                "overturn_logit_intercept": round(self.gamma[p], 6),
                "adjudication_lag_gamma": tim["adjudication_lag_gamma"][p],
                "payment_cycle_weekdays": V["payers"]["payment_cycle_weekdays"][p],
                "realised_first_pass_payment_lag_days": lag_real.get(p),
                "underpayment": V["underpayment"]["behaviour"][p],
                "post_payment_events": V["remit_behaviour"]["post_payment_events"][p],
                "id_mangling": V["remit_behaviour"]["id_mangling"][p],
                "line_control_echo_probability": V["remit_behaviour"]["line_control_echo_probability"][p],
                "check_payment_share": V["payers"]["check_payment_share"][p],
                "bank_posting_shift_probability": tim["bank_posting_shift_probability"][p],
                "trace_truncation_probability": V["cash_defects"]["trace_truncation_probability"][p],
                "trace_missing_probability": V["cash_defects"]["trace_missing_probability"][p],
                "contract_terms": V["pricing"]["contract_terms"][p],
            }
        return {
            "note": ("Parameters planted in the synthetic data generator. Evaluation and tests may read this file; "
                     "pipeline and model code must not. All values are assumptions or public-calibrated inputs "
                     "to a simulation, not findings about real payers."),
            "label": "synthetic_record",
            "seed": self.seed, "n_claims": self.n_claims, "as_of_date": self.as_of.isoformat(),
            "states": STATES, "absorbing_states": ABSORBING,
            "denial_model": {
                "form": "logit P(deny | leaving IN_REVIEW, not pended) = intercept[payer] + service_line[sl] + amount * (ln(claim_total_charge_usd) - ln_center)",
                "service_line": den["service_line_logit"], "amount": den["amount_logit"],
                "ln_center": round(self.ln_center, 6),
                "intercept_calibration": "intercept solved so that the mean probability over the payer's claims equals first_pass_denial_rate_target",
            },
            "overturn_model": {
                "form": "logit P(overturn | appealed, reason c) = intercept[payer] + reason[c] + service_line[sl] + amount * (ln(claim_total_charge_usd) - ln_center)",
                "reason": den["overturn_reason_logit"], "service_line": den["overturn_service_line_logit"],
                "amount": den["overturn_amount_logit"],
                "intercept_calibration": "intercept solved so that sum_i q_i * obar_i / sum_i q_i equals overturn_target, obar_i averaging over reason shares among appealed denials",
                "note": "The reason of an appealed denial is the reason on the DENIED event that preceded the appeal. An upheld appeal returns the claim to DENIED with a freshly drawn reason, which keeps the observed chain Markov.",
            },
            "hospital_action_by_reason": den["hospital_action_by_reason"],
            "pended_writeoff_probability": den["pended_writeoff_probability"],
            "timing": {k: tim[k] for k in ("ack_days_range", "pend_resolution_days", "resubmit_days",
                                            "appeal_filing_days", "appeal_decision_days", "write_off_days")},
            "payment_lag_definition": ("days from submission to payment = ack days + Gamma(shape, scale) adjudication "
                                       "lag, then wait to the payer's next payment-cycle weekday strictly after the decision"),
            "markov_by_payer": self.population_matrices(),
            "payers": payers,
            "drugs": drugs,
            "inventory_policy": V["drugs"]["inventory_policy"],
            "cash": {"opening_balance_cents": self.opening_balance_c, "min_balance_cents": self.min_balance_c,
                     "remit_missing_trace_rate": V["cash_defects"]["remit_missing_trace_rate"],
                     "deposit_missing_trace_rate": V["cash_defects"]["deposit_missing_trace_rate"]},
            "calibration_overrides": override_record(self.cal),
        }


def generate(raw_dir: Path | str = RAW_DIR, truth_dir: Path | str = TRUTH_DIR, *, cal: Calibration | None = None,
             n_claims: int | None = None, seed: int | None = None) -> dict:
    """Generate the raw files and the planted ground truth. Returns the generation metadata."""
    raw_dir, truth_dir = Path(raw_dir), Path(truth_dir)
    cal = cal or load_calibration()
    g = Generator(cal, n_claims=n_claims, seed=seed)
    g.build_reference()
    g.build_claims()
    g.build_twins()
    g.build_models()
    g.build_runs()
    g.simulate_lifecycle()
    g.build_drugs()
    g.build_remittances()
    g.build_orphans()
    g.build_traces()
    g.build_bank()
    g.write(raw_dir, truth_dir)
    planted = g.planted()
    (truth_dir / "planted_parameters.json").write_text(json.dumps(planted, indent=2, default=str), encoding="utf-8")
    files = {}
    for name in RAW_COLUMNS:
        p = raw_dir / name
        with open(p, encoding="utf-8") as f:
            n = sum(1 for _ in f) - 1
        files[name] = {"rows": n, "sha256": sha256_file(p)}
    meta = {
        "generator": "recon_engine.datagen.generate",
        "seed": g.seed, "n_claims": g.n_claims, "as_of_date": g.as_of.isoformat(),
        "label": "synthetic_record",
        "files": files,
        "calibration": override_record(cal),
        "note": "Synthetic raw documents for a fictional hospital. Not real patients, payers or banks.",
    }
    (raw_dir / "generation_metadata.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta
