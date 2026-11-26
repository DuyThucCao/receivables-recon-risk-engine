-- Domain 2 (remittance codes). Split adjustment dollars by group code (CO = contractual
-- obligation, the provider absorbs it; PR = patient responsibility, billed to the patient) and
-- by reason code, with each code's share of all adjustment dollars.
select a.group_code,
       a.reason_code,
       rc.bucket,
       rc.description,
       count(*)                                                    as adjustments,
       sum(a.amount)                                               as amount,
       round(sum(a.amount) / sum(sum(a.amount)) over (), 4)        as share_of_all_adjustments,
       round(sum(a.amount) / sum(sum(a.amount)) over (partition by a.group_code), 4) as share_within_group
from core.fact_remit_adjustment a
join core.fact_remit_line r using (remit_line_id)
left join core.dim_reason_code rc using (group_code, reason_code)
where not r.is_reversal
  and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = r.remit_line_id)
group by a.group_code, a.reason_code, rc.bucket, rc.description
order by a.group_code, amount desc;
