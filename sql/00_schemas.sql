-- 00_schemas.sql
-- Rebuild from nothing: drop and recreate every schema. Files whose name starts with "0" run
-- BEFORE the raw files are landed; all other files run after (see recon_engine/pipeline.py).
drop schema if exists mart cascade;
drop schema if exists recon cascade;
drop schema if exists core cascade;
drop schema if exists stg cascade;
drop schema if exists raw cascade;

create schema raw;    -- files as landed, every column text
create schema stg;    -- typed, trimmed, normalised; rejected rows in stg.reject with a reason
create schema core;   -- one data model per business process (facts and dimensions)
create schema recon;  -- matching, balances, status, exceptions, controls, daily close
create schema mart;   -- outputs for the decision questions

comment on schema raw is 'Raw documents as landed: all columns text plus _file, _row_no, _loaded_at.';
comment on schema stg is 'Typed and normalised rows; rejects with reasons in stg.reject.';
comment on schema core is 'Per-process facts and dimensions (claims, remittances, cash, life cycle, drugs, contracts).';
comment on schema recon is 'Reconciliation: rules, matches, balances, claim-line status, exceptions, control totals, daily close.';
comment on schema mart is 'Decision outputs. Every table is registered in mart.table_label with one data label.';
