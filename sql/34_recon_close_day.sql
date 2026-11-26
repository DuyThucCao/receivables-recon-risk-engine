-- 34_recon_close_day.sql
-- recon.close_day(p_date): verify one day against the base tables and mark it CLOSED.
-- It REFUSES (raises an exception, so nothing is written) if any check fails:
--   * the previous day is not closed (days close in order);
--   * daily_recon is stale (stored numbers differ from a fresh recomputation);
--   * the bank statement does not chain: previous balance + amount <> running balance, or
--     opening + credits + debits <> closing balance;
--   * a deposit's allocations do not sum to the deposit, or a matched payment is not fully allocated;
--   * remittance side does not tie: payments dated that day <> same-day deposits + timing
--     differences + unmatched remittances;
--   * any unmatched item of that day (unapplied cash, payment without deposit, remittance line
--     without claim, header/line mismatch) is missing from the exception queue;
--   * a claim line posted that day fails the control identity or differs from a fresh sum of its postings.
create or replace procedure recon.close_day(p_date date) language plpgsql as $$
declare
    v_first date; v_prev text; v_err text[] := '{}'; v_n integer; d record; f record;
begin
    select min(recon_date) into v_first from recon.daily_recon;
    select * into d from recon.daily_recon where recon_date = p_date;
    if not found then
        raise exception 'close_day(%) refused: no daily_recon row for this date (run recon.run_all first)', p_date;
    end if;
    if p_date > v_first then
        select status into v_prev from recon.daily_recon where recon_date = p_date - 1;
        if v_prev is distinct from 'CLOSED' then
            v_err := v_err || format('previous day %s is %s, not CLOSED', p_date - 1, coalesce(v_prev, 'missing'));
        end if;
    end if;

    select * into f from recon.daily_numbers(p_date, p_date);
    if (d.opening_balance, d.total_credits, d.total_debits, d.closing_balance_statement, d.payer_credits,
        d.payer_credits_matched, d.remit_total, d.remit_matched_same_day, d.remit_timing_difference, d.remit_unmatched,
        d.lines_posted, d.posted_paid)
       is distinct from
       (f.opening_balance, f.total_credits, f.total_debits, f.closing_balance_statement, f.payer_credits,
        f.payer_credits_matched, f.remit_total, f.remit_matched_same_day, f.remit_timing_difference, f.remit_unmatched,
        f.lines_posted, f.posted_paid) then
        v_err := v_err || 'daily_recon is stale: stored numbers differ from the base tables (rerun recon.run_all)'::text;
    end if;
    if f.closing_balance_computed <> f.closing_balance_statement then
        v_err := v_err || format('bank: opening %s + credits %s + debits %s = %s but statement closing balance is %s',
                                 f.opening_balance, f.total_credits, f.total_debits, f.closing_balance_computed, f.closing_balance_statement);
    end if;
    select count(*) into v_n from (
        select posting_date, amount, running_balance, lag(running_balance) over (order by posting_date, bank_txn_id) as prev
        from core.fact_bank_txn where posting_date <= p_date) t
    where t.posting_date = p_date and t.prev is not null and t.prev + t.amount <> t.running_balance;
    if v_n > 0 then
        v_err := v_err || format('bank: %s statement rows on this day break the running-balance chain', v_n);
    end if;

    select count(*) into v_n from (
        select b.bank_txn_id from core.fact_bank_txn b join recon.remit_bank_match m using (bank_txn_id)
        where b.posting_date = p_date group by b.bank_txn_id, b.amount having sum(m.allocated_amount) <> b.amount) x;
    if v_n > 0 then v_err := v_err || format('cash: %s deposits whose allocations do not sum to the deposit', v_n); end if;
    select count(*) into v_n from (
        select t.trace_key from recon.remit_trace t join recon.remit_bank_match m using (trace_key)
        where t.payment_date = p_date group by t.trace_key, t.header_total having sum(m.allocated_amount) <> t.header_total) x;
    if v_n > 0 then v_err := v_err || format('cash: %s payments not fully allocated to their deposit', v_n); end if;
    if f.remit_difference <> 0 then
        v_err := v_err || format('cash: payments dated %s total %s but same-day %s + timing %s + unmatched %s leaves %s unexplained',
                                 p_date, f.remit_total, f.remit_matched_same_day, f.remit_timing_difference, f.remit_unmatched, f.remit_difference);
    end if;

    select count(*) into v_n from core.fact_bank_txn b
    where b.posting_date = p_date and b.txn_category in ('PAYER_EFT', 'LOCKBOX')
      and not exists (select 1 from recon.remit_bank_match m where m.bank_txn_id = b.bank_txn_id)
      and not exists (select 1 from recon.exception_queue q where q.status = 'OPEN' and q.exception_key = 'UNAPPLIED_CASH:' || b.bank_txn_id);
    if v_n > 0 then v_err := v_err || format('cash: %s payer credits neither matched nor queued as unapplied cash', v_n); end if;
    select count(*) into v_n from recon.remit_trace t
    where t.payment_date = p_date and t.header_total > 0 and t.payment_method in ('ACH', 'CHK')
      and not exists (select 1 from recon.remit_bank_match m where m.trace_key = t.trace_key)
      and not exists (select 1 from recon.exception_queue q where q.status = 'OPEN'
                      and q.exception_key in ('IN_TRANSIT:' || t.trace_key, 'REMIT_NO_DEPOSIT:' || t.trace_key));
    if v_n > 0 then v_err := v_err || format('cash: %s payments neither matched to a deposit nor queued', v_n); end if;
    select count(*) into v_n from recon.remit_trace t
    where t.payment_date = p_date and t.header_total <> t.lines_total_net
      and not exists (select 1 from recon.exception_queue q where q.status = 'OPEN' and q.exception_key = 'TRACE_TOTAL_MISMATCH:' || t.trace_key);
    if v_n > 0 then v_err := v_err || format('remittance: %s payments whose lines do not sum to the header and are not queued', v_n); end if;

    select count(*) into v_n from core.fact_remit_line r
    where r.payment_date = p_date
      and not exists (select 1 from recon.claim_remit_match m where m.remit_line_id = r.remit_line_id)
      and not exists (select 1 from recon.remit_duplicate x where x.remit_line_id = r.remit_line_id)
      and not exists (select 1 from recon.exception_queue q where q.status = 'OPEN'
                      and q.exception_key in ('UNMATCHED_REMIT:' || r.remit_line_id, 'PROVIDER_LEVEL_PAYMENT:' || r.remit_line_id));
    if v_n > 0 then v_err := v_err || format('remittance: %s lines neither posted, excluded as duplicate, nor queued', v_n); end if;

    select count(*) into v_n from recon.claim_line_balance b
    join (select m.claim_line_id, sum(r.paid_amount) p, sum(r.contractual_amount) c, sum(r.denied_amount) dn, sum(r.patient_resp_amount) pr
          from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id)
          where m.claim_line_id in (select m2.claim_line_id from recon.claim_remit_match m2 join core.fact_remit_line r2 using (remit_line_id)
                                    where r2.payment_date = p_date)
          group by m.claim_line_id) s on s.claim_line_id = b.claim_line_id
    where (b.paid, b.contractual, b.denied, b.patient_resp) is distinct from (s.p, s.c, s.dn, s.pr)
       or b.billed <> b.paid + b.contractual + b.denied + b.patient_resp + b.open_balance;
    if v_n > 0 then v_err := v_err || format('claims: %s claim lines posted today fail the control identity or differ from their postings', v_n); end if;

    if coalesce(array_length(v_err, 1), 0) > 0 then
        raise exception 'close_day(%) refused: %', p_date, array_to_string(v_err, '; ');
    end if;
    update recon.daily_recon set status = 'CLOSED', closed_at = now() where recon_date = p_date;
    insert into recon.day_close_log (recon_date, outcome, message) values (p_date, 'CLOSED', null);
end $$;
