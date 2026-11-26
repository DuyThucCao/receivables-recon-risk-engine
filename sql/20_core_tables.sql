-- 20_core_tables.sql
-- One data model per business process (claim submission, remittance, cash, claim life cycle,
-- drugs, contracts). Money is numeric(14,2) everywhere. src_file/src_row_no point back to raw.

-- ---------- contracts
create table core.dim_payer (
    payer_id         text primary key,
    payer_name       text not null,
    payer_type       text,
    payer_tax_id     text,
    ach_company_name text           -- how the payer's name appears in bank ACH descriptions
);

create table core.dim_contract_rate (
    contract_rate_id  bigserial primary key,
    contract_id       text,
    payer_id          text not null references core.dim_payer,
    procedure_scope   text not null check (procedure_scope in ('CODE', 'ALL')),
    procedure_code    text,
    rate_type         text not null check (rate_type in ('FEE_SCHEDULE', 'PCT_OF_CHARGE')),
    rate_value        numeric(14,4) not null,   -- dollars per unit, or percent of charge
    unit_basis        text,
    effective_from    date not null,
    effective_to      date not null,
    lesser_of_charges boolean not null default true,
    src_file text not null, src_row_no integer not null,
    unique (payer_id, procedure_scope, procedure_code, effective_from),
    check (effective_to >= effective_from)
);
create index on core.dim_contract_rate (payer_id, procedure_code, effective_from);

-- ---------- claim submission
create table core.dim_procedure (
    procedure_code    text primary key,
    service_line_code text,
    revenue_code      text,
    is_drug           boolean not null
);

create table core.fact_claim_line (
    claim_line_id        bigserial primary key,
    claim_id             text not null,
    claim_id_norm        text not null,
    line_no              integer not null,
    payer_id             text not null references core.dim_payer,
    patient_account_no   text,
    member_id_norm       text,
    service_line_code    text references core.dim_service_line,
    facility_name        text,
    form_type            text,
    procedure_code       text not null references core.dim_procedure,
    modifier_1           text,
    revenue_code         text,
    ndc_code             text,
    diagnosis_code_1     text,
    diagnosis_code_2     text,
    units                numeric(12,3),
    line_charge          numeric(14,2) not null check (line_charge >= 0),
    service_date         date not null,
    submission_date      date not null,
    claim_total_charge   numeric(14,2),
    line_item_control_no text,
    lic_norm             text,
    src_file text not null, src_row_no integer not null,
    unique (claim_id, line_no),
    unique (src_file, src_row_no)
);
create index on core.fact_claim_line (claim_id_norm);
create index on core.fact_claim_line (member_id_norm, service_date);
create index on core.fact_claim_line (submission_date);
create index on core.fact_claim_line (payer_id, service_date);

-- ---------- remittance
create table core.fact_remit_line (
    remit_line_id           bigserial primary key,
    src_file text not null, src_row_no integer not null,
    remit_file_id           text,
    payer_id                text not null,
    trace_no                text not null,
    trace_key               text not null,        -- payer_id || ':' || trace_no
    payment_method          text,                 -- ACH, CHK, NON (no money moved)
    payment_date            date not null,
    file_payment_amount     numeric(14,2),
    patient_control_no_raw  text,
    patient_control_no_norm text,
    payer_claim_control_no  text,
    claim_status_code       text,
    is_reversal             boolean not null,
    is_provider_level       boolean not null default false,  -- PLB row: provider-level payment, not a claim line
    member_id_norm          text,
    service_date            date not null,
    procedure_code          text,
    units                   numeric(12,3),
    line_item_control_no    text,
    lic_norm                text,
    line_charge             numeric(14,2) not null,
    allowed_amount          numeric(14,2) not null,
    paid_amount             numeric(14,2) not null,
    contractual_amount      numeric(14,2) not null,
    denied_amount           numeric(14,2) not null,
    patient_resp_amount     numeric(14,2) not null,
    denial_group_code       text,
    denial_reason_code      text,
    denial_category         text,
    has_unknown_reason      boolean not null default false,
    line_hash               text not null,
    unique (src_file, src_row_no)
);
create index on core.fact_remit_line (trace_key);
create index on core.fact_remit_line (payment_date);
create index on core.fact_remit_line (patient_control_no_raw);
create index on core.fact_remit_line (patient_control_no_norm);
create index on core.fact_remit_line (member_id_norm, service_date);

