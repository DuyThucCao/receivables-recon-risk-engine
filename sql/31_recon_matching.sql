-- 31_recon_matching.sql
-- Matching. Each rule METHOD is a function; recon.run_matching(stage) runs the active rules of a
-- stage in priority order, reading tolerance and date window from recon.match_rule. Matches are
-- only added for rows that are still unmatched, so the same code serves full and incremental runs.
-- A candidate is accepted only if it is unique (no guessing between two equally good candidates).

-- ---------------------------------------------------------------- duplicates
create or replace function recon.m_identical_line_in_trace(p_rule text, p_run bigint) returns integer
language plpgsql as $$
declare n integer;
begin
    -- Copies of a verbatim line within one trace. A claim can legitimately bill the same service
    -- twice (two identical lines); if the payer did not echo line control numbers, both remittance
    -- lines are verbatim identical too. So only copies beyond the number of identical claim lines
    -- are duplicates. With a line control number, a line can appear once.
    insert into recon.remit_duplicate (remit_line_id, duplicate_of_remit_line_id, rule_id, run_id)
    with g as (
        select r.remit_line_id, r.patient_control_no_norm, r.lic_norm, r.procedure_code, r.service_date,
               abs(r.line_charge) as charge,
               row_number() over w as rn, first_value(r.remit_line_id) over w as first_id
        from core.fact_remit_line r
        where not r.is_provider_level
        window w as (partition by r.trace_key, r.line_hash order by r.remit_line_id)
    ), k as (
        select g.*,
               case when g.lic_norm is not null then 1
                    else greatest(1, (select count(*) from core.fact_claim_line c
                                      where c.claim_id_norm = g.patient_control_no_norm and c.procedure_code = g.procedure_code
                                        and c.service_date = g.service_date and c.line_charge = g.charge)) end as allowed_copies
        from g where g.rn > 1
    )
    select k.remit_line_id, k.first_id, p_rule, p_run
    from k
    where k.rn > k.allowed_copies
      and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = k.remit_line_id)
      and not exists (select 1 from recon.claim_remit_match m where m.remit_line_id = k.remit_line_id);
    get diagnostics n = row_count;
    return n;
end $$;

-- remittance lines still waiting for a claim line
create or replace function recon._unmatched_remit() returns void language plpgsql as $$
begin
    drop table if exists _todo;
    create temp table _todo as
    select r.remit_line_id, r.patient_control_no_raw, r.patient_control_no_norm, r.member_id_norm, r.lic_norm,
           r.procedure_code, r.service_date, abs(r.line_charge) as charge, r.payer_claim_control_no, r.claim_status_code
    from core.fact_remit_line r
    where not r.is_provider_level
      and not exists (select 1 from recon.claim_remit_match m where m.remit_line_id = r.remit_line_id)
      and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = r.remit_line_id);
    create index on _todo (patient_control_no_raw);
    create index on _todo (patient_control_no_norm);
    analyze _todo;
end $$;

