-- Test 1, Q2. The ten largest unapplied-cash credits and possible matching payments.
-- Unapplied cash = payer money in the bank with no remittance posted against it. A candidate is
-- a payment of the same payer that never found a deposit (REMIT_NO_DEPOSIT), dated within
-- 5 days of the credit, whose amount equals the credit or is larger than it (a split deposit).
with unapplied as (
    select b.bank_txn_id, b.posting_date, b.amount, b.payer_id_inferred as payer_id,
           b.description, e.reason
    from recon.exception_queue e
    join core.fact_bank_txn b using (bank_txn_id)
    where e.exception_type = 'UNAPPLIED_CASH' and e.status = 'OPEN'
    order by b.amount desc
    limit 10
),
no_deposit as (
    select t.trace_key, t.payer_id, t.payment_date, t.header_total
    from recon.exception_queue e
    join recon.remit_trace t using (trace_key)
    where e.exception_type = 'REMIT_NO_DEPOSIT' and e.status = 'OPEN'
)
select u.bank_txn_id, u.posting_date, u.payer_id, u.amount,
       left(u.reason, 60)                         as queue_reason,
       n.trace_key                                as candidate_payment,
       n.payment_date                             as candidate_date,
       n.header_total                             as candidate_amount,
       case when n.header_total = u.amount then 'EXACT AMOUNT'
            when n.header_total > u.amount then 'POSSIBLE SPLIT' end as candidate_type,
       u.posting_date - n.payment_date            as days_after_payment
from unapplied u
left join no_deposit n
       on n.payer_id = u.payer_id
      and abs(u.posting_date - n.payment_date) <= 5
      and n.header_total >= u.amount
order by u.amount desc, abs(u.posting_date - n.payment_date) nulls last;
