# Data dictionary

_Raw-file sections are written by hand. Table and column listings were produced from the live database (`information_schema`) on 2026-11-21 so names and types match the code; columns added later by other workstreams are documented with their modules._

All records are synthetic (fictional hospital, payers, bank and wholesaler). Money is `numeric(14,2)`.

## 1. Raw documents (`data/raw/`, landed as `raw.<name>` with `_file`, `_row_no`, `_loaded_at`)

### `claims_export.csv`

One claim service line; the claim header is repeated on every line (CMS-1500 / UB-04 export).

| Column | Meaning |
|---|---|
| `claim_id` | Hospital claim number (patient control number), e.g. MHS240012345; key with line_no |
| `form_type` | UB04 (institutional) or CMS1500 (professional) |
| `bill_type` | UB-04 type of bill (131 = hospital outpatient) |
| `facility_name` | Fictional facility |
| `facility_npi` | Synthetic 10-digit facility identifier (not a registry NPI) |
| `patient_account_no` | Synthetic patient account |
| `member_id` | Insurance member id as registered by the hospital |
| `payer_id` | Payer code P01-P06 (sometimes lower case or padded with spaces) |
| `payer_name` | Payer name, spelling varies |
| `service_line` | Hospital service line (EMERGENCY, INFUSION, ...) |
| `attending_provider_id` | Synthetic provider id |
| `statement_from_date` | Statement period start (MM/DD/YYYY) |
| `statement_to_date` | Statement period end |
| `submission_date` | Date the claim was sent (YYYY-MM-DD or MM/DD/YYYY) |
| `claim_total_charge` | Sum of line charges ('1234.50' or '$1,234.50') |
| `line_no` | Line number within the claim |
| `line_item_control_no` | Hospital line control number, claim_id-NN; payers may echo it |
| `revenue_code` | UB-04 revenue code |
| `procedure_code` | HCPCS/CPT code (J-codes for drugs); blank = reject |
| `modifier_1` | Modifier (JZ on drug lines: no waste) |
| `ndc_code` | Synthetic NDC on drug lines |
| `diagnosis_code_1` | ICD-10-CM primary diagnosis |
| `diagnosis_code_2` | ICD-10-CM secondary diagnosis |
| `service_date` | Date of service (MM/DD/YYYY or YYYYMMDD) |
| `units` | Billing units |
| `line_charge` | Billed charge for the line (list price x units) |

### `remittance_advice.csv`

One adjudicated service line on a payer remittance (835/EOB-like); payment header and claim header repeated.

| Column | Meaning |
|---|---|
| `remit_file_id` | Remittance file identifier; one payment can span several files (split 835) |
| `payer_id` | Payer code |
| `payer_name` | Payer name |
| `payer_tax_id` | Synthetic payer tax id |
| `payee_name` | Hospital |
| `payee_npi` | Synthetic payee id |
| `trace_no` | Payment trace number (EFT trace or check number); one per payer payment run |
| `payment_method` | ACH (EFT), CHK (paper check to lockbox), NON (no money: notification only) |
| `payment_date` | Payer effective payment date, CCYYMMDD |
| `file_payment_amount` | Total paid in this remittance file (BPR02-like) |
| `patient_control_no` | Hospital claim number as the payer echoes it (may be padded, prefixed, punctuated, lower-cased or transposed); blank on PLB rows |
| `payer_claim_control_no` | Payer's own claim number (ICN) |
| `claim_status_code` | 1 processed, 4 denied, 22 reversal of earlier payment, PLB provider-level row |
| `claim_charge_amount` | Claim charge on this claim payment |
| `claim_paid_amount` | Claim paid on this claim payment |
| `member_id` | Member id as printed by the payer (format varies by payer) |
| `service_date` | Date of service, CCYYMMDD |
| `procedure_code` | Procedure code |
| `modifier_1` | Modifier |
| `units` | Units |
| `line_item_control_no` | Echoed hospital line control number (blank for some payers) |
| `line_charge_amount` | Charge echoed (negative on reversals) |
| `line_allowed_amount` | Payer allowed amount |
| `line_paid_amount` | Paid amount (negative on reversals and recoupments) |
| `adj_group_1` | Adjustment group (CO contractual obligation, PR patient responsibility) |
| `adj_reason_1` | Claim adjustment reason code (45 = contractual, 197 = no authorisation, ...) |
| `adj_amount_1` | Adjustment amount |
| `adj_group_2` | Second adjustment group |
| `adj_reason_2` | Second reason |
| `adj_amount_2` | Second amount |
| `adj_group_3` | Third adjustment group |
| `adj_reason_3` | Third reason |
| `adj_amount_3` | Third amount |
| `remark_code` | Remark code (N381 on prior-authorisation denials) |