create table core.fact_remit_adjustment (
    remit_line_id bigint not null references core.fact_remit_line on delete cascade,
    adj_seq       smallint not null,
    group_code    text not null,
    reason_code   text not null,
    amount        numeric(14,2) not null,
    bucket        text not null,
    primary key (remit_line_id, adj_seq)
);


-- ---------- cash
create table core.fact_bank_txn (
    bank_txn_id        bigserial primary key,   -- statement order: ascending id = ascending statement row
    src_file text not null, src_row_no integer not null,
    account_number     text,
    posting_date       date not null,
    value_date         date,
    transaction_type   text,
    description        text,
    amount             numeric(14,2) not null,  -- credit positive, debit negative
    running_balance    numeric(14,2) not null,
    bank_reference     text not null unique,
    customer_reference text,
    txn_category       text not null,           -- PAYER_EFT, LOCKBOX, CARD_SETTLEMENT, INTEREST, FEE, TRANSFER, VENDOR_PAYMENT, OTHER
    trace_no_extracted text,
    payer_id_inferred  text,
    lockbox_item_count integer,
    unique (src_file, src_row_no)
);
create index on core.fact_bank_txn (posting_date, bank_txn_id);
create index on core.fact_bank_txn (trace_no_extracted);

-- ---------- claim life cycle
create table core.fact_claim_event (
    claim_event_id  bigserial primary key,
    src_file text not null, src_row_no integer not null,
    event_id        text,
    claim_id        text not null,
    status          text not null,
    event_ts        timestamp not null,
    event_date      date not null,
    reason_code     text,         -- e.g. 'CO-197'
    reason_category text,         -- from core.dim_reason_code
    appeal_level    integer,
    source_system   text,
    recorded_by     text,
    note            text,
    claim_known     boolean not null,   -- claim exists in core.fact_claim_line
    unique (src_file, src_row_no)
);
create index on core.fact_claim_event (claim_id, event_ts);

-- ---------- drugs
create table core.dim_drug (
    hcpcs_code                text primary key,
    drug_description          text,
    ndc_code                  text,
    billing_units_per_package numeric(12,3)
);

create table core.fact_drug_purchase (
    purchase_line_id          bigserial primary key,
    src_file text not null, src_row_no integer not null,
    invoice_no                text not null,
    invoice_line_no           integer not null,
    invoice_date              date not null,
    due_date                  date,
    wholesaler_id             text,
    wholesaler_name           text,
    order_type                text,
    ndc_code                  text not null,
    hcpcs_code                text,
    product_description       text,
    lot_no                    text not null,
    expiry_date               date not null,
    qty_packages              integer not null,
    billing_units_per_package numeric(12,3) not null,
    billing_units             numeric(14,3) not null,
    package_price             numeric(14,2) not null,
    extended_amount           numeric(14,2) not null,
    unit_cost                 numeric(14,6) not null,   -- per billing unit
    unique (invoice_no, invoice_line_no),
    unique (src_file, src_row_no)
);
create index on core.fact_drug_purchase (hcpcs_code, invoice_date);
create index on core.fact_drug_purchase (lot_no);

create table core.fact_drug_dispense (
    dispense_id        text primary key,
    src_file text not null, src_row_no integer not null,
    event_ts           timestamp not null,
    event_date         date not null,
    event_type         text not null check (event_type in ('ADMINISTER', 'WASTE_EXPIRED')),
    facility_name      text,
    ndc_code           text,
    hcpcs_code         text,
    lot_no             text,
    billing_units      numeric(12,3) not null,
    patient_account_no text,
    claim_id           text,
    claim_line_no      integer,
    claim_line_id      bigint,   -- resolved link to core.fact_claim_line when the claim reference is present
    recorded_by        text,
    units_imputed      boolean not null default false,  -- units unreadable in the raw log; imputed from the claim line (row also stays in stg.reject)
    unique (src_file, src_row_no)
);
create index on core.fact_drug_dispense (hcpcs_code, event_date);
