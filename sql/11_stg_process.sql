-- 11_stg_process.sql
-- stg.process_new() types every raw row not yet staged or rejected. It is incremental: the
-- full build and the intake helper call the same function. Each loader parses into a temporary
-- table, assigns a reject reason where a required value is missing or malformed or the row is
-- a repeat of an already staged business key, then splits into stg.<table> and stg.reject.

create or replace function stg.write_rejects(p_table text) returns void language plpgsql as $$
begin
    execute format($f$
        insert into stg.reject (source_table, src_file, src_row_no, reason_code, reason_detail, amount, raw_record)
        select %L, t._file, t._row_no, t._reason, t._detail, t._amount, to_jsonb(r) - '_loaded_at'
        from _t t join %s r on r._file = t._file and r._row_no = t._row_no
        where t._reason is not null
        on conflict do nothing $f$, p_table, p_table);
end $$;

-- ---------------------------------------------------------------- claims
create or replace function stg.load_claim_lines() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no,
           upper(stg.clean(r.claim_id)) as claim_id, stg.norm_id(r.claim_id) as claim_id_norm,
           stg.clean(r.form_type) as form_type, stg.clean(r.bill_type) as bill_type,
           stg.clean(r.facility_name) as facility_name, stg.clean(r.facility_npi) as facility_npi,
           stg.clean(r.patient_account_no) as patient_account_no, stg.clean(r.member_id) as member_id,
           stg.norm_member(r.member_id) as member_id_norm, upper(stg.clean(r.payer_id)) as payer_id,
           stg.clean(r.payer_name) as payer_name, upper(stg.clean(r.service_line)) as service_line,
           stg.clean(r.attending_provider_id) as attending_provider_id,
           stg.parse_date(r.statement_from_date) as statement_from_date,
           stg.parse_date(r.statement_to_date) as statement_to_date,
           stg.parse_date(r.submission_date) as submission_date,
           stg.parse_money(r.claim_total_charge) as claim_total_charge,
           stg.parse_int(r.line_no) as line_no, stg.clean(r.line_item_control_no) as line_item_control_no,
           stg.norm_id(r.line_item_control_no) as lic_norm, stg.clean(r.revenue_code) as revenue_code,
           upper(stg.clean(r.procedure_code)) as procedure_code, stg.clean(r.modifier_1) as modifier_1,
           stg.clean(r.ndc_code) as ndc_code, stg.clean(r.diagnosis_code_1) as diagnosis_code_1,
           stg.clean(r.diagnosis_code_2) as diagnosis_code_2, stg.parse_date(r.service_date) as service_date,
           stg.parse_decimal(r.units) as units, stg.parse_money(r.line_charge) as line_charge,
           md5(concat_ws('|', r.claim_id, r.line_no, r.procedure_code, r.service_date, r.line_charge, r.units)) as _md5,
           null::text as _reason, null::text as _detail, stg.parse_money(r.line_charge) as _amount
    from raw.claims_export r
    where not exists (select 1 from stg.claim_line s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.claims_export'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when claim_id is null then 'MISSING_CLAIM_ID'
            when line_no is null then 'UNPARSEABLE_LINE_NO'
            when payer_id is null then 'MISSING_PAYER'
            when procedure_code is null then 'MISSING_PROCEDURE_CODE'
            when line_charge is null then 'UNPARSEABLE_AMOUNT'
            when service_date is null or submission_date is null then 'UNPARSEABLE_DATE'
            when line_charge < 0 then 'NEGATIVE_CHARGE' end;
    update _t set _detail = 'raw line_charge=' || coalesce(r.line_charge, '<null>') || ', procedure_code=' || coalesce(r.procedure_code, '<null>')
    from raw.claims_export r where r._file = _t._file and r._row_no = _t._row_no and _t._reason is not null;
    -- repeated business key (claim_id, line_no): within this batch, then against staged rows
    with d as (
        select _file, _row_no, _md5,
               first_value(_md5) over w as first_md5, row_number() over w as rn
        from _t where _reason is null
        window w as (partition by claim_id, line_no order by _file, _row_no))
    update _t set _reason = case when d._md5 = d.first_md5 then 'DUPLICATE_ROW' else 'CONFLICTING_DUPLICATE_KEY' end,
                  _detail = 'same claim_id and line_no as an earlier row in this load'
    from d where d._file = _t._file and d._row_no = _t._row_no and d.rn > 1;
    update _t set _reason = 'DUPLICATE_ROW', _detail = 'claim line already staged from an earlier load'
    where _reason is null and exists (select 1 from stg.claim_line s where s.claim_id = _t.claim_id and s.line_no = _t.line_no);
    perform stg.write_rejects('raw.claims_export');
    insert into stg.claim_line
    select _file, _row_no, claim_id, claim_id_norm, form_type, bill_type, facility_name, facility_npi,
           patient_account_no, member_id, member_id_norm, payer_id, payer_name, service_line,
           attending_provider_id, statement_from_date, statement_to_date, submission_date, claim_total_charge,
           line_no, line_item_control_no, lic_norm, revenue_code, procedure_code, modifier_1, ndc_code,
           diagnosis_code_1, diagnosis_code_2, service_date, units, line_charge
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- remittances
create or replace function stg.load_remit_lines() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no, stg.clean(r.remit_file_id) as remit_file_id, upper(stg.clean(r.payer_id)) as payer_id,
           stg.clean(r.payer_name) as payer_name, stg.clean(r.payer_tax_id) as payer_tax_id,
           stg.clean(r.payee_name) as payee_name, stg.clean(r.payee_npi) as payee_npi,
           upper(stg.clean(r.trace_no)) as trace_no, upper(stg.clean(r.payment_method)) as payment_method,
           stg.parse_date(r.payment_date) as payment_date, stg.parse_money(r.file_payment_amount) as file_payment_amount,
           stg.clean(r.patient_control_no) as patient_control_no_raw, stg.norm_id(r.patient_control_no) as patient_control_no_norm,
           stg.clean(r.payer_claim_control_no) as payer_claim_control_no, stg.clean(r.claim_status_code) as claim_status_code,
           stg.parse_money(r.claim_charge_amount) as claim_charge_amount, stg.parse_money(r.claim_paid_amount) as claim_paid_amount,
           stg.clean(r.member_id) as member_id, stg.norm_member(r.member_id) as member_id_norm,
           stg.parse_date(r.service_date) as service_date, upper(stg.clean(r.procedure_code)) as procedure_code,
           stg.clean(r.modifier_1) as modifier_1, stg.parse_decimal(r.units) as units,
           stg.clean(r.line_item_control_no) as line_item_control_no, stg.norm_id(r.line_item_control_no) as lic_norm,
           stg.parse_money(r.line_charge_amount) as line_charge, stg.parse_money(r.line_allowed_amount) as allowed_amount,
           stg.parse_money(r.line_paid_amount) as paid_amount,
           upper(stg.clean(r.adj_group_1)) as adj_group_1, stg.clean(r.adj_reason_1) as adj_reason_1, stg.parse_money(r.adj_amount_1) as adj_amount_1,
           upper(stg.clean(r.adj_group_2)) as adj_group_2, stg.clean(r.adj_reason_2) as adj_reason_2, stg.parse_money(r.adj_amount_2) as adj_amount_2,
           upper(stg.clean(r.adj_group_3)) as adj_group_3, stg.clean(r.adj_reason_3) as adj_reason_3, stg.parse_money(r.adj_amount_3) as adj_amount_3,
           stg.clean(r.remark_code) as remark_code,
           md5(concat_ws('|', r.remit_file_id, r.payer_id, r.trace_no, r.payment_date, r.patient_control_no,
                         r.payer_claim_control_no, r.claim_status_code, r.member_id, r.service_date, r.procedure_code,
                         r.units, r.line_item_control_no, r.line_charge_amount, r.line_allowed_amount, r.line_paid_amount,
                         r.adj_group_1, r.adj_reason_1, r.adj_amount_1, r.adj_group_2, r.adj_reason_2, r.adj_amount_2,
                         r.adj_group_3, r.adj_reason_3, r.adj_amount_3)) as line_hash,
           -- raw texts kept to detect "present but unparseable"
           r.line_paid_amount as _paid_txt, r.adj_amount_1 as _a1, r.adj_amount_2 as _a2, r.adj_amount_3 as _a3,
           null::text as _reason, null::text as _detail, stg.parse_money(r.line_paid_amount) as _amount
    from raw.remittance_advice r
    where not exists (select 1 from stg.remit_line s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.remittance_advice'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when payer_id is null then 'MISSING_PAYER'
            when trace_no is null then 'MISSING_TRACE_NO'
            when payment_date is null or service_date is null then 'UNPARSEABLE_DATE'
            when paid_amount is null or line_charge is null or allowed_amount is null then 'UNPARSEABLE_AMOUNT'
            when (stg.clean(_a1) is not null and adj_amount_1 is null) or (stg.clean(_a2) is not null and adj_amount_2 is null)
              or (stg.clean(_a3) is not null and adj_amount_3 is null) then 'UNPARSEABLE_AMOUNT' end;
    update _t set _detail = 'raw paid=' || coalesce(_paid_txt, '<null>')
    from raw.remittance_advice r where r._file = _t._file and r._row_no = _t._row_no and _t._reason is not null;
    perform stg.write_rejects('raw.remittance_advice');
    insert into stg.remit_line
    select _file, _row_no, remit_file_id, payer_id, payer_name, payer_tax_id, payee_name, payee_npi, trace_no,
           payment_method, payment_date, file_payment_amount, patient_control_no_raw, patient_control_no_norm,
           payer_claim_control_no, claim_status_code, claim_charge_amount, claim_paid_amount, member_id, member_id_norm,
           service_date, procedure_code, modifier_1, units, line_item_control_no, lic_norm, line_charge, allowed_amount,
           paid_amount, adj_group_1, adj_reason_1, adj_amount_1, adj_group_2, adj_reason_2, adj_amount_2,
           adj_group_3, adj_reason_3, adj_amount_3, remark_code, line_hash
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- bank statement
create or replace function stg.load_bank_txns() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no, stg.clean(r.account_number) as account_number,
           stg.parse_date(r.posting_date) as posting_date, stg.parse_date(r.value_date) as value_date,
           upper(stg.clean(r.transaction_type)) as transaction_type, stg.clean(r.description) as description,
           stg.parse_money(r.debit) as debit, stg.parse_money(r.credit) as credit,
           coalesce(stg.parse_money(r.credit), 0) - coalesce(stg.parse_money(r.debit), 0) as amount,
           stg.parse_money(r.running_balance) as running_balance, upper(stg.clean(r.bank_reference)) as bank_reference,
           stg.clean(r.customer_reference) as customer_reference,
           upper(stg.extract_trace(r.description)) as trace_no_extracted,
           stg.extract_item_count(r.description) as lockbox_item_count,
           (stg.clean(r.debit) is not null and stg.parse_money(r.debit) is null)
             or (stg.clean(r.credit) is not null and stg.parse_money(r.credit) is null)
             or (stg.clean(r.debit) is null and stg.clean(r.credit) is null) as _bad_amount,
           null::text as _reason, null::text as _detail,
           coalesce(stg.parse_money(r.credit), 0) - coalesce(stg.parse_money(r.debit), 0) as _amount
    from raw.bank_statement r
    where not exists (select 1 from stg.bank_txn s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.bank_statement'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when posting_date is null then 'UNPARSEABLE_DATE'
            when _bad_amount or running_balance is null then 'UNPARSEABLE_AMOUNT'
            when bank_reference is null then 'MISSING_BANK_REFERENCE' end;
    with d as (select _file, _row_no, row_number() over (partition by bank_reference order by _file, _row_no) rn
               from _t where _reason is null)
    update _t set _reason = 'DUPLICATE_BANK_REFERENCE', _detail = 'bank reference repeated in this load (overlapping statement exports)'
    from d where d._file = _t._file and d._row_no = _t._row_no and d.rn > 1;
    update _t set _reason = 'DUPLICATE_BANK_REFERENCE', _detail = 'bank reference already staged from an earlier load'
    where _reason is null and exists (select 1 from stg.bank_txn s where s.bank_reference = _t.bank_reference);
    perform stg.write_rejects('raw.bank_statement');
    insert into stg.bank_txn
    select _file, _row_no, account_number, posting_date, value_date, transaction_type, description, debit, credit,
           amount, running_balance, bank_reference, customer_reference, trace_no_extracted, lockbox_item_count
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- contracts
create or replace function stg.load_contract_rates() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no, stg.clean(r.contract_id) as contract_id, upper(stg.clean(r.payer_id)) as payer_id,
           stg.clean(r.payer_name) as payer_name, upper(stg.clean(r.payer_type)) as payer_type,
           stg.clean(r.payer_tax_id) as payer_tax_id, upper(stg.clean(r.ach_company_name)) as ach_company_name,
           upper(stg.clean(r.procedure_scope)) as procedure_scope, upper(stg.clean(r.procedure_code)) as procedure_code,
           upper(stg.clean(r.rate_type)) as rate_type, stg.parse_decimal(r.rate) as rate_value,
           upper(stg.clean(r.unit_basis)) as unit_basis, stg.parse_date(r.effective_from) as effective_from,
           stg.parse_date(r.effective_to) as effective_to, upper(stg.clean(r.lesser_of_charges)) = 'Y' as lesser_of_charges,
           stg.clean(r.notes) as notes, null::text as _reason, null::text as _detail, null::numeric(14,2) as _amount
    from raw.payer_contracts r
    where not exists (select 1 from stg.contract_rate s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.payer_contracts'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when payer_id is null then 'MISSING_PAYER'
            when rate_type not in ('FEE_SCHEDULE', 'PCT_OF_CHARGE') or rate_type is null then 'UNKNOWN_RATE_TYPE'
            when rate_value is null then 'UNPARSEABLE_RATE'
            when effective_from is null or effective_to is null then 'UNPARSEABLE_DATE'
            when procedure_scope = 'CODE' and procedure_code is null then 'MISSING_PROCEDURE_CODE' end;
    with d as (select _file, _row_no, row_number() over (partition by payer_id, procedure_scope, coalesce(procedure_code, ''), effective_from
                                                          order by _file, _row_no) rn from _t where _reason is null)
    update _t set _reason = 'DUPLICATE_ROW', _detail = 'same payer, procedure and effective date as an earlier row'
    from d where d._file = _t._file and d._row_no = _t._row_no and d.rn > 1;
    update _t set _reason = 'DUPLICATE_ROW', _detail = 'rate already staged from an earlier load'
    where _reason is null and exists (select 1 from stg.contract_rate s where s.payer_id = _t.payer_id
          and s.procedure_scope = _t.procedure_scope and coalesce(s.procedure_code, '') = coalesce(_t.procedure_code, '')
          and s.effective_from = _t.effective_from);
    perform stg.write_rejects('raw.payer_contracts');
    insert into stg.contract_rate
    select _file, _row_no, contract_id, payer_id, payer_name, payer_type, payer_tax_id, ach_company_name, procedure_scope,
           procedure_code, rate_type, rate_value, unit_basis, effective_from, effective_to, lesser_of_charges, notes
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- drug purchases
create or replace function stg.load_drug_purchases() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no, stg.clean(r.invoice_no) as invoice_no, stg.parse_date(r.invoice_date) as invoice_date,
           stg.clean(r.wholesaler_name) as wholesaler_name, stg.clean(r.wholesaler_id) as wholesaler_id,
           stg.clean(r.ship_to_facility) as ship_to_facility, stg.clean(r.po_number) as po_number,
           stg.parse_int(r.invoice_line_no) as invoice_line_no, stg.clean(r.ndc_code) as ndc_code,
           stg.clean(r.product_description) as product_description, upper(stg.clean(r.lot_no)) as lot_no,
           stg.parse_date(r.expiry_date) as expiry_date, stg.parse_int(r.qty_packages) as qty_packages,
           stg.parse_decimal(r.billing_units_per_package) as billing_units_per_package,
           stg.parse_money(r.unit_price) as unit_price, stg.parse_money(r.extended_amount) as extended_amount,
           stg.clean(r.payment_terms) as payment_terms, stg.parse_date(r.due_date) as due_date,
           upper(stg.clean(r.order_type)) as order_type,
           null::text as _reason, null::text as _detail, stg.parse_money(r.extended_amount) as _amount
    from raw.drug_purchase_invoices r
    where not exists (select 1 from stg.drug_purchase s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.drug_purchase_invoices'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when invoice_no is null or invoice_line_no is null then 'MISSING_INVOICE_KEY'
            when invoice_date is null or expiry_date is null then 'UNPARSEABLE_DATE'
            when qty_packages is null or billing_units_per_package is null then 'UNPARSEABLE_QUANTITY'
            when unit_price is null or extended_amount is null then 'UNPARSEABLE_AMOUNT'
            when ndc_code is null or lot_no is null then 'MISSING_PRODUCT_OR_LOT'
            when extended_amount <> unit_price * qty_packages then 'EXTENSION_MISMATCH' end;
    with d as (select _file, _row_no, row_number() over (partition by invoice_no, invoice_line_no order by _file, _row_no) rn
               from _t where _reason is null)
    update _t set _reason = 'DUPLICATE_ROW' from d where d._file = _t._file and d._row_no = _t._row_no and d.rn > 1;
    update _t set _reason = 'DUPLICATE_ROW' where _reason is null and exists (
        select 1 from stg.drug_purchase s where s.invoice_no = _t.invoice_no and s.invoice_line_no = _t.invoice_line_no);
    perform stg.write_rejects('raw.drug_purchase_invoices');
    insert into stg.drug_purchase
    select _file, _row_no, invoice_no, invoice_date, wholesaler_name, wholesaler_id, ship_to_facility, po_number,
           invoice_line_no, ndc_code, product_description, lot_no, expiry_date, qty_packages, billing_units_per_package,
           unit_price, extended_amount, payment_terms, due_date, order_type
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- drug dispense log
create or replace function stg.load_drug_dispenses() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no, stg.clean(r.dispense_id) as dispense_id, stg.parse_ts(r.event_datetime) as event_ts,
           upper(stg.clean(r.event_type)) as event_type, stg.clean(r.facility_name) as facility_name,
           stg.clean(r.ndc_code) as ndc_code, upper(stg.clean(r.hcpcs_code)) as hcpcs_code,
           stg.clean(r.drug_description) as drug_description, upper(stg.clean(r.lot_no)) as lot_no,
           stg.parse_decimal(r.billing_units) as billing_units, stg.clean(r.patient_account_no) as patient_account_no,
           upper(stg.clean(r.claim_id)) as claim_id, stg.parse_int(r.claim_line_no) as claim_line_no,
           stg.clean(r.recorded_by) as recorded_by,
           null::text as _reason, null::text as _detail, null::numeric(14,2) as _amount
    from raw.drug_dispense_log r
    where not exists (select 1 from stg.drug_dispense s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.drug_dispense_log'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when dispense_id is null then 'MISSING_DISPENSE_ID'
            when event_ts is null then 'UNPARSEABLE_DATE'
            when event_type not in ('ADMINISTER', 'WASTE_EXPIRED') or event_type is null then 'UNKNOWN_EVENT_TYPE'
            when billing_units is null then 'UNPARSEABLE_UNITS' end;
    update _t set _detail = 'raw billing_units=' || coalesce(r.billing_units, '<null>')
    from raw.drug_dispense_log r where r._file = _t._file and r._row_no = _t._row_no and _t._reason is not null;
    with d as (select _file, _row_no, row_number() over (partition by dispense_id order by _file, _row_no) rn from _t where _reason is null)
    update _t set _reason = 'DUPLICATE_ROW' from d where d._file = _t._file and d._row_no = _t._row_no and d.rn > 1;
    update _t set _reason = 'DUPLICATE_ROW' where _reason is null and exists (
        select 1 from stg.drug_dispense s where s.dispense_id = _t.dispense_id);
    perform stg.write_rejects('raw.drug_dispense_log');
    insert into stg.drug_dispense
    select _file, _row_no, dispense_id, event_ts, event_type, facility_name, ndc_code, hcpcs_code, drug_description,
           lot_no, billing_units, patient_account_no, claim_id, claim_line_no, recorded_by
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

-- ---------------------------------------------------------------- claim status feed
create or replace function stg.load_claim_events() returns integer language plpgsql as $$
declare n integer;
begin
    drop table if exists _t;
    create temp table _t as
    select r._file, r._row_no, stg.clean(r.event_id) as event_id, upper(stg.clean(r.claim_id)) as claim_id,
           a.status, stg.clean(r.status_text) as status_text_raw, stg.parse_ts(r.status_datetime) as event_ts,
           upper(stg.clean(r.reason_code)) as reason_code, stg.parse_int(r.appeal_level) as appeal_level,
           upper(stg.clean(r.source_system)) as source_system, stg.clean(r.recorded_by) as recorded_by,
           stg.clean(r.note) as note, null::text as _reason, null::text as _detail, null::numeric(14,2) as _amount
    from raw.claim_status_history r
    left join stg.status_alias a on a.alias = upper(btrim(r.status_text))
    where not exists (select 1 from stg.claim_event s where s.src_file = r._file and s.src_row_no = r._row_no)
      and not exists (select 1 from stg.reject j where j.source_table = 'raw.claim_status_history'
                                                   and j.src_file = r._file and j.src_row_no = r._row_no);
    update _t set _reason = case
            when claim_id is null then 'MISSING_CLAIM_ID'
            when status is null then 'UNKNOWN_STATUS'
            when event_ts is null then 'UNPARSEABLE_DATE' end,
                  _detail = case when status is null then 'status_text=' || coalesce(status_text_raw, '<null>') end;
    -- the feed re-sends events under a new event id: same claim, status and timestamp is one event
    with d as (select _file, _row_no, row_number() over (partition by claim_id, status, event_ts order by _file, _row_no) rn
               from _t where _reason is null)
    update _t set _reason = 'DUPLICATE_EVENT', _detail = 'same claim, status and timestamp as an earlier row'
    from d where d._file = _t._file and d._row_no = _t._row_no and d.rn > 1;
    update _t set _reason = 'DUPLICATE_EVENT', _detail = 'event already staged from an earlier load'
    where _reason is null and exists (select 1 from stg.claim_event s where s.claim_id = _t.claim_id
                                      and s.status = _t.status and s.event_ts = _t.event_ts);
    perform stg.write_rejects('raw.claim_status_history');
    insert into stg.claim_event
    select _file, _row_no, event_id, claim_id, status, status_text_raw, event_ts, reason_code, appeal_level,
           source_system, recorded_by, note
    from _t where _reason is null;
    get diagnostics n = row_count;
    return n;
end $$;

create or replace function stg.process_new() returns jsonb language plpgsql as $$
declare out jsonb := '{}'::jsonb;
begin
    out := out || jsonb_build_object('contract_rate', stg.load_contract_rates());
    out := out || jsonb_build_object('claim_line', stg.load_claim_lines());
    out := out || jsonb_build_object('remit_line', stg.load_remit_lines());
    out := out || jsonb_build_object('bank_txn', stg.load_bank_txns());
    out := out || jsonb_build_object('claim_event', stg.load_claim_events());
    out := out || jsonb_build_object('drug_purchase', stg.load_drug_purchases());
    out := out || jsonb_build_object('drug_dispense', stg.load_drug_dispenses());
    drop table if exists _t;
    return out;
end $$;

select stg.process_new();