-- ---------------------------------------------------------------- claim <-> remittance
-- CLAIM_ID_EXACT / CLAIM_ID_NORMALISED: find the claim by identifier, then the line by line item
-- control number (preferred) or by procedure + service date + charge. A candidate is rejected if
-- the remittance carries a line control number or member id that contradicts it (this guard was
-- added after evaluation showed transposed claim numbers colliding with other real claims).
create or replace function recon.m_claim_by_id(p_rule text, p_run bigint, p_normalised boolean) returns integer
language plpgsql as $$
declare v_tol numeric; n integer; v_claim_col text; v_remit_col text;
begin
    select amount_tolerance into v_tol from recon.match_rule where rule_id = p_rule;
    perform recon._unmatched_remit();
    v_claim_col := case when p_normalised then 'claim_id_norm' else 'claim_id' end;
    v_remit_col := case when p_normalised then 'patient_control_no_norm' else 'patient_control_no_raw' end;
    execute format($q$
        with cand as (
            select t.remit_line_id, c.claim_line_id,
                   case when t.lic_norm is not null and t.lic_norm = c.lic_norm then 1 else 2 end as line_rank
            from _todo t
            join core.fact_claim_line c on c.%I = t.%I
            where ((t.lic_norm is not null and t.lic_norm = c.lic_norm)
                   or (t.procedure_code = c.procedure_code and t.service_date = c.service_date
                       and abs(t.charge - c.line_charge) <= %L::numeric))
              -- corroborating evidence must not contradict the identifier: an echoed line control
              -- number or a member id that disagrees rules the candidate out
              and (t.lic_norm is null or c.lic_norm is null or t.lic_norm = c.lic_norm)
              and (t.member_id_norm is null or c.member_id_norm is null or t.member_id_norm = c.member_id_norm)
        ), ranked as (
            select *, min(line_rank) over (partition by remit_line_id) as best,
                   row_number() over (partition by remit_line_id order by line_rank, claim_line_id) as rn
            from cand
        ), best as (
            select remit_line_id, claim_line_id, rn, count(*) filter (where line_rank = best) over (partition by remit_line_id) as n_best
            from ranked where line_rank = best
        )
        insert into recon.claim_remit_match (remit_line_id, claim_line_id, rule_id, run_id)
        select remit_line_id, claim_line_id, %L, %s from best where rn = 1 and n_best = 1 $q$,
        v_claim_col, v_remit_col, v_tol, p_rule, p_run);
    get diagnostics n = row_count;
    -- second pass: several equally good candidates can only be identical sibling lines of one claim
    -- (same procedure, date and charge, no line control number). Pair the k-th such remittance line
    -- of a claim payment (same payer claim number and status) with the k-th sibling line by line number.
    execute format($q$
        with cand as (
            select t.remit_line_id, c.claim_line_id, c.claim_id
            from _todo t
            join core.fact_claim_line c on c.%I = t.%I
            where t.lic_norm is null
              and t.procedure_code = c.procedure_code and t.service_date = c.service_date
              and abs(t.charge - c.line_charge) <= %L::numeric
              and (t.member_id_norm is null or c.member_id_norm is null or t.member_id_norm = c.member_id_norm)
              and not exists (select 1 from recon.claim_remit_match m where m.remit_line_id = t.remit_line_id)
        ), multi as (
            select remit_line_id, min(claim_id) as claim_id from cand group by remit_line_id
            having count(*) > 1 and count(distinct claim_id) = 1
        ), r_ord as (
            select t.remit_line_id, mu.claim_id, t.procedure_code, t.service_date, t.charge,
                   row_number() over (partition by mu.claim_id, t.procedure_code, t.service_date, t.charge,
                                                   t.payer_claim_control_no, t.claim_status_code order by t.remit_line_id) as k
            from _todo t join multi mu on mu.remit_line_id = t.remit_line_id
        ), c_ord as (
            select c.claim_line_id, c.claim_id, c.procedure_code, c.service_date, c.line_charge,
                   row_number() over (partition by c.claim_id, c.procedure_code, c.service_date, c.line_charge order by c.line_no) as k
            from core.fact_claim_line c where c.claim_id in (select claim_id from multi)
        )
        insert into recon.claim_remit_match (remit_line_id, claim_line_id, rule_id, run_id)
        select r.remit_line_id, c.claim_line_id, %L, %s
        from r_ord r join c_ord c on c.claim_id = r.claim_id and c.procedure_code = r.procedure_code
                                  and c.service_date = r.service_date and c.line_charge = r.charge and c.k = r.k $q$,
        v_claim_col, v_remit_col, v_tol, p_rule, p_run);
    declare m integer; begin get diagnostics m = row_count; n := n + m; end;
    return n;
end $$;

-- MEMBER_DATE_AMOUNT: identifier failed (e.g. transposed digits). Same member, service date in
-- window, charge within tolerance; procedure code agreement ranks first; unique best candidate.
create or replace function recon.m_member_date_amount(p_rule text, p_run bigint) returns integer
language plpgsql as $$
declare v_tol numeric; v_win integer; n integer;
begin
    select amount_tolerance, coalesce(date_window_days, 0) into v_tol, v_win from recon.match_rule where rule_id = p_rule;
    perform recon._unmatched_remit();
    with cand as (
        select t.remit_line_id, c.claim_line_id,
               case when t.lic_norm is not null and t.lic_norm = c.lic_norm then 0
                    when t.procedure_code = c.procedure_code then 1 else 2 end as rk
        from _todo t
        join core.fact_claim_line c
          on c.member_id_norm = t.member_id_norm
         and c.service_date between t.service_date - v_win and t.service_date + v_win
        where abs(c.line_charge - t.charge) <= v_tol
    ), ranked as (
        select *, min(rk) over (partition by remit_line_id) as best from cand
    ), best as (
        select remit_line_id, claim_line_id,
               row_number() over (partition by remit_line_id order by claim_line_id) as rn,
               count(*) over (partition by remit_line_id) as n_best
        from ranked where rk = best
    )
    insert into recon.claim_remit_match (remit_line_id, claim_line_id, rule_id, run_id)
    select remit_line_id, claim_line_id, p_rule, p_run from best where rn = 1 and n_best = 1;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- reversals
