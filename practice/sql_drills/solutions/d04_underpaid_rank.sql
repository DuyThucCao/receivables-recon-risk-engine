-- Drill 4 (medium, RANK window). For each service month, rank payers by underpaid dollars
-- (payer allowed below contract) and keep the top 2 per month.
with m as (
    select date_trunc('month', service_date)::date as service_month,
           payer_id,
           sum(underpaid_amount) as underpaid
    from recon.claim_line_balance
    group by 1, 2
),
r as (
    select *, rank() over (partition by service_month order by underpaid desc) as rnk
    from m
)
select service_month, payer_id, underpaid, rnk
from r
where rnk <= 2
order by service_month, rnk;
