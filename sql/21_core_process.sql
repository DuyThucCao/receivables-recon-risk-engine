-- 21_core_process.sql
-- core.process_new(): move staged rows not yet in core into the per-process model.
-- Incremental (keyed on src_file, src_row_no); the full build calls it once.

create or replace function core.process_new() returns jsonb language plpgsql as $$
declare out jsonb := '{}'::jsonb; n integer;
begin
    -- payers: canonical name from the contract file (latest row per payer), else from claims
    insert into core.dim_payer (payer_id, payer_name, payer_type, payer_tax_id, ach_company_name)
    select distinct on (payer_id) payer_id, payer_name, payer_type, payer_tax_id, ach_company_name
    from stg.contract_rate order by payer_id, effective_from desc, src_row_no desc
    on conflict (payer_id) do update set payer_name = excluded.payer_name, payer_type = excluded.payer_type,
        payer_tax_id = excluded.payer_tax_id, ach_company_name = excluded.ach_company_name;
    insert into core.dim_payer (payer_id, payer_name)
    select distinct on (payer_id) payer_id, coalesce(payer_name, payer_id) from stg.claim_line
    order by payer_id, src_row_no on conflict do nothing;
    insert into core.dim_payer (payer_id, payer_name)
    select distinct on (payer_id) payer_id, coalesce(payer_name, payer_id) from stg.remit_line
    order by payer_id, src_row_no on conflict do nothing;

    insert into core.dim_contract_rate (contract_id, payer_id, procedure_scope, procedure_code, rate_type, rate_value,
                                        unit_basis, effective_from, effective_to, lesser_of_charges, src_file, src_row_no)
    select contract_id, payer_id, procedure_scope, procedure_code, rate_type, rate_value, unit_basis, effective_from,
           effective_to, coalesce(lesser_of_charges, true), src_file, src_row_no
    from stg.contract_rate s
    where not exists (select 1 from core.dim_contract_rate c where c.src_file = s.src_file and c.src_row_no = s.src_row_no)
    order by src_file, src_row_no
    on conflict do nothing;
    get diagnostics n = row_count; out := out || jsonb_build_object('dim_contract_rate', n);

    insert into core.dim_service_line (service_line_code, service_line_name)
    select distinct service_line, initcap(replace(service_line, '_', ' ')) from stg.claim_line
    where service_line is not null on conflict do nothing;

    insert into core.dim_procedure (procedure_code, service_line_code, revenue_code, is_drug)
    select procedure_code, mode() within group (order by service_line), mode() within group (order by revenue_code),
           procedure_code ~ '^J[0-9]{4}$'
    from stg.claim_line group by procedure_code
    on conflict do nothing;

    -- claim lines
    insert into core.fact_claim_line (claim_id, claim_id_norm, line_no, payer_id, patient_account_no, member_id_norm,
        service_line_code, facility_name, form_type, procedure_code, modifier_1, revenue_code, ndc_code,
        diagnosis_code_1, diagnosis_code_2, units, line_charge, service_date, submission_date, claim_total_charge,
        line_item_control_no, lic_norm, src_file, src_row_no)
    select claim_id, claim_id_norm, line_no, payer_id, patient_account_no, member_id_norm, service_line, facility_name,
           form_type, procedure_code, modifier_1, revenue_code, ndc_code, diagnosis_code_1, diagnosis_code_2, units,
           line_charge, service_date, submission_date, claim_total_charge, line_item_control_no, lic_norm, src_file, src_row_no
    from stg.claim_line s
    where not exists (select 1 from core.fact_claim_line c where c.src_file = s.src_file and c.src_row_no = s.src_row_no)
    order by src_file, src_row_no;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_claim_line', n);

    -- remittance lines + adjustments (long format), buckets from dim_reason_code
    drop table if exists _new_remit;
    create temp table _new_remit as
    select s.* from stg.remit_line s
    where not exists (select 1 from core.fact_remit_line c where c.src_file = s.src_file and c.src_row_no = s.src_row_no);

    drop table if exists _adj;
    create temp table _adj as
    select a.src_file, a.src_row_no, a.adj_seq, a.group_code, a.reason_code, a.amount,
           coalesce(rc.bucket, case when a.group_code = 'PR' then 'PATIENT_RESP' else 'DENIAL' end) as bucket,
           rc.category, rc.bucket is null as unknown
    from (
        select src_file, src_row_no, 1 as adj_seq, adj_group_1 as group_code, adj_reason_1 as reason_code, adj_amount_1 as amount from _new_remit
        union all select src_file, src_row_no, 2, adj_group_2, adj_reason_2, adj_amount_2 from _new_remit
        union all select src_file, src_row_no, 3, adj_group_3, adj_reason_3, adj_amount_3 from _new_remit
    ) a
    left join core.dim_reason_code rc on rc.group_code = a.group_code and rc.reason_code = a.reason_code
    where a.group_code is not null and a.amount is not null;

    insert into core.fact_remit_line (src_file, src_row_no, remit_file_id, payer_id, trace_no, trace_key, payment_method,
        payment_date, file_payment_amount, patient_control_no_raw, patient_control_no_norm, payer_claim_control_no,
        claim_status_code, is_reversal, is_provider_level, member_id_norm, service_date, procedure_code, units, line_item_control_no, lic_norm,
        line_charge, allowed_amount, paid_amount, contractual_amount, denied_amount, patient_resp_amount,
        denial_group_code, denial_reason_code, denial_category, has_unknown_reason, line_hash)
    select s.src_file, s.src_row_no, s.remit_file_id, s.payer_id, s.trace_no, s.payer_id || ':' || s.trace_no,
           s.payment_method, s.payment_date, s.file_payment_amount, s.patient_control_no_raw, s.patient_control_no_norm,
           s.payer_claim_control_no, s.claim_status_code, coalesce(s.claim_status_code = '22', false),
           coalesce(s.claim_status_code = 'PLB', false), s.member_id_norm,
           s.service_date, s.procedure_code, s.units, s.line_item_control_no, s.lic_norm, s.line_charge,
           s.allowed_amount, s.paid_amount,
           coalesce(a.contractual, 0), coalesce(a.denied, 0), coalesce(a.patient_resp, 0),
           a.denial_group, a.denial_reason, a.denial_category, coalesce(a.unknown, false), s.line_hash
    from _new_remit s
    left join (
        select src_file, src_row_no,
               sum(amount) filter (where bucket = 'CONTRACTUAL') as contractual,
               sum(amount) filter (where bucket = 'DENIAL') as denied,
               sum(amount) filter (where bucket = 'PATIENT_RESP') as patient_resp,
               min(group_code) filter (where bucket = 'DENIAL') as denial_group,
               min(reason_code) filter (where bucket = 'DENIAL') as denial_reason,
               min(category) filter (where bucket = 'DENIAL') as denial_category,
               bool_or(unknown) as unknown
        from _adj group by src_file, src_row_no) a on a.src_file = s.src_file and a.src_row_no = s.src_row_no
    order by s.src_file, s.src_row_no;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_remit_line', n);

    insert into core.fact_remit_adjustment (remit_line_id, adj_seq, group_code, reason_code, amount, bucket)
    select r.remit_line_id, a.adj_seq, a.group_code, a.reason_code, a.amount, a.bucket
    from _adj a join core.fact_remit_line r on r.src_file = a.src_file and r.src_row_no = a.src_row_no;

    -- bank transactions, categorised; payer recognised from the ACH company name
    insert into core.fact_bank_txn (src_file, src_row_no, account_number, posting_date, value_date, transaction_type,
        description, amount, running_balance, bank_reference, customer_reference, txn_category, trace_no_extracted,
        payer_id_inferred, lockbox_item_count)
    select s.src_file, s.src_row_no, s.account_number, s.posting_date, s.value_date, s.transaction_type, s.description,
           s.amount, s.running_balance, s.bank_reference, s.customer_reference,
           case when s.description ~* 'LOCKBOX' then 'LOCKBOX'
                when s.description ~* 'HCCLAIMPMT' and s.amount > 0 then 'PAYER_EFT'
                when s.description ~* 'CARD' then 'CARD_SETTLEMENT'
                when s.description ~* 'INTEREST' then 'INTEREST'
                when s.description ~* 'FEE' then 'FEE'
                when s.description ~* 'TRANSFER' then 'TRANSFER'
                when s.transaction_type = 'ACH DEBIT' then 'VENDOR_PAYMENT'
                when s.amount >= 0 then 'OTHER_CREDIT' else 'OTHER_DEBIT' end,
           s.trace_no_extracted,
           (select p.payer_id from core.dim_payer p
             where p.ach_company_name is not null and position(p.ach_company_name in upper(s.description)) > 0
             order by length(p.ach_company_name) desc limit 1),
           s.lockbox_item_count
    from stg.bank_txn s
    where not exists (select 1 from core.fact_bank_txn c where c.src_file = s.src_file and c.src_row_no = s.src_row_no)
    order by s.src_file, s.src_row_no;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_bank_txn', n);

    -- claim events
    insert into core.fact_claim_event (src_file, src_row_no, event_id, claim_id, status, event_ts, event_date, reason_code,
        reason_category, appeal_level, source_system, recorded_by, note, claim_known)
    select s.src_file, s.src_row_no, s.event_id, s.claim_id, s.status, s.event_ts, s.event_ts::date, s.reason_code,
           rc.category, s.appeal_level, s.source_system, s.recorded_by, s.note,
           exists (select 1 from core.fact_claim_line c where c.claim_id = s.claim_id)
    from stg.claim_event s
    left join core.dim_reason_code rc on rc.group_code || '-' || rc.reason_code = s.reason_code
    where not exists (select 1 from core.fact_claim_event c where c.src_file = s.src_file and c.src_row_no = s.src_row_no)
    order by s.event_ts, s.src_file, s.src_row_no;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_claim_event', n);
    update core.fact_claim_event e set claim_known = true
    where not e.claim_known and exists (select 1 from core.fact_claim_line c where c.claim_id = e.claim_id);

    -- drugs: dimension from the dispense log (HCPCS + NDC) and invoices (package size)
    insert into core.dim_drug (hcpcs_code, drug_description, ndc_code)
    select distinct on (hcpcs_code) hcpcs_code, drug_description, ndc_code
    from stg.drug_dispense where hcpcs_code is not null order by hcpcs_code, src_row_no
    on conflict do nothing;
    update core.dim_drug d set billing_units_per_package = p.bupp
    from (select ndc_code, max(billing_units_per_package) bupp from stg.drug_purchase group by ndc_code) p
    where p.ndc_code = d.ndc_code and d.billing_units_per_package is distinct from p.bupp;

    insert into core.fact_drug_purchase (src_file, src_row_no, invoice_no, invoice_line_no, invoice_date, due_date,
        wholesaler_id, wholesaler_name, order_type, ndc_code, hcpcs_code, product_description, lot_no, expiry_date,
        qty_packages, billing_units_per_package, billing_units, package_price, extended_amount, unit_cost)
    select s.src_file, s.src_row_no, s.invoice_no, s.invoice_line_no, s.invoice_date, s.due_date, s.wholesaler_id,
           s.wholesaler_name, s.order_type, s.ndc_code, d.hcpcs_code, s.product_description, s.lot_no, s.expiry_date,
           s.qty_packages, s.billing_units_per_package, s.qty_packages * s.billing_units_per_package, s.unit_price,
           s.extended_amount, round(s.unit_price / s.billing_units_per_package, 6)
    from stg.drug_purchase s left join core.dim_drug d on d.ndc_code = s.ndc_code
    where not exists (select 1 from core.fact_drug_purchase c where c.src_file = s.src_file and c.src_row_no = s.src_row_no)
    order by s.src_file, s.src_row_no;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_drug_purchase', n);
    update core.fact_drug_purchase p set hcpcs_code = d.hcpcs_code
    from core.dim_drug d where p.hcpcs_code is null and d.ndc_code = p.ndc_code;

    insert into core.fact_drug_dispense (dispense_id, src_file, src_row_no, event_ts, event_date, event_type, facility_name,
        ndc_code, hcpcs_code, lot_no, billing_units, patient_account_no, claim_id, claim_line_no, claim_line_id, recorded_by)
    select s.dispense_id, s.src_file, s.src_row_no, s.event_ts, s.event_ts::date, s.event_type, s.facility_name,
           s.ndc_code, s.hcpcs_code, s.lot_no, s.billing_units, s.patient_account_no, s.claim_id, s.claim_line_no,
           c.claim_line_id, s.recorded_by
    from stg.drug_dispense s
    left join core.fact_claim_line c on c.claim_id = s.claim_id and c.line_no = s.claim_line_no
    where not exists (select 1 from core.fact_drug_dispense x where x.src_file = s.src_file and x.src_row_no = s.src_row_no)
    order by s.src_file, s.src_row_no
    on conflict (dispense_id) do nothing;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_drug_dispense', n);

    -- administrations whose units were unreadable: the raw row stays rejected (and counted), but
    -- the event is carried into core with units imputed from the billed claim line minus the units
    -- of the other dispense rows for that line, flagged units_imputed. Without a claim reference
    -- (e.g. expiry waste) nothing can be imputed and the event stays only in stg.reject.
    insert into core.fact_drug_dispense (dispense_id, src_file, src_row_no, event_ts, event_date, event_type, facility_name,
        ndc_code, hcpcs_code, lot_no, billing_units, patient_account_no, claim_id, claim_line_no, claim_line_id, recorded_by,
        units_imputed)
    select x.dispense_id, j.src_file, j.src_row_no, x.event_ts, x.event_ts::date, x.event_type, x.facility_name, x.ndc_code,
           x.hcpcs_code, x.lot_no,
           c.units - coalesce((select sum(d.billing_units) from core.fact_drug_dispense d
                               where d.claim_line_id = c.claim_line_id and d.event_type = 'ADMINISTER'), 0),
           x.patient_account_no, x.claim_id, x.claim_line_no, c.claim_line_id, x.recorded_by, true
    from stg.reject j
    cross join lateral (select stg.clean(j.raw_record->>'dispense_id') as dispense_id,
                               stg.parse_ts(j.raw_record->>'event_datetime') as event_ts,
                               upper(stg.clean(j.raw_record->>'event_type')) as event_type,
                               stg.clean(j.raw_record->>'facility_name') as facility_name,
                               stg.clean(j.raw_record->>'ndc_code') as ndc_code,
                               upper(stg.clean(j.raw_record->>'hcpcs_code')) as hcpcs_code,
                               upper(stg.clean(j.raw_record->>'lot_no')) as lot_no,
                               stg.clean(j.raw_record->>'patient_account_no') as patient_account_no,
                               upper(stg.clean(j.raw_record->>'claim_id')) as claim_id,
                               stg.parse_int(j.raw_record->>'claim_line_no') as claim_line_no,
                               stg.clean(j.raw_record->>'recorded_by') as recorded_by) x
    join core.fact_claim_line c on c.claim_id = x.claim_id and c.line_no = x.claim_line_no
    where j.source_table = 'raw.drug_dispense_log' and j.reason_code = 'UNPARSEABLE_UNITS'
      and x.event_type = 'ADMINISTER' and x.event_ts is not null
      and not exists (select 1 from core.fact_drug_dispense d where d.src_file = j.src_file and d.src_row_no = j.src_row_no)
      and c.units - coalesce((select sum(d.billing_units) from core.fact_drug_dispense d
                              where d.claim_line_id = c.claim_line_id and d.event_type = 'ADMINISTER'), 0) > 0
    on conflict (dispense_id) do nothing;
    get diagnostics n = row_count; out := out || jsonb_build_object('fact_drug_dispense_units_imputed', n);

    drop table if exists _new_remit;
    drop table if exists _adj;
    analyze core.fact_claim_line; analyze core.fact_remit_line; analyze core.fact_bank_txn;
    return out;
end $$;

-- helper view: claim header (one row per claim)
create or replace view core.v_claim as
select claim_id, min(payer_id) as payer_id, min(service_line_code) as service_line_code, min(service_date) as service_date,
       min(submission_date) as submission_date, min(patient_account_no) as patient_account_no,
       count(*) as n_lines, sum(line_charge) as total_charge
from core.fact_claim_line group by claim_id;

select core.process_new();
