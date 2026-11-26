-- 63_econ_underpayment.sql  (Question 7, consulting economist: which payers underpay against contract?)
-- Needs mart.econ_param(as_of, shortfall_tolerance, prompt_pay_days), written by econ.run().
--
-- One row per claim line that the payer priced (some remittance line with allowed > 0) and that
-- has a contract rate. Amounts compared in ALLOWED terms, so patient responsibility (deductible,
-- coinsurance), which the contract counts as part of the allowed amount, is never mistaken for
-- underpayment. Denied lines are not here: a denial is a different decision (question 2).
--
--   expected_allowed      contract amount (recon.claim_line_balance, lesser of contract and charge)
--   first_allowed         allowed amount on the payer's first pricing of the line
--   net_allowed           allowed amount after every later reversal, repost and adjustment
--   pricing_shortfall     expected - first_allowed when that exceeds the tolerance: priced below
--                         contract at adjudication, with no adjustment reason that explains it
--   post_payment_reduction / increase   first_allowed - net_allowed (repricing on repost, late
--                         downward adjustments, unrecouped duplicate payments)
--   pending_partial       on PARTIAL lines, allowed - patient responsibility - paid: what the payer
--                         has allowed but not yet sent after a partial payment (timing, not price)
--
-- Classification (gap_reason) of each line, first match wins:
--   PRICED_BELOW_CONTRACT   pricing_shortfall > 0
--   REDUCED_AFTER_PAYMENT   post_payment_reduction > tolerance
--   PARTIAL_PENDING         pending_partial > tolerance (remainder may still come)
--   OVERPAID                net_allowed above contract by more than the tolerance
--   AT_CONTRACT             everything else (any paid shortfall is patient responsibility)

drop table if exists mart.underpayment_line;
create table mart.underpayment_line as
with prm as (
    select as_of, shortfall_tolerance from mart.econ_param
), rl as (
    select m.claim_line_id, r.remit_line_id, r.payment_date, r.is_reversal, r.allowed_amount, r.paid_amount,
           r.denied_amount
    from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id)
    where r.payment_date <= (select as_of from prm)
      and not r.is_provider_level          -- PLB rows are payer-level, not claim lines
), first_price as (
    select distinct on (claim_line_id) claim_line_id, allowed_amount as first_allowed, payment_date as first_priced_date
    from rl where not is_reversal and allowed_amount > 0
    order by claim_line_id, payment_date, remit_line_id
), flags as (
    select claim_line_id,
           bool_or(is_reversal)                                   as had_reversal,
           bool_or(not is_reversal and allowed_amount < 0)        as had_downward_adjustment,
           count(*) filter (where not is_reversal and allowed_amount > 0) as n_pricings,
           bool_or(denied_amount > 0 and not is_reversal)         as had_denial,
           min(payment_date) filter (where paid_amount > 0 and not is_reversal) as first_paid_date
    from rl group by claim_line_id
), x as (
    select b.claim_line_id, b.claim_id, b.line_no, b.payer_id, b.procedure_code, b.service_line_code,
           b.service_date, b.submission_date, b.billed, b.expected_allowed, fp.first_allowed,
           b.payer_allowed as net_allowed, b.paid, b.patient_resp, b.open_balance, s.status,
           f.had_reversal, f.had_downward_adjustment, f.n_pricings, f.had_denial, fp.first_priced_date, f.first_paid_date,
           case when b.expected_allowed - fp.first_allowed >= (select shortfall_tolerance from prm)
                then b.expected_allowed - fp.first_allowed else 0 end                   as pricing_shortfall,
           greatest(fp.first_allowed - b.payer_allowed, 0)                              as post_payment_reduction,
           greatest(b.payer_allowed - fp.first_allowed, 0)                              as post_payment_increase,
           -- allowed by the payer but not yet paid (allowed terms, so a stray reversal that leaves
           -- open_balance = billed on a fully paid line does not count as pending)
           case when s.status = 'PARTIAL' then greatest(b.payer_allowed - b.patient_resp - b.paid, 0) else 0 end
                                                                                        as pending_partial
    from recon.claim_line_balance b
    join recon.claim_line_status s using (claim_line_id)
    join first_price fp using (claim_line_id)
    join flags f using (claim_line_id)
    where b.expected_allowed is not null and b.expected_allowed > 0
      and b.service_date <= (select as_of from prm)
)
select x.*,
       round(x.pricing_shortfall / x.expected_allowed, 6) as shortfall_fraction,
       case when x.pricing_shortfall > 0 then 'PRICED_BELOW_CONTRACT'
            when x.post_payment_reduction > (select shortfall_tolerance from prm) then 'REDUCED_AFTER_PAYMENT'
            when x.pending_partial > (select shortfall_tolerance from prm) then 'PARTIAL_PENDING'
            when x.net_allowed - x.expected_allowed > (select shortfall_tolerance from prm) then 'OVERPAID'
            else 'AT_CONTRACT' end as gap_reason,
       (select as_of from prm) as as_of_date
from x;
create index on mart.underpayment_line (payer_id, claim_id);

-- One row per claim: the unit a payer prices and the hospital would appeal.
drop table if exists mart.underpayment_claim;
create table mart.underpayment_claim as
select payer_id, claim_id, min(service_date) as service_date, min(submission_date) as submission_date,
       min(first_priced_date) as first_priced_date,
       count(*) as n_lines, sum(expected_allowed) as expected_allowed, sum(first_allowed) as first_allowed,
       sum(net_allowed) as net_allowed, sum(paid) as paid, sum(patient_resp) as patient_resp,
       sum(pricing_shortfall) as pricing_shortfall,
       round(sum(pricing_shortfall) / nullif(sum(expected_allowed), 0), 6) as shortfall_fraction,
       sum(post_payment_reduction) as post_payment_reduction, sum(post_payment_increase) as post_payment_increase,
       sum(pending_partial) as pending_partial,
       bool_or(pricing_shortfall > 0) as is_underpaid
from mart.underpayment_line
group by payer_id, claim_id;
create index on mart.underpayment_claim (payer_id);
