-- 10_stg_tables.sql
-- Typed, trimmed, normalised copies of the raw documents. A raw row ends up in exactly one of
-- its stg table or stg.reject (control total RAW_ROWS_ACCOUNTED checks this).

create table stg.reject (
    reject_id     bigserial primary key,
    source_table  text not null,
    src_file      text not null,
    src_row_no    integer not null,
    reason_code   text not null,
    reason_detail text,
    amount        numeric(14,2),           -- parsed money amount when one could be parsed
    raw_record    jsonb not null,
    rejected_at   timestamptz not null default now(),
    unique (source_table, src_file, src_row_no)
);

create table stg.claim_line (
    src_file text not null, src_row_no integer not null,
    claim_id text not null, claim_id_norm text not null, form_type text, bill_type text,
    facility_name text, facility_npi text, patient_account_no text, member_id text, member_id_norm text,
    payer_id text not null, payer_name text, service_line text, attending_provider_id text,
    statement_from_date date, statement_to_date date, submission_date date not null,
    claim_total_charge numeric(14,2), line_no integer not null, line_item_control_no text, lic_norm text,
    revenue_code text, procedure_code text not null, modifier_1 text, ndc_code text,
    diagnosis_code_1 text, diagnosis_code_2 text, service_date date not null, units numeric(12,3),
    line_charge numeric(14,2) not null,
    primary key (src_file, src_row_no)
);

create table stg.remit_line (
    src_file text not null, src_row_no integer not null,
    remit_file_id text, payer_id text not null, payer_name text, payer_tax_id text, payee_name text,
    payee_npi text, trace_no text not null, payment_method text, payment_date date not null,
    file_payment_amount numeric(14,2), patient_control_no_raw text, patient_control_no_norm text,
    payer_claim_control_no text, claim_status_code text, claim_charge_amount numeric(14,2),
    claim_paid_amount numeric(14,2), member_id text, member_id_norm text, service_date date not null,
    procedure_code text, modifier_1 text, units numeric(12,3), line_item_control_no text, lic_norm text,
    line_charge numeric(14,2) not null, allowed_amount numeric(14,2) not null, paid_amount numeric(14,2) not null,
    adj_group_1 text, adj_reason_1 text, adj_amount_1 numeric(14,2),
    adj_group_2 text, adj_reason_2 text, adj_amount_2 numeric(14,2),
    adj_group_3 text, adj_reason_3 text, adj_amount_3 numeric(14,2),
    remark_code text, line_hash text not null,
    primary key (src_file, src_row_no)
);

create table stg.bank_txn (
    src_file text not null, src_row_no integer not null,
    account_number text, posting_date date not null, value_date date, transaction_type text,
    description text, debit numeric(14,2), credit numeric(14,2), amount numeric(14,2) not null,
    running_balance numeric(14,2) not null, bank_reference text not null, customer_reference text,
    trace_no_extracted text, lockbox_item_count integer,
    primary key (src_file, src_row_no)
);

create table stg.contract_rate (
    src_file text not null, src_row_no integer not null,
    contract_id text, payer_id text not null, payer_name text, payer_type text, payer_tax_id text,
    ach_company_name text, procedure_scope text not null, procedure_code text, rate_type text not null,
    rate_value numeric(14,4) not null, unit_basis text, effective_from date not null, effective_to date not null,
    lesser_of_charges boolean, notes text,
    primary key (src_file, src_row_no)
);

create table stg.drug_purchase (
    src_file text not null, src_row_no integer not null,
    invoice_no text not null, invoice_date date not null, wholesaler_name text, wholesaler_id text,
    ship_to_facility text, po_number text, invoice_line_no integer not null, ndc_code text not null,
    product_description text, lot_no text not null, expiry_date date not null, qty_packages integer not null,
    billing_units_per_package numeric(12,3) not null, unit_price numeric(14,2) not null,
    extended_amount numeric(14,2) not null, payment_terms text, due_date date, order_type text,
    primary key (src_file, src_row_no)
);

create table stg.drug_dispense (
    src_file text not null, src_row_no integer not null,
    dispense_id text not null, event_ts timestamp not null, event_type text not null, facility_name text,
    ndc_code text, hcpcs_code text, drug_description text, lot_no text, billing_units numeric(12,3) not null,
    patient_account_no text, claim_id text, claim_line_no integer, recorded_by text,
    primary key (src_file, src_row_no)
);

create table stg.claim_event (
    src_file text not null, src_row_no integer not null,
    event_id text, claim_id text not null, status text not null, status_text_raw text,
    event_ts timestamp not null, reason_code text, appeal_level integer, source_system text,
    recorded_by text, note text,
    primary key (src_file, src_row_no)
);
