# Public app date grouping

Stars converts the same forecast hour to a viewing night for thousands of
sites. Trips groups every photo by its trip's timezone. Each conversion
previously constructed an ICU date formatter.

Stars now reuses one London formatter and keeps up to 1,024 timestamp results.
Trips reuses up to 32 timezone formatters. Both caches are bounded. The night
calculation still subtracts twelve absolute hours before formatting in London;
the trip calculation still groups by the trip's IANA zone and falls back to
UTC for an invalid zone.

## Local measurements

Measured on 2026-11-14 against base
`1f536d94934ccfef73aa3a2129f00be60f8a44ca`, using Node v24.22.1 on the task
host. Each sample ran in a fresh Node process. Parsing the input and importing
the module happened before timing; hashing the complete output happened after.
Three processes per version used the same saved public production data.

| Function | Input | Before, ms | Changed, ms | Median before, ms | Median changed, ms |
| --- | --- | --- | --- | --- | --- |
| `starsNights` | 14,234 sites, 94,586 windows, 31 distinct timestamps | 8598.77, 6641.52, 6313.51 | 78.88, 41.65, 46.11 | 6641.52 | 46.11 |
| `groupByDay` | 4,838 points from `2025-liard-hot-springs` | 251.65, 239.95, 295.96 | 25.62, 54.23, 31.46 | 251.65 | 31.46 |

The stars input came from the data inline in `/app/stars` at 22:35 UTC. Its
clock was fixed to `2026-11-14T22:35:00Z`. The trip input came from the data
inline in `/app/trips/2025-liard-hot-springs`. The full output's JSON SHA-256
matched across all six runs for each function:

| Function | Output SHA-256 |
| --- | --- |
| `starsNights` | `a85ffffec366065b9aba69c84e71ab076ca7875fcccca4549165341f931717f2` |
| `groupByDay` | `3633c83ad63f05c4e4be48cce3b5eff292ded012847fecf16ee42215f7613b8c` |

These are local CPU measurements. They do not establish a production or mobile
page speedup. Network, LCP and interaction measurements need a verified rollout
and a fresh browser baseline.

## Regression checks

The existing frontend `:test` target includes the modified helper tests and
the stars/trips page server, SSR and DOM tests. The targeted run passed 137
tests across 14 files:

```sh
cd projects/monolith/frontend
node node_modules/vitest/vitest.mjs run src/routes/public/app/stars src/routes/public/app/trips src/lib/public/stars src/lib/trips
```

The helper tests cover London daylight-saving boundaries, equivalent timestamp
offsets, advancing forecast horizons, Vancouver day boundaries, timezone
switches, and the existing invalid-date and invalid-zone fallbacks.
