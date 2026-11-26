-- 61_drug_fifo.sql  (Question 6, pharmacy manager)
-- FIFO lot costing, set-based. Reads only:
--   mart.drug_fifo_param(as_of date, ...)
--   mart.drug_fifo_lot_input(lot_key, hcpcs, lot_no, invoice_no, received_date, expiry_date, units, lot_value)
--   mart.drug_fifo_event_input(event_id, hcpcs, event_ts, event_type, lot_no, units, claim_line_id, source)
-- so the same code runs on core data and on the hand-made test example.
--
-- Method
-- 1. Expired-waste events name their lot. An expiry is a physical fact about one lot, so those
--    units are charged to that lot ("specific identification"). A waste event whose lot cannot
--    be found joins the FIFO stream like an administration.
-- 2. Each lot's FIFO capacity = purchased units - its expired units. Lots of a drug are ordered
--    first in, first out: by received date, then lot key. Running sums give every lot a range
--    [cum_start, cum_end) on the drug's cumulative-units axis.
-- 3. Administrations of a drug are ordered by event time (then event id) and get a range on the
--    same axis. Unit k consumed is unit k purchased.
-- 4. Allocation = range-overlap join: an event uses lot L for
--       least(event.cum_end, lot.cum_end) - greatest(event.cum_start, lot.cum_start) units
--    whenever the two ranges overlap. One event can span several lots (a partial lot) and
--    one lot can serve many events. No loops.
-- 5. Dollars to the cent: inside a lot the pieces (allocations in FIFO order, then expired
--    waste, then what is still on hand) get cumulative units q_1 <= ... <= q_n = lot units, and
--       cost_k = round(V * q_k / U, 2) - round(V * q_(k-1) / U, 2)
--    where V is the invoice value and U the lot units. The sum telescopes to exactly V, so
--    purchased = administered + wasted + on hand holds per lot to the unit and to the cent.
--
-- Outputs
--   mart.drug_fifo_piece        every piece (ADMINISTER / WASTE_EXPIRED / ON_HAND) of every lot
--   mart.drug_fifo              allocation of events to lots (pieces other than ON_HAND)
--   mart.drug_fifo_unallocated  event units for which no purchased unit exists (data error)
--   mart.drug_lot_position      one row per lot: purchased = administered + wasted + on hand

drop table if exists mart.drug_fifo_piece cascade;
create table mart.drug_fifo_piece as
with prm as (
    select as_of from mart.drug_fifo_param
), lot as (
    select l.*, row_number() over (partition by l.hcpcs order by l.received_date, l.lot_key) as fifo_seq
    from mart.drug_fifo_lot_input l
    where l.received_date <= (select as_of from prm)
), ev as (
    select e.* from mart.drug_fifo_event_input e
    where e.event_ts::date <= (select as_of from prm) and e.units > 0
), waste_specific as (                       -- step 1
    select e.event_id, e.hcpcs, e.event_ts, e.event_type, e.units, e.claim_line_id, e.source, l.lot_key
    from ev e
    join lot l on l.hcpcs = e.hcpcs and l.lot_no = e.lot_no
    where e.event_type = 'WASTE_EXPIRED'
), lot_range as (                            -- step 2
    select l.*,
           l.units - coalesce(w.u, 0) as fifo_capacity,
           sum(l.units - coalesce(w.u, 0)) over (partition by l.hcpcs order by l.fifo_seq
                                                 rows between unbounded preceding and current row) as cum_end
    from lot l
    left join (select lot_key, sum(units) as u from waste_specific group by lot_key) w using (lot_key)
), stream as (                               -- step 3
    select e.*,
           sum(e.units) over (partition by e.hcpcs order by e.event_ts, e.event_id
                              rows between unbounded preceding and current row) as cum_end
    from ev e
    where not exists (select 1 from waste_specific w where w.event_id = e.event_id)
), alloc as (                                -- step 4
    select s.event_id, s.hcpcs, s.event_ts, s.event_type, s.claim_line_id, s.source, l.lot_key,
           least(s.cum_end, l.cum_end) - greatest(s.cum_end - s.units, l.cum_end - l.fifo_capacity) as qty,
           greatest(s.cum_end - s.units, l.cum_end - l.fifo_capacity) as axis_pos,
           'FIFO'::text as method,
           1 as kind_rank
    from stream s
    join lot_range l on l.hcpcs = s.hcpcs
                    and l.cum_end - l.fifo_capacity < s.cum_end
                    and s.cum_end - s.units < l.cum_end
    union all
    select w.event_id, w.hcpcs, w.event_ts, w.event_type, w.claim_line_id, w.source, w.lot_key,
           w.units, null, 'SPECIFIC_LOT', 2
    from waste_specific w
), onhand as (
    select null::text as event_id, l.hcpcs, null::timestamp as event_ts, 'ON_HAND'::text as event_type,
           null::bigint as claim_line_id, null::text as source, l.lot_key,
           l.units - coalesce(a.q, 0) as qty, null::numeric as axis_pos, 'REMAINDER'::text as method, 3 as kind_rank
    from lot l
    left join (select lot_key, sum(qty) as q from alloc group by lot_key) a using (lot_key)
), pieces as (
    select * from alloc
    union all
    select * from onhand
), cum as (                                  -- step 5
    select p.*, l.lot_no, l.invoice_no, l.received_date, l.expiry_date, l.units as lot_units, l.lot_value,
           sum(p.qty) over (partition by p.lot_key order by p.kind_rank, p.axis_pos, p.event_ts, p.event_id
                            rows between unbounded preceding and current row) as cum_qty
    from pieces p join lot l using (lot_key)
)
select lot_key, hcpcs, lot_no, invoice_no, received_date, expiry_date, lot_units, lot_value,
       event_id, event_type, event_ts, event_ts::date as event_date, claim_line_id, source, method, kind_rank,
       qty,
       (round(lot_value * cum_qty / lot_units, 2) - round(lot_value * (cum_qty - qty) / lot_units, 2))::numeric(14,2) as cost,
       (event_type = 'ADMINISTER' and event_ts::date < received_date) as before_receipt,
       (event_type = 'ADMINISTER' and event_ts::date > expiry_date)   as after_expiry
