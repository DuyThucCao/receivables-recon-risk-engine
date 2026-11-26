-- 01_raw_tables.sql
-- One table per raw document. Columns mirror the CSV headers exactly and are all text, so a
-- malformed value never stops a load; typing happens in stg with an explicit reject reason.

create table raw.claims_export (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    claim_id text, form_type text, bill_type text, facility_name text, facility_npi text,
    patient_account_no text, member_id text, payer_id text, payer_name text, service_line text,
    attending_provider_id text, statement_from_date text, statement_to_date text, submission_date text,
    claim_total_charge text, line_no text, line_item_control_no text, revenue_code text,
    procedure_code text, modifier_1 text, ndc_code text, diagnosis_code_1 text, diagnosis_code_2 text,
    service_date text, units text, line_charge text,
    primary key (_file, _row_no)
);

create table raw.remittance_advice (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    remit_file_id text, payer_id text, payer_name text, payer_tax_id text, payee_name text, payee_npi text,
    trace_no text, payment_method text, payment_date text, file_payment_amount text,
    patient_control_no text, payer_claim_control_no text, claim_status_code text,
    claim_charge_amount text, claim_paid_amount text, member_id text, service_date text,
    procedure_code text, modifier_1 text, units text, line_item_control_no text,
    line_charge_amount text, line_allowed_amount text, line_paid_amount text,
    adj_group_1 text, adj_reason_1 text, adj_amount_1 text,
    adj_group_2 text, adj_reason_2 text, adj_amount_2 text,
    adj_group_3 text, adj_reason_3 text, adj_amount_3 text, remark_code text,
    primary key (_file, _row_no)
);

create table raw.bank_statement (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    account_number text, posting_date text, value_date text, transaction_type text, description text,
    debit text, credit text, running_balance text, bank_reference text, customer_reference text,
    primary key (_file, _row_no)
);

create table raw.payer_contracts (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    contract_id text, payer_id text, payer_name text, payer_type text, payer_tax_id text,
    ach_company_name text, procedure_scope text, procedure_code text, rate_type text, rate text,
    unit_basis text, effective_from text, effective_to text, lesser_of_charges text, notes text,
    primary key (_file, _row_no)
);

create table raw.drug_purchase_invoices (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    invoice_no text, invoice_date text, wholesaler_name text, wholesaler_id text, ship_to_facility text,
    po_number text, invoice_line_no text, ndc_code text, product_description text, lot_no text,
    expiry_date text, qty_packages text, billing_units_per_package text, unit_price text,
    extended_amount text, payment_terms text, due_date text, order_type text,
    primary key (_file, _row_no)
);

create table raw.drug_dispense_log (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    dispense_id text, event_datetime text, event_type text, facility_name text, ndc_code text,
    hcpcs_code text, drug_description text, lot_no text, billing_units text, patient_account_no text,
    claim_id text, claim_line_no text, recorded_by text,
    primary key (_file, _row_no)
);

create table raw.claim_status_history (
    _file text not null, _row_no integer not null, _loaded_at timestamptz not null default now(),
    event_id text, claim_id text, status_text text, status_datetime text, reason_code text,
    appeal_level text, source_system text, recorded_by text, note text,
    primary key (_file, _row_no)
);