### `bank_statement.csv`

One transaction on the operating account statement, in statement order.

| Column | Meaning |
|---|---|
| `account_number` | Masked account |
| `posting_date` | Bank posting date (MM/DD/YYYY) |
| `value_date` | Value / effective date |
| `transaction_type` | ACH CREDIT, LOCKBOX DEPOSIT, CARD SETTLEMENT, INTEREST, FEE, ACH DEBIT, BOOK TRANSFER |
| `description` | Free text; ACH payer credits carry TRN*1*<trace>*<payer tax id> unless truncated or missing; lockbox carries ITEMS n |
| `debit` | Debit amount, '1,234.56' |
| `credit` | Credit amount |
| `running_balance` | Balance after this transaction |
| `bank_reference` | Bank's unique reference (repeated when two exports overlap) |
| `customer_reference` | Optional reference |

### `payer_contracts.csv`

One payer x procedure (or ALL) contract rate for a period.

| Column | Meaning |
|---|---|
| `contract_id` | Contract identifier |
| `payer_id` | Payer code |
| `payer_name` | Canonical payer name |
| `payer_type` | COMMERCIAL, GOVERNMENT, SELF_FUNDED_TPA |
| `payer_tax_id` | Synthetic tax id |
| `ach_company_name` | Payer name as it appears in ACH descriptions |
| `procedure_scope` | CODE (one procedure) or ALL |
| `procedure_code` | Procedure code when scope = CODE |
| `rate_type` | FEE_SCHEDULE ($ per unit) or PCT_OF_CHARGE |
| `rate` | '$1,234.50', '1234.50' or '58.00%' |
| `unit_basis` | PER UNIT or PER LINE |
| `effective_from` | Start date |
| `effective_to` | End date |
| `lesser_of_charges` | Y = payer allows the lesser of contract and billed charge |
| `notes` | Free text |

### `drug_purchase_invoices.csv`

One wholesaler invoice line: drug, lot, expiry, quantity and price.

| Column | Meaning |
|---|---|
| `invoice_no` | Invoice number |
| `invoice_date` | Invoice / receipt date |
| `wholesaler_name` | Fictional wholesaler |
| `wholesaler_id` | Wholesaler code |
| `ship_to_facility` | Receiving facility |
| `po_number` | Purchase order |
| `invoice_line_no` | Line on invoice |
| `ndc_code` | Synthetic NDC (join to dispense log) |
| `product_description` | Product text |
| `lot_no` | Manufacturer lot |
| `expiry_date` | Expiry (YYYY-MM-DD or MM/YYYY = month end) |
| `qty_packages` | Packages bought |
| `billing_units_per_package` | HCPCS billing units per package |
| `unit_price` | Price per package |
| `extended_amount` | qty x price |
| `payment_terms` | NET 30 |
| `due_date` | Payment due date |
| `order_type` | STANDARD or EXPEDITED (stock-out) |

### `drug_dispense_log.csv`

One administration or expiry-waste event in the pharmacy system.

| Column | Meaning |
|---|---|
| `dispense_id` | Event id |
| `event_datetime` | Timestamp |
| `event_type` | ADMINISTER or WASTE_EXPIRED |
| `facility_name` | Facility |
| `ndc_code` | Synthetic NDC |
| `hcpcs_code` | HCPCS J-code |
| `drug_description` | Drug text |
| `lot_no` | Lot consumed (blank on some administrations) |
| `billing_units` | Billing units administered or wasted |
| `patient_account_no` | Patient account |
| `claim_id` | Claim billed for the administration (blank on some) |
| `claim_line_no` | Claim line of the drug |
| `recorded_by` | Synthetic pharmacist id |

### `claim_status_history.csv`

One status change of a claim in the clearinghouse / work-queue feed.

