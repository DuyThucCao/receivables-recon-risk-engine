-- 32_recon_derive.sql
-- Derived reconciliation state, rebuilt on every run from core + match tables:
-- claim-line balances (control identity), the claim-line state machine, the exception queue,
-- control totals and the daily reconciliation table.

-- ---------------------------------------------------------------- balances
create or replace function recon.build_balances() returns integer language plpgsql as $$
declare n integer; v_tol numeric;
begin
    select amount_tolerance into v_tol from recon.match_rule where rule_id = 'UP10_BELOW_CONTRACT';
    truncate recon.claim_line_balance cascade;
    insert into recon.claim_line_balance
    with p as (
        select m.claim_line_id, sum(r.paid_amount) as paid, sum(r.contractual_amount) as ca,
               sum(r.denied_amount) as den, sum(r.patient_resp_amount) as pr, sum(r.allowed_amount) as allowed,
               count(*) as n, min(r.payment_date) as first_d, max(r.payment_date) as last_d
        from recon.claim_remit_match m join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
        group by m.claim_line_id
    )
    select c.claim_line_id, c.claim_id, c.line_no, c.payer_id, c.service_line_code, c.procedure_code, c.service_date,
           c.submission_date, c.line_charge,
           coalesce(p.paid, 0), coalesce(p.ca, 0), coalesce(p.den, 0), coalesce(p.pr, 0),
           c.line_charge - coalesce(p.paid, 0) - coalesce(p.ca, 0) - coalesce(p.den, 0) - coalesce(p.pr, 0),
           coalesce(p.allowed, 0), e.expected, e.contract_rate_id,
           case when coalesce(p.allowed, 0) > 0 and e.expected is not null and e.expected - p.allowed > v_tol
                then e.expected - p.allowed else 0 end,
           coalesce(p.n, 0), p.first_d, p.last_d
    from core.fact_claim_line c
    left join p on p.claim_line_id = c.claim_line_id
    left join lateral (
        select k.contract_rate_id,
               case when k.lesser_of_charges then least(c.line_charge, x.amt) else x.amt end as expected
        from core.dim_contract_rate k
        cross join lateral (select case k.rate_type
                                       when 'FEE_SCHEDULE' then round(k.rate_value * coalesce(c.units, 1), 2)
                                       when 'PCT_OF_CHARGE' then round(c.line_charge * k.rate_value / 100, 2) end as amt) x
        where k.payer_id = c.payer_id and c.service_date between k.effective_from and k.effective_to
          and (k.procedure_scope = 'ALL' or k.procedure_code = c.procedure_code)
        order by (k.procedure_scope = 'CODE') desc, k.effective_from desc
        limit 1) e on true;
    get diagnostics n = row_count;
    analyze recon.claim_line_balance;
    return n;
end $$;

