-- Drill 5 (medium, running SUM). Recompute the bank running balance from the opening balance and
-- the amounts, in statement order, and list rows where it differs from the printed balance.
-- Expect zero rows: the check_ok statement asserts it.
with s as (
    select bank_txn_id, posting_date, amount, running_balance,
           first_value(running_balance - amount) over (order by bank_txn_id) as opening,
           sum(amount) over (order by bank_txn_id rows between unbounded preceding and current row) as cum
    from core.fact_bank_txn
)
select count(*) filter (where opening + cum <> running_balance) = 0 as check_ok,
       count(*)                                                    as rows_checked,
       count(*) filter (where opening + cum <> running_balance)    as breaks,
       max(running_balance) filter (where bank_txn_id = (select max(bank_txn_id) from core.fact_bank_txn)) as closing
from s;
