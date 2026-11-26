-- Drill 8 (hard, gaps and islands). (a) Islands: runs of consecutive calendar weeks in which a
-- payer sent at least one payment; the longest run per payer and how many runs there are
-- (more than one run means the payer skipped at least one week). (b) Gaps: the longest silence
-- between two payment dates per payer.
-- Trick: within one unbroken run, week_start - 7 * row_number is constant.
with w as (
    select distinct payer_id, date_trunc('week', payment_date)::date as wk from recon.remit_trace
),
g as (
    select payer_id, wk,
           wk - 7 * (row_number() over (partition by payer_id order by wk))::int as grp
    from w
),
islands as (
    select payer_id, min(wk) as first_week, max(wk) as last_week, count(*) as weeks
    from g group by payer_id, grp
),
ranked as (
    select *, row_number() over (partition by payer_id order by weeks desc, first_week) as rn,
              count(*) over (partition by payer_id) as n_islands
    from islands
)
select payer_id, first_week, last_week, weeks as longest_run_weeks, n_islands
from ranked where rn = 1
order by payer_id;

with d as (
    select distinct payer_id, payment_date from recon.remit_trace
),
gaps as (
    select payer_id, payment_date,
           payment_date - lag(payment_date) over (partition by payer_id order by payment_date) as gap_days
    from d
)
select payer_id,
       max(gap_days)                                           as longest_gap_days,
       percentile_cont(0.5) within group (order by gap_days)   as median_gap_days
from gaps
where gap_days is not null
group by payer_id
order by payer_id;
