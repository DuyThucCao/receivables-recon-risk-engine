-- Test 2, Q1. Denial mix by payer: which denial categories cost each payer's claims the most?
-- Use first-time denials only: exclude reversals (status 22, negative amounts), provider-level
-- rows and file duplicates excluded from posting. Shares use a window over the grouped result.
select r.payer_id,
       r.denial_category,
       count(*)                                                        as denied_lines,
       sum(r.denied_amount)                                            as denied_usd,
       round(sum(r.denied_amount)
             / sum(sum(r.denied_amount)) over (partition by r.payer_id), 4) as share_of_payer_denied_usd,
       rank() over (partition by r.payer_id order by sum(r.denied_amount) desc) as rank_in_payer
from core.fact_remit_line r
where r.denied_amount > 0
  and not r.is_reversal
  and not r.is_provider_level
  and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = r.remit_line_id)
group by r.payer_id, r.denial_category
order by r.payer_id, rank_in_payer;

-- Same denials, by adjustment reason code (CARC), with the reference description.
select a.group_code || '-' || a.reason_code as carc,
       rc.category,
       rc.description,
       count(*)      as adjustments,
       sum(a.amount) as amount
from core.fact_remit_adjustment a
join core.fact_remit_line r using (remit_line_id)
left join core.dim_reason_code rc using (group_code, reason_code)
where a.bucket = 'DENIAL' and not r.is_reversal
group by 1, 2, 3
order by amount desc;
