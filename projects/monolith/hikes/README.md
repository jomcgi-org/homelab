# Recover the walk catalogue

`seed/walks_seed.sql`, `seed/seed-prod.sh` and `tools/generate_seed.py` are
retained for disaster-recovery reseeding (#7030). Bulk corpus data stays outside
`chart/migrations/`: it previously exceeded the migrations ConfigMap's
client-side annotation limit.

## Use the retained snapshot

The checked-in SQL contains the legacy WalkHighlands catalogue. It inserts
walk identity, route details and coordinates into `hikes.walks`. It does not
restore forecasts or a full database backup. The target must have the current
hikes schema and its defaults installed through Atlas first.

Each insert uses `ON CONFLICT (uuid) DO NOTHING`. Replaying the snapshot fills
missing walks and preserves existing rows, including newer scraped details and
forecast timestamps. It does not refresh existing walks. PostgreSQL regression
coverage loads the retained snapshot against the current migration chain and
replays it; this is test-database evidence, not a production recovery rehearsal.

`seed/seed-prod.sh` is an operator-only database mutation through `kubectl exec`
and `psql -U postgres`. Running it requires separate operational authorization,
hub access and a verified writable Postgres primary. Inspect its `NS`, `PG_POD`
and `DB` settings against the intended target; the hardcoded defaults are not
discovery. The script uses `ON_ERROR_STOP` and prints the resulting row count.
It does not wrap the entire multi-batch load in a transaction. After a partial
failure, inspect the error and target before an authorized replay. This task
does not authorize running the loader.

## Regenerate from an available SQLite corpus

Regeneration needs Python 3 and an operator-supplied SQLite file with a `walks`
table containing `uuid`, `name`, `url`, `distance_km`, `ascent_m`, `duration_h`,
`summary`, `latitude` and `longitude`. Supply a real input path explicitly:

```bash
python3 projects/monolith/hikes/tools/generate_seed.py \
  --db /absolute/path/to/recovered-walks.db \
  --out /tmp/walks-recovery.sql
```

The example input is a placeholder. No source SQLite database is checked in.
The generator's legacy default, `projects/hikes/scrape_walkhighlands/walks.db`,
points into a retired project and is unusable in a fresh checkout. The generator
opens its input read-only, orders rows by UUID and emits deterministic batches.
A NULL summary becomes an empty string; rows with any other NULL source column
are skipped and reported on stderr. Review emitted/skipped counts and the SQL
before replacing the retained snapshot. The legacy `viable_dates` is ignored.

## Keep walks and forecasts current

Catalogue refresh runs through `hikes-scrape-walks`, the Argo CronWorkflow
calling `hikes.jobs.scrape_walks_handler`. The current chart suspends its schedule
and retains it for authorized manual submission when a refresh is wanted.
Its scraper fetches WalkHighlands and
upserts route fields and `scraped_at` while preserving weather rows. The
`hikes-refresh-forecasts` job refreshes met.no windows every two hours and
materializes the connector snapshot. Reseeding does not replace either job.