| Column | Meaning |
|---|---|
| `event_id` | Feed event id (re-sent events get a new id) |
| `claim_id` | Claim number (sometimes lower case / leading space) |
| `status_text` | Status text; many spellings map to SUBMITTED, IN_REVIEW, PENDED, DENIED, APPEALED, PAID, WRITTEN_OFF |
| `status_datetime` | Timestamp (two formats) |
| `reason_code` | Denial reason as GROUP-CARC, e.g. CO-197 |
| `appeal_level` | 1, 2, ... on APPEALED events |
| `source_system` | CLEARINGHOUSE, PAYER_PORTAL, 835, WORKQUEUE |
| `recorded_by` | SYSTEM or synthetic staff id |
| `note` | Free text |

## 2. Staging (`stg`)

Typed copies of each raw table (`stg.claim_line`, `stg.remit_line`, `stg.bank_txn`, `stg.contract_rate`, `stg.drug_purchase`, `stg.drug_dispense`, `stg.claim_event`) with the same business columns, trimmed, parsed (`stg.parse_money`, `stg.parse_date`, ...) and normalised (`*_norm`). A raw row lands in exactly one of its stg table or `stg.reject`.

### `stg.reject`

Every raw row that failed typing or was a repeat, with reason and the raw record as JSON.

| Column | Type | Meaning |
|---|---|---|
| `reject_id` | bigint, not null |  |
| `source_table` | text, not null |  |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |
| `reason_code` | text, not null |  |
| `reason_detail` | text |  |
| `amount` | numeric(14,2) |  |
| `raw_record` | jsonb, not null |  |
| `rejected_at` | timestamp with time zone, not null |  |

## 3. `core`

### `core.dim_contract_rate`

Contract rates by payer, procedure (or ALL) and period.

| Column | Type | Meaning |
|---|---|---|
| `contract_rate_id` | bigint, not null |  |
| `contract_id` | text |  |
| `payer_id` | text, not null | payer code P01-P06 |
| `procedure_scope` | text, not null |  |
| `procedure_code` | text |  |
| `rate_type` | text, not null |  |
| `rate_value` | numeric(14,4), not null |  |
| `unit_basis` | text |  |
| `effective_from` | date, not null |  |
| `effective_to` | date, not null |  |
| `lesser_of_charges` | boolean, not null |  |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |

### `core.dim_drug`

Drugs by HCPCS with NDC and package size.

| Column | Type | Meaning |
|---|---|---|
| `hcpcs_code` | text, not null |  |
| `drug_description` | text |  |
| `ndc_code` | text |  |
| `billing_units_per_package` | numeric(12,3) |  |

### `core.dim_payer`

Payers (canonical name from contracts; ach_company_name used to recognise payer credits).

| Column | Type | Meaning |
|---|---|---|
| `payer_id` | text, not null | payer code P01-P06 |
| `payer_name` | text, not null |  |
| `payer_type` | text |  |
| `payer_tax_id` | text |  |
| `ach_company_name` | text |  |

### `core.dim_procedure`

Procedures seen on claims (service line and revenue code = most frequent).

| Column | Type | Meaning |
|---|---|---|
| `procedure_code` | text, not null |  |
| `service_line_code` | text |  |
| `revenue_code` | text |  |
| `is_drug` | boolean, not null |  |

### `core.dim_reason_code`

Adjustment reason codes with their bucket in the control identity and denial category.

| Column | Type | Meaning |
|---|---|---|
| `group_code` | text, not null |  |
| `reason_code` | text, not null |  |
| `bucket` | text, not null |  |
| `category` | text |  |
| `description` | text, not null |  |

### `core.dim_service_line`

Service lines (reference data).

| Column | Type | Meaning |
|---|---|---|
| `service_line_code` | text, not null |  |
| `service_line_name` | text, not null |  |

### `core.fact_bank_txn`

Cash: one row per bank statement transaction.

| Column | Type | Meaning |
|---|---|---|
| `bank_txn_id` | bigint, not null | surrogate key of core.fact_bank_txn; ascending = statement order |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |
| `account_number` | text |  |
| `posting_date` | date, not null | bank posting date |
| `value_date` | date |  |
| `transaction_type` | text |  |
| `description` | text |  |
| `amount` | numeric(14,2), not null |  |
| `running_balance` | numeric(14,2), not null | statement balance after the row |
| `bank_reference` | text, not null |  |
| `customer_reference` | text |  |
| `txn_category` | text, not null | PAYER_EFT, LOCKBOX, CARD_SETTLEMENT, INTEREST, FEE, TRANSFER, VENDOR_PAYMENT, OTHER_* |
| `trace_no_extracted` | text | trace parsed from the bank description (may be truncated) |
| `payer_id_inferred` | text | payer recognised from the ACH company name |
| `lockbox_item_count` | integer |  |