-- Rebuilt every run: pair the k-th reversal on a line with the k-th earlier posting it negates.
create or replace function recon.m_negated_posting(p_rule text, p_run bigint) returns integer
language plpgsql as $$
declare n integer;
begin
    truncate recon.reversal_link;
    insert into recon.reversal_link (reversal_remit_line_id, original_remit_line_id, claim_line_id, rule_id)
    with posted as (
        select m.claim_line_id, r.remit_line_id, r.payment_date, r.is_reversal, r.paid_amount, r.allowed_amount, r.denied_amount
        from recon.claim_remit_match m join core.fact_remit_line r on r.remit_line_id = m.remit_line_id
    ), orig as (
        select *, row_number() over (partition by claim_line_id, paid_amount, allowed_amount, denied_amount
                                     order by payment_date, remit_line_id) as k
        from posted where not is_reversal
    ), rev as (
        select *, row_number() over (partition by claim_line_id, paid_amount, allowed_amount, denied_amount
                                     order by payment_date, remit_line_id) as k
        from posted where is_reversal
    )
    select rev.remit_line_id, orig.remit_line_id, rev.claim_line_id, p_rule
    from rev join orig
      on orig.claim_line_id = rev.claim_line_id and orig.paid_amount = -rev.paid_amount
     and orig.allowed_amount = -rev.allowed_amount and orig.denied_amount = -rev.denied_amount and orig.k = rev.k
    where orig.payment_date <= rev.payment_date;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- traces (rebuilt every run)
create or replace function recon.build_traces() returns integer language plpgsql as $$
declare n integer;
begin
    truncate recon.remit_trace;
    insert into recon.remit_trace
    with files as (
        select trace_key, remit_file_id, max(file_payment_amount) as amt
        from core.fact_remit_line group by trace_key, remit_file_id
    ), rej as (
        select upper(btrim(raw_record->>'payer_id')) || ':' || upper(btrim(raw_record->>'trace_no')) as trace_key, count(*) as n
        from stg.reject where source_table = 'raw.remittance_advice' group by 1
    )
    select r.trace_key, min(r.payer_id), min(r.trace_no), min(r.payment_method), min(r.payment_date),
           (select count(*) from files f where f.trace_key = r.trace_key),
           (select coalesce(sum(f.amt), 0) from files f where f.trace_key = r.trace_key),
           coalesce(sum(r.paid_amount) filter (where d.remit_line_id is null), 0),
           sum(r.paid_amount), count(*), count(d.remit_line_id), coalesce(max(rej.n), 0)
    from core.fact_remit_line r
    left join recon.remit_duplicate d on d.remit_line_id = r.remit_line_id
    left join rej on rej.trace_key = r.trace_key
    group by r.trace_key;
    get diagnostics n = row_count;
    analyze recon.remit_trace;
    return n;
end $$;

create or replace function recon._unmatched_cash() returns void language plpgsql as $$
begin
    drop table if exists _tr;
    create temp table _tr as
    select t.* from recon.remit_trace t
    where t.header_total > 0 and t.payment_method in ('ACH', 'CHK')
      and not exists (select 1 from recon.remit_bank_match m where m.trace_key = t.trace_key);
    drop table if exists _bk;
    create temp table _bk as
    select b.* from core.fact_bank_txn b
    where b.txn_category in ('PAYER_EFT', 'LOCKBOX')
      and not exists (select 1 from recon.remit_bank_match m where m.bank_txn_id = b.bank_txn_id);
end $$;

