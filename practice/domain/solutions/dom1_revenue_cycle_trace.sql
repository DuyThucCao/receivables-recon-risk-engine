-- Domain 1 (revenue cycle). Trace one claim that was denied, appealed and finally paid: its
-- status events, then its remittance postings with the rule that matched each one.
with target as (
    select claim_id
    from core.fact_claim_event
    group by claim_id
    having bool_or(status = 'APPEALED') and bool_or(status = 'PAID') and bool_or(status = 'DENIED')
    order by claim_id
    limit 1
)
select e.claim_id, e.event_ts, e.status, e.reason_code, e.reason_category, e.appeal_level, e.source_system
from core.fact_claim_event e
join target using (claim_id)
order by e.event_ts, e.claim_event_id;

with target as (
    select claim_id
    from core.fact_claim_event
    group by claim_id
    having bool_or(status = 'APPEALED') and bool_or(status = 'PAID') and bool_or(status = 'DENIED')
    order by claim_id
    limit 1
)
select c.claim_id, c.line_no, c.procedure_code, c.line_charge,
       r.payment_date, r.claim_status_code, r.trace_key,
       r.allowed_amount, r.paid_amount, r.contractual_amount, r.denied_amount, r.patient_resp_amount,
       r.denial_group_code || '-' || r.denial_reason_code as denial_code,
       m.rule_id
from core.fact_claim_line c
join target using (claim_id)
join recon.claim_remit_match m using (claim_line_id)
join core.fact_remit_line r using (remit_line_id)
order by c.line_no, r.payment_date, r.remit_line_id;