### `core.fact_claim_event`

Claim life cycle: one row per status event (input to the Markov chain).

| Column | Type | Meaning |
|---|---|---|
| `claim_event_id` | bigint, not null |  |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |
| `event_id` | text |  |
| `claim_id` | text, not null | hospital claim number |
| `status` | text, not null | current state |
| `event_ts` | timestamp without time zone, not null |  |
| `event_date` | date, not null |  |
| `reason_code` | text |  |
| `reason_category` | text |  |
| `appeal_level` | integer |  |
| `source_system` | text |  |
| `recorded_by` | text |  |
| `note` | text |  |
| `claim_known` | boolean, not null |  |

### `core.fact_claim_line`

Claim submission: one row per billed line.

| Column | Type | Meaning |
|---|---|---|
| `claim_line_id` | bigint, not null | surrogate key of core.fact_claim_line |
| `claim_id` | text, not null | hospital claim number |
| `claim_id_norm` | text, not null | normalised claim number (stg.norm_id) |
| `line_no` | integer, not null | claim line number |
| `payer_id` | text, not null | payer code P01-P06 |
| `patient_account_no` | text |  |
| `member_id_norm` | text | member id, upper-case letters and digits |
| `service_line_code` | text |  |
| `facility_name` | text |  |
| `form_type` | text |  |
| `procedure_code` | text, not null |  |
| `modifier_1` | text |  |
| `revenue_code` | text |  |
| `ndc_code` | text |  |
| `diagnosis_code_1` | text |  |
| `diagnosis_code_2` | text |  |
| `units` | numeric(12,3) |  |
| `line_charge` | numeric(14,2), not null | billed charge (remittance: echoed charge, negative on reversals) |
| `service_date` | date, not null | date of service |
| `submission_date` | date, not null | date sent |
| `claim_total_charge` | numeric(14,2) |  |
| `line_item_control_no` | text |  |
| `lic_norm` | text | normalised line item control number |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |

### `core.fact_drug_dispense`

Drug administrations and expiry waste.

| Column | Type | Meaning |
|---|---|---|
| `dispense_id` | text, not null |  |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |
| `event_ts` | timestamp without time zone, not null |  |
| `event_date` | date, not null |  |
| `event_type` | text, not null | event kind |
| `facility_name` | text |  |
| `ndc_code` | text |  |
| `hcpcs_code` | text |  |
| `lot_no` | text |  |
| `billing_units` | numeric(12,3), not null |  |
| `patient_account_no` | text |  |
| `claim_id` | text | hospital claim number |
| `claim_line_no` | integer |  |
| `claim_line_id` | bigint | surrogate key of core.fact_claim_line |
| `recorded_by` | text |  |

### `core.fact_drug_purchase`

Drug purchases by lot.

| Column | Type | Meaning |
|---|---|---|
| `purchase_line_id` | bigint, not null |  |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |
| `invoice_no` | text, not null |  |
| `invoice_line_no` | integer, not null |  |
| `invoice_date` | date, not null |  |
| `due_date` | date |  |
| `wholesaler_id` | text |  |
| `wholesaler_name` | text |  |
| `order_type` | text |  |
| `ndc_code` | text, not null |  |
| `hcpcs_code` | text |  |
| `product_description` | text |  |
| `lot_no` | text, not null |  |
| `expiry_date` | date, not null |  |
| `qty_packages` | integer, not null |  |
| `billing_units_per_package` | numeric(12,3), not null |  |
| `billing_units` | numeric(14,3), not null |  |
| `package_price` | numeric(14,2), not null |  |
| `extended_amount` | numeric(14,2), not null |  |
| `unit_cost` | numeric(14,6), not null | package price / billing units per package |

### `core.fact_remit_adjustment`

Remittance adjustments in long format.

