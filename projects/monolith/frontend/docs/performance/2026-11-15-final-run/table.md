Page cohort: keyed (__public_perf_page=run-43671642559-attempt-2), reused across runs; actual edge states below. Do not combine with canonical cohorts. Redirect destinations that drop the key are marked key-not-retained in JSON and are not a fresh-key destination observation.

| Route | Runs | Complete profile | Header arrival ms | Raw nav TTFB ms | Visible content ms | LCP ms | CLS | Lab interaction ms | Lab samples | INP | Transfer KiB | JS KiB | Requests | Edge |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |
| /app/hikes | 3/3 | 0/3 | 184 | 35 | 1061 | 1064 | 0.003 | 24 | 3/3 | unavailable | unavailable | unavailable | 85 | MISS, HIT, HIT |
| /app/ships | 3/3 | 0/3 | 184 | 33 | 1032 | 1036 | 0.003 | 8 | 3/3 | unavailable | unavailable | unavailable | 73 | MISS, HIT, HIT |
| /app/stars | 3/3 | 0/3 | 183 | 34 | 1047 | 1048 | 0.002 | 304 | 3/3 | unavailable | unavailable | unavailable | 82 | MISS, HIT, HIT |
| /app/trips | 3/3 | 3/3 | 184 | 31 | unavailable | 1380 | 0.000 | unavailable | 0/3 | unavailable | 187.4 | 76.5 | 49 | MISS, HIT, HIT |
| /scotland | 3/3 | 3/3 | 184 | 36 | unavailable | 868 | 0.000 | unavailable | 0/3 | unavailable | 161.0 | 92.2 | 51 | BYPASS, BYPASS, BYPASS |
| /app/llm-leaderboard | 3/3 | 3/3 | 183 | 32 | unavailable | 876 | 0.000 | unavailable | 0/3 | unavailable | 163.4 | 83.7 | 45 | MISS, HIT, HIT |
| /app/notes | 3/3 | 3/3 | 183 | 28 | 825 | 828 | 0.001 | unavailable | 0/3 | unavailable | 247.4 | 116.1 | 58 | BYPASS, BYPASS, BYPASS |
| /app/grimoire | 3/3 | 3/3 | 214 | 38 | unavailable | 2164 | 0.003 | unavailable | 0/3 | unavailable | 318.9 | 114.3 | 63 | BYPASS, BYPASS, BYPASS |
| /app/frank | 3/3 | 3/3 | 168 | 39 | unavailable | 288 | 0.000 | unavailable | 0/3 | unavailable | unavailable | 10.2 | 8 | MISS, HIT, HIT |
| /app/firecracker | 3/3 | 3/3 | 341 | 189 | unavailable | 1048 | 0.000 | unavailable | 0/3 | unavailable | unavailable | 84.9 | 55 | BYPASS, BYPASS, BYPASS |
| /ember/firecracker | 3/3 | 3/3 | 183 | 28 | unavailable | 888 | 0.000 | unavailable | 0/3 | unavailable | 154.0 | 84.9 | 54 | BYPASS, BYPASS, BYPASS |
| /scotland/privacy | 3/3 | 3/3 | 183 | 28 | unavailable | 872 | 0.000 | unavailable | 0/3 | unavailable | 160.7 | 93.4 | 53 | BYPASS, BYPASS, BYPASS |
| /scotland/terms | 3/3 | 3/3 | 177 | 25 | unavailable | 860 | 0.000 | unavailable | 0/3 | unavailable | 159.0 | 92.3 | 52 | BYPASS, BYPASS, BYPASS |
| /scotland/support | 3/3 | 3/3 | 183 | 29 | unavailable | 864 | 0.000 | unavailable | 0/3 | unavailable | 159.8 | 93.3 | 53 | BYPASS, BYPASS, BYPASS |
| /app/trips/2025-liard-hot-springs | 3/3 | 0/3 | 192 | 29 | 1114 | 6396 | 0.000 | unavailable | 0/3 | unavailable | unavailable | unavailable | 86 | MISS, HIT, HIT |
| /app/trips/2025-liard-hot-springs/timeline | 3/3 | 3/3 | 191 | 32 | unavailable | 1648 | 0.000 | unavailable | 0/3 | unavailable | 855.1 | 89.3 | 68 | MISS, HIT, HIT |
| /app/trips/2025-liard-hot-springs/day/1 | 3/3 | 0/3 | 184 | 44 | 1115 | 2036 | 0.000 | 104 | 3/3 | unavailable | unavailable | unavailable | 76 | MISS, HIT, HIT |

Normal-key endpoints

| Public endpoint | Successful samples | p50 full response ms | p95 full response ms | Edge samples |
| --- | ---: | ---: | ---: | --- |
| /app/hikes/__data.json | 20/20 | 25 | 62 | EXPIRED: 1, HIT: 19 |
| /app/hikes/walk/7fc20756-89c2-5280-9be9-3ddb3db01e44 | 20/20 | 9 | 35 | EXPIRED: 1, HIT: 19 |
| /app/ships/__data.json | 20/20 | 26 | 60 | EXPIRED: 1, HIT: 19 |
| /app/ships/track/369514002 | 20/20 | 10 | 37 | EXPIRED: 1, HIT: 19 |
| /app/stars/__data.json | 20/20 | 28 | 48 | EXPIRED: 1, HIT: 19 |
| /app/trips/__data.json | 20/20 | 9 | 17 | EXPIRED: 1, HIT: 19 |
| /app/stars/map | 20/20 | 14 | 24 | EXPIRED: 1, HIT: 19 |
| /app/stars/history-map | 20/20 | 17 | 36 | HIT: 20 |
| /app/stars/history | 20/20 | 18 | 29 | HIT: 20 |
| /app/ships/heat | 20/20 | 22 | 44 | EXPIRED: 1, HIT: 19 |
| /app/notes/graph | 20/20 | 13 | 29 | EXPIRED: 1, HIT: 19 |
| /app/notes/stats | 20/20 | 10 | 12 | UPDATING: 14, HIT: 6 |
| /scotland/marketplace.json | 20/20 | 9 | 12 | UPDATING: 12, HIT: 8 |

Unique-query probes (observed edge state)

| Public endpoint | Successful samples | p50 full response ms | p95 full response ms | Edge samples |
| --- | ---: | ---: | ---: | --- |
| /app/hikes/__data.json | 20/20 | 817 | 924 | MISS: 20 |
| /app/hikes/walk/7fc20756-89c2-5280-9be9-3ddb3db01e44 | 20/20 | 125 | 141 | MISS: 20 |
| /app/ships/__data.json | 20/20 | 822 | 968 | MISS: 20 |
| /app/ships/track/369514002 | 20/20 | 128 | 141 | MISS: 20 |
| /app/stars/__data.json | 20/20 | 1063 | 1155 | MISS: 20 |
| /app/trips/__data.json | 20/20 | 125 | 135 | MISS: 20 |
| /app/stars/map | 20/20 | 284 | 323 | MISS: 20 |
| /app/stars/history-map | 20/20 | 354 | 469 | MISS: 20 |
| /app/stars/history | 20/20 | 1263 | 1670 | MISS: 20 |
| /app/ships/heat | 20/20 | 1699 | 2181 | MISS: 20 |
| /app/notes/graph | 20/20 | 133 | 143 | MISS: 20 |
| /app/notes/stats | 20/20 | 122 | 137 | MISS: 20 |
| /scotland/marketplace.json | 20/20 | 115 | 122 | MISS: 20 |
