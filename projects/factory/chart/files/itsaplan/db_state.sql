-- itsaplan-secret-init's db-state step: the number of accounts, settings and
-- stored secrets, read-only. Each table counts 0 until the migrations create
-- it, so an empty database reads 0.
select coalesce(sum(n), 0)
from (
  select case
    when to_regclass(t) is null then 0
    else (xpath(
      '/row/n/text()',
      query_to_xml('select count(*) as n from ' || t, false, true, '')
    ))[1]::text::bigint
  end as n
  from unnest(array['public."user"', 'public.app_setting', 'public.app_secret']) as t
) counts;