from cum;

drop table if exists mart.drug_fifo;
create table mart.drug_fifo as
select lot_key, hcpcs, lot_no, received_date, expiry_date, event_id, event_type, event_ts, event_date,
       claim_line_id, source, method, qty, cost, round(cost / nullif(qty, 0), 6) as unit_cost,
       before_receipt, after_expiry
from mart.drug_fifo_piece
where event_type <> 'ON_HAND';

drop table if exists mart.drug_fifo_unallocated;
create table mart.drug_fifo_unallocated as
select e.event_id, e.hcpcs, e.event_ts, e.event_type, e.units,
       e.units - coalesce(a.q, 0) as unallocated_units
from mart.drug_fifo_event_input e
left join (select event_id, sum(qty) as q from mart.drug_fifo group by event_id) a using (event_id)
where e.event_ts::date <= (select as_of from mart.drug_fifo_param) and e.units > 0
  and e.units - coalesce(a.q, 0) <> 0;

drop table if exists mart.drug_lot_position;
create table mart.drug_lot_position as
select p.lot_key, p.hcpcs, p.lot_no, p.invoice_no, p.received_date, p.expiry_date,
       max(p.lot_units)                                            as purchased_units,
       max(p.lot_value)                                            as purchased_value,
       coalesce(sum(p.qty)  filter (where p.event_type = 'ADMINISTER'), 0)    as administered_units,
       coalesce(sum(p.cost) filter (where p.event_type = 'ADMINISTER'), 0)    as administered_cost,
       coalesce(sum(p.qty)  filter (where p.event_type = 'WASTE_EXPIRED'), 0) as wasted_units,
       coalesce(sum(p.cost) filter (where p.event_type = 'WASTE_EXPIRED'), 0) as wasted_cost,
       coalesce(sum(p.qty)  filter (where p.event_type = 'ON_HAND'), 0)       as on_hand_units,
       coalesce(sum(p.cost) filter (where p.event_type = 'ON_HAND'), 0)       as on_hand_value,
       coalesce(sum(p.qty)  filter (where p.before_receipt or p.after_expiry), 0) as infeasible_units,
       (select as_of from mart.drug_fifo_param)                    as as_of_date,
       (max(p.expiry_date) < (select as_of from mart.drug_fifo_param)
        and coalesce(sum(p.qty) filter (where p.event_type = 'ON_HAND'), 0) > 0) as expired_still_on_hand
from mart.drug_fifo_piece p
group by p.lot_key, p.hcpcs, p.lot_no, p.invoice_no, p.received_date, p.expiry_date;

alter table mart.drug_lot_position
    add column units_identity_ok boolean, add column value_identity_ok boolean;
update mart.drug_lot_position
set units_identity_ok = (purchased_units = administered_units + wasted_units + on_hand_units),
    value_identity_ok = (purchased_value = administered_cost + wasted_cost + on_hand_value);
