-- 60_drug_inputs.sql  (Question 6, pharmacy manager)
-- Builds the two input tables that the FIFO engine (61_drug_fifo.sql) reads, from core.
-- Kept separate so the engine can be tested on a hand-made example with the same code.
--
--   mart.drug_fifo_param        one row: as_of (snapshot date), mature_cutoff (service dates with
--                               at least 180 days of payment run-out). Created by drug.run().
--   mart.drug_fifo_lot_input    one row per purchase lot (invoice line): units, value, dates
--   mart.drug_fifo_event_input  one row per administration or waste event
--
-- Work-around (see docs/requests.md): dispense rows whose billing_units could not be parsed
-- are rejected by staging and are missing from core.fact_drug_dispense. When the rejected row
-- names a claim line, its units are imputed as (claim line units - units of the other dispense
-- rows for that claim line), and the row is flagged source = 'IMPUTED_FROM_CLAIM_LINE'.

drop table if exists mart.drug_fifo_lot_input;
create table mart.drug_fifo_lot_input as
select p.purchase_line_id            as lot_key,
       p.hcpcs_code                  as hcpcs,
       p.lot_no,
       p.invoice_no,
       p.invoice_date                as received_date,
       p.expiry_date,
       p.billing_units               as units,
       p.extended_amount             as lot_value          -- dollars, what the invoice says we paid
from core.fact_drug_purchase p
where p.invoice_date <= (select as_of from mart.drug_fifo_param);

drop table if exists mart.drug_fifo_event_input;
create table mart.drug_fifo_event_input as
select d.dispense_id   as event_id,
       d.hcpcs_code    as hcpcs,
       d.event_ts,
       d.event_type,
       d.lot_no,
       d.billing_units as units,
       d.claim_line_id,
       'CORE'::text    as source
from core.fact_drug_dispense d
where d.event_date <= (select as_of from mart.drug_fifo_param)
union all
select x.event_id, x.hcpcs, x.event_ts, x.event_type, x.lot_no,
       c.units - coalesce((select sum(o.billing_units) from core.fact_drug_dispense o
                           where o.claim_line_id = c.claim_line_id and o.event_type = 'ADMINISTER'), 0),
       c.claim_line_id, 'IMPUTED_FROM_CLAIM_LINE'
from (
    select j.raw_record ->> 'dispense_id'                 as event_id,
           j.raw_record ->> 'hcpcs_code'                  as hcpcs,
           (j.raw_record ->> 'event_datetime')::timestamp as event_ts,
           j.raw_record ->> 'event_type'                  as event_type,
           nullif(j.raw_record ->> 'lot_no', '')          as lot_no,
           j.raw_record ->> 'claim_id'                    as claim_id,
           nullif(j.raw_record ->> 'claim_line_no', '')::integer as claim_line_no
    from stg.reject j
    where j.source_table = 'raw.drug_dispense_log' and j.reason_code = 'UNPARSEABLE_UNITS'
) x
join core.fact_claim_line c on c.claim_id = x.claim_id and c.line_no = x.claim_line_no
where x.event_type = 'ADMINISTER'
  and x.event_ts::date <= (select as_of from mart.drug_fifo_param)
  and not exists (select 1 from core.fact_drug_dispense d where d.dispense_id = x.event_id);

delete from mart.drug_fifo_event_input where source = 'IMPUTED_FROM_CLAIM_LINE' and units <= 0;