| Column | Type | Meaning |
|---|---|---|
| `remit_line_id` | bigint, not null | surrogate key of core.fact_remit_line |
| `adj_seq` | smallint, not null |  |
| `group_code` | text, not null |  |
| `reason_code` | text, not null |  |
| `amount` | numeric(14,2), not null |  |
| `bucket` | text, not null |  |

### `core.fact_remit_line`

Remittance: one row per remittance line, adjustments bucketed.

| Column | Type | Meaning |
|---|---|---|
| `remit_line_id` | bigint, not null | surrogate key of core.fact_remit_line |
| `src_file` | text, not null | raw file label the row came from |
| `src_row_no` | integer, not null | row number in that file (1 = first data row) |
| `remit_file_id` | text |  |
| `payer_id` | text, not null | payer code P01-P06 |
| `trace_no` | text, not null |  |
| `trace_key` | text, not null | payer_id || ':' || trace_no; one payer payment |
| `payment_method` | text |  |
| `payment_date` | date, not null | payer effective payment date |
| `file_payment_amount` | numeric(14,2) |  |
| `patient_control_no_raw` | text | claim number as printed by the payer |
| `patient_control_no_norm` | text | same, normalised |
| `payer_claim_control_no` | text |  |
| `claim_status_code` | text |  |
| `is_reversal` | boolean, not null | claim status 22 (reverses an earlier posting) |
| `is_provider_level` | boolean, not null | PLB row: provider-level payment, not a claim line |
| `member_id_norm` | text | member id, upper-case letters and digits |
| `service_date` | date, not null | date of service |
| `procedure_code` | text |  |
| `units` | numeric(12,3) |  |
| `line_item_control_no` | text |  |
| `lic_norm` | text | normalised line item control number |
| `line_charge` | numeric(14,2), not null | billed charge (remittance: echoed charge, negative on reversals) |
| `allowed_amount` | numeric(14,2), not null | payer allowed |
| `paid_amount` | numeric(14,2), not null | payer paid |
| `contractual_amount` | numeric(14,2), not null | sum of CONTRACTUAL adjustments (CO-45) |
| `denied_amount` | numeric(14,2), not null | sum of DENIAL adjustments |
| `patient_resp_amount` | numeric(14,2), not null | sum of PR adjustments |
| `denial_group_code` | text |  |
| `denial_reason_code` | text |  |
| `denial_category` | text |  |
| `has_unknown_reason` | boolean, not null |  |
| `line_hash` | text, not null |  |

### `core.v_claim` (view)

View: one row per claim.

| Column | Type | Meaning |
|---|---|---|
| `claim_id` | text | hospital claim number |
| `payer_id` | text | payer code P01-P06 |
| `service_line_code` | text |  |
| `service_date` | date | date of service |
| `submission_date` | date | date sent |
| `patient_account_no` | text |  |
| `n_lines` | bigint |  |
| `total_charge` | numeric |  |

## 4. `recon`

### `recon.claim_line_balance`

Control identity per claim line (CHECK constraint) plus contract expectation.

| Column | Type | Meaning |
|---|---|---|
| `claim_line_id` | bigint, not null | surrogate key of core.fact_claim_line |
| `claim_id` | text, not null | hospital claim number |
| `line_no` | integer, not null | claim line number |
| `payer_id` | text, not null | payer code P01-P06 |
| `service_line_code` | text |  |
| `procedure_code` | text |  |
| `service_date` | date, not null | date of service |
| `submission_date` | date, not null | date sent |
| `billed` | numeric(14,2), not null | claim line charge |
| `paid` | numeric(14,2), not null | sum of posted paid |
| `contractual` | numeric(14,2), not null | sum of posted contractual adjustments |
| `denied` | numeric(14,2), not null | sum of posted denials |
| `patient_resp` | numeric(14,2), not null | sum of posted patient responsibility |
| `open_balance` | numeric(14,2), not null | billed - paid - contractual - denied - patient_resp (negative = credit balance) |
| `payer_allowed` | numeric(14,2), not null | sum of payer allowed over postings |
| `expected_allowed` | numeric(14,2) | contract allowed: rate x units or % of charge, lesser of charge |
| `contract_rate_id` | bigint |  |
| `underpaid_amount` | numeric(14,2), not null | expected_allowed - payer_allowed when above the UP10 tolerance |
| `n_postings` | integer, not null |  |
| `first_remit_date` | date |  |
| `last_remit_date` | date |  |

