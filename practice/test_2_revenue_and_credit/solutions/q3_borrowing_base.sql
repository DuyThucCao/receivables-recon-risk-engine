-- Test 2, Q3. A simple borrowing base on 2025-09-30 (assumptions given in the question):
--   collateral value of a line = contract expected allowed - patient share - paid to date (floor 0)
--   ineligible: denied to date, or older than 120 days since submission. Note: a posted denial
--   settles the line (denied is a component of the identity), so a denied line is no longer
--   "open" in this simple version and drops out at the open-balance filter. The project's
--   credit model instead values workable denials from the claim status feed and excludes them
--   explicitly (models/credit.py).
--   concentration limit 30% of the final eligible base, advance rate 85%
-- Step 1 builds point-in-time lines; step 2 the eligibility waterfall per payer.
with p as (select date '2025-09-30' as d, 120 as max_age, 0.30::numeric as conc, 0.85::numeric as adv),
postings as (
    select m.claim_line_id,
           sum(r.paid_amount)         as paid,
           sum(r.patient_resp_amount) as patient,
           sum(r.denied_amount)       as denied,
           sum(r.paid_amount + r.contractual_amount + r.denied_amount + r.patient_resp_amount) as settled
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    cross join p
    where r.payment_date <= p.d
    group by m.claim_line_id
),
lines as (
    select c.payer_id,
           p.d - c.submission_date as age_days,
           coalesce(x.denied, 0) > 0 as is_denied,
           greatest(b.expected_allowed - coalesce(x.patient, 0) - coalesce(x.paid, 0), 0) as value
    from core.fact_claim_line c
    join recon.claim_line_balance b using (claim_line_id)
    cross join p
    left join postings x using (claim_line_id)
    where c.submission_date <= p.d
      and c.line_charge - coalesce(x.settled, 0) > 0          -- still open on that day
),
by_payer as (
    select payer_id,
           sum(value)                                                     as gross_value,
           count(*) filter (where is_denied)                              as open_lines_with_denial,
           sum(value) filter (where not is_denied and age_days > (select max_age from p)) as inel_aged,
           coalesce(sum(value) filter (where not is_denied and age_days <= (select max_age from p)), 0) as eligible_pre_cap
    from lines
    group by payer_id
)
select payer_id, gross_value, open_lines_with_denial, inel_aged, eligible_pre_cap,
       round(eligible_pre_cap / sum(eligible_pre_cap) over (), 4) as share_pre_cap
from by_payer
order by eligible_pre_cap desc;

-- Step 3: the concentration cap is a fixed point E = sum_p min(e_p, L*E). Iterate it with a
-- recursive CTE (30 rounds is far more than needed), then apply the advance rate.
with recursive p as (select date '2025-09-30' as d, 120 as max_age, 0.30::numeric as conc, 0.85::numeric as adv),
postings as (
    select m.claim_line_id, sum(r.paid_amount) as paid, sum(r.patient_resp_amount) as patient,
           sum(r.denied_amount) as denied,
           sum(r.paid_amount + r.contractual_amount + r.denied_amount + r.patient_resp_amount) as settled
    from recon.claim_remit_match m
    join core.fact_remit_line r using (remit_line_id)
    cross join p
    where r.payment_date <= p.d
    group by m.claim_line_id
),
e as (
    select c.payer_id,
           sum(greatest(b.expected_allowed - coalesce(x.patient, 0) - coalesce(x.paid, 0), 0)) as e_p
    from core.fact_claim_line c
    join recon.claim_line_balance b using (claim_line_id)
    cross join p
    left join postings x using (claim_line_id)
    where c.submission_date <= p.d
      and c.line_charge - coalesce(x.settled, 0) > 0
      and coalesce(x.denied, 0) = 0
      and p.d - c.submission_date <= p.max_age
    group by c.payer_id
),
it (n, base) as (
    select 0, (select sum(e_p) from e)
    union all
    select n + 1, (select sum(least(e_p, (select conc from p) * it.base)) from e)
    from it where n < 30
)
select base                                   as eligible_after_cap,
       (select sum(e_p) from e) - base        as concentration_excess,
       round((select adv from p) * base, 2)   as availability_before_reserves
from it
where n = 30;
