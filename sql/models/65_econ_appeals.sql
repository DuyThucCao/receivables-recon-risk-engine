-- 65_econ_appeals.sql  (Question 7c input: how often does an appeal win, by payer?)
-- From the claim life-cycle feed: every APPEALED event and the next status of the same claim.
-- Next status PAID = appeal won (overturned); DENIED or WRITTEN_OFF = lost; none yet = pending.
-- Only appeals whose outcome is dated on or before the as-of date count as decided.
-- These are DENIAL appeals. Underpayment disputes are not in the feed, so the model uses this
-- rate as a stand-in for P(win) and shows a sensitivity at a higher assumed rate.

drop table if exists mart.econ_appeal_outcome;
create table mart.econ_appeal_outcome as
with prm as (select as_of from mart.econ_param),
claim_payer as (
    select distinct on (claim_id) claim_id, payer_id from core.fact_claim_line order by claim_id, claim_line_id
), ev as (
    select e.claim_id, e.status, e.event_date,
           lead(e.status) over (partition by e.claim_id order by e.event_ts, e.claim_event_id) as next_status,
           lead(e.event_date) over (partition by e.claim_id order by e.event_ts, e.claim_event_id) as next_date
    from core.fact_claim_event e
    where e.claim_known and e.event_date <= (select as_of from prm)
)
select cp.payer_id,
       count(*) as n_appeals,
       count(*) filter (where ev.next_status = 'PAID') as n_won,
       count(*) filter (where ev.next_status in ('DENIED', 'WRITTEN_OFF')) as n_lost,
       count(*) filter (where ev.next_status is null or ev.next_status not in ('PAID', 'DENIED', 'WRITTEN_OFF')) as n_pending,
       round(count(*) filter (where ev.next_status = 'PAID')::numeric
             / nullif(count(*) filter (where ev.next_status in ('PAID', 'DENIED', 'WRITTEN_OFF')), 0), 4) as win_rate,
       percentile_cont(0.5) within group (order by ev.next_date - ev.event_date)
           filter (where ev.next_status in ('PAID', 'DENIED', 'WRITTEN_OFF')) as median_days_to_decision,
       (select as_of from prm) as as_of_date
from ev join claim_payer cp using (claim_id)
where ev.status = 'APPEALED'
group by cp.payer_id
order by cp.payer_id;
