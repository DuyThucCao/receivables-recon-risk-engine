# Public data sources

This file lists every public number the simulation is calibrated against, where it came from,
and what it can and cannot be used to claim. The values themselves live in
`config/public_calibration.yaml`, which `scripts/fetch_public_data.py` generates. Each
downloaded file's URL, retrieval time, byte size and SHA-256 are in `data/public/manifest.json`.
The `data/public/` folder is not committed.

**Retrieval date of the files in this version: 2026-11-21.** The KFF figures were checked by
hand against their source pages on 2026-11-16.

How to refresh:

```
python -I scripts/fetch_public_data.py                 # download, then regenerate the YAML
python -I scripts/fetch_public_data.py --offline       # re-parse local files (hashes checked)
python -I scripts/fetch_public_data.py --check-latest  # has CMS published a newer release?
```

If any source is unreachable the script exits non-zero, names the source, and leaves the
committed YAML untouched. `--allow-partial` writes the YAML anyway: a failed section gets
`status: unavailable` and no values.

The source URLs are fixed in the script rather than discovered at run time, so a rerun fetches
the same release this document describes. To move to a newer release, change the URL in
`SOURCES`, rerun, and update this file.

Data labels follow `docs/DESIGN.md` section 8.

---

## 1. CMS Transparency in Coverage Public Use File (TC-PUF), plan year 2027

| | |
|---|---|
| What it is | Issuer-level and plan-level counts of claims received, denied and resubmitted, plus appeals, for qualified health plans sold on HealthCare.gov |
| Publisher | Centers for Medicare & Medicaid Services (CMS), Center for Consumer Information & Insurance Oversight |
| Listing page | https://www.cms.gov/marketplace/resources/data/public-use-files ("2027 Exchange PUF Datasets", "Transparency in Coverage PUF (Updated October 22, 2026)") |
| File | https://download.cms.gov/marketplace-puf/2027/transparency-in-coverage-puf.zip (1,389,304 bytes; contains `transparency-in-coverage-puf.xlsx`; server Last-Modified 30 Oct 2026) |
| Documentation read | Data dictionary https://www.cms.gov/files/document/transparency-coverage-datadictionary-py27.pdf ; disclaimer https://www.cms.gov/files/document/transparency-coverage-datadisclaimer-py27.pdf |
| Data year | **2025.** The dictionary says the PUF always holds data from two plan years earlier: "the plan year 2027 PUF contains data from plan year 2025". The YAML stores `plan_year: 2025` (year of the claims) and `puf_year: 2027` |
| Retrieved | 2026-11-21 |
| Terms of use | CMS Disclaimer-User Agreement: CMS asks users to cite CMS as the data source, and data the user has altered or reprocessed may not be presented as CMS data. CMS does not guarantee 100 percent accuracy. So the rates below must be described as **computed by this project from CMS data**, not as CMS statistics |
| Label | `observed_public` (computed directly from counts in a public file) |

**Fields used.** These come from sheet `Transparency 2027 - Ind QHP` (individual-market medical
plans): `Issuer_ID`, `Issuer_Claims_Received_In_Network`, `Issuer_Claims_Denied_In_Network`,
`Issuer_Claims_Resubmitted_In_Network`, `Issuer_Claims_Received_Out_Of_Network`,
`Issuer_Claims_Denied_Out_Of_Network`, `Issuer_Internal_Appeals_Claims_Filed`,
`Issuer_Number_Internal_Appeals_Claims_Overturned`. Issuer-level values repeat on every plan
row. The script checks that they agree within each issuer, then keeps one row per issuer. The
dental (SADP) and SHOP sheets are not used.

**Inclusion rule.** The sheet has 152 issuers.
- 16 dropped: in-network claims received or denied is `N/A`. The legend defines `N/A` as "issuer or plan offering is new to the Exchange".
- 2 dropped: fewer than 1,000 in-network claims received. One reported 0. The 1,000 threshold matches KFF's published rule ("insurers with more than 1,000 claims").
- **134 issuers remain.**

