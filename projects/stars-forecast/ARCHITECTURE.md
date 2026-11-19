# Stars forecast writer

The grid loader imports the hub's light-pollution grid from R2 every six hours. On every Argo CD sync, the `inputs` hook imports the grid and the monthly CERRA climatology before the refresh CronJob syncs. The loader and the hook alone receive read-only R2 source credentials. The weather writer reads both inputs from private Garage storage and starts up to 15 met.no requests per second, with at most 15 in flight. It replaces successful forecasts in 64 site-ID shards. Failed sites retain their hours and fetch time. Refresh removes elapsed hours from each shard before writing it. Only the refresh job writes forecast shards, and its concurrency policy forbids overlapping scheduled runs.

The writer filters elapsed hours and counts clear windows using the hub's rounded solar elevation. It ranks sites before capping their visible windows at 25. It encodes each public dataset one site at a time to temporary disk, hashes those exact bytes and uploads them with immutable caching. A final root manifest references the three uploads. A source mirror in the `factory-public` release transfers those objects in the same order to prefix `stars-forecast/` of R2 bucket `factory-public`. This namespace holds no R2 write credential (HOME-492). The site snapshot verifies each digest before updating its section index.

```mermaid
flowchart LR
  R2[Stars input bucket] --> Loader --> State[Private Garage bucket]
  MET[met.no] --> Writer
  State --> Writer --> Sink[Public Garage bucket]
  Sink -- read-only key --> Mirror[factory-public source mirror] --> Public[R2 factory-public/stars-forecast/]
  Public --> Snapshot[Site snapshot]
```

| Component | Untrusted input | Credentials held |
| --- | --- | --- |
| Grid and climatology loader | R2 objects | Read-only R2 source and private Garage key |
| Forecast writer | Weather responses and private inputs | Scoped Garage state and publication keys |
| Source mirror, in namespace `factory-public` | Public Garage objects | Garage read key and the `factory-public` R2 token |

## Direction

The site will resolve each generation from the mirrored manifest once the public objects and their cache policies have been verified.
