-- Domain 5 (prompt pay). For clean claims (lines never denied), how often does each payer pay
-- later than 30 days after submission, and what would 12% simple annual interest on the late
-- days cost the payer? 30 days and 12% are illustrative assumptions, not a statute.
with first_pay as (
    select m.claim_line_id, min(r.payment_date) as first_paid, sum(r.paid_amount) as paid
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    where not r.is_reversal and r.paid_amount > 0
    group by m.claim_line_id
),
denied as (
    select distinct m.claim_line_id
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    where r.denied_amount > 0
),
clean as (
    select b.payer_id, f.paid, f.first_paid - b.submission_date as days
    from first_pay f
    join recon.claim_line_balance b using (claim_line_id)
    where not exists (select 1 from denied d where d.claim_line_id = f.claim_line_id)
)
select payer_id,
       count(*)                                                      as clean_paid_lines,
       round(avg((days > 30)::int), 4)                               as share_late,
       round(sum(paid) filter (where days > 30), 2)                  as paid_late_usd,
       round(sum(paid * 0.12 * greatest(days - 30, 0) / 365.0), 2)   as interest_at_12pct
from clean
group by payer_id
order by share_late desc;
