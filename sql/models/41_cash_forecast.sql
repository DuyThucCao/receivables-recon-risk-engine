-- 41_cash_forecast.sql  (owner: chain, cash and credit; executed by recon_engine.models.cashflow.run)
-- Question 3 (Treasurer): will we be short of cash in the next 13 weeks?
--
-- recon.remit_bank_match can hold several rows per trace_key (one payment split over deposits,
-- rule RB25_TRACE_SPLIT): deposit lags are measured per (payment, deposit) row and weighted by
-- allocated_amount; "deposited" means at least one deposit row posted on or before the date.
-- Point-in-time inputs (every function reads only rows dated on or before the forecast date):
--   mart.cash_deposit_lag_asof(f, lookback)  days from payer payment date to bank posting, per matched payment
--   mart.cash_in_transit_asof(f)             payments the payer has made (remittance received) that are not yet in the bank
--   mart.cash_opening_balance(f)             bank running balance at the end of day f
--   mart.cash_payer_receipts(d0, d1)         payer deposits (EFT + lockbox) per posting day; used for backtest actuals
-- Output tables (rebuilt by cashflow.run):
--   mart.cash_forecast           weekly forecast distribution at the headline forecast date
--   mart.cash_forecast_backtest  the same at past forecast dates, with the actual receipts
--   mart.cash_forecast_lag       payment-lag survival curve (Kaplan-Meier) and the curve used in simulation

create or replace function mart.cash_deposit_lag_asof(p_f date, p_lookback integer)
returns table (payer_id text, payment_method text, lag_days integer, amount numeric)
language sql stable as $$
    select t.payer_id, t.payment_method, (b.posting_date - t.payment_date)::int, m.allocated_amount
    from recon.remit_bank_match m
    join recon.remit_trace t on t.trace_key = m.trace_key
    join core.fact_bank_txn b on b.bank_txn_id = m.bank_txn_id
    where t.payment_date between p_f - p_lookback and p_f - 14   -- lags of up to 14 days fully observed
      and b.posting_date <= p_f
      and t.header_total > 0
$$;

create or replace function mart.cash_in_transit_asof(p_f date)
returns table (trace_key text, payer_id text, payment_date date, amount numeric)
language sql stable as $$
    select t.trace_key, t.payer_id, t.payment_date, t.header_total
    from recon.remit_trace t
    where t.payment_date between p_f - 14 and p_f
      and t.header_total > 0
      and t.payment_method in ('ACH', 'CHK')
      and not exists (select 1 from recon.remit_bank_match m join core.fact_bank_txn b on b.bank_txn_id = m.bank_txn_id
                      where m.trace_key = t.trace_key and b.posting_date <= p_f)
$$;

create or replace function mart.cash_opening_balance(p_f date)
returns numeric language sql stable as $$
    select running_balance from core.fact_bank_txn where posting_date <= p_f
    order by posting_date desc, bank_txn_id desc limit 1
$$;

create or replace function mart.cash_payer_receipts(p_d0 date, p_d1 date)
returns table (posting_date date, amount numeric)
language sql stable as $$
    select posting_date, sum(amount) from core.fact_bank_txn
    where txn_category in ('PAYER_EFT', 'LOCKBOX') and posting_date between p_d0 and p_d1
    group by posting_date
$$;

drop table if exists mart.cash_forecast;
create table mart.cash_forecast (
    forecast_date   date not null,
    week_no         integer not null,
    week_start      date not null,      -- first day of the week (forecast_date + 7*(week_no-1) + 1)
    week_end        date not null,
    mean            numeric(14,2) not null,
    p02_5           numeric(14,2) not null,
    p10             numeric(14,2) not null,
    p50             numeric(14,2) not null,
    p90             numeric(14,2) not null,
    p97_5           numeric(14,2) not null,
    from_open_claims numeric(14,2) not null,   -- expected receipts from claims open at forecast_date
    from_new_claims  numeric(14,2) not null,   -- expected receipts from claims submitted after forecast_date
    from_in_transit  numeric(14,2) not null,   -- remitted but not yet deposited at forecast_date
    cash_need        numeric(14,2) not null,   -- financial_assumption
    p_balance_below_minimum double precision,  -- P(cumulative position below the minimum balance by week end)
    actual          numeric(14,2),             -- payer deposits in the bank, when the week is observed
    label           text not null default 'model_estimate',
    primary key (forecast_date, week_no)
);

drop table if exists mart.cash_forecast_backtest;
create table mart.cash_forecast_backtest (
    lag_model       text not null,
    forecast_date   date not null,
    week_no         integer not null,
    mean            numeric(14,2) not null,
    p02_5           numeric(14,2) not null,
    p10             numeric(14,2) not null,
    p90             numeric(14,2) not null,
    p97_5           numeric(14,2) not null,
    actual          numeric(14,2) not null,
    in_80           boolean not null,
    in_95           boolean not null,
    label           text not null default 'model_estimate',
    primary key (lag_model, forecast_date, week_no)
);

drop table if exists mart.cash_forecast_lag;
create table mart.cash_forecast_lag (
    forecast_date   date not null,
    payer_id        text not null,
    day             integer not null,
    km_survival     double precision,          -- weighted Kaplan-Meier P(T > day | eventually paid)
    sim_cdf         double precision not null, -- curve used in the simulation (KM body, parametric tail)
    risk_set        double precision,
    label           text not null default 'model_estimate',
    primary key (forecast_date, payer_id, day)
);
