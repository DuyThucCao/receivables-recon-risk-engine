-- 03_reference.sql
-- Reference data and rule tables. Reconciliation rules are DATA: recon.match_rule lists every
-- rule with its priority and tolerance, and every match row stores the rule that produced it.

create table recon.run_parameter (
    name  text primary key,
    value text not null,
    note  text
);
insert into recon.run_parameter (name, value, note) values
    ('as_of_date', '2026-03-31', 'Snapshot date; overwritten by the pipeline from config'),
    ('in_transit_window_bdays', '5', 'An unmatched remittance younger than this many business days is a timing difference, older is REMIT_NO_DEPOSIT'),
    ('no_response_days', '90', 'Claim lines with no remittance this many days after submission go to the exception queue'),
    ('stale_reversal_days', '30', 'A line left reversed without repost for this many days goes to the exception queue');

create or replace function recon.param(p_name text) returns text
language sql stable as $$ select value from recon.run_parameter where name = p_name $$;

create table core.dim_service_line (
    service_line_code text primary key,
    service_line_name text not null
);
insert into core.dim_service_line values
    ('EMERGENCY', 'Emergency department'), ('PRIMARY_CARE', 'Primary care clinic'),
    ('LABORATORY', 'Outpatient laboratory'), ('RADIOLOGY', 'Diagnostic imaging'),
    ('CARDIOLOGY', 'Cardiology'), ('ORTHOPEDICS', 'Orthopedics'),
    ('INFUSION', 'Oncology and specialty infusion'), ('OPHTHALMOLOGY', 'Retina and ophthalmology');

-- CARC subset used by the payers. Descriptions are this project's paraphrases.
-- bucket drives the control identity: billed = paid + CONTRACTUAL + DENIAL + PATIENT_RESP + open.
create table core.dim_reason_code (
    group_code  text not null,
    reason_code text not null,
    bucket      text not null check (bucket in ('CONTRACTUAL', 'DENIAL', 'PATIENT_RESP')),
    category    text,
    description text not null,
    primary key (group_code, reason_code)
);
insert into core.dim_reason_code values
    ('CO', '45',  'CONTRACTUAL',  null,                'Charge exceeds the contracted allowance'),
    ('CO', '16',  'DENIAL',       'ADMINISTRATIVE',    'Claim lacks information needed to adjudicate'),
    ('CO', '29',  'DENIAL',       'ADMINISTRATIVE',    'Filing time limit expired'),
    ('CO', '27',  'DENIAL',       'ELIGIBILITY',       'Service after coverage ended'),
    ('CO', '31',  'DENIAL',       'ELIGIBILITY',       'Patient not identified as a member'),
    ('CO', '4',   'DENIAL',       'CODING',            'Procedure code inconsistent with modifier'),
    ('CO', '11',  'DENIAL',       'CODING',            'Diagnosis inconsistent with procedure'),
    ('CO', '197', 'DENIAL',       'PRIOR_AUTH',        'Required authorisation absent'),
    ('CO', '50',  'DENIAL',       'MEDICAL_NECESSITY', 'Not considered medically necessary'),
    ('CO', '96',  'DENIAL',       'NON_COVERED',       'Non-covered service'),
    ('PR', '1',   'PATIENT_RESP', null,                'Deductible'),
    ('PR', '2',   'PATIENT_RESP', null,                'Coinsurance'),
    ('PR', '3',   'PATIENT_RESP', null,                'Copayment');

-- status texts seen in the work-queue / clearinghouse feed -> canonical life-cycle state
create table stg.status_alias (
    alias  text primary key,   -- upper-cased, trimmed
    status text not null check (status in ('SUBMITTED','IN_REVIEW','PENDED','DENIED','APPEALED','PAID','WRITTEN_OFF'))
);
insert into stg.status_alias values
    ('SUBMITTED', 'SUBMITTED'), ('SUBMITTED TO CLEARINGHOUSE', 'SUBMITTED'),
    ('IN_REVIEW', 'IN_REVIEW'), ('IN REVIEW', 'IN_REVIEW'), ('ACCEPTED - IN REVIEW', 'IN_REVIEW'),
    ('PENDED', 'PENDED'), ('PEND - INFO REQUESTED', 'PENDED'), ('PEND - INFO REQ', 'PENDED'),
    ('DENIED', 'DENIED'), ('DENIAL RECEIVED', 'DENIED'),
    ('APPEALED', 'APPEALED'), ('APPEAL FILED', 'APPEALED'),
    ('PAID', 'PAID'), ('PAID IN FULL', 'PAID'),
    ('WRITTEN_OFF', 'WRITTEN_OFF'), ('WRITE OFF', 'WRITTEN_OFF'), ('W/O - FINAL', 'WRITTEN_OFF');

