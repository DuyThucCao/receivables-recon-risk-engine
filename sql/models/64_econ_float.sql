-- 64_econ_float.sql  (Question 7b: what does a payer gain by paying late?)
-- Needs mart.econ_param(as_of, shortfall_tolerance, prompt_pay_days) and
-- mart.econ_sofr_month(month, sofr_pct, n_obs), both written by econ.run().
--
-- Value of float = amount x annual rate x days / 365 (simple interest, the payer's opportunity
-- gain from holding the hospital's cash). Rate = average SOFR in the month the payment arrived
-- (observed_public); if a month has no SOFR observation the latest earlier month is used.
--
--   days_to_pay  = first payment date - submission date           (calendar days)
--   days_late    = max(days_to_pay - prompt_pay_days, 0)          (prompt_pay_days: an assumption)
--   float_total  = paid x r x days_to_pay / 365   (all holding time, including normal processing)
--   float_late   = paid x r x days_late / 365     (holding time beyond the prompt-pay window)
-- 'paid' is the line's net payer payment as of the snapshot. Paths: FIRST_PASS (no denial before
-- payment) and PAID_AFTER_DENIAL (paid only after a denial was appealed or corrected).
-- Lines still unpaid at the snapshot are censored; mart.float_value_open shows the float still
-- accruing on open, undenied lines past the window, valued at the contract amount.

drop table if exists mart.float_value_line;
create table mart.float_value_line as
with prm as (
    select as_of, prompt_pay_days from mart.econ_param
), rl as (
    select m.claim_line_id, r.payment_date, r.paid_amount, r.denied_amount, r.is_reversal
    from recon.claim_remit_match m join core.fact_remit_line r using (remit_line_id)
    where r.payment_date <= (select as_of from prm)
      and not r.is_provider_level          -- PLB rows are payer-level, not claim lines
), fp as (
    select claim_line_id,
           min(payment_date) filter (where paid_amount > 0 and not is_reversal) as first_paid_date
    from rl group by claim_line_id
), den as (
    select r.claim_line_id, bool_or(r.denied_amount > 0 and not r.is_reversal) as denied_before_pay
    from rl r join fp using (claim_line_id)
    where r.payment_date <= fp.first_paid_date
    group by r.claim_line_id
), x as (
    select b.claim_line_id, b.claim_id, b.payer_id, b.service_line_code, b.submission_date, fp.first_paid_date,
           b.paid, fp.first_paid_date - b.submission_date as days_to_pay,
           greatest(fp.first_paid_date - b.submission_date - (select prompt_pay_days from prm), 0) as days_late,
           case when coalesce(den.denied_before_pay, false) then 'PAID_AFTER_DENIAL' else 'FIRST_PASS' end as path,
           (select s.sofr_pct from mart.econ_sofr_month s
             where s.month <= date_trunc('month', fp.first_paid_date)::date order by s.month desc limit 1) / 100.0 as rate
    from recon.claim_line_balance b
    join fp using (claim_line_id)
    left join den using (claim_line_id)
    where fp.first_paid_date is not null and b.paid > 0
)
select x.*,
       round(x.paid * x.rate * x.days_to_pay / 365.0, 2) as float_total,
       round(x.paid * x.rate * x.days_late / 365.0, 2)   as float_late,
       (select as_of from prm) as as_of_date
from x;
create index on mart.float_value_line (payer_id);

-- Open, never-denied lines already past the prompt-pay window at the snapshot (censored float).
drop table if exists mart.float_value_open;
create table mart.float_value_open as
with prm as (select as_of, prompt_pay_days from mart.econ_param),
r as (select s.sofr_pct / 100.0 as rate from mart.econ_sofr_month s
      where s.month <= date_trunc('month', (select as_of from prm))::date order by s.month desc limit 1)
select b.payer_id, count(*) as n_lines, sum(b.expected_allowed) as expected_allowed,
       sum(round(b.expected_allowed * (select rate from r)
                 * ((select as_of from prm) - b.submission_date - (select prompt_pay_days from prm)) / 365.0, 2)) as float_accruing,
       (select as_of from prm) as as_of_date
from recon.claim_line_balance b
join recon.claim_line_status s using (claim_line_id)
where s.status in ('SUBMITTED', 'REVERSED') and not s.is_written_off and b.expected_allowed is not null
  and (select as_of from prm) - b.submission_date > (select prompt_pay_days from prm)
group by b.payer_id order by b.payer_id;
