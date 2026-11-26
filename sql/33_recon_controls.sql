-- 33_recon_controls.sql
-- Control totals (layer-to-layer and identity checks, to the cent) and the daily
-- reconciliation table that recon.close_day verifies and closes.

create or replace function recon.build_control_totals(p_run bigint) returns jsonb language plpgsql as $$
declare v_asof date := recon.param('as_of_date')::date; out jsonb;
begin
    truncate recon.control_totals;
    -- 1. every raw row is staged or rejected, per document
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'RAW_ROWS_ACCOUNTED', x.tbl, x.raw_n, x.stg_n + x.rej_n, x.raw_n = x.stg_n + x.rej_n,
           format('raw %s = staged %s + rejected %s', x.raw_n, x.stg_n, x.rej_n)
    from (
        select 'raw.claims_export' as tbl, (select count(*) from raw.claims_export) as raw_n, (select count(*) from stg.claim_line) as stg_n,
               (select count(*) from stg.reject where source_table = 'raw.claims_export') as rej_n
        union all select 'raw.remittance_advice', (select count(*) from raw.remittance_advice), (select count(*) from stg.remit_line),
               (select count(*) from stg.reject where source_table = 'raw.remittance_advice')
        union all select 'raw.bank_statement', (select count(*) from raw.bank_statement), (select count(*) from stg.bank_txn),
               (select count(*) from stg.reject where source_table = 'raw.bank_statement')
        union all select 'raw.payer_contracts', (select count(*) from raw.payer_contracts), (select count(*) from stg.contract_rate),
               (select count(*) from stg.reject where source_table = 'raw.payer_contracts')
        union all select 'raw.drug_purchase_invoices', (select count(*) from raw.drug_purchase_invoices), (select count(*) from stg.drug_purchase),
               (select count(*) from stg.reject where source_table = 'raw.drug_purchase_invoices')
        union all select 'raw.drug_dispense_log', (select count(*) from raw.drug_dispense_log), (select count(*) from stg.drug_dispense),
               (select count(*) from stg.reject where source_table = 'raw.drug_dispense_log')
        union all select 'raw.claim_status_history', (select count(*) from raw.claim_status_history), (select count(*) from stg.claim_event),
               (select count(*) from stg.reject where source_table = 'raw.claim_status_history')
    ) x;
    -- 1b. staged rows all reach core
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    values
    (p_run, 'STG_TO_CORE_ROWS', 'claim lines', (select count(*) from stg.claim_line), (select count(*) from core.fact_claim_line),
        (select count(*) from stg.claim_line) = (select count(*) from core.fact_claim_line), null),
    (p_run, 'STG_TO_CORE_ROWS', 'remittance lines', (select count(*) from stg.remit_line), (select count(*) from core.fact_remit_line),
        (select count(*) from stg.remit_line) = (select count(*) from core.fact_remit_line), null),
    (p_run, 'STG_TO_CORE_ROWS', 'bank transactions', (select count(*) from stg.bank_txn), (select count(*) from core.fact_bank_txn),
        (select count(*) from stg.bank_txn) = (select count(*) from core.fact_bank_txn), null);

    -- 2. money: parseable raw amounts = core + rejected amounts
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'BILLED_ACCOUNTED', 'claim line charges', e, a, e = a, 'sum of parseable raw line_charge = core + rejected rows'
    from (select (select coalesce(sum(stg.parse_money(line_charge)), 0) from raw.claims_export) e,
                 (select coalesce(sum(line_charge), 0) from core.fact_claim_line)
                 + (select coalesce(sum(amount), 0) from stg.reject where source_table = 'raw.claims_export') a) x;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'REMIT_PAID_ACCOUNTED', 'remittance paid amounts', e, a, e = a, 'sum of parseable raw line_paid_amount = core + rejected rows'
    from (select (select coalesce(sum(stg.parse_money(line_paid_amount)), 0) from raw.remittance_advice) e,
                 (select coalesce(sum(paid_amount), 0) from core.fact_remit_line)
                 + (select coalesce(sum(amount), 0) from stg.reject where source_table = 'raw.remittance_advice') a) x;
    -- 3. every core remittance dollar is posted, excluded as a duplicate, or queued as unmatched
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'REMIT_POSTING_SPLIT', 'remittance paid amounts', e, a, e = a,
           'core paid = posted to claim lines + excluded duplicates + unmatched (exception queue) + provider-level suspense'
    from (select (select coalesce(sum(paid_amount), 0) from core.fact_remit_line) e,
                 (select coalesce(sum(r.paid_amount), 0) from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id))
               + (select coalesce(sum(r.paid_amount), 0) from recon.remit_duplicate d join core.fact_remit_line r using (remit_line_id))
               + (select coalesce(sum(q.amount), 0) from recon.exception_queue q where q.status = 'OPEN' and q.exception_type = 'UNMATCHED_REMIT')
               + (select coalesce(sum(q.amount), 0) from recon.exception_queue q where q.status = 'OPEN' and q.exception_type = 'PROVIDER_LEVEL_PAYMENT') a) x;
    -- 4. control identity, in total and per line, plus an independent recomputation from postings
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'CONTROL_IDENTITY_TOTAL', 'all claim lines', e, a, e = a, 'sum billed = sum(paid + contractual + denied + patient_resp + open)'
    from (select sum(billed) e, sum(paid + contractual + denied + patient_resp + open_balance) a from recon.claim_line_balance) x;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'CONTROL_IDENTITY_LINES_BROKEN', 'claim lines', 0, count(*), count(*) = 0, 'lines where the identity fails'
    from recon.claim_line_balance where billed <> paid + contractual + denied + patient_resp + open_balance;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'BALANCE_RECOMPUTE_MISMATCH', 'claim lines', 0, count(*), count(*) = 0,
           'lines whose stored components differ from a fresh sum of their postings'
    from recon.claim_line_balance b
    left join (select m.claim_line_id, sum(r.paid_amount) p, sum(r.contractual_amount) c, sum(r.denied_amount) d, sum(r.patient_resp_amount) pr
               from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id) group by 1) s on s.claim_line_id = b.claim_line_id
    where (b.paid, b.contractual, b.denied, b.patient_resp) is distinct from (coalesce(s.p, 0), coalesce(s.c, 0), coalesce(s.d, 0), coalesce(s.pr, 0));
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'BILLED_TIES_TO_CORE', 'claim lines', (select sum(line_charge) from core.fact_claim_line), (select sum(billed) from recon.claim_line_balance),
           (select sum(line_charge) from core.fact_claim_line) = (select sum(billed) from recon.claim_line_balance), null;

    -- 5. bank: statement balance chain
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'BANK_BALANCE_CHAIN_BREAKS', 'bank rows', 0, count(*), count(*) = 0, 'rows where previous balance + amount <> running balance'
    from (select running_balance, amount, lag(running_balance) over (order by posting_date, bank_txn_id) prev from core.fact_bank_txn) t
    where prev is not null and prev + amount <> running_balance;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'BANK_OPENING_PLUS_ACTIVITY', 'statement', e, a, e = a, 'opening + sum(amounts) = final running balance'
    from (select (select running_balance from core.fact_bank_txn order by posting_date desc, bank_txn_id desc limit 1) e,
                 (select running_balance - amount from core.fact_bank_txn order by posting_date, bank_txn_id limit 1)
                 + (select sum(amount) from core.fact_bank_txn) a) x;

    -- 6. cash identity, remittance side and bank side
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'CASH_REMIT_SIDE', 'payments dated <= as-of', e, a, e = a,
           'sum(remittance paid by trace) = matched bank deposits + unmatched remittance + timing differences'
    from (select (select coalesce(sum(header_total), 0) from recon.remit_trace
                  where header_total > 0 and payment_method in ('ACH', 'CHK') and payment_date <= v_asof) e,
                 (select coalesce(sum(m.allocated_amount), 0) from recon.remit_bank_match m join recon.remit_trace t using (trace_key) where t.payment_date <= v_asof)
               + (select coalesce(sum(amount), 0) from recon.exception_queue where status = 'OPEN' and exception_type in ('REMIT_NO_DEPOSIT', 'IN_TRANSIT')) a) x;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'CASH_BANK_SIDE', 'payer credits', e, a, e = a, 'payer credits = allocated to remittances + unapplied cash'
    from (select (select coalesce(sum(amount), 0) from core.fact_bank_txn where txn_category in ('PAYER_EFT', 'LOCKBOX')) e,
                 (select coalesce(sum(allocated_amount), 0) from recon.remit_bank_match)
               + (select coalesce(sum(amount), 0) from recon.exception_queue where status = 'OPEN' and exception_type = 'UNAPPLIED_CASH') a) x;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'ALLOCATION_INTEGRITY', 'matched deposits', 0, count(*), count(*) = 0, 'deposits whose allocations do not sum to the deposit'
    from (select b.bank_txn_id from core.fact_bank_txn b join recon.remit_bank_match m using (bank_txn_id)
          group by b.bank_txn_id, b.amount having sum(m.allocated_amount) <> b.amount) x;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'PAYMENT_ALLOCATION_INTEGRITY', 'matched payments', 0, count(*), count(*) = 0, 'payments whose allocations do not sum to the payment'
    from (select t.trace_key from recon.remit_trace t join recon.remit_bank_match m using (trace_key)
          group by t.trace_key, t.header_total having sum(m.allocated_amount) <> t.header_total) x;
    insert into recon.control_totals (run_id, check_name, scope, expected, actual, passed, detail)
    select p_run, 'TRACE_HEADER_EXPLAINED', 'all traces', e, a, e = a,
           'sum of payment headers = sum of posted lines (net of duplicates) + queued header/line mismatches'
    from (select (select coalesce(sum(header_total), 0) from recon.remit_trace) e,
                 (select coalesce(sum(lines_total_net), 0) from recon.remit_trace)
               + (select coalesce(sum(amount), 0) from recon.exception_queue where status = 'OPEN' and exception_type = 'TRACE_TOTAL_MISMATCH') a) x;

    select jsonb_build_object('checks', count(*), 'failed', count(*) filter (where not passed)) into out from recon.control_totals;
    return out;