create table recon.match_rule (
    rule_id          text primary key,
    stage            text not null check (stage in ('DUPLICATE','CLAIM_REMIT','REVERSAL','REMIT_BANK','VARIANCE')),
    method           text not null,
    priority         integer not null,
    description      text not null,
    amount_tolerance numeric(14,2) not null default 0.00,
    date_window_days integer,
    active           boolean not null default true,
    unique (stage, priority)
);
insert into recon.match_rule values
    ('DUP10_SAME_TRACE_IDENTICAL', 'DUPLICATE', 'IDENTICAL_LINE_IN_TRACE', 10,
     'A remittance line repeated verbatim within the same payer trace is a file duplicate, unless the claim itself has that many identical lines (same procedure, date, charge) and no line control number tells them apart: copies beyond that count are excluded from posting.', 0.00, null, true),
    ('CR10_EXACT_CLAIM_ID', 'CLAIM_REMIT', 'CLAIM_ID_EXACT', 10,
     'Patient control number equals the hospital claim id exactly; line resolved by line item control number, else procedure + service date + charge.', 0.00, null, true),
    ('CR20_NORMALISED_CLAIM_ID', 'CLAIM_REMIT', 'CLAIM_ID_NORMALISED', 20,
     'Normalised patient control number (case, punctuation, PCN/CLM prefixes, leading zeros removed) equals the normalised claim id; same line resolution.', 0.00, null, true),
    -- (both identifier rules: an echoed line control number or member id that contradicts the candidate rules it out;
    --  identical sibling lines on one claim are paired in order within one claim payment)
    ('CR30_MEMBER_DATE_AMOUNT', 'CLAIM_REMIT', 'MEMBER_DATE_AMOUNT', 30,
     'Fallback when the identifier fails: same normalised member id, service date within the window and line charge within tolerance; an echoed line control number, then the procedure code, break ties; otherwise a unique candidate is required (ambiguous lines go to the exception queue).', 0.00, 0, true),
    ('REV10_REVERSAL_PAIR', 'REVERSAL', 'NEGATED_POSTING_ON_LINE', 10,
     'A reversal (claim status 22) is paired with the earlier posting on the same claim line whose paid, allowed and denied amounts it negates exactly.', 0.00, null, true),
    ('RB10_TRACE_EXACT', 'REMIT_BANK', 'TRACE_EXACT', 10,
     'Trace number extracted from the bank description equals the remittance trace and amounts are equal within tolerance; posting within the date window.', 0.00, 6, true),
    ('RB20_TRACE_NORMALISED', 'REMIT_BANK', 'TRACE_NORMALISED', 20,
     'Bank trace equals the remittance trace after removing leading zeros, or is a truncated prefix of at least 6 characters; equal amount, date window, unique candidate.', 0.00, 6, true),
    ('RB25_TRACE_SPLIT', 'REMIT_BANK', 'TRACE_SPLIT', 25,
     'One payment arriving as two or more bank credits that all carry its trace (exact or truncated): the credits must sum to the payment within tolerance, inside the date window, and belong to no other payment.', 0.00, 6, true),
    ('RB30_LOCKBOX_SUBSET', 'REMIT_BANK', 'LOCKBOX_SUBSET_SUM', 30,
     'Lockbox deposit with ITEMS n equals the sum of exactly n unmatched check remittances dated within the window before the deposit; the combination must be unique.', 0.00, 10, true),
    ('RB40_PAYER_AMOUNT_WINDOW', 'REMIT_BANK', 'PAYER_AMOUNT_WINDOW', 40,
     'EFT credit with no usable trace: payer recognised from the ACH company name, amount equal within tolerance, posting within the date window; unique on both sides.', 0.00, 4, true),
    ('UP10_BELOW_CONTRACT', 'VARIANCE', 'ALLOWED_BELOW_CONTRACT', 10,
     'Payer allowed amount below the contract allowed amount by more than the tolerance (dollars) on a line the payer has priced.', 1.00, null, true);

-- Claim-line state machine: which status changes a posting may cause. Anything else is an
-- ILLEGAL_TRANSITION exception.
create table recon.line_status_transition (
    from_status text not null,
    to_status   text not null,
    description text not null,
    primary key (from_status, to_status)
);
insert into recon.line_status_transition values
    ('SUBMITTED', 'PAID', 'first adjudication pays the line in full'),
    ('SUBMITTED', 'PARTIAL', 'first adjudication pays part, balance still open'),
    ('SUBMITTED', 'DENIED', 'first adjudication denies the line'),
    ('PARTIAL', 'PAID', 'remaining balance paid'),
    ('PARTIAL', 'PARTIAL', 'another partial amount'),
    ('PARTIAL', 'REVERSED', 'payer reverses the partial payment'),
    ('PARTIAL', 'CREDIT_BALANCE', 'payments exceed the expected amount'),
    ('PAID', 'PAID', 'late adjustment keeps the line closed'),
    ('PAID', 'PARTIAL', 'recoupment or adjustment re-opens a balance'),
    ('PAID', 'REVERSED', 'payer reverses the payment'),
    ('PAID', 'CREDIT_BALANCE', 'payer pays the line again'),
    ('DENIED', 'REVERSED', 'denial reversed after appeal or corrected claim'),
    ('DENIED', 'CREDIT_BALANCE', 'payment posted without the denial being reversed'),
    ('REVERSED', 'PAID', 'corrected repost pays the line'),
    ('REVERSED', 'PARTIAL', 'corrected repost pays part'),
    ('REVERSED', 'DENIED', 'corrected adjudication denies the line'),
    ('CREDIT_BALANCE', 'PAID', 'overpayment recouped'),
    ('CREDIT_BALANCE', 'PARTIAL', 'recoupment larger than the overpayment'),
    ('CREDIT_BALANCE', 'CREDIT_BALANCE', 'further postings, still overpaid');

create table mart.table_label (
    table_name text primary key,
    label      text not null check (label in ('observed_public','published_aggregate','synthetic_record','financial_assumption','model_estimate')),
    note       text
);