-- ---------------------------------------------------------------- remittance <-> bank
-- TRACE_EXACT and TRACE_NORMALISED (leading zeros removed, or bank trace truncated to a prefix)
create or replace function recon.m_trace(p_rule text, p_run bigint, p_normalised boolean) returns integer
language plpgsql as $$
declare v_tol numeric; v_win integer; n integer;
begin
    select amount_tolerance, coalesce(date_window_days, 5) into v_tol, v_win from recon.match_rule where rule_id = p_rule;
    perform recon._unmatched_cash();
    with cand as (
        select t.trace_key, b.bank_txn_id, t.header_total
        from _tr t join _bk b
          on b.txn_category = 'PAYER_EFT'
         and b.posting_date between t.payment_date - 1 and t.payment_date + v_win
         and abs(b.amount - t.header_total) <= v_tol
        where t.payment_method = 'ACH' and b.trace_no_extracted is not null
          and case when p_normalised then
                    b.trace_no_extracted <> t.trace_no
                    and (ltrim(b.trace_no_extracted, '0') = ltrim(t.trace_no, '0')
                         or (length(b.trace_no_extracted) >= 6 and length(b.trace_no_extracted) < length(t.trace_no)
                             and left(t.trace_no, length(b.trace_no_extracted)) = b.trace_no_extracted))
               else b.trace_no_extracted = t.trace_no end
    ), u as (
        select *, count(*) over (partition by trace_key) as nt, count(*) over (partition by bank_txn_id) as nb from cand
    )
    insert into recon.remit_bank_match (trace_key, bank_txn_id, allocated_amount, rule_id, run_id)
    select trace_key, bank_txn_id, header_total, p_rule, p_run from u where nt = 1 and nb = 1;
    get diagnostics n = row_count;
    return n;
end $$;

-- TRACE_SPLIT: one payment arrives as two or more credits, each carrying its trace (exact or a
-- truncated prefix). All its candidate credits must sum to the payment and belong to no other payment.
create or replace function recon.m_trace_split(p_rule text, p_run bigint) returns integer
language plpgsql as $$
declare v_tol numeric; v_win integer; n integer;
begin
    select amount_tolerance, coalesce(date_window_days, 6) into v_tol, v_win from recon.match_rule where rule_id = p_rule;
    perform recon._unmatched_cash();
    with cand as (
        select t.trace_key, t.header_total, b.bank_txn_id, b.amount
        from _tr t join _bk b
          on b.txn_category = 'PAYER_EFT' and b.trace_no_extracted is not null
         and (b.trace_no_extracted = t.trace_no
              or (length(b.trace_no_extracted) >= 6 and left(t.trace_no, length(b.trace_no_extracted)) = b.trace_no_extracted))
         and b.posting_date between t.payment_date - 1 and t.payment_date + v_win
        where t.payment_method = 'ACH'
    ), shared as (
        select bank_txn_id from cand group by bank_txn_id having count(distinct trace_key) > 1
    ), ok as (
        select trace_key from cand
        group by trace_key
        having count(*) >= 2 and abs(sum(amount) - max(header_total)) <= v_tol
           and not bool_or(bank_txn_id in (select bank_txn_id from shared))
    )
    insert into recon.remit_bank_match (trace_key, bank_txn_id, allocated_amount, rule_id, run_id)
    select c.trace_key, c.bank_txn_id, c.amount, p_rule, p_run from cand c join ok using (trace_key);
    get diagnostics n = row_count;
    return n;
end $$;

-- LOCKBOX_SUBSET_SUM: one lockbox deposit = several check payments. Search combinations of
-- exactly ITEMS n unmatched check traces dated in the window before the deposit whose totals sum
-- to the deposit. Accept only a unique combination. Candidate sets are small (a few checks a
-- week), and the search is bounded (at most 20 candidates, at most 8 items).
create or replace function recon.m_lockbox_subset(p_rule text, p_run bigint) returns integer
language plpgsql as $$
declare v_tol numeric; v_win integer; b record; keys text[]; amts numeric[]; k integer; v_items integer;
        sol text; n_sol integer; v_total integer := 0; sol_keys text[];