A HIOS Issuer_ID is specific to one state, so a national carrier counts once per state. Each
issuer's rate is `denied_in_network / received_in_network`. Percentiles use linear
interpolation and weight every issuer equally.

**Result** (`denial_rate_in_network` in the YAML, as fractions; data year 2025):

| p10 | p25 | median | p75 | p90 | claims-weighted mean | unweighted mean |
|---|---|---|---|---|---|---|
| 0.0901 | 0.1217 | 0.1690 | 0.2087 | 0.2449 | 0.1571 | 0.1754 |

The weighted mean is 83,907,610 denied out of 533,959,947 in-network claims. The lowest issuer
rate is 0.0252. The highest is 0.9621, a single issuer whose next-highest peer is 0.5013. Use
the p10 to p90 range for calibration, not the extremes.

**Check of the method against a published figure.** The same code was run on the previous
release (PY2026 PUF = 2024 data,
https://download.cms.gov/marketplace-puf/2026/transparency-in-coverage-puf.zip, 999,595 bytes).
KFF analysed that same file and published the numbers on the right:

| | this script, 2024 data | KFF, 2024 data |
|---|---|---|
| issuers included | 157 | 156 |
| in-network claims | 451,249,490 | "451 million" |
| claims-weighted in-network denial rate | 18.73% | "19%" |
| insurer range | 2.7% to 35.65% | "3% to 36%" |

The counts and rates match KFF to rounding. KFF's issuer count is one lower, probably because
of a different treatment of one issuer. This is stored as `method_check_prior_year`.

**2024 to 2025 change.** The 2025 weighted rate (15.71%) is lower than 2024 (18.73%). The drop
is not only a change in which issuers are included. For the 119 issuers present in both years,
the weighted rate went from 18.75% to 16.46%. This project does not explain why. If the
difference matters, say that issuer self-reporting can change between years.

**Extra ratios from the same 134 issuers** (in `benchmarks`, label `observed_public`):

| name | value | how |
|---|---|---|
| `cms_tic_out_of_network_claim_denial_rate_2025` | 32.65% | 17,346,382 denied / 53,129,866 received, out-of-network |
| `cms_tic_in_network_claims_resubmitted_share_2025` | 5.90% | 31,509,209 resubmitted one or more times / 533,959,947 received |
| `cms_tic_internal_appeals_overturned_share_2025` | 26.78% | 64,636 overturned / 241,328 filed, over the 116 issuers where neither count is suppressed |
| `cms_tic_internal_appeals_filed_per_denied_claim_2025` | 0.247% | 241,557 internal appeals / 97,977,189 denied claims (in- plus out-of-network), 125 issuers with an unsuppressed appeal count |

**What it can support.**
- A range of plausible payer-level denial rates for simulated commercial payers. For example, draw each fictional payer's in-network denial rate from about the p10 to p90 band.
- The order of magnitude of resubmission and of enrollee appeal rates.

**What it cannot support.**
- It is **not "the hospital denial rate"**. It covers only HealthCare.gov marketplace issuers: no employer plans, Medicare, Medicaid or state-run exchanges.
- It mixes hospital, physician and pharmacy claims. The data dictionary's definition of a claim includes "a hospital, physician, or pharmacy".
- It counts denials **for all reasons**, including duplicates and claims later paid on resubmission. The disclaimer says the issuer figure "simply provides a raw number".
- Issuers self-report the data, and CMS does not audit it to 100 percent.
- The appeal figures are internal appeals by the insured person, not provider appeals. Appealed claims are self-selected, so the overturn share is not the overturn probability of a typical denial.
- CMS suppresses cells below 10 (`**`). Suppressed appeal counts are left out, not imputed.
- Rates are per claim, not per dollar.

---

## 2. FRED: Secured Overnight Financing Rate (SOFR)

| | |
|---|---|
| What it is | Daily overnight rate for borrowing cash against US Treasury securities in the repo market |
| Publisher | Federal Reserve Bank of New York (owner); distributed by FRED, Federal Reserve Bank of St. Louis |
| Series page | https://fred.stlouisfed.org/series/SOFR |
| File | https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR (35,682 bytes; 2018-04-03 to 2026-11-19) |
| Retrieved | 2026-11-21 |
| Terms of use | FRED copyright status for this series is **"Copyrighted: Citation Required"**. FRED's legal page says such series may be used "with proper attribution of the source and acknowledgment that you obtained the data from FRED". Suggested citation: Federal Reserve Bank of New York, Secured Overnight Financing Rate [SOFR], retrieved from FRED, Federal Reserve Bank of St. Louis; https://fred.stlouisfed.org/series/SOFR |
| Label | `observed_public` |

**Fields used.** `observation_date` and `SOFR`. Blank values (market holidays) are skipped. The
latest observation is stored: **3.91% on 2026-11-19** (`interest_rates.sofr`).

**Can support.** A current benchmark rate for the receivables-financing interest charge
(question 5) and for the value of payment float (question 7). It can also be the base for
interest-rate stress scenarios.

**Cannot support.**
- The rate a hospital would actually borrow at. A lender adds a credit spread, and that spread is a `financial_assumption`, not this number.
- A forecast. The value goes stale daily. Always quote it with its `as_of` date.

---

## 3. FRED: 3-month Treasury constant-maturity yield (DGS3MO)

| | |
|---|---|
| What it is | Market yield on US Treasury securities at 3-month constant maturity, quoted on an investment basis |
| Publisher | Board of Governors of the Federal Reserve System (H.15 release); distributed by FRED |
| Series page | https://fred.stlouisfed.org/series/DGS3MO |
| File | https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO (187,195 bytes; 1981-09-01 to 2026-11-19) |
| Retrieved | 2026-11-21 |
| Terms of use | FRED copyright status **"Public Domain: Citation Requested"**. Cite the source and FRED, for example "Source: Board of Governors of the Federal Reserve System (US) via FRED" |
| Label | `observed_public` |

**Fields used.** `observation_date` and `DGS3MO`. Latest: **4.34% on 2026-11-19**
(`interest_rates.treasury_3m`).

**Why 3-month.** It is close to the 13-week cash-forecast horizon (question 3), so it fits as a
short risk-free discount or opportunity rate. DGS1MO and DGS2 were also downloaded and looked
at, but they are not used.

**Can and cannot support.** Same limits as SOFR. It is a market reference rate, not a borrowing
cost. It is a term yield, not an overnight rate, so it is not interchangeable with SOFR.

---

## 4. CMS Medicare Part B drug payment limit file (ASP pricing), October 2026

| | |
|---|---|
| What it is | Quarterly Medicare Part B payment allowance limits per HCPCS billing unit for physician-administered drugs. Most limits are based on manufacturer-reported average sales price (ASP) |
| Publisher | Centers for Medicare & Medicaid Services |
| Listing page | https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files ("October 2026 Medicare Part B Payment Limit Files – 09/17/2026 - Final file"). This was the latest quarter listed on 2026-11-21 |
| File | https://www.cms.gov/files/zip/october-2026-medicare-part-b-payment-limit-files-final.zip (85,594 bytes). The member parsed is `section 508 version of October 2026 Medicare Part B Payment Limit File 091626.csv` (Windows-1252 encoding) |
| Effective period | October 1, 2026 through December 31, 2026. The file notes that limits "are based on 2Q26 ASP data" |
| Retrieved | 2026-11-21 |
| Terms of use | No licence or terms statement was found on the listing page or in the file. Older files on the same page (2020 and earlier) were behind a CMS/AMA licence click-through, but the current files are not. Our reading, not legal advice: this is a US federal government publication, and HCPCS Level II J-codes are maintained by CMS. Cite CMS and the quarter |
| Label | `published_aggregate` (a figure CMS publishes; not computed by us) |

**Fields used.** `HCPCS Code`, `Short Description`, `HCPCS Code Dosage` (the billing unit),
`Payment Limit` (USD per billing unit), `Co-insurance Percentage`, and `Notes`. Notes are kept
when present, for example "Inflation-adjusted coinsurance" or "\"lesser of\" methodology
applied".

**Selection.** The project chose 25 high-cost physician-administered J-codes:
- 12 oncology
- 5 immunology
- 2 neurology
- 3 ophthalmology
- one each of bone health, immune globulin and hematology

All 25 are present in the file; codes missing from a future quarter would be listed under
`missing_codes` and omitted. The choice of drugs and the `therapeutic_area` label are
editorial. Every other value is copied from the CMS file. Examples:

| HCPCS | CMS short description | per unit | limit (USD) |
|---|---|---|---|
| J9271 | Inj pembrolizumab | 1 MG | 62.253 |
| J0178 | Aflibercept injection | 1 MG | 722.251 |
| J1745 | Infliximab not biosimil 10mg | 10 MG | 29.856 |

**Can support.**
- A realistic order of magnitude and relative ranking of unit prices for the simulated drug catalogue (question 6).
- A plausible "Medicare-like" reimbursement reference per billing unit.

**Cannot support.**
- **Not a hospital's acquisition cost.** What the hospital pays the wholesaler depends on contracts, 340B status and discounts, and it is not in this file.
- **Not a commercial payer's rate.**
- Not the price of a dose. One administration bills many units, so multiply by the units given.
- Valid only for the quarter shown. The file itself says the presence of a code "does not indicate whether Medicare covers a drug".

---

## 5. Published benchmarks (quoted, not recomputed)

These are in `benchmarks` with label `published_aggregate`. Each entry stores the exact
sentence it came from (`quote`) and the date it was checked on the page (`verified_on:
2026-11-16`). The script does not fetch these pages; it carries the hand-checked values.

### 5a. KFF, "Claims Denials and Appeals in ACA Marketplace Plans in 2024"

- Authors: Michelle Long, Justin Lo, Kaye Pestaina. Published 2026-03-24.
- URL: https://www.kff.org/private-insurance/claims-denials-and-appeals-in-aca-marketplace-plans-in-2024/ (this redirected to the `/patient-consumer-protections/` path at the time of checking).
- Data: KFF's analysis of the CMS Transparency in Coverage 2026 PUF (2024 data). It includes insurers with more than 1,000 claims and excludes dental and SHOP plans.
- Licence: KFF website content is under **CC BY-NC-ND 4.0**. Quote it unaltered, with attribution.

| name | value | from the page |
|---|---|---|
| `kff_marketplace_in_network_claim_denial_rate_2024` | 19% | "denied 19% of in-network claims in 2024 and 37% of out-of-network claims" |
| `kff_marketplace_out_of_network_claim_denial_rate_2024` | 37% | same sentence |
| `kff_marketplace_insurer_in_network_denial_rate_min_2024` / `_max_2024` | 3% / 36% | "The in-network denial rate ranged from 3% to 36%" |
| `kff_marketplace_denied_claims_appealed_share_2024_upper_bound` | 1% (an upper bound) | "fewer than 1% of denied claims were appealed" |
| `kff_marketplace_internal_appeals_upheld_share_2024` | 66% | "Insurers upheld 165,863 (66%) denials on appeal." |
| `kff_marketplace_denials_administrative_reason_share_2024` | 25% | "administrative reasons (25%)" |
| `kff_marketplace_denials_prior_authorization_or_referral_share_2024` | 9% | "Nine percent of denials were for lack of prior authorization or referral" |
| `kff_marketplace_denials_medical_necessity_share_2024` | 5% | "only 5% of denials were for lack of medical necessity" |

Limits:
- These figures share every limit of source 1: marketplace only, all denial reasons, and enrollee appeals rather than provider appeals.
- The denial-reason shares come from plan-level data that KFF calls "limited information", and one claim can carry several reasons.

### 5b. KFF, "Medicare Advantage Insurers Made Nearly 53 Million Prior Authorization Determinations in 2024"

- Authors: Jeannie Fuglesten Biniek, Nolan Sroczynski, Meredith Freed, Tricia Neuman. Published 2026-01-28.
- URL: https://www.kff.org/medicare/medicare-advantage-insurers-made-nearly-53-million-prior-authorization-determinations-in-2024/
- Licence: CC BY-NC-ND 4.0, as above.

| name | value | from the page |
|---|---|---|
| `kff_medicare_advantage_prior_auth_denied_share_2024` | 7.7% | "Medicare Advantage insurers denied 4.1 million (7.7%) prior authorization requests in 2024." |
| `kff_medicare_advantage_prior_auth_denials_appealed_share_2024` | 11.5% | "Just 11.5% of denied prior authorization requests were appealed to Medicare Advantage insurers in 2024." |
| `kff_medicare_advantage_prior_auth_appeals_overturned_share_2024` | 80.7% | "most appeals (80.7%) were partially or fully overturned in 2024" |

Limits:
- These are **prior-authorization requests** (decided before the service), not claims, and Medicare Advantage, not commercial payers.
- They are useful as an outside reference that appealed denials are often overturned. They are not a claim overturn probability.
- Appealed cases are self-selected.

---

## 6. Evaluated and not used

| Source | What was checked | Why not used |
|---|---|---|
| **CMS Synthetic Medicare Enrollment, Fee-for-Service Claims and Prescription Drug Event data** (synthetic RIF; https://data.cms.gov/collection/synthetic-medicare-enrollment-fee-for-service-claims-and-prescription-drug-event ; user guide https://data.cms.gov/sites/default/files/2023-05/d51e1218-68c3-4c7c-9598-0b81f22fe903/User%20Guide%20-%20CMS%20Synthetic%20RIF%20Files%20May%202023_AM508_v2.pdf, 1,842,891 bytes) | Read the collection page on 2026-11-16. It contains "realistic-but-not-real" records for 8,671 synthetic beneficiaries in the RIF layout, beneficiary files for 2015 to 2025, and seven FFS claim files plus Part D events. Sizes from HTTP headers, nothing downloaded: `All FFS Claims.zip` 48,755,653 bytes; `Outpatient.zip` 27,038,538; `Carrier.zip` 16,337,902; `All Beneficiary Years.zip` 3,177,048. `inpatient.csv` and `pde.csv` returned no size header | The combined claims file is over the 25 MB limit. CMS's own disclaimer says the data "have very limited inferential research value and should not be used to draw conclusions about Medicare beneficiaries", so it cannot calibrate anything. It is also Medicare FFS, with no remittance or bank data. It could serve as a column-layout reference for the claim export, but `docs/DESIGN.md` already specifies the layouts, so it is not needed |
| **Hospital price transparency machine-readable files** (CMS programme page https://www.cms.gov/priorities/key-initiatives/hospital-price-transparency) | Read the CMS page on 2026-11-16. Each hospital must publish "a comprehensive machine-readable file with all items and services" on its own website, and new requirements are enforced from April 1, 2026 | CMS hosts no central download. Files are per hospital, on hospital websites, and their sizes were not measured. Using one would mean choosing and downloading a real hospital's file, which conflicts with the fictional-organisation rule in `docs/DESIGN.md`. The negotiated rates are hospital-specific and cannot be joined to anything here with a valid key |
| **TC-PUF dental (SADP) and SHOP sheets** | Present in the same workbook (2027: 187 SADP issuers, 6 SHOP issuers) | Dental claims are not part of a hospital's receivables. Six SHOP issuers are too few for a distribution |
| **FRED DGS1MO and DGS2** | Downloaded once to a scratch folder on 2026-11-16 to compare | DGS3MO alone covers the short horizon. The others add nothing to the questions asked |
| **HFMA MAP Keys / net days in accounts receivable** | The HFMA MAP Award page returned HTTP 404 on 2026-11-16. Web search found secondary sites quoting target ranges for days in A/R but no primary page showing a published figure with a date | No figure could be verified on a primary source page, so none is included. A days-in-A/R target, if needed, must be labelled `financial_assumption` |
| **Secondary write-ups of the KFF marketplace analysis** (news and blog summaries found by web search) | Some quote different appeal figures, for example a different upheld share | Only the primary KFF page is cited |
