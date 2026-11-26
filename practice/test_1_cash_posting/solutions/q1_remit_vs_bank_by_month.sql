-- Test 1, Q1. Remitted vs deposited, by payer and payment month.
-- A payment is one remittance trace (recon.remit_trace). Its header_total is what the payer
-- says it paid. recon.remit_bank_match has one row per (payment, deposit), so a payment split
-- across two deposits has two rows: always SUM allocated_amount per trace_key first.
with alloc as (
    select trace_key, sum(allocated_amount) as matched
    from recon.remit_bank_match
    group by trace_key
),
pay as (
    select t.payer_id,
           date_trunc('month', t.payment_date)::date as payment_month,
           t.header_total,
           coalesce(a.matched, 0) as matched
    from recon.remit_trace t
    left join alloc a using (trace_key)
)
select payer_id,
       payment_month,
       count(*)                                   as payments,
       sum(header_total)                          as remitted,
       sum(matched)                               as matched_to_bank,
       sum(header_total) - sum(matched)           as not_matched,
       round(sum(matched) / nullif(sum(header_total), 0), 4) as match_ratio,
       case when sum(matched) < 0.98 * sum(header_total) then 'REVIEW' else 'OK' end as flag
from pay
group by payer_id, payment_month
order by payer_id, payment_month;

-- Control: the not-matched total must equal the REMIT_NO_DEPOSIT bucket plus timing differences.
with alloc as (
    select trace_key, sum(allocated_amount) as matched from recon.remit_bank_match group by trace_key
)
select sum(t.header_total - coalesce(a.matched, 0))
         = (select coalesce(sum(amount), 0) from recon.exception_queue
             where exception_type in ('REMIT_NO_DEPOSIT', 'IN_TRANSIT') and status = 'OPEN') as check_ok,
       sum(t.header_total - coalesce(a.matched, 0)) as not_matched_total,
       (select coalesce(sum(amount), 0) from recon.exception_queue
         where exception_type = 'REMIT_NO_DEPOSIT' and status = 'OPEN')    as remit_no_deposit_queue,
       (select coalesce(sum(amount), 0) from recon.exception_queue
         where exception_type = 'IN_TRANSIT' and status = 'OPEN')          as in_transit_queue
from recon.remit_trace t
left join alloc a using (trace_key);