end $$;

-- ---------------------------------------------------------------- daily reconciliation
create or replace function recon.daily_numbers(p_from date, p_to date)
returns table (
    recon_date date, opening_balance numeric, total_credits numeric, total_debits numeric, closing_balance_computed numeric,
    closing_balance_statement numeric, balance_difference numeric, payer_credits numeric, payer_credits_matched numeric,
    payer_credits_unapplied numeric, non_payer_credits numeric, remit_traces integer, remit_total numeric,
    remit_matched_same_day numeric, remit_timing_difference numeric, remit_unmatched numeric, remit_difference numeric,
    lines_billed integer, billed_amount numeric, lines_posted integer, posted_paid numeric)
language sql stable as $$
    with cal as (select g::date as d from generate_series(p_from, p_to, interval '1 day') g),
    bt0 as (   -- previous balance needs the whole statement; allocations only for the requested days
        select b.*, coalesce(lag(running_balance) over (order by posting_date, bank_txn_id), running_balance - amount) as prev_bal
        from core.fact_bank_txn b
    ),
    bt as (
        select bt0.*, coalesce(a.alloc, 0) as alloc, a.bank_txn_id is not null as is_matched
        from bt0
        left join (select bank_txn_id, sum(allocated_amount) as alloc from recon.remit_bank_match group by bank_txn_id) a
               on a.bank_txn_id = bt0.bank_txn_id
        where bt0.posting_date between p_from and p_to
    ),
    bday as (
        select posting_date as d,
               (array_agg(prev_bal order by bank_txn_id))[1] as opening,
               (array_agg(running_balance order by bank_txn_id desc))[1] as closing_stmt,
               sum(amount) filter (where amount > 0) as credits, sum(amount) filter (where amount < 0) as debits,
               sum(amount) filter (where txn_category in ('PAYER_EFT', 'LOCKBOX')) as payer_credits,
               sum(alloc) filter (where txn_category in ('PAYER_EFT', 'LOCKBOX')) as payer_matched,
               sum(amount) filter (where txn_category in ('PAYER_EFT', 'LOCKBOX') and not is_matched) as payer_unapplied
        from bt group by posting_date
    ),
    last_bal as (   -- statement balance carried into days without activity
        select c.d, (select running_balance from core.fact_bank_txn b where b.posting_date <= c.d
                     order by posting_date desc, bank_txn_id desc limit 1) as bal_eod,
                    (select running_balance from core.fact_bank_txn b where b.posting_date < c.d
                     order by posting_date desc, bank_txn_id desc limit 1) as bal_prev
        from cal c
    ),
    tr as (
        select t.trace_key, t.payment_date as d, t.header_total,
               coalesce((select sum(m.allocated_amount) from recon.remit_bank_match m join core.fact_bank_txn b using (bank_txn_id)
                         where m.trace_key = t.trace_key and b.posting_date = t.payment_date), 0) as same_day,
               coalesce((select sum(m.allocated_amount) from recon.remit_bank_match m join core.fact_bank_txn b using (bank_txn_id)
                         where m.trace_key = t.trace_key and b.posting_date <> t.payment_date), 0) as other_day,
               coalesce((select q.amount from recon.exception_queue q where q.status = 'OPEN' and q.exception_key = 'IN_TRANSIT:' || t.trace_key), 0) as in_transit,
               coalesce((select q.amount from recon.exception_queue q where q.status = 'OPEN' and q.exception_key = 'REMIT_NO_DEPOSIT:' || t.trace_key), 0) as no_deposit
        from recon.remit_trace t
        where t.header_total > 0 and t.payment_method in ('ACH', 'CHK') and t.payment_date between p_from and p_to
    ),
    rday as (
        select d, count(*)::integer as n, sum(header_total) as total, sum(same_day) as same_day,
               sum(other_day + in_transit) as timing, sum(no_deposit) as unmatched
        from tr group by d
    ),
    billed as (select submission_date as d, count(*)::integer as n, sum(line_charge) as amt from core.fact_claim_line group by 1),
    posted as (select r.payment_date as d, count(*)::integer as n, sum(r.paid_amount) as amt
               from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id) group by 1)
    select c.d,
           coalesce(bd.opening, lb.bal_prev, (select running_balance - amount from core.fact_bank_txn order by posting_date, bank_txn_id limit 1)),
           coalesce(bd.credits, 0), coalesce(bd.debits, 0),
           coalesce(bd.opening, lb.bal_prev, (select running_balance - amount from core.fact_bank_txn order by posting_date, bank_txn_id limit 1))
             + coalesce(bd.credits, 0) + coalesce(bd.debits, 0),
           coalesce(bd.closing_stmt, lb.bal_eod, (select running_balance - amount from core.fact_bank_txn order by posting_date, bank_txn_id limit 1)),
           null::numeric,
           coalesce(bd.payer_credits, 0), coalesce(bd.payer_matched, 0), coalesce(bd.payer_unapplied, 0),
           coalesce(bd.credits, 0) - coalesce(bd.payer_credits, 0),
           coalesce(rd.n, 0), coalesce(rd.total, 0), coalesce(rd.same_day, 0), coalesce(rd.timing, 0), coalesce(rd.unmatched, 0),
           coalesce(rd.total, 0) - coalesce(rd.same_day, 0) - coalesce(rd.timing, 0) - coalesce(rd.unmatched, 0),
           coalesce(bi.n, 0), coalesce(bi.amt, 0), coalesce(po.n, 0), coalesce(po.amt, 0)
    from cal c
    left join bday bd on bd.d = c.d
    left join last_bal lb on lb.d = c.d
    left join rday rd on rd.d = c.d
    left join billed bi on bi.d = c.d
    left join posted po on po.d = c.d
    order by c.d
