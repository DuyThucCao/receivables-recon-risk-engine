-- Drill 9 (hard, running FIFO). Allocate every administration of one drug (J9271) to purchase
-- lots first-in-first-out, in pure SQL. Each lot and each administration gets a range on the
-- cumulative-units axis; the overlap of two ranges is the quantity taken from that lot.
-- Simplification for the drill: expired-waste events are ignored (the project charges them to
-- their own lot first; see sql/models/61_drug_fifo.sql).
with lots as (
    select purchase_line_id, lot_no, invoice_date, billing_units, extended_amount,
           sum(billing_units) over (order by invoice_date, purchase_line_id) as cum_end
    from core.fact_drug_purchase
    where hcpcs_code = 'J9271'
),
adm as (
    select dispense_id, event_ts, billing_units,
           sum(billing_units) over (order by event_ts, dispense_id) as cum_end
    from core.fact_drug_dispense
    where hcpcs_code = 'J9271' and event_type = 'ADMINISTER'
),
alloc as (
    select a.dispense_id, a.event_ts, l.lot_no,
           least(a.cum_end, l.cum_end) - greatest(a.cum_end - a.billing_units, l.cum_end - l.billing_units) as units,
           l.extended_amount / l.billing_units as unit_cost
    from adm a
    join lots l
      on a.cum_end - a.billing_units < l.cum_end
     and l.cum_end - l.billing_units < a.cum_end
)
select dispense_id, event_ts, lot_no, units, round(units * unit_cost, 2) as fifo_cost
from alloc
order by event_ts, dispense_id, lot_no
limit 50;

-- Control: every administered unit is allocated (as long as purchases cover administrations).
with lots as (
    select billing_units, sum(billing_units) over (order by invoice_date, purchase_line_id) as cum_end
    from core.fact_drug_purchase where hcpcs_code = 'J9271'
),
adm as (
    select billing_units, sum(billing_units) over (order by event_ts, dispense_id) as cum_end
    from core.fact_drug_dispense where hcpcs_code = 'J9271' and event_type = 'ADMINISTER'
),
alloc as (
    select least(a.cum_end, l.cum_end) - greatest(a.cum_end - a.billing_units, l.cum_end - l.billing_units) as units
    from adm a join lots l
      on a.cum_end - a.billing_units < l.cum_end and l.cum_end - l.billing_units < a.cum_end
)
select (select sum(units) from alloc) = least((select sum(billing_units) from adm), (select sum(billing_units) from lots)) as check_ok,
       (select sum(units) from alloc)         as units_allocated,
       (select sum(billing_units) from adm)   as units_administered,
       (select sum(billing_units) from lots)  as units_purchased;