begin
    select amount_tolerance, coalesce(date_window_days, 10) into v_tol, v_win from recon.match_rule where rule_id = p_rule;
    perform recon._unmatched_cash();
    for b in select * from _bk where txn_category = 'LOCKBOX' order by posting_date, bank_txn_id loop
        select array_agg(t.trace_key order by t.payment_date, t.trace_key), array_agg(t.header_total order by t.payment_date, t.trace_key)
          into keys, amts
        from _tr t
        where t.payment_method = 'CHK' and t.payment_date between b.posting_date - v_win and b.posting_date
          and not exists (select 1 from recon.remit_bank_match m where m.trace_key = t.trace_key);
        k := coalesce(array_length(keys, 1), 0);
        continue when k = 0 or k > 20;
        v_items := coalesce(b.lockbox_item_count, 0);
        continue when v_items > 8;
        with recursive combo(last_i, ids, ctotal, n) as (
            select i, array[i], amts[i], 1 from generate_series(1, k) i where amts[i] <= b.amount + v_tol
            union all
            select j, c.ids || j, c.ctotal + amts[j], c.n + 1
            from combo c join generate_series(1, k) j on j > c.last_i
            where c.n < (case when v_items > 0 then v_items else 8 end) and c.ctotal + amts[j] <= b.amount + v_tol
        )
        select count(*), min(array_to_string(ids, ',')) into n_sol, sol
        from combo where abs(ctotal - b.amount) <= v_tol and (v_items = 0 or combo.n = v_items);
        if n_sol = 1 then
            select array_agg(keys[x::int]) into sol_keys from unnest(string_to_array(sol, ',')) x;
            insert into recon.remit_bank_match (trace_key, bank_txn_id, allocated_amount, rule_id, run_id)
            select t.trace_key, b.bank_txn_id, t.header_total, p_rule, p_run
            from recon.remit_trace t where t.trace_key = any(sol_keys);
            v_total := v_total + array_length(sol_keys, 1);
        end if;
    end loop;
    return v_total;
end $$;

-- PAYER_AMOUNT_WINDOW: EFT with no usable trace: payer from ACH company name + amount + window
create or replace function recon.m_payer_amount_window(p_rule text, p_run bigint) returns integer
language plpgsql as $$
declare v_tol numeric; v_win integer; n integer;
begin
    select amount_tolerance, coalesce(date_window_days, 4) into v_tol, v_win from recon.match_rule where rule_id = p_rule;
    perform recon._unmatched_cash();
    with cand as (
        select t.trace_key, b.bank_txn_id, t.header_total
        from _tr t join _bk b
          on b.txn_category = 'PAYER_EFT' and b.payer_id_inferred = t.payer_id
         and b.posting_date between t.payment_date - 1 and t.payment_date + v_win
         and abs(b.amount - t.header_total) <= v_tol
        where t.payment_method = 'ACH'
    ), u as (
        select *, count(*) over (partition by trace_key) as nt, count(*) over (partition by bank_txn_id) as nb from cand
    )
    insert into recon.remit_bank_match (trace_key, bank_txn_id, allocated_amount, rule_id, run_id)
    select trace_key, bank_txn_id, header_total, p_rule, p_run from u where nt = 1 and nb = 1;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- dispatcher
create or replace function recon.run_matching(p_stage text, p_run bigint) returns jsonb
language plpgsql as $$
declare r record; n integer; out jsonb := '{}'::jsonb;
begin
    for r in select * from recon.match_rule where stage = p_stage and active order by priority loop
        n := case r.method
            when 'IDENTICAL_LINE_IN_TRACE' then recon.m_identical_line_in_trace(r.rule_id, p_run)
            when 'CLAIM_ID_EXACT'          then recon.m_claim_by_id(r.rule_id, p_run, false)
            when 'CLAIM_ID_NORMALISED'     then recon.m_claim_by_id(r.rule_id, p_run, true)
            when 'MEMBER_DATE_AMOUNT'      then recon.m_member_date_amount(r.rule_id, p_run)
            when 'NEGATED_POSTING_ON_LINE' then recon.m_negated_posting(r.rule_id, p_run)
            when 'TRACE_EXACT'             then recon.m_trace(r.rule_id, p_run, false)
            when 'TRACE_NORMALISED'        then recon.m_trace(r.rule_id, p_run, true)
            when 'TRACE_SPLIT'             then recon.m_trace_split(r.rule_id, p_run)
            when 'LOCKBOX_SUBSET_SUM'      then recon.m_lockbox_subset(r.rule_id, p_run)
            when 'PAYER_AMOUNT_WINDOW'     then recon.m_payer_amount_window(r.rule_id, p_run)
            else null end;
        if n is null then
            raise exception 'match_rule % has unknown method %', r.rule_id, r.method;
        end if;
        out := out || jsonb_build_object(r.rule_id, n);
    end loop;
    return out;
end $$;