$$;

create or replace function recon.build_daily_recon() returns integer language plpgsql as $$
declare v_from date; v_to date; n integer;
begin
    select least((select min(posting_date) from core.fact_bank_txn), (select min(payment_date) from recon.remit_trace)),
           greatest(recon.param('as_of_date')::date, (select max(posting_date) from core.fact_bank_txn))
      into v_from, v_to;
    if v_from is null then return 0; end if;
    insert into recon.daily_recon as d (recon_date, opening_balance, total_credits, total_debits, closing_balance_computed,
        closing_balance_statement, balance_difference, payer_credits, payer_credits_matched, payer_credits_unapplied,
        non_payer_credits, remit_traces, remit_total, remit_matched_same_day, remit_timing_difference, remit_unmatched,
        remit_difference, lines_billed, billed_amount, lines_posted, posted_paid, status, numbers_hash)
    select x.recon_date, x.opening_balance, x.total_credits, x.total_debits, x.closing_balance_computed,
           x.closing_balance_statement, x.closing_balance_computed - x.closing_balance_statement, x.payer_credits,
           x.payer_credits_matched, x.payer_credits_unapplied, x.non_payer_credits, x.remit_traces, x.remit_total,
           x.remit_matched_same_day, x.remit_timing_difference, x.remit_unmatched, x.remit_difference, x.lines_billed,
           x.billed_amount, x.lines_posted, x.posted_paid, 'OPEN',
           md5(concat_ws('|', x.opening_balance, x.total_credits, x.total_debits, x.closing_balance_statement,
                         x.payer_credits, x.payer_credits_matched, x.remit_total, x.remit_matched_same_day,
                         x.remit_timing_difference, x.remit_unmatched, x.lines_billed, x.billed_amount, x.lines_posted, x.posted_paid))
    from recon.daily_numbers(v_from, v_to) x
    on conflict (recon_date) do update set
        opening_balance = excluded.opening_balance, total_credits = excluded.total_credits, total_debits = excluded.total_debits,
        closing_balance_computed = excluded.closing_balance_computed, closing_balance_statement = excluded.closing_balance_statement,
        balance_difference = excluded.balance_difference, payer_credits = excluded.payer_credits,
        payer_credits_matched = excluded.payer_credits_matched, payer_credits_unapplied = excluded.payer_credits_unapplied,
        non_payer_credits = excluded.non_payer_credits, remit_traces = excluded.remit_traces, remit_total = excluded.remit_total,
        remit_matched_same_day = excluded.remit_matched_same_day, remit_timing_difference = excluded.remit_timing_difference,
        remit_unmatched = excluded.remit_unmatched, remit_difference = excluded.remit_difference,
        lines_billed = excluded.lines_billed, billed_amount = excluded.billed_amount, lines_posted = excluded.lines_posted,
        posted_paid = excluded.posted_paid,
        status = case when d.numbers_hash = excluded.numbers_hash then d.status
                      when d.status = 'CLOSED' then 'REOPENED' else 'OPEN' end,
        closed_at = case when d.numbers_hash = excluded.numbers_hash then d.closed_at end,
        numbers_hash = excluded.numbers_hash;
    get diagnostics n = row_count;
    return n;
end $$;
