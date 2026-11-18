# Factory public mirror

This chart owns the Garage bucket `factory-public` and its mirror to the
Cloudflare R2 bucket `factory-public`, which is public at
`https://factory-data.jomcgi.dev` (HOME-243). It is the first consumer of the
factory Helm library's `buckets:` field
([library README](../shared/helm/factory-library/README.md), HOME-137).
Argo CD deploys it in namespace `factory-public` through
`projects/platform-factory/kustomization.yaml`.

```mermaid
flowchart LR
  publisher["Publisher (HOME-213)"] -- write --> garage[("Garage factory-public")]
  garage -- "rclone, every minute" --> r2[("R2 factory-public")]
  r2 --> cdn["factory-data.jomcgi.dev"]
```

## Values

The chart has one `buckets:` entry in `chart/values.yaml`. It renders:

- the Garage bucket claim and the key Secret, which garage-init fills;
- the mirror CronJob (stock `rclone/rclone`, pinned by digest), under its own
  ServiceAccount with no token mounted;
- the `OnePasswordItem` for `vaults/k8s-homelab/items/factory-r2-public-mirror`
  (fields `access-key-id`, `secret-access-key` and `s3-api-endpoint`);
- a network policy that allows egress only to Garage on 3900, the R2 S3
  endpoint on 443, and DNS;
- an admission rule that lets only the mirror's ServiceAccount mount the R2
  Secret.

Each run syncs every object except `manifest.json` with
`Cache-Control: public, max-age=31536000, immutable`, then copies
`manifest.json` with `Cache-Control: public, max-age=60`.

The publisher gets write access through its own `buckets:` entry without
`public:`. Only this chart owns `public:` for this destination.

## Before the mirror runs

The CronJob ships with `public.suspend: true`. `rclone sync` deletes from R2
every object that Garage does not have, so check these first and record the
result on HOME-243, never with credential values:

1. The 1Password operator syncs the item into the Secret with the three fields.
2. The endpoint is
   `https://7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com`, and
   the token can read and write objects only in R2 bucket `factory-public`.
3. An authenticated listing shows that R2 bucket `factory-public` holds
   nothing that must be kept.
4. The mirror is the only writer to R2 bucket `factory-public`. Every key
   has one writer, and here that writer is the mirror. Another job that
   writes to the R2 bucket directly loses its objects at the next sync. The
   trips export (`projects/monolith/trips/export`) writes `trips/photos/`
   to R2 `factory-public` directly when `TRIPS_EXPORT_PUBLIC_ENABLED` is
   `true`. Check that `projects/monolith/chart/values.yaml` sets it to
   `false`, and keep it `false` while the mirror runs.

Then set `public.suspend: false` in a PR.

**End state.** Every public object reaches R2 through Garage: a job writes
to the Garage bucket `factory-public` through its own `buckets:` entry, and
this mirror copies it to R2. The trips export moves its public photos to
Garage that way (HOME-242, HOME-258). Then no job writes to the R2 bucket
directly.

## Live check

After the mirror runs:

1. Argo CD shows the Application Synced and Healthy, and the key and R2
   Secrets have data.
2. A test object put into Garage `factory-public` appears at
   `https://factory-data.jomcgi.dev/<key>` within 2 minutes, with the
   immutable `Cache-Control`.
3. A `manifest.json` written after it appears with `max-age=60`.
4. After you delete both from Garage, the next run removes them from R2.
   Check with an authenticated listing, because the CDN can keep a cached
   copy.

## Validation

```bash
bb remote test --config=ci //projects/factory-public/...
```

`chart:public_test` renders the chart and checks the single `buckets:` entry,
the rendered resources and the Application. The admission rules are tested
in `//projects/shared/helm/factory-library/admission:admission_test`.
