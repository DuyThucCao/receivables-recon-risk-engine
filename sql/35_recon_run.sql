-- 35_recon_run.sql
-- recon.run_all(kind): the whole reconciliation in order. Called once by the full build and
-- again by the intake helper after new rows land (kind = 'INCREMENTAL').
create or replace function recon.run_all(p_kind text default 'FULL') returns jsonb language plpgsql as $$
declare v_run bigint; out jsonb := '{}'::jsonb;
begin
    insert into recon.run_log (kind) values (p_kind) returning run_id into v_run;
    out := out || jsonb_build_object('duplicates', recon.run_matching('DUPLICATE', v_run));
    out := out || jsonb_build_object('claim_remit', recon.run_matching('CLAIM_REMIT', v_run));
    out := out || jsonb_build_object('reversals', recon.run_matching('REVERSAL', v_run));
    out := out || jsonb_build_object('traces', recon.build_traces());
    out := out || jsonb_build_object('remit_bank', recon.run_matching('REMIT_BANK', v_run));
    out := out || jsonb_build_object('balances', recon.build_balances());
    out := out || jsonb_build_object('line_status', recon.build_line_status());
    out := out || jsonb_build_object('exceptions', recon.build_exceptions(v_run));
    out := out || jsonb_build_object('controls', recon.build_control_totals(v_run));
    out := out || jsonb_build_object('daily_recon_rows', recon.build_daily_recon());
    drop table if exists _todo; drop table if exists _tr; drop table if exists _bk;
    -- freshly rebuilt tables have no planner statistics until autovacuum gets to them; close_day
    -- runs hundreds of small queries against them right after, so analyze explicitly (see sql_notes)
    analyze recon.claim_remit_match; analyze recon.remit_bank_match; analyze recon.remit_duplicate;
    analyze recon.reversal_link; analyze recon.remit_trace; analyze recon.claim_line_balance;
    analyze recon.claim_line_status_history; analyze recon.claim_line_status; analyze recon.exception_queue;
    analyze recon.daily_recon; analyze core.fact_remit_adjustment; analyze core.fact_claim_event;
    update recon.run_log set summary = out where run_id = v_run;
    return out;
end $$;

select recon.run_all('FULL');
