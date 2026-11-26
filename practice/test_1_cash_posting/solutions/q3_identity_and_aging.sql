-- Test 1, Q3a. Prove the books tie per payer:
-- billed = paid + contractual + denied + patient responsibility + open balance.
select payer_id,
       count(*)                                                     as claim_lines,
       sum(billed)                                                  as billed,
       sum(paid + contractual + denied + patient_resp + open_balance) as components,
       sum(billed) - sum(paid + contractual + denied + patient_resp + open_balance) as difference
from recon.claim_line_balance
group by rollup (payer_id)
order by payer_id nulls last;

-- Test 1, Q3b. Open receivables aging on 2025-09-30, by payer, rebuilt point in time.
-- At the official as-of date the simulated hospital has stopped billing, so almost everything
-- is old. The question is therefore asked on 2025-09-30, and balances must be rebuilt from
-- postings dated on or before that day (no look-ahead): lines submitted by then, minus every
-- matched remittance posting with payment_date <= that day. A denial settles the line (the
-- denied amount is one of the identity's components), so denied dollars are shown in their own
-- column and are not part of the open balance.
with p as (select date '2025-09-30' as d),
postings as (
    select m.claim_line_id,
           sum(r.paid_amount + r.contractual_amount + r.denied_amount + r.patient_resp_amount) as settled,
           sum(r.denied_amount) as denied
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    cross join p
    where r.payment_date <= p.d
    group by m.claim_line_id
),
pit as (
    select c.payer_id,
           c.line_charge - coalesce(x.settled, 0) as open_balance,
           coalesce(x.denied, 0)                  as denied_to_date,
           p.d - c.submission_date                as age_days
    from core.fact_claim_line c
    cross join p
    left join postings x using (claim_line_id)
    where c.submission_date <= p.d
)
select payer_id,
       count(*) filter (where open_balance > 0)                                        as open_lines,
       sum(open_balance) filter (where open_balance > 0 and age_days <= 30)             as d000_030,
       sum(open_balance) filter (where open_balance > 0 and age_days between 31 and 60) as d031_060,
       sum(open_balance) filter (where open_balance > 0 and age_days between 61 and 90) as d061_090,
       sum(open_balance) filter (where open_balance > 0 and age_days between 91 and 120) as d091_120,
       sum(open_balance) filter (where open_balance > 0 and age_days > 120)             as d120_plus,
       sum(open_balance) filter (where open_balance > 0)                                as total_open,
       round(sum(open_balance) filter (where open_balance > 0 and age_days > 90)
             / nullif(sum(open_balance) filter (where open_balance > 0), 0), 4)          as share_over_90,
       sum(denied_to_date)                                                             as denied_to_date
from pit
group by payer_id
order by share_over_90 desc nulls last;

-- Control: on the official as-of date the same rebuild must equal recon.claim_line_balance.
with postings as (
    select m.claim_line_id,
           sum(r.paid_amount + r.contractual_amount + r.denied_amount + r.patient_resp_amount) as settled
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    group by m.claim_line_id
)
select sum(c.line_charge - coalesce(x.settled, 0)) = (select sum(open_balance) from recon.claim_line_balance) as check_ok,
       sum(c.line_charge - coalesce(x.settled, 0)) as rebuilt_open,
       (select sum(open_balance) from recon.claim_line_balance) as recon_open
from core.fact_claim_line c
left join postings x using (claim_line_id);