### `recon.claim_line_status`

Current status per claim line.

| Column | Type | Meaning |
|---|---|---|
| `claim_line_id` | bigint, not null | surrogate key of core.fact_claim_line |
| `status` | text, not null | current state |
| `status_since` | date |  |
| `last_event_type` | text |  |
| `n_events` | integer, not null |  |
| `is_underpaid` | boolean, not null |  |
| `claim_status` | text |  |
| `is_written_off` | boolean, not null |  |

### `recon.claim_line_status_history`

State machine: one row per posting with status before/after.

| Column | Type | Meaning |
|---|---|---|
| `claim_line_id` | bigint, not null | surrogate key of core.fact_claim_line |
| `seq` | integer, not null |  |
| `remit_line_id` | bigint, not null | surrogate key of core.fact_remit_line |
| `event_date` | date, not null |  |
| `event_type` | text, not null | event kind |
| `status_before` | text, not null |  |
| `status_after` | text, not null |  |
| `transition_allowed` | boolean, not null |  |

### `recon.claim_remit_match`

Remittance line -> claim line matches.

| Column | Type | Meaning |
|---|---|---|
| `remit_line_id` | bigint, not null | surrogate key of core.fact_remit_line |
| `claim_line_id` | bigint, not null | surrogate key of core.fact_claim_line |
| `rule_id` | text, not null | recon.match_rule that produced the row |
| `run_id` | bigint | recon.run_log run that produced the row |
| `matched_at` | timestamp with time zone, not null |  |

### `recon.control_totals`

Layer and identity checks of the latest run, to the cent.

| Column | Type | Meaning |
|---|---|---|
| `check_id` | integer, not null |  |
| `run_id` | bigint | recon.run_log run that produced the row |
| `check_name` | text, not null |  |
| `scope` | text, not null |  |
| `expected` | numeric(18,2), not null |  |
| `actual` | numeric(18,2), not null |  |
| `difference` | numeric(18,2) |  |
| `passed` | boolean, not null |  |
| `detail` | text |  |

### `recon.daily_recon`

One row per calendar day: bank chain, payer cash, remittance-to-cash split; status set by recon.close_day.

| Column | Type | Meaning |
|---|---|---|
| `recon_date` | date, not null |  |
| `opening_balance` | numeric(14,2) |  |
| `total_credits` | numeric(14,2), not null |  |
| `total_debits` | numeric(14,2), not null |  |
| `closing_balance_computed` | numeric(14,2) |  |
| `closing_balance_statement` | numeric(14,2) |  |
| `balance_difference` | numeric(14,2) |  |
| `payer_credits` | numeric(14,2), not null |  |
| `payer_credits_matched` | numeric(14,2), not null |  |
| `payer_credits_unapplied` | numeric(14,2), not null |  |
| `non_payer_credits` | numeric(14,2), not null |  |
| `remit_traces` | integer, not null |  |
| `remit_total` | numeric(14,2), not null |  |
| `remit_matched_same_day` | numeric(14,2), not null |  |
| `remit_timing_difference` | numeric(14,2), not null |  |
| `remit_unmatched` | numeric(14,2), not null |  |
| `remit_difference` | numeric(14,2), not null |  |
| `lines_billed` | integer, not null |  |
| `billed_amount` | numeric(14,2), not null |  |
| `lines_posted` | integer, not null |  |
| `posted_paid` | numeric(14,2), not null |  |
| `status` | text, not null | current state |
| `closed_at` | timestamp with time zone |  |
| `numbers_hash` | text, not null |  |

### `recon.day_close_log`

Every close attempt (CLOSED / REFUSED with message).

| Column | Type | Meaning |
|---|---|---|
| `log_id` | bigint, not null |  |
| `recon_date` | date, not null |  |
| `attempted_at` | timestamp with time zone, not null |  |
| `outcome` | text, not null |  |
| `message` | text |  |

### `recon.exception_queue`

Every unresolved item with a type, severity, amount and reason.

