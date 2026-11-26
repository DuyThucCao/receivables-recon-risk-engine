-- Drill 1 (easy, GROUP BY). Claim lines, claims and billed charges per payer, largest first.
select payer_id,
       count(*)                  as claim_lines,
       count(distinct claim_id)  as claims,
       sum(line_charge)          as billed,
       round(avg(line_charge), 2) as avg_line_charge
from core.fact_claim_line
group by payer_id
order by billed desc;
