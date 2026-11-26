-- 55_work_queue.sql
-- Question 2 (revenue-cycle lead): which denied claims should staff work first today?
-- Executed by recon_engine.models.prioritize.run(), not by pipeline.py.
--
-- mart.denial_episodes(p_asof) returns one row per DENIED event in the claim status feed, as it
-- could have been seen on p_asof: events dated after p_asof are invisible, remittance postings
-- dated after p_asof are ignored. For each denial it gives what happened next (as known on
-- p_asof): resubmitted, appealed, written off, or nothing yet; and for an appeal, its outcome:
-- OVERTURNED (the next event after APPEALED is PAID) or UPHELD (the next event is DENIED again).
--
-- Claim-level money comes from the reconciliation core:
--   billed           = sum of line charges
--   contract_allowed = sum of recon.claim_line_balance.expected_allowed (contract rate, lesser of charge)
--   paid_to_date     = net remittance paid on the claim's matched lines, dated <= p_asof

drop function if exists mart.denial_episodes(date);
create function mart.denial_episodes(p_asof date)
returns table (
    claim_id            text,
    denial_seq          integer,       -- 1 = first denial of the claim, 2 = denial after an upheld appeal or resubmission, ...
    payer_id            text,
    service_line_code   text,
    submission_date     date,
    denial_date         date,
    reason_code         text,
    reason_category     text,
    n_prior_appeals     integer,
    next_status         text,          -- IN_REVIEW (resubmitted), APPEALED, WRITTEN_OFF, PAID, or NULL (no action yet)
    next_date           date,
    appealed            boolean,
    appeal_date         date,
    appeal_outcome      text,          -- OVERTURNED, UPHELD, or NULL (pending / not appealed)
    outcome_date        date,
    claim_total_charge  numeric(14,2),
    billed              numeric(14,2),
    contract_allowed    numeric(14,2),
    paid_to_date        numeric(14,2)
) language sql stable as $$
    with ev as (
        select e.claim_id, e.status, e.event_ts, e.event_date, e.reason_code, e.reason_category, e.claim_event_id,
               lead(e.status, 1)     over w as s1, lead(e.event_date, 1) over w as d1,
               lead(e.status, 2)     over w as s2, lead(e.event_date, 2) over w as d2,
               count(*) filter (where e.status = 'APPEALED') over (partition by e.claim_id order by e.event_ts, e.claim_event_id
                                                                  rows between unbounded preceding and current row) as n_app,
               count(*) filter (where e.status = 'DENIED') over (partition by e.claim_id order by e.event_ts, e.claim_event_id
                                                                rows between unbounded preceding and current row) as n_den
        from core.fact_claim_event e
        where e.event_date <= p_asof and e.claim_known
        window w as (partition by e.claim_id order by e.event_ts, e.claim_event_id)
    ), claim as (
        select c.claim_id, min(c.payer_id) as payer_id, min(c.service_line_code) as service_line_code,
               min(c.submission_date) as submission_date, max(c.claim_total_charge) as claim_total_charge,
               sum(c.line_charge) as billed, sum(b.expected_allowed) as contract_allowed
        from core.fact_claim_line c join recon.claim_line_balance b using (claim_line_id)
        where c.submission_date <= p_asof
        group by c.claim_id
    ), paid as (
        select c.claim_id, sum(r.paid_amount) as paid
        from core.fact_claim_line c
        join recon.claim_remit_match m on m.claim_line_id = c.claim_line_id
        join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
        where r.payment_date <= p_asof and not r.is_provider_level
        group by c.claim_id
    )
    select ev.claim_id, ev.n_den::int, cl.payer_id, cl.service_line_code, cl.submission_date,
           ev.event_date, ev.reason_code, ev.reason_category, ev.n_app::int,
           ev.s1, ev.d1,
           coalesce(ev.s1 = 'APPEALED', false),
           case when ev.s1 = 'APPEALED' then ev.d1 end,
           case when ev.s1 = 'APPEALED' and ev.s2 = 'PAID' then 'OVERTURNED'
                when ev.s1 = 'APPEALED' and ev.s2 = 'DENIED' then 'UPHELD' end,
           case when ev.s1 = 'APPEALED' and ev.s2 in ('PAID', 'DENIED') then ev.d2 end,
           cl.claim_total_charge, cl.billed, cl.contract_allowed, coalesce(pd.paid, 0)
    from ev
    join claim cl on cl.claim_id = ev.claim_id
    left join paid pd on pd.claim_id = ev.claim_id
    where ev.status = 'DENIED'
    order by ev.event_date, ev.claim_id, ev.n_den