-- ---------------------------------------------------------------- claim-line state machine
create or replace function recon.build_line_status() returns integer language plpgsql as $$
declare n integer; v_tol numeric;
begin
    select amount_tolerance into v_tol from recon.match_rule where rule_id = 'UP10_BELOW_CONTRACT';
    truncate recon.claim_line_status_history;
    insert into recon.claim_line_status_history
    with ev as (
        select m.claim_line_id, r.remit_line_id, r.payment_date, r.is_reversal, r.paid_amount, r.contractual_amount,
               r.denied_amount, r.patient_resp_amount,
               row_number() over (partition by m.claim_line_id order by r.payment_date, r.remit_line_id) as seq
        from recon.claim_remit_match m join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
    ), cum as (
        select ev.*, b.billed,
               sum(paid_amount) over w as cp, sum(contractual_amount) over w as cc,
               sum(denied_amount) over w as cd, sum(patient_resp_amount) over w as cpr,
               bool_or(is_reversal) over w as any_rev
        from ev join recon.claim_line_balance b on b.claim_line_id = ev.claim_line_id
        window w as (partition by ev.claim_line_id order by seq rows between unbounded preceding and current row)
    ), st as (
        select *,
               case when billed - cp - cc - cd - cpr < 0 then 'CREDIT_BALANCE'
                    when cp = 0 and cc = 0 and cd = 0 and cpr = 0 then case when any_rev then 'REVERSED' else 'SUBMITTED' end
                    when billed - cp - cc - cd - cpr = 0 and cd > 0 and cp = 0 then 'DENIED'
                    when billed - cp - cc - cd - cpr = 0 then 'PAID'
                    else 'PARTIAL' end as status_after,
               case when is_reversal then 'REVERSAL'
                    when paid_amount > 0 then 'PAYMENT'
                    when paid_amount < 0 then 'ADJUSTMENT'
                    when denied_amount > 0 then 'DENIAL'
                    else 'OTHER' end as event_type
        from cum
    )
    select claim_line_id, seq, remit_line_id, payment_date, event_type,
           coalesce(lag(status_after) over (partition by claim_line_id order by seq), 'SUBMITTED'),
           status_after, false
    from st;
    update recon.claim_line_status_history h set transition_allowed = true
    from recon.line_status_transition t where t.from_status = h.status_before and t.to_status = h.status_after;

    truncate recon.claim_line_status;
    insert into recon.claim_line_status
    select b.claim_line_id, coalesce(h.status_after, 'SUBMITTED'), h.event_date, h.event_type, b.n_postings,
           b.underpaid_amount > v_tol, ce.status, coalesce(ce.status = 'WRITTEN_OFF', false)
    from recon.claim_line_balance b
    left join lateral (select * from recon.claim_line_status_history x where x.claim_line_id = b.claim_line_id
                       order by seq desc limit 1) h on true
    left join lateral (select e.status from core.fact_claim_event e where e.claim_id = b.claim_id
                       order by e.event_ts desc, e.claim_event_id desc limit 1) ce on true;
    get diagnostics n = row_count;
    -- A reversal whose original posting never reached us leaves the balance meaningless (e.g. a
    -- negative denial plus a later payment nets to open_balance = billed). Flag the line and give it
    -- its own status so it is not read as a partial payment.
    -- The same applies to a downward adjustment (negative paid) with no earlier payment on the line.
    update recon.claim_line_status s set has_orphan_reversal = true, status = 'ORPHAN_REVERSAL'
    where exists (select 1 from recon.claim_remit_match m join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
                  where m.claim_line_id = s.claim_line_id and r.is_reversal
                    and not exists (select 1 from recon.reversal_link l where l.reversal_remit_line_id = r.remit_line_id))
       or exists (select 1 from recon.claim_line_status_history h join core.fact_remit_line r on r.remit_line_id = h.remit_line_id
                  where h.claim_line_id = s.claim_line_id and h.event_type = 'ADJUSTMENT'
                    and not exists (select 1 from recon.claim_line_status_history h2 join core.fact_remit_line r2 on r2.remit_line_id = h2.remit_line_id
                                    where h2.claim_line_id = h.claim_line_id and h2.seq < h.seq and r2.paid_amount > 0));
    return n;
end $$;

-- business days strictly between two dates (weekdays only; good enough for a window test)
create or replace function recon.weekdays_between(d0 date, d1 date) returns integer
language sql immutable as $$
    select count(*)::integer from generate_series(d0 + 1, d1, interval '1 day') g where extract(isodow from g) < 6
$$;

-- ---------------------------------------------------------------- exception queue
create or replace function recon.build_exceptions(p_run bigint) returns jsonb language plpgsql as $$
declare v_asof date := recon.param('as_of_date')::date;
        v_win integer := recon.param('in_transit_window_bdays')::integer;
        v_noresp integer := recon.param('no_response_days')::integer;
        v_stale integer := recon.param('stale_reversal_days')::integer;
        out jsonb;
