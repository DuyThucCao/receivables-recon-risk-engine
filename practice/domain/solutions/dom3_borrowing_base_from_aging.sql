-- Domain 3 (borrowing base). Start from the CFO's aging mart (open expected value by payer and
-- age bucket at the as-of date) and apply textbook rules: buckets over 120 days ineligible,
-- 85% advance rate, credit balances deducted as a reserve. At the as-of date the simulated
-- hospital has stopped billing, so expect almost nothing eligible: the point is the mechanics.
with a as (
    select payer_id, aging_bucket, open_expected, credit_balance
    from mart.ar_aging
),
per_payer as (
    select payer_id,
           sum(open_expected)                                                          as gross,
           sum(open_expected) filter (where aging_bucket in ('000-030','031-060','061-090','091-120')) as eligible,
           sum(credit_balance)                                                         as credit_balance
    from a
    group by payer_id
)
select payer_id, gross, coalesce(eligible, 0) as eligible, credit_balance,
       round(0.85 * coalesce(eligible, 0), 2) as advance_at_85pct
from per_payer
order by payer_id;

select distinct aging_bucket from mart.ar_aging order by 1;
