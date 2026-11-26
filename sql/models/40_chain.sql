-- 40_chain.sql  (owner: chain, cash and credit; executed by recon_engine.models.markov.run)
-- Point-in-time views of the claim life cycle for the absorbing Markov chain (Q3, Q4, Q5).
--
-- No look-ahead by construction: every function takes a cutoff date and reads only
--   claims with submission_date <= cutoff, life-cycle events with event_date <= cutoff and
--   remittance postings with payment_date <= cutoff.
-- Re-runnable: functions are "create or replace", output tables are dropped and rebuilt by run().
--
-- mart.chain_events_asof(cutoff)  one row per life-cycle event of a known claim, in claim order
-- mart.chain_claim_asof(cutoff)   one row per claim: payer, billed, state at cutoff, first payment
-- mart.chain_transition_count     transition counts by payer   (written by markov.run)
-- mart.chain_matrix               estimated transition matrix  (written by markov.run)
-- mart.chain_absorption           N, B and expected steps per transient state (written by markov.run)

-- claim header: facts fixed at submission (payer, first submission date, total charge)
create or replace function mart.chain_claim_header(p_cutoff date)
returns table (claim_id text, payer_id text, submission_date date, billed numeric, n_lines integer)
language sql stable as $$
    select c.claim_id, min(c.payer_id), min(c.submission_date), sum(c.line_charge), count(*)::int
    from core.fact_claim_line c
    group by c.claim_id
    having min(c.submission_date) <= p_cutoff
$$;

create or replace function mart.chain_events_asof(p_cutoff date)
returns table (claim_id text, payer_id text, submission_date date, seq integer, status text,
               event_ts timestamp, event_date date, reason_category text)
language sql stable as $$
    select e.claim_id, h.payer_id, h.submission_date,
           (row_number() over (partition by e.claim_id order by e.event_ts, e.claim_event_id))::int,
           e.status, e.event_ts, e.event_date, e.reason_category
    from core.fact_claim_event e
    join mart.chain_claim_header(p_cutoff) h on h.claim_id = e.claim_id
    where e.event_date <= p_cutoff
$$;

-- payer postings per claim up to the cutoff (file duplicates excluded, reversals net out)
create or replace function mart.chain_paid_asof(p_cutoff date)
returns table (claim_id text, paid_to_date numeric, first_payment_date date, n_postings integer)
language sql stable as $$
    select c.claim_id, sum(r.paid_amount), min(r.payment_date) filter (where r.paid_amount > 0), count(*)::int
    from core.fact_remit_line r
    join recon.claim_remit_match m on m.remit_line_id = r.remit_line_id
    join core.fact_claim_line c on c.claim_line_id = m.claim_line_id
    where r.payment_date <= p_cutoff
      and not r.is_provider_level            -- PLB rows are provider-level, not claim payments
      and not exists (select 1 from recon.remit_duplicate d where d.remit_line_id = r.remit_line_id)
      and c.submission_date <= p_cutoff
    group by c.claim_id
$$;

create or replace function mart.chain_claim_asof(p_cutoff date)
returns table (claim_id text, payer_id text, submission_date date, billed numeric, n_lines integer,
               state text, state_since date, first_paid_event_date date, ever_denied boolean,
               ever_pended boolean, n_events integer, paid_to_date numeric, first_payment_date date)
language sql stable as $$
    with ev as (select * from mart.chain_events_asof(p_cutoff)),
    last_ev as (
        select distinct on (claim_id) claim_id, status, event_date
        from ev order by claim_id, seq desc
    ),
    agg as (
        select claim_id,
               min(event_date) filter (where status = 'PAID') as first_paid_event_date,
               bool_or(status = 'DENIED') as ever_denied,
               bool_or(status = 'PENDED') as ever_pended,
               count(*)::int as n_events
        from ev group by claim_id
    )
    select h.claim_id, h.payer_id, h.submission_date, h.billed, h.n_lines,
           coalesce(l.status, 'SUBMITTED'), coalesce(l.event_date, h.submission_date),
           a.first_paid_event_date, coalesce(a.ever_denied, false), coalesce(a.ever_pended, false),
           coalesce(a.n_events, 0), coalesce(p.paid_to_date, 0), p.first_payment_date
    from mart.chain_claim_header(p_cutoff) h
    left join last_ev l on l.claim_id = h.claim_id
    left join agg a on a.claim_id = h.claim_id
    left join mart.chain_paid_asof(p_cutoff) p on p.claim_id = h.claim_id
$$;

-- output tables (rebuilt by markov.run)
drop table if exists mart.chain_transition_count;
create table mart.chain_transition_count (
    cutoff_date date not null,
    payer_id    text not null,          -- 'ALL' = pooled over payers
    from_state  text not null,
    to_state    text not null,
    n           integer not null,
    label       text not null default 'synthetic_record',
    primary key (cutoff_date, payer_id, from_state, to_state)
);

drop table if exists mart.chain_matrix;
create table mart.chain_matrix (
    cutoff_date   date not null,
    payer_id      text not null,
    from_state    text not null,
    to_state      text not null,
    probability   double precision not null,
    row_n         integer not null,     -- exits observed from from_state for this payer
    pooled_row    boolean not null,     -- true if the row was too sparse and uses the pooled row
    label         text not null default 'model_estimate',
    primary key (cutoff_date, payer_id, from_state, to_state)
);

drop table if exists mart.chain_absorption;
create table mart.chain_absorption (
    cutoff_date            date not null,
    payer_id               text not null,
    from_state             text not null,
    p_paid                 double precision not null,
    p_written_off          double precision not null,
    p_paid_lo              double precision,   -- bootstrap percentile interval (from SUBMITTED only)
    p_paid_hi              double precision,
    expected_steps         double precision not null,
    sd_steps               double precision not null,
    label                  text not null default 'model_estimate',
    primary key (cutoff_date, payer_id, from_state)
);
