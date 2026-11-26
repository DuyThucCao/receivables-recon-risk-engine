-- 42_borrowing_base.sql  (owner: chain, cash and credit; executed by recon_engine.models.credit.run)
-- Question 5 (Bank credit analyst): how much can we lend against these receivables?
--
-- mart.bb_lines_asof(d)     one row per claim line submitted on or before d: billed, postings up to d
--                           (matched, non-duplicate, non-provider-level remittance lines), open balance
--                           and the claim's life-cycle state at d. Point in time: nothing dated after d.
--                           At the as-of date its open balance ties to recon.claim_line_balance
--                           (checked by credit.run and a test).
-- Output tables (rebuilt by credit.run):
--   mart.borrowing_base        one row per certificate date x payer (eligibility waterfall, availability,
--                              expected loss)
--   mart.borrowing_base_line   line-level eligibility with the first failing rule, per certificate date
--   mart.borrowing_base_stress interest-rate and slower-payment scenarios

create or replace function mart.bb_lines_asof(p_d date)
returns table (claim_line_id bigint, claim_id text, payer_id text, submission_date date, billed numeric,
               paid numeric, contractual numeric, denied numeric, patient_resp numeric, open_balance numeric,
               n_postings integer, claim_state text, claim_state_since date)
language sql stable as $$
    with post as (
        select m.claim_line_id, sum(r.paid_amount) paid, sum(r.contractual_amount) contractual,
               sum(r.denied_amount) denied, sum(r.patient_resp_amount) patient_resp, count(*)::int n
        from core.fact_remit_line r
        join recon.claim_remit_match m on m.remit_line_id = r.remit_line_id
        where r.payment_date <= p_d and not r.is_provider_level
          and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = r.remit_line_id)
        group by m.claim_line_id
    ), st as (
        select distinct on (e.claim_id) e.claim_id, e.status, e.event_date
        from core.fact_claim_event e where e.event_date <= p_d
        order by e.claim_id, e.event_ts desc, e.claim_event_id desc
    )
    select c.claim_line_id, c.claim_id, c.payer_id, c.submission_date, c.line_charge,
           coalesce(p.paid, 0), coalesce(p.contractual, 0), coalesce(p.denied, 0), coalesce(p.patient_resp, 0),
           c.line_charge - coalesce(p.paid, 0) - coalesce(p.contractual, 0) - coalesce(p.denied, 0) - coalesce(p.patient_resp, 0),
           coalesce(p.n, 0), coalesce(st.status, 'SUBMITTED'), coalesce(st.event_date, c.submission_date)
    from core.fact_claim_line c
    left join post p on p.claim_line_id = c.claim_line_id
    left join st on st.claim_id = c.claim_id
    where c.submission_date <= p_d
$$;

drop table if exists mart.borrowing_base;
create table mart.borrowing_base (
    certificate_date        date not null,
    payer_id                text not null,
    gross_ncv               numeric(14,2) not null,  -- net collectible value of open lines (model_estimate)
    inel_denied             numeric(14,2) not null,
    inel_disputed           numeric(14,2) not null,
    inel_aged               numeric(14,2) not null,
    inel_cross_aged         numeric(14,2) not null,
    eligible_pre_cap        numeric(14,2) not null,
    concentration_excess    numeric(14,2) not null,
    eligible                numeric(14,2) not null,
    credit_balance_reserve  numeric(14,2) not null,  -- credit balances owed back to the payer
    advance_rate            double precision not null,   -- financial_assumption
    availability            numeric(14,2) not null,
    pd                      double precision,        -- NCV-weighted Markov write-off probability (eligible lines)
    lgd                     double precision,        -- 1 - recovery on written-off claims
    ead                     numeric(14,2),
    expected_loss           numeric(14,2),
    label                   text not null default 'model_estimate',
    primary key (certificate_date, payer_id)
);

drop table if exists mart.borrowing_base_line;
create table mart.borrowing_base_line (
    certificate_date date not null,
    claim_line_id    bigint not null,
    payer_id         text not null,
    age_days         integer not null,
    claim_state      text not null,
    open_balance     numeric(14,2) not null,
    ncv              numeric(14,2) not null,
    reason           text not null,      -- ELIGIBLE, DENIED, DISPUTED, AGED, CROSS_AGED
    p_written_off    double precision,
    label            text not null default 'model_estimate',
    primary key (certificate_date, claim_line_id)
);

drop table if exists mart.borrowing_base_stress;
create table mart.borrowing_base_stress (
    certificate_date        date not null,
    scenario                text not null,
    rate_annual             double precision not null,
    drawn                   numeric(14,2) not null,
    interest_cost_annual    numeric(14,2) not null,
    interest_cost_13w       numeric(14,2) not null,
    carry_cost_of_delay     numeric(14,2) not null,   -- financing cost of waiting for open receivables to pay
    availability            numeric(14,2) not null,
    label                   text not null default 'model_estimate',
    primary key (certificate_date, scenario)
);
