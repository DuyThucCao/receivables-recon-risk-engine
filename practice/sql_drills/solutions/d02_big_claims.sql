-- Drill 2 (easy, HAVING). Claims with more than 5 lines; the 10 largest by total charge.
select claim_id, payer_id, service_line_code, n_lines, total_charge
from core.v_claim
where n_lines > 5
order by total_charge desc
limit 10;

-- Same result without the view, to show GROUP BY ... HAVING.
select claim_id, min(payer_id) as payer_id, count(*) as n_lines, sum(line_charge) as total_charge
from core.fact_claim_line
group by claim_id
having count(*) > 5
order by total_charge desc
limit 10;
