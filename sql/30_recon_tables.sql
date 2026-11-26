-- 30_recon_tables.sql
-- Reconciliation tables. Match tables are append-only (incremental runs only add matches for
-- rows still unmatched). Derived tables (traces, balances, status, controls) are rebuilt on
-- every run from core + matches, so they can never drift from their inputs.

create table recon.run_log (
    run_id     bigserial primary key,
    kind       text not null,          -- FULL or INCREMENTAL
    started_at timestamptz not null default now(),
    summary    jsonb
);

create table recon.remit_duplicate (
    remit_line_id              bigint primary key references core.fact_remit_line,
    duplicate_of_remit_line_id bigint not null references core.fact_remit_line,
    rule_id                    text not null references recon.match_rule,
    run_id                     bigint
);

create table recon.claim_remit_match (
    remit_line_id bigint primary key references core.fact_remit_line,
    claim_line_id bigint not null references core.fact_claim_line,
    rule_id       text not null references recon.match_rule,
    run_id        bigint,
    matched_at    timestamptz not null default now()
);
create index on recon.claim_remit_match (claim_line_id);

create table recon.reversal_link (
    reversal_remit_line_id bigint primary key references core.fact_remit_line,
    original_remit_line_id bigint not null references core.fact_remit_line,
    claim_line_id          bigint not null references core.fact_claim_line,
    rule_id                text not null references recon.match_rule
);

-- one row per payer payment (trace). header_total = what the payer said it paid (sum of the
-- file-level payment amounts over the distinct remittance files carrying this trace).
create table recon.remit_trace (
    trace_key          text primary key,
    payer_id           text not null,
    trace_no           text not null,
    payment_method     text,
    payment_date       date not null,
    n_files            integer not null,
    header_total       numeric(14,2) not null,
    lines_total_net    numeric(14,2) not null,   -- excluding file duplicates
    lines_total_all    numeric(14,2) not null,
    n_lines            integer not null,
    n_duplicate_lines  integer not null,
    n_rejected_lines   integer not null
);
create index on recon.remit_trace (payment_date);

-- one row per (payment, deposit): usually 1:1; many payments -> one lockbox deposit (RB30);
-- one payment -> several deposits (RB25 split). allocated_amount always sums to the deposit and to the payment.
create table recon.remit_bank_match (
    trace_key        text not null,
    bank_txn_id      bigint not null references core.fact_bank_txn,
    allocated_amount numeric(14,2) not null,
    rule_id          text not null references recon.match_rule,
    run_id           bigint,
    matched_at       timestamptz not null default now(),
    primary key (trace_key, bank_txn_id)
);
create index on recon.remit_bank_match (bank_txn_id);

-- Control identity per claim line, to the cent:
--   billed = paid + contractual + denied + patient_resp + open_balance
create table recon.claim_line_balance (
    claim_line_id     bigint primary key references core.fact_claim_line,
    claim_id          text not null,
    line_no           integer not null,
    payer_id          text not null,
    service_line_code text,
    procedure_code    text,
    service_date      date not null,
    submission_date   date not null,
    billed            numeric(14,2) not null,
    paid              numeric(14,2) not null,
    contractual       numeric(14,2) not null,
    denied            numeric(14,2) not null,
    patient_resp      numeric(14,2) not null,
    open_balance      numeric(14,2) not null,
    payer_allowed     numeric(14,2) not null,
    expected_allowed  numeric(14,2),          -- from the contract; NULL if no rate applies
    contract_rate_id  bigint,
    underpaid_amount  numeric(14,2) not null default 0,
    n_postings        integer not null,
    first_remit_date  date,
    last_remit_date   date,
    constraint control_identity check (billed = paid + contractual + denied + patient_resp + open_balance)
);
create index on recon.claim_line_balance (payer_id, submission_date);

create table recon.claim_line_status_history (
    claim_line_id      bigint not null,
    seq                integer not null,
    remit_line_id      bigint not null,
    event_date         date not null,
    event_type         text not null,
    status_before      text not null,
    status_after       text not null,
    transition_allowed boolean not null,
    primary key (claim_line_id, seq)
);

create table recon.claim_line_status (
    claim_line_id   bigint primary key references core.fact_claim_line,
    status          text not null,     -- SUBMITTED, PAID, PARTIAL, DENIED, REVERSED, CREDIT_BALANCE
    status_since    date,
    last_event_type text,
    n_events        integer not null,
    is_underpaid    boolean not null,
    claim_status    text,              -- latest claim-level status from the work-queue feed
    is_written_off  boolean not null,
    has_orphan_reversal boolean not null default false  -- a reversal on this line negates a posting we never received
);

create table recon.exception_queue (
    exception_id   bigserial primary key,
    exception_key  text not null unique,
    exception_type text not null,
    severity       text not null check (severity in ('HIGH', 'MEDIUM', 'LOW', 'INFO')),
    claim_id       text,
    claim_line_id  bigint,
    remit_line_id  bigint,
    trace_key      text,
    bank_txn_id    bigint,
    source_table   text,
    src_file       text,
    src_row_no     integer,
    amount         numeric(14,2),
    event_date     date,
    rule_id        text,
    reason         text not null,
    status         text not null default 'OPEN' check (status in ('OPEN', 'CLEARED')),
    first_seen_run bigint,
    last_seen_run  bigint
);
create index on recon.exception_queue (exception_type, status);
create index on recon.exception_queue (event_date);

create table recon.control_totals (
    check_id    serial primary key,
    run_id      bigint,
    check_name  text not null,
    scope       text not null,
    expected    numeric(18,2) not null,
    actual      numeric(18,2) not null,
    difference  numeric(18,2) generated always as (actual - expected) stored,
    passed      boolean not null,
    detail      text
);

create table recon.daily_recon (
    recon_date                date primary key,
    opening_balance           numeric(14,2),
    total_credits             numeric(14,2) not null,
    total_debits              numeric(14,2) not null,
    closing_balance_computed  numeric(14,2),
    closing_balance_statement numeric(14,2),
    balance_difference        numeric(14,2),
    payer_credits             numeric(14,2) not null,
    payer_credits_matched     numeric(14,2) not null,
    payer_credits_unapplied   numeric(14,2) not null,
    non_payer_credits         numeric(14,2) not null,
    remit_traces              integer not null,
    remit_total               numeric(14,2) not null,
    remit_matched_same_day    numeric(14,2) not null,
    remit_timing_difference   numeric(14,2) not null,
    remit_unmatched           numeric(14,2) not null,
    remit_difference          numeric(14,2) not null,
    lines_billed              integer not null,
    billed_amount             numeric(14,2) not null,
    lines_posted              integer not null,
    posted_paid               numeric(14,2) not null,
    status                    text not null default 'OPEN' check (status in ('OPEN', 'CLOSED', 'REOPENED')),
    closed_at                 timestamptz,
    numbers_hash              text not null
);

create table recon.day_close_log (
    log_id       bigserial primary key,
    recon_date   date not null,
    attempted_at timestamptz not null default now(),
    outcome      text not null,     -- CLOSED or REFUSED
    message      text
);