$$;

-- Payer share of the contract allowance that is actually paid on clean (undenied, positive)
-- remittance lines dated <= p_asof. Recoverable amount of an overturned denial is estimated as
-- contract_allowed x this ratio (it nets out patient responsibility and typical underpayment).
drop function if exists mart.payer_paid_ratio_at(date);
create function mart.payer_paid_ratio_at(p_asof date)
returns table (payer_id text, n_lines bigint, paid numeric(14,2), contract_allowed numeric(14,2), ratio numeric(10,6))
language sql stable as $$
    select b.payer_id, count(*), sum(r.paid_amount)::numeric(14,2), sum(b.expected_allowed)::numeric(14,2),
           (sum(r.paid_amount) / nullif(sum(b.expected_allowed), 0))::numeric(10,6)
    from recon.claim_remit_match m
    join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
    join recon.claim_line_balance b on b.claim_line_id = m.claim_line_id
    where r.payment_date <= p_asof and r.paid_amount > 0 and r.denied_amount = 0 and not r.is_reversal
      and not r.is_provider_level
      and b.expected_allowed > 0
    group by b.payer_id
    order by b.payer_id
$$;

-- Output tables, filled by prioritize.run().
drop table if exists mart.work_queue;
create table mart.work_queue (
    as_of_date           date not null,
    queue_rank           integer not null,        -- order to work; selected claims first
    selected             boolean not null,        -- inside today's staff-hours budget (exact 0/1 knapsack)
    claim_id             text not null,
    payer_id             text not null,
    service_line_code    text,
    reason_category      text,
    reason_code          text,
    denial_date          date not null,
    days_since_denial    integer not null,
    days_left_to_appeal  integer not null,        -- appeal window minus days since denial
    billed               numeric(14,2),
    contract_allowed     numeric(14,2),
    expected_recoverable numeric(14,2),           -- contract_allowed x payer paid ratio
    p_overturn           numeric(8,6),
    handling_minutes     integer not null,
    handling_cost        numeric(10,2) not null,
    expected_value       numeric(14,2) not null,  -- p x recoverable - cost; 0 when past the appeal window
    cumulative_minutes   integer,
    reason               text not null,
    label                text not null default 'model_estimate',
    primary key (as_of_date, claim_id)
);

drop table if exists mart.work_queue_coef;
create table mart.work_queue_coef (
    fit_cutoff   date not null,
    term         text not null,
    estimate     numeric(12,6),
    std_error    numeric(12,6),
    ci_low       numeric(12,6),
    ci_high      numeric(12,6),
    p_value      numeric(12,8),
    odds_ratio   numeric(12,6),
    label        text not null default 'model_estimate',
    primary key (fit_cutoff, term)
);

drop table if exists mart.work_queue_backtest;
create table mart.work_queue_backtest (
    pool              text not null,     -- ALL_DENIALS (expected value) or APPEALED_WITH_OUTCOME (realised)
    policy            text not null,     -- MODEL_KNAPSACK, OLDEST_FIRST, LARGEST_FIRST
    hours_per_day     numeric(6,2) not null,
    n_worked          integer not null,
    minutes_used      integer not null,
    expected_net      numeric(14,2),     -- sum of p x recoverable - cost, model p
    realised_net      numeric(14,2),     -- sum of overturned x paid - cost (APPEALED_WITH_OUTCOME only)
    n_expired_unworked integer,          -- claims that passed the appeal window without being worked
    label             text not null default 'model_estimate',
    primary key (pool, policy, hours_per_day)
);

-- Which claims each policy worked in the backtest, so evaluation scripts can score the choices
-- with other information (for example the generator's planted probabilities) without rerunning.
drop table if exists mart.work_queue_backtest_selection;
create table mart.work_queue_backtest_selection (
    pool          text not null,
    policy        text not null,
    hours_per_day numeric(6,2) not null,
    claim_id      text not null,
    denial_seq    integer not null,
    work_date     date not null,
    label         text not null default 'model_estimate',
    primary key (pool, policy, hours_per_day, claim_id, denial_seq)
);
