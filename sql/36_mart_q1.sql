-- 36_mart_q1.sql
-- Question 1 (hospital CFO): how much did we really collect, and where is cash stuck?
-- mart.cash_position  : monthly billed, remitted, deposited, matched, unapplied, bank balance
-- mart.cash_stuck     : as-of snapshot of every place money is waiting, with amounts
-- mart.ar_aging       : open receivables by payer and age since submission
-- mart.pnl_by_payer_service : revenue waterfall by payer x service line x service month
-- All are sums over simulated records: label synthetic_record. Rebuilt by mart.refresh_q1().

create or replace function mart.refresh_q1() returns jsonb language plpgsql as $$
declare v_asof date := recon.param('as_of_date')::date;
begin
    drop table if exists mart.cash_position;
    create table mart.cash_position as
    with months as (
        select g::date as month from generate_series(
            date_trunc('month', least((select min(submission_date) from core.fact_claim_line), (select min(posting_date) from core.fact_bank_txn))),
            date_trunc('month', v_asof), interval '1 month') g
    ), billed as (
        select date_trunc('month', submission_date)::date m, sum(line_charge) amt from core.fact_claim_line group by 1
    ), remitted as (
        select date_trunc('month', r.payment_date)::date m, sum(r.paid_amount) amt
        from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id) group by 1
    ), bank as (
        select date_trunc('month', b.posting_date)::date m,
               sum(b.amount) filter (where b.txn_category in ('PAYER_EFT', 'LOCKBOX')) payer_cash,
               sum(a.alloc) filter (where b.txn_category in ('PAYER_EFT', 'LOCKBOX')) matched,
               sum(b.amount) filter (where b.txn_category in ('PAYER_EFT', 'LOCKBOX') and a.alloc is null) unapplied,
               sum(b.amount) filter (where b.amount > 0 and b.txn_category not in ('PAYER_EFT', 'LOCKBOX')) other_credits,
               sum(b.amount) filter (where b.amount < 0) debits
        from core.fact_bank_txn b
        left join (select bank_txn_id, sum(allocated_amount) alloc from recon.remit_bank_match group by 1) a using (bank_txn_id)
        group by 1
    )
    select mo.month,
           coalesce(bi.amt, 0)::numeric(14,2) as billed_charges,
           coalesce(re.amt, 0)::numeric(14,2) as payer_paid_per_remittance,
           coalesce(bk.payer_cash, 0)::numeric(14,2) as payer_cash_deposited,
           coalesce(bk.matched, 0)::numeric(14,2) as payer_cash_matched,
           coalesce(bk.unapplied, 0)::numeric(14,2) as unapplied_cash,
           coalesce(bk.other_credits, 0)::numeric(14,2) as non_payer_credits,
           coalesce(bk.debits, 0)::numeric(14,2) as debits,
           (select running_balance from core.fact_bank_txn b where b.posting_date < mo.month + interval '1 month'
             order by posting_date desc, bank_txn_id desc limit 1)::numeric(14,2) as closing_bank_balance,
           (select coalesce(sum(t.header_total), 0) from recon.remit_trace t
             where t.header_total > 0 and t.payment_method in ('ACH', 'CHK') and t.payment_date < mo.month + interval '1 month'
               and not exists (select 1 from recon.remit_bank_match m join core.fact_bank_txn b using (bank_txn_id)
                               where m.trace_key = t.trace_key and b.posting_date < mo.month + interval '1 month'))::numeric(14,2)
             as remitted_not_deposited_eom,
           'synthetic_record'::text as label
    from months mo
    left join billed bi on bi.m = mo.month left join remitted re on re.m = mo.month left join bank bk on bk.m = mo.month
    order by mo.month;

    drop table if exists mart.cash_stuck;
    create table mart.cash_stuck as
    with b as (select b.*, s.status, s.is_written_off from recon.claim_line_balance b join recon.claim_line_status s using (claim_line_id)),
    q as (select exception_type, count(*) n, coalesce(sum(amount), 0) amt from recon.exception_queue where status = 'OPEN' group by 1)
    select * from (
        select 1 as sort, 'NOT_YET_ADJUDICATED' as bucket, 'claim lines with no remittance yet (not written off); amount = contract allowed' as description,
               count(*) as n_items, coalesce(sum(expected_allowed), 0)::numeric(14,2) as amount, coalesce(sum(billed), 0)::numeric(14,2) as billed_basis
        from b where status in ('SUBMITTED', 'REVERSED') and not is_written_off
        union all
        select 2, 'PARTIAL_PAYMENT_BALANCE', 'payer balance still open after a partial payment', count(*), coalesce(sum(open_balance), 0), coalesce(sum(billed), 0)
        from b where status = 'PARTIAL'
        union all
        select 3, 'DENIED_IN_PLAY', 'denied lines whose claim is not written off (appeal or correction possible); amount = contract allowed', count(*),
               coalesce(sum(expected_allowed), 0), coalesce(sum(denied), 0)
        from b where status = 'DENIED' and not is_written_off
        union all
        select 4, 'DENIED_WRITTEN_OFF', 'denied lines on written-off claims (lost); amount = contract allowed', count(*), coalesce(sum(expected_allowed), 0), coalesce(sum(denied), 0)
        from b where status = 'DENIED' and is_written_off
        union all
        select 5, 'UNDERPAID_VS_CONTRACT', 'payer allowed below contract (recoverable by dispute)', count(*), coalesce(sum(underpaid_amount), 0), null
        from b where underpaid_amount > 0
        union all
        select 6, 'CREDIT_BALANCE', 'overpaid lines (owed back to payers; negative = liability)', count(*), coalesce(sum(open_balance), 0), null
        from b where open_balance < 0
        union all
        select 7, 'UNAPPLIED_CASH', 'payer money in the bank with no remittance to post it against', coalesce((select n from q where exception_type = 'UNAPPLIED_CASH'), 0),
               coalesce((select amt from q where exception_type = 'UNAPPLIED_CASH'), 0), null
        union all
        select 8, 'REMIT_NO_DEPOSIT', 'payer says it paid, money never arrived (past the in-transit window)', coalesce((select n from q where exception_type = 'REMIT_NO_DEPOSIT'), 0),
               coalesce((select amt from q where exception_type = 'REMIT_NO_DEPOSIT'), 0), null
        union all
        select 9, 'IN_TRANSIT', 'payments dated within the in-transit window, not yet deposited', coalesce((select n from q where exception_type = 'IN_TRANSIT'), 0),
               coalesce((select amt from q where exception_type = 'IN_TRANSIT'), 0), null
        union all
        select 12, 'ORPHAN_REVERSAL_LINES', 'lines carrying a reversal of a posting never received; balance needs the missing remittance (amount = billed)',
               count(*), coalesce(sum(billed), 0), coalesce(sum(billed), 0)
        from b where status = 'ORPHAN_REVERSAL'
        union all
        select 11, 'PROVIDER_LEVEL_SUSPENSE', 'provider-level payments (not tied to claims) awaiting payer reconciliation', coalesce((select n from q where exception_type = 'PROVIDER_LEVEL_PAYMENT'), 0),
               coalesce((select amt from q where exception_type = 'PROVIDER_LEVEL_PAYMENT'), 0), null
        union all
        select 10, 'UNMATCHED_REMITTANCE', 'remittance lines that could not be tied to a claim line', coalesce((select n from q where exception_type = 'UNMATCHED_REMIT'), 0),
               coalesce((select amt from q where exception_type = 'UNMATCHED_REMIT'), 0), null
    ) x order by sort;
    alter table mart.cash_stuck add column as_of_date date, add column label text;
    update mart.cash_stuck set as_of_date = v_asof, label = 'synthetic_record';

    drop table if exists mart.ar_aging;
    create table mart.ar_aging as
    with b as (
        select b.*, s.status, s.is_written_off, v_asof - b.submission_date as age_days
        from recon.claim_line_balance b join recon.claim_line_status s using (claim_line_id)
    ), aged as (
        select *, case when age_days <= 30 then '000-030' when age_days <= 60 then '031-060' when age_days <= 90 then '061-090'
                       when age_days <= 120 then '091-120' when age_days <= 180 then '121-180' when age_days <= 365 then '181-365'
                       else '366+' end as aging_bucket
        from b
    )
    select payer_id, aging_bucket,
           count(*) filter (where open_balance > 0 and not is_written_off and status <> 'ORPHAN_REVERSAL') as n_open_lines,
           coalesce(sum(open_balance) filter (where open_balance > 0 and not is_written_off and status <> 'ORPHAN_REVERSAL'), 0)::numeric(14,2) as open_billed,
           coalesce(sum(case when status in ('SUBMITTED', 'REVERSED') then expected_allowed else open_balance end)
                    filter (where open_balance > 0 and not is_written_off and status <> 'ORPHAN_REVERSAL'), 0)::numeric(14,2) as open_expected,
           count(*) filter (where status = 'ORPHAN_REVERSAL') as n_orphan_reversal_lines,
           count(*) filter (where status = 'DENIED' and not is_written_off) as n_denied_in_play,
           coalesce(sum(expected_allowed) filter (where status = 'DENIED' and not is_written_off), 0)::numeric(14,2) as denied_in_play_expected,
           coalesce(sum(open_balance) filter (where open_balance < 0), 0)::numeric(14,2) as credit_balance,
           coalesce(sum(open_balance) filter (where open_balance > 0 and is_written_off), 0)::numeric(14,2) as written_off_open_billed,
           v_asof as as_of_date, 'synthetic_record'::text as label
    from aged group by payer_id, aging_bucket order by payer_id, aging_bucket;

    drop table if exists mart.pnl_by_payer_service;
    create table mart.pnl_by_payer_service as
    with avg_cost as (
        select hcpcs_code, sum(extended_amount) / nullif(sum(billing_units), 0) as unit_cost
        from core.fact_drug_purchase group by 1
    )
    select b.payer_id, b.service_line_code, date_trunc('month', b.service_date)::date as service_month,
           count(distinct b.claim_id) as n_claims, count(*) as n_lines,
           sum(b.billed)::numeric(14,2) as billed, sum(b.contractual)::numeric(14,2) as contractual_adjustment,
           sum(b.denied)::numeric(14,2) as denied, sum(b.patient_resp)::numeric(14,2) as patient_responsibility,
           sum(b.paid)::numeric(14,2) as payer_paid, sum(b.open_balance)::numeric(14,2) as open_balance,
           coalesce(sum(b.expected_allowed), 0)::numeric(14,2) as contract_expected,
           sum(b.underpaid_amount)::numeric(14,2) as underpaid_vs_contract,
           (sum(b.paid) + sum(b.patient_resp))::numeric(14,2) as net_revenue_recognised,
           coalesce(sum(c.units * ac.unit_cost) filter (where p.is_drug), 0)::numeric(14,2) as drug_cost_avg_basis,
           (sum(b.paid) + sum(b.patient_resp) - coalesce(sum(c.units * ac.unit_cost) filter (where p.is_drug), 0))::numeric(14,2)
               as margin_after_drug_cost,
           'synthetic_record'::text as label
    from recon.claim_line_balance b
    join core.fact_claim_line c using (claim_line_id)
    join core.dim_procedure p on p.procedure_code = c.procedure_code
    left join avg_cost ac on ac.hcpcs_code = c.procedure_code
    group by 1, 2, 3 order by 1, 2, 3;

    insert into mart.table_label (table_name, label, note) values
        ('mart.cash_position', 'synthetic_record', 'Monthly sums over simulated claims, remittances and bank rows.'),
        ('mart.cash_stuck', 'synthetic_record', 'As-of snapshot of balances in simulated data.'),
        ('mart.ar_aging', 'synthetic_record', 'Open simulated receivables by payer and days since submission.'),
        ('mart.pnl_by_payer_service', 'synthetic_record', 'Revenue waterfall over simulated claims; drug cost at average invoice cost, not FIFO.')
    on conflict (table_name) do update set label = excluded.label, note = excluded.note;
    comment on table mart.cash_position is 'label=synthetic_record. Monthly collections and bank position.';
    comment on table mart.cash_stuck is 'label=synthetic_record. Where cash is stuck as of the snapshot date.';
    comment on table mart.ar_aging is 'label=synthetic_record. A/R aging by payer.';
    comment on table mart.pnl_by_payer_service is 'label=synthetic_record. P&L by payer and service line; drug cost at average cost.';
    return jsonb_build_object('cash_position', (select count(*) from mart.cash_position),
                              'ar_aging', (select count(*) from mart.ar_aging),
                              'pnl_by_payer_service', (select count(*) from mart.pnl_by_payer_service));
end $$;

select mart.refresh_q1();
