-- 50_reserve_triangle.sql
-- Question 4 (payer actuary): how much is still owed on claims incurred but not yet paid?
-- Executed by recon_engine.models.reserve.run(), not by pipeline.py.
--
-- mart.dev_triangle_at(p_asof) returns the paid-amount development triangle as it could have been
-- built on p_asof: only claim lines submitted on or before p_asof and only remittance postings
-- dated on or before p_asof are used (no look-ahead). Rows are accident (service) months, columns
-- are development lags in months (payment month minus service month). Every cell of the
-- observed upper triangle is present, including zero cells, so the Python side can pivot it.
--
-- Paid = remittance paid_amount on lines matched to a claim line by the reconciliation core
-- (recon.claim_remit_match), net of reversals and recoupments; provider-level (PLB) rows excluded. File-level duplicate lines are
-- never in claim_remit_match, so they are excluded automatically. At the official as-of date the
-- triangle total ties to sum(recon.claim_line_balance.paid) to the cent (checked in run()).
--
-- The count columns support the frequency-severity view: a claim is counted once, at the lag of
-- its first positive payment.

drop function if exists mart.dev_triangle_at(date);
create function mart.dev_triangle_at(p_asof date)
returns table (
    accident_month               date,
    dev_lag                      integer,
    incremental_paid             numeric(14,2),
    cumulative_paid              numeric(14,2),
    n_claims_first_paid          integer,
    cumulative_claims_first_paid integer,
    n_claims_reported            integer,
    contract_allowed_reported    numeric(14,2)   -- exposure for Bornhuetter-Ferguson / Cape Cod
) language sql stable as $$
    with lines as (       -- claim lines known on p_asof
        select c.claim_line_id, c.claim_id, c.service_date, date_trunc('month', c.service_date)::date as acc,
               b.expected_allowed
        from core.fact_claim_line c
        join recon.claim_line_balance b on b.claim_line_id = c.claim_line_id
        where c.submission_date <= p_asof and c.service_date <= p_asof
    ), pay as (           -- postings known on p_asof
        select l.acc, l.claim_id, r.payment_date, r.paid_amount,
               ((extract(year from r.payment_date) - extract(year from l.acc)) * 12
                + extract(month from r.payment_date) - extract(month from l.acc))::int as lag
        from lines l
        join recon.claim_remit_match m on m.claim_line_id = l.claim_line_id
        join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
        where r.payment_date <= p_asof and not r.is_provider_level
    ), first_paid as (    -- one row per claim: lag of its first positive payment
        select distinct on (claim_id) claim_id, acc, lag
        from pay where paid_amount > 0
        order by claim_id, payment_date, lag
    ), grid as (          -- every observed cell, so zero cells exist
        select a::date as acc, g.lag
        from generate_series((select min(acc) from lines), (select max(acc) from lines), interval '1 month') a
        cross join lateral generate_series(0, ((extract(year from p_asof) - extract(year from a)) * 12
                                               + extract(month from p_asof) - extract(month from a))::int) as g(lag)
    ), inc as (
        select acc, lag, sum(paid_amount) as paid from pay group by 1, 2
    ), cnt as (
        select acc, lag, count(*)::int as n from first_paid group by 1, 2
    ), rep as (
        select acc, count(distinct claim_id)::int as n, coalesce(sum(expected_allowed), 0) as allowed from lines group by 1
    )
    select g.acc, g.lag,
           coalesce(i.paid, 0)::numeric(14,2),
           (sum(coalesce(i.paid, 0)) over w)::numeric(14,2),
           coalesce(k.n, 0),
           (sum(coalesce(k.n, 0)) over w)::int,
           coalesce(rp.n, 0),
           coalesce(rp.allowed, 0)::numeric(14,2)
    from grid g
    left join inc i on i.acc = g.acc and i.lag = g.lag
    left join cnt k on k.acc = g.acc and k.lag = g.lag
    left join rep rp on rp.acc = g.acc
    window w as (partition by g.acc order by g.lag rows between unbounded preceding and current row)
    order by g.acc, g.lag
$$;

