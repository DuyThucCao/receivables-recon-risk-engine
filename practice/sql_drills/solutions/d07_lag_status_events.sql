-- Drill 7 (medium, LAG/LEAD). Average days from a DENIED event to the claim's next event, and
-- what that next event is, by denial reason category.
with e as (
    select claim_id, status, reason_category, event_ts,
           lead(status)   over (partition by claim_id order by event_ts, claim_event_id) as next_status,
           lead(event_ts) over (partition by claim_id order by event_ts, claim_event_id) as next_ts
    from core.fact_claim_event
)
select reason_category,
       next_status,
       count(*)                                                       as n,
       round(avg(extract(epoch from next_ts - event_ts) / 86400)::numeric, 1) as avg_days_to_next
from e
where status = 'DENIED' and next_status is not null
group by 1, 2
order by 1, 3 desc;
