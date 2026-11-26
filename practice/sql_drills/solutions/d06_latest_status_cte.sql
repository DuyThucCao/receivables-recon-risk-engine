-- Drill 6 (medium, CTE + ROW_NUMBER). The latest status of each claim line from its posting
-- history, compared with the stored current status. Lines never posted stay SUBMITTED.
with h as (
    select claim_line_id, status_after,
           row_number() over (partition by claim_line_id order by seq desc) as rn
    from recon.claim_line_status_history
),
latest as (select claim_line_id, status_after from h where rn = 1)
select s.status                                   as stored_status,
       coalesce(l.status_after, 'SUBMITTED')      as rebuilt_status,
       count(*)                                   as lines
from recon.claim_line_status s
left join latest l using (claim_line_id)
group by 1, 2
order by 3 desc;