begin
    drop table if exists _exc;
    create temp table _exc as select * from recon.exception_queue where false;
    alter table _exc drop column exception_id;

    -- remittance lines nobody could match
    insert into _exc (exception_key, exception_type, severity, claim_id, remit_line_id, trace_key, source_table, src_file,
                      src_row_no, amount, event_date, reason)
    select 'UNMATCHED_REMIT:' || r.remit_line_id, 'UNMATCHED_REMIT', 'HIGH', null, r.remit_line_id, r.trace_key,
           'core.fact_remit_line', r.src_file, r.src_row_no, r.paid_amount, r.payment_date,
           case when (select count(*) from core.fact_claim_line c where c.member_id_norm = r.member_id_norm
                      and c.service_date = r.service_date and c.line_charge = abs(r.line_charge)) >= 2
                     and not exists (select 1 from core.fact_claim_line c where c.claim_id_norm = r.patient_control_no_norm)
                then format('AMBIGUOUS: identifier %s matches no claim, and %s claim lines share member %s, service date %s and charge %s with no line control number to tell them apart; not matched automatically',
                            r.patient_control_no_raw,
                            (select count(*) from core.fact_claim_line c where c.member_id_norm = r.member_id_norm
                               and c.service_date = r.service_date and c.line_charge = abs(r.line_charge)),
                            r.member_id_norm, r.service_date, abs(r.line_charge))
                when exists (select 1 from core.fact_claim_line c where c.claim_id_norm = r.patient_control_no_norm)
                then format('claim %s found but no line with procedure %s, service date %s, charge %s (line may have been rejected in staging)',
                            r.patient_control_no_norm, r.procedure_code, r.service_date, abs(r.line_charge))
                when exists (select 1 from core.fact_claim_line c where c.member_id_norm = r.member_id_norm)
                then format('no claim for identifier %s; member %s known but no unique line on %s for %s',
                            r.patient_control_no_raw, r.member_id_norm, r.service_date, abs(r.line_charge))
                else format('no claim for identifier %s and member %s is not ours (possible payer misdirection)',
                            r.patient_control_no_raw, r.member_id_norm) end
    from core.fact_remit_line r
    where not r.is_provider_level
      and not exists (select 1 from recon.claim_remit_match m where m.remit_line_id = r.remit_line_id)
      and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = r.remit_line_id);

    -- provider-level payments (PLB) are not claim money: they wait in a suspense bucket until reconciled
    insert into _exc (exception_key, exception_type, severity, remit_line_id, trace_key, source_table, src_file, src_row_no,
                      amount, event_date, reason)
    select 'PROVIDER_LEVEL_PAYMENT:' || r.remit_line_id, 'PROVIDER_LEVEL_PAYMENT', 'INFO', r.remit_line_id, r.trace_key,
           'core.fact_remit_line', r.src_file, r.src_row_no, r.paid_amount, r.payment_date,
           format('provider-level payment %s from %s (not tied to claims; held in suspense until the payer reconciles it)', r.paid_amount, r.payer_id)
    from core.fact_remit_line r where r.is_provider_level;

    insert into _exc (exception_key, exception_type, severity, remit_line_id, trace_key, source_table, src_file, src_row_no,
                      amount, event_date, rule_id, reason)
    select 'DUPLICATE_REMIT_LINE:' || d.remit_line_id, 'DUPLICATE_REMIT_LINE', 'INFO', d.remit_line_id, r.trace_key,
           'core.fact_remit_line', r.src_file, r.src_row_no, r.paid_amount, r.payment_date, d.rule_id,
           format('verbatim repeat of remittance line %s in trace %s; excluded from posting', d.duplicate_of_remit_line_id, r.trace_key)
    from recon.remit_duplicate d join core.fact_remit_line r on r.remit_line_id = d.remit_line_id;

    insert into _exc (exception_key, exception_type, severity, claim_id, claim_line_id, remit_line_id, trace_key, amount, event_date, reason)
    select 'ORPHAN_REVERSAL:' || r.remit_line_id, 'ORPHAN_REVERSAL', 'MEDIUM', c.claim_id, c.claim_line_id, r.remit_line_id,
           r.trace_key, r.paid_amount, r.payment_date,
           'reversal (status 22) with no earlier posting on this line that it negates exactly'
    from recon.claim_remit_match m join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
    join core.fact_claim_line c on c.claim_line_id = m.claim_line_id
    where r.is_reversal and not exists (select 1 from recon.reversal_link l where l.reversal_remit_line_id = r.remit_line_id);

    insert into _exc (exception_key, exception_type, severity, claim_id, claim_line_id, remit_line_id, amount, event_date, reason)
    select 'ILLEGAL_TRANSITION:' || h.claim_line_id || ':' || h.seq, 'ILLEGAL_TRANSITION', 'HIGH', b.claim_id, h.claim_line_id,
           h.remit_line_id, null, h.event_date, format('posting moved the line from %s to %s (%s), which the state machine does not allow',
           h.status_before, h.status_after, h.event_type)
    from recon.claim_line_status_history h join recon.claim_line_balance b on b.claim_line_id = h.claim_line_id
    where not h.transition_allowed;

    insert into _exc (exception_key, exception_type, severity, claim_id, claim_line_id, amount, event_date, reason)
    select 'CREDIT_BALANCE:' || b.claim_line_id, 'CREDIT_BALANCE', 'HIGH', b.claim_id, b.claim_line_id, -b.open_balance,
           b.last_remit_date,
           case when (select count(distinct r.trace_key) from recon.claim_remit_match m
                      join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
                      where m.claim_line_id = b.claim_line_id and not r.is_reversal and r.paid_amount > 0) >= 2
                then 'postings exceed the billed charge: the payer paid this line in more than one payment (possible duplicate payment; expect a recoupment)'
                else 'postings exceed the billed charge' end
    from recon.claim_line_balance b where b.open_balance < 0;

    insert into _exc (exception_key, exception_type, severity, claim_id, claim_line_id, amount, event_date, rule_id, reason)
    select 'UNDERPAYMENT:' || b.claim_line_id, 'UNDERPAYMENT', 'MEDIUM', b.claim_id, b.claim_line_id, b.underpaid_amount,
           b.last_remit_date, 'UP10_BELOW_CONTRACT',
           format('payer allowed %s versus contract %s (%s %s)', b.payer_allowed, b.expected_allowed, b.payer_id, b.procedure_code)
    from recon.claim_line_balance b where b.underpaid_amount > 0;

    insert into _exc (exception_key, exception_type, severity, trace_key, amount, event_date, reason)
    select 'TRACE_TOTAL_MISMATCH:' || t.trace_key, 'TRACE_TOTAL_MISMATCH', 'HIGH', t.trace_key,
           t.header_total - t.lines_total_net, t.payment_date,
           format('payment header says %s, posted lines sum to %s (%s lines rejected in staging, %s duplicates excluded)',
                  t.header_total, t.lines_total_net, t.n_rejected_lines, t.n_duplicate_lines)
    from recon.remit_trace t where t.header_total <> t.lines_total_net;

    insert into _exc (exception_key, exception_type, severity, bank_txn_id, source_table, src_file, src_row_no, amount, event_date, reason)
    select 'UNAPPLIED_CASH:' || b.bank_txn_id, 'UNAPPLIED_CASH', 'HIGH', b.bank_txn_id, 'core.fact_bank_txn', b.src_file,
           b.src_row_no, b.amount, b.posting_date,
           case when b.trace_no_extracted is not null and exists (
                         select 1 from recon.remit_trace t where t.trace_no = b.trace_no_extracted
                            or (length(b.trace_no_extracted) >= 6 and left(t.trace_no, length(b.trace_no_extracted)) = b.trace_no_extracted))
                then format('POSSIBLE SPLIT OR PARTIAL DEPOSIT: trace %s belongs to payment(s) %s, but this credit is %s; needs manual allocation',
                            b.trace_no_extracted,
                            (select string_agg(t.trace_key || ' of ' || t.header_total, ', ') from recon.remit_trace t
                              where t.trace_no = b.trace_no_extracted
                                 or (length(b.trace_no_extracted) >= 6 and left(t.trace_no, length(b.trace_no_extracted)) = b.trace_no_extracted)),
                            b.amount)
                when (select count(*) from recon.remit_trace t
                      where t.payer_id = b.payer_id_inferred and t.header_total = b.amount and t.payment_method = 'ACH'
                        and b.posting_date between t.payment_date - 1 and t.payment_date + 6
                        and not exists (select 1 from recon.remit_bank_match m where m.trace_key = t.trace_key)) >= 1
                then format('AMBIGUOUS: credit %s from %s with no trace fits %s unmatched payment(s) of the same amount in the date window, and other credits compete for them; not matched automatically',
                            b.amount, b.payer_id_inferred,
                            (select count(*) from recon.remit_trace t
                              where t.payer_id = b.payer_id_inferred and t.header_total = b.amount and t.payment_method = 'ACH'
                                and b.posting_date between t.payment_date - 1 and t.payment_date + 6
                                and not exists (select 1 from recon.remit_bank_match m where m.trace_key = t.trace_key)))
                else format('%s credit %s on %s has no remittance (%s)', b.txn_category, b.amount, b.posting_date,
                            coalesce('payer ' || b.payer_id_inferred, 'payer not recognised') || coalesce(', trace ' || b.trace_no_extracted, ', no trace in description')) end
    from core.fact_bank_txn b
    where b.txn_category in ('PAYER_EFT', 'LOCKBOX')
      and not exists (select 1 from recon.remit_bank_match m where m.bank_txn_id = b.bank_txn_id);

    insert into _exc (exception_key, exception_type, severity, trace_key, amount, event_date, reason)
    select case when recon.weekdays_between(t.payment_date, v_asof) <= v_win then 'IN_TRANSIT:' else 'REMIT_NO_DEPOSIT:' end || t.trace_key,
           case when recon.weekdays_between(t.payment_date, v_asof) <= v_win then 'IN_TRANSIT' else 'REMIT_NO_DEPOSIT' end,
           case when recon.weekdays_between(t.payment_date, v_asof) <= v_win then 'INFO' else 'HIGH' end,
           t.trace_key, t.header_total, t.payment_date,
           case when exists (select 1 from core.fact_bank_txn b
                             where b.payer_id_inferred = t.payer_id and b.amount = t.header_total and b.txn_category = 'PAYER_EFT'
                               and b.posting_date between t.payment_date - 1 and t.payment_date + 6
                               and not exists (select 1 from recon.remit_bank_match m where m.bank_txn_id = b.bank_txn_id))
                then format('AMBIGUOUS: %s payment %s of %s dated %s has a same-payer, same-amount credit in the window but another payment competes for it; not matched automatically',
                            t.payment_method, t.trace_no, t.header_total, t.payment_date)
                when exists (select 1 from core.fact_bank_txn b
                             where b.trace_no_extracted is not null and b.txn_category = 'PAYER_EFT'
                               and (b.trace_no_extracted = t.trace_no or (length(b.trace_no_extracted) >= 6 and left(t.trace_no, length(b.trace_no_extracted)) = b.trace_no_extracted))
                               and not exists (select 1 from recon.remit_bank_match m where m.bank_txn_id = b.bank_txn_id))
                then format('POSSIBLE SPLIT OR PARTIAL DEPOSIT: a credit carrying trace %s exists but does not equal the payment of %s; needs manual allocation',
                            t.trace_no, t.header_total)
                else format('%s payment %s of %s dated %s has no matching bank deposit as of %s', t.payment_method, t.trace_no,
                            t.header_total, t.payment_date, v_asof) end
    from recon.remit_trace t
    where t.header_total > 0 and t.payment_method in ('ACH', 'CHK') and t.payment_date <= v_asof
      and not exists (select 1 from recon.remit_bank_match m where m.trace_key = t.trace_key);

    insert into _exc (exception_key, exception_type, severity, claim_id, claim_line_id, amount, event_date, reason)
    select 'STALE_REVERSAL:' || s.claim_line_id, 'STALE_REVERSAL', 'MEDIUM', b.claim_id, s.claim_line_id, b.open_balance, s.status_since,
           format('line reversed on %s and not reposted by %s', s.status_since, v_asof)
    from recon.claim_line_status s join recon.claim_line_balance b on b.claim_line_id = s.claim_line_id
    where s.status = 'REVERSED' and s.status_since < v_asof - v_stale;

    insert into _exc (exception_key, exception_type, severity, claim_id, claim_line_id, amount, event_date, reason)
    select 'NO_RESPONSE:' || b.claim_line_id, 'NO_RESPONSE', 'MEDIUM', b.claim_id, b.claim_line_id, b.billed, b.submission_date,
           format('no remittance %s days after submission (claim status %s)', v_asof - b.submission_date, coalesce(s.claim_status, 'unknown'))
    from recon.claim_line_balance b join recon.claim_line_status s on s.claim_line_id = b.claim_line_id
    where s.status = 'SUBMITTED' and b.submission_date < v_asof - v_noresp and not s.is_written_off;

    insert into _exc (exception_key, exception_type, severity, remit_line_id, amount, event_date, reason)
    select 'UNKNOWN_REASON_CODE:' || r.remit_line_id, 'UNKNOWN_REASON_CODE', 'LOW', r.remit_line_id, r.denied_amount, r.payment_date,
           'adjustment reason code not in core.dim_reason_code; treated as a denial'
    from core.fact_remit_line r where r.has_unknown_reason;

    insert into _exc (exception_key, exception_type, severity, source_table, src_file, src_row_no, amount, reason)
    select 'STAGING_REJECT:' || j.source_table || ':' || j.src_file || ':' || j.src_row_no, 'STAGING_REJECT',
           case when j.source_table in ('raw.claims_export', 'raw.remittance_advice', 'raw.bank_statement')
                     and j.reason_code not in ('DUPLICATE_ROW', 'DUPLICATE_BANK_REFERENCE') then 'HIGH' else 'LOW' end,
           j.source_table, j.src_file, j.src_row_no, j.amount, j.reason_code || coalesce(': ' || j.reason_detail, '')
    from stg.reject j;

    insert into recon.exception_queue (exception_key, exception_type, severity, claim_id, claim_line_id, remit_line_id, trace_key,
        bank_txn_id, source_table, src_file, src_row_no, amount, event_date, rule_id, reason, status, first_seen_run, last_seen_run)
    select exception_key, exception_type, severity, claim_id, claim_line_id, remit_line_id, trace_key, bank_txn_id, source_table,
           src_file, src_row_no, amount, event_date, rule_id, reason, 'OPEN', p_run, p_run
    from _exc
    on conflict (exception_key) do update set severity = excluded.severity, amount = excluded.amount,
        event_date = excluded.event_date, reason = excluded.reason, status = 'OPEN', last_seen_run = excluded.last_seen_run;
    update recon.exception_queue set status = 'CLEARED' where status = 'OPEN' and last_seen_run < p_run;

    select jsonb_object_agg(exception_type, n) into out
    from (select exception_type, count(*) n from recon.exception_queue where status = 'OPEN' group by 1) x;
    drop table if exists _exc;
    return coalesce(out, '{}'::jsonb);
end $$;