-- Severity input for the frequency-severity view: one row per claim paid by p_asof, with the lag
-- of its first positive payment and its net paid amount as of p_asof.
drop function if exists mart.paid_claim_severity_at(date);
create function mart.paid_claim_severity_at(p_asof date)
returns table (claim_id text, payer_id text, service_line_code text, accident_month date,
               first_paid_lag integer, net_paid numeric(14,2))
language sql stable as $$
    with pay as (
        select c.claim_id, c.payer_id, c.service_line_code, date_trunc('month', c.service_date)::date as acc,
               r.payment_date, r.paid_amount
        from core.fact_claim_line c
        join recon.claim_remit_match m on m.claim_line_id = c.claim_line_id
        join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
        where c.submission_date <= p_asof and r.payment_date <= p_asof and not r.is_provider_level
    ), firstpay as (
        select claim_id, min(payment_date) filter (where paid_amount > 0) as d from pay group by 1
    )
    select p.claim_id, min(p.payer_id), min(p.service_line_code), min(p.acc),
           ((extract(year from f.d) - extract(year from min(p.acc))) * 12
            + extract(month from f.d) - extract(month from min(p.acc)))::int,
           sum(p.paid_amount)::numeric(14,2)
    from pay p join firstpay f using (claim_id)
    where f.d is not null
    group by p.claim_id, f.d
$$;

-- Materialise the official triangle and the (empty) reserve output tables.
drop table if exists mart.dev_triangle;
create table mart.dev_triangle as
select recon.param('as_of_date')::date as as_of_date, 'month'::text as grain, t.*, 'synthetic_record'::text as label
from mart.dev_triangle_at(recon.param('as_of_date')::date) t;

drop table if exists mart.reserve_factors;
create table mart.reserve_factors (
    as_of_date       date not null,
    dev_lag_from     integer not null,
    dev_lag_to       integer not null,
    n_periods        integer not null,       -- accident months contributing to the factor
    age_to_age       numeric(12,6),          -- volume-weighted chain-ladder factor f_j
    cumulative_to_ultimate numeric(12,6),    -- product of f_j from this lag onward
    mack_sigma2      numeric(18,6),          -- Mack variance parameter sigma_j^2
    count_age_to_age numeric(12,6),          -- the same factor on first-paid claim counts
    label            text not null default 'model_estimate',
    primary key (as_of_date, dev_lag_from)
);

drop table if exists mart.reserve_by_period;
create table mart.reserve_by_period (
    as_of_date        date not null,
    accident_month    date not null,
    latest_lag        integer not null,
    paid_to_date      numeric(14,2) not null,
    cdf_to_ultimate   numeric(12,6) not null,
    ultimate_cl       numeric(14,2) not null,
    unpaid_cl         numeric(14,2) not null,   -- chain-ladder IBNR / unpaid reserve
    mack_se           numeric(14,2),
    boot_mean         numeric(14,2),
    boot_p05          numeric(14,2),
    boot_p50          numeric(14,2),
    boot_p95          numeric(14,2),
    unpaid_bf         numeric(14,2),            -- Bornhuetter-Ferguson with a Cape Cod expected ratio
    unpaid_freq_sev   numeric(14,2),            -- future paid-claim count x late-claim severity
    claims_reported   integer,
    claims_paid       integer,
    claims_paid_ultimate_cl numeric(12,2),
    label             text not null default 'model_estimate',
    primary key (as_of_date, accident_month)
);

drop table if exists mart.reserve_retro;
create table mart.reserve_retro (
    retro_as_of            date primary key,
    unpaid_cl              numeric(14,2) not null,
    boot_p05               numeric(14,2),
    boot_p95               numeric(14,2),
    unpaid_bf              numeric(14,2),
    paid_later_observed    numeric(14,2) not null,   -- paid after retro_as_of, up to the official as-of
    projected_next_3m      numeric(14,2),
    paid_next_3m           numeric(14,2),
    projected_next_6m      numeric(14,2),
    paid_next_6m           numeric(14,2),
    label                  text not null default 'model_estimate'
);
