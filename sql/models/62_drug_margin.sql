-- 62_drug_margin.sql  (Question 6, pharmacy manager: which drugs lose money after reimbursement?)
-- Needs: 61_drug_fifo.sql outputs, mart.drug_fifo_param, mart.drug_reference_price (CMS Part B
-- payment limits, loaded by drug.run() from config/public_calibration.yaml), core and recon.
--
-- Two cohorts:
--   ALL     service / event dates up to the as-of date (recent claims still open)
--   MATURE  service / event dates up to mature_cutoff = as_of - 180 days (payment mostly settled)
--
-- Per drug (HCPCS code) and cohort, with E = contract expected allowed, P = payer paid (reconciled,
-- net of reversals and recoupments), R = patient responsibility, C = FIFO cost of administered
-- units, W = FIFO cost of expired units:
--   margin_collected = P - C - W
--   and the additive decomposition
--   margin_collected = (E - C) - W - R - denied_leak - pending_leak - pricing_gap
--   where for each claim line gap = E - P - R is put in one bucket by line status:
--   DENIED -> denied_leak; SUBMITTED / REVERSED / PARTIAL -> pending_leak; any other status (PAID,
--   CREDIT_BALANCE, ORPHAN_REVERSAL) -> pricing_gap (payer priced below (+) or above (-) contract, including later adjustments).
-- Break-even final-denial rate. d = D / E is the dollar-weighted final-denial rate (share of
-- contract value on lines whose final status is DENIED). Non-denied contract dollars are
-- collected at the observed ratio k = P / (E - D) (patient share, pending and pricing gaps stay as
-- observed), so P(d) = k E (1 - d) and margin(d) = k E (1 - d) - C - W. Setting this to zero:
--   breakeven_denial_share  d* = 1 - (C + W) / (k E).
-- d* below the observed d means the drug loses money at the simulated denial rate; d* < 0 means
-- it loses money even with no denials.
-- The ASP payment limit is a Medicare reference price per billing unit, shown for context only:
-- it is not the hospital's acquisition cost and not what these (fictional) payers pay.

drop table if exists mart.drug_margin;
create table mart.drug_margin as
with prm as (
    select as_of, mature_cutoff from mart.drug_fifo_param
), cohorts as (
    select 'ALL'::text as cohort, as_of as cutoff from prm
    union all
    select 'MATURE', mature_cutoff from prm
), first_paid as (
    select m.claim_line_id, min(r.payment_date) as first_paid_date
    from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id)
    where r.paid_amount > 0 and not r.is_reversal
      and not r.is_provider_level          -- PLB rows are payer-level, not claim lines
    group by m.claim_line_id
), lines as (
    select k.cohort, c.procedure_code as hcpcs, c.claim_line_id, c.units, b.billed,
           b.expected_allowed as e, b.paid as p, b.patient_resp as r, s.status,
           b.expected_allowed - b.paid - b.patient_resp as gap,
           fp.first_paid_date - c.service_date as days_to_pay
    from cohorts k
    join core.fact_claim_line c on c.service_date <= k.cutoff
    join core.dim_drug d on d.hcpcs_code = c.procedure_code
    join recon.claim_line_balance b on b.claim_line_id = c.claim_line_id
    join recon.claim_line_status s on s.claim_line_id = c.claim_line_id
    left join first_paid fp on fp.claim_line_id = c.claim_line_id
), claim_side as (
    select cohort, hcpcs, count(*) as n_claim_lines, sum(units) as units_billed, sum(billed) as billed,
           sum(e) as expected_allowed, sum(p) as payer_paid, sum(r) as patient_resp,
           coalesce(sum(gap) filter (where status = 'DENIED'), 0) as denied_leak,
           coalesce(sum(gap) filter (where status in ('SUBMITTED', 'REVERSED', 'PARTIAL')), 0) as pending_leak,
           -- every other status (PAID, CREDIT_BALANCE, ORPHAN_REVERSAL, ...) was priced: pricing gap
           coalesce(sum(gap) filter (where status not in ('DENIED', 'SUBMITTED', 'REVERSED', 'PARTIAL')), 0) as pricing_gap,
           avg((status = 'DENIED')::int)::numeric(8,4) as denied_line_share,
           percentile_cont(0.5) within group (order by days_to_pay) as median_days_to_pay
    from lines group by cohort, hcpcs
), cost_side as (
    select k.cohort, f.hcpcs,
           coalesce(sum(f.qty)  filter (where f.event_type = 'ADMINISTER'), 0) as units_administered,
           coalesce(sum(f.cost) filter (where f.event_type = 'ADMINISTER'), 0) as fifo_cogs,
           coalesce(sum(f.qty)  filter (where f.event_type = 'WASTE_EXPIRED'), 0) as waste_units,
           coalesce(sum(f.cost) filter (where f.event_type = 'WASTE_EXPIRED'), 0) as waste_cost
    from cohorts k join mart.drug_fifo f on f.event_date <= k.cutoff
    group by k.cohort, f.hcpcs
), lot_side as (
    select hcpcs, sum(purchased_units) as purchased_units, sum(purchased_value) as purchased_value,
           avg(expiry_date - received_date)::numeric(8,1) as avg_shelf_life_days,
           sum(wasted_units) / nullif(sum(purchased_units), 0) as waste_share_of_purchases,
           sum(on_hand_units) as on_hand_units, sum(on_hand_value) as on_hand_value
    from mart.drug_lot_position group by hcpcs
), base as (
    select cs.cohort, cs.hcpcs, ref.short_description, ref.dosage, ref.payment_limit_usd as asp_payment_limit,
           cs.n_claim_lines, cs.units_billed, co.units_administered, cs.billed, cs.expected_allowed,
           cs.payer_paid, cs.patient_resp, cs.denied_leak, cs.pending_leak, cs.pricing_gap,
           co.fifo_cogs, co.waste_units, co.waste_cost,
           cs.denied_line_share, cs.median_days_to_pay,
           ls.avg_shelf_life_days, ls.waste_share_of_purchases, ls.on_hand_units, ls.on_hand_value
    from claim_side cs
    left join cost_side co on co.cohort = cs.cohort and co.hcpcs = cs.hcpcs
    left join lot_side ls on ls.hcpcs = cs.hcpcs
    left join mart.drug_reference_price ref on ref.hcpcs = cs.hcpcs
)
select b.*,
       (b.payer_paid - b.fifo_cogs - b.waste_cost)::numeric(14,2)                   as margin_collected,
       (b.payer_paid + b.patient_resp - b.fifo_cogs - b.waste_cost)::numeric(14,2)  as margin_if_patient_pays,
       (b.expected_allowed - b.fifo_cogs)::numeric(14,2)                            as price_gap,
       round(b.fifo_cogs / nullif(b.units_administered, 0), 4)                      as fifo_cost_per_unit,
       round(b.expected_allowed / nullif(b.units_billed, 0), 4)                     as contract_per_unit,
       round(b.payer_paid / nullif(b.units_billed, 0), 4)                           as collected_per_unit,
       round(b.waste_cost / nullif(b.units_administered, 0), 4)                     as waste_per_unit,
       round((b.payer_paid - b.fifo_cogs - b.waste_cost) / nullif(b.units_administered, 0), 4) as margin_per_unit,
       round(b.fifo_cogs / nullif(b.units_administered, 0) / nullif(b.asp_payment_limit, 0), 4) as cost_to_asp,
       round(b.payer_paid / nullif(b.units_billed, 0) / nullif(b.asp_payment_limit, 0), 4)    as collected_to_asp,
       round(b.expected_allowed / nullif(b.units_billed, 0) / nullif(b.asp_payment_limit, 0), 4) as contract_to_asp,
       -- break-even final-denial rate (see header): d* = 1 - (C + W) / (k E), k = P / (E - D)
       round(b.denied_leak / nullif(b.expected_allowed, 0), 4)                          as denied_dollar_share,
       round(b.payer_paid / nullif(b.expected_allowed - b.denied_leak, 0), 4)           as collection_ratio_non_denied,
       round(1 - (b.fifo_cogs + b.waste_cost)
                 / nullif(b.payer_paid / nullif(b.expected_allowed - b.denied_leak, 0) * b.expected_allowed, 0), 4)
                                                                                         as breakeven_denial_share,
       drv.primary_driver, drv.primary_driver_amount, drv.secondary_driver, drv.secondary_driver_amount,
       (select as_of from prm) as as_of_date