| Column | Type | Meaning |
|---|---|---|
| `exception_id` | bigint, not null |  |
| `exception_key` | text, not null | stable natural key: TYPE:id |
| `exception_type` | text, not null | see docs/reconciliation_rulebook.md |
| `severity` | text, not null | HIGH, MEDIUM, LOW, INFO |
| `claim_id` | text | hospital claim number |
| `claim_line_id` | bigint | surrogate key of core.fact_claim_line |
| `remit_line_id` | bigint | surrogate key of core.fact_remit_line |
| `trace_key` | text | payer_id || ':' || trace_no; one payer payment |
| `bank_txn_id` | bigint | surrogate key of core.fact_bank_txn; ascending = statement order |
| `source_table` | text |  |
| `src_file` | text | raw file label the row came from |
| `src_row_no` | integer | row number in that file (1 = first data row) |
| `amount` | numeric(14,2) |  |
| `event_date` | date |  |
| `rule_id` | text | recon.match_rule that produced the row |
| `reason` | text, not null | plain-English reason, starts with AMBIGUOUS or POSSIBLE SPLIT when that applies |
| `status` | text, not null | current state |
| `first_seen_run` | bigint |  |
| `last_seen_run` | bigint |  |

### `recon.line_status_transition`

Allowed claim-line state transitions.

| Column | Type | Meaning |
|---|---|---|
| `from_status` | text, not null |  |
| `to_status` | text, not null |  |
| `description` | text, not null |  |

### `recon.match_rule`

Reconciliation rules as data (priority, tolerance, window).

| Column | Type | Meaning |
|---|---|---|
| `rule_id` | text, not null | recon.match_rule that produced the row |
| `stage` | text, not null |  |
| `method` | text, not null |  |
| `priority` | integer, not null |  |
| `description` | text, not null |  |
| `amount_tolerance` | numeric(14,2), not null |  |
| `date_window_days` | integer |  |
| `active` | boolean, not null |  |

### `recon.remit_bank_match`

Payment <-> bank deposit allocations (1:1, many payments to one lockbox deposit, or one payment split across deposits).

| Column | Type | Meaning |
|---|---|---|
| `trace_key` | text, not null | payer_id || ':' || trace_no; one payer payment |
| `bank_txn_id` | bigint, not null | surrogate key of core.fact_bank_txn; ascending = statement order |
| `allocated_amount` | numeric(14,2), not null | part of the deposit attributed to the payment |
| `rule_id` | text, not null | recon.match_rule that produced the row |
| `run_id` | bigint | recon.run_log run that produced the row |
| `matched_at` | timestamp with time zone, not null |  |

### `recon.remit_duplicate`

Remittance lines excluded as file duplicates.

| Column | Type | Meaning |
|---|---|---|
| `remit_line_id` | bigint, not null | surrogate key of core.fact_remit_line |
| `duplicate_of_remit_line_id` | bigint, not null |  |
| `rule_id` | text, not null | recon.match_rule that produced the row |
| `run_id` | bigint | recon.run_log run that produced the row |

### `recon.remit_trace`

One row per payer payment (trace) with header and line totals.

| Column | Type | Meaning |
|---|---|---|
| `trace_key` | text, not null | payer_id || ':' || trace_no; one payer payment |
| `payer_id` | text, not null | payer code P01-P06 |
| `trace_no` | text, not null |  |
| `payment_method` | text |  |
| `payment_date` | date, not null | payer effective payment date |
| `n_files` | integer, not null |  |
| `header_total` | numeric(14,2), not null | sum of file payment amounts of the payment |
| `lines_total_net` | numeric(14,2), not null | sum of paid on the payment's lines, duplicates excluded |
| `lines_total_all` | numeric(14,2), not null |  |
| `n_lines` | integer, not null |  |
| `n_duplicate_lines` | integer, not null |  |
| `n_rejected_lines` | integer, not null |  |

### `recon.reversal_link`

Reversal -> original posting pairs.

| Column | Type | Meaning |
|---|---|---|
| `reversal_remit_line_id` | bigint, not null |  |
| `original_remit_line_id` | bigint, not null |  |
| `claim_line_id` | bigint, not null | surrogate key of core.fact_claim_line |
| `rule_id` | text, not null | recon.match_rule that produced the row |

### `recon.run_log`

One row per reconciliation run with a JSON summary.

| Column | Type | Meaning |
|---|---|---|
| `run_id` | bigint, not null | recon.run_log run that produced the row |
| `kind` | text, not null |  |
| `started_at` | timestamp with time zone, not null |  |
| `summary` | jsonb |  |

