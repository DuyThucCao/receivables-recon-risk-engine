-- Test 2, Q2. Days from submission to first payment, per payer: median, 90th percentile and the
-- share of paid lines paid within 30 days, split into first-pass lines (never denied) and lines
-- paid after a denial.
with first_pay as (
    select m.claim_line_id, min(r.payment_date) as first_paid
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    where r.paid_amount > 0 and not r.is_reversal
    group by m.claim_line_id
),
ever_denied as (
    select m.claim_line_id
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    where r.denied_amount > 0 and not r.is_reversal
    group by m.claim_line_id
)
select b.payer_id,
       case when d.claim_line_id is null then 'FIRST_PASS' else 'AFTER_DENIAL' end as path,
       count(*)                                                                    as paid_lines,
       percentile_cont(0.5) within group (order by f.first_paid - b.submission_date) as median_days,
       percentile_cont(0.9) within group (order by f.first_paid - b.submission_date) as p90_days,
       round(avg(((f.first_paid - b.submission_date) <= 30)::int), 4)               as share_within_30d
from first_pay f
join recon.claim_line_balance b using (claim_line_id)
left join ever_denied d using (claim_line_id)
group by b.payer_id, path
order by path desc, median_days;
