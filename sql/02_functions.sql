-- 02_functions.sql
-- Parsing and normalisation helpers used by staging. They return NULL instead of failing, and
-- staging turns a NULL where a value was required into a reject with a reason.

create or replace function stg.clean(t text) returns text
language sql immutable parallel safe as $$ select nullif(btrim(t), '') $$;

-- '1234.5', '1,234.50', '$1,234.50', '-12' -> numeric(14,2); anything else -> NULL
create or replace function stg.parse_money(t text) returns numeric(14,2)
language sql immutable parallel safe as $$
    select case when s ~ '^-?[0-9]+(\.[0-9]{1,2})?$' then s::numeric(14,2) end
    from (select regexp_replace(btrim(t), '[$, ]', '', 'g') as s) x
$$;

-- general decimal (units, rates); '58.00%' -> 58.00
create or replace function stg.parse_decimal(t text) returns numeric
language sql immutable parallel safe as $$
    select case when s ~ '^-?[0-9]+(\.[0-9]+)?$' then s::numeric end
    from (select regexp_replace(btrim(t), '[$,% ]', '', 'g') as s) x
$$;

create or replace function stg.parse_int(t text) returns integer
language sql immutable parallel safe as $$
    select case when btrim(t) ~ '^-?[0-9]{1,9}$' then btrim(t)::integer end
$$;

-- dates seen in the exports: YYYY-MM-DD, YYYYMMDD (835), MM/DD/YYYY, MM/YYYY (expiry = month end)
-- not parallel safe: the exception block opens a subtransaction
create or replace function stg.parse_date(t text) returns date
language plpgsql immutable as $$
declare s text := btrim(t);
begin
    if s is null or s = '' then return null; end if;
    if s ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then return to_date(s, 'YYYY-MM-DD');
    elsif s ~ '^[0-9]{8}$' then return to_date(s, 'YYYYMMDD');
    elsif s ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$' then return to_date(s, 'MM/DD/YYYY');
    elsif s ~ '^[0-9]{1,2}/[0-9]{4}$' then
        return (to_date('01/' || s, 'DD/MM/YYYY') + interval '1 month' - interval '1 day')::date;
    end if;
    return null;
exception when others then
    return null;
end $$;

create or replace function stg.parse_ts(t text) returns timestamp
language plpgsql immutable as $$
declare s text := btrim(t);
begin
    if s is null or s = '' then return null; end if;
    if s ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$' then
        return s::timestamp;
    elsif s ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4} [0-9]{1,2}:[0-9]{2} ?(AM|PM|am|pm)$' then
        return to_timestamp(s, 'MM/DD/YYYY HH12:MI AM')::timestamp;
    elsif s ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then
        return s::timestamp;
    end if;
    return null;
exception when others then
    return null;
end $$;

-- Claim identifier normalisation: upper case, keep letters and digits, drop known prefixes
-- (PCN, CLM, CLAIM, REF) and leading zeros. 'PCN:mhs-24-0001234' and '000MHS240001234' -> 'MHS240001234'.
create or replace function stg.norm_id(t text) returns text
language sql immutable parallel safe as $$
    select nullif(ltrim(regexp_replace(upper(regexp_replace(coalesce(t, ''), '[^A-Za-z0-9]', '', 'g')),
                                       '^(PCN|CLAIM|CLM|REF)+', ''), '0'), '')
$$;

-- Member identifier normalisation: upper case letters and digits only.
create or replace function stg.norm_member(t text) returns text
language sql immutable parallel safe as $$
    select nullif(upper(regexp_replace(coalesce(t, ''), '[^A-Za-z0-9]', '', 'g')), '')
$$;

-- Trace number from an ACH description: '... TRN*1*1234567890*1900000101\' -> '1234567890'
create or replace function stg.extract_trace(t text) returns text
language sql immutable parallel safe as $$ select (regexp_match(t, 'TRN\*1\*([0-9A-Za-z]+)'))[1] $$;

create or replace function stg.extract_item_count(t text) returns integer
language sql immutable parallel safe as $$ select ((regexp_match(t, 'ITEMS\s+([0-9]+)'))[1])::integer $$;