from base b
cross join lateral (
    select max(name) filter (where rk = 1) as primary_driver,  max(amt) filter (where rk = 1) as primary_driver_amount,
           max(name) filter (where rk = 2) as secondary_driver, max(amt) filter (where rk = 2) as secondary_driver_amount
    from (
        select name, amt, row_number() over (order by amt) as rk
        from (values ('acquisition cost above contract rate', b.expected_allowed - b.fifo_cogs),
                     ('expired waste',                        -b.waste_cost),
                     ('denied claims',                        -b.denied_leak),
                     ('slow or pending payment',              -b.pending_leak),
                     ('patient share not yet collected',      -b.patient_resp),
                     ('priced below contract',                -b.pricing_gap)) v(name, amt)
        where amt < 0
    ) z
) drv
order by b.cohort, (b.payer_paid - b.fifo_cogs - b.waste_cost);

-- Contract rate per unit by payer against the drug's FIFO average cost per unit (MATURE cohort).
drop table if exists mart.drug_margin_payer;
create table mart.drug_margin_payer as
with prm as (select mature_cutoff from mart.drug_fifo_param),
cost as (
    select hcpcs, fifo_cost_per_unit from mart.drug_margin where cohort = 'MATURE'
)
select c.procedure_code as hcpcs, c.payer_id, count(*) as n_claim_lines, sum(c.units) as units_billed,
       sum(b.expected_allowed) as expected_allowed, sum(b.paid) as payer_paid,
       round(sum(b.expected_allowed) / nullif(sum(c.units), 0), 4) as contract_per_unit,
       round(sum(b.paid) / nullif(sum(c.units), 0), 4)             as collected_per_unit,
       k.fifo_cost_per_unit,
       round(sum(b.expected_allowed) / nullif(sum(c.units), 0) - k.fifo_cost_per_unit, 4) as contract_minus_cost_per_unit,
       avg((s.status = 'DENIED')::int)::numeric(8,4) as denied_line_share
from core.fact_claim_line c
join core.dim_drug d on d.hcpcs_code = c.procedure_code
join recon.claim_line_balance b on b.claim_line_id = c.claim_line_id
join recon.claim_line_status s on s.claim_line_id = c.claim_line_id
left join cost k on k.hcpcs = c.procedure_code
where c.service_date <= (select mature_cutoff from prm)
group by c.procedure_code, c.payer_id, k.fifo_cost_per_unit
order by 1, 2;