### `recon.run_parameter`

As-of date and windows used by reconciliation.

| Column | Type | Meaning |
|---|---|---|
| `name` | text, not null |  |
| `value` | text, not null |  |
| `note` | text |  |

## 5. `mart`

### `mart.ar_aging`

Q1: open receivables by payer and age since submission.
Label: `synthetic_record`.

| Column | Type | Meaning |
|---|---|---|
| `payer_id` | text | payer code P01-P06 |
| `aging_bucket` | text |  |
| `n_open_lines` | bigint |  |
| `open_billed` | numeric(14,2) |  |
| `open_expected` | numeric(14,2) |  |
| `n_denied_in_play` | bigint |  |
| `denied_in_play_expected` | numeric(14,2) |  |
| `credit_balance` | numeric(14,2) |  |
| `written_off_open_billed` | numeric(14,2) |  |
| `as_of_date` | date |  |
| `label` | text | data label (DESIGN.md section 8) |

### `mart.cash_position`

Q1: monthly billed, remitted, deposited, matched, unapplied and bank balance.
Label: `synthetic_record`.

| Column | Type | Meaning |
|---|---|---|
| `month` | date |  |
| `billed_charges` | numeric(14,2) |  |
| `payer_paid_per_remittance` | numeric(14,2) |  |
| `payer_cash_deposited` | numeric(14,2) |  |
| `payer_cash_matched` | numeric(14,2) |  |
| `unapplied_cash` | numeric(14,2) |  |
| `non_payer_credits` | numeric(14,2) |  |
| `debits` | numeric(14,2) |  |
| `closing_bank_balance` | numeric(14,2) |  |
| `remitted_not_deposited_eom` | numeric(14,2) |  |
| `label` | text | data label (DESIGN.md section 8) |

### `mart.cash_stuck`

Q1: as-of snapshot of where money is waiting.
Label: `synthetic_record`.

| Column | Type | Meaning |
|---|---|---|
| `sort` | integer |  |
| `bucket` | text |  |
| `description` | text |  |
| `n_items` | bigint |  |
| `amount` | numeric |  |
| `billed_basis` | numeric |  |
| `as_of_date` | date |  |
| `label` | text | data label (DESIGN.md section 8) |

### `mart.pnl_by_payer_service`

Q1: revenue waterfall by payer x service line x service month (drug cost at average invoice cost).
Label: `synthetic_record`.

| Column | Type | Meaning |
|---|---|---|
| `payer_id` | text | payer code P01-P06 |
| `service_line_code` | text |  |
| `service_month` | date |  |
| `n_claims` | bigint |  |
| `n_lines` | bigint |  |
| `billed` | numeric(14,2) | claim line charge |
| `contractual_adjustment` | numeric(14,2) |  |
| `denied` | numeric(14,2) | sum of posted denials |
| `patient_responsibility` | numeric(14,2) |  |
| `payer_paid` | numeric(14,2) |  |
| `open_balance` | numeric(14,2) | billed - paid - contractual - denied - patient_resp (negative = credit balance) |
| `contract_expected` | numeric(14,2) |  |
| `underpaid_vs_contract` | numeric(14,2) |  |
| `net_revenue_recognised` | numeric(14,2) |  |
| `drug_cost_avg_basis` | numeric(14,2) |  |
| `margin_after_drug_cost` | numeric(14,2) |  |
| `label` | text | data label (DESIGN.md section 8) |

### `mart.table_label`

Data label of each mart table.

| Column | Type | Meaning |
|---|---|---|
| `table_name` | text, not null |  |
| `label` | text, not null | data label (DESIGN.md section 8) |
| `note` | text |  |

## 6. Key functions and procedures

| Object | Purpose |
|---|---|
| `stg.process_new()` | type and stage raw rows not yet staged; rejects with reasons |
| `core.process_new()` | move staged rows into the core model |
| `recon.run_all(kind)` | matching, balances, state machine, exceptions, controls, daily recon |
| `recon.run_matching(stage, run)` | run the active rules of a stage in priority order |
| `recon.close_day(date)` | verify one day and mark it CLOSED, or raise and refuse |
| `recon.daily_numbers(from, to)` | fresh recomputation of the daily reconciliation numbers |
| `mart.refresh_q1()` | rebuild the question-1 marts |
