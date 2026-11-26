-- Domain 4 (IBNR / unpaid claims). A quarterly paid development triangle and chain-ladder
-- factors, entirely in SQL. Accident quarter = service quarter; development = quarters between
-- service and payment. Only remittance postings matched to claim lines, provider-level rows
-- excluded; reversals and recoupments enter with their negative sign.
with inc as (
    select date_trunc('quarter', c.service_date)::date as aq,
           (extract(year from r.payment_date) * 4 + extract(quarter from r.payment_date))
         - (extract(year from c.service_date) * 4 + extract(quarter from c.service_date)) as dev,
           sum(r.paid_amount) as paid
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    join core.fact_claim_line c using (claim_line_id)
    where not r.is_provider_level
    group by 1, 2
),
cum as (
    select aq, dev, sum(paid) over (partition by aq order by dev) as cum_paid
    from inc
)
select aq, dev, round(cum_paid, 2) as cum_paid
from cum
order by aq, dev;

-- Volume-weighted age-to-age factors f(d -> d+1) = sum C(d+1) / sum C(d) over accident quarters
-- observed at both ages.
with inc as (
    select date_trunc('quarter', c.service_date)::date as aq,
           (extract(year from r.payment_date) * 4 + extract(quarter from r.payment_date))
         - (extract(year from c.service_date) * 4 + extract(quarter from c.service_date)) as dev,
           sum(r.paid_amount) as paid
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    join core.fact_claim_line c using (claim_line_id)
    where not r.is_provider_level
    group by 1, 2
),
grid as (   -- fill missing (aq, dev) cells up to the latest observable age of each quarter
    select q.aq, d.dev
    from (select distinct aq from inc) q
    cross join generate_series(0, (select max(dev)::int from inc)) as d(dev)
    where (date_trunc('quarter', date '2026-03-31')::date - q.aq) / 91 >= d.dev
),
cum as (
    select g.aq, g.dev,
           sum(coalesce(i.paid, 0)) over (partition by g.aq order by g.dev) as c
    from grid g
    left join inc i on i.aq = g.aq and i.dev = g.dev
),
pairs as (
    select aq, dev, c, lead(c) over (partition by aq order by dev) as c_next from cum
)
select dev as from_dev, dev + 1 as to_dev,
       count(*) as accident_quarters,
       round(sum(c_next) / nullif(sum(c), 0), 4) as factor
from pairs
where c_next is not null
group by dev
order by dev;
