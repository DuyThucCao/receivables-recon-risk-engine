-- Drill 3 (easy, JOIN). How many remittance lines did each matching rule match, and what share
-- of all claim-to-remittance matches is that? Include rules that matched nothing.
select r.rule_id, r.priority, r.description,
       count(m.remit_line_id) as matched,
       round(count(m.remit_line_id)::numeric / sum(count(m.remit_line_id)) over (), 4) as share
from recon.match_rule r
left join recon.claim_remit_match m using (rule_id)
where r.stage = (select stage from recon.match_rule where rule_id = 'CR10_EXACT_CLAIM_ID')
group by r.rule_id, r.priority, r.description
order by r.priority;
