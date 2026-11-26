-- Drill 10 (hard, daily close). Rebuild the bank side of the daily reconciliation from the raw
-- transactions and compare with recon.daily_recon for every day: opening balance, credits,
-- debits, computed closing and the statement's closing balance. Expect no differences.
with day as (
    select posting_date as d,
           coalesce(sum(amount) filter (where amount > 0), 0)  as credits,
           coalesce(sum(amount) filter (where amount < 0), 0)  as debits,
           (array_agg(running_balance order by bank_txn_id desc))[1] as closing_statement,
           (array_agg(running_balance - amount order by bank_txn_id))[1] as opening
    from core.fact_bank_txn
    group by posting_date
),
cmp as (
    select r.recon_date,
           coalesce(day.credits, 0) - r.total_credits                as credit_diff,
           coalesce(day.debits, 0)  - r.total_debits                 as debit_diff,
           coalesce(day.closing_statement, r.closing_balance_statement) - r.closing_balance_statement as closing_diff,
           r.opening_balance + r.total_credits + r.total_debits - r.closing_balance_computed as identity_diff
    from recon.daily_recon r
    left join day on day.d = r.recon_date
)
select count(*) filter (where credit_diff <> 0 or debit_diff <> 0 or closing_diff <> 0 or identity_diff <> 0) = 0 as check_ok,
       count(*) as days_compared,
       count(*) filter (where credit_diff <> 0 or debit_diff <> 0 or closing_diff <> 0 or identity_diff <> 0) as days_with_difference
from cmp;

-- The five days with the largest payer credits, with matched and unapplied parts.
select recon_date, payer_credits, payer_credits_matched, payer_credits_unapplied, status
from recon.daily_recon
order by payer_credits desc
limit 5;
